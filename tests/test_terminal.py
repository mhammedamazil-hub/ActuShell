"""Terminal tool tests: execution, stdout/stderr/exit code, timeouts, sandboxing."""

from __future__ import annotations

import subprocess

import pytest

from agentlite.core.models import ToolCall
from agentlite.core.permissions import PermissionEngine
from agentlite.core.registry import ToolRegistry
from agentlite.tools.terminal import TerminalTool, build_environment


@pytest.fixture
def runner(config, audit):
    """Run a terminal command through the full executor stack."""
    from agentlite.core.agent import Agent
    from agentlite.core.confirmation import AutoAllowHandler
    from agentlite.providers.mock import MockProvider

    agent = Agent.from_config(
        config,
        provider=MockProvider(steps=[]),
        confirmation_handler=AutoAllowHandler(),
        audit=audit,
    )

    def run(command: str, **extra):
        arguments = {"command": command, **extra}
        return agent.executor.execute(
            ToolCall(id="call_1", name="terminal.run", arguments=arguments),
            _context(agent),
        )

    return run


def _context(agent):
    from agentlite.tools.base import ToolContext

    return ToolContext(
        run_id="run_test",
        config=agent.config,
        workspace=agent.config.workspace_root,
        timeout=30,
    )


def test_stdout_and_exit_code(runner):
    result = runner("echo hello")
    assert result.ok is True
    assert result.exit_code == 0
    assert result.output.strip() == "hello"
    assert result.decision == "allow"


def test_stderr_and_non_zero_exit(runner):
    result = runner("""python3 -c 'import sys; sys.stderr.write("boom"); sys.exit(3)'""")
    assert result.ok is False
    assert result.exit_code == 3
    assert result.meta["stderr"].strip() == "boom"
    assert "status 3" in result.error


def test_working_directory_is_the_workspace(runner, config):
    result = runner("pwd")
    assert result.output.strip() == str(config.workspace_root)


def test_relative_cwd_is_confined_to_workspace(runner, config):
    (config.workspace_root / "sub").mkdir(exist_ok=True)
    allowed = runner("pwd", cwd="sub")
    assert allowed.output.strip() == str(config.workspace_root / "sub")
    denied = runner("pwd", cwd="/tmp")
    assert denied.decision == "denied"
    assert "outside" in denied.reason


def test_dangerous_command_is_denied(runner):
    result = runner("sudo rm -rf /etc")
    assert result.decision == "denied"
    assert result.ok is False
    assert "deny rule" in result.reason


def test_confirmation_required_command_is_denied_in_deny_mode(runner):
    result = runner("rm notes.txt")
    assert result.decision == "denied"
    assert result.ok is False


def test_confirmation_can_be_approved(runner, config):
    config.security.confirmation_mode = "allow"
    (config.workspace_root / "notes.txt").write_text("hi", encoding="utf-8")
    result = runner("rm notes.txt")
    assert result.ok is True
    assert not (config.workspace_root / "notes.txt").exists()


def test_no_shell_metacharacters(runner, config):
    """Without a shell, `;` is just a character - not a command separator."""
    (config.workspace_root / "keep.txt").write_text("safe", encoding="utf-8")
    result = runner("echo one; cat keep.txt")
    assert result.ok is True, result.error
    assert "one; cat keep.txt" in result.output
    assert "safe" not in result.output  # the second command never ran
    assert (config.workspace_root / "keep.txt").exists()


def test_timeout_kills_the_process_tree(runner):
    result = runner("sleep 30", timeout=1)
    assert result.ok is False
    assert result.meta["timed_out"] is True
    assert "timed out" in result.error
    leftover = subprocess.run(
        ["pgrep", "-f", "sleep 30"], capture_output=True, text=True, check=False
    )
    assert leftover.stdout.strip() == "", "the sleeping process should have been killed"


def test_timeout_is_clamped(runner, config):
    config.permissions.terminal.max_timeout = 2
    result = runner("sleep 30", timeout=5000)
    assert result.meta["timed_out"] is True


def test_output_is_truncated(runner, config):
    config.permissions.terminal.max_output_bytes = 2048
    result = runner("""python3 -c 'print("x" * 50000)'""")
    assert result.meta["truncated"] is True
    assert len(result.output.encode()) <= 2048


def test_secrets_are_not_passed_to_commands(runner, monkeypatch):
    monkeypatch.setenv("MY_SECRET_KEY", "supersecretvalue123")
    result = runner("env")
    assert result.ok is True
    assert "supersecretvalue123" not in result.output
    assert "AGENTLITE=1" in result.output


def test_env_allowlist_overrides_denylist(monkeypatch):
    monkeypatch.setenv("DEPLOY_TOKEN", "abc123456789")
    env = build_environment([".*TOKEN.*"], [])
    assert "DEPLOY_TOKEN" not in env
    env = build_environment([".*TOKEN.*"], ["DEPLOY_TOKEN"])
    assert env.get("DEPLOY_TOKEN") == "abc123456789"


def test_missing_binary_reports_error(runner):
    result = runner("definitely-not-a-real-binary-xyz")
    assert result.ok is False
    assert "not found" in result.error


def test_empty_command_is_rejected(runner):
    result = runner("   ")
    assert result.ok is False


def test_unbalanced_quote_is_reported(runner):
    result = runner('echo "unbalanced')
    assert result.ok is False


def test_registry_and_engine_are_independent(config):
    registry = ToolRegistry()
    tool = TerminalTool()
    registry.register(tool, enabled=True)
    engine = PermissionEngine(config)
    assert registry.get("terminal.run") is tool
    assert engine.evaluate(
        tool.permission_request(
            {"command": "ls"},
            _context_namespace(config),
        )
    ).allowed


def _context_namespace(config):
    from agentlite.tools.base import ToolContext

    return ToolContext(run_id="r", config=config, workspace=config.workspace_root)


def test_tool_metadata_exposes_parameters():
    tool = TerminalTool()
    assert tool.name == "terminal.run"
    assert tool.parameters["required"] == ["command"]
    assert "command" in tool.parameters["properties"]


def test_audit_log_records_the_call(runner, audit):
    runner("echo audited")
    events = audit.tail(10)
    assert any(event.get("tool") == "terminal.run" for event in events)
    event = [e for e in events if e.get("tool") == "terminal.run"][-1]
    assert event["decision"] == "allow"
    assert event["ok"] is True
