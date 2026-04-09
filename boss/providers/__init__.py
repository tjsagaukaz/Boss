"""Multi-provider model layer with capability-driven routing."""

from __future__ import annotations

from boss.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderInfo,
    ProviderStatus,
)
from boss.providers.registry import (
    get_provider,
    get_registry,
    provider_diagnostics,
    resolve_for_capability,
    resolve_model_for_mode,
)

__all__ = [
    "ProviderCapability",
    "ProviderHealth",
    "ProviderInfo",
    "ProviderStatus",
    "get_provider",
    "get_registry",
    "provider_diagnostics",
    "resolve_for_capability",
    "resolve_model_for_mode",
]
