"""Filesystem tool tests, focused on containment."""

from __future__ import annotations

import pytest

from agentlite.core.agent import Agent
from agentlite.core.confirmation import AutoAllowHandler
from agentlite.core.models import ToolCall
from agentlite.providers.mock import MockProvider
from agentlite.tools.base import ToolContext


@pytest.fixture
def fs(config, audit):
    agent = Agent.from_config(
        config,
        provider=MockProvider(steps=[]),
        confirmation_handler=AutoAllowHandler(),
        audit=audit,
    )
    context = ToolContext(
        run_id="run_fs", config=agent.config, workspace=agent.config.workspace_root
    )

    def call(name: str, **arguments):
        return agent.executor.execute(
            ToolCall(id=f"call_{name}", name=name, arguments=arguments), context
        )

    return call


def test_write_then_read_then_list(fs, config):
    written = fs("filesystem.write", path="notes/hello.txt", content="hello world")
    assert written.ok is True, written.error
    assert written.meta["created"] is True
    assert (config.workspace_root / "notes" / "hello.txt").read_text() == "hello world"

    read = fs("filesystem.read", path="notes/hello.txt")
    assert read.ok is True
    assert read.output == "hello world"

    listing = fs("filesystem.list", path=".")
    assert listing.ok is True
    names = [entry["name"] for entry in listing.meta["entries"]]
    assert "notes/" in names


def test_relative_paths_resolve_against_workspace(fs, config):
    fs("filesystem.write", path="a/b/c.txt", content="deep")
    assert (config.workspace_root / "a" / "b" / "c.txt").exists()


def test_parent_directory_escape_is_denied(fs, config):
    result = fs("filesystem.read", path="../agentlite.yaml")
    assert result.decision == "denied"
    assert result.ok is False
    assert "outside the allowed paths" in (result.reason or "")


def test_absolute_path_outside_workspace_is_denied(fs):
    result = fs("filesystem.read", path="/etc/hostname")
    assert result.decision == "denied"


def test_write_outside_workspace_is_denied(fs, tmp_path):
    result = fs("filesystem.write", path=str(tmp_path / "escape.txt"), content="nope")
    assert result.decision == "denied"
    assert not (tmp_path / "escape.txt").exists()


def test_hidden_files_are_refused_by_default(fs, config):
    (config.workspace_root / ".env").write_text("SECRET=1", encoding="utf-8")
    denied = fs("filesystem.read", path=".env")
    assert denied.decision == "denied"
    assert "hidden" in denied.reason

    config.permissions.filesystem.allow_hidden = True
    allowed = fs("filesystem.read", path=".env")
    assert allowed.ok is True


def test_symlink_escape_is_denied(fs, config, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = config.workspace_root / "link.txt"
    link.symlink_to(secret)
    result = fs("filesystem.read", path="link.txt")
    assert result.decision == "denied"
    assert "symlink" in result.reason


def test_symlinks_inside_the_workspace_can_be_allowed(fs, config):
    """follow_symlinks=true means 'follow links inside the workspace', not 'escape it'."""
    (config.workspace_root / "real.txt").write_text("inside the workspace", encoding="utf-8")
    link = config.workspace_root / "link.txt"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(config.workspace_root / "real.txt")
    config.permissions.filesystem.follow_symlinks = True
    result = fs("filesystem.read", path="link.txt")
    assert result.ok is True, result.error
    assert result.output == "inside the workspace"


def test_following_symlinks_cannot_escape_the_workspace(fs, config, tmp_path):
    """The critical case: an allowed symlink must still resolve inside the roots."""
    secret = tmp_path / "outside.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = config.workspace_root / "escape.txt"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(secret)
    config.permissions.filesystem.follow_symlinks = True
    result = fs("filesystem.read", path="escape.txt")
    assert result.ok is False
    assert "outside the allowed paths" in result.error


def test_symlinked_directory_cannot_be_used_to_escape(fs, config, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    link = config.workspace_root / "door"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside)
    config.permissions.filesystem.follow_symlinks = True
    assert fs("filesystem.read", path="door/secret.txt").ok is False
    assert fs("filesystem.list", path="door").ok is False
    assert fs("filesystem.write", path="door/new.txt", content="x").ok is False


def test_read_only_blocks_writes(fs, config):
    config.permissions.filesystem.read_only = True
    result = fs("filesystem.write", path="blocked.txt", content="x")
    assert result.decision == "denied"
    assert "read-only" in result.reason


def test_write_size_limit(fs, config):
    config.permissions.filesystem.max_write_bytes = 100
    result = fs("filesystem.write", path="big.txt", content="x" * 500)
    assert result.ok is False
    assert "limit" in result.error


def test_read_size_is_truncated(fs, config):
    config.permissions.filesystem.max_read_bytes = 128
    fs("filesystem.write", path="big.txt", content="y" * 5000)
    result = fs("filesystem.read", path="big.txt")
    assert result.ok is True
    assert result.meta["truncated"] is True
    assert result.meta["bytes"] <= 128


def test_binary_file_is_refused(fs, config):
    (config.workspace_root / "blob.bin").write_bytes(b"\x00\x01\x02\x03" * 100)
    result = fs("filesystem.read", path="blob.bin")
    assert result.ok is False
    assert "binary" in result.error


def test_reading_a_directory_is_an_error(fs, config):
    (config.workspace_root / "dir").mkdir(exist_ok=True)
    result = fs("filesystem.read", path="dir")
    assert result.ok is False
    assert "directory" in result.error


def test_missing_file_is_reported(fs):
    result = fs("filesystem.read", path="nope.txt")
    assert result.ok is False
    assert "no such file" in result.error


def test_denied_path_pattern(fs, config):
    config.permissions.filesystem.denied_paths = ["*.pem"]
    (config.workspace_root / "key.pem").write_text("x", encoding="utf-8")
    assert fs("filesystem.read", path="key.pem").decision == "denied"


def test_append_mode(fs, config):
    fs("filesystem.write", path="log.txt", content="one\n")
    fs("filesystem.write", path="log.txt", content="two\n", append=True)
    assert (config.workspace_root / "log.txt").read_text() == "one\ntwo\n"


def test_multiple_allowed_roots(config_factory, tmp_path):
    second = tmp_path / "shared"
    second.mkdir()
    config = config_factory(
        permissions={"filesystem": {"allowed_paths": ["./workspace", str(second)]}}
    )
    agent = Agent.from_config(
        config, provider=MockProvider(steps=[]), confirmation_handler=AutoAllowHandler()
    )
    context = ToolContext(run_id="r", config=config, workspace=config.workspace_root)
    result = agent.executor.execute(
        ToolCall(
            id="c",
            name="filesystem.write",
            arguments={"path": str(second / "ok.txt"), "content": "shared"},
        ),
        context,
    )
    assert result.ok is True, result.error


def test_list_limit(fs, config):
    config.permissions.filesystem.max_list_entries = 3
    for index in range(10):
        (config.workspace_root / f"file{index}.txt").write_text("x", encoding="utf-8")
    result = fs("filesystem.list", path=".")
    assert result.ok is True
    assert result.meta["count"] == 3
    assert result.meta["truncated"] is True


def test_named_pipe_is_refused_instead_of_hanging(fs, config):
    """A FIFO in the workspace would block the read forever - refuse it."""
    import os as _os

    fifo = config.workspace_root / "pipe"
    if fifo.exists():
        fifo.unlink()
    _os.mkfifo(fifo)
    try:
        result = fs("filesystem.read", path="pipe")
        assert result.ok is False
        assert "not a regular file" in result.error
    finally:
        fifo.unlink()


def test_devices_and_sockets_are_refused(fs, config):
    result = fs("filesystem.read", path="/dev/null")
    assert result.ok is False
