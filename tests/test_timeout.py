"""Timeout handling.

A stuck tool must never take the whole runtime down with it.
"""

from __future__ import annotations

import time

import httpx

from agentlite.core.agent import Agent
from agentlite.core.confirmation import AutoAllowHandler
from agentlite.core.models import RiskLevel, RunStatus, ToolCall
from agentlite.providers.mock import MockProvider
from agentlite.tools.base import ToolContext


def test_terminal_timeout(agent_factory):
    agent = agent_factory(MockProvider(steps=[]))
    context = ToolContext(run_id="r", config=agent.config, workspace=agent.config.workspace_root)
    started = time.monotonic()
    result = agent.executor.execute(
        ToolCall(id="c1", name="terminal.run", arguments={"command": "sleep 30", "timeout": 1}),
        context,
    )
    elapsed = time.monotonic() - started
    assert result.ok is False
    assert result.meta["timed_out"] is True
    assert elapsed < 10


def test_timeout_budget_is_passed_to_the_tool(agent_factory):
    agent = agent_factory(MockProvider(steps=[]))
    tool = agent.registry.get("terminal.run")
    assert tool.timeout_for({"command": "ls"}) == 30
    assert tool.timeout_for({"command": "ls", "timeout": 5}) == 5
    agent.config.permissions.terminal.max_timeout = 3
    context = ToolContext(run_id="r", config=agent.config, workspace=agent.config.workspace_root)
    result = agent.executor.execute(
        ToolCall(id="c2", name="terminal.run", arguments={"command": "sleep 30", "timeout": 600}),
        context,
    )
    assert result.meta["timed_out"] is True


def test_provider_timeout_surfaces_as_a_failed_run(agent_factory, monkeypatch):
    monkeypatch.setattr("agentlite.providers.openai_compatible.time.sleep", lambda seconds: None)
    from agentlite.core.config import ProviderConfig
    from agentlite.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url="https://slow.test/v1", api_key="k", max_retries=0),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("too slow"))
            )
        ),
    )
    agent = agent_factory(provider)
    result = agent.run("hello")
    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, str)


def test_the_executor_hands_a_budget_to_the_tool(agent_factory):
    """Every tool call runs with an explicit, recorded timeout budget."""
    from agentlite.core.audit import AuditLogger
    from agentlite.core.executor import ToolExecutor
    from agentlite.core.permissions import PermissionEngine, PermissionRequest
    from agentlite.core.registry import ToolRegistry
    from agentlite.tools.base import Tool, ToolOutput

    seen = {}

    class RecordingTool(Tool):
        """A third-party tool: it describes its action and gets a budget."""

        name = "record.budget"
        family = "filesystem"
        description = "records the timeout it was given"
        risk = RiskLevel.LOW

        def permission_request(self, arguments, context):
            return PermissionRequest(
                tool=self.name,
                action="read",
                family=self.family,
                risk=RiskLevel.LOW,
                path=context.workspace,
            )

        def timeout_for(self, arguments):
            return int(arguments.get("timeout", 4))

        def execute(self, arguments, context):
            seen["timeout"] = context.timeout
            return ToolOutput(output=f"ran with {context.timeout}s")

    registry = ToolRegistry()
    registry.register(RecordingTool(), enabled=True)
    agent = agent_factory(MockProvider(steps=[]))
    executor = ToolExecutor(
        registry=registry,
        permissions=PermissionEngine(agent.config),
        confirmation=AutoAllowHandler(),
        audit=AuditLogger(enabled=False),
        config=agent.config,
    )
    context = ToolContext(run_id="r", config=agent.config, workspace=agent.config.workspace_root)
    result = executor.execute(
        ToolCall(id="c3", name="record.budget", arguments={"timeout": 9}), context
    )
    assert result.ok is True
    assert seen["timeout"] == 9


def test_run_level_deadline(agent_factory, config_factory):
    config = config_factory()
    config.security.max_run_seconds = 1
    config.security.max_steps = 50
    agent = agent_factory(
        MockProvider(steps=[[("filesystem.list", {"path": "."})]] * 50), config=config
    )
    started = time.monotonic()
    result = agent.run("loop")
    assert result.status in (RunStatus.TIMEOUT, RunStatus.MAX_STEPS_REACHED)
    assert time.monotonic() - started < 30


def test_browser_timeout_uses_the_configured_budget(config_factory):
    from agentlite.tools.browser import BrowserSession, browser_tools

    config = config_factory(permissions={"browser": {"timeout_ms": 5000}})
    session = BrowserSession(config)
    tools = {tool.name: tool for tool in browser_tools(config, session)}
    assert tools["browser.open"].timeout_for({}) == 7  # 5s of Playwright time + slack


def test_filesystem_tools_have_short_budgets(config_factory):
    config = config_factory()
    agent = Agent.from_config(config, provider=MockProvider(steps=[]))
    for name in ("filesystem.read", "filesystem.write", "filesystem.list"):
        assert agent.registry.get(name).timeout_for({}) <= 30
