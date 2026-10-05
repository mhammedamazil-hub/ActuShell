"""Confirmation handlers.

The permission engine only says "this needs a human". *How* that human is
reached is the handler's job, so the same engine works for an interactive CLI,
a headless job or an HTTP API that has to round-trip to a client.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional, Protocol

from .permissions import Decision, PermissionRequest


class ConfirmationRequired(Exception):
    """Raised when a handler cannot answer inline (e.g. the HTTP API)."""

    def __init__(self, request: PermissionRequest, decision: Decision):
        super().__init__(decision.reason)
        self.request = request
        self.decision = decision


class ConfirmationHandler(Protocol):
    def confirm(self, request: PermissionRequest, decision: Decision) -> bool:  # pragma: no cover
        ...


class AutoAllowHandler:
    """Approves everything (development, demos, scripted runs)."""

    name = "auto-allow"

    def confirm(self, request: PermissionRequest, decision: Decision) -> bool:
        return True


class DenyHandler:
    """Refuses everything that would need confirmation."""

    name = "deny"

    def confirm(self, request: PermissionRequest, decision: Decision) -> bool:
        return False


class CliConfirmationHandler:
    """Prompts on the terminal (used by ``agentlite run``)."""

    name = "cli-prompt"

    def __init__(
        self,
        stdin: Optional[Callable[[str], str]] = None,
        stdout: Optional[Callable[[str], None]] = None,
    ):
        self._stdin = stdin or input
        self._stdout = stdout or (lambda text: print(text, file=sys.stderr))

    def confirm(self, request: PermissionRequest, decision: Decision) -> bool:
        self._stdout("")
        self._stdout(f"  [confirm] {request.tool}: {request.summary}")
        if request.command:
            self._stdout(f"  command : {request.command}")
        if request.path:
            self._stdout(f"  path    : {request.path}")
        if request.url:
            self._stdout(f"  url     : {request.url}")
        self._stdout(f"  reason  : {decision.reason}")
        try:
            answer = self._stdin("  Allow? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}


class PendingConfirmationHandler:
    """Defers the answer to the caller (HTTP API).

    Raises :class:`ConfirmationRequired`; the agent loop turns it into a
    ``needs_confirmation`` run that can be resumed with an explicit decision.
    """

    name = "pending"

    def confirm(self, request: PermissionRequest, decision: Decision) -> bool:
        raise ConfirmationRequired(request, decision)


def build_handler(mode: str, interactive: bool = True) -> ConfirmationHandler:
    """Pick the handler for ``security.confirmation_mode``.

    ``mode`` is one of prompt | allow | deny. The handler for ``prompt`` depends
    on the entry point: an interactive CLI asks on stdin, a server defers.
    """
    if mode == "allow":
        return AutoAllowHandler()
    if mode == "deny":
        return DenyHandler()
    return CliConfirmationHandler() if interactive else PendingConfirmationHandler()
