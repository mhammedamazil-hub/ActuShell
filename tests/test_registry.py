"""Tool registry tests."""

from __future__ import annotations

import pytest

from agentlite.core.registry import ToolRegistry, build_registry
from agentlite.tools.base import Tool, ToolContext, ToolOutput


class DummyTool(Tool):
    name = "dummy.echo"
    description = "echoes"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, arguments, context: ToolContext) -> ToolOutput:
        return ToolOutput(output=arguments["text"])


def test_register_and_get():
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)
    assert registry.get("dummy.echo") is tool
    assert registry.has("dummy.echo")
    assert len(registry) == 1


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry()
    registry.register(DummyTool())
    with pytest.raises(ValueError):
        registry.register(DummyTool())


def test_enable_disable():
    registry = ToolRegistry()
    registry.register(DummyTool(), enabled=True)
    assert registry.is_enabled("dummy.echo") is True
    registry.set_enabled("dummy.echo", False, reason="turned off")
    assert registry.is_enabled("dummy.echo") is False
    assert [e.reason for e in registry] == ["turned off"]
    with pytest.raises(KeyError):
        registry.set_enabled("nope", True)


def test_specs_only_include_enabled_tools(config):
    registry = build_registry(config)
    registry.set_enabled("terminal.run", False)
    names = [spec.name for spec in registry.specs()]
    assert "terminal.run" not in names
    assert "filesystem.read" in names
    assert len(registry.describe()) == len(registry)


def test_default_registry_contains_the_mvp_tools(config):
    registry = build_registry(config)
    for name in (
        "terminal.run",
        "filesystem.list",
        "filesystem.read",
        "filesystem.write",
        "browser.open",
        "browser.click",
        "browser.type",
        "browser.read_page",
        "browser.screenshot",
        "browser.back",
    ):
        assert registry.has(name), name


def test_config_disables_tools(config):
    config.permissions.terminal.enabled = False
    config.permissions.filesystem.enabled = False
    registry = build_registry(config)
    assert registry.is_enabled("terminal.run") is False
    assert registry.is_enabled("filesystem.read") is False
    assert (
        "disabled in configuration"
        in [e.reason for e in registry if e.tool.name == "terminal.run"][0]
    )


def test_browser_tools_report_missing_browser_binary(config, monkeypatch):
    monkeypatch.setattr("agentlite.tools.browser.playwright_installed", lambda: True)
    monkeypatch.setattr("agentlite.tools.browser.browser_binary_installed", lambda: False)
    registry = build_registry(config)
    entry = [e for e in registry if e.tool.name == "browser.open"][0]
    assert entry.enabled is False
    assert "playwright install chromium" in entry.reason


def test_browser_tools_enabled_when_available(config, monkeypatch):
    """Hermetic: this must pass whether or not Playwright is installed here."""
    monkeypatch.setattr("agentlite.tools.browser.playwright_installed", lambda: True)
    monkeypatch.setattr("agentlite.tools.browser.browser_binary_installed", lambda: True)
    registry = build_registry(config)
    entry = [e for e in registry if e.tool.name == "browser.open"][0]
    assert entry.enabled is True


def test_browser_tools_report_missing_package(config, monkeypatch):
    monkeypatch.setattr("agentlite.tools.browser.playwright_installed", lambda: False)
    registry = build_registry(config)
    entry = [e for e in registry if e.tool.name == "browser.open"][0]
    assert entry.enabled is False
    assert "pip install" in entry.reason


def test_registry_close_releases_the_browser(config, monkeypatch):
    calls = []
    registry = build_registry(config)
    monkeypatch.setattr(registry.browser_session, "close", lambda: calls.append("closed"))
    registry.close()
    assert calls == ["closed"]


def test_describe_exposes_risk_and_parameters(config):
    registry = build_registry(config)
    description = {item["name"]: item for item in registry.describe()}
    assert description["terminal.run"]["risk"] == "high"
    assert description["filesystem.write"]["risk"] == "medium"
    assert "properties" in description["terminal.run"]["parameters"]
