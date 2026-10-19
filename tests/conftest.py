"""Shared test fixtures.

Tests run fully offline: no API keys, no network, no browser binary. The real
LLM is replaced by :class:`agentlite.providers.mock.MockProvider` (or by a fake
OpenAI-compatible HTTP server when the provider itself is under test).
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from agentlite.core.agent import Agent
from agentlite.core.audit import AuditLogger
from agentlite.core.config import Config, load_config
from agentlite.core.confirmation import AutoAllowHandler, ConfirmationHandler
from agentlite.core.registry import ToolRegistry

_counter = itertools.count()


@pytest.fixture
def config_factory(tmp_path):
    """Build a :class:`Config` rooted in a temporary directory."""

    def factory(workspace: str = "./workspace", **sections: Any) -> Config:
        directory = tmp_path / f"cfg{next(_counter)}"
        directory.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {"workspace": workspace}
        data.update(sections)
        path = directory / "agentlite.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        config = load_config(str(path), apply_env=False)
        config.ensure_directories()
        return config

    return factory


@pytest.fixture
def config(config_factory) -> Config:
    """Sensible default config for tests."""
    cfg = config_factory()
    cfg.security.confirmation_mode = "deny"  # deterministic: no interactive prompts
    cfg.security.audit_log = False
    return cfg


@pytest.fixture
def audit(tmp_path) -> AuditLogger:
    return AuditLogger(path=tmp_path / "audit.jsonl", enabled=True)


@pytest.fixture
def agent_factory(config_factory):
    """Build an agent. Pass ``config=`` to share a mutated configuration."""

    def factory(
        provider,
        config: Optional[Config] = None,
        confirmation: Optional[ConfirmationHandler] = None,
        **sections: Any,
    ) -> Agent:
        cfg = config or config_factory(**sections)
        return Agent.from_config(
            cfg,
            provider=provider,
            confirmation_handler=confirmation or AutoAllowHandler(),
            audit=AuditLogger(enabled=False),
        )

    return factory


@pytest.fixture
def workspace_dir(config) -> Path:
    path = config.workspace_root
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def registry(config) -> ToolRegistry:
    from agentlite.core.registry import build_registry

    return build_registry(config)
