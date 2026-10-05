"""HTTP API tests."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agentlite.api.server import Runtime, create_app
from agentlite.core.agent import Agent
from agentlite.core.audit import AuditLogger
from agentlite.core.confirmation import PendingConfirmationHandler
from agentlite.core.models import RunStatus
from agentlite.providers.mock import MockProvider


@pytest.fixture
def client_factory(config_factory):
    def factory(steps=None, confirmation_handler=None, token=None, audit=None, **sections):
        config = config_factory(**sections)
        if token:
            config.server.api_token = token
        provider = MockProvider(steps=steps if steps is not None else ["hello from the agent"])
        agent = Agent.from_config(
            config,
            provider=provider,
            confirmation_handler=confirmation_handler or PendingConfirmationHandler(),
            audit=audit or AuditLogger(enabled=False),
        )
        runtime = Runtime(config, agent=agent)
        return TestClient(create_app(config, runtime=runtime)), runtime

    return factory


def test_health(client_factory):
    client, _ = client_factory()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "agentlite"


def test_status(client_factory):
    client, runtime = client_factory()
    body = client.get("/api/status").json()
    assert body["status"] == "ok"
    assert body["workspace"] == str(runtime.config.workspace_root)
    assert body["provider"]["model"]
    assert "terminal.run" in body["tools"]
    assert body["security"]["confirmation_mode"]


def test_tools_endpoint(client_factory):
    client, _ = client_factory()
    tools = client.get("/api/tools").json()["tools"]
    names = [tool["name"] for tool in tools]
    assert "terminal.run" in names
    assert "filesystem.write" in names


def test_run_returns_structured_result(client_factory):
    client, _ = client_factory(
        steps=[[("filesystem.list", {"path": "."})], "The workspace is empty."]
    )
    response = client.post("/api/run", json={"task": "inspect the workspace"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"] == "The workspace is empty."
    assert len(body["actions"]) == 1
    assert body["actions"][0]["tool"] == "filesystem.list"
    assert body["actions"][0]["decision"] == "allow"
    assert body["steps"] == 2
    assert body["run_id"]


def test_run_rejects_empty_task(client_factory):
    client, _ = client_factory()
    assert client.post("/api/run", json={"task": "   "}).status_code == 400


def test_run_asks_for_confirmation_and_resumes(client_factory):
    client, _ = client_factory(
        steps=[[("terminal.run", {"command": "rm notes.txt"})], "Removed."],
    )
    response = client.post("/api/run", json={"task": "delete the notes"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "needs_confirmation"
    run_id = body["run_id"]
    assert body["pending"]["tool"] == "terminal.run"

    parked = client.get(f"/api/runs/{run_id}")
    assert parked.status_code == 200
    assert parked.json()["status"] == "needs_confirmation"

    allowed = client.post(f"/api/runs/{run_id}/confirm", json={"decision": "allow"})
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "completed"

    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert client.post(f"/api/runs/{run_id}/confirm", json={"decision": "allow"}).status_code == 404


def test_confirmation_can_be_denied(client_factory):
    client, runtime = client_factory(
        steps=[[("terminal.run", {"command": "rm notes.txt"})], "Refused."]
    )
    (runtime.config.workspace_root / "notes.txt").write_text("keep", encoding="utf-8")
    response = client.post("/api/run", json={"task": "delete the notes"})
    run_id = response.json()["run_id"]
    denied = client.post(f"/api/runs/{run_id}/confirm", json={"decision": "deny"})
    assert denied.status_code == 200
    assert denied.json()["actions"][0]["decision"] == "denied"
    assert (runtime.config.workspace_root / "notes.txt").exists()


def test_confirm_decision_is_validated(client_factory):
    client, _ = client_factory(steps=[[("terminal.run", {"command": "rm x"})], "ok"])
    run_id = client.post("/api/run", json={"task": "x"}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/confirm", json={"decision": "maybe"}).status_code == 422


def test_api_token_is_required_when_configured(client_factory):
    client, _ = client_factory(token="secret-token-abc")
    assert client.get("/api/status").status_code == 401
    assert client.get("/health").status_code == 200
    ok = client.get("/api/status", headers={"Authorization": "Bearer secret-token-abc"})
    assert ok.status_code == 200
    with_key = client.get("/api/status", headers={"X-API-Key": "secret-token-abc"})
    assert with_key.status_code == 200
    wrong = client.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


def test_no_token_on_loopback_is_open(client_factory):
    client, _ = client_factory()
    assert client.get("/api/status").status_code == 200


def test_audit_endpoint(client_factory, tmp_path):
    client, runtime = client_factory(
        steps=[[("filesystem.list", {"path": "."})], "done"],
        audit=AuditLogger(path=tmp_path / "audit.jsonl", enabled=True),
    )
    client.post("/api/run", json={"task": "list"})
    events = client.get("/api/audit?limit=10").json()["events"]
    assert any(event["event"] == "tool_call" for event in events)
    assert any(event["event"] == "run_finished" for event in events)

    # The audit log is a JSONL file on disk, one record per line.
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert lines and all(json.loads(line)["ts"] for line in lines)


def test_denied_action_is_visible_in_the_api(client_factory):
    client, _ = client_factory(
        steps=[[("terminal.run", {"command": "sudo rm -rf /etc"})], "Refused."]
    )
    body = client.post("/api/run", json={"task": "clean the system"}).json()
    assert body["status"] == "completed"
    assert body["actions"][0]["decision"] == "denied"


def test_max_steps_override(client_factory):
    client, _ = client_factory(steps=[[("filesystem.list", {"path": "."})]] * 20)
    body = client.post("/api/run", json={"task": "loop", "max_steps": 2}).json()
    assert body["status"] == "max_steps_reached"
    assert body["steps"] == 2


def test_openapi_docs_available(client_factory):
    client, _ = client_factory()
    assert client.get("/docs").status_code == 200
    assert "/api/run" in client.get("/openapi.json").text


def test_run_status_values_are_strings(client_factory):
    client, _ = client_factory(steps=["hi"])
    body = client.post("/api/run", json={"task": "hi"}).json()
    assert isinstance(body["status"], str)
    assert RunStatus(body["status"]) is RunStatus.COMPLETED
