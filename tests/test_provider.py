"""Provider abstraction tests.

The OpenAI-compatible provider is exercised both against an in-process fake
transport (error handling, retries, parsing) and against a real HTTP server
speaking the OpenAI wire format over a socket.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from fake_llm_server import start_fake_server  # noqa: E402

from agentlite.core.config import ProviderConfig  # noqa: E402
from agentlite.core.models import ToolSpec  # noqa: E402
from agentlite.providers import build_provider  # noqa: E402
from agentlite.providers.base import (  # noqa: E402
    Message,
    ProviderError,
    from_wire_name,
    to_wire_name,
)
from agentlite.providers.mock import MockProvider  # noqa: E402
from agentlite.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
    resolve_api_key,
)

TOOLS = [
    ToolSpec(
        name="terminal.run",
        description="run a command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )
]


def make_provider(handler, api_key="sk-test-key-123", **overrides) -> OpenAICompatibleProvider:
    kwargs = {
        "name": "openai",
        "model": "gpt-4o-mini",
        "base_url": "https://api.example.com/v1",
        "api_key": api_key,
        "max_retries": 0,
    }
    kwargs.update(overrides)
    config = ProviderConfig(**kwargs)
    return OpenAICompatibleProvider(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


# --------------------------------------------------------------------------- #
# Name mapping
# --------------------------------------------------------------------------- #


def test_wire_names_replace_dots():
    assert to_wire_name("terminal.run") == "terminal_run"
    assert from_wire_name("terminal_run", ["terminal.run"]) == "terminal.run"
    assert from_wire_name("terminal.run", ["terminal.run"]) == "terminal.run"
    assert from_wire_name("filesystemwrite", ["filesystem.write"]) == "filesystem.write"


# --------------------------------------------------------------------------- #
# Request / response encoding
# --------------------------------------------------------------------------- #


def test_request_payload_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    provider = make_provider(handler)
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="do it"),
    ]
    response = provider.complete(messages, TOOLS)
    assert response.content == "hi"
    assert seen["model"] == "gpt-4o-mini"
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["tools"][0]["function"]["name"] == "terminal_run"
    assert seen["tools"][0]["function"]["parameters"]["required"] == ["command"]


def test_tool_call_round_trip():
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "terminal_run", "arguments": '{"command": "ls"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    provider = make_provider(lambda request: httpx.Response(200, json=payload))
    response = provider.complete([Message(role="user", content="go")], TOOLS)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "terminal.run"
    assert call.arguments() == {"command": "ls"}
    assert response.usage.total_tokens == 12
    assert response.finish_reason == "tool_calls"


def test_tool_results_are_encoded_with_ids():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = make_provider(handler)
    messages = [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            tool_calls=[
                type("C", (), {"id": "call_9", "name": "terminal.run", "arguments_json": "{}"})()
            ],
        ),
        Message(role="tool", tool_call_id="call_9", content="{'ok': True}"),
    ]
    provider.complete(messages, TOOLS)
    encoded = seen["messages"]
    assert encoded[1]["tool_calls"][0]["function"]["name"] == "terminal_run"
    assert encoded[2] == {"role": "tool", "tool_call_id": "call_9", "content": "{'ok': True}"}


def test_malformed_arguments_are_flagged_not_crashing():
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "terminal_run", "arguments": "{not json"},
                        }
                    ]
                }
            }
        ]
    }
    provider = make_provider(lambda request: httpx.Response(200, json=payload))
    response = provider.complete([Message(role="user", content="go")], TOOLS)
    assert "_invalid_arguments" in response.tool_calls[0].arguments()


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_http_errors_are_explained():
    provider = make_provider(lambda request: httpx.Response(401, json={"error": "nope"}))
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([Message(role="user", content="x")], TOOLS)
    assert "401" in str(excinfo.value)
    assert "API key" in str(excinfo.value)


def test_malformed_response_is_reported():
    provider = make_provider(lambda request: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([Message(role="user", content="x")], TOOLS)
    assert "malformed" in str(excinfo.value)


def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("agentlite.providers.openai_compatible.time.sleep", lambda seconds: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "finally"}}]})

    provider = make_provider(handler, max_retries=2)
    response = provider.complete([Message(role="user", content="x")], TOOLS)
    assert response.content == "finally"
    assert attempts["count"] == 3


def test_transport_errors_raise_provider_error(monkeypatch):
    monkeypatch.setattr("agentlite.providers.openai_compatible.time.sleep", lambda seconds: None)
    provider = make_provider(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom")), max_retries=1
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([Message(role="user", content="x")], TOOLS)
    assert "transport error" in str(excinfo.value)


def test_timeout_is_reported(monkeypatch):
    monkeypatch.setattr("agentlite.providers.openai_compatible.time.sleep", lambda seconds: None)
    provider = make_provider(
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("too slow")), max_retries=0
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([Message(role="user", content="x")], TOOLS)
    assert "timed out" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_api_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("MY_MODEL_KEY", "secret-from-env")
    config = ProviderConfig(api_key_env="MY_MODEL_KEY")
    assert resolve_api_key(config) == "secret-from-env"
    monkeypatch.delenv("MY_MODEL_KEY")
    monkeypatch.setenv("AGENTLITE_API_KEY", "fallback-key")
    assert resolve_api_key(config) == "fallback-key"


def test_authorization_header_is_omitted_without_a_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = make_provider(handler, api_key=None)
    provider.complete([Message(role="user", content="x")], TOOLS)
    assert seen["auth"] is None


def test_describe_never_leaks_the_key():
    provider = make_provider(
        lambda request: httpx.Response(200, json={}), api_key="sk-secret-123456"
    )
    description = provider.describe()
    assert "sk-secret" not in json.dumps(description)
    assert description["api_key_present"] is True


def test_missing_base_url_is_an_error():
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(ProviderConfig(name="unknown-provider", base_url=None))


def test_explicit_base_url_beats_the_preset():
    config = ProviderConfig(name="openai", base_url="http://localhost:11434/v1")
    assert OpenAICompatibleProvider(config).base_url == "http://localhost:11434/v1"


def test_factory_picks_the_openai_compatible_provider(config):
    provider = build_provider(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://api.openai.com/v1"


def test_factory_picks_the_mock_provider(config):
    config.provider.name = "mock"
    assert isinstance(build_provider(config), MockProvider)


def test_presets_resolve_urls(config):
    config.provider.name = "openrouter"
    provider = build_provider(config)
    assert provider.base_url == "https://openrouter.ai/api/v1"
    config.provider.name = "ollama"
    assert build_provider(config).base_url == "http://localhost:11434/v1"
    config.provider.name = "gemini"
    assert "generativelanguage.googleapis.com" in build_provider(config).base_url


# --------------------------------------------------------------------------- #
# Real HTTP round trip
# --------------------------------------------------------------------------- #


def test_provider_talks_to_a_real_http_server():
    base_url, shutdown = start_fake_server()
    try:
        config = ProviderConfig(
            name="openai-compatible", base_url=base_url, model="fake-model", api_key="test-key"
        )
        provider = OpenAICompatibleProvider(config)
        response = provider.complete([Message(role="user", content="inspect the workspace")], TOOLS)
        assert response.content == "" or response.tool_calls
        assert [call.name for call in response.tool_calls] == ["filesystem.list"]
        assert response.tool_calls[0].arguments() == {"path": "."}
    finally:
        shutdown()


def test_provider_sends_tool_results_over_http():
    base_url, shutdown = start_fake_server(scenario="demo")
    try:
        provider = OpenAICompatibleProvider(
            ProviderConfig(base_url=base_url, model="fake", api_key="k")
        )
        messages = [
            Message(role="user", content="task"),
            Message(
                role="assistant",
                tool_calls=[
                    type(
                        "C",
                        (),
                        {"id": "call_1", "name": "filesystem.list", "arguments_json": "{}"},
                    )()
                ],
            ),
            Message(role="tool", tool_call_id="call_1", content='{"ok": true}'),
        ]
        response = provider.complete(messages, TOOLS)
        # Second step of the script: run `ls -la`.
        assert [call.name for call in response.tool_calls] == ["terminal.run"]
    finally:
        shutdown()
