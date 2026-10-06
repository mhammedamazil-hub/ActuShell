"""PlaywrightBackend tests.

The Chromium binary is not downloadable in every environment (CI without
network, locked-down sandboxes), so ``sync_playwright`` is replaced by a fake
that records the calls our backend makes. This verifies the integration code -
launch flags, context options, timeouts, error wrapping - not Chromium itself.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, Dict, List

import pytest

from agentlite.core.config import load_config
from agentlite.tools.base import ToolError
from agentlite.tools.browser import PlaywrightBackend


class FakeLocator:
    def __init__(self, log: List[tuple], selector: str):
        self._log = log
        self._selector = selector

    def wait_for(self, state=None, timeout=None):
        self._log.append(("wait_for", self._selector, state, timeout))

    def click(self, timeout=None):
        self._log.append(("click", self._selector, timeout))

    def press_sequentially(self, text, delay=None, timeout=None):
        self._log.append(("press_sequentially", self._selector, text, timeout))


class FakePage:
    def __init__(self, log: List[tuple]):
        self._log = log
        self.url = "https://example.com"

    def goto(self, url, wait_until=None, timeout=None):
        self._log.append(("goto", url, wait_until, timeout))
        self.url = url

    def title(self):
        return "Fake Title"

    def click(self, selector, timeout=None):
        self._log.append(("click", selector, timeout))

    def locator(self, selector):
        return FakeLocator(self._log, selector)

    def evaluate(self, script):
        self._log.append(("evaluate", script))
        return "page text"

    def screenshot(self, path=None, timeout=None, full_page=None):
        self._log.append(("screenshot", path, timeout))
        from pathlib import Path

        Path(path).write_bytes(b"PNG")

    def go_back(self, timeout=None, wait_until=None):
        self._log.append(("go_back", timeout, wait_until))
        return None


class FakeContext:
    def __init__(self, log: List[tuple], options: Dict[str, Any]):
        self._log = log
        self._options = options
        self.closed = False

    def set_default_timeout(self, value):
        self._log.append(("set_default_timeout", value))

    def set_default_navigation_timeout(self, value):
        self._log.append(("set_default_navigation_timeout", value))

    def new_page(self):
        return FakePage(self._log)

    def close(self):
        self.closed = True
        self._log.append(("context.close",))


class FakeBrowser:
    def __init__(self, log: List[tuple], options: Dict[str, Any]):
        self._log = log
        self._options = options

    def new_context(self, **kwargs):
        self._log.append(("new_context", kwargs))
        return FakeContext(self._log, kwargs)

    def close(self):
        self._log.append(("browser.close",))


class FakePlaywright:
    def __init__(self, log: List[tuple], error: Exception | None = None):
        self._log = log
        self._error = error
        self.chromium = self

    def launch(self, **kwargs):
        self._log.append(("launch", kwargs))
        if self._error:
            raise self._error
        return FakeBrowser(self._log, kwargs)

    def start(self):
        self._log.append(("start",))
        return self

    def stop(self):
        self._log.append(("stop",))


@pytest.fixture
def backend_factory(monkeypatch):
    def factory(config=None, error: Exception | None = None):
        log: List[tuple] = []
        fake = FakePlaywright(log, error=error)
        module = ModuleType("playwright.sync_api")
        module.sync_playwright = lambda: fake
        monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
        cfg = config or load_config(apply_env=False)
        return PlaywrightBackend(cfg.permissions.browser), log

    return factory


def test_browser_is_launched_lazily_with_safe_flags(backend_factory):
    backend, log = backend_factory()
    assert backend.is_open is False
    backend.open("https://example.com", 1234)
    assert backend.is_open is True
    launch = [entry for entry in log if entry[0] == "launch"][0][1]
    assert launch["headless"] is True
    assert "--no-sandbox" in launch["args"]
    assert "--disable-dev-shm-usage" in launch["args"]


def test_navigation_uses_domcontentloaded_and_the_timeout(backend_factory):
    backend, log = backend_factory()
    info = backend.open("https://example.com", 4321)
    goto = [entry for entry in log if entry[0] == "goto"][0]
    assert goto[1] == "https://example.com"
    assert goto[2] == "domcontentloaded"
    assert goto[3] == 4321
    assert info["title"] == "Fake Title"


def test_context_options_come_from_the_config(backend_factory, config_factory):
    config = config_factory(
        permissions={
            "browser": {
                "viewport_width": 800,
                "viewport_height": 600,
                "user_agent": "agentlite-test",
            }
        }
    )
    backend, log = backend_factory(config=config)
    backend.open("https://example.com", 1000)
    context_kwargs = [entry for entry in log if entry[0] == "new_context"][0][1]
    assert context_kwargs["viewport"] == {"width": 800, "height": 600}
    assert context_kwargs["user_agent"] == "agentlite-test"
    assert ("set_default_navigation_timeout", 30000) in log


def test_click_read_type_back(backend_factory):
    backend, log = backend_factory()
    backend.open("https://example.com", 1000)
    backend.click("button#go", 2000)
    backend.type_text("input", "hello", 3000)
    info = backend.read_page(100)
    backend.back(4000)
    assert ("click", "button#go", 2000) in log
    assert ("press_sequentially", "input", "hello", 3000) in log
    assert info["text"] == "page text"
    assert ("go_back", 4000, "domcontentloaded") in log


def test_screenshot_writes_the_file(backend_factory, tmp_path):
    backend, log = backend_factory()
    backend.open("https://example.com", 1000)
    target = tmp_path / "shot.png"
    info = backend.screenshot(target, 1000)
    assert target.exists()
    assert info["path"] == str(target)
    assert ("screenshot", str(target), 1000) in log


def test_close_stops_everything(backend_factory):
    backend, log = backend_factory()
    backend.open("https://example.com", 1000)
    backend.close()
    assert ("context.close",) in log
    assert ("browser.close",) in log
    assert ("stop",) in log
    assert backend.is_open is False


def test_playwright_errors_are_wrapped(backend_factory):
    backend, _log = backend_factory(error=RuntimeError("Executable doesn't exist"))
    with pytest.raises(ToolError) as excinfo:
        backend.open("https://example.com", 100)
    assert "Executable doesn't exist" in str(excinfo.value)


def test_missing_playwright_package_is_reported(monkeypatch, config_factory):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    backend = PlaywrightBackend(config_factory().permissions.browser)
    with pytest.raises(ToolError) as excinfo:
        backend.open("https://example.com", 100)
    assert "pip install" in str(excinfo.value)
