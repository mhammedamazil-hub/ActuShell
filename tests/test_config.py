"""Configuration loading, layering and environment overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agentlite.core.config import (
    PROVIDER_PRESETS,
    Config,
    Permissions,
    collect_secret_values,
    default_config_path,
    find_config_file,
    load_config,
    validate_config,
)


def test_packaged_defaults_load(config_factory):
    config = config_factory()
    assert config.permissions.terminal.enabled is True
    assert config.permissions.terminal.require_confirmation is True
    assert config.server.host == "127.0.0.1"
    assert config.security.confirmation_mode == "prompt"
    assert config.permissions.filesystem.allowed_paths == []
    assert config.allowed_roots == [config.workspace_root]


def test_default_yaml_matches_the_dataclass_defaults():
    from dataclasses import asdict

    data = yaml.safe_load(default_config_path().read_text())
    defaults = asdict(Config())
    defaults.pop("base_dir")
    defaults.pop("config_path")

    def compare(actual, expected, path=""):
        for key, value in actual.items():
            assert key in expected, f"{path}{key} is not a known setting"
            if isinstance(value, dict):
                compare(value, expected[key], f"{path}{key}.")
            else:
                assert value == expected[key], f"{path}{key}"

    compare(data, defaults)


def test_file_overrides_defaults(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workspace": "./data",
                "server": {"port": 9999},
                "permissions": {"terminal": {"enabled": False, "timeout": 5}},
            }
        )
    )
    config = load_config(str(path), apply_env=False)
    assert config.server.port == 9999
    assert config.permissions.terminal.enabled is False
    assert config.permissions.terminal.timeout == 5
    # untouched defaults survive
    assert config.permissions.filesystem.enabled is True
    assert config.workspace_root == (tmp_path / "data").resolve()
    assert config.config_path == path


def test_paths_resolve_against_the_config_directory(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workspace": "./ws",
                "permissions": {"filesystem": {"allowed_paths": ["./ws", "./other"]}},
            }
        )
    )
    config = load_config(str(path), apply_env=False)
    assert config.workspace_root == tmp_path / "ws"
    assert config.allowed_roots == [tmp_path / "ws", tmp_path / "other"]
    assert config.terminal_cwd == tmp_path / "ws"


def test_env_overrides_win(monkeypatch, tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(yaml.safe_dump({"server": {"port": 1111}, "provider": {"model": "a-model"}}))
    monkeypatch.setenv("AGENTLITE__SERVER__PORT", "2222")
    monkeypatch.setenv("AGENTLITE__PROVIDER__MODEL", "env-model")
    monkeypatch.setenv("AGENTLITE__PERMISSIONS__TERMINAL__TIMEOUT", "7")
    monkeypatch.setenv("AGENTLITE_API_TOKEN", "token-from-env")
    monkeypatch.setenv("AGENTLITE_CONFIRM", "deny")

    config = load_config(str(path))
    assert config.server.port == 2222
    assert config.provider.model == "env-model"
    assert config.permissions.terminal.timeout == 7
    assert config.server.api_token == "token-from-env"
    assert config.security.confirmation_mode == "deny"


def test_env_shortcuts(monkeypatch, tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(yaml.safe_dump({"workspace": "./ws"}))
    monkeypatch.setenv("AGENTLITE_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTLITE_MODEL", "llama3.2")
    monkeypatch.setenv("AGENTLITE_BASE_URL", "http://localhost:11434/v1")
    config = load_config(str(path))
    assert config.provider.name == "ollama"
    assert config.provider.model == "llama3.2"
    assert config.provider.effective_base_url == "http://localhost:11434/v1"


def test_missing_config_file_raises():
    with pytest.raises(FileNotFoundError):
        find_config_file("/definitely/not/here.yaml")


def test_no_config_file_uses_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentlite.core.config.USER_CONFIG_PATH", tmp_path / "nope.yaml")
    config = load_config(None, apply_env=False)
    assert config.config_path is None
    assert config.base_dir == tmp_path


def test_terminal_cwd_defaults_to_the_workspace(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(yaml.safe_dump({"workspace": "./ws"}))
    config = load_config(str(path), apply_env=False)
    assert config.terminal_cwd == tmp_path / "ws"

    path.write_text(
        yaml.safe_dump({"workspace": "./ws", "permissions": {"terminal": {"cwd": "./elsewhere"}}})
    )
    config = load_config(str(path), apply_env=False)
    assert config.terminal_cwd == tmp_path / "elsewhere"


def test_validation_flags_remote_without_token(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(yaml.safe_dump({"server": {"host": "0.0.0.0"}}))
    problems = validate_config(load_config(str(path), apply_env=False))
    assert any("api_token" in problem for problem in problems)


def test_remote_with_token_is_allowed(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(yaml.safe_dump({"server": {"host": "0.0.0.0"}}))
    config = load_config(str(path), apply_env=False)
    config.server.api_token = "secret"
    assert validate_config(config) == []


def test_validation_rejects_bad_confirmation_mode(config_factory):
    config = config_factory()
    config.security.confirmation_mode = "sometimes"
    assert any("confirmation_mode" in problem for problem in validate_config(config))


def test_validation_rejects_timeout_above_the_cap(config_factory):
    config = config_factory()
    config.permissions.terminal.timeout = 99999
    assert any("max_timeout" in problem for problem in validate_config(config))


def test_to_dict_redacts_and_shows_resolved_paths(config_factory):
    config = config_factory()
    config.server.api_token = "supersecret"
    payload = config.to_dict()
    assert payload["server"]["api_token"] == "***redacted***"
    assert payload["resolved"]["workspace"] == str(config.workspace_root)
    assert "base_dir" not in payload


def test_presets_are_complete():
    for name, preset in PROVIDER_PRESETS.items():
        assert preset["base_url"].startswith("http"), name
        assert preset["api_key_env"].endswith("_API_KEY"), name


def test_secret_values_are_collected_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENTLITE_TEST_KEY", "value-that-is-a-secret")
    monkeypatch.setenv("AGENTLITE_NOT_SECRET", "hello")
    values = collect_secret_values()
    assert "value-that-is-a-secret" in values
    assert "hello" not in values


def test_permissions_dataclass_defaults():
    permissions = Permissions()
    assert permissions.terminal.enabled is True
    assert permissions.filesystem.allowed_paths == []
    assert permissions.browser.headless is True


def test_coercion_of_string_env_values(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(yaml.safe_dump({"workspace": "./ws"}))
    monkeypatch_env = {
        "AGENTLITE__PERMISSIONS__TERMINAL__ENABLED": "false",
        "AGENTLITE__SECURITY__MAX_STEPS": "3",
        "AGENTLITE__PERMISSIONS__TERMINAL__TIMEOUT": "12",
    }
    for key, value in monkeypatch_env.items():
        os.environ[key] = value
    try:
        config = load_config(str(path))
        assert config.permissions.terminal.enabled is False
        assert config.security.max_steps == 3
        assert config.permissions.terminal.timeout == 12
    finally:
        for key in monkeypatch_env:
            os.environ.pop(key, None)


def test_example_config_in_the_repo_is_valid():
    example = Path(__file__).resolve().parents[1] / "agentlite.yaml.example"
    if example.is_file():
        config = load_config(str(example), apply_env=False)
        assert validate_config(config) == []
