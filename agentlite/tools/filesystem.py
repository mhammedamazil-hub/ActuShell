"""Filesystem tools: ``filesystem.list``, ``filesystem.read``, ``filesystem.write``.

Every call resolves the requested path first and then asks the permission
engine whether that path is inside ``permissions.filesystem.allowed_paths``.
Relative paths are resolved against the first allowed root, so the model can
only ever reach what the operator configured.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel
from ..core.permissions import PermissionRequest
from .base import Tool, ToolContext, ToolError, ToolOutput

TEXT_SAMPLE = 2048


class FilesystemToolBase(Tool):
    """Shared path resolution for the filesystem tools."""

    family = "filesystem"

    def _resolve(self, arguments: Dict[str, Any], context: ToolContext) -> Path:
        raw = str(arguments.get("path", ".") if arguments.get("path") is not None else ".")
        roots = context.config.allowed_roots
        candidate = Path(os.path.expanduser(raw))
        if not candidate.is_absolute():
            candidate = roots[0] / candidate
        resolved = Path(os.path.normpath(str(candidate)))
        return resolved

    def _symlinked(self, raw: Path, context: ToolContext) -> bool:
        """True if the path (or a parent below the root) is a symlink."""
        for root in context.config.allowed_roots:
            try:
                raw.relative_to(root)
            except ValueError:
                continue
            current = raw
            while True:
                if current.is_symlink():
                    return True
                if current == root or current.parent == current:
                    break
                current = current.parent
        return False

    def permission_request(
        self, arguments: Dict[str, Any], context: ToolContext
    ) -> PermissionRequest:
        path = self._resolve(arguments, context)
        return PermissionRequest(
            tool=self.name,
            action=self.action,
            family=self.family,
            risk=self.risk,
            summary=f"{self.action} {path}",
            path=path,
            meta={
                "raw_path": str(arguments.get("path")),
                "symlink": self._symlinked(path, context),
            },
        )

    action: str = "read"

    def timeout_for(self, arguments: Dict[str, Any]) -> int:
        return 10


class FilesystemListTool(FilesystemToolBase):
    name = "filesystem.list"
    action = "list"
    risk = RiskLevel.LOW
    description = (
        "List the contents of a directory inside the workspace. Returns one "
        "entry per line with its type and size. Use '.' for the workspace root."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list, relative to the workspace (default '.').",
            }
        },
        "required": [],
    }

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        path = self._resolve(arguments, context)
        if not path.exists():
            raise ToolError(f"no such directory: {path}")
        if not path.is_dir():
            raise ToolError(f"not a directory: {path}")

        policy = context.config.permissions.filesystem
        entries: List[Dict[str, Any]] = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    stat = entry.stat(follow_symlinks=False)
                    entries.append(
                        {
                            "name": entry.name
                            + ("/" if entry.is_dir(follow_symlinks=False) else ""),
                            "type": "dir" if entry.is_dir(follow_symlinks=False) else "file",
                            "size": stat.st_size,
                        }
                    )
                except OSError:
                    entries.append({"name": entry.name, "type": "unknown", "size": 0})

        entries.sort(key=lambda item: (item["type"] != "dir", item["name"]))
        truncated = len(entries) > policy.max_list_entries
        entries = entries[: policy.max_list_entries]

        lines = [
            f"{item['type'][:1].upper()}  {item['size']:>9}  {item['name']}" for item in entries
        ]
        return ToolOutput(
            output="\n".join(lines) if lines else "(empty directory)",
            meta={
                "path": str(path),
                "entries": entries,
                "count": len(entries),
                "truncated": truncated,
            },
        )


class FilesystemReadTool(FilesystemToolBase):
    name = "filesystem.read"
    action = "read"
    risk = RiskLevel.LOW
    description = (
        "Read a UTF-8 text file inside the workspace. Binary files and files "
        "larger than the configured limit are truncated or refused."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to read, relative to the workspace."},
            "max_bytes": {
                "type": "integer",
                "description": "Maximum number of bytes to return (clamped by configuration).",
                "minimum": 1,
            },
        },
        "required": ["path"],
    }

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        path = self._resolve(arguments, context)
        if not path.exists():
            raise ToolError(f"no such file: {path}")
        if path.is_dir():
            raise ToolError(f"is a directory: {path}")

        policy = context.config.permissions.filesystem
        limit = min(int(arguments.get("max_bytes") or policy.max_read_bytes), policy.max_read_bytes)
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        data = data[:limit]

        if b"\x00" in data[:TEXT_SAMPLE]:
            raise ToolError(f"refusing to read binary file: {path}")

        text = data.decode("utf-8", errors="replace")
        return ToolOutput(
            output=text,
            meta={"path": str(path), "bytes": len(data), "truncated": truncated},
        )


class FilesystemWriteTool(FilesystemToolBase):
    name = "filesystem.write"
    action = "write"
    risk = RiskLevel.MEDIUM
    description = (
        "Write UTF-8 text to a file inside the workspace, creating parent "
        "directories as needed. Existing files are overwritten."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to write, relative to the workspace."},
            "content": {"type": "string", "description": "Text content to write."},
            "append": {
                "type": "boolean",
                "description": "Append to the file instead of overwriting it.",
            },
        },
        "required": ["path", "content"],
    }

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        path = self._resolve(arguments, context)
        content = str(arguments.get("content", ""))
        policy = context.config.permissions.filesystem
        encoded = content.encode("utf-8")
        if len(encoded) > policy.max_write_bytes:
            raise ToolError(
                f"content is {len(encoded)} bytes, limit is {policy.max_write_bytes} "
                "(filesystem.max_write_bytes)"
            )
        if path.exists() and path.is_dir():
            raise ToolError(f"is a directory: {path}")

        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if arguments.get("append") else "wb"
        with open(path, mode) as handle:
            handle.write(encoded)

        return ToolOutput(
            output=f"wrote {len(encoded)} bytes to {path}",
            meta={
                "path": str(path),
                "bytes": len(encoded),
                "created": not existed,
                "appended": bool(arguments.get("append")),
            },
        )


# Convenience alias used by the registry and documentation.
FilesystemTool = FilesystemListTool


def filesystem_tools() -> list:
    return [FilesystemListTool(), FilesystemReadTool(), FilesystemWriteTool()]


def _unused(_: Optional[str]) -> None:  # pragma: no cover
    return None
