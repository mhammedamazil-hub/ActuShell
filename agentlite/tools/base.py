"""Tool abstraction.

A tool is a small, declarative object:

* ``name``        - dotted tool name advertised to the model (``terminal.run``)
* ``parameters``  - JSON-Schema object describing the arguments
* ``risk``        - how dangerous the tool is (used by the permission engine)
* ``permission_request`` - describes an intended action, without performing it
* ``execute``     - performs the action, and only runs when allowed

Tools never talk to the model and never talk to the permission engine: they
describe what they want, and the executor decides.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.config import Config
from ..core.models import RiskLevel
from ..core.permissions import PermissionRequest


class ToolError(Exception):
    """Raised by tools for expected, reportable failures."""


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about the current run."""

    run_id: str
    config: Config
    workspace: Path
    timeout: int = 30
    actor: str = "agent"


@dataclass
class ToolOutput:
    """Result of a tool execution (the executor wraps it into a ToolResult)."""

    output: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


class Tool(ABC):
    """Base class for every AgentLite tool."""

    name: str = "tool"
    description: str = ""
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk: RiskLevel = RiskLevel.LOW
    #: Family used by the permission engine (``terminal``, ``filesystem``...).
    family: str = "tool"

    # -- contract ---------------------------------------------------------- #

    @abstractmethod
    def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolOutput:
        """Perform the action. Must honour ``context.timeout``."""

    def permission_request(
        self, arguments: Dict[str, Any], context: ToolContext
    ) -> PermissionRequest:
        """Describe the action so the permission engine can judge it."""
        return PermissionRequest(
            tool=self.name,
            action="execute",
            family=self.family,
            risk=self.risk,
            summary=self.describe_call(arguments),
        )

    def describe_call(self, arguments: Dict[str, Any]) -> str:
        """One-line human/LLM readable summary of the call."""
        return f"{self.name}({_brief(arguments)})"

    def timeout_for(self, arguments: Dict[str, Any]) -> int:
        """Wall-clock budget in seconds for this call."""
        requested = arguments.get("timeout")
        try:
            return max(1, int(requested)) if requested is not None else 30
        except (TypeError, ValueError):
            return 30

    # -- helpers ----------------------------------------------------------- #

    @property
    def spec_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk.value,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name}>"


def _brief(arguments: Dict[str, Any], limit: int = 120) -> str:
    parts = []
    for key, value in (arguments or {}).items():
        text = str(value)
        if len(text) > 40:
            text = text[:40] + "..."
        parts.append(f"{key}={text}")
    text = ", ".join(parts)
    return text[:limit]
