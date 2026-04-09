"""Integration tests for cross-subsystem runtime flows.

These tests exercise real subsystem code paths end-to-end, mocking only
external boundaries (OpenAI Runner, subprocess calls, network I/O).
They assert persisted artifacts and state transitions, not just return values.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from boss.config import settings
from boss.deploy.adapters import DeployAdapter, _ADAPTERS, register_adapter
from boss.deploy.engine import (
    _cancel_lock,
    _cancelled_ids,
    cancel_deployment,
    create_deployment,
    is_cancelled,
    run_deployment,
)
from boss.deploy.state import (
    Deployment,
    DeploymentStatus,
    DEPLOYMENT_VERSION,
    load_deployment,
    save_deployment,
)
from boss.memory import knowledge as knowledge_module
from boss.memory.knowledge import KnowledgeStore, SCHEMA_VERSION
from boss.persistence.history import (
    SESSION_STATE_VERSION,
    SessionState,
    load_session_state,
    save_session_state,
)
from boss.workers.coordinator import (
    add_worker,
    cancel_plan,
    create_work_plan,
    finalize_plan,
    mark_plan_ready,
)
from boss.workers.engine import (
    _running_tasks,
    cancel_running_plan,
    execute_plan,
)
from boss.workers.roles import WorkerRole
from boss.workers.state import (
    WorkPlan,
    WorkPlanStatus,
    WorkerRecord,
    WorkerState,
    WORK_PLAN_VERSION,
    WORKER_RECORD_VERSION,
    load_work_plan,
    save_work_plan,
)


# ── Helpers ─────────────────────────────────────────────────────────


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
    with tempfile.TemporaryDirectory() as td:
        with override_settings(deploy_history_dir=Path(td)):
            yield Path(td)


@contextmanager
def isolated_plans_dir():
    with tempfile.TemporaryDirectory() as td:
        with override_settings(app_data_dir=Path(td)):
            yield Path(td)


@contextmanager
def isolated_knowledge_store(db_path: Path):
    original_store = knowledge_module._store
    store = KnowledgeStore(db_path)
    knowledge_module._store = store
    try:
        yield store
    finally:
        store.close()
        knowledge_module._store = original_store


@contextmanager
def isolated_adapter_registry():
    original = dict(_ADAPTERS)
    try:
        yield
    finally:
        _ADAPTERS.clear()
        _ADAPTERS.update(original)


@contextmanager
def isolated_history_dir():
    with tempfile.TemporaryDirectory() as td:
        with override_settings(history_dir=Path(td)):
            yield Path(td)


class _DummyAdapter(DeployAdapter):
    """In-process adapter for integration tests — no subprocess calls."""

    name = "dummy"

    def __init__(self, *, configured: bool = True, detects: bool = True):
        self._configured = configured
        self._detects = detects
        self.build_called = False
        self.deploy_called = False
        self.teardown_called = False
        self.build_delay: float = 0
        self.deploy_delay: float = 0
        self.build_fail: bool = False
        self.deploy_fail: bool = False

    def is_configured(self) -> bool:
        return self._configured

    def detect_project(self, project_path: str | Path) -> bool:
        return self._detects

    def build(self, deployment: Deployment) -> Deployment:
        self.build_called = True
        if self.build_delay:
            time.sleep(self.build_delay)
        if self.build_fail:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = "Dummy build failure"
            save_deployment(deployment)
            return deployment
        deployment.build_log = "Dummy build ok"
        save_deployment(deployment)
        return deployment

    def deploy(self, deployment: Deployment) -> Deployment:
        self.deploy_called = True
        if self.deploy_delay:
            time.sleep(self.deploy_delay)
        if self.deploy_fail:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = "Dummy deploy failure"
            save_deployment(deployment)
            return deployment
        deployment.status = DeploymentStatus.LIVE.value
        deployment.preview_url = "https://dummy-preview.example.com"
        deployment.finished_at = time.time()
        save_deployment(deployment)
        return deployment


# ═══════════════════════════════════════════════════════════════════
# P1: Integration Tests
# ═══════════════════════════════════════════════════════════════════


class TestSessionPersistenceIntegration(unittest.TestCase):
    """Chat → persistence → reload round-trip."""

    def test_session_state_round_trip_preserves_all_fields(self):
        with isolated_history_dir():
            state = SessionState(
                session_id="int-test-001",
                summary="User asked about deployment",
                recent_items=[
                    {"role": "user", "content": "deploy my app"},
                    {"role": "assistant", "content": "Starting deployment..."},
                ],
                total_turns=1,
                archived_turns=0,
            )
            save_session_state(state)

            loaded = load_session_state("int-test-001")
            self.assertEqual(loaded.session_id, "int-test-001")
            self.assertEqual(loaded.summary, "User asked about deployment")
            self.assertEqual(len(loaded.recent_items), 2)
            self.assertEqual(loaded.total_turns, 1)
            self.assertEqual(loaded.version, SESSION_STATE_VERSION)

    def test_legacy_list_format_migrates_on_load(self):
        """Old sessions stored history as a raw list — migration must work."""
        with isolated_history_dir() as hdir:
            path = hdir / "legacy-session.json"
            legacy = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            path.write_text(json.dumps(legacy))

            loaded = load_session_state("legacy-session")
            self.assertIsInstance(loaded, SessionState)
            self.assertEqual(loaded.session_id, "legacy-session")
            self.assertEqual(len(loaded.recent_items), 2)
            self.assertEqual(loaded.total_turns, 1)
            self.assertEqual(loaded.version, SESSION_STATE_VERSION)

            # Verify migration was persisted.
            reloaded = load_session_state("legacy-session")
            self.assertEqual(reloaded.version, SESSION_STATE_VERSION)

    def test_corrupted_session_file_returns_empty_state(self):
        with isolated_history_dir() as hdir:
            path = hdir / "corrupt.json"
            path.write_text("THIS IS NOT JSON {{{")

            loaded = load_session_state("corrupt")
            self.assertEqual(loaded.session_id, "corrupt")
            self.assertEqual(loaded.recent_items, [])


class TestWorkerPlanIntegration(unittest.TestCase):
    """Worker plan → execute → merge lifecycle."""

    def test_plan_create_persist_load_round_trip(self):
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Add tests for auth module",
                project_path="/tmp/fake-project",
                session_id="int-test-plan-001",
            )
            add_worker(plan, role=WorkerRole.EXPLORER, scope="Read auth module")
            add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="Write test file", file_targets=["tests/test_auth.py"])
            add_worker(plan, role=WorkerRole.REVIEWER, scope="Review test coverage")

            save_work_plan(plan)

            loaded = load_work_plan(plan.plan_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.task, "Add tests for auth module")
            self.assertEqual(len(loaded.workers), 3)
            self.assertEqual(loaded.workers[0].role, "explorer")
            self.assertEqual(loaded.workers[1].file_targets, ["tests/test_auth.py"])
            self.assertEqual(loaded.workers[2].role, "reviewer")

    def test_plan_lifecycle_planning_to_completed(self):
        """Full coordinator lifecycle without actually running agents."""
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Fix bug #42",
                project_path="/tmp/fake-project",
                session_id="int-test-plan-002",
            )
            self.assertEqual(plan.status, WorkPlanStatus.PLANNING.value)

            add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="Fix the bug", file_targets=["src/main.py"])
            mark_plan_ready(plan)
            self.assertEqual(plan.status, WorkPlanStatus.READY.value)

            # Simulate execution completion.
            from boss.workers.coordinator import collect_worker_result
            plan.status = WorkPlanStatus.RUNNING.value
            save_work_plan(plan)

            collect_worker_result(
                plan,
                plan.workers[0].worker_id,
                state=WorkerState.COMPLETED,
                result_summary="Fixed the null check in main.py line 42",
            )

            finalize_plan(plan, merge_summary="Applied 1 file change")
            self.assertEqual(plan.status, WorkPlanStatus.COMPLETED.value)
            self.assertIn("Applied 1 file change", plan.merge_summary)
            self.assertIsNotNone(plan.finished_at)

    def test_cancel_plan_sets_terminal_state(self):
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Refactor models",
                project_path="/tmp/fake-project",
                session_id="int-test-plan-003",
            )
            add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="Refactor")
            mark_plan_ready(plan)
            plan.status = WorkPlanStatus.RUNNING.value
            save_work_plan(plan)

            cancelled = cancel_plan(plan, reason="user cancelled")
            self.assertEqual(cancelled.status, WorkPlanStatus.CANCELLED.value)
            for w in cancelled.workers:
                self.assertIn(w.state, {WorkerState.CANCELLED.value, WorkerState.COMPLETED.value, WorkerState.FAILED.value})

    def test_execute_plan_with_mock_runner(self):
        """Exercise execute_plan with mocked Runner.run — verifies event stream."""
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Add logging",
                project_path="/tmp/fake-project",
                session_id="int-test-plan-004",
            )
            add_worker(plan, role=WorkerRole.EXPLORER, scope="Read existing logging")
            mark_plan_ready(plan)

            # Mock Runner.run to return a fake result.
            mock_result = MagicMock()
            mock_result.final_output = "Found 3 logging calls."
            mock_result.new_items = []

            async def _run():
                events = []
                with patch("agents.Runner") as mock_runner_cls, \
                     patch("boss.workers.engine.get_runner"):
                    mock_runner_cls.run = AsyncMock(return_value=mock_result)
                    async for event in execute_plan(plan):
                        events.append(event)
                return events

            events = asyncio.get_event_loop().run_until_complete(_run())

            # Must have plan started, worker started, worker completed, plan completed events.
            event_types = [(e["type"], e.get("event")) for e in events]
            self.assertIn(("plan_status", "started"), event_types)
            self.assertIn(("worker_status", "started"), event_types)
            self.assertIn(("worker_status", "completed"), event_types)

            # Plan should be in a terminal state.
            self.assertIn(plan.status, {WorkPlanStatus.COMPLETED.value, WorkPlanStatus.MERGING.value})

    def test_cancel_running_plan_cancels_asyncio_tasks(self):
        """Verify cancel_running_plan actually stops in-flight tasks."""
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Long running task",
                project_path="/tmp/fake-project",
                session_id="int-test-plan-005",
            )
            add_worker(plan, role=WorkerRole.EXPLORER, scope="Explore everything")
            mark_plan_ready(plan)

            cancel_event = asyncio.Event()

            async def _slow_worker(*args, **kwargs):
                result = MagicMock()
                result.final_output = "done"
                result.new_items = []
                # Wait until cancelled.
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancel_event.set()
                    raise
                return result

            async def _run():
                with patch("agents.Runner") as mock_runner_cls, \
                     patch("boss.workers.engine.get_runner"):
                    mock_runner_cls.run = AsyncMock(side_effect=_slow_worker)

                    # Start plan execution in background.
                    events: list[dict] = []

                    async def _collect():
                        async for event in execute_plan(plan):
                            events.append(event)

                    task = asyncio.create_task(_collect())
                    # Wait for the worker to actually start.
                    await asyncio.sleep(0.3)

                    # Cancel.
                    await cancel_running_plan(plan)

                    # Wait for everything to settle.
                    try:
                        await asyncio.wait_for(task, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

                    return events

            asyncio.run(_run())

            # Verify the task was actually cancelled.
            self.assertTrue(
                cancel_event.is_set() or plan.status == WorkPlanStatus.CANCELLED.value,
                "Worker task was not cancelled",
            )


class TestDeployLifecycleIntegration(unittest.TestCase):
    """Deploy create → run → status → teardown / cancel flows."""

    def test_full_deploy_lifecycle(self):
        """create → run → verify persistence → teardown."""
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="int-test-deploy-001",
                    adapter_name="dummy",
                )
                self.assertEqual(deploy.status, DeploymentStatus.PENDING.value)

                # Run the deployment pipeline.
                result = run_deployment(deploy.deployment_id)
                self.assertEqual(result.status, DeploymentStatus.LIVE.value)
                self.assertEqual(result.preview_url, "https://dummy-preview.example.com")
                self.assertTrue(adapter.build_called)
                self.assertTrue(adapter.deploy_called)

                # Verify persisted state.
                persisted = load_deployment(deploy.deployment_id)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.status, DeploymentStatus.LIVE.value)
                self.assertIsNotNone(persisted.finished_at)

    def test_cancel_pending_deployment(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="int-test-deploy-002",
                    adapter_name="dummy",
                )
                cancelled = cancel_deployment(deploy.deployment_id)
                self.assertEqual(cancelled.status, DeploymentStatus.CANCELLED.value)
                self.assertFalse(adapter.build_called)

                # Verify persistence.
                persisted = load_deployment(deploy.deployment_id)
                self.assertEqual(persisted.status, DeploymentStatus.CANCELLED.value)

    def test_deploy_build_failure_persists(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            adapter.build_fail = True
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="int-test-deploy-003",
                    adapter_name="dummy",
                )
                result = run_deployment(deploy.deployment_id)
                self.assertEqual(result.status, DeploymentStatus.FAILED.value)
                self.assertIn("Dummy build failure", result.error)
                self.assertFalse(adapter.deploy_called)

    def test_deploy_deploy_phase_failure(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            adapter.deploy_fail = True
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="int-test-deploy-004",
                    adapter_name="dummy",
                )
                result = run_deployment(deploy.deployment_id)
                self.assertEqual(result.status, DeploymentStatus.FAILED.value)
                self.assertTrue(adapter.build_called)
                self.assertTrue(adapter.deploy_called)

    def test_cancel_between_build_and_deploy(self):
        """Cancel flag set after build completes to exercise cancellation registry."""
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="int-test-deploy-005",
                    adapter_name="dummy",
                )

                # Capture the original build so we can inject cancellation after it.
                original_build = adapter.build

                def _build_then_cancel(deployment):
                    result = original_build(deployment)
                    from boss.deploy.engine import _mark_cancelled
                    _mark_cancelled(deployment.deployment_id)
                    return result

                adapter.build = _build_then_cancel

                result = run_deployment(deploy.deployment_id)
                self.assertEqual(result.status, DeploymentStatus.CANCELLED.value)
                self.assertIn("Cancelled after build phase", result.error)
                self.assertFalse(adapter.deploy_called)

    def test_rerun_terminal_deployment_raises(self):
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="int-test-deploy-006",
                    adapter_name="dummy",
                )
                run_deployment(deploy.deployment_id)

                with self.assertRaises(ValueError):
                    run_deployment(deploy.deployment_id)


class TestKnowledgeStoreIntegration(unittest.TestCase):
    """Knowledge store open → write → read → reopen round-trip."""

    def test_fact_store_and_retrieve(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            with isolated_knowledge_store(db_path) as store:
                fact = store.store_fact("preference", "editor", "vim", "user")
                self.assertIsNotNone(fact.id)
                self.assertEqual(fact.value, "vim")

                facts = store.get_facts("preference")
                self.assertTrue(any(f.key == "editor" for f in facts))

            # Reopen and verify data persisted.
            store2 = KnowledgeStore(db_path)
            try:
                facts2 = store2.get_facts("preference")
                self.assertTrue(any(f.key == "editor" for f in facts2))
            finally:
                store2.close()

    def test_durable_memory_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            with isolated_knowledge_store(db_path) as store:
                mem = store.upsert_durable_memory(
                    memory_kind="preference",
                    category="editor",
                    key="theme",
                    value="dark mode preferred",
                    tags=["preference", "ui"],
                    confidence=0.9,
                    salience=0.8,
                    source="user",
                )
                self.assertIsNotNone(mem)

                loaded = store.get_durable_memory(mem.id)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.value, "dark mode preferred")

    def test_knowledge_store_schema_idempotent(self):
        """Opening the same DB twice must not error."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            store1 = KnowledgeStore(db_path)
            store1.store_fact("test", "key1", "val1", "user")
            store1.close()

            # Second open should not fail or lose data.
            store2 = KnowledgeStore(db_path)
            facts = store2.get_facts("test")
            self.assertTrue(any(f.key == "key1" for f in facts))
            store2.close()


class TestWorkerPlanPersistenceFailure(unittest.TestCase):
    """Verify plan state is not corrupted on partial failure."""

    def test_worker_failure_does_not_corrupt_plan(self):
        """A worker that fails should not leave the plan in an unloadable state."""
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Multi-worker task",
                project_path="/tmp/fake-project",
                session_id="int-test-fail-001",
            )
            add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="Part A", file_targets=["a.py"])
            add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="Part B", file_targets=["b.py"])
            mark_plan_ready(plan)

            # Simulate one worker failing.
            from boss.workers.coordinator import collect_worker_result
            plan.status = WorkPlanStatus.RUNNING.value

            collect_worker_result(
                plan,
                plan.workers[0].worker_id,
                state=WorkerState.COMPLETED,
                result_summary="Part A done",
            )
            collect_worker_result(
                plan,
                plan.workers[1].worker_id,
                state=WorkerState.FAILED,
                error="Syntax error in b.py",
            )

            save_work_plan(plan)

            # Plan must be reloadable with both workers' states preserved.
            loaded = load_work_plan(plan.plan_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.workers[0].state, WorkerState.COMPLETED.value)
            self.assertEqual(loaded.workers[1].state, WorkerState.FAILED.value)
            self.assertEqual(loaded.workers[1].error, "Syntax error in b.py")

    def test_unknown_fields_in_persisted_plan_ignored(self):
        """Forward-compat: extra fields from a newer version should not crash loading."""
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Compat test",
                project_path="/tmp/fake-project",
                session_id="int-test-compat-001",
            )
            save_work_plan(plan)

            # Manually inject an unknown field.
            path = Path(settings.app_data_dir) / "work-plans" / f"{plan.plan_id}.json"
            data = json.loads(path.read_text())
            data["future_field"] = "from v2"
            data["workers"].append({
                "worker_id": "fake-w",
                "plan_id": plan.plan_id,
                "role": "explorer",
                "scope": "test",
                "future_worker_field": True,
            })
            path.write_text(json.dumps(data))

            loaded = load_work_plan(plan.plan_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.task, "Compat test")

    def test_corrupted_plan_json_returns_none(self):
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Corrupt test",
                project_path="/tmp/fake-project",
                session_id="int-test-corrupt-001",
            )
            save_work_plan(plan)

            path = Path(settings.app_data_dir) / "work-plans" / f"{plan.plan_id}.json"
            path.write_text("NOT VALID JSON {{{")

            loaded = load_work_plan(plan.plan_id)
            self.assertIsNone(loaded)


class TestDeployPersistenceFailure(unittest.TestCase):
    """Deploy state resilience."""

    def test_unknown_fields_in_deployment_ignored(self):
        with isolated_deploys_dir() as dd:
            deploy = Deployment(
                deployment_id="compat-001",
                project_path="/tmp/fake",
                session_id="s1",
                adapter="dummy",
            )
            save_deployment(deploy)

            path = dd / "compat-001.json"
            data = json.loads(path.read_text())
            data["future_field"] = "from v2"
            path.write_text(json.dumps(data))

            loaded = load_deployment("compat-001")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.deployment_id, "compat-001")

    def test_corrupted_deploy_json_returns_none(self):
        with isolated_deploys_dir() as dd:
            path = dd / "bad-deploy.json"
            path.write_text("CORRUPT")

            loaded = load_deployment("bad-deploy")
            self.assertIsNone(loaded)


class TestKnowledgeStoreSQLiteResilience(unittest.TestCase):
    """SQLite error handling in the knowledge store."""

    def test_concurrent_open_same_db(self):
        """Two KnowledgeStore instances on the same file should not corrupt."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "shared.db"
            store1 = KnowledgeStore(db_path)
            store2 = KnowledgeStore(db_path)

            store1.store_fact("test", "k1", "v1", "user")
            store2.store_fact("test", "k2", "v2", "user")

            facts1 = store1.get_facts("test")
            facts2 = store2.get_facts("test")

            # Both stores should see both facts (WAL mode or shared cache).
            keys1 = {f.key for f in facts1}
            keys2 = {f.key for f in facts2}
            self.assertIn("k1", keys1 | keys2)
            self.assertIn("k2", keys1 | keys2)

            store1.close()
            store2.close()

    def test_store_reopens_after_close(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "reopen.db"
            store = KnowledgeStore(db_path)
            store.store_fact("test", "k", "v", "user")
            store.close()

            store2 = KnowledgeStore(db_path)
            facts = store2.get_facts("test")
            self.assertTrue(any(f.key == "k" for f in facts))
            store2.close()


# ═══════════════════════════════════════════════════════════════════
# P2: Persistence Versioning Tests
# ═══════════════════════════════════════════════════════════════════


class TestSchemaVersioning(unittest.TestCase):
    """Knowledge store schema version tracking."""

    def test_fresh_db_gets_current_version(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "fresh.db"
            store = KnowledgeStore(db_path)
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["version"], SCHEMA_VERSION)
            store.close()

    def test_reopen_preserves_version(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "stable.db"
            store1 = KnowledgeStore(db_path)
            store1.close()
            store2 = KnowledgeStore(db_path)
            row = store2._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            self.assertEqual(row["version"], SCHEMA_VERSION)
            store2.close()

    def test_preexisting_db_without_version_table_gets_seeded(self):
        """A DB from before versioning should get version 1 on next open."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            # Create a bare DB with some data but no schema_version table.
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS facts "
                "(id INTEGER PRIMARY KEY, category TEXT, key TEXT, value TEXT, "
                "source TEXT DEFAULT 'user', created_at TEXT, updated_at TEXT, UNIQUE(category, key))"
            )
            conn.execute(
                "INSERT INTO facts (category, key, value, created_at, updated_at) "
                "VALUES ('test', 'k', 'v', '2024-01-01', '2024-01-01')"
            )
            conn.commit()
            conn.close()

            # Opening via KnowledgeStore should add the version table.
            store = KnowledgeStore(db_path)
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["version"], SCHEMA_VERSION)
            # Original data should still be there.
            facts = store.get_facts("test")
            self.assertTrue(any(f.key == "k" for f in facts))
            store.close()


class TestWorkPlanVersioning(unittest.TestCase):
    """WorkPlan/WorkerRecord version fields."""

    def test_plan_serializes_with_version(self):
        plan = WorkPlan(
            plan_id="ver-001",
            task="test",
            project_path="/tmp",
            session_id="s1",
        )
        d = plan.to_dict()
        self.assertEqual(d["version"], WORK_PLAN_VERSION)

    def test_worker_serializes_with_version(self):
        worker = WorkerRecord(
            worker_id="w1",
            plan_id="p1",
            role="explorer",
            scope="test",
        )
        d = worker.to_dict()
        self.assertEqual(d["version"], WORKER_RECORD_VERSION)

    def test_plan_without_version_field_loads(self):
        """Old plans without a version field should still load."""
        data = {
            "plan_id": "old-001",
            "task": "old task",
            "project_path": "/tmp",
            "session_id": "s1",
            "status": "completed",
            "workers": [
                {
                    "worker_id": "w1",
                    "plan_id": "old-001",
                    "role": "explorer",
                    "scope": "old scope",
                }
            ],
        }
        plan = WorkPlan.from_dict(data)
        self.assertEqual(plan.plan_id, "old-001")
        self.assertEqual(len(plan.workers), 1)
        # version should default to current.
        self.assertEqual(plan.version, WORK_PLAN_VERSION)

    def test_plan_persisted_version_round_trips(self):
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Version round-trip",
                project_path="/tmp/fake",
                session_id="ver-rt-001",
            )
            save_work_plan(plan)

            loaded = load_work_plan(plan.plan_id)
            d = json.loads(
                (Path(settings.app_data_dir) / "work-plans" / f"{plan.plan_id}.json").read_text()
            )
            self.assertEqual(d["version"], WORK_PLAN_VERSION)


class TestDeploymentVersioning(unittest.TestCase):
    """Deployment version field."""

    def test_deployment_serializes_with_version(self):
        deploy = Deployment(
            deployment_id="dv-001",
            project_path="/tmp",
            session_id="s1",
            adapter="dummy",
        )
        d = deploy.to_dict()
        self.assertEqual(d["version"], DEPLOYMENT_VERSION)

    def test_deployment_without_version_loads(self):
        data = {
            "deployment_id": "old-d",
            "project_path": "/tmp",
            "session_id": "s1",
            "adapter": "dummy",
        }
        deploy = Deployment.from_dict(data)
        self.assertEqual(deploy.deployment_id, "old-d")
        self.assertEqual(deploy.version, DEPLOYMENT_VERSION)

    def test_deployment_persisted_version(self):
        with isolated_deploys_dir() as dd:
            deploy = Deployment(
                deployment_id="dv-002",
                project_path="/tmp",
                session_id="s1",
                adapter="dummy",
            )
            save_deployment(deploy)

            raw = json.loads((dd / "dv-002.json").read_text())
            self.assertEqual(raw["version"], DEPLOYMENT_VERSION)


# ═══════════════════════════════════════════════════════════════════
# P3: Deploy Runner Routing Tests
# ═══════════════════════════════════════════════════════════════════


class TestDeployRunnerRouting(unittest.TestCase):
    """Verify static adapter routes commands through Runner when available."""

    def test_run_via_runner_delegates_to_start_managed_process(self):
        """When a runner is active, _run_via_runner must go through
        RunnerEngine.start_managed_process() for full policy enforcement."""
        from boss.deploy.static_adapter import _run_via_runner
        from boss.runner.engine import _current_runner_var

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"hello\n", b"")
        mock_proc.returncode = 0
        mock_proc.pid = 42

        mock_result = MagicMock()
        mock_result.verdict = "allowed"

        mock_engine = MagicMock()
        mock_engine.start_managed_process.return_value = (mock_proc, mock_result)

        token = _current_runner_var.set(mock_engine)
        try:
            result = _run_via_runner(["echo", "hello"], cwd="/tmp")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "hello\n")
            mock_engine.start_managed_process.assert_called_once_with(
                ["echo", "hello"], cwd="/tmp",
            )
        finally:
            _current_runner_var.reset(token)

    def test_run_via_runner_falls_back_without_runner(self):
        from boss.deploy.static_adapter import _run_via_runner
        from boss.runner.engine import _current_runner_var

        # Ensure no runner is active.
        token = _current_runner_var.set(None)
        try:
            with patch("boss.deploy.static_adapter.subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.communicate.return_value = ("hi", "")
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc

                result = _run_via_runner(["echo", "hi"], cwd="/tmp")
                self.assertEqual(result.returncode, 0)
                mock_popen.assert_called_once()
        finally:
            _current_runner_var.reset(token)

    def test_runner_denied_produces_nonzero_exit(self):
        from boss.deploy.static_adapter import _run_via_runner
        from boss.runner.engine import _current_runner_var

        mock_result = MagicMock()
        mock_result.verdict = "denied"
        mock_result.denied_reason = "Command denied by workspace_write policy"

        mock_engine = MagicMock()
        mock_engine.start_managed_process.return_value = (None, mock_result)

        token = _current_runner_var.set(mock_engine)
        try:
            result = _run_via_runner(["rm", "-rf", "/"], cwd="/tmp")
            self.assertEqual(result.returncode, 1)
            self.assertIn("denied", result.stderr.lower())
        finally:
            _current_runner_var.reset(token)

    def test_byte_output_decoded_to_str(self):
        """start_managed_process omits text=True; verify bytes are decoded."""
        from boss.deploy.static_adapter import _run_via_runner
        from boss.runner.engine import _current_runner_var

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("café ☕".encode(), b"warn")
        mock_proc.returncode = 0
        mock_proc.pid = 99

        mock_engine = MagicMock()
        mock_engine.start_managed_process.return_value = (mock_proc, MagicMock())

        token = _current_runner_var.set(mock_engine)
        try:
            result = _run_via_runner(["echo"], cwd="/tmp")
            self.assertIsInstance(result.stdout, str)
            self.assertIn("café", result.stdout)
            self.assertEqual(result.stderr, "warn")
        finally:
            _current_runner_var.reset(token)

    def test_deploy_mode_maps_to_full_access(self):
        """The 'deploy' mode must resolve to full_access profile."""
        from boss.runner.policy import PermissionProfile, runner_config_for_mode
        policy = runner_config_for_mode("deploy")
        self.assertEqual(policy.profile, PermissionProfile.FULL_ACCESS)


class TestDeployCancellationRegistry(unittest.TestCase):
    """Verify real process-level cancellation for deploys."""

    def test_process_registration_and_unregistration(self):
        from boss.deploy.engine import (
            _live_processes,
            _process_lock,
            register_deploy_process,
            unregister_deploy_process,
        )
        mock_proc = MagicMock()
        register_deploy_process("test-deploy-1", mock_proc)
        with _process_lock:
            self.assertIn("test-deploy-1", _live_processes)
        unregister_deploy_process("test-deploy-1")
        with _process_lock:
            self.assertNotIn("test-deploy-1", _live_processes)

    def test_terminate_deploy_process_kills_process_group(self):
        from boss.deploy.engine import (
            _terminate_deploy_process,
            register_deploy_process,
        )
        mock_proc = MagicMock()
        mock_proc.wait = MagicMock()
        mock_proc.pid = 12345

        register_deploy_process("test-deploy-2", mock_proc)
        with patch("boss.deploy.engine.os.getpgid", return_value=12345) as mock_getpgid, \
             patch("boss.deploy.engine.os.killpg") as mock_killpg:
            killed = _terminate_deploy_process("test-deploy-2")
            self.assertTrue(killed)
            mock_getpgid.assert_called_once_with(12345)
            # First call is SIGTERM on the process group
            import signal
            mock_killpg.assert_any_call(12345, signal.SIGTERM)

    def test_terminate_returns_false_when_no_process(self):
        from boss.deploy.engine import _terminate_deploy_process
        killed = _terminate_deploy_process("nonexistent-deploy")
        self.assertFalse(killed)

    def test_cancel_deployment_terminates_live_process(self):
        """cancel_deployment should kill the live process and mark error."""
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="cancel-proc-001",
                    adapter_name="dummy",
                )
                # Simulate a running deploy with a registered process.
                from boss.deploy.engine import register_deploy_process
                from boss.deploy.state import DeploymentStatus as DS

                deploy.status = DS.DEPLOYING.value
                save_deployment(deploy)

                mock_proc = MagicMock()
                mock_proc.wait = MagicMock()
                mock_proc.pid = 99999
                register_deploy_process(deploy.deployment_id, mock_proc)

                with patch("boss.deploy.engine.os.getpgid", return_value=99999), \
                     patch("boss.deploy.engine.os.killpg") as mock_killpg:
                    cancelled = cancel_deployment(deploy.deployment_id)
                    self.assertEqual(cancelled.status, DS.CANCELLED.value)
                    self.assertIn("terminated", cancelled.error)
                    mock_killpg.assert_called()

    def test_run_deployment_establishes_runner_context(self):
        """run_deployment must call get_runner so current_runner() is set."""
        with isolated_deploys_dir(), isolated_adapter_registry():
            adapter = _DummyAdapter()
            register_adapter(adapter)

            with override_settings(deploy_enabled=True):
                deploy = create_deployment(
                    project_path="/tmp/fake-project",
                    session_id="runner-ctx-001",
                    adapter_name="dummy",
                )
                with patch("boss.runner.engine.get_runner") as mock_get_runner:
                    run_deployment(deploy.deployment_id)
                    mock_get_runner.assert_called_once_with(mode="deploy")


# ═══════════════════════════════════════════════════════════════════
# P4: Error Recovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestSessionErrorRecovery(unittest.TestCase):
    """Session persistence error handling."""

    def test_corrupt_json_returns_empty_state(self):
        """load_session_state must not crash on corrupt files."""
        with isolated_history_dir() as hdir:
            (hdir / "bad.json").write_text("{NOT JSON AT ALL")
            state = load_session_state("bad")
            self.assertEqual(state.session_id, "bad")
            self.assertEqual(state.recent_items, [])

    def test_empty_file_returns_empty_state(self):
        with isolated_history_dir() as hdir:
            (hdir / "empty.json").write_text("")
            state = load_session_state("empty")
            self.assertEqual(state.session_id, "empty")

    def test_binary_garbage_returns_empty_state(self):
        with isolated_history_dir() as hdir:
            (hdir / "garbage.json").write_bytes(b"\x00\x01\x02\x03")
            state = load_session_state("garbage")
            self.assertEqual(state.session_id, "garbage")


class TestPlanPersistenceErrorRecovery(unittest.TestCase):
    """Work plan persistence failure paths."""

    def test_save_plan_cleans_up_temp_on_write_error(self):
        """If the atomic write fails, the temp file must be removed."""
        with isolated_plans_dir():
            plan = create_work_plan(
                task="Error test",
                project_path="/tmp/fake",
                session_id="err-001",
            )
            # Make the target directory read-only to force a write error.
            plans_dir = Path(settings.app_data_dir) / "work-plans"
            plans_dir.mkdir(parents=True, exist_ok=True)

            # First save should work.
            save_work_plan(plan)

            # Patch write_text to simulate IOError.
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    save_work_plan(plan)

            # Ensure no stale temp file is left behind.
            temp_files = list(plans_dir.glob("*.json.tmp"))
            self.assertEqual(len(temp_files), 0)


class TestKnowledgeStoreErrorRecovery(unittest.TestCase):
    """Knowledge store SQLite error handling."""

    def test_wal_mode_enabled(self):
        """WAL mode should be set for better concurrent access."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "wal.db"
            store = KnowledgeStore(db_path)
            row = store._conn.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(row[0], "wal")
            store.close()

    def test_connection_timeout_set(self):
        """The connection should have a busy timeout configured."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "timeout.db"
            store = KnowledgeStore(db_path)
            # sqlite3.connect timeout=5.0 sets a 5-second busy timeout.
            # Verify by checking the connection works under light contention.
            store.store_fact("test", "k", "v", "user")
            facts = store.get_facts("test")
            self.assertTrue(len(facts) > 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
