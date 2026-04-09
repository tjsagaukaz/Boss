"""Provider registry — capability discovery, routing, and diagnostics."""

from __future__ import annotations

import logging
import time
from typing import Any

from boss.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderInfo,
    ProviderStatus,
)

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Central registry of model providers.

    Providers are registered at startup based on configuration.
    The registry answers routing queries ("which provider handles embeddings?")
    and exposes a capability map for diagnostics.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ProviderInfo] = {}
        self._routing: dict[str, str] = {}  # mode -> provider name
        self._fallback: str | None = None

    # ── registration ────────────────────────────────────────────────

    def register(self, info: ProviderInfo) -> None:
        self._providers[info.name] = info
        logger.debug("Provider registered: %s (%s)", info.name, info.capability_list())

    def set_routing(self, routing: dict[str, str]) -> None:
        """Set per-mode routing.  Keys: chat, reasoning, embeddings, vision, fallback."""
        self._fallback = routing.pop("fallback", None)
        self._routing = routing

    # ── query ───────────────────────────────────────────────────────

    def get(self, name: str) -> ProviderInfo | None:
        return self._providers.get(name)

    @property
    def providers(self) -> list[ProviderInfo]:
        return list(self._providers.values())

    @property
    def enabled_providers(self) -> list[ProviderInfo]:
        return [p for p in self._providers.values() if p.enabled]

    def resolve_for_mode(self, mode: str) -> ProviderInfo | None:
        """Resolve which provider handles a given mode (chat, reasoning, vision, embeddings).

        Falls back to the configured fallback, then to the first enabled provider
        with the matching capability.
        """
        # Check explicit routing
        target_name = self._routing.get(mode)
        if target_name:
            provider = self._providers.get(target_name)
            if provider and provider.enabled:
                return provider

        # Map mode to required capability
        capability = _mode_to_capability(mode)

        # Try fallback
        if self._fallback:
            fb = self._providers.get(self._fallback)
            if fb and fb.enabled and (capability is None or fb.supports(capability)):
                return fb

        # First enabled provider with matching capability
        if capability:
            for p in self._providers.values():
                if p.enabled and p.supports(capability):
                    return p

        # First enabled provider as last resort
        for p in self._providers.values():
            if p.enabled:
                return p
        return None

    def resolve_for_capability(self, cap: ProviderCapability) -> ProviderInfo | None:
        """Find first enabled provider with a given capability."""
        for p in self._providers.values():
            if p.enabled and p.supports(cap):
                return p
        return None

    # ── routing table ───────────────────────────────────────────────

    @property
    def routing_table(self) -> dict[str, str]:
        """Resolved routing for all modes."""
        modes = ["chat", "reasoning", "embeddings", "vision"]
        table: dict[str, str] = {}
        for mode in modes:
            provider = self.resolve_for_mode(mode)
            table[mode] = provider.name if provider else "none"
        if self._fallback:
            table["fallback"] = self._fallback
        return table

    # ── capability map ──────────────────────────────────────────────

    @property
    def capability_map(self) -> dict[str, list[str]]:
        """Provider → capability list map."""
        return {p.name: p.capability_list() for p in self._providers.values()}

    # ── diagnostics ─────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "providers": [p.to_dict() for p in self._providers.values()],
            "routing": self.routing_table,
            "capability_map": self.capability_map,
        }


# ── module-level singleton ──────────────────────────────────────────

_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """Lazily initialize and return the provider registry."""
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def reset_registry() -> None:
    """Reset the singleton for testing."""
    global _registry
    _registry = None


def get_provider(name: str) -> ProviderInfo | None:
    return get_registry().get(name)


def resolve_for_capability(cap: ProviderCapability) -> ProviderInfo | None:
    return get_registry().resolve_for_capability(cap)


def resolve_model_for_mode(mode: str) -> ProviderInfo | None:
    return get_registry().resolve_for_mode(mode)


def provider_diagnostics() -> dict[str, Any]:
    return get_registry().diagnostics()


# ── health check ────────────────────────────────────────────────────


def check_provider_health(info: ProviderInfo) -> ProviderHealth:
    """Probe a provider and return health status.

    For OpenAI: checks api key presence.
    For Ollama/compatible: tries GET /api/tags or /v1/models.
    """
    start = time.time()
    try:
        if info.kind == "openai":
            return _check_openai_health(info, start)
        elif info.kind in ("ollama", "openai_compatible"):
            return _check_compatible_health(info, start)
        else:
            return ProviderHealth(status=ProviderStatus.UNCHECKED)
    except Exception as exc:
        return ProviderHealth(
            status=ProviderStatus.UNAVAILABLE,
            error=str(exc),
            checked_at=time.time(),
        )


def _check_openai_health(info: ProviderInfo, start: float) -> ProviderHealth:
    import os

    key_var = info.api_key_env or "OPENAI_API_KEY"
    if not os.getenv(key_var):
        return ProviderHealth(
            status=ProviderStatus.UNAVAILABLE,
            error=f"API key env var {key_var} not set",
            checked_at=time.time(),
        )
    elapsed = (time.time() - start) * 1000
    return ProviderHealth(
        status=ProviderStatus.HEALTHY,
        latency_ms=elapsed,
        checked_at=time.time(),
    )


def _check_compatible_health(info: ProviderInfo, start: float) -> ProviderHealth:
    """Check health of an OpenAI-compatible endpoint (Ollama, LM Studio, etc.)."""
    import urllib.error
    import urllib.request

    base = (info.base_url or "http://localhost:11434").rstrip("/")

    # Try Ollama API first, then OpenAI-compatible models list
    for path in ("/api/tags", "/v1/models"):
        url = base + path
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    elapsed = (time.time() - start) * 1000
                    return ProviderHealth(
                        status=ProviderStatus.HEALTHY,
                        latency_ms=elapsed,
                        checked_at=time.time(),
                    )
        except (urllib.error.URLError, OSError):
            continue

    return ProviderHealth(
        status=ProviderStatus.UNAVAILABLE,
        error=f"Cannot reach {base}",
        checked_at=time.time(),
    )


# ── construction ────────────────────────────────────────────────────


def _mode_to_capability(mode: str) -> ProviderCapability | None:
    return {
        "chat": ProviderCapability.CHAT,
        "reasoning": ProviderCapability.CHAT,
        "embeddings": ProviderCapability.EMBEDDINGS,
        "vision": ProviderCapability.VISION,
    }.get(mode)


def _build_registry() -> ProviderRegistry:
    """Build the registry from config.toml and environment."""
    from boss.providers.openai_provider import build_openai_provider_info
    from boss.providers.ollama_provider import build_ollama_provider_info

    registry = ProviderRegistry()

    config = _load_provider_config()
    providers_cfg = config.get("providers", {})
    routing_cfg = config.get("routing", {})

    # Always register OpenAI if configured
    openai_cfg = providers_cfg.get("openai", {})
    if openai_cfg.get("enabled", True):
        openai_info = build_openai_provider_info(openai_cfg)
        registry.register(openai_info)

    # Register Ollama / OpenAI-compatible if configured
    ollama_cfg = providers_cfg.get("ollama", {})
    if ollama_cfg.get("enabled", False):
        ollama_info = build_ollama_provider_info(ollama_cfg)
        registry.register(ollama_info)

    # Register any additional openai_compatible providers
    for name, cfg in providers_cfg.items():
        if name in ("openai", "ollama"):
            continue
        if cfg.get("kind") == "openai_compatible" and cfg.get("enabled", False):
            from boss.providers.ollama_provider import build_compatible_provider_info

            registry.register(build_compatible_provider_info(name, cfg))

    registry.set_routing(dict(routing_cfg))

    return registry


def _load_provider_config() -> dict[str, Any]:
    """Load provider config from .boss/config.toml."""
    from boss.config import settings

    config_path = settings.app_data_dir / "config.toml"
    if not config_path.exists():
        return {}

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.warning("No TOML parser available; skipping provider config from config.toml")
            return {}

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        logger.warning("Failed to load provider config from %s: %s", config_path, exc)
        return {}
