"""Filesystem tools: ``filesystem.list``, ``filesystem.read``, ``filesystem.write``.

Every call resolves the requested path first and then asks the permission
engine whether that path is inside ``permissions.filesystem.allowed_paths``.
Relative paths are resolved against the first allowed root, so the model can
only ever reach what the operator configured.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel
from ..core.permissions import PermissionRequest, is_inside
from .base import Tool, ToolContext, ToolError, ToolOutput

TEXT_SAMPLE = 2048
#: Flags used when the configuration refuses symlinks.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


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

    # -- safe file handling ------------------------------------------------ #

    def _real_path_of_fd(self, fd: int) -> Optional[Path]:
        """Best-effort real path of an open descriptor (Linux: /proc)."""
        try:
            return Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:  # pragma: no cover - not Linux, or fd already closed
            return None

    def _assert_still_inside(self, fd: int, path: Path, context: ToolContext) -> None:
        """Re-check containment *after* opening.

        The permission engine runs before the file is opened. Between the two,
        a directory component can be replaced by a symlink, so we resolve the
        descriptor and check again.
        """
        real = self._real_path_of_fd(fd)
        if real is None:  # pragma: no cover - non-Linux
            real = Path(os.path.realpath(str(path)))
        if not is_inside(real, context.config.allowed_roots):
            raise ToolError(f"path resolves outside the allowed paths: {real}")

    def _open_verified(
        self,
        path: Path,
        context: ToolContext,
        flags: int,
        *,
        directory: bool = False,
    ) -> int:
        """Open a path without following a late symlink, and verify it after.

        Returns an open file descriptor. The caller must close it.
        """
        policy = context.config.permissions.filesystem
        open_flags = flags | _NO_FOLLOW if not policy.follow_symlinks else flags
        if directory:
            open_flags |= _DIRECTORY

        # Inspect the path *before* opening it: opening a FIFO blocks until a
        # writer appears, which would hang the runtime past every timeout.
        try:
            pre = os.lstat(path)
        except FileNotFoundError:
            pre = None  # a new file (write) or a genuinely missing path
        except OSError as exc:
            raise ToolError(f"cannot access {path}: {exc.strerror or exc}") from exc
        if pre is not None:
            if stat.S_ISLNK(pre.st_mode) and not policy.follow_symlinks:
                raise ToolError(f"refusing to follow symlink: {path}")
            if not stat.S_ISLNK(pre.st_mode):
                if directory and not stat.S_ISDIR(pre.st_mode):
                    raise ToolError(f"not a directory: {path}")
                if not directory and not stat.S_ISREG(pre.st_mode):
                    raise ToolError(f"not a regular file: {path}")
        if not directory:
            # Belt and braces: a regular file ignores O_NONBLOCK, a FIFO does not.
            open_flags |= getattr(os, "O_NONBLOCK", 0)

        try:
            fd = os.open(path, open_flags, 0o644)
        except FileNotFoundError as exc:
            raise ToolError(f"no such file or directory: {path}") from exc
        except IsADirectoryError as exc:
            raise ToolError(f"is a directory: {path}") from exc
        except NotADirectoryError as exc:
            raise ToolError(f"not a directory: {path}") from exc
        except OSError as exc:
            raise ToolError(f"cannot open {path}: {exc.strerror or exc}") from exc

        try:
            info = os.fstat(fd)
            if not policy.follow_symlinks:
                # The descriptor and the path must still be the same inode.
                try:
                    on_disk = os.lstat(path)
                except OSError as exc:  # pragma: no cover - vanished mid-flight
                    raise ToolError(f"path disappeared while opening: {path}") from exc
                if (info.st_dev, info.st_ino) != (on_disk.st_dev, on_disk.st_ino) or stat.S_ISLNK(
                    on_disk.st_mode
                ):
                    raise ToolError(f"path changed while opening it: {path}")
            if directory and not stat.S_ISDIR(info.st_mode):
                raise ToolError(f"not a directory: {path}")
            if not directory and not stat.S_ISREG(info.st_mode):
                # FIFOs and devices would block forever or stream endlessly.
                raise ToolError(f"not a regular file: {path}")
            self._assert_still_inside(fd, path, context)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _ensure_parent(self, path: Path, context: ToolContext) -> None:
        """Create parent directories and confirm they are still inside the roots."""
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"cannot create directory {parent}: {exc.strerror or exc}") from exc
        real = Path(os.path.realpath(str(parent)))
        if not is_inside(real, context.config.allowed_roots):
            raise ToolError(f"path resolves outside the allowed paths: {real}")


def _write_all(fd: int, data: bytes, path: Path) -> None:
    """Write every byte: ``os.write`` is allowed to write less than asked."""
    written = 0
    while written < len(data):
        written += os.write(fd, memoryview(data)[written:])


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
        policy = context.config.permissions.filesystem
        # Opening the directory first means the listing cannot drift to another
        # directory if a component is swapped between check and use.
        fd = self._open_verified(path, context, os.O_RDONLY, directory=True)
        try:
            return self._list_fd(fd, path, policy)
        finally:
            os.close(fd)

    def _list_fd(self, fd: int, path: Path, policy: Any) -> ToolOutput:
        entries: List[Dict[str, Any]] = []
        with os.scandir(fd) as iterator:
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
        fd = self._open_verified(path, context, os.O_RDONLY)
        try:
            data = b""
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)

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
        existed = path.exists()
        if existed and path.is_dir():
            raise ToolError(f"is a directory: {path}")
        self._ensure_parent(path, context)

        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_APPEND if arguments.get("append") else os.O_TRUNC
        fd = self._open_verified(path, context, flags)
        try:
            _write_all(fd, encoded, path)
        except OSError as exc:
            raise ToolError(f"cannot write {path}: {exc.strerror or exc}") from exc
        finally:
            os.close(fd)

        return ToolOutput(
            output=f"wrote {len(encoded)} bytes to {path}",
            meta={
                "path": str(path),
                "bytes": len(encoded),
                "created": not existed,
                "appended": bool(arguments.get("append")),
            },
        )


# Backwards-compatible alias (AgentLite 0.1.0 exposed this name).
FilesystemTool = FilesystemListTool


def filesystem_tools() -> list:
    return [FilesystemListTool(), FilesystemReadTool(), FilesystemWriteTool()]
