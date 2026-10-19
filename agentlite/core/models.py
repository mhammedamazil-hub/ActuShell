"""Core data structures shared by the agent loop, the tools, the permission
system and the HTTP API.

Everything here is plain data (dataclasses). Keeping the models dependency free
means tools, providers and the API can all speak the same language without
importing each other.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def new_id(prefix: str = "run") -> str:
    """Short, sortable-enough unique identifier."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


#: Tool metadata worth sending back to the model, in payload order.
_PAYLOAD_META_KEYS = ("exit_code", "cwd", "path", "url", "stdout", "stderr", "entries", "timed_out")


def _cap_entries(key: str, value: Any, entries_cap: int, budget: int) -> Any:
    """Keep metadata small enough to be worth its tokens."""
    if isinstance(value, str):
        return truncate(value, budget)
    if isinstance(value, list):
        max_items = entries_cap if key == "entries" else min(entries_cap, 20)
        if len(value) > max_items:
            return value[:max_items] + [f"...{len(value) - max_items} more"]
        return value
    return value


def truncate(text: str, limit: int) -> str:
    """Truncate text in the middle, keeping head and tail (tails carry errors)."""
    if text is None:
        return ""
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(0, int(limit * 0.7))
    tail = max(0, limit - head - 25)
    return f"{text[:head]}\n...[truncated]...\n{text[-tail:] if tail else ''}"


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #


class RiskLevel(str, Enum):
    """How much damage a tool call could do if it misbehaved."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@dataclass
class ToolSpec:
    """Description of a tool, in a provider-agnostic shape."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW

    def to_provider_tool(self, wire_name: Optional[str] = None) -> Dict[str, Any]:
        """OpenAI-style function definition (also understood by compatible APIs)."""
        return {
            "type": "function",
            "function": {
                "name": wire_name or self.name,
                "description": self.description,
                "parameters": self.parameters
                or {"type": "object", "properties": {}, "required": []},
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk.value,
        }


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Outcome of a tool invocation, whether or not it was allowed to run."""

    call_id: str
    name: str
    ok: bool
    output: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None
    decision: str = "allow"  # allow | denied | confirmation_denied | error
    reason: str = ""
    duration_ms: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_model_payload(self, max_chars: int = 16000) -> str:
        """Compact JSON payload handed back to the model as the tool message.

        The cap applies to the *serialised* payload, not just to ``output``:
        a directory listing of 500 entries is metadata, and metadata counts
        against the context window too. Whatever happens the return value is
        valid JSON -- a payload cut mid-string would break the provider call.
        """
        limit = max(500, int(max_chars or 16000))

        def build(entries_cap: int) -> Dict[str, Any]:
            fields: Dict[str, Any] = {"ok": self.ok, "tool": self.name}
            if self.error:
                fields["error"] = truncate(self.error, 1000 if entries_cap else 500)
            if entries_cap:
                for key in _PAYLOAD_META_KEYS:
                    if key in self.meta:
                        fields[key] = _cap_entries(key, self.meta[key], entries_cap, 4000)
            if not self.ok and self.decision != "allow":
                fields["permission"] = self.decision
                if self.reason:
                    fields["reason"] = truncate(self.reason, 500)
            return fields

        def serialise(output: str, fields: Dict[str, Any]) -> str:
            return json.dumps({**fields, "output": output}, ensure_ascii=False)

        best = build(50)
        text = serialise(truncate(self.output, limit), best)
        if len(text) <= limit:
            return text

        # Trim the metadata first, then the output: a run summary without the
        # full file list is still useful, a file list without the summary is not.
        for entries_cap in (50, 20, 5, 0):
            fields = build(entries_cap)
            available = limit - len(serialise("", fields))
            if available >= 64:
                return serialise(truncate(self.output, available), fields)

        # Nothing fits; say so in a form the model can still reason about.
        return json.dumps(
            {
                "ok": self.ok,
                "tool": self.name,
                "output": f"...omitted: the result is larger than the {limit} character limit",
            },
            ensure_ascii=False,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


@dataclass
class ActionRecord:
    """One line in the `actions` list returned by the API / CLI."""

    step: int
    tool: str
    arguments: Dict[str, Any]
    decision: str
    reason: str = ""
    ok: bool = True
    output_preview: str = ""
    error: Optional[str] = None
    duration_ms: int = 0
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    MAX_STEPS_REACHED = "max_steps_reached"
    TIMEOUT = "timeout"


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0

    def add(self, other: TokenUsage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.requests += other.requests

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentRunResult:
    """Structured result of a full agent loop."""

    run_id: str
    status: RunStatus
    result: str = ""
    actions: List[ActionRecord] = field(default_factory=list)
    steps: int = 0
    error: Optional[str] = None
    provider: str = ""
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    elapsed_ms: int = 0
    pending: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["usage"] = self.usage.to_dict()
        data["actions"] = [a.to_dict() for a in self.actions]
        return data
