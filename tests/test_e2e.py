"""End-to-end tests.

These start the real pieces:

    uvicorn (AgentLite API)  <-->  AgentLite runtime  <-->  real HTTP LLM API
                                          |
                                   real tools on a real filesystem

The only stand-in is the *model*: a small OpenAI-compatible HTTP server that
replays a scripted conversation (``examples/fake_llm_server.py``). The HTTP
transport, JSON encoding, tool-call parsing, permission checks, tool execution
and result flow are all the real code paths.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from fake_llm_server import start_fake_server  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UvicornServer(uvicorn.Server):
    def install_signal_handlers(self):  # pragma: no cover - thread safety
        pass


@pytest.fixture(scope="module")
def llm_server():
    base_url, shutdown = start_fake_server(port=0, scenario="demo")
    yield base_url
    shutdown()


@pytest.fixture(scope="module")
def llm_server_dangerous():
    base_url, shutdown = start_fake_server(port=0, scenario="dangerous")
    yield base_url
    shutdown()


@pytest.fixture
def running_server(tmp_path, llm_server):
    """Start the AgentLite API on a real port, pointed at the fake model."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello from the workspace", encoding="utf-8")

    config_file = tmp_path / "agentlite.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "workspace": str(workspace),
                "provider": {
                    "name": "openai-compatible",
                    "base_url": llm_server,
                    "api_key_env": "AGENTLITE_E2E_KEY",
                    "model": "fake-model",
                },
                "security": {"audit_log": True, "log_dir": str(tmp_path / "logs")},
                "logging": {"dir": str(tmp_path / "logs")},
            }
        ),
        encoding="utf-8",
    )

    from agentlite.api.server import create_app
    from agentlite.core.config import load_config

    os.environ.setdefault("AGENTLITE_E2E_KEY", "e2e-key")
    config = load_config(str(config_file), apply_env=False)
    app = create_app(config)
    port = free_port()
    server = UvicornServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the API server did not start"

    base = f"http://127.0.0.1:{port}"
    try:
        yield base, config
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_health_of_a_live_server(running_server):
    base, _config = running_server
    with httpx.Client(base_url=base, timeout=10) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_full_workflow_over_http(running_server):
    """task -> LLM -> tool -> permission -> tool result -> LLM -> answer."""
    base, config = running_server
    with httpx.Client(base_url=base, timeout=60) as client:
        response = client.post(
            "/api/run", json={"task": "Inspect the workspace and write a report."}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed", body
    assert body["provider"] == "openai-compatible"
    assert body["model"] == "fake-model"

    tools = [action["tool"] for action in body["actions"]]
    assert tools == ["filesystem.list", "terminal.run", "filesystem.write"]
    assert all(action["decision"] == "allow" for action in body["actions"])
    assert all(action["ok"] for action in body["actions"])

    # The run happened on a real filesystem, through a real process.
    report = config.workspace_root / "report.md"
    assert report.is_file()
    assert "hello.txt" in report.read_text(encoding="utf-8")
    assert "Workspace report" in report.read_text(encoding="utf-8")

    # ...and it was logged.
    audit_lines = list((config.log_path.parent).glob("*.jsonl"))
    assert audit_lines, "the audit log should exist"
    logged = audit_lines[0].read_text(encoding="utf-8")
    assert "tool_call" in logged
    assert "filesystem.write" in logged


def test_status_endpoint_describes_the_live_server(running_server):
    base, config = running_server
    with httpx.Client(base_url=base, timeout=10) as client:
        body = client.get("/api/status").json()
    assert body["workspace"] == str(config.workspace_root)
    assert body["provider"]["base_url"].startswith("http://127.0.0.1")
    assert body["tools"]["terminal.run"]["enabled"] is True


def test_dangerous_task_is_refused_end_to_end(tmp_path, llm_server_dangerous):
    """The model asks for `sudo rm -rf /etc`; the permission system says no."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_file = tmp_path / "agentlite.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "workspace": str(workspace),
                "provider": {
                    "name": "openai-compatible",
                    "base_url": llm_server_dangerous,
                    "api_key_env": "AGENTLITE_E2E_KEY",
                    "model": "fake-model",
                },
                "security": {"audit_log": False},
            }
        ),
        encoding="utf-8",
    )
    from agentlite.api.server import create_app
    from agentlite.core.config import load_config

    os.environ.setdefault("AGENTLITE_E2E_KEY", "e2e-key")
    config = load_config(str(config_file), apply_env=False)
    app = create_app(config)
    port = free_port()
    server = UvicornServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            body = client.post("/api/run", json={"task": "Clean up the system"}).json()
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert body["status"] == "completed"
    assert body["actions"][0]["tool"] == "terminal.run"
    assert body["actions"][0]["decision"] == "denied"
    assert "deny rule" in body["actions"][0]["reason"]
    assert "refused" in body["result"].lower()


def test_cli_run_against_a_live_model(tmp_path, llm_server, monkeypatch, capsys):
    """`agentlite run` drives the same loop from the command line."""
    from agentlite.cli.main import main

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("keep me", encoding="utf-8")
    config_file = tmp_path / "agentlite.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "workspace": str(workspace),
                "provider": {
                    "name": "openai-compatible",
                    "base_url": llm_server,
                    "api_key_env": "AGENTLITE_E2E_KEY",
                    "model": "fake-model",
                },
                "security": {"audit_log": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTLITE_E2E_KEY", "e2e-key")
    code = main(["run", "--config", str(config_file), "inspect the workspace"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert (workspace / "report.md").is_file()
    assert "filesystem.list" in out
    assert "terminal.run" in out
