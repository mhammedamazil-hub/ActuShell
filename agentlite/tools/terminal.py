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
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.models import RiskLevel
from ..core.permissions import PermissionRequest
from .base import Tool, ToolContext, ToolError, ToolOutput

DEFAULT_TIMEOUT = 30
READ_CHUNK = 65536
_KILL_GRACE_SECONDS = 5


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

    def timeout_for(self, arguments: Dict[str, Any], context: Optional[ToolContext] = None) -> int:
        """Seconds this command may run: per-call value, else the configured default."""
        requested = arguments.get("timeout")
        if isinstance(requested, bool):
            requested = None
        if isinstance(requested, (int, float)) and requested > 0:
            return int(requested)
        if context is not None:
            return int(context.config.permissions.terminal.timeout or DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT

    # -- execution --------------------------------------------------------- #

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        policy = context.config.permissions.terminal
        command = str(arguments.get("command", ""))
        if "\x00" in command:
            raise ToolError("command contains a NUL byte")

        cwd = self._resolve_cwd(arguments, context)
        if not cwd.is_dir():
            raise ToolError(f"working directory does not exist: {cwd}")

        timeout = self._clamp_timeout(self.timeout_for(arguments, context), policy.max_timeout)
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
                preexec_fn=_resource_limiter(policy),
            )
        except FileNotFoundError as exc:
            raise ToolError(f"command not found: {exc}") from exc
        except PermissionError as exc:
            raise ToolError(f"permission denied executing command: {exc}") from exc
        except OSError as exc:
            raise ToolError(f"could not start command: {exc}") from exc

        limit = max(1024, int(policy.max_output_bytes))
        stdout_reader = _PipeReader(process.stdout, limit)
        stderr_reader = _PipeReader(process.stderr, limit)
        stdout_reader.start()
        stderr_reader.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(process)
            try:
                process.wait(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process, force=True)
                try:
                    process.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover - pathological
                    pass

        # The readers drain (and cap) whatever is left in the pipes.
        stdout_reader.join(timeout=_KILL_GRACE_SECONDS)
        stderr_reader.join(timeout=_KILL_GRACE_SECONDS)
        _close_quietly(process.stdout)
        _close_quietly(process.stderr)

        duration = time.monotonic() - started
        stdout = stdout_reader.text()
        stderr = stderr_reader.text()

        meta: Dict[str, Any] = {
            "cwd": str(cwd),
            "duration_s": round(duration, 3),
            "timed_out": timed_out,
            "truncated": stdout_reader.truncated or stderr_reader.truncated,
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
        """Absolute *real* path of the working directory.

        ``resolve()`` (not ``normpath``) matters: a symlinked directory inside the
        workspace must not let a command run somewhere else.
        """
        base = context.config.terminal_cwd
        requested = arguments.get("cwd")
        if requested:
            candidate = Path(os.path.expanduser(str(requested)))
            base = candidate if candidate.is_absolute() else (base / candidate)
        try:
            return Path(os.path.realpath(str(base)))
        except OSError as exc:  # pragma: no cover - ELOOP / ENAMETOOLONG
            raise ToolError(f"cannot resolve working directory {base}: {exc}") from exc

    @staticmethod
    def _clamp_timeout(requested: int, maximum: int) -> int:
        return max(1, min(int(requested), max(1, int(maximum))))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def build_environment(denylist, allowlist) -> Dict[str, str]:
    """Filtered copy of the environment for spawned processes."""
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


class _PipeReader(threading.Thread):
    """Drains a pipe into memory, keeping at most ``limit`` bytes.

    ``subprocess.communicate()`` buffers *everything* a command prints, so
    `yes` can exhaust a 4 GB machine in seconds. This keeps the first
    ``limit`` bytes and discards the rest, while still draining the pipe so the
    child never blocks on a full buffer.
    """

    def __init__(self, stream, limit: int):
        super().__init__(daemon=True, name="agentlite-pipe-reader")
        self._stream = stream
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._read_chunk()
                if not chunk:
                    break
                room = self._limit - len(self._buffer)
                if room > 0:
                    self._buffer += chunk[:room]
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (ValueError, OSError):  # pragma: no cover - closed pipe
            pass

    def _read_chunk(self) -> bytes:
        reader = getattr(self._stream, "read1", None)
        return reader(READ_CHUNK) if callable(reader) else self._stream.read(READ_CHUNK)

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


def _close_quietly(stream) -> None:
    try:
        if stream is not None and not stream.closed:
            stream.close()
    except (OSError, ValueError):  # pragma: no cover
        pass


def _resource_limiter(policy):
    """Build a ``preexec_fn`` that applies POSIX resource limits.

    Returns ``None`` when no limit is configured or the platform is not POSIX,
    so the child runs exactly as before.
    """
    if os.name != "posix":
        return None
    configured = (
        policy.max_memory_mb or 0,
        policy.max_cpu_seconds or 0,
        policy.max_file_size_mb or 0,
        policy.max_processes or 0,
    )
    if not any(configured):
        return None

    import resource

    def apply_limits() -> None:
        # Core dumps can contain secrets and fill the disk: never.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if policy.max_memory_mb:
            limit = int(policy.max_memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if policy.max_cpu_seconds:
            seconds = int(policy.max_cpu_seconds)
            resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
        if policy.max_file_size_mb:
            limit = int(policy.max_file_size_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        if policy.max_processes:
            count = int(policy.max_processes)
            resource.setrlimit(resource.RLIMIT_NPROC, (count, count))

    return apply_limits


def _kill_process_tree(process: subprocess.Popen, force: bool = False) -> None:
    """SIGTERM (or SIGKILL) the whole process group, not just the child.

    Commands are started with ``start_new_session=True``, so killing the group
    takes down grandchildren too - a shell that spawned a background job cannot
    outlive its parent's timeout.
    """
    signal_to_send = signal.SIGKILL if force else signal.SIGTERM
    killed_group = False
    try:
        os.killpg(os.getpgid(process.pid), signal_to_send)
        killed_group = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if not killed_group:
        try:
            process.kill() if force else process.terminate()
        except (ProcessLookupError, OSError):  # pragma: no cover - already gone
            pass


def _shorten(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
