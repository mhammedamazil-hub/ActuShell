"""OpenAI-compatible provider.

Works with anything that implements ``POST {base_url}/chat/completions`` with
function/tool calling:

    OpenAI        https://api.openai.com/v1
    OpenRouter    https://openrouter.ai/api/v1
    Gemini        https://generativelanguage.googleapis.com/v1beta/openai
    Ollama        http://localhost:11434/v1
    LM Studio     http://localhost:1234/v1
    vLLM / Groq / Together / any OpenAI-compatible gateway

Preset names resolve the base URL and the environment variable holding the API
key. Keys are only ever read from the environment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from ..core.config import PROVIDER_PRESETS, ProviderConfig
from ..core.models import TokenUsage, ToolSpec
from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ToolCallRequest,
    from_wire_name,
    to_wire_name,
)

logger = logging.getLogger("agentlite.provider")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(LLMProvider):
    """Provider for the OpenAI chat-completions wire format."""

    name = "openai-compatible"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: Optional[httpx.Client] = None,
        api_key: Optional[str] = None,
    ):
        self.config = config
        self.model = config.model
        self.base_url = (config.effective_base_url or "").rstrip("/")
        if not self.base_url:
            raise ProviderError(
                "provider.base_url is empty; set it in the config or AGENTLITE_BASE_URL"
            )
        self.api_key = api_key if api_key is not None else resolve_api_key(config)
        self._client = client
        self._owns_client = client is None

    # -- public API -------------------------------------------------------- #

    def complete(
        self,
        messages: List[Message],
        tools: List[ToolSpec],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [self._encode_message(m) for m in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if max_tokens or self.config.max_tokens:
            payload["max_tokens"] = max_tokens or self.config.max_tokens
        if tools:
            payload["tools"] = [spec.to_provider_tool(to_wire_name(spec.name)) for spec in tools]
        if self.config.extra_body:
            payload.update(self.config.extra_body)

        data = self._post("/chat/completions", payload)
        return self._decode(data, tools)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "preset": self.config.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.config.api_key_env,
            "api_key_present": bool(self.api_key),
        }

    def close(self) -> None:
        """Close the HTTP client (called by the API on shutdown)."""
        if self._client is not None and self._owns_client:
            try:
                self._client.close()
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                logger.debug("error closing provider client: %s", exc)
            self._client = None

    # -- transport --------------------------------------------------------- #

    def _client_or_create(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.config.timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                follow_redirects=False,
            )
        return self._client

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.config.extra_headers or {})
        return headers

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        client = self._client_or_create()
        last_error = "unknown error"
        for attempt in range(self.config.max_retries + 1):
            try:
                response = client.post(url, json=payload, headers=self._headers())
            except httpx.TimeoutException as exc:
                last_error = f"request timed out after {self.config.timeout}s"
                logger.warning("provider timeout (attempt %s): %s", attempt + 1, exc)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                logger.warning("provider transport error (attempt %s): %s", attempt + 1, exc)
                if attempt >= self.config.max_retries:
                    break
            else:
                if response.status_code in RETRY_STATUS and attempt < self.config.max_retries:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    time.sleep(min(2**attempt, 8))
                    continue
                if response.status_code >= 400:
                    raise ProviderError(_explain_http_error(response))
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(f"provider returned invalid JSON: {exc}") from exc
            time.sleep(min(2**attempt, 8))
        raise ProviderError(f"provider request failed after retries: {last_error}")

    # -- encoding / decoding ----------------------------------------------- #

    @staticmethod
    def _encode_message(message: Message) -> Dict[str, Any]:
        encoded: Dict[str, Any] = {"role": message.role}
        if message.content is not None:
            encoded["content"] = message.content
        elif message.role == "assistant" and not message.tool_calls:
            encoded["content"] = ""
        if message.tool_calls:
            encoded["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": to_wire_name(call.name),
                        "arguments": call.arguments_json,
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            encoded["tool_call_id"] = message.tool_call_id
        if message.name:
            encoded["name"] = message.name
        return encoded

    def _decode(self, data: Dict[str, Any], tools: List[ToolSpec]) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"malformed provider response: {str(data)[:400]}") from exc

        message = choice.get("message") or {}
        known = [spec.name for spec in tools]
        calls = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            calls.append(
                ToolCallRequest(
                    id=raw_call.get("id") or f"call_{len(calls)}",
                    name=from_wire_name(function.get("name") or "", known),
                    arguments_json=function.get("arguments") or "{}",
                )
            )

        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
            requests=1,
        )
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "",
            usage=usage,
            raw=data,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def resolve_api_key(config: ProviderConfig) -> Optional[str]:
    """Find the API key: explicit override -> configured env var -> fallback var."""
    if config.api_key:
        return config.api_key
    for name in (config.api_key_env, "AGENTLITE_API_KEY"):
        if name and os.environ.get(name):
            return os.environ[name]
    return None


def preset_for(name: str) -> Dict[str, str]:
    return PROVIDER_PRESETS.get((name or "").lower(), {})


def _explain_http_error(response: httpx.Response) -> str:
    status = response.status_code
    body = response.text[:300]
    hints = {
        401: "the API key was rejected (check the environment variable)",
        403: "the key is not allowed to use this model",
        404: "unknown endpoint or model (check provider.base_url and provider.model)",
        429: "rate limited or out of quota",
    }
    hint = hints.get(status, "")
    return f"provider returned HTTP {status}{(': ' + hint) if hint else ''}: {body}"


def encode_tool_payload(spec: ToolSpec) -> Dict[str, Any]:  # pragma: no cover - convenience
    return json.loads(json.dumps(spec.to_provider_tool(to_wire_name(spec.name))))
