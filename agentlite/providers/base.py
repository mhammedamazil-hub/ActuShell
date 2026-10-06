"""Provider abstraction.

AgentLite is deliberately not tied to one AI company. A provider is anything
that can turn a list of messages plus tool definitions into either a final
answer or a list of tool calls.

v1 ships one real provider (:class:`OpenAICompatibleProvider`, which speaks the
OpenAI chat-completions wire format used by OpenAI, OpenRouter, Gemini's
OpenAI-compatible endpoint, Ollama, vLLM, LM Studio, Groq...) and a scripted
:class:`MockProvider` used by the tests and the offline demo.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.models import TokenUsage, ToolSpec


class ProviderError(Exception):
    """Raised when the model backend cannot be reached or answers nonsense."""


@dataclass
class ToolCallRequest:
    """A tool call as returned by a model."""

    id: str
    name: str
    arguments_json: str = "{}"

    def arguments(self) -> Dict[str, Any]:
        try:
            parsed = json.loads(self.arguments_json or "{}")
        except json.JSONDecodeError:
            return {"_invalid_arguments": self.arguments_json}
        if not isinstance(parsed, dict):
            return {"_invalid_arguments": parsed}
        return parsed


@dataclass
class Message:
    """Provider-agnostic chat message."""

    role: str  # system | user | assistant | tool
    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any = None


class LLMProvider(ABC):
    """Interface every provider must implement."""

    name: str = "base"
    model: str = "unknown"

    @abstractmethod
    def complete(
        self,
        messages: List[Message],
        tools: List[ToolSpec],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Send a conversation to the model and return its reply."""

    def describe(self) -> Dict[str, Any]:
        """Operator-facing description (never includes the key itself)."""
        return {"name": self.name, "model": self.model}

    def close(self) -> None:  # noqa: B027 - optional hook, not part of the contract
        """Release resources. Providers that hold a connection override this."""


# --------------------------------------------------------------------------- #
# Tool name mapping
# --------------------------------------------------------------------------- #


#: Some backends reject dots in function names; map to underscores on the wire.
def to_wire_name(name: str) -> str:
    return name.replace(".", "_")


def from_wire_name(wire: str, known: Optional[List[str]] = None) -> str:
    """Map a model-supplied function name back to the canonical tool name.

    AgentLite tool names contain dots, but several backends rewrite or reject
    them, so ``terminal_run`` and ``terminal.run`` both mean the same tool.
    """
    if not wire:
        return wire
    if known and wire in known:
        return wire
    dotted = wire.replace("__", ".").replace("_", ".", 1) if "_" in wire else wire
    if known is None or dotted in known:
        return dotted
    # Last resort: compare names ignoring separators.
    normalized = wire.replace("_", "").replace(".", "").lower()
    for candidate in known:
        if candidate.replace(".", "").lower() == normalized:
            return candidate
    return dotted
