"""Provider factory.

``build_provider`` is the only place that knows which provider class backs
which configuration name.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.config import Config, ProviderConfig
from .base import LLMProvider

__all__ = ["build_provider", "LLMProvider", "OpenAICompatibleProvider", "MockProvider"]


def build_provider(
    config: Config,
    *,
    provider_config: Optional[ProviderConfig] = None,
    client: Any = None,
) -> LLMProvider:
    """Instantiate the provider described by the configuration.

    ``provider.name: mock`` -> the scripted test provider.
    Anything else         -> the OpenAI-compatible provider, with the preset
                             (openai / openrouter / gemini / ollama / ...) used
                             to resolve the base URL and API key env var.
    """
    from .mock import MockProvider
    from .openai_compatible import OpenAICompatibleProvider

    provider_config = provider_config or config.provider
    name = (provider_config.name or "").strip().lower()

    if name == "mock":
        return MockProvider()
    return OpenAICompatibleProvider(provider_config, client=client)
