"""Terminal tool: ``terminal.run``.

Runs a command on the user's machine and returns stdout, stderr and the exit
code. Safety properties:

* no shell by default - the command is parsed with :mod:`shlex` and executed
  without a shell, so ``;``, ``&&``, ``|`` and backticks are just characters;
* the process runs in its own session so a timeout can kill the whole tree;
* environment variables that look like secrets are not passed down;
* the command string is checked against deny / confirm rules *before* it runs;
* working directory is confined to ``permissions.terminal.cwd`` by default.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.config import Config
from ..core.models import RiskLevel
from ..core.permissions import PermissionRequest
from .base import Tool, ToolContext, ToolError, ToolOutput

DEFAULT_TIMEOUT = 30


class TerminalTool(Tool):
    """Run a shell command inside the workspace."""

    name = "terminal.run"
    family = "terminal"
    risk = RiskLevel.HIGH
    description = (
        "Run a non-interactive shell command on the user's machine and return "
        "stdout, stderr and the exit code. The command runs in the workspace "
        "directory. Use it for inspecting files, running builds, tests and "
        "scripts. Dangerous commands require confirmation or are refused."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to execute, e.g. 'ls -la' or 'python3 -m pytest'.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Optional working directory, relative to the workspace "
                    "(defaults to the configured terminal.cwd)."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (clamped by the configuration).",
                "minimum": 1,
            },
        },
        "required": ["command"],
    }

    # -- permission -------------------------------------------------------- #

    def permission_request(
        self, arguments: Dict[str, Any], context: ToolContext
    ) -> PermissionRequest:
        command = str(arguments.get("command", "")).strip()
        cwd = self._resolve_cwd(arguments, context)
        policy = context.config.permissions.terminal
        if policy.allow_shell:
            argv: Optional[Sequence[str]] = None
        else:
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                raise ToolError(f"could not parse command: {exc}") from exc
            if not argv:
                raise ToolError("empty command")
        return PermissionRequest(
            tool=self.name,
            action="execute",
            family=self.family,
            risk=RiskLevel.HIGH,
            summary=f"run command: {_shorten(command)}",
            command=command,
            argv=argv,
            meta={"cwd": str(cwd), "shell": policy.allow_shell, "parsed": argv},
        )

    def timeout_for(self, arguments: Dict[str, Any]) -> int:
        requested = arguments.get("timeout")
        return int(requested) if isinstance(requested, int) and requested > 0 else DEFAULT_TIMEOUT

    # -- execution --------------------------------------------------------- #

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        policy = context.config.permissions.terminal
        command = str(arguments.get("command", ""))
        cwd = self._resolve_cwd(arguments, context)
        if not cwd.is_dir():
            raise ToolError(f"working directory does not exist: {cwd}")

        timeout = self._clamp_timeout(self.timeout_for(arguments), policy.max_timeout)
        env = build_environment(policy.env_denylist, policy.env_allowlist)

        if policy.allow_shell:
            argv: Any = ["/bin/sh", "-c", command]
        else:
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                raise ToolError(f"could not parse command: {exc}") from exc
            if not argv:
                raise ToolError("empty command")

        started = time.monotonic()
        try:
            process = subprocess.Popen(  # noqa: S603 - this is the point of the tool
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ToolError(f"command not found: {exc}") from exc
        except PermissionError as exc:
            raise ToolError(f"permission denied executing command: {exc}") from exc

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(process)
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_bytes, stderr_bytes = process.communicate(timeout=5)

        duration = time.monotonic() - started
        limit = max(1024, int(policy.max_output_bytes))
        stdout, stdout_cut = _decode_limited(stdout_bytes, limit)
        stderr, stderr_cut = _decode_limited(stderr_bytes, limit)

        meta: Dict[str, Any] = {
            "cwd": str(cwd),
            "duration_s": round(duration, 3),
            "timed_out": timed_out,
            "truncated": stdout_cut or stderr_cut,
            "shell": policy.allow_shell,
        }
        if stderr:
            meta["stderr"] = stderr

        if timed_out:
            return ToolOutput(
                output=stdout,
                error=f"command timed out after {timeout}s",
                exit_code=process.returncode if process.returncode is not None else -9,
                meta=meta,
            )

        exit_code = process.returncode
        error = None if exit_code == 0 else f"command exited with status {exit_code}"
        return ToolOutput(output=stdout, error=error, exit_code=exit_code, meta=meta)

    # -- helpers ----------------------------------------------------------- #

    def _resolve_cwd(self, arguments: Dict[str, Any], context: ToolContext) -> Path:
        base = context.config.terminal_cwd
        requested = arguments.get("cwd")
        if requested:
            candidate = Path(os.path.expanduser(str(requested)))
            base = candidate if candidate.is_absolute() else (base / candidate)
        return Path(os.path.normpath(str(base)))

    @staticmethod
    def _clamp_timeout(requested: int, maximum: int) -> int:
        return max(1, min(int(requested), max(1, int(maximum))))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def build_environment(denylist, allowlist) -> Dict[str, str]:
    """Filtered copy of the environment for spawned processes."""
    import re

    deny = [re.compile(p, re.IGNORECASE) for p in (denylist or [])]
    allow = {name.upper() for name in (allowlist or [])}
    env: Dict[str, str] = {}
    for name, value in os.environ.items():
        if name.upper() in allow:
            env[name] = value
            continue
        if any(pattern.search(name) for pattern in deny):
            continue
        env[name] = value
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("LANG", "C.UTF-8")
    env["AGENTLITE"] = "1"
    return env


def _kill_process_tree(process: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL if it refuses to die."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()


def _decode_limited(data: Optional[bytes], limit: int) -> tuple:
    if not data:
        return "", False
    truncated = len(data) > limit
    chunk = data[:limit] if truncated else data
    return chunk.decode("utf-8", errors="replace"), truncated


def _shorten(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def available(config: Config) -> bool:  # pragma: no cover - trivial
    return config.permissions.terminal.enabled
