"""Scripted provider used by the test suite and the offline demo.

This is *not* a language model. It replays a fixed script of tool calls so the
agent loop, the permission system and the tools can be exercised end to end
without an API key or network access. It is intentionally simple, and it is
never the default provider for real use.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, Callable, Dict, List, Optional, Union

from ..core.models import ToolSpec
from .base import LLMProvider, LLMResponse, Message, ToolCallRequest

Step = Union[str, Sequence[tuple], Callable[[List[Message], List[ToolSpec]], Any]]

#: Script used when no script is supplied: inspect the workspace, then report.
DEFAULT_SCRIPT: List[Step] = [
    [("filesystem.list", {"path": "."})],
    [("filesystem.write", {"path": "report.md", "content": "# Workspace report\n"})],
    "I listed the workspace and wrote report.md.",
]


class MockProvider(LLMProvider):
    """Replays a script of assistant turns."""

    name = "mock"

    def __init__(self, steps: Optional[Iterable[Step]] = None, model: str = "mock-1"):
        self.model = model
        self.steps: List[Step] = list(steps) if steps is not None else list(DEFAULT_SCRIPT)
        self.index = 0
        self.requests: List[Dict[str, Any]] = []
        self.last_messages: List[Message] = []

    @classmethod
    def from_script(cls, *steps: Step, model: str = "mock-1") -> MockProvider:
        return cls(steps=list(steps), model=model)

    def complete(
        self,
        messages: List[Message],
        tools: List[ToolSpec],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        self.last_messages = list(messages)
        self.requests.append(
            {
                "messages": len(messages),
                "tools": [tool.name for tool in tools],
                "roles": [message.role for message in messages],
            }
        )
        if self.index >= len(self.steps):
            return LLMResponse(content="(mock provider: script exhausted)", finish_reason="stop")

        step = self.steps[self.index]
        self.index += 1
        if callable(step):
            step = step(messages, tools)
        if isinstance(step, str):
            return LLMResponse(content=step, finish_reason="stop")

        calls: List[ToolCallRequest] = []
        for position, item in enumerate(step):
            name, arguments = item
            calls.append(
                ToolCallRequest(
                    id=f"call_{self.index}_{position}",
                    name=name,
                    arguments_json=arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments),
                )
            )
        return LLMResponse(tool_calls=calls, finish_reason="tool_calls")

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "scripted_steps": len(self.steps),
        }
