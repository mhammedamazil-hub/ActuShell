"""The agent execution loop.

    task -> LLM -> tool request -> permission system -> tool -> result -> LLM
         -> ... -> final answer

The model never touches the machine directly: every action goes through the
:class:`ToolExecutor`, which asks the permission engine first. A run that needs
a human parks itself in ``needs_confirmation`` and can be resumed with an
explicit decision (that is how the HTTP API does it).
"""

from __future__ import annotations

import logging
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..providers.base import LLMProvider, Message, ProviderError
from ..tools.base import ToolContext
from .audit import AuditLogger
from .config import Config
from .executor import ToolExecutor
from .models import (
    ActionRecord,
    AgentRunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolResult,
    new_id,
    truncate,
)
from .registry import ToolRegistry

logger = logging.getLogger("agentlite.agent")

SYSTEM_PROMPT_TEMPLATE = """You are AgentLite, an AI assistant with controlled computer access.

Environment
- OS: {os}
- Workspace: {workspace}
- Shell commands run inside the workspace, without a shell (no pipes, no `&&`).

Tools
{tools}

Rules
1. Do the work with tools. Do not ask the user to run something you can run.
2. Prefer the smallest action that answers the question (read before write).
3. Paths are relative to the workspace; never try to escape it.
4. If an action is denied or needs confirmation, adapt (try another allowed
   approach) and say so in your final answer. Never try to circumvent the
   permission system.
5. Do not attempt to bypass CAPTCHAs, logins, paywalls, rate limits or any
   other access control. If a site blocks you, report it and stop.
6. Finish with a short, concrete summary of what you did and what you found.
"""


@dataclass
class Agent:
    """Wires a provider, a tool registry and the executor together."""

    provider: LLMProvider
    registry: ToolRegistry
    executor: ToolExecutor
    config: Config
    audit: AuditLogger = field(default_factory=lambda: AuditLogger(enabled=False))

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_config(
        cls,
        config: Config,
        provider: Optional[LLMProvider] = None,
        registry: Optional[ToolRegistry] = None,
        confirmation_handler=None,
        audit: Optional[AuditLogger] = None,
    ) -> Agent:
        """Build a ready-to-use agent (used by the CLI, the API and the tests)."""
        from ..providers import build_provider
        from .confirmation import build_handler
        from .permissions import PermissionEngine

        registry = registry or _default_registry(config)
        permissions = PermissionEngine(config)
        handler = confirmation_handler or build_handler(config.security.confirmation_mode)
        audit_logger = audit if audit is not None else _default_audit(config)
        executor = ToolExecutor(
            registry=registry,
            permissions=permissions,
            confirmation=handler,
            audit=audit_logger,
            config=config,
        )
        return cls(
            provider=provider or build_provider(config),
            registry=registry,
            executor=executor,
            config=config,
            audit=audit_logger,
        )

    # -- runs -------------------------------------------------------------- #

    def run(
        self, task: str, max_steps: Optional[int] = None, run_id: Optional[str] = None
    ) -> AgentRunResult:
        return self.create_run(task, max_steps=max_steps, run_id=run_id).resume()

    def create_run(
        self, task: str, max_steps: Optional[int] = None, run_id: Optional[str] = None
    ) -> AgentRun:
        return AgentRun(self, task, max_steps=max_steps, run_id=run_id)

    # -- prompt ------------------------------------------------------------ #

    def system_prompt(self) -> str:
        tools = "\n".join(
            f"- {spec.name}: {spec.description.splitlines()[0] if spec.description else ''}"
            for spec in self.registry.specs()
        )
        return SYSTEM_PROMPT_TEMPLATE.format(
            os=f"{platform.system()} {platform.release()}",
            workspace=self.config.workspace_root,
            tools=tools or "(no tools enabled)",
        )


class AgentRun:
    """State of one task, resumable across confirmation round-trips."""

    def __init__(
        self,
        agent: Agent,
        task: str,
        max_steps: Optional[int] = None,
        run_id: Optional[str] = None,
    ):
        self.agent = agent
        self.task = task
        self.run_id = run_id or new_id("run")
        self.max_steps = max(1, int(max_steps or agent.config.security.max_steps))
        self.messages: List[Message] = [
            Message(role="system", content=agent.system_prompt()),
            Message(role="user", content=task),
        ]
        self.actions: List[ActionRecord] = []
        self.usage = TokenUsage()
        self.step = 0
        self.pending: Optional[Dict[str, Any]] = None
        self.final = ""
        self.error: Optional[str] = None
        self.finished = False
        self.started_at = time.monotonic()
        self.context = ToolContext(
            run_id=self.run_id,
            config=agent.config,
            workspace=agent.config.workspace_root,
            timeout=30,
        )
        agent.audit.log("run_started", run_id=self.run_id, task=task, model=agent.provider.model)

    # -- public API -------------------------------------------------------- #

    def resume(self, confirmation: Optional[bool] = None) -> AgentRunResult:
        """Advance the run.

        ``confirmation`` answers a pending confirmation request (True = allow,
        False = refuse). ``None`` means "keep going / report if blocked".
        """
        if self.finished:
            return self._build(RunStatus.COMPLETED)

        if self.pending is not None:
            if confirmation is None:
                return self._build(RunStatus.NEEDS_CONFIRMATION)
            call = self.pending["call"]
            result = self.agent.executor.execute(
                call, self.context, forced_decision=bool(confirmation)
            )
            self.pending = None
            self._apply(result, call.arguments)

        deadline = self.started_at + self.agent.config.security.max_run_seconds
        while True:
            if self.step >= self.max_steps:
                return self._build(RunStatus.MAX_STEPS_REACHED)
            if time.monotonic() > deadline:
                return self._build(RunStatus.TIMEOUT)

            self.step += 1
            try:
                response = self.agent.provider.complete(
                    self.messages,
                    self.agent.registry.specs(),
                    temperature=self.agent.config.provider.temperature,
                    max_tokens=self.agent.config.provider.max_tokens,
                )
            except ProviderError as exc:
                self.error = str(exc)
                return self._build(RunStatus.FAILED)

            self.usage.add(response.usage)

            if not response.tool_calls:
                self.final = (response.content or "").strip()
                return self._build(RunStatus.COMPLETED)

            self.messages.append(
                Message(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=response.tool_calls,
                )
            )

            for call in response.tool_calls:
                tool_call = ToolCall(
                    id=call.id,
                    name=call.name,
                    arguments=call.arguments(),
                )
                if "_invalid_arguments" in tool_call.arguments:
                    result = ToolResult(
                        call_id=tool_call.id,
                        name=tool_call.name,
                        ok=False,
                        decision="error",
                        error=f"the model sent malformed arguments: {tool_call.arguments}",
                    )
                    self._apply(result, tool_call.arguments)
                    continue

                result = self.agent.executor.execute(tool_call, self.context)
                if result.decision == "needs_confirmation":
                    self.pending = {
                        "call": tool_call,
                        "tool": tool_call.name,
                        "arguments": tool_call.arguments,
                        "reason": result.reason,
                        "summary": _summarize(tool_call),
                    }
                    return self._build(RunStatus.NEEDS_CONFIRMATION)
                self._apply(result, tool_call.arguments)

    # -- internals --------------------------------------------------------- #

    def _apply(self, result: ToolResult, arguments: Optional[Dict[str, Any]] = None) -> None:
        limit = self.agent.config.security.max_tool_result_chars
        self.actions.append(
            ActionRecord(
                step=self.step,
                tool=result.name,
                arguments=arguments or {},
                decision=result.decision,
                reason=result.reason,
                ok=result.ok,
                output_preview=truncate(result.output or result.error or "", 400),
                error=result.error,
                duration_ms=result.duration_ms,
            )
        )
        self.messages.append(
            Message(
                role="tool",
                tool_call_id=result.call_id,
                content=result.to_model_payload(max_chars=limit),
            )
        )

    def _build(self, status: RunStatus) -> AgentRunResult:
        if status is not RunStatus.NEEDS_CONFIRMATION:
            self.finished = True
        if status is RunStatus.MAX_STEPS_REACHED and not self.final:
            self.final = f"Stopped after {self.max_steps} steps without a final answer."
        if status is RunStatus.TIMEOUT and not self.final:
            self.final = (
                f"Stopped after the {self.agent.config.security.max_run_seconds}s run limit."
            )
        result = AgentRunResult(
            run_id=self.run_id,
            status=status,
            result=self.final,
            actions=self.actions,
            steps=self.step,
            error=self.error,
            provider=self.agent.provider.name,
            model=self.agent.provider.model,
            usage=self.usage,
            elapsed_ms=int((time.monotonic() - self.started_at) * 1000),
            pending=_public_pending(self.pending),
        )
        self.agent.audit.log(
            "run_finished",
            run_id=self.run_id,
            status=status.value,
            steps=self.step,
            actions=len(self.actions),
            error=self.error,
        )
        return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _summarize(call: ToolCall) -> str:
    parts = []
    for key, value in call.arguments.items():
        text = str(value)
        parts.append(f"{key}={text[:80]}")
    return f"{call.name}({', '.join(parts)})"


def _public_pending(pending: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not pending:
        return None
    return {key: value for key, value in pending.items() if key != "call"}


def _default_registry(config: Config) -> ToolRegistry:
    from .registry import build_registry

    return build_registry(config)


def _default_audit(config: Config) -> AuditLogger:
    from .audit import build_audit_logger

    return build_audit_logger(
        config.log_path, config.security.audit_log, config.security.redact_secrets
    )
