"""OpenAI provider — wraps the existing OpenAI integration."""

from __future__ import annotations

import os
from typing import Any

from boss.providers.base import ProviderCapability, ProviderInfo


def build_openai_provider_info(config: dict[str, Any] | None = None) -> ProviderInfo:
    """Build a ProviderInfo for OpenAI from config and environment."""
    config = config or {}
    api_key_env = config.get("api_key_env", "OPENAI_API_KEY")
    base_url = config.get("base_url")  # None means default OpenAI URL

    capabilities: set[ProviderCapability] = set()

    if os.getenv(api_key_env):
        # OpenAI supports all standard capabilities
        capabilities = {
            ProviderCapability.CHAT,
            ProviderCapability.STREAMING,
            ProviderCapability.VISION,
            ProviderCapability.EMBEDDINGS,
            ProviderCapability.TOOL_USE,
        }

    # Allow config to restrict capabilities
    explicit_caps = config.get("capabilities")
    if explicit_caps and isinstance(explicit_caps, list):
        allowed = set()
        for c in explicit_caps:
            try:
                allowed.add(ProviderCapability(c))
            except ValueError:
                pass
        capabilities = capabilities & allowed

    return ProviderInfo(
        name="openai",
        kind="openai",
        base_url=base_url,
        api_key_env=api_key_env,
        capabilities=capabilities,
        models=config.get("models", []),
        enabled=config.get("enabled", True),
    )
