"""Tool executor.

The executor is the only path from a model request to a real action:

    tool lookup -> argument check -> permission request -> engine decision
    -> confirmation (if any) -> execution -> audit log -> ToolResult

Nothing else in AgentLite is allowed to touch the machine directly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..tools.base import ToolContext, ToolError
from .audit import AuditLogger
from .config import Config
from .confirmation import ConfirmationHandler, ConfirmationRequired, DenyHandler
from .models import ToolCall, ToolResult, new_id
from .permissions import Effect, PermissionEngine, PermissionRequest
from .registry import ToolRegistry

logger = logging.getLogger("agentlite.executor")


@dataclass
class ToolContextImpl(ToolContext):
    run_id: str = field(default_factory=lambda: new_id("run"))
    config: Optional[Config] = None
    workspace: Path = field(default_factory=Path.cwd)
    timeout: int = 30
    actor: str = "agent"


class ToolExecutor:
    """Runs tool calls behind the permission system."""

    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        confirmation: ConfirmationHandler,
        audit: Optional[AuditLogger] = None,
        config: Optional[Config] = None,
    ):
        self.registry = registry
        self.permissions = permissions
        self.confirmation = confirmation
        self.audit = audit or AuditLogger(enabled=False)
        self.config = config

    # -- main entry point -------------------------------------------------- #

    def execute(
        self,
        call: ToolCall,
        context: ToolContext,
        forced_decision: Optional[bool] = None,
    ) -> ToolResult:
        started = time.monotonic()
        tool = self.registry.get(call.name)

        if tool is None:
            return self._finish(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    decision="error",
                    error=f"unknown tool: {call.name}",
                    reason="not registered",
                ),
                started,
                context,
            )

        if not self.registry.is_enabled(call.name):
            entry_reason = next((e.reason for e in self.registry if e.tool.name == call.name), "")
            return self._finish(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    decision="denied",
                    error=f"tool {call.name} is disabled",
                    reason=entry_reason or "disabled by configuration",
                ),
                started,
                context,
            )

        missing = _missing_required(tool.parameters, call.arguments)
        if missing:
            return self._finish(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    decision="error",
                    error=f"missing required argument(s): {', '.join(missing)}",
                    reason="invalid arguments",
                ),
                started,
                context,
            )

        try:
            request = tool.permission_request(call.arguments, context)
        except ToolError as exc:
            return self._finish(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    decision="error",
                    error=str(exc),
                    reason="invalid request",
                ),
                started,
                context,
            )

        decision = self.permissions.evaluate(request)
        logger.debug("permission %s -> %s (%s)", call.name, decision.effect.value, decision.reason)

        if decision.effect is Effect.CONFIRM:
            decision, blocked = self._resolve_confirmation(request, decision, forced_decision)
            if blocked is not None:
                # blocked is True -> deferred to a human (API), False -> refused.
                return self._finish(
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        ok=False,
                        decision="needs_confirmation" if blocked else "denied",
                        error=(
                            "confirmation required" if blocked else f"denied: {decision.reason}"
                        ),
                        reason=decision.reason,
                    ),
                    started,
                    context,
                    request=request,
                )

        if decision.effect is Effect.DENY:
            return self._finish(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    decision="denied",
                    error=f"denied by permission system: {decision.reason}",
                    reason=decision.reason,
                ),
                started,
                context,
                request=request,
            )

        context.timeout = tool.timeout_for(call.arguments)
        try:
            produced = tool.execute(call.arguments, context)
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                ok=produced.ok,
                output=produced.output or "",
                error=produced.error,
                exit_code=produced.exit_code,
                meta=produced.meta,
                decision="allow",
            )
        except ToolError as exc:
            result = ToolResult(
                call_id=call.id, name=call.name, ok=False, decision="error", error=str(exc)
            )
        except TimeoutError as exc:
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                decision="error",
                error=f"timed out after {context.timeout}s: {exc}",
                meta={"timed_out": True},
            )
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the run
            logger.exception("tool %s crashed", call.name)
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                decision="error",
                error=f"{type(exc).__name__}: {exc}",
            )

        result.decision = result.decision or "allow"
        result.reason = result.reason or decision.reason
        return self._finish(result, started, context, request=request)

    # -- internals --------------------------------------------------------- #

    def _resolve_confirmation(self, request, decision, forced_decision):
        """Turn CONFIRM into ALLOW/DENY, or signal that a human is needed.

        Returns ``(decision, blocked)`` where ``blocked`` is None when the call
        may proceed, True when it must wait for a human and False when refused.
        """
        from .permissions import Decision

        if forced_decision is True:
            return Decision(Effect.ALLOW, "approved by user"), None
        if forced_decision is False:
            return Decision(Effect.DENY, "refused by user"), False

        mode = (self.config.security.confirmation_mode if self.config else "prompt").lower()
        if mode == "allow":
            return Decision(Effect.ALLOW, "auto-approved (confirmation_mode=allow)"), None
        if mode == "deny" or isinstance(self.confirmation, DenyHandler):
            return (
                Decision(
                    Effect.DENY,
                    f"confirmation required but unavailable (mode={mode}); {decision.reason}",
                ),
                False,
            )

        try:
            approved = self.confirmation.confirm(request, decision)
        except ConfirmationRequired:
            return decision, True
        if approved:
            return Decision(Effect.ALLOW, "approved by user"), None
        return Decision(Effect.DENY, "refused by user"), False

    def _finish(
        self,
        result: ToolResult,
        started: float,
        context: ToolContext,
        request: Optional[PermissionRequest] = None,
    ) -> ToolResult:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        payload: Dict[str, Any] = {
            "run_id": getattr(context, "run_id", ""),
            "tool": result.name,
            "arguments": (request.command if request else None)
            or (str(request.path) if request and request.path else None)
            or (request.url if request and request.url else None),
            "decision": result.decision,
            "reason": result.reason,
            "ok": result.ok,
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
        }
        if self.config and self.config.security.log_tool_output:
            payload["output"] = result.output[: self.config.security.max_output_preview]
        if result.error:
            payload["error"] = (
                result.error[: self.config.security.max_output_preview]
                if self.config
                else result.error
            )
        self.audit.log("tool_call", **payload)
        return result


def _missing_required(parameters: Dict[str, Any], arguments: Dict[str, Any]) -> list:
    required = (parameters or {}).get("required", [])
    return [name for name in required if name not in arguments or arguments[name] is None]
