"""Tests for boss.providers — multi-provider registry and routing."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from boss.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderInfo,
    ProviderStatus,
)
from boss.providers.registry import (
    ProviderRegistry,
    check_provider_health,
    reset_registry,
)
from boss.providers.openai_provider import build_openai_provider_info
from boss.providers.ollama_provider import build_ollama_provider_info, build_compatible_provider_info


class TestProviderCapability(unittest.TestCase):
    """ProviderCapability enum values."""

    def test_values(self):
        self.assertEqual(ProviderCapability.CHAT, "chat")
        self.assertEqual(ProviderCapability.STREAMING, "streaming")
        self.assertEqual(ProviderCapability.VISION, "vision")
        self.assertEqual(ProviderCapability.EMBEDDINGS, "embeddings")
        self.assertEqual(ProviderCapability.TOOL_USE, "tool_use")


class TestProviderStatus(unittest.TestCase):
    """ProviderStatus enum values."""

    def test_values(self):
        self.assertEqual(ProviderStatus.HEALTHY, "healthy")
        self.assertEqual(ProviderStatus.DEGRADED, "degraded")
        self.assertEqual(ProviderStatus.UNAVAILABLE, "unavailable")
        self.assertEqual(ProviderStatus.UNCHECKED, "unchecked")


class TestProviderHealth(unittest.TestCase):
    """ProviderHealth data structure."""

    def test_defaults(self):
        h = ProviderHealth()
        self.assertEqual(h.status, ProviderStatus.UNCHECKED)
        self.assertIsNone(h.latency_ms)
        self.assertIsNone(h.error)

    def test_to_dict(self):
        h = ProviderHealth(status=ProviderStatus.HEALTHY, latency_ms=42.567)
        d = h.to_dict()
        self.assertEqual(d["status"], "healthy")
        self.assertEqual(d["latency_ms"], 42.6)

    def test_to_dict_with_error(self):
        h = ProviderHealth(status=ProviderStatus.UNAVAILABLE, error="No key")
        d = h.to_dict()
        self.assertEqual(d["error"], "No key")


class TestProviderInfo(unittest.TestCase):
    """ProviderInfo data structure."""

    def test_supports(self):
        info = ProviderInfo(
            name="test",
            kind="openai",
            capabilities={ProviderCapability.CHAT, ProviderCapability.VISION},
        )
        self.assertTrue(info.supports(ProviderCapability.CHAT))
        self.assertTrue(info.supports(ProviderCapability.VISION))
        self.assertFalse(info.supports(ProviderCapability.EMBEDDINGS))

    def test_capability_list_sorted(self):
        info = ProviderInfo(
            name="test",
            kind="openai",
            capabilities={ProviderCapability.VISION, ProviderCapability.CHAT},
        )
        self.assertEqual(info.capability_list(), ["chat", "vision"])

    def test_mark_healthy(self):
        info = ProviderInfo(name="test", kind="openai")
        info.mark_healthy(15.0)
        self.assertEqual(info.health.status, ProviderStatus.HEALTHY)
        self.assertEqual(info.health.latency_ms, 15.0)

    def test_mark_unavailable(self):
        info = ProviderInfo(name="test", kind="openai")
        info.mark_unavailable("connection refused")
        self.assertEqual(info.health.status, ProviderStatus.UNAVAILABLE)
        self.assertEqual(info.health.error, "connection refused")

    def test_to_dict(self):
        info = ProviderInfo(
            name="openai",
            kind="openai",
            base_url=None,
            capabilities={ProviderCapability.CHAT},
            enabled=True,
        )
        d = info.to_dict()
        self.assertEqual(d["name"], "openai")
        self.assertEqual(d["capabilities"], ["chat"])
        self.assertTrue(d["enabled"])


class TestProviderRegistry(unittest.TestCase):
    """Registry routing and capability queries."""

    def setUp(self):
        self.registry = ProviderRegistry()
        self.openai = ProviderInfo(
            name="openai",
            kind="openai",
            capabilities={
                ProviderCapability.CHAT,
                ProviderCapability.STREAMING,
                ProviderCapability.VISION,
                ProviderCapability.EMBEDDINGS,
                ProviderCapability.TOOL_USE,
            },
            enabled=True,
        )
        self.ollama = ProviderInfo(
            name="ollama",
            kind="ollama",
            capabilities={ProviderCapability.CHAT, ProviderCapability.STREAMING},
            enabled=True,
        )
        self.registry.register(self.openai)
        self.registry.register(self.ollama)

    def test_get_provider(self):
        self.assertEqual(self.registry.get("openai"), self.openai)
        self.assertIsNone(self.registry.get("nonexistent"))

    def test_enabled_providers(self):
        self.assertEqual(len(self.registry.enabled_providers), 2)

    def test_disabled_provider_excluded(self):
        self.ollama.enabled = False
        self.assertEqual(len(self.registry.enabled_providers), 1)

    def test_resolve_for_capability(self):
        provider = self.registry.resolve_for_capability(ProviderCapability.EMBEDDINGS)
        self.assertEqual(provider.name, "openai")

    def test_resolve_for_capability_missing(self):
        reg = ProviderRegistry()
        reg.register(self.ollama)
        self.assertIsNone(reg.resolve_for_capability(ProviderCapability.EMBEDDINGS))

    def test_resolve_for_mode_explicit_routing(self):
        self.registry.set_routing({"chat": "ollama", "embeddings": "openai"})
        provider = self.registry.resolve_for_mode("chat")
        self.assertEqual(provider.name, "ollama")

    def test_resolve_for_mode_with_fallback(self):
        self.registry.set_routing({"chat": "nonexistent", "fallback": "ollama"})
        provider = self.registry.resolve_for_mode("chat")
        self.assertEqual(provider.name, "ollama")

    def test_resolve_for_mode_capability_match(self):
        self.registry.set_routing({})
        provider = self.registry.resolve_for_mode("embeddings")
        self.assertEqual(provider.name, "openai")

    def test_routing_table(self):
        self.registry.set_routing({"chat": "openai", "fallback": "ollama"})
        table = self.registry.routing_table
        self.assertIn("chat", table)
        self.assertIn("embeddings", table)
        self.assertEqual(table["fallback"], "ollama")

    def test_capability_map(self):
        cmap = self.registry.capability_map
        self.assertIn("openai", cmap)
        self.assertIn("ollama", cmap)
        self.assertIn("chat", cmap["openai"])
        self.assertIn("embeddings", cmap["openai"])
        self.assertNotIn("embeddings", cmap["ollama"])

    def test_diagnostics(self):
        diag = self.registry.diagnostics()
        self.assertIn("providers", diag)
        self.assertIn("routing", diag)
        self.assertIn("capability_map", diag)
        self.assertEqual(len(diag["providers"]), 2)


class TestOpenAIProviderInfo(unittest.TestCase):
    """OpenAI provider construction."""

    def test_with_api_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            info = build_openai_provider_info()
            self.assertTrue(info.supports(ProviderCapability.CHAT))
            self.assertTrue(info.supports(ProviderCapability.EMBEDDINGS))
            self.assertTrue(info.supports(ProviderCapability.VISION))
            self.assertEqual(info.name, "openai")

    def test_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            env = os.environ.copy()
            env.pop("OPENAI_API_KEY", None)
            with patch.dict(os.environ, env, clear=True):
                info = build_openai_provider_info()
                self.assertEqual(len(info.capabilities), 0)

    def test_explicit_capabilities_restrict(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            info = build_openai_provider_info({"capabilities": ["chat", "streaming"]})
            self.assertTrue(info.supports(ProviderCapability.CHAT))
            self.assertTrue(info.supports(ProviderCapability.STREAMING))
            self.assertFalse(info.supports(ProviderCapability.EMBEDDINGS))


class TestOllamaProviderInfo(unittest.TestCase):
    """Ollama provider construction."""

    def test_defaults(self):
        info = build_ollama_provider_info()
        self.assertEqual(info.name, "ollama")
        self.assertEqual(info.kind, "ollama")
        self.assertTrue(info.supports(ProviderCapability.CHAT))
        self.assertTrue(info.supports(ProviderCapability.STREAMING))
        self.assertFalse(info.supports(ProviderCapability.EMBEDDINGS))
        self.assertFalse(info.enabled)

    def test_custom_url(self):
        info = build_ollama_provider_info({"base_url": "http://gpu-box:11434"})
        self.assertEqual(info.base_url, "http://gpu-box:11434")

    def test_additional_capabilities(self):
        info = build_ollama_provider_info({"capabilities": ["tool_use"]})
        self.assertTrue(info.supports(ProviderCapability.TOOL_USE))


class TestCompatibleProviderInfo(unittest.TestCase):
    """Generic OpenAI-compatible provider construction."""

    def test_with_url(self):
        info = build_compatible_provider_info("lmstudio", {
            "base_url": "http://localhost:1234",
            "capabilities": ["chat"],
            "enabled": True,
        })
        self.assertEqual(info.name, "lmstudio")
        self.assertEqual(info.kind, "openai_compatible")
        self.assertTrue(info.supports(ProviderCapability.CHAT))

    def test_without_url_disabled(self):
        info = build_compatible_provider_info("broken", {})
        self.assertFalse(info.enabled)


class TestHealthCheck(unittest.TestCase):
    """Provider health checking."""

    def test_openai_health_with_key(self):
        info = ProviderInfo(name="openai", kind="openai", api_key_env="OPENAI_API_KEY")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            health = check_provider_health(info)
            self.assertEqual(health.status, ProviderStatus.HEALTHY)

    def test_openai_health_no_key(self):
        info = ProviderInfo(name="openai", kind="openai", api_key_env="OPENAI_API_KEY")
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            health = check_provider_health(info)
            self.assertEqual(health.status, ProviderStatus.UNAVAILABLE)

    def test_ollama_health_no_server(self):
        info = ProviderInfo(
            name="ollama",
            kind="ollama",
            base_url="http://127.0.0.1:19999",
        )
        health = check_provider_health(info)
        self.assertEqual(health.status, ProviderStatus.UNAVAILABLE)

    def test_unknown_kind_unchecked(self):
        info = ProviderInfo(name="custom", kind="unknown_kind")
        health = check_provider_health(info)
        self.assertEqual(health.status, ProviderStatus.UNCHECKED)


class TestRegistryFromConfig(unittest.TestCase):
    """Registry singleton construction."""

    def tearDown(self):
        reset_registry()

    def test_get_registry_returns_registry(self):
        from boss.providers.registry import get_registry

        reg = get_registry()
        self.assertIsInstance(reg, ProviderRegistry)

    def test_get_registry_singleton(self):
        from boss.providers.registry import get_registry

        r1 = get_registry()
        r2 = get_registry()
        self.assertIs(r1, r2)

    def test_reset_clears_singleton(self):
        from boss.providers.registry import get_registry

        r1 = get_registry()
        reset_registry()
        r2 = get_registry()
        self.assertIsNot(r1, r2)


class TestProviderDiagnostics(unittest.TestCase):
    """provider_diagnostics() integration."""

    def tearDown(self):
        reset_registry()

    def test_returns_dict(self):
        from boss.providers.registry import provider_diagnostics

        diag = provider_diagnostics()
        self.assertIn("providers", diag)
        self.assertIn("routing", diag)
        self.assertIn("capability_map", diag)


class TestModelRoutingIntegration(unittest.TestCase):
    """Model routing through provider registry."""

    def test_resolve_alternative_client_none_for_openai_model(self):
        from boss.models import _resolve_alternative_client

        result = _resolve_alternative_client("gpt-4o")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
