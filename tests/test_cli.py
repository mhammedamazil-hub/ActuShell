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
