"""Security-boundary tests.

These are the tests that should fail loudly if someone weakens a control.
They are grouped here (rather than spread across the tool tests) so that a
reviewer can see the full security surface in one file.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from agentlite.core.agent import Agent
from agentlite.core.audit import AuditLogger, Redactor
from agentlite.core.config import security_warnings
from agentlite.core.confirmation import AutoAllowHandler
from agentlite.core.models import ToolCall, ToolResult
from agentlite.core.permissions import Effect, PermissionEngine
from agentlite.providers.mock import MockProvider
from agentlite.tools.base import ToolContext
from agentlite.tools.filesystem import FilesystemReadTool


@pytest.fixture
def runtime(config_factory):
    """An agent plus the ToolContext used to drive tools directly."""
    config = config_factory()
    agent = Agent.from_config(
        config, provider=MockProvider(steps=[]), confirmation_handler=AutoAllowHandler()
    )
    context = ToolContext(run_id="run_sec", config=config, workspace=config.workspace_root)
    return agent, config, context


def call(agent, context, name, **arguments):
    return agent.executor.execute(ToolCall(id="c", name=name, arguments=arguments), context)


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #


def test_output_is_capped_without_buffering_it_all(runtime):
    """A chatty command must not be able to exhaust the machine's memory."""
    agent, config, context = runtime
    config.permissions.terminal.max_output_bytes = 4096
    command = "python3 -c \"import sys; sys.stdout.write('A' * (80 * 1024 * 1024))\""
    started = time.monotonic()
    result = call(agent, context, "terminal.run", command=command, timeout=60)
    assert time.monotonic() - started < 60
    assert result.ok is True
    assert len(result.output) == 4096
    assert result.meta["truncated"] is True


def test_configured_timeout_is_honoured(runtime):
    """`permissions.terminal.timeout` must actually apply (it is the default)."""
    agent, config, context = runtime
    config.permissions.terminal.timeout = 2
    started = time.monotonic()
    result = call(agent, context, "terminal.run", command="sleep 30")
    elapsed = time.monotonic() - started
    assert result.meta["timed_out"] is True
    assert elapsed < 15, "the configured timeout was ignored"


def test_timeout_kills_the_whole_process_tree(runtime):
    """A child spawned by a command must not outlive the timeout."""
    agent, config, context = runtime
    command = (
        'python3 -c "import subprocess, sys, time; '
        "child = subprocess.Popen(['sleep', '25']); print(child.pid); "
        'sys.stdout.flush(); time.sleep(25)"'
    )
    result = call(agent, context, "terminal.run", command=command, timeout=1)
    assert result.meta["timed_out"] is True
    child_pid = int(result.output.strip().splitlines()[0])
    time.sleep(0.5)
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass  # gone, as intended
    else:
        os.kill(child_pid, 9)
        pytest.fail(f"process {child_pid} spawned by the command survived the timeout")


def test_no_shell_means_no_shell_syntax(runtime):
    """Without allow_shell the command is argv, so `&&`/`;`/backticks are inert."""
    agent, config, context = runtime
    result = call(agent, context, "terminal.run", command="echo one; echo two")
    assert result.ok is True
    assert "one; echo two" in result.output  # the metacharacters were just text


def test_symlinked_directory_cannot_move_the_working_directory(runtime, tmp_path):
    """cwd is resolved, so a symlink inside the workspace cannot escape it."""
    agent, config, context = runtime
    outside = tmp_path / "outside"
    outside.mkdir()
    link = config.workspace_root / "door"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside)
    result = call(agent, context, "terminal.run", command="pwd", cwd="door")
    assert result.decision == "denied"
    assert "outside" in result.reason


def test_nul_byte_in_command_is_refused(runtime):
    agent, _config, context = runtime
    result = call(agent, context, "terminal.run", command="echo hello\x00touch pwned")
    assert result.ok is False
    assert "NUL" in result.error


def test_shell_metacharacters_are_inert(runtime):
    agent, config, context = runtime
    (config.workspace_root / "keep.txt").write_text("safe", encoding="utf-8")
    result = call(agent, context, "terminal.run", command="echo one; cat keep.txt")
    assert result.ok is True
    assert "safe" not in result.output


@pytest.mark.skipif(os.name != "posix", reason="POSIX resource limits")
def test_file_size_limit_stops_a_runaway_write(runtime):
    agent, config, context = runtime
    config.permissions.terminal.max_file_size_mb = 1
    result = call(
        agent,
        context,
        "terminal.run",
        command="dd if=/dev/zero of=big.bin bs=1M count=8",
        timeout=30,
    )
    assert result.exit_code != 0, "the write should have been stopped by RLIMIT_FSIZE"
    assert (config.workspace_root / "big.bin").stat().st_size <= 2 * 1024 * 1024


@pytest.mark.skipif(os.name != "posix", reason="POSIX resource limits")
def test_memory_limit_stops_a_runaway_allocation(runtime):
    agent, config, context = runtime
    config.permissions.terminal.max_memory_mb = 96
    result = call(
        agent,
        context,
        "terminal.run",
        command="python3 -c \"x = 'a' * (400 * 1024 * 1024)\"",
        timeout=60,
    )
    assert result.exit_code != 0


def test_secrets_are_not_handed_to_child_processes(runtime, monkeypatch):
    agent, _config, context = runtime
    monkeypatch.setenv("SERVICE_API_KEY", "supersecretvalue123")
    result = call(agent, context, "terminal.run", command="env")
    assert "supersecretvalue123" not in result.output


# --------------------------------------------------------------------------- #
# Filesystem
# --------------------------------------------------------------------------- #


def test_path_traversal_is_refused(runtime):
    agent, _config, context = runtime
    for path in ("../agentlite.yaml", "../../etc/passwd", "/etc/passwd", "a/../../b"):
        result = call(agent, context, "filesystem.read", path=path)
        assert result.decision == "denied", path


def test_symlink_escape_is_refused_even_when_symlinks_are_allowed(runtime, tmp_path):
    agent, config, context = runtime
    config.permissions.filesystem.follow_symlinks = True
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    link = config.workspace_root / "link.txt"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(secret)
    result = call(agent, context, "filesystem.read", path="link.txt")
    assert result.ok is False
    assert "outside the allowed paths" in result.error


def test_swap_after_the_permission_check_is_detected(runtime, monkeypatch):
    """Simulate a TOCTOU swap: the inode changes between open and verify."""
    agent, config, context = runtime
    target = config.workspace_root / "victim.txt"
    target.write_text("safe", encoding="utf-8")

    tool = FilesystemReadTool()
    real_lstat = os.lstat
    swapped = {"done": False}

    def sneaky_lstat(path):
        info = real_lstat(path)
        if not swapped["done"]:
            swapped["done"] = True
            # First (pre-open) call passes; the post-open call disagrees.
            return info
        return os.stat_result((info.st_mode | stat.S_IFLNK, 999_999, info.st_dev) + tuple(info)[3:])

    monkeypatch.setattr("agentlite.tools.filesystem.os.lstat", sneaky_lstat)
    with pytest.raises(Exception) as excinfo:
        tool._open_verified(target, context, os.O_RDONLY)
    assert "changed while opening" in str(excinfo.value) or "symlink" in str(excinfo.value)


def test_open_verified_refuses_a_fifo(runtime):
    agent, config, context = runtime
    fifo = config.workspace_root / "pipe"
    if fifo.exists():
        fifo.unlink()
    os.mkfifo(fifo)
    try:
        tool = FilesystemReadTool()
        with pytest.raises(Exception) as excinfo:
            tool._open_verified(fifo, context, os.O_RDONLY)
        assert "not a regular file" in str(excinfo.value)
    finally:
        fifo.unlink()


def test_write_respects_the_size_limit(runtime):
    agent, config, context = runtime
    config.permissions.filesystem.max_write_bytes = 128
    result = call(agent, context, "filesystem.write", path="big.txt", content="x" * 4096)
    assert result.ok is False
    assert not (config.workspace_root / "big.txt").exists()


# --------------------------------------------------------------------------- #
# Browser / SSRF
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine(config_factory) -> PermissionEngine:
    return PermissionEngine(config_factory())


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/admin",
        "http://127.0.0.1:22/",
        "http://[::1]/",
        "http://2130706433/",  # 127.0.0.1 in decimal
        "http://0x7f000001/",  # 127.0.0.1 in hex
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://metadata.google.internal/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "http://example.com@127.0.0.1/",  # userinfo hides the real host
        "http://example.com%40127.0.0.1/",
        "http://127.1/",  # inet_aton short form
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6
        "http://LOCALHOST/",
        "http://localhost./",
    ],
)
def test_private_and_local_urls_are_refused(engine, url):
    decision = engine.check_url(url)
    assert decision.effect is Effect.DENY, f"{url} should have been refused"


def test_public_url_is_allowed(engine):
    assert engine.check_url("https://example.com/page").effect is Effect.ALLOW


def test_a_slow_dns_lookup_is_refused_instead_of_stalling(engine, monkeypatch):
    """getaddrinfo has no timeout; a hostile resolver must not hang the run."""
    from agentlite.core import permissions

    monkeypatch.setattr(
        permissions.socket, "getaddrinfo", lambda *a, **k: __import__("time").sleep(30)
    )
    monkeypatch.setattr(permissions, "DNS_TIMEOUT_SECONDS", 0.2)
    decision = engine.check_url("http://slow.example.com/")
    assert decision.effect is Effect.DENY
    assert "timed out" in decision.reason


def test_private_networks_can_be_enabled_deliberately(config_factory, monkeypatch):
    config = config_factory()
    config.permissions.browser.allow_private_networks = True
    engine = PermissionEngine(config)
    assert engine.check_url("http://127.0.0.1:8080/").effect is Effect.ALLOW
    assert "allow_private_networks" in " ".join(security_warnings(config)).lower()


def test_unresolvable_host_is_refused(engine):
    decision = engine.check_url("http://this-host-does-not-exist.invalid/")
    assert decision.effect is Effect.DENY


def test_redirect_to_a_private_host_is_refused_after_the_fact(config_factory):
    """A server that redirects to 127.0.0.1 must not be readable."""
    from agentlite.core.registry import ToolRegistry
    from agentlite.tools.browser import BrowserSession, browser_tools

    class RedirectingBackend:
        def open(self, url, timeout_ms):
            return {"url": "http://127.0.0.1:8080/private", "title": "private"}

        def click(self, selector, timeout_ms):
            return {"url": "http://127.0.0.1:8080/private", "title": "private"}

        def type_text(self, selector, text, timeout_ms):
            return {"selector": selector, "characters": len(text)}

        def read_page(self, max_chars):
            return {"url": "http://127.0.0.1:8080/private", "title": "t", "text": "secret"}

        def screenshot(self, path, timeout_ms):
            return {"path": str(path), "bytes": 0}

        def back(self, timeout_ms):
            return {"url": "http://127.0.0.1:8080/private", "title": "t"}

        def close(self):
            pass

        @property
        def is_open(self):
            return True

    config = config_factory()
    session = BrowserSession(config, backend=RedirectingBackend())
    registry = ToolRegistry()
    for tool in browser_tools(config, session):
        registry.register(tool, enabled=True)
    agent = Agent.from_config(
        config,
        provider=MockProvider(steps=[]),
        registry=registry,
        confirmation_handler=AutoAllowHandler(),
    )
    context = ToolContext(run_id="r", config=config, workspace=config.workspace_root)

    for name, arguments in (
        ("browser.open", {"url": "https://example.com/redirect"}),
        ("browser.click", {"selector": "a"}),
        ("browser.read_page", {}),
    ):
        result = call(agent, context, name, **arguments)
        assert result.ok is False, name
        assert "blocked" in (result.error or "").lower(), name


def test_browser_calls_are_serialised_on_one_thread(config_factory):
    """Playwright is not thread-safe: concurrent tool calls must still work."""
    from agentlite.tools.browser import BrowserSession

    threads_seen = []

    class RecordingBackend:
        def open(self, url, timeout_ms):
            threads_seen.append(threading.current_thread().name)
            return {"url": url, "title": "t"}

        def click(self, selector, timeout_ms):
            threads_seen.append(threading.current_thread().name)
            return {"url": "https://example.com", "title": "t"}

        def type_text(self, selector, text, timeout_ms):
            return {"selector": selector, "characters": len(text)}

        def read_page(self, max_chars):
            return {"url": "https://example.com", "title": "t", "text": ""}

        def screenshot(self, path, timeout_ms):
            return {"path": str(path), "bytes": 0}

        def back(self, timeout_ms):
            return {"url": "https://example.com", "title": "t"}

        def close(self):
            pass

        @property
        def is_open(self):
            return True

    config = config_factory()
    session = BrowserSession(config, backend=RecordingBackend())
    errors = []

    def worker():
        try:
            session.call(lambda backend: backend.open("https://example.com", 1000))
            session.call(lambda backend: backend.click("a", 1000))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(4)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=10)

    assert not errors
    assert len(threads_seen) == 8
    # Every call ran on the browser's dedicated thread.
    assert len(set(threads_seen)) == 1
    assert threads_seen[0].startswith("agentlite-browser")


# --------------------------------------------------------------------------- #
# Payload size
# --------------------------------------------------------------------------- #


def test_model_payload_is_capped_including_metadata():
    entries = [{"name": f"file{i}", "type": "file", "size": i} for i in range(500)]
    result = ToolResult(
        call_id="1", name="filesystem.list", ok=True, output="x" * 20000, meta={"entries": entries}
    )
    payload = result.to_model_payload(max_chars=2000)
    assert len(payload) <= 2200
    parsed = json.loads(payload)
    assert len(parsed["entries"]) <= 51


def test_short_payloads_are_not_touched():
    result = ToolResult(call_id="1", name="terminal.run", ok=True, output="hello")
    assert json.loads(result.to_model_payload())["output"] == "hello"


# --------------------------------------------------------------------------- #
# Audit log / secrets
# --------------------------------------------------------------------------- #


def test_audit_log_redacts_credential_shapes(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=path, enabled=True)
    logger.log(
        "tool_call",
        arguments="curl -H 'Authorization: Bearer abcdef1234567890' https://api.test",
        output="key=sk-abcdefghijklmnopqrstuvwx token=AKIAABCDEFGHIJKLMNOP",
    )
    text = path.read_text()
    assert "sk-abcdefghijklmnopqrstuvwx" not in text
    assert "AKIAABCDEFGHIJKLMNOP" not in text
    assert "abcdef1234567890" not in text
    assert "***redacted***" in text


def test_redactor_keeps_ordinary_text():
    redactor = Redactor(secrets=["verysecretvalue123"], enabled=True)
    assert redactor.scrub("hello world") == "hello world"
    assert "verysecretvalue123" not in redactor.scrub("token verysecretvalue123 here")


def test_audit_log_rotates_instead_of_growing_forever(tmp_path):
    """An audit log must not be the thing that fills the disk."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path=path, enabled=True, max_bytes=4096, backups=2)
    for index in range(200):
        logger.log("tool_call", tool="terminal.run", arguments=f"command number {index}")
    assert path.stat().st_size <= 8192, "the active log was never rotated"
    assert (tmp_path / "audit.jsonl.1").exists()
    assert len(logger.tail(500)) < 200, "old entries were rotated away"
    # Rotation must keep working once the backups are full.
    for index in range(200):
        logger.log("tool_call", tool="terminal.run", arguments=f"more commands {index}")
    assert path.exists()
    assert (tmp_path / "audit.jsonl.2").exists()


def test_audit_log_disables_itself_instead_of_crashing_the_run(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    logger = AuditLogger(path=blocker / "audit.jsonl", enabled=True)
    logger.log("tool_call", tool="terminal.run")  # must not raise
    assert logger.enabled is False


# --------------------------------------------------------------------------- #
# Configuration warnings
# --------------------------------------------------------------------------- #


def test_dangerous_settings_are_reported(config_factory):
    config = config_factory()
    config.permissions.terminal.allow_shell = True
    config.security.confirmation_mode = "allow"
    config.permissions.browser.allow_private_networks = True
    config.permissions.filesystem.allowed_paths = ["/"]
    warnings = security_warnings(config)
    joined = " ".join(warnings).lower()
    assert "allow_shell" in joined
    assert "confirmation_mode" in joined
    assert "private_networks" in joined
    assert "allowed_paths" in joined


def test_safe_defaults_produce_no_warnings(config_factory):
    assert security_warnings(config_factory()) == []


# --------------------------------------------------------------------------- #
# API limits
# --------------------------------------------------------------------------- #


def _api(config_factory, **sections):
    from fastapi.testclient import TestClient

    from agentlite.api.server import Runtime, create_app
    from agentlite.core.agent import Agent
    from agentlite.core.audit import AuditLogger
    from agentlite.core.confirmation import AutoAllowHandler

    config = config_factory(**sections)
    agent = Agent.from_config(
        config,
        provider=MockProvider(steps=["done"]),
        confirmation_handler=AutoAllowHandler(),
        audit=AuditLogger(enabled=False),
    )
    runtime = Runtime(config, agent=agent)
    return TestClient(create_app(config, runtime=runtime)), runtime


def test_api_rejects_an_oversized_task(config_factory):
    client, _ = _api(config_factory, server={"max_task_chars": 64})
    response = client.post("/api/run", json={"task": "x" * 5000})
    assert response.status_code == 413
    assert "characters" in response.json()["detail"]


def test_api_rejects_a_malformed_task(config_factory):
    client, _ = _api(config_factory)
    assert client.post("/api/run", json={"task": {"nested": "object"}}).status_code == 422
    assert client.post("/api/run", json={"not_a_task": "hi"}).status_code == 422


def test_api_limits_concurrent_runs(config_factory):
    """A slow run must not let callers pile up unlimited work."""
    import threading

    from agentlite.providers.base import LLMProvider, LLMResponse

    release = threading.Event()
    started = threading.Event()

    class BlockingProvider(LLMProvider):
        name = "blocking"
        model = "blocking-1"

        def complete(self, messages, tools, temperature=None, max_tokens=None):
            started.set()
            release.wait(timeout=10)
            return LLMResponse(content="done", finish_reason="stop")

    from fastapi.testclient import TestClient

    from agentlite.api.server import Runtime, create_app
    from agentlite.core.agent import Agent
    from agentlite.core.audit import AuditLogger
    from agentlite.core.confirmation import AutoAllowHandler

    config = config_factory(server={"max_concurrent_runs": 1})
    agent = Agent.from_config(
        config,
        provider=BlockingProvider(),
        confirmation_handler=AutoAllowHandler(),
        audit=AuditLogger(enabled=False),
    )
    client = TestClient(create_app(config, runtime=Runtime(config, agent=agent)))

    results = {}

    def first():
        results["first"] = client.post("/api/run", json={"task": "slow one"}).status_code

    def second():
        results["second"] = client.post("/api/run", json={"task": "too many"})

    first_thread = threading.Thread(target=first)
    first_thread.start()
    assert started.wait(timeout=10), "the first run never reached the provider"
    second_thread = threading.Thread(target=second)
    second_thread.start()
    second_thread.join(timeout=10)
    release.set()
    first_thread.join(timeout=10)

    assert results["first"] == 200
    assert results["second"].status_code == 429
    assert results["second"].headers["retry-after"] == "5"


def test_api_reports_security_warnings_and_limits(config_factory):
    client, _ = _api(config_factory)
    body = client.get("/api/status").json()
    assert body["limits"]["max_concurrent_runs"] >= 1
    assert body["limits"]["max_task_chars"] > 0
    assert isinstance(body["security_warnings"], list)
    assert isinstance(body["warnings"], list)
