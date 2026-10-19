"""CLI tests."""

from __future__ import annotations

import json

import pytest
import yaml

from agentlite.cli.main import main


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workspace": "./workspace",
                "provider": {"name": "mock"},
                "security": {"confirmation_mode": "allow", "audit_log": False},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_version(capsys):
    assert main(["version"]) == 0
    assert "agentlite" in capsys.readouterr().out


def test_tools_lists_the_mvp_tools(capsys, config_file):
    assert main(["tools", "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    assert "terminal.run" in out
    assert "filesystem.write" in out
    assert "browser.open" in out


def test_tools_json(capsys, config_file):
    assert main(["tools", "--config", str(config_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"name", "enabled", "risk"} <= set(payload[0])


def test_config_show(capsys, config_file):
    assert main(["config", "--config", str(config_file)]) == 0
    assert "workspace:" in capsys.readouterr().out


def test_config_path(capsys, config_file):
    assert main(["config", "--path", "--config", str(config_file)]) == 0
    assert str(config_file) in capsys.readouterr().out


def test_config_check_valid(capsys, config_file):
    assert main(["config", "--check", "--config", str(config_file)]) == 0
    assert "valid" in capsys.readouterr().out


def test_config_check_reports_problems(capsys, tmp_path):
    bad = tmp_path / "agentlite.yaml"
    bad.write_text(yaml.safe_dump({"server": {"host": "0.0.0.0"}}), encoding="utf-8")
    assert main(["config", "--check", "--config", str(bad)]) == 1
    assert "api_token" in capsys.readouterr().out


def test_config_init(tmp_path, capsys):
    target = tmp_path / "agentlite.yaml"
    assert main(["config", "--init", str(target)]) == 0
    assert target.is_file()
    assert "workspace" in target.read_text()
    # refuses to overwrite without --force
    assert main(["config", "--init", str(target)]) == 1
    assert main(["config", "--init", str(target), "--force"]) == 0


def test_doctor_reports_status(capsys, config_file, monkeypatch):
    monkeypatch.chdir(config_file.parent)
    main(["doctor", "--config", str(config_file)])
    out = capsys.readouterr().out
    assert "python" in out
    assert "workspace" in out
    assert "tool terminal.run" in out


def test_doctor_flags_missing_api_key(capsys, tmp_path, monkeypatch):
    config = tmp_path / "agentlite.yaml"
    config.write_text(yaml.safe_dump({"workspace": "./ws"}), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    code = main(["doctor", "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert "OPENAI_API_KEY" in out


def test_run_executes_a_task(capsys, config_file, monkeypatch):
    monkeypatch.chdir(config_file.parent)
    code = main(
        [
            "run",
            "--config",
            str(config_file),
            "--provider",
            "mock",
            "--yes",
            "inspect the workspace",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "report.md" in out or "Workspace report" in out or "filesystem" in out


def test_run_json_output(capsys, config_file, monkeypatch):
    monkeypatch.chdir(config_file.parent)
    code = main(["run", "--config", str(config_file), "--yes", "--json", "list the workspace"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["actions"]


def test_run_reports_denied_actions(capsys, tmp_path, monkeypatch):
    """A read-only workspace makes the mock script's write fail the permission check."""
    config = tmp_path / "agentlite.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "workspace": "./ws",
                "provider": {"name": "mock"},
                "security": {"audit_log": False, "confirmation_mode": "deny"},
                "permissions": {"filesystem": {"read_only": True}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    code = main(["run", "--config", str(config), "write a report"])
    out = capsys.readouterr().out
    assert code == 0
    assert "denied" in out
    assert not (tmp_path / "ws" / "report.md").exists()


def test_missing_config_file_exits(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["tools", "--config", str(tmp_path / "nope.yaml")])


# --------------------------------------------------------------------------- #
# Paths a human actually takes
# --------------------------------------------------------------------------- #


def test_start_refuses_a_public_bind_without_a_token(capsys, config_file, monkeypatch):
    monkeypatch.delenv("AGENTLITE_API_TOKEN", raising=False)
    assert main(["start", "--config", str(config_file), "--host", "0.0.0.0", "--port", "8765"]) == 2
    out = capsys.readouterr().out
    assert "api_token" in out
    assert "Refusing to start" in out


def test_start_accepts_a_public_bind_with_a_token(capsys, config_file, monkeypatch):
    """With a token the refusal goes away - but do not actually start uvicorn."""
    monkeypatch.setenv("AGENTLITE_API_TOKEN", "test-token")
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    assert main(["start", "--config", str(config_file), "--host", "0.0.0.0"]) == 0
    out = capsys.readouterr().out
    assert "token required" in out


def test_a_broken_config_file_names_itself(capsys, tmp_path):
    broken = tmp_path / "agentlite.yaml"
    broken.write_text("server:\n  port: [unclosed\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["doctor", "--config", str(broken)])
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "invalid YAML" in out
    assert str(broken) in out


def test_a_missing_config_file_suggests_how_to_create_one(capsys, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        main(["doctor", "--config", str(tmp_path / "nope.yaml")])
    assert excinfo.value.code == 2
    assert "config --init" in capsys.readouterr().out


def test_config_init_writes_a_usable_file(capsys, tmp_path):
    target = tmp_path / "fresh.yaml"
    assert main(["config", "--init", str(target)]) == 0
    assert target.exists()
    assert "workspace" in target.read_text(encoding="utf-8")
    # ...and the file it wrote validates.
    assert main(["config", "--check", "--config", str(target)]) == 0


@pytest.fixture
def browser_available(monkeypatch):
    """Doctor's exit code should say something about *your* config, not about
    whether Playwright happens to be installed on this machine."""
    monkeypatch.setattr("agentlite.tools.browser.playwright_installed", lambda: True)
    monkeypatch.setattr("agentlite.tools.browser.browser_binary_installed", lambda: True)


def test_doctor_reports_missing_key_and_dangerous_settings(capsys, tmp_path, browser_available):
    path = tmp_path / "agentlite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workspace": "./workspace",
                "provider": {"name": "openai", "model": "gpt-4o-mini"},
                "security": {"confirmation_mode": "allow"},
                "permissions": {"terminal": {"allow_shell": True}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch_key = pytest.MonkeyPatch()
    monkeypatch_key.delenv("OPENAI_API_KEY", raising=False)
    try:
        assert main(["doctor", "--config", str(path)]) == 1
        out = capsys.readouterr().out
        assert "OPENAI_API_KEY" in out  # the key is missing
        assert "allow_shell" in out  # and the risky settings are named
        assert "confirmation_mode=allow" in out
    finally:
        monkeypatch_key.undo()


def test_doctor_accepts_a_local_provider_without_a_key(capsys, tmp_path, browser_available):
    """Ollama needs no API key, so a missing one is not a problem."""
    path = tmp_path / "agentlite.yaml"
    path.write_text(
        yaml.safe_dump({"workspace": "./workspace", "provider": {"name": "ollama"}}),
        encoding="utf-8",
    )
    monkeypatch_key = pytest.MonkeyPatch()
    monkeypatch_key.delenv("OLLAMA_API_KEY", raising=False)
    try:
        assert main(["doctor", "--config", str(path)]) == 0
        out = capsys.readouterr().out
        assert "ollama" in out
        assert "optional for ollama" in out
    finally:
        monkeypatch_key.undo()


def test_run_refuses_to_start_without_credentials(capsys, tmp_path):
    path = tmp_path / "agentlite.yaml"
    path.write_text(
        yaml.safe_dump({"workspace": "./workspace", "provider": {"name": "openai"}}),
        encoding="utf-8",
    )
    monkeypatch_key = pytest.MonkeyPatch()
    monkeypatch_key.delenv("OPENAI_API_KEY", raising=False)
    try:
        with pytest.raises(SystemExit) as excinfo:
            main(["run", "--config", str(path), "hello"])
        assert excinfo.value.code == 2
        out = capsys.readouterr().out
        assert "export OPENAI_API_KEY" in out
        assert "fake_llm_server" in out  # points at the keyless demo
    finally:
        monkeypatch_key.undo()


def test_run_without_a_terminal_refuses_confirmations(capsys, config_file, monkeypatch):
    """A pipe or cron job must not wait for an answer that cannot come."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "agentlite.core.confirmation.CliConfirmationHandler.confirm", lambda *a, **k: True
    )
    config_file.write_text(
        yaml.safe_dump(
            {
                "workspace": "./workspace",
                "provider": {"name": "mock"},
                "security": {"confirmation_mode": "prompt", "audit_log": False},
            }
        ),
        encoding="utf-8",
    )
    assert main(["run", "--config", str(config_file), "clean up"]) == 0
    out = capsys.readouterr().out
    assert "not a terminal" in out


def test_run_yes_auto_approves(capsys, config_file, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["run", "--config", str(config_file), "clean up", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "not a terminal" not in out  # --yes means nobody is asked
