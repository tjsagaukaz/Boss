"""Base types for the provider layer."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProviderCapability(str, Enum):
    """Capability a provider can advertise."""

    CHAT = "chat"
    STREAMING = "streaming"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    TOOL_USE = "tool_use"


class ProviderStatus(str, Enum):
    """Coarse health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNCHECKED = "unchecked"


@dataclass
class ProviderHealth:
    """Result of a provider health check."""

    status: ProviderStatus = ProviderStatus.UNCHECKED
    latency_ms: float | None = None
    error: str | None = None
    checked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status.value}
        if self.latency_ms is not None:
            d["latency_ms"] = round(self.latency_ms, 1)
        if self.error:
            d["error"] = self.error
        if self.checked_at is not None:
            d["checked_at"] = self.checked_at
        return d


@dataclass
class ProviderInfo:
    """Static + runtime information about a registered provider."""

    name: str
    kind: str  # "openai", "ollama", "openai_compatible"
    base_url: str | None = None
    api_key_env: str | None = None
    capabilities: set[ProviderCapability] = field(default_factory=set)
    models: list[str] = field(default_factory=list)
    enabled: bool = True
    health: ProviderHealth = field(default_factory=ProviderHealth)

    # ── capability helpers ──────────────────────────────────────────

    def supports(self, cap: ProviderCapability) -> bool:
        return cap in self.capabilities

    def capability_list(self) -> list[str]:
        return sorted(c.value for c in self.capabilities)

    # ── health ──────────────────────────────────────────────────────

    def mark_healthy(self, latency_ms: float) -> None:
        self.health = ProviderHealth(
            status=ProviderStatus.HEALTHY,
            latency_ms=latency_ms,
            checked_at=time.time(),
        )

    def mark_unavailable(self, error: str) -> None:
        self.health = ProviderHealth(
            status=ProviderStatus.UNAVAILABLE,
            error=error,
            checked_at=time.time(),
        )

    # ── serialization ───────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "capabilities": self.capability_list(),
            "models": self.models,
            "enabled": self.enabled,
            "health": self.health.to_dict(),
        }
