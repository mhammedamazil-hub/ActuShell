"""Confirmation handlers: how a human (or the absence of one) is handled.

These decide whether an action happens, so every branch matters.
"""

from __future__ import annotations

import pytest

from agentlite.core.confirmation import (
    AutoAllowHandler,
    CliConfirmationHandler,
    ConfirmationRequired,
    DenyHandler,
    PendingConfirmationHandler,
    build_handler,
)
from agentlite.core.models import RiskLevel
from agentlite.core.permissions import Decision, Effect, PermissionRequest


@pytest.fixture
def request_() -> PermissionRequest:
    return PermissionRequest(
        tool="terminal.run",
        family="terminal",
        action="execute",
        risk=RiskLevel.HIGH,
        command="rm notes.txt",
        path=None,
        url=None,
        summary="run command",
    )


@pytest.fixture
def decision() -> Decision:
    return Decision(Effect.CONFIRM, "command needs confirmation", rule="terminal.confirm")


def test_auto_allow_approves_everything(request_, decision):
    assert AutoAllowHandler().confirm(request_, decision) is True


def test_deny_handler_refuses_and_is_reported(request_, decision):
    assert DenyHandler().confirm(request_, decision) is False


def test_pending_handler_defers_to_the_caller(request_, decision):
    with pytest.raises(ConfirmationRequired) as excinfo:
        PendingConfirmationHandler().confirm(request_, decision)
    assert excinfo.value.request is request_


def test_cli_prompt_shows_the_action_and_accepts_yes(request_, decision, capsys):
    shown: list[str] = []
    handler = CliConfirmationHandler(stdin=lambda prompt: "y", stdout=shown.append)
    assert handler.confirm(request_, decision) is True
    text = "\n".join(shown)
    assert "rm notes.txt" in text
    assert "command needs confirmation" in text


@pytest.mark.parametrize("answer", ["n", "N", "", "no", "maybe"])
def test_cli_prompt_refuses_anything_but_yes(request_, decision, answer):
    handler = CliConfirmationHandler(stdin=lambda prompt: answer, stdout=lambda text: None)
    assert handler.confirm(request_, decision) is False


def test_cli_prompt_refuses_when_there_is_no_one_to_answer(request_, decision):
    """A pipe or a cron job must never be treated as consent."""

    def eof(_prompt):
        raise EOFError

    def interrupted(_prompt):
        raise KeyboardInterrupt

    assert (
        CliConfirmationHandler(stdin=eof, stdout=lambda t: None).confirm(request_, decision)
        is False
    )
    assert (
        CliConfirmationHandler(stdin=interrupted, stdout=lambda t: None).confirm(request_, decision)
        is False
    )


def test_cli_prompt_shows_paths_and_urls():
    shown: list[str] = []
    handler = CliConfirmationHandler(stdin=lambda prompt: "n", stdout=shown.append)
    request = PermissionRequest(
        tool="filesystem.write",
        family="filesystem",
        action="write",
        risk=RiskLevel.MEDIUM,
        path="/workspace/notes.txt",
        summary="write file",
    )
    handler.confirm(request, Decision(Effect.CONFIRM, "path is writable", rule="x"))
    text = "\n".join(shown)
    assert "/workspace/notes.txt" in text


def test_build_handler_maps_modes_to_handlers():
    assert isinstance(build_handler("allow"), AutoAllowHandler)
    assert isinstance(build_handler("deny"), DenyHandler)
    # "prompt" depends on the entry point: a tty asks, a server defers.
    assert isinstance(build_handler("prompt", interactive=True), CliConfirmationHandler)
    assert isinstance(build_handler("prompt", interactive=False), PendingConfirmationHandler)
    # An unknown mode falls back to prompting rather than approving.
    assert isinstance(build_handler("nonsense"), CliConfirmationHandler)
