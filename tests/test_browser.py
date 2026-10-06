"""Browser tool tests.

The Playwright engine itself is exercised in ``test_playwright_backend.py``.
Here the tools are driven through a stub backend, which is what lets us test
URL policy, page handling and payload shaping without a browser binary.
"""

from __future__ import annotations

import pytest

from agentlite.core.agent import Agent
from agentlite.core.confirmation import AutoAllowHandler
from agentlite.core.models import ToolCall
from agentlite.core.registry import ToolRegistry
from agentlite.providers.mock import MockProvider
from agentlite.tools.base import ToolContext, ToolError, ToolOutput
from agentlite.tools.browser import (
    INSTALL_HINT,
    BrowserSession,
    BrowserToolBase,
    browser_tools,
)


class StubBackend:
    """Minimal BrowserBackend implementation."""

    def __init__(self, title="Example", text="hello from the page"):
        self.calls = []
        self.title = title
        self.text = text
        self._open = False

    def open(self, url, timeout_ms):
        self.calls.append(("open", url, timeout_ms))
        self._open = True
        return {"url": url, "title": self.title}

    def click(self, selector, timeout_ms):
        self.calls.append(("click", selector))
        return {"selector": selector, "url": "https://example.com", "title": self.title}

    def type_text(self, selector, text, timeout_ms):
        self.calls.append(("type", selector, text))
        return {"selector": selector, "characters": len(text)}

    def read_page(self, max_chars):
        self.calls.append(("read_page", max_chars))
        return {
            "url": "https://example.com",
            "title": self.title,
            "text": self.text[:max_chars],
            "truncated": len(self.text) > max_chars,
        }

    def screenshot(self, path, timeout_ms):
        self.calls.append(("screenshot", str(path)))
        path.write_bytes(b"\x89PNG-stub")
        return {"path": str(path), "bytes": 10}

    def back(self, timeout_ms):
        self.calls.append(("back",))
        return {"url": "https://example.com/prev", "title": "previous"}

    def close(self):
        self.calls.append(("close",))
        self._open = False

    @property
    def is_open(self):
        return self._open


@pytest.fixture
def browser_env(config):
    backend = StubBackend()
    session = BrowserSession(config, backend=backend)
    registry = ToolRegistry()
    for tool in browser_tools(config, session):
        registry.register(tool, enabled=True)
    agent = Agent.from_config(
        config,
        provider=MockProvider(steps=[]),
        registry=registry,
        confirmation_handler=AutoAllowHandler(),
    )
    context = ToolContext(run_id="run_browser", config=config, workspace=config.workspace_root)

    def call(name, **arguments):
        return agent.executor.execute(
            ToolCall(id=f"call_{name}", name=name, arguments=arguments), context
        )

    return backend, session, call, config


def test_open_read_flow(browser_env):
    backend, _session, call, _config = browser_env
    opened = call("browser.open", url="https://example.com")
    assert opened.ok is True, opened.error
    assert backend.calls[0][0] == "open"

    read = call("browser.read_page")
    assert read.ok is True
    assert "hello from the page" in read.output
    assert "https://example.com" in read.output


def test_click_type_back_and_close(browser_env):
    backend, _session, call, _config = browser_env
    call("browser.open", url="https://example.com")
    assert call("browser.click", selector="button#go").ok
    assert call("browser.type", selector="input[name=q]", text="agentlite").ok
    assert call("browser.back").ok
    assert call("browser.close").ok
    kinds = [entry[0] for entry in backend.calls]
    assert kinds == ["open", "click", "type", "back", "close"]


def test_page_text_is_truncated_to_the_configured_limit(browser_env):
    backend, _session, call, config = browser_env
    config.permissions.browser.max_page_chars = 5
    call("browser.open", url="https://example.com")
    result = call("browser.read_page")
    assert result.meta["truncated"] is True
    assert result.meta["text"] == "hello"


def test_screenshot_is_written_into_the_workspace(browser_env):
    _backend, _session, call, config = browser_env
    call("browser.open", url="https://example.com")
    result = call("browser.screenshot")
    assert result.ok is True, result.error
    path = config.workspace_root / "screenshots"
    assert any(path.iterdir())
    assert result.meta["path"].startswith(str(path))


def test_screenshot_cannot_escape_the_directory(browser_env):
    _backend, _session, call, _config = browser_env
    result = call("browser.screenshot", path="../../etc/evil.png")
    assert result.ok is False
    assert "escapes" in result.error


def test_navigation_to_a_denied_domain_is_refused(browser_env):
    _backend, _session, call, config = browser_env
    config.permissions.browser.denied_domains = ["tracker.test"]
    result = call("browser.open", url="https://tracker.test/x")
    assert result.decision == "denied"
    assert "denied" in result.reason


def test_domain_allow_list_is_enforced(browser_env):
    _backend, _session, call, config = browser_env
    config.permissions.browser.allowed_domains = ["example.com"]
    assert call("browser.open", url="https://example.com/a").ok
    assert call("browser.open", url="https://other.test").decision == "denied"


def test_non_http_scheme_is_refused(browser_env):
    _backend, _session, call, _config = browser_env
    assert call("browser.open", url="file:///etc/passwd").decision == "denied"


def test_browser_can_be_disabled(browser_env):
    _backend, _session, call, config = browser_env
    config.permissions.browser.enabled = False
    assert call("browser.open", url="https://example.com").decision == "denied"


def test_missing_playwright_gives_an_actionable_error(config):
    session = BrowserSession(config, backend=None)
    original = session.is_available
    session.is_available = lambda: False  # simulate "pip install" not done
    try:
        with pytest.raises(ToolError) as excinfo:
            session.backend()
        assert "playwright install chromium" in str(excinfo.value)
    finally:
        session.is_available = original


def test_backend_interface_is_enforced():
    class Incomplete(BrowserToolBase):
        name = "browser.incomplete"

        def execute(self, arguments, context):
            return ToolOutput()

    session = BrowserSession.__new__(BrowserSession)
    tool = Incomplete(session)
    assert tool.family == "browser"
    assert INSTALL_HINT.startswith("Playwright is not installed")


def test_a_wedged_browser_cannot_hang_the_run(config):
    """The wait for the browser thread is bounded, and close() recovers."""
    import time

    from agentlite.tools.base import ToolError
    from agentlite.tools.browser import BrowserSession

    class WedgedBackend:
        def __init__(self, hang=False):
            self.hang = hang
            self.closed = False

        def open(self, url, timeout_ms):
            if self.hang:
                time.sleep(30)
            return {"url": url, "title": "t"}

        def click(self, selector, timeout_ms):
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
            self.closed = True

        @property
        def is_open(self):
            return True

    wedged = WedgedBackend(hang=True)
    session = BrowserSession(config, backend=wedged)
    with pytest.raises(TimeoutError):
        session.call(lambda backend: backend.open("https://example.com", 1000), timeout=0.2)

    # Until the session is closed, further calls are refused rather than queued
    # behind the stuck one.
    with pytest.raises(ToolError, match="stuck"):
        session.call(lambda backend: backend.click("a", 1000), timeout=1)

    session.close()
    assert wedged.closed is True
    healthy = WedgedBackend()
    session._backend = healthy
    assert session.call(lambda backend: backend.click("a", 1000), timeout=5)["url"]


def test_browser_tools_share_one_session(config):
    tools = browser_tools(config)
    sessions = {id(tool.session) for tool in tools}
    assert len(sessions) == 1
    names = [tool.name for tool in tools]
    assert names == [
        "browser.open",
        "browser.click",
        "browser.type",
        "browser.read_page",
        "browser.screenshot",
        "browser.back",
        "browser.close",
    ]


def test_browser_tools_register_and_run_through_the_registry(config, monkeypatch):
    monkeypatch.setattr("agentlite.tools.browser.browser_binary_installed", lambda: True)
    backend = StubBackend()
    registry = ToolRegistry()
    session = BrowserSession(config, backend=backend)
    for tool in browser_tools(config, session):
        registry.register(tool, enabled=True)
    assert registry.is_enabled("browser.open")
    assert len(registry) == 7
