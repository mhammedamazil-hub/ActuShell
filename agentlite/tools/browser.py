"""Browser tools, backed by Playwright (optional dependency).

Tools: ``browser.open``, ``browser.click``, ``browser.type``,
``browser.read_page``, ``browser.screenshot``, ``browser.back`` and
``browser.close``.

The Playwright implementation lives behind the :class:`BrowserBackend`
interface, so another engine (CDP client, headless Firefox, a remote browser)
can be dropped in without touching the tools or the agent loop.

Security note: this drives a normal browser through its normal user-facing
interfaces. It deliberately contains nothing to defeat CAPTCHAs, anti-bot
systems, rate limits or authentication - automating those is the user's
responsibility, not AgentLite's job.
"""

from __future__ import annotations

import importlib.util
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel
from ..core.permissions import PermissionRequest
from .base import Tool, ToolContext, ToolError, ToolOutput

INSTALL_HINT = (
    "Playwright is not installed. Run: "
    "pip install 'agentlite[browser]' && playwright install chromium"
)


def browser_binary_installed() -> bool:
    """True when Playwright's browsers directory looks populated."""
    import os

    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
        Path.home() / ".cache" / "ms-playwright"
    )
    try:
        return Path(root).is_dir() and any(Path(root).iterdir())
    except OSError:  # pragma: no cover - unreadable directory
        return False


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


class BrowserBackend(ABC):
    """Interface a browser implementation must provide."""

    @abstractmethod
    def open(self, url: str, timeout_ms: int) -> Dict[str, Any]: ...

    @abstractmethod
    def click(self, selector: str, timeout_ms: int) -> Dict[str, Any]: ...

    @abstractmethod
    def type_text(self, selector: str, text: str, timeout_ms: int) -> Dict[str, Any]: ...

    @abstractmethod
    def read_page(self, max_chars: int) -> Dict[str, Any]: ...

    @abstractmethod
    def screenshot(self, path: Path, timeout_ms: int) -> Dict[str, Any]: ...

    @abstractmethod
    def back(self, timeout_ms: int) -> Dict[str, Any]: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...


class PlaywrightBackend(BrowserBackend):
    """Chromium via Playwright's synchronous API."""

    def __init__(self, policy: Any):
        self.policy = policy
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # -- lifecycle --------------------------------------------------------- #

    def _page_or_start(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ToolError(INSTALL_HINT) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.policy.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context_kwargs: Dict[str, Any] = {
            "viewport": {
                "width": self.policy.viewport_width,
                "height": self.policy.viewport_height,
            }
        }
        if self.policy.user_agent:
            context_kwargs["user_agent"] = self.policy.user_agent
        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(self.policy.timeout_ms)
        self._context.set_default_navigation_timeout(self.policy.navigation_timeout_ms)
        self._page = self._context.new_page()
        return self._page

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface Playwright errors cleanly
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    # -- operations -------------------------------------------------------- #

    def open(self, url: str, timeout_ms: int) -> Dict[str, Any]:
        def _go():
            page = self._page_or_start()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return {"url": page.url, "title": page.title()}

        return self._call(_go)

    def click(self, selector: str, timeout_ms: int) -> Dict[str, Any]:
        def _click():
            page = self._page_or_start()
            page.click(selector, timeout=timeout_ms)
            return {"selector": selector, "url": page.url, "title": page.title()}

        return self._call(_click)

    def type_text(self, selector: str, text: str, timeout_ms: int) -> Dict[str, Any]:
        def _type():
            page = self._page_or_start()
            locator = page.locator(selector)
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            if hasattr(locator, "press_sequentially"):
                locator.press_sequentially(text, delay=0, timeout=timeout_ms)
            else:  # pragma: no cover - older Playwright
                locator.type(text, timeout=timeout_ms)
            return {"selector": selector, "characters": len(text), "url": page.url}

        return self._call(_type)

    def read_page(self, max_chars: int) -> Dict[str, Any]:
        def _read():
            page = self._page_or_start()
            title = page.title()
            text = page.evaluate("() => (document.body ? document.body.innerText : '')")
            text = (text or "").strip()
            return {
                "url": page.url,
                "title": title,
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
                "length": len(text),
            }

        return self._call(_read)

    def screenshot(self, path: Path, timeout_ms: int) -> Dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)

        def _shot():
            page = self._page_or_start()
            page.screenshot(path=str(path), timeout=timeout_ms)
            return {"path": str(path), "bytes": path.stat().st_size, "url": page.url}

        return self._call(_shot)

    def back(self, timeout_ms: int) -> Dict[str, Any]:
        def _back():
            page = self._page_or_start()
            page.go_back(timeout=timeout_ms, wait_until="domcontentloaded")
            return {"url": page.url, "title": page.title()}

        return self._call(_back)

    def close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        self._page = self._context = self._browser = self._playwright = None

    @property
    def is_open(self) -> bool:
        return self._page is not None


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class BrowserSession:
    """Owns the browser backend so all browser tools share one page."""

    def __init__(self, config: Any, backend: Optional[BrowserBackend] = None):
        self.config = config
        self._backend = backend
        self._policy = config.permissions.browser

    @property
    def policy(self):
        return self._policy

    def is_available(self) -> bool:
        if self._backend is not None:
            return True
        if importlib.util.find_spec("playwright") is None:
            return False
        return browser_binary_installed()

    def availability_reason(self) -> str:
        if self._backend is not None:
            return "ok"
        if importlib.util.find_spec("playwright") is None:
            return INSTALL_HINT
        if not browser_binary_installed():
            return (
                "the playwright package is installed but no browser binary was found "
                "(run: playwright install chromium)"
            )
        return "ok"

    def backend(self) -> BrowserBackend:
        if self._backend is None:
            if not self.is_available():
                raise ToolError(INSTALL_HINT)
            self._backend = PlaywrightBackend(self._policy)
        return self._backend

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


class BrowserToolBase(Tool):
    family = "browser"
    needs_page = True

    def __init__(self, session: BrowserSession):
        self.session = session

    def _page_required(self) -> None:
        if self.needs_page and not self.session.is_available():
            raise ToolError(INSTALL_HINT)

    def timeout_for(self, arguments: Dict[str, Any]) -> int:
        # Playwright enforces timeout_ms itself; this budget adds a little slack
        # for start-up so the executor never cuts a legitimate call short.
        return max(2, int(self.session.policy.timeout_ms / 1000) + 2)

    def close(self) -> None:
        """Release the shared browser (safe to call repeatedly)."""
        self.session.close()


class BrowserOpenTool(BrowserToolBase):
    name = "browser.open"
    needs_page = False
    risk = RiskLevel.MEDIUM
    description = (
        "Open a URL in the headless browser and wait for the page to load. "
        "Use browser.read_page afterwards to read the text of the page."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL to open."}},
        "required": ["url"],
    }

    def permission_request(
        self, arguments: Dict[str, Any], context: ToolContext
    ) -> PermissionRequest:
        url = str(arguments.get("url", ""))
        return PermissionRequest(
            tool=self.name,
            action="navigate",
            family=self.family,
            risk=self.risk,
            summary=f"open {url}",
            url=url,
        )

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        url = str(arguments.get("url", "")).strip()
        if not url:
            raise ToolError("url is required")
        info = self.session.backend().open(url, self.session.policy.navigation_timeout_ms)
        return ToolOutput(
            output=f"opened {info.get('url')} - {info.get('title', '')}".strip(),
            meta=info,
        )


class BrowserClickTool(BrowserToolBase):
    name = "browser.click"
    risk = RiskLevel.MEDIUM
    description = "Click an element matching a CSS selector on the current page."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector, e.g. 'button.submit'."}
        },
        "required": ["selector"],
    }

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        selector = str(arguments.get("selector", "")).strip()
        if not selector:
            raise ToolError("selector is required")
        info = self.session.backend().click(selector, self.session.policy.timeout_ms)
        return ToolOutput(output=f"clicked {selector}", meta=info)


class BrowserTypeTool(BrowserToolBase):
    name = "browser.type"
    risk = RiskLevel.MEDIUM
    description = (
        "Type text into an input matching a CSS selector (clears the field first "
        "when using fill semantics). Does not press Enter."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector of the input."},
            "text": {"type": "string", "description": "Text to type."},
        },
        "required": ["selector", "text"],
    }

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        selector = str(arguments.get("selector", "")).strip()
        text = str(arguments.get("text", ""))
        if not selector:
            raise ToolError("selector is required")
        info = self.session.backend().type_text(selector, text, self.session.policy.timeout_ms)
        return ToolOutput(output=f"typed {len(text)} characters into {selector}", meta=info)

    def describe_call(self, arguments: Dict[str, Any]) -> str:
        text = str(arguments.get("text", ""))
        return f"browser.type(selector={arguments.get('selector')!r}, text={text[:40]!r})"


class BrowserReadPageTool(BrowserToolBase):
    name = "browser.read_page"
    risk = RiskLevel.LOW
    description = (
        "Read the visible text of the current page (title, URL and body text). "
        "Cheaper than a screenshot and usually enough to answer a question."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return (clamped by configuration).",
            }
        },
        "required": [],
    }

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        max_chars = min(
            int(arguments.get("max_chars") or self.session.policy.max_page_chars),
            self.session.policy.max_page_chars,
        )
        info = self.session.backend().read_page(max_chars)
        header = f"# {info.get('title', '')}\n{info.get('url', '')}\n"
        text = info.get("text", "")
        return ToolOutput(output=(header + "\n" + text).strip(), meta=info)


class BrowserScreenshotTool(BrowserToolBase):
    name = "browser.screenshot"
    risk = RiskLevel.LOW
    description = (
        "Save a PNG screenshot of the current page into the workspace and return the file path."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional file name (relative to the screenshot directory).",
            }
        },
        "required": [],
    }

    def permission_request(
        self, arguments: Dict[str, Any], context: ToolContext
    ) -> PermissionRequest:
        target = self._target_path(arguments, context)
        return PermissionRequest(
            tool=self.name,
            action="write",
            family=self.family,
            risk=self.risk,
            summary=f"save screenshot to {target}",
            path=target,
            meta={"url": None},
        )

    def _target_path(self, arguments: Dict[str, Any], context: ToolContext) -> Path:
        directory = context.config.screenshot_dir
        requested = arguments.get("path")
        if requested:
            candidate = Path(str(requested))
            if candidate.is_absolute():
                raise ToolError("screenshot path must be relative")
            target = (directory / candidate).resolve()
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = (directory / f"screenshot-{stamp}.png").resolve()
        directory_resolved = directory.resolve()
        if not (target == directory_resolved or directory_resolved in target.parents):
            raise ToolError(f"screenshot path escapes the screenshot directory: {target}")
        directory_resolved.mkdir(parents=True, exist_ok=True)
        return target

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        target = self._target_path(arguments, context)
        info = self.session.backend().screenshot(target, self.session.policy.timeout_ms)
        return ToolOutput(output=f"screenshot saved to {target}", meta=info)


class BrowserBackTool(BrowserToolBase):
    name = "browser.back"
    risk = RiskLevel.LOW
    description = "Go back to the previous page in the browser history."

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        info = self.session.backend().back(self.session.policy.navigation_timeout_ms)
        return ToolOutput(output=f"went back to {info.get('url')}", meta=info)


class BrowserCloseTool(BrowserToolBase):
    name = "browser.close"
    needs_page = False
    risk = RiskLevel.LOW
    description = "Close the browser and release its memory."

    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        self.session.close()
        return ToolOutput(output="browser closed")


def browser_tools(config: Any, session: Optional[BrowserSession] = None) -> List[Tool]:
    """Build the browser tools sharing a single session."""
    session = session or BrowserSession(config)
    return [
        BrowserOpenTool(session),
        BrowserClickTool(session),
        BrowserTypeTool(session),
        BrowserReadPageTool(session),
        BrowserScreenshotTool(session),
        BrowserBackTool(session),
        BrowserCloseTool(session),
    ]
