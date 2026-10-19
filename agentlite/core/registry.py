"""Tool registry.

The registry is the single place that knows which tools exist. Both the agent
loop (to advertise tools to the model) and the API (to list them) read from it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..tools.base import Tool
from .config import Config
from .models import ToolSpec


@dataclass
class RegisteredTool:
    tool: Tool
    enabled: bool
    reason: str = ""


class ToolRegistry:
    """Holds tool instances and their enabled/disabled state."""

    def __init__(self) -> None:
        self._tools: Dict[str, RegisteredTool] = {}
        #: Browser session shared by the browser tools (may be ``None``).
        self.browser_session: Optional[object] = None

    def close(self) -> None:
        """Release resources held by tools (e.g. the browser process)."""
        if self.browser_session is not None:
            closer = getattr(self.browser_session, "close", None)
            if callable(closer):
                closer()

    # -- registration ------------------------------------------------------ #

    def register(self, tool: Tool, enabled: bool = True, reason: str = "") -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = RegisteredTool(tool=tool, enabled=enabled, reason=reason)

    def set_enabled(self, name: str, enabled: bool, reason: str = "") -> None:
        entry = self._tools.get(name)
        if entry is None:
            raise KeyError(name)
        entry.enabled = enabled
        entry.reason = reason

    # -- lookup ------------------------------------------------------------ #

    def get(self, name: str) -> Optional[Tool]:
        entry = self._tools.get(name)
        return entry.tool if entry else None

    def has(self, name: str) -> bool:
        return name in self._tools

    def is_enabled(self, name: str) -> bool:
        entry = self._tools.get(name)
        return bool(entry and entry.enabled)

    def enabled_tools(self) -> List[Tool]:
        return [e.tool for e in self._tools.values() if e.enabled]

    def names(self) -> List[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools.values())

    # -- descriptions ------------------------------------------------------ #

    def specs(self) -> List[ToolSpec]:
        return [
            ToolSpec(
                name=e.tool.name,
                description=e.tool.description,
                parameters=e.tool.parameters,
                risk=e.tool.risk,
            )
            for e in self._tools.values()
            if e.enabled
        ]

    def describe(self) -> List[Dict[str, object]]:
        return [
            {
                "name": e.tool.name,
                "description": e.tool.description,
                "risk": e.tool.risk.value,
                "enabled": e.enabled,
                "reason": e.reason,
                "parameters": e.tool.parameters,
            }
            for e in self._tools.values()
        ]


def build_registry(config: Config) -> ToolRegistry:
    """Create the default registry for a configuration.

    Tools are imported lazily so that a missing optional dependency (Playwright)
    never breaks startup: the tool is registered but marked disabled, with the
    reason shown by ``agentlite doctor``.
    """
    from ..tools.browser import BrowserSession, browser_tools
    from ..tools.filesystem import filesystem_tools
    from ..tools.terminal import TerminalTool

    registry = ToolRegistry()

    registry.register(
        TerminalTool(),
        enabled=config.permissions.terminal.enabled,
        reason="" if config.permissions.terminal.enabled else "disabled in configuration",
    )
    for tool in filesystem_tools():
        registry.register(
            tool,
            enabled=config.permissions.filesystem.enabled,
            reason="" if config.permissions.filesystem.enabled else "disabled in configuration",
        )

    session = BrowserSession(config)
    available = session.is_available()
    for tool in browser_tools(config, session):
        if not config.permissions.browser.enabled:
            registry.register(tool, enabled=False, reason="disabled in configuration")
        elif not available:
            registry.register(tool, enabled=False, reason=session.availability_reason())
        else:
            registry.register(tool, enabled=True, reason="ok")

    registry.browser_session = session  # type: ignore[attr-defined]
    return registry
