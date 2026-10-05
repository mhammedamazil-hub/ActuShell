"""Agent loop tests: task -> LLM -> tool -> permission -> result -> LLM."""

from __future__ import annotations

import pytest

from agentlite.core.agent import Agent
from agentlite.core.confirmation import AutoAllowHandler, DenyHandler, PendingConfirmationHandler
from agentlite.core.models import RunStatus
from agentlite.providers.base import ProviderError
from agentlite.providers.mock import MockProvider


def test_simple_tool_loop(agent_factory):
    provider = MockProvider.from_script(
        [("filesystem.list", {"path": "."})],
        [("filesystem.write", {"path": "report.md", "content": "# report"})],
        "I listed the workspace and wrote report.md.",
    )
    agent = agent_factory(provider)
    result = agent.run("inspect the workspace")

    assert result.status is RunStatus.COMPLETED
    assert result.result == "I listed the workspace and wrote report.md."
    assert [action.tool for action in result.actions] == ["filesystem.list", "filesystem.write"]
    assert all(action.ok for action in result.actions)
    assert result.steps == 3
    assert (agent.config.workspace_root / "report.md").read_text() == "# report"


def test_tool_results_reach_the_model(agent_factory):
    provider = MockProvider.from_script(
        [("filesystem.write", {"path": "note.txt", "content": "hello"})],
        "done",
    )
    agent = agent_factory(provider)
    agent.run("write a note")
    assert provider.requests[-1]["roles"][-1] == "tool"
    assert "note.txt" in provider.last_messages[-1].content


def test_denied_action_is_reported_back_to_the_model(agent_factory, config_factory):
    config = config_factory()
    config.security.confirmation_mode = "deny"
    provider = MockProvider.from_script(
        [("terminal.run", {"command": "sudo rm -rf /etc"})],
        "The command was refused, so I stopped.",
    )
    agent = agent_factory(provider, config=config, confirmation=DenyHandler())
    result = agent.run("clean the system")

    assert result.status is RunStatus.COMPLETED
    assert result.actions[0].decision == "denied"
    assert result.actions[0].ok is False
    tool_messages = [m for m in provider.last_messages if m.role == "tool"]
    assert "denied" in tool_messages[-1].content


def test_unknown_tool_does_not_crash_the_run(agent_factory):
    provider = MockProvider.from_script([("kernel.panic", {})], "That tool does not exist.")
    result = agent_factory(provider).run("break things")
    assert result.status is RunStatus.COMPLETED
    assert result.actions[0].decision == "error"
    assert "unknown tool" in result.actions[0].error


def test_missing_required_argument_is_reported(agent_factory):
    provider = MockProvider.from_script([("filesystem.read", {})], "Missing argument.")
    result = agent_factory(provider).run("read a file")
    assert result.actions[0].decision == "error"
    assert "missing required argument" in result.actions[0].error


def test_malformed_model_arguments_are_reported(agent_factory):
    provider = MockProvider(steps=[[("filesystem.read", "{not json")], "Malformed."])
    result = agent_factory(provider).run("read a file")
    assert result.actions[0].ok is False
    assert "malformed" in result.actions[0].error


def test_confirmation_parks_the_run(agent_factory, config_factory):
    config = config_factory()
    config.security.confirmation_mode = "prompt"
    provider = MockProvider.from_script([("terminal.run", {"command": "rm notes.txt"})], "Removed.")
    agent = agent_factory(provider, config=config, confirmation=PendingConfirmationHandler())
    run = agent.create_run("delete the notes")

    pending_result = run.resume()
    assert pending_result.status is RunStatus.NEEDS_CONFIRMATION
    assert pending_result.pending["tool"] == "terminal.run"
    assert pending_result.pending["arguments"] == {"command": "rm notes.txt"}
    assert run.finished is False
    # no tool message yet: the human has not answered
    assert [m.role for m in provider.last_messages].count("tool") == 0

    (config.workspace_root / "notes.txt").write_text("bye", encoding="utf-8")
    allowed = run.resume(confirmation=True)
    assert allowed.status is RunStatus.COMPLETED
    assert allowed.actions[0].decision == "allow"
    assert not (config.workspace_root / "notes.txt").exists()


def test_confirmation_can_be_refused(agent_factory, config_factory):
    config = config_factory()
    config.security.confirmation_mode = "prompt"
    (config.workspace_root / "notes.txt").write_text("keep me", encoding="utf-8")
    provider = MockProvider.from_script([("terminal.run", {"command": "rm notes.txt"})], "Refused.")
    agent = agent_factory(provider, config=config, confirmation=PendingConfirmationHandler())
    run = agent.create_run("delete the notes")

    run.resume()
    refused = run.resume(confirmation=False)
    assert refused.status is RunStatus.COMPLETED
    assert refused.actions[0].decision == "denied"
    assert "refused by user" in refused.actions[0].reason
    assert (config.workspace_root / "notes.txt").exists()


def test_confirmation_mode_allow_skips_the_round_trip(agent_factory, config_factory):
    config = config_factory()
    config.security.confirmation_mode = "allow"
    (config.workspace_root / "notes.txt").write_text("bye", encoding="utf-8")
    provider = MockProvider.from_script([("terminal.run", {"command": "rm notes.txt"})], "Removed.")
    result = agent_factory(provider, config=config, confirmation=AutoAllowHandler()).run(
        "delete the notes"
    )
    assert result.status is RunStatus.COMPLETED
    assert result.actions[0].decision == "allow"


def test_max_steps_is_enforced(agent_factory):
    provider = MockProvider(steps=[[("filesystem.list", {"path": "."})]] * 50)
    result = agent_factory(provider).run("loop forever", max_steps=3)
    assert result.status is RunStatus.MAX_STEPS_REACHED
    assert result.steps == 3


def test_run_deadline_is_enforced(agent_factory, config_factory):
    config = config_factory()
    config.security.max_run_seconds = 0
    provider = MockProvider.from_script([("filesystem.list", {"path": "."})], "never reached")
    result = agent_factory(provider, config=config).run("too slow")
    assert result.status is RunStatus.TIMEOUT


def test_provider_failure_is_reported(agent_factory):
    class BrokenProvider(MockProvider):
        def complete(self, messages, tools, temperature=None, max_tokens=None):
            raise ProviderError("the model backend is unreachable")

    result = agent_factory(BrokenProvider()).run("hello")
    assert result.status is RunStatus.FAILED
    assert "unreachable" in result.error


def test_disabled_tool_is_not_offered_and_not_executed(agent_factory, config_factory):
    config = config_factory()
    config.permissions.terminal.enabled = False
    agent = agent_factory(
        MockProvider.from_script([("terminal.run", {"command": "ls"})], "ok"), config=config
    )
    assert "terminal.run" not in [tool.name for tool in agent.registry.specs()]
    result = agent.run("run ls")
    assert result.actions[0].decision == "denied"


def test_system_prompt_lists_only_enabled_tools(agent_factory, config_factory):
    config = config_factory()
    config.permissions.browser.enabled = False
    agent = agent_factory(MockProvider(steps=[]), config=config)
    prompt = agent.system_prompt()
    assert "terminal.run" in prompt
    assert "browser.open" not in prompt
    assert str(agent.config.workspace_root) in prompt


def test_run_ids_are_unique(agent_factory):
    agent = agent_factory(MockProvider.from_script("ok"))
    assert agent.run("a").run_id != agent.run("b").run_id


def test_result_serialisation(agent_factory):
    result = agent_factory(
        MockProvider.from_script([("filesystem.list", {"path": "."})], "done")
    ).run("list")
    payload = result.to_dict()
    assert payload["status"] == "completed"
    assert payload["actions"][0]["tool"] == "filesystem.list"
    assert isinstance(payload["usage"], dict)


@pytest.mark.parametrize("steps", [1, 2, 5])
def test_max_steps_override(agent_factory, steps):
    provider = MockProvider(steps=[[("filesystem.list", {"path": "."})]] * 20)
    result = agent_factory(provider).run("loop", max_steps=steps)
    assert result.steps == steps
    assert result.status in (RunStatus.MAX_STEPS_REACHED, RunStatus.COMPLETED)


def test_agent_from_config_builds_the_full_stack(config_factory):
    config = config_factory()
    config.provider.name = "mock"
    agent = Agent.from_config(config, confirmation_handler=AutoAllowHandler())
    assert agent.registry.get("terminal.run") is not None
    assert agent.provider.name == "mock"
    assert agent.executor.permissions is not None


def test_actions_record_arguments_and_timings(agent_factory):
    result = agent_factory(
        MockProvider.from_script([("filesystem.write", {"path": "a.txt", "content": "x"})], "done")
    ).run("write")
    action = result.actions[0]
    assert action.arguments == {"path": "a.txt", "content": "x"}
    assert action.duration_ms >= 0
    assert action.step == 1
