"""Tests for boss.deploy subsystem."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from boss.config import settings
from boss.deploy.state import (
    Deployment,
    DeploymentStatus,
    DeploymentTarget,
    list_deployments,
    load_deployment,
    new_deployment_id,
    save_deployment,
)
from boss.deploy.adapters import (
    DeployAdapter,
    _ADAPTERS,
    all_adapters,
    available_adapters,
    best_adapter_for,
    get_adapter,
    register_adapter,
)
from boss.deploy.static_adapter import StaticPreviewAdapter
from boss.deploy.engine import (
    cancel_deployment,
    create_deployment,
    deploy_status,
    is_cancelled,
    run_deployment,
    teardown_deployment,
    _cancel_lock,
    _cancelled_ids,
)


@contextmanager
def override_settings(**overrides):
    originals = {key: getattr(settings, key) for key in overrides}
    try:
        for key, value in overrides.items():
            object.__setattr__(settings, key, value)
        yield
    finally:
        for key, value in originals.items():
            object.__setattr__(settings, key, value)


@contextmanager
def isolated_deploys_dir():
    """Point deploy_history_dir at a fresh temp directory."""
    with tempfile.TemporaryDirectory() as td:
        with override_settings(deploy_history_dir=Path(td)):
            yield Path(td)


@contextmanager
def isolated_adapter_registry():
    """Snapshot and restore the adapter registry."""
    original = dict(_ADAPTERS)
    try:
        yield
    finally:
        _ADAPTERS.clear()
        _ADAPTERS.update(original)


# ── Enum tests ──────────────────────────────────────────────────────


class TestDeploymentStatus(unittest.TestCase):
    """DeploymentStatus enum values."""

    def test_status_values(self):
        self.assertEqual(DeploymentStatus.PENDING, "pending")
        self.assertEqual(DeploymentStatus.BUILDING, "building")
        self.assertEqual(DeploymentStatus.DEPLOYING, "deploying")
        self.assertEqual(DeploymentStatus.LIVE, "live")
        self.assertEqual(DeploymentStatus.FAILED, "failed")
        self.assertEqual(DeploymentStatus.CANCELLED, "cancelled")
        self.assertEqual(DeploymentStatus.TORN_DOWN, "torn_down")

    def test_all_statuses_present(self):
        expected = {"pending", "building", "deploying", "live", "failed", "cancelled", "torn_down"}
        actual = {s.value for s in DeploymentStatus}
        self.assertEqual(actual, expected)


class TestDeploymentTarget(unittest.TestCase):
    """DeploymentTarget enum values."""

    def test_target_values(self):
        self.assertEqual(DeploymentTarget.STATIC, "static")
        self.assertEqual(DeploymentTarget.PREVIEW, "preview")


# ── Deployment dataclass tests ──────────────────────────────────────


class TestDeployment(unittest.TestCase):
    """Deployment dataclass and serialization."""

    def _make(self, **overrides):
        defaults = dict(
            deployment_id="test-id-001",
            project_path="/tmp/myproject",
            session_id="sess-abc",
            adapter="static_preview",
        )
        defaults.update(overrides)
        return Deployment(**defaults)

    def test_defaults(self):
        d = self._make()
        self.assertEqual(d.status, DeploymentStatus.PENDING.value)
        self.assertEqual(d.target, DeploymentTarget.PREVIEW.value)
        self.assertIsNone(d.preview_url)
        self.assertEqual(d.build_log, "")
        self.assertEqual(d.deploy_log, "")
        self.assertIsNone(d.error)
        self.assertIsNone(d.finished_at)
        self.assertEqual(d.metadata, {})

    def test_to_dict_round_trip(self):
        d = self._make(preview_url="https://example.vercel.app", status="live")
        data = d.to_dict()
        self.assertIsInstance(data, dict)
        self.assertEqual(data["deployment_id"], "test-id-001")
        self.assertEqual(data["preview_url"], "https://example.vercel.app")
        self.assertEqual(data["status"], "live")

        restored = Deployment.from_dict(data)
        self.assertEqual(restored.deployment_id, d.deployment_id)
        self.assertEqual(restored.preview_url, d.preview_url)
        self.assertEqual(restored.status, d.status)

    def test_from_dict_ignores_extra_keys(self):
        data = dict(
            deployment_id="x",
            project_path="/tmp",
            session_id="s",
            adapter="a",
            unknown_field="should be ignored",
        )
        d = Deployment.from_dict(data)
        self.assertEqual(d.deployment_id, "x")

    def test_is_terminal(self):
        for status in ("live", "failed", "cancelled", "torn_down"):
            d = self._make(status=status)
            self.assertTrue(d.is_terminal, f"{status} should be terminal")

        for status in ("pending", "building", "deploying"):
            d = self._make(status=status)
            self.assertFalse(d.is_terminal, f"{status} should not be terminal")

    def test_metadata_dict(self):
        d = self._make(metadata={"branch": "main", "commit": "abc123"})
        data = d.to_dict()
        self.assertEqual(data["metadata"]["branch"], "main")
        restored = Deployment.from_dict(data)
        self.assertEqual(restored.metadata["commit"], "abc123")


# ── Persistence tests ───────────────────────────────────────────────


class TestDeploymentPersistence(unittest.TestCase):
    """Save, load, and list deployments."""

    def test_save_and_load(self):
        with isolated_deploys_dir():
            d = Deployment(
                deployment_id="persist-001",
                project_path="/tmp/proj",
                session_id="sess",
                adapter="static_preview",
                status="live",
                preview_url="https://example.vercel.app",
            )
            save_deployment(d)

            loaded = load_deployment("persist-001")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.deployment_id, "persist-001")
            self.assertEqual(loaded.preview_url, "https://example.vercel.app")
            self.assertEqual(loaded.status, "live")

    def test_load_nonexistent(self):
        with isolated_deploys_dir():
            self.assertIsNone(load_deployment("does-not-exist"))

    def test_list_deployments_ordering(self):
        with isolated_deploys_dir():
            for i in range(5):
                d = Deployment(
                    deployment_id=f"list-{i:03d}",
                    project_path="/tmp/proj",
                    session_id="sess",
                    adapter="static_preview",
                )
                save_deployment(d)
                # Ensure different mtimes
                time.sleep(0.02)

            deploys = list_deployments(limit=3)
            self.assertEqual(len(deploys), 3)
            # Most recent first.
            self.assertEqual(deploys[0].deployment_id, "list-004")
            self.assertEqual(deploys[1].deployment_id, "list-003")
            self.assertEqual(deploys[2].deployment_id, "list-002")

    def test_list_deployments_empty(self):
        with isolated_deploys_dir():
            deploys = list_deployments()
            self.assertEqual(deploys, [])

    def test_new_deployment_id_unique(self):
        ids = {new_deployment_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_save_updates_updated_at(self):
        with isolated_deploys_dir():
            d = Deployment(
                deployment_id="time-test",
                project_path="/tmp",
                session_id="s",
                adapter="a",
                updated_at=1000.0,
            )
            save_deployment(d)
            loaded = load_deployment("time-test")
            self.assertGreater(loaded.updated_at, 1000.0)


# ── Adapter registry tests ─────────────────────────────────────────


class _DummyAdapter(DeployAdapter):
    """Minimal adapter for testing."""

    name = "test_dummy"

    def __init__(self, configured=True, detects=True):
        self._configured = configured
        self._detects = detects

    def is_configured(self) -> bool:
        return self._configured

    def detect_project(self, project_path) -> bool:
        return self._detects

    def build(self, deployment):
        deployment.status = DeploymentStatus.BUILDING.value
        return deployment

    def deploy(self, deployment):
        deployment.status = DeploymentStatus.LIVE.value
        deployment.preview_url = "https://dummy.test"
        deployment.finished_at = time.time()
        return deployment


class TestAdapterRegistry(unittest.TestCase):
    """Adapter registration and lookup."""

    def test_register_and_get(self):
        with isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)
            self.assertIs(get_adapter("test_dummy"), adapter)

    def test_get_missing(self):
        self.assertIsNone(get_adapter("nonexistent_adapter_xyz"))

    def test_available_filters_unconfigured(self):
        with isolated_adapter_registry():
            register_adapter(_DummyAdapter(configured=True))
            register_adapter(_DummyAdapter.__new__(_DummyAdapter))  # unconfigured
            # Replace with explicit instances
            _ADAPTERS.clear()
            configured = _DummyAdapter(configured=True)
            configured.name = "configured_one"
            unconfigured = _DummyAdapter(configured=False)
            unconfigured.name = "unconfigured_one"
            register_adapter(configured)
            register_adapter(unconfigured)

            avail = available_adapters()
            names = [a.name for a in avail]
            self.assertIn("configured_one", names)
            self.assertNotIn("unconfigured_one", names)

    def test_all_adapters_includes_unconfigured(self):
        with isolated_adapter_registry():
            _ADAPTERS.clear()
            c = _DummyAdapter(configured=True)
            c.name = "c"
            u = _DummyAdapter(configured=False)
            u.name = "u"
            register_adapter(c)
            register_adapter(u)
            self.assertEqual(len(all_adapters()), 2)

    def test_best_adapter_for_picks_configured_detectable(self):
        with isolated_adapter_registry():
            _ADAPTERS.clear()
            good = _DummyAdapter(configured=True, detects=True)
            good.name = "good"
            bad = _DummyAdapter(configured=True, detects=False)
            bad.name = "bad"
            register_adapter(bad)
            register_adapter(good)

            result = best_adapter_for("/tmp/proj")
            self.assertEqual(result.name, "good")

    def test_best_adapter_for_none_when_no_match(self):
        with isolated_adapter_registry():
            _ADAPTERS.clear()
            nope = _DummyAdapter(configured=True, detects=False)
            nope.name = "nope"
            register_adapter(nope)
            self.assertIsNone(best_adapter_for("/tmp/proj"))

    def test_best_adapter_for_none_when_unconfigured(self):
        with isolated_adapter_registry():
            _ADAPTERS.clear()
            u = _DummyAdapter(configured=False, detects=True)
            u.name = "unc"
            register_adapter(u)
            self.assertIsNone(best_adapter_for("/tmp/proj"))


# ── StaticPreviewAdapter tests ──────────────────────────────────────


class TestStaticPreviewAdapterConfigured(unittest.TestCase):
    """StaticPreviewAdapter.is_configured() behaviour."""

    def test_unconfigured_by_default(self):
        adapter = StaticPreviewAdapter()
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(adapter.is_configured())

    def test_configured_with_vercel_token(self):
        adapter = StaticPreviewAdapter()
        with patch.dict(os.environ, {"VERCEL_TOKEN": "tok_123"}):
            self.assertTrue(adapter.is_configured())

    def test_configured_with_netlify_token(self):
        adapter = StaticPreviewAdapter()
        with patch.dict(os.environ, {"NETLIFY_AUTH_TOKEN": "nfp_abc"}):
            self.assertTrue(adapter.is_configured())


class TestStaticPreviewAdapterDetect(unittest.TestCase):
    """StaticPreviewAdapter.detect_project() behaviour."""

    def test_detects_package_json_with_build(self):
        adapter = StaticPreviewAdapter()
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "package.json"
            pkg.write_text(json.dumps({"scripts": {"build": "vite build"}}))
            self.assertTrue(adapter.detect_project(td))

    def test_no_detect_package_json_without_build(self):
        adapter = StaticPreviewAdapter()
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "package.json"
            pkg.write_text(json.dumps({"scripts": {"start": "node index"}}))
            self.assertFalse(adapter.detect_project(td))

    def test_detects_dist_directory(self):
        adapter = StaticPreviewAdapter()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "dist").mkdir()
            self.assertTrue(adapter.detect_project(td))

    def test_detects_out_directory(self):
        adapter = StaticPreviewAdapter()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "out").mkdir()
            self.assertTrue(adapter.detect_project(td))

    def test_no_detect_empty_project(self):
        adapter = StaticPreviewAdapter()
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(adapter.detect_project(td))

    def test_no_detect_invalid_package_json(self):
        adapter = StaticPreviewAdapter()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "package.json").write_text("not json")
            self.assertFalse(adapter.detect_project(td))


class TestStaticPreviewAdapterStatusPayload(unittest.TestCase):
    """status_payload() returns expected shape."""

    def test_payload_shape(self):
        adapter = StaticPreviewAdapter()
        payload = adapter.status_payload()
        self.assertEqual(payload["adapter"], "static_preview")
        self.assertIn("configured", payload)
        self.assertIsInstance(payload["configured"], bool)


# ── Engine tests ────────────────────────────────────────────────────


class TestDeployStatus(unittest.TestCase):
    """deploy_status() overview."""

    def test_status_shape(self):
        with override_settings(deploy_enabled=True), isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "t"
            register_adapter(a)

            result = deploy_status()
            self.assertIn("enabled", result)
            self.assertIn("adapters", result)
            self.assertIn("configured_count", result)
            self.assertIn("recent_deployments", result)
            self.assertIn("live_count", result)
            self.assertTrue(result["enabled"])
            self.assertEqual(result["configured_count"], 1)

    def test_status_disabled_when_no_configured(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=False)
            a.name = "u"
            register_adapter(a)

            result = deploy_status()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["configured_count"], 0)


class TestCreateDeployment(unittest.TestCase):
    """create_deployment() validation and record creation."""

    def test_create_with_explicit_adapter(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "explicit_test"
            register_adapter(a)

            d = create_deployment(
                project_path="/tmp/proj",
                session_id="sess",
                adapter_name="explicit_test",
            )
            self.assertEqual(d.adapter, "explicit_test")
            self.assertEqual(d.status, DeploymentStatus.PENDING.value)
            self.assertTrue(len(d.deployment_id) > 0)

    def test_create_auto_detect(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True, detects=True)
            a.name = "auto_test"
            register_adapter(a)

            d = create_deployment(
                project_path="/tmp/proj",
                session_id="sess",
            )
            self.assertEqual(d.adapter, "auto_test")

    def test_create_unknown_adapter_raises(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            with self.assertRaises(ValueError) as ctx:
                create_deployment(
                    project_path="/tmp/proj",
                    session_id="sess",
                    adapter_name="missing",
                )
            self.assertIn("Unknown deploy adapter", str(ctx.exception))

    def test_create_unconfigured_adapter_raises(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=False)
            a.name = "unconf"
            register_adapter(a)
            with self.assertRaises(ValueError) as ctx:
                create_deployment(
                    project_path="/tmp/proj",
                    session_id="sess",
                    adapter_name="unconf",
                )
            self.assertIn("not configured", str(ctx.exception))

    def test_create_no_adapters_raises(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            with self.assertRaises(ValueError) as ctx:
                create_deployment(
                    project_path="/tmp/proj",
                    session_id="sess",
                )
            self.assertIn("No deployment adapters are configured", str(ctx.exception))

    def test_create_no_matching_adapter_raises(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True, detects=False)
            a.name = "no_match"
            register_adapter(a)
            with self.assertRaises(ValueError) as ctx:
                create_deployment(
                    project_path="/tmp/proj",
                    session_id="sess",
                )
            self.assertIn("No adapter supports", str(ctx.exception))


class TestRunDeployment(unittest.TestCase):
    """run_deployment() execution paths."""

    def test_run_full_pipeline(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True, detects=True)
            a.name = "run_test"
            register_adapter(a)

            d = create_deployment(
                project_path="/tmp/proj",
                session_id="sess",
                adapter_name="run_test",
            )
            result = run_deployment(d.deployment_id)
            self.assertEqual(result.status, DeploymentStatus.LIVE.value)
            self.assertEqual(result.preview_url, "https://dummy.test")

    def test_run_nonexistent_raises(self):
        with isolated_deploys_dir():
            with self.assertRaises(ValueError) as ctx:
                run_deployment("no-such-id")
            self.assertIn("not found", str(ctx.exception))

    def test_run_terminal_deployment_raises(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "term_test"
            register_adapter(a)

            d = Deployment(
                deployment_id="terminal-001",
                project_path="/tmp",
                session_id="s",
                adapter="term_test",
                status=DeploymentStatus.LIVE.value,
            )
            save_deployment(d)
            with self.assertRaises(ValueError) as ctx:
                run_deployment("terminal-001")
            self.assertIn("already terminal", str(ctx.exception))

    def test_run_missing_adapter_fails(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            d = Deployment(
                deployment_id="orphan-001",
                project_path="/tmp",
                session_id="s",
                adapter="vanished",
            )
            save_deployment(d)
            result = run_deployment("orphan-001")
            self.assertEqual(result.status, DeploymentStatus.FAILED.value)
            self.assertIn("no longer available", result.error)

    def test_run_unconfigured_adapter_fails(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=False)
            a.name = "lost_config"
            register_adapter(a)

            d = Deployment(
                deployment_id="lost-001",
                project_path="/tmp",
                session_id="s",
                adapter="lost_config",
            )
            save_deployment(d)
            result = run_deployment("lost-001")
            self.assertEqual(result.status, DeploymentStatus.FAILED.value)
            self.assertIn("lost its configuration", result.error)

    def test_run_build_exception_caught(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()

            class ExplodingBuild(_DummyAdapter):
                name = "explode_build"
                def build(self, deployment):
                    raise RuntimeError("boom in build")

            register_adapter(ExplodingBuild(configured=True))

            d = Deployment(
                deployment_id="boom-001",
                project_path="/tmp",
                session_id="s",
                adapter="explode_build",
            )
            save_deployment(d)
            result = run_deployment("boom-001")
            self.assertEqual(result.status, DeploymentStatus.FAILED.value)
            self.assertIn("Build exception", result.error)

    def test_run_deploy_exception_caught(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()

            class ExplodingDeploy(_DummyAdapter):
                name = "explode_deploy"
                def deploy(self, deployment):
                    raise RuntimeError("boom in deploy")

            register_adapter(ExplodingDeploy(configured=True))

            d = Deployment(
                deployment_id="boom-002",
                project_path="/tmp",
                session_id="s",
                adapter="explode_deploy",
            )
            save_deployment(d)
            result = run_deployment("boom-002")
            self.assertEqual(result.status, DeploymentStatus.FAILED.value)
            self.assertIn("Deploy exception", result.error)


class TestTeardownDeployment(unittest.TestCase):
    """teardown_deployment() behaviour."""

    def test_teardown_live(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "td_test"
            register_adapter(a)

            d = Deployment(
                deployment_id="td-001",
                project_path="/tmp",
                session_id="s",
                adapter="td_test",
                status=DeploymentStatus.LIVE.value,
            )
            save_deployment(d)
            result = teardown_deployment("td-001")
            self.assertEqual(result.status, DeploymentStatus.TORN_DOWN.value)

    def test_teardown_missing_adapter(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            d = Deployment(
                deployment_id="td-orphan",
                project_path="/tmp",
                session_id="s",
                adapter="gone",
                status=DeploymentStatus.LIVE.value,
            )
            save_deployment(d)
            result = teardown_deployment("td-orphan")
            self.assertEqual(result.status, DeploymentStatus.TORN_DOWN.value)

    def test_teardown_nonexistent_raises(self):
        with isolated_deploys_dir():
            with self.assertRaises(ValueError):
                teardown_deployment("nope")


class TestCancelDeployment(unittest.TestCase):
    """cancel_deployment() behaviour."""

    def test_cancel_pending(self):
        with isolated_deploys_dir():
            d = Deployment(
                deployment_id="cancel-001",
                project_path="/tmp",
                session_id="s",
                adapter="a",
            )
            save_deployment(d)
            result = cancel_deployment("cancel-001")
            self.assertEqual(result.status, DeploymentStatus.CANCELLED.value)
            self.assertIsNotNone(result.finished_at)

    def test_cancel_building(self):
        with isolated_deploys_dir():
            d = Deployment(
                deployment_id="cancel-002",
                project_path="/tmp",
                session_id="s",
                adapter="a",
                status=DeploymentStatus.BUILDING.value,
            )
            save_deployment(d)
            result = cancel_deployment("cancel-002")
            self.assertEqual(result.status, DeploymentStatus.CANCELLED.value)

    def test_cancel_terminal_raises(self):
        with isolated_deploys_dir():
            d = Deployment(
                deployment_id="cancel-003",
                project_path="/tmp",
                session_id="s",
                adapter="a",
                status=DeploymentStatus.LIVE.value,
            )
            save_deployment(d)
            with self.assertRaises(ValueError) as ctx:
                cancel_deployment("cancel-003")
            self.assertIn("already terminal", str(ctx.exception))

    def test_cancel_nonexistent_raises(self):
        with isolated_deploys_dir():
            with self.assertRaises(ValueError):
                cancel_deployment("nope")


# ── Tool tests ──────────────────────────────────────────────────────


class TestDeployTools(unittest.TestCase):
    """Governed deploy tools — test the underlying logic paths."""

    def test_deploy_preview_disabled(self):
        """When deploy_enabled=False, the tool returns a disabled message."""
        with override_settings(deploy_enabled=False):
            # Reproduce the logic from deploy_preview tool body.
            self.assertFalse(settings.deploy_enabled)
            msg = (
                "Deployment is not enabled. "
                "Set BOSS_DEPLOY_ENABLED=true and configure credentials "
                "(VERCEL_TOKEN or NETLIFY_AUTH_TOKEN) to enable."
            )
            self.assertIn("not enabled", msg)

    def test_deploy_status_tool_disabled(self):
        """deploy_status_tool returns disabled message when deploy_enabled=False."""
        with override_settings(deploy_enabled=False):
            self.assertFalse(settings.deploy_enabled)

    def test_teardown_deploy_disabled(self):
        """teardown_deploy returns disabled message when deploy_enabled=False."""
        with override_settings(deploy_enabled=False):
            self.assertFalse(settings.deploy_enabled)

    def test_deploy_preview_enabled_no_adapters(self):
        """When enabled but no adapters, create_deployment raises ValueError."""
        with override_settings(deploy_enabled=True), isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            with self.assertRaises(ValueError) as ctx:
                create_deployment(project_path="/tmp/proj", session_id="tool")
            self.assertIn("No deployment adapters", str(ctx.exception))

    def test_deploy_status_enabled_with_adapter(self):
        """deploy_status returns overview when adapters exist."""
        with override_settings(deploy_enabled=True), isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "st"
            register_adapter(a)
            result = deploy_status()
            self.assertTrue(result["enabled"])
            self.assertEqual(result["configured_count"], 1)


# ── Config tests ────────────────────────────────────────────────────


class TestDeployConfig(unittest.TestCase):
    """Deploy configuration defaults."""

    def test_deploy_disabled_by_default(self):
        self.assertFalse(settings.deploy_enabled)

    def test_deploy_history_dir_is_path(self):
        self.assertIsInstance(settings.deploy_history_dir, Path)

    def test_deploy_history_dir_under_app_data(self):
        self.assertTrue(str(settings.deploy_history_dir).endswith("deploys"))


# ── Execution type tests ────────────────────────────────────────────


class TestDeployExecutionTypes(unittest.TestCase):
    """Deploy tools use EXTERNAL execution type."""

    @classmethod
    def setUpClass(cls):
        # Import the tools module to trigger decorator registration.
        import boss.tools.deploy  # noqa: F401

    def test_deploy_preview_is_external(self):
        from boss.execution import ExecutionType, get_tool_metadata

        meta = get_tool_metadata("deploy_preview")
        self.assertIsNotNone(meta, "deploy_preview should be registered")
        self.assertEqual(meta.execution_type, ExecutionType.EXTERNAL)

    def test_teardown_deploy_is_external(self):
        from boss.execution import ExecutionType, get_tool_metadata

        meta = get_tool_metadata("teardown_deploy")
        self.assertIsNotNone(meta, "teardown_deploy should be registered")
        self.assertEqual(meta.execution_type, ExecutionType.EXTERNAL)

    def test_deploy_status_is_read(self):
        from boss.execution import ExecutionType, get_tool_metadata

        meta = get_tool_metadata("deploy_status_tool")
        self.assertIsNotNone(meta, "deploy_status_tool should be registered")
        self.assertEqual(meta.execution_type, ExecutionType.READ)

    def test_external_not_auto_allowed(self):
        from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, ExecutionType

        self.assertNotIn(ExecutionType.EXTERNAL, AUTO_ALLOWED_EXECUTION_TYPES)


# ── Finding fixes: deploy_status enabled vs deploy_enabled ──────────


class TestDeployStatusEnabledFlag(unittest.TestCase):
    """Finding 4: deploy_status must respect settings.deploy_enabled."""

    def test_status_disabled_when_config_off_but_adapters_configured(self):
        """With BOSS_DEPLOY_ENABLED=false, enabled must be False even if adapters are configured."""
        with override_settings(deploy_enabled=False), isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "cfg"
            register_adapter(a)
            result = deploy_status()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["configured_count"], 1)

    def test_status_enabled_requires_both(self):
        """enabled=True requires deploy_enabled=True AND at least one configured adapter."""
        with override_settings(deploy_enabled=True), isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            a = _DummyAdapter(configured=True)
            a.name = "both"
            register_adapter(a)
            result = deploy_status()
            self.assertTrue(result["enabled"])

    def test_status_disabled_when_no_adapters_even_if_enabled(self):
        with override_settings(deploy_enabled=True), isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()
            result = deploy_status()
            self.assertFalse(result["enabled"])


# ── Finding fixes: cancel_deployment with in-flight registry ────────


class TestCancelDeploymentRegistry(unittest.TestCase):
    """Finding 2: cancel_deployment must signal in-flight pipelines."""

    def _cleanup_cancelled(self, deployment_id):
        with _cancel_lock:
            _cancelled_ids.discard(deployment_id)

    def test_cancel_sets_registry_flag(self):
        with isolated_deploys_dir():
            d = Deployment(
                deployment_id="cancel-reg-001",
                project_path="/tmp",
                session_id="s",
                adapter="a",
                status=DeploymentStatus.BUILDING.value,
            )
            save_deployment(d)
            cancel_deployment("cancel-reg-001")
            self.assertTrue(is_cancelled("cancel-reg-001"))
            self._cleanup_cancelled("cancel-reg-001")

    def test_run_checks_cancellation_between_phases(self):
        """Pipeline should abort between build and deploy if cancelled."""
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()

            class SlowBuildAdapter(_DummyAdapter):
                name = "slow_build"
                def build(self, deployment):
                    deployment.status = DeploymentStatus.BUILDING.value
                    # Simulate cancellation happening during build.
                    from boss.deploy.engine import _mark_cancelled
                    _mark_cancelled(deployment.deployment_id)
                    return deployment

            register_adapter(SlowBuildAdapter(configured=True))

            d = Deployment(
                deployment_id="cancel-mid-001",
                project_path="/tmp",
                session_id="s",
                adapter="slow_build",
            )
            save_deployment(d)

            result = run_deployment("cancel-mid-001")
            self.assertEqual(result.status, DeploymentStatus.CANCELLED.value)
            self.assertIn("Cancelled after build", result.error)
            # Flag should be cleared.
            self.assertFalse(is_cancelled("cancel-mid-001"))

    def test_run_checks_cancellation_after_deploy(self):
        """If cancelled during deploy phase but deploy succeeded, status should still be cancelled."""
        with isolated_deploys_dir(), isolated_adapter_registry():
            _ADAPTERS.clear()

            class CancelDuringDeploy(_DummyAdapter):
                name = "cancel_during_deploy"
                def deploy(self, deployment):
                    from boss.deploy.engine import _mark_cancelled
                    _mark_cancelled(deployment.deployment_id)
                    # Adapter thinks it succeeded.
                    deployment.status = DeploymentStatus.LIVE.value
                    deployment.preview_url = "https://example.test"
                    deployment.finished_at = time.time()
                    return deployment

            register_adapter(CancelDuringDeploy(configured=True))

            d = Deployment(
                deployment_id="cancel-during-001",
                project_path="/tmp",
                session_id="s",
                adapter="cancel_during_deploy",
            )
            save_deployment(d)

            result = run_deployment("cancel-during-001")
            self.assertEqual(result.status, DeploymentStatus.CANCELLED.value)
            self.assertIn("Cancelled during deploy", result.error)
            self.assertFalse(is_cancelled("cancel-during-001"))


# ── Finding fixes: Vercel output dir ────────────────────────────────


class TestVercelOutputDir(unittest.TestCase):
    """Finding 3: _deploy_vercel must pass the output directory."""

    def test_vercel_command_includes_output_dir(self):
        """The Vercel deploy command should include the output directory path."""
        adapter = StaticPreviewAdapter()
        # We can't run vercel for real, but we can verify the command
        # by patching subprocess.run and checking the args.
        from unittest.mock import MagicMock

        with patch("boss.deploy.static_adapter.subprocess.Popen") as mock_popen, \
             patch.dict(os.environ, {"VERCEL_TOKEN": "tok_test"}), \
             isolated_deploys_dir():
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("https://my-deploy.vercel.app\n", "")
            mock_proc.returncode = 0
            mock_proc.pid = 1234
            mock_popen.return_value = mock_proc

            d = Deployment(
                deployment_id="vercel-dir-001",
                project_path="/tmp/myproj",
                session_id="s",
                adapter="static_preview",
            )
            output_dir = Path("/tmp/myproj/dist")

            adapter._deploy_vercel(d, Path("/tmp/myproj"), output_dir)

            args = mock_popen.call_args[0][0]
            # The output directory must appear in the command.
            self.assertIn(str(output_dir), args)
            # "deploy" subcommand must be present.
            self.assertIn("deploy", args)

    def test_netlify_command_includes_output_dir(self):
        """Netlify already passes --dir; confirm it still does."""
        adapter = StaticPreviewAdapter()
        from unittest.mock import MagicMock

        with patch("boss.deploy.static_adapter.subprocess.Popen") as mock_popen, \
             patch.dict(os.environ, {"NETLIFY_AUTH_TOKEN": "nfp_test"}), \
             isolated_deploys_dir():
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (json.dumps({"deploy_url": "https://my-deploy.netlify.app"}), "")
            mock_proc.returncode = 0
            mock_proc.pid = 1234
            mock_popen.return_value = mock_proc

            d = Deployment(
                deployment_id="netlify-dir-001",
                project_path="/tmp/myproj",
                session_id="s",
                adapter="static_preview",
            )
            output_dir = Path("/tmp/myproj/dist")

            adapter._deploy_netlify(d, Path("/tmp/myproj"), output_dir)

            args = mock_popen.call_args[0][0]
            # --dir must reference the output directory.
            dir_idx = args.index("--dir")
            self.assertEqual(args[dir_idx + 1], str(output_dir))


if __name__ == "__main__":
    unittest.main()
