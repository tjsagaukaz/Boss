"""Ollama / OpenAI-compatible provider."""

from __future__ import annotations

import logging
from typing import Any

from boss.providers.base import ProviderCapability, ProviderInfo

logger = logging.getLogger(__name__)

_DEFAULT_OLLAMA_URL = "http://localhost:11434"


def build_ollama_provider_info(config: dict[str, Any] | None = None) -> ProviderInfo:
    """Build a ProviderInfo for Ollama."""
    config = config or {}
    base_url = config.get("base_url", _DEFAULT_OLLAMA_URL)

    # Ollama supports chat and streaming; vision depends on model; no embeddings via chat API
    capabilities: set[ProviderCapability] = {
        ProviderCapability.CHAT,
        ProviderCapability.STREAMING,
    }

    # Allow config to add capabilities (e.g. tool_use for newer Ollama)
    explicit_caps = config.get("capabilities")
    if explicit_caps and isinstance(explicit_caps, list):
        for c in explicit_caps:
            try:
                capabilities.add(ProviderCapability(c))
            except ValueError:
                pass

    return ProviderInfo(
        name="ollama",
        kind="ollama",
        base_url=base_url,
        capabilities=capabilities,
        models=config.get("models", []),
        enabled=config.get("enabled", False),
    )


def build_compatible_provider_info(name: str, config: dict[str, Any]) -> ProviderInfo:
    """Build a ProviderInfo for a generic OpenAI-compatible endpoint."""
    base_url = config.get("base_url")
    if not base_url:
        logger.warning("Provider %s has no base_url; disabling", name)
        return ProviderInfo(name=name, kind="openai_compatible", enabled=False)

    capabilities: set[ProviderCapability] = set()
    explicit_caps = config.get("capabilities")
    if explicit_caps and isinstance(explicit_caps, list):
        for c in explicit_caps:
            try:
                capabilities.add(ProviderCapability(c))
            except ValueError:
                pass

    return ProviderInfo(
        name=name,
        kind="openai_compatible",
        base_url=base_url,
        api_key_env=config.get("api_key_env"),
        capabilities=capabilities,
        models=config.get("models", []),
        enabled=config.get("enabled", False),
    )


def get_ollama_client(info: ProviderInfo):
    """Create an AsyncOpenAI client pointing at the Ollama/compatible endpoint.

    Ollama exposes an OpenAI-compatible API at /v1, so we reuse the OpenAI SDK.
    """
    from openai import AsyncOpenAI

    base = (info.base_url or _DEFAULT_OLLAMA_URL).rstrip("/")
    # Ollama's OpenAI-compat endpoint is at /v1
    if not base.endswith("/v1"):
        base = base + "/v1"

    return AsyncOpenAI(
        base_url=base,
        api_key="ollama",  # Ollama doesn't need a real key
    )
