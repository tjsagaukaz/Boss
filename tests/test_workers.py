"""Tests for the boss.workers subsystem."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from boss.workers.roles import (
    ROLE_AGENT_MODE,
    ROLE_NEEDS_ISOLATION,
    ROLE_PERMISSION_PROFILES,
    WorkerRole,
)
from boss.workers.state import (
    WorkPlan,
    WorkPlanStatus,
    WorkerRecord,
    WorkerState,
    new_plan_id,
    new_worker_id,
)
from boss.workers.conflicts import (
    ConflictReport,
    FileConflict,
    detect_conflicts,
    detect_directory_overlap,
)
from boss.workers.coordinator import (
    add_worker,
    cancel_plan,
    collect_worker_result,
    create_work_plan,
    finalize_plan,
    mark_plan_ready,
    plan_summary,
    validate_plan,
    validate_plan_directory_overlap,
)


# ── Roles ───────────────────────────────────────────────────────────


class TestWorkerRole(unittest.TestCase):

    def test_role_values(self):
        self.assertEqual(WorkerRole.EXPLORER.value, "explorer")
        self.assertEqual(WorkerRole.IMPLEMENTER.value, "implementer")
        self.assertEqual(WorkerRole.REVIEWER.value, "reviewer")

    def test_permission_profiles_defined_for_all_roles(self):
        for role in WorkerRole:
            self.assertIn(role, ROLE_PERMISSION_PROFILES)

    def test_isolation_map(self):
        self.assertFalse(ROLE_NEEDS_ISOLATION[WorkerRole.EXPLORER])
        self.assertTrue(ROLE_NEEDS_ISOLATION[WorkerRole.IMPLEMENTER])
        self.assertFalse(ROLE_NEEDS_ISOLATION[WorkerRole.REVIEWER])

    def test_agent_mode_map(self):
        self.assertEqual(ROLE_AGENT_MODE[WorkerRole.EXPLORER], "ask")
        self.assertEqual(ROLE_AGENT_MODE[WorkerRole.IMPLEMENTER], "agent")
        self.assertEqual(ROLE_AGENT_MODE[WorkerRole.REVIEWER], "review")


# ── State ───────────────────────────────────────────────────────────


class TestWorkerState(unittest.TestCase):

    def test_state_values(self):
        self.assertEqual(WorkerState.PENDING.value, "pending")
        self.assertEqual(WorkerState.RUNNING.value, "running")
        self.assertEqual(WorkerState.COMPLETED.value, "completed")
        self.assertEqual(WorkerState.FAILED.value, "failed")
        self.assertEqual(WorkerState.CANCELLED.value, "cancelled")


class TestWorkPlanStatus(unittest.TestCase):

    def test_status_values(self):
        expected = {"planning", "ready", "running", "merging", "completed", "failed", "cancelled"}
        actual = {s.value for s in WorkPlanStatus}
        self.assertEqual(actual, expected)


class TestWorkerRecord(unittest.TestCase):

    def test_round_trip(self):
        rec = WorkerRecord(
            worker_id="abc123",
            plan_id="plan-1",
            role="explorer",
            scope="Read src/ directory",
            file_targets=["src/foo.py"],
        )
        d = rec.to_dict()
        rec2 = WorkerRecord.from_dict(d)
        self.assertEqual(rec2.worker_id, "abc123")
        self.assertEqual(rec2.role, "explorer")
        self.assertEqual(rec2.file_targets, ["src/foo.py"])
        self.assertEqual(rec2.state, "pending")

    def test_default_state_is_pending(self):
        rec = WorkerRecord(worker_id="x", plan_id="y", role="implementer", scope="test")
        self.assertEqual(rec.state, "pending")


class TestWorkPlan(unittest.TestCase):

    def test_round_trip(self):
        plan = WorkPlan(
            plan_id="plan-1",
            task="Fix all bugs",
            project_path="/tmp/proj",
            session_id="sess-1",
            workers=[
                WorkerRecord(
                    worker_id="w1",
                    plan_id="plan-1",
                    role="explorer",
                    scope="Read codebase",
                ),
                WorkerRecord(
                    worker_id="w2",
                    plan_id="plan-1",
                    role="implementer",
                    scope="Edit files",
                    file_targets=["src/main.py"],
                ),
            ],
        )
        d = plan.to_dict()
        plan2 = WorkPlan.from_dict(d)
        self.assertEqual(plan2.plan_id, "plan-1")
        self.assertEqual(len(plan2.workers), 2)
        self.assertEqual(plan2.workers[1].file_targets, ["src/main.py"])

    def test_default_status_is_planning(self):
        plan = WorkPlan(plan_id="p", task="t", project_path="/tmp", session_id="s")
        self.assertEqual(plan.status, "planning")


class TestIdGenerators(unittest.TestCase):

    def test_plan_id_is_hex(self):
        pid = new_plan_id()
        self.assertEqual(len(pid), 32)
        int(pid, 16)  # Should not raise

    def test_worker_id_is_short(self):
        wid = new_worker_id()
        self.assertEqual(len(wid), 12)


# ── Conflicts ───────────────────────────────────────────────────────


class TestConflictDetection(unittest.TestCase):

    def test_no_conflicts(self):
        w1 = WorkerRecord(
            worker_id="w1", plan_id="p", role="implementer", scope="A",
            file_targets=["src/a.py"],
        )
        w2 = WorkerRecord(
            worker_id="w2", plan_id="p", role="implementer", scope="B",
            file_targets=["src/b.py"],
        )
        report = detect_conflicts([w1, w2])
        self.assertFalse(report.has_conflicts)
        self.assertEqual(len(report.conflicts), 0)

    def test_detects_overlap(self):
        w1 = WorkerRecord(
            worker_id="w1", plan_id="p", role="implementer", scope="A",
            file_targets=["src/shared.py"],
        )
        w2 = WorkerRecord(
            worker_id="w2", plan_id="p", role="implementer", scope="B",
            file_targets=["src/shared.py"],
        )
        report = detect_conflicts([w1, w2])
        self.assertTrue(report.has_conflicts)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(set(report.conflicts[0].worker_ids), {"w1", "w2"})

    def test_ignores_non_implementers(self):
        w1 = WorkerRecord(
            worker_id="w1", plan_id="p", role="explorer", scope="A",
            file_targets=["src/shared.py"],
        )
        w2 = WorkerRecord(
            worker_id="w2", plan_id="p", role="implementer", scope="B",
            file_targets=["src/shared.py"],
        )
        report = detect_conflicts([w1, w2])
        self.assertFalse(report.has_conflicts)

    def test_summary_text(self):
        report = ConflictReport(has_conflicts=False)
        self.assertIn("No file-target conflicts", report.summary())

        report2 = ConflictReport(
            has_conflicts=True,
            conflicts=[FileConflict(path="src/x.py", worker_ids=["w1", "w2"])],
        )
        self.assertIn("src/x.py", report2.summary())

    @patch("boss.workers.conflicts._CASE_INSENSITIVE_PATHS", True)
    def test_detects_case_insensitive_overlap(self):
        w1 = WorkerRecord(
            worker_id="w1", plan_id="p", role="implementer", scope="A",
            file_targets=["src/Foo.swift"],
        )
        w2 = WorkerRecord(
            worker_id="w2", plan_id="p", role="implementer", scope="B",
            file_targets=["src/foo.swift"],
        )
        report = detect_conflicts([w1, w2])
        self.assertTrue(report.has_conflicts)


class TestDirectoryOverlap(unittest.TestCase):

    def test_detects_same_directory(self):
        w1 = WorkerRecord(
            worker_id="w1", plan_id="p", role="implementer", scope="A",
            file_targets=["src/foo/bar.py"],
        )
        w2 = WorkerRecord(
            worker_id="w2", plan_id="p", role="implementer", scope="B",
            file_targets=["src/foo/baz.py"],
        )
        report = detect_directory_overlap([w1, w2])
        self.assertTrue(report.has_conflicts)

    def test_no_overlap_different_dirs(self):
        w1 = WorkerRecord(
            worker_id="w1", plan_id="p", role="implementer", scope="A",
            file_targets=["src/a/file.py"],
        )
        w2 = WorkerRecord(
            worker_id="w2", plan_id="p", role="implementer", scope="B",
            file_targets=["src/b/file.py"],
        )
        report = detect_directory_overlap([w1, w2])
        self.assertFalse(report.has_conflicts)


# ── Coordinator ─────────────────────────────────────────────────────


class TestCoordinator(unittest.TestCase):

    def _temp_plans_dir(self):
        return tempfile.mkdtemp()

    @patch("boss.workers.state._plans_dir")
    def test_create_plan(self, mock_dir):
        d = Path(self._temp_plans_dir())
        mock_dir.return_value = d
        plan = create_work_plan(
            task="Build feature X",
            project_path="/tmp/proj",
            session_id="sess-1",
        )
        self.assertEqual(plan.task, "Build feature X")
        self.assertEqual(plan.status, "planning")
        self.assertEqual(len(plan.workers), 0)
        # Persisted to disk.
        files = list(d.glob("*.json"))
        self.assertEqual(len(files), 1)

    @patch("boss.workers.state._plans_dir")
    def test_add_worker(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        worker = add_worker(plan, role=WorkerRole.EXPLORER, scope="Read tests/")
        self.assertEqual(worker.role, "explorer")
        self.assertEqual(worker.scope, "Read tests/")
        self.assertEqual(len(plan.workers), 1)

    @patch("boss.workers.state._plans_dir")
    def test_add_worker_to_running_plan_raises(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        plan.status = WorkPlanStatus.RUNNING.value
        with self.assertRaises(ValueError):
            add_worker(plan, role=WorkerRole.EXPLORER, scope="any")

    @patch("boss.workers.state._plans_dir")
    def test_validate_no_conflicts(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["a.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["b.py"])
        report = validate_plan(plan)
        self.assertFalse(report.has_conflicts)

    @patch("boss.workers.state._plans_dir")
    def test_validate_with_conflicts(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["shared.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["shared.py"])
        report = validate_plan(plan)
        self.assertTrue(report.has_conflicts)

    @patch("boss.workers.state._plans_dir")
    def test_mark_ready_blocks_on_conflict(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["x.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["x.py"])
        report = mark_plan_ready(plan)
        self.assertTrue(report.has_conflicts)
        self.assertNotEqual(plan.status, "ready")

    @patch("boss.workers.state._plans_dir")
    def test_mark_ready_force(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["x.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["x.py"])
        report = mark_plan_ready(plan, force=True)
        self.assertTrue(report.has_conflicts)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.merge_strategy, "manual")

    @patch("boss.workers.state._plans_dir")
    def test_mark_ready_clean(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["a.py"])
        add_worker(plan, role=WorkerRole.EXPLORER, scope="B")
        report = mark_plan_ready(plan)
        self.assertFalse(report.has_conflicts)
        self.assertEqual(plan.status, "ready")

    @patch("boss.workers.state._plans_dir")
    def test_collect_result(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        w = add_worker(plan, role=WorkerRole.EXPLORER, scope="Explore")
        plan.status = WorkPlanStatus.RUNNING.value
        result = collect_worker_result(
            plan, w.worker_id, state=WorkerState.COMPLETED, result_summary="Found 3 files."
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.result_summary, "Found 3 files.")

    @patch("boss.workers.state._plans_dir")
    def test_plan_transitions_to_merging(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        w = add_worker(plan, role=WorkerRole.EXPLORER, scope="Explore")
        plan.status = WorkPlanStatus.RUNNING.value
        collect_worker_result(plan, w.worker_id, state=WorkerState.COMPLETED)
        self.assertEqual(plan.status, "merging")

    @patch("boss.workers.state._plans_dir")
    def test_finalize_plan(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        plan.status = WorkPlanStatus.MERGING.value
        finalize_plan(plan, merge_summary="All good.")
        self.assertEqual(plan.status, "completed")
        self.assertEqual(plan.merge_summary, "All good.")
        self.assertIsNotNone(plan.finished_at)

    @patch("boss.workers.state._plans_dir")
    def test_cancel_plan(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        w1 = add_worker(plan, role=WorkerRole.EXPLORER, scope="E")
        w2 = add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="I")
        plan.status = WorkPlanStatus.RUNNING.value
        cancel_plan(plan)
        self.assertEqual(plan.status, "cancelled")
        self.assertEqual(w1.state, "cancelled")
        self.assertEqual(w2.state, "cancelled")

    @patch("boss.workers.state._plans_dir")
    def test_plan_summary(self, mock_dir):
        mock_dir.return_value = Path(self._temp_plans_dir())
        plan = create_work_plan(task="Fix bugs", project_path="/tmp", session_id="s")
        w = add_worker(plan, role=WorkerRole.EXPLORER, scope="Explore")
        plan.status = WorkPlanStatus.RUNNING.value
        collect_worker_result(plan, w.worker_id, state=WorkerState.COMPLETED, result_summary="Found 3 issues.")
        summary = plan_summary(plan)
        self.assertEqual(summary["worker_count"], 1)
        self.assertEqual(len(summary["completed_summaries"]), 1)


# ── Persistence ─────────────────────────────────────────────────────


class TestPersistence(unittest.TestCase):

    @patch("boss.workers.state._plans_dir")
    def test_save_and_load(self, mock_dir):
        d = Path(tempfile.mkdtemp())
        mock_dir.return_value = d
        from boss.workers.state import save_work_plan, load_work_plan

        plan = WorkPlan(
            plan_id="test-persist",
            task="Persist test",
            project_path="/tmp",
            session_id="s",
        )
        plan.workers.append(WorkerRecord(
            worker_id="w1", plan_id=plan.plan_id, role="implementer",
            scope="Edit", file_targets=["f.py"],
        ))
        save_work_plan(plan)
        loaded = load_work_plan("test-persist")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task, "Persist test")
        self.assertEqual(len(loaded.workers), 1)
        self.assertEqual(loaded.workers[0].file_targets, ["f.py"])

    @patch("boss.workers.state._plans_dir")
    def test_load_missing(self, mock_dir):
        mock_dir.return_value = Path(tempfile.mkdtemp())
        from boss.workers.state import load_work_plan
        self.assertIsNone(load_work_plan("nonexistent"))

    @patch("boss.workers.state._plans_dir")
    def test_list_plans(self, mock_dir):
        d = Path(tempfile.mkdtemp())
        mock_dir.return_value = d
        from boss.workers.state import save_work_plan, list_work_plans

        for i in range(3):
            p = WorkPlan(plan_id=f"p{i}", task=f"Task {i}", project_path="/tmp", session_id="s")
            save_work_plan(p)
            time.sleep(0.01)  # ensure mtime ordering

        plans = list_work_plans(limit=10)
        self.assertEqual(len(plans), 3)
        # Most recent first.
        self.assertEqual(plans[0].plan_id, "p2")


# ── Isolation ───────────────────────────────────────────────────────


class TestIsolation(unittest.TestCase):

    def test_explorer_no_workspace(self):
        from boss.workers.isolation import provision_workspace
        rec = WorkerRecord(worker_id="w1", plan_id="p", role="explorer", scope="Read")
        result = provision_workspace(rec, "/tmp/proj")
        self.assertIsNone(result)
        self.assertIsNone(rec.workspace_id)

    def test_reviewer_no_workspace(self):
        from boss.workers.isolation import provision_workspace
        rec = WorkerRecord(worker_id="w1", plan_id="p", role="reviewer", scope="Review")
        result = provision_workspace(rec, "/tmp/proj")
        self.assertIsNone(result)

    @patch("boss.workers.isolation.create_task_workspace")
    @patch("boss.workers.isolation.update_task_workspace")
    def test_implementer_gets_workspace(self, mock_update, mock_create):
        from boss.workers.isolation import provision_workspace

        mock_ws = MagicMock()
        mock_ws.workspace_id = "ws-123"
        mock_ws.workspace_path = "/tmp/ws-123"
        mock_create.return_value = mock_ws

        rec = WorkerRecord(worker_id="w1", plan_id="p", role="implementer", scope="Edit")
        result = provision_workspace(rec, "/tmp/proj")
        self.assertIsNotNone(result)
        self.assertEqual(rec.workspace_id, "ws-123")
        self.assertEqual(rec.workspace_path, "/tmp/ws-123")
        mock_create.assert_called_once()

    @patch("boss.workers.isolation.cleanup_task_workspace")
    def test_release_workspace(self, mock_cleanup):
        from boss.workers.isolation import release_workspace
        rec = WorkerRecord(worker_id="w1", plan_id="p", role="implementer", scope="Edit")
        rec.workspace_id = "ws-456"
        release_workspace(rec)
        mock_cleanup.assert_called_once_with("ws-456")

    @patch("boss.workers.isolation.cleanup_task_workspace")
    def test_release_no_workspace(self, mock_cleanup):
        from boss.workers.isolation import release_workspace
        rec = WorkerRecord(worker_id="w1", plan_id="p", role="explorer", scope="Read")
        release_workspace(rec)
        mock_cleanup.assert_not_called()


# ── Engine (unit-level) ─────────────────────────────────────────────


class TestEngineHelpers(unittest.TestCase):

    def test_worker_event(self):
        from boss.workers.engine import _worker_event
        rec = WorkerRecord(worker_id="w1", plan_id="p1", role="explorer", scope="Read")
        rec.state = "running"
        evt = _worker_event("p1", rec, "started")
        self.assertEqual(evt["type"], "worker_status")
        self.assertEqual(evt["plan_id"], "p1")
        self.assertEqual(evt["worker_id"], "w1")
        self.assertEqual(evt["event"], "started")

    def test_plan_event(self):
        from boss.workers.engine import _plan_event
        plan = WorkPlan(plan_id="p1", task="T", project_path="/tmp", session_id="s")
        evt = _plan_event(plan, "started")
        self.assertEqual(evt["type"], "plan_status")
        self.assertEqual(evt["event"], "started")
        self.assertEqual(evt["worker_count"], 0)

    def test_build_worker_prompt_explorer(self):
        from boss.workers.engine import _build_worker_prompt
        rec = WorkerRecord(worker_id="w1", plan_id="p1", role="explorer", scope="Read src/")
        prompt = _build_worker_prompt("Fix layout bug", rec)
        self.assertIn("explorer", prompt)
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("Fix layout bug", prompt)
        self.assertIn("Read src/", prompt)

    def test_build_worker_prompt_implementer(self):
        from boss.workers.engine import _build_worker_prompt
        rec = WorkerRecord(
            worker_id="w1", plan_id="p1", role="implementer", scope="Edit views",
            file_targets=["src/views.py"],
        )
        prompt = _build_worker_prompt("Fix layout", rec)
        self.assertIn("implementer", prompt)
        self.assertIn("src/views.py", prompt)

    def test_build_worker_prompt_reviewer(self):
        from boss.workers.engine import _build_worker_prompt
        rec = WorkerRecord(worker_id="w1", plan_id="p1", role="reviewer", scope="Review changes")
        prompt = _build_worker_prompt("Fix layout", rec)
        self.assertIn("reviewer", prompt)
        self.assertIn("Do NOT edit", prompt)

    def test_build_merge_summary(self):
        from boss.workers.engine import _build_merge_summary
        plan = WorkPlan(plan_id="p1", task="Do stuff", project_path="/tmp", session_id="s")
        plan.workers = [
            WorkerRecord(
                worker_id="w1", plan_id="p1", role="explorer", scope="Read",
                state="completed", result_summary="Found 5 files.",
            ),
            WorkerRecord(
                worker_id="w2", plan_id="p1", role="implementer", scope="Edit",
                state="failed", error="Timeout",
            ),
        ]
        summary = _build_merge_summary(plan)
        self.assertIn("Found 5 files", summary)
        self.assertIn("Timeout", summary)
        self.assertIn("Failed workers", summary)


# ── Finding regressions ─────────────────────────────────────────────


class TestImplementerOutputMerge(unittest.TestCase):
    """Regression: implementer diffs must be collected and applied."""

    def test_collect_workspace_changes_returns_dataclass(self):
        from boss.workers.isolation import WorkspaceChanges
        changes = WorkspaceChanges(worker_id="w1", diff_text="--- a\n+++ b\n", files_changed=["b.py"])
        self.assertEqual(changes.worker_id, "w1")
        self.assertEqual(changes.files_changed, ["b.py"])

    def test_extract_changed_files_from_diff(self):
        from boss.workers.isolation import _extract_changed_files
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        self.assertEqual(_extract_changed_files(diff), ["foo.py"])

    def test_extract_changed_files_multiple(self):
        from boss.workers.isolation import _extract_changed_files
        diff = "+++ b/alpha.py\n+++ b/beta.py\n+++ /dev/null\n"
        self.assertEqual(_extract_changed_files(diff), ["alpha.py", "beta.py"])

    @patch("boss.workers.isolation.subprocess.run")
    @patch("boss.workers.isolation.load_task_workspace")
    def test_apply_workspace_changes_calls_git_apply(self, mock_load, mock_run):
        from boss.workers.isolation import WorkspaceChanges, apply_workspace_changes
        mock_run.return_value = MagicMock(returncode=0)

        changes = WorkspaceChanges(
            worker_id="w1",
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
            files_changed=["foo.py"],
        )
        result = apply_workspace_changes(changes, "/tmp/project")
        self.assertTrue(result)
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        self.assertIn("git", call_args[0][0])
        self.assertIn("apply", call_args[0][0])

    @patch("boss.workers.isolation.subprocess.run")
    @patch("boss.workers.isolation.load_task_workspace")
    def test_apply_workspace_changes_returns_false_on_failure(self, mock_load, mock_run):
        from boss.workers.isolation import WorkspaceChanges, apply_workspace_changes
        mock_run.return_value = MagicMock(returncode=1, stderr="patch failed")

        changes = WorkspaceChanges(
            worker_id="w1", diff_text="bad diff", files_changed=["foo.py"],
        )
        result = apply_workspace_changes(changes, "/tmp/project")
        self.assertFalse(result)

    def test_apply_empty_diff_is_noop(self):
        from boss.workers.isolation import WorkspaceChanges, apply_workspace_changes
        changes = WorkspaceChanges(worker_id="w1", diff_text="  ", files_changed=[])
        result = apply_workspace_changes(changes, "/tmp/project")
        self.assertTrue(result)

    def test_collect_returns_none_without_workspace(self):
        from boss.workers.isolation import collect_workspace_changes
        worker = WorkerRecord(worker_id="w1", plan_id="p1", role="implementer", scope="A")
        self.assertIsNone(collect_workspace_changes(worker, "/tmp"))

    def test_apply_file_diff_syncs_temp_workspace_changes(self):
        from boss.runner.workspace import cleanup_task_workspace, create_task_workspace
        from boss.workers.isolation import apply_workspace_changes, collect_workspace_changes

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source"
            source.mkdir()
            (source / "a.txt").write_text("one\n", encoding="utf-8")

            workspace = create_task_workspace(source_path=source, task_slug="worker-merge")
            worker = WorkerRecord(worker_id="w1", plan_id="p1", role="implementer", scope="Edit")
            worker.workspace_id = workspace.workspace_id
            worker.workspace_path = workspace.workspace_path

            (Path(workspace.workspace_path) / "a.txt").write_text("one\ntwo\n", encoding="utf-8")

            changes = collect_workspace_changes(worker, source)
            self.assertIsNotNone(changes)
            assert changes is not None
            self.assertEqual(changes.strategy, "file_diff")
            self.assertEqual(changes.files_changed, ["a.txt"])
            self.assertTrue(apply_workspace_changes(changes, source))
            self.assertEqual((source / "a.txt").read_text(encoding="utf-8"), "one\ntwo\n")

            cleanup_task_workspace(workspace.workspace_id)


class TestCancellationStopsWorkers(unittest.TestCase):
    """Regression: cancel must stop live asyncio tasks."""

    def test_running_tasks_registry_exists(self):
        from boss.workers.engine import _running_tasks
        self.assertIsInstance(_running_tasks, dict)

    def test_cancel_running_plan_cancels_tasks(self):
        from boss.workers.engine import _running_tasks, cancel_running_plan

        plan = WorkPlan(plan_id="p-cancel", task="T", project_path="/tmp", session_id="s")
        plan.status = WorkPlanStatus.RUNNING.value
        w = WorkerRecord(worker_id="w-c1", plan_id="p-cancel", role="explorer", scope="E", state="running")
        plan.workers.append(w)

        # Create a mock task.
        mock_task = MagicMock(spec=asyncio.Task)
        _running_tasks["p-cancel"] = {"w-c1": mock_task}

        with patch("boss.workers.state._plans_dir", return_value=Path(tempfile.mkdtemp())):
            result = asyncio.run(cancel_running_plan(plan))

        mock_task.cancel.assert_called_once()
        self.assertEqual(result.status, "cancelled")
        # Tasks dict should be cleaned up.
        self.assertNotIn("p-cancel", _running_tasks)

    @patch("boss.workers.state._plans_dir")
    def test_maybe_finish_plan_does_not_override_cancelled(self, mock_dir):
        """_maybe_finish_plan must not transition cancelled plans to merging."""
        mock_dir.return_value = Path(tempfile.mkdtemp())
        from boss.workers.coordinator import _maybe_finish_plan

        plan = WorkPlan(plan_id="p-guard", task="T", project_path="/tmp", session_id="s")
        plan.status = WorkPlanStatus.CANCELLED.value
        w = WorkerRecord(
            worker_id="w1", plan_id="p-guard", role="explorer", scope="E",
            state=WorkerState.COMPLETED.value,
        )
        plan.workers.append(w)

        _maybe_finish_plan(plan)
        self.assertEqual(plan.status, "cancelled")

    def test_cancelled_implementer_plan_cleans_up_workspace(self):
        from boss.workers.engine import _worker_event, cancel_running_plan, execute_plan

        async def _scenario() -> tuple[str | None, bool, str]:
            async def fake_run_single_worker(plan, worker, event_queue):
                worker.state = WorkerState.RUNNING.value
                await event_queue.put(_worker_event(plan.plan_id, worker, "started"))
                await asyncio.sleep(5)

            with tempfile.TemporaryDirectory() as td:
                plans_dir = Path(td) / "plans"
                plans_dir.mkdir()
                project = Path(td) / "project"
                project.mkdir()
                (project / "a.txt").write_text("x", encoding="utf-8")

                with patch("boss.workers.state._plans_dir", return_value=plans_dir), patch(
                    "boss.workers.engine._run_single_worker", side_effect=fake_run_single_worker
                ):
                    plan = create_work_plan(task="T", project_path=str(project), session_id="s")
                    add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="Edit", file_targets=["a.txt"])
                    plan.status = WorkPlanStatus.READY.value

                    stream = execute_plan(plan)
                    await stream.__anext__()
                    await stream.__anext__()

                    workspace_path = plan.workers[0].workspace_path
                    await cancel_running_plan(plan)
                    async for _ in stream:
                        pass
                    exists_after = Path(workspace_path).exists() if workspace_path else False
                    return workspace_path, exists_after, plan.status

        workspace_path, exists_after, status = asyncio.run(_scenario())
        self.assertIsNotNone(workspace_path)
        self.assertFalse(exists_after)
        self.assertEqual(status, WorkPlanStatus.CANCELLED.value)


class TestReviewerUsesReviewMode(unittest.TestCase):
    """Regression: reviewer workers must use review mode, not plan."""

    def test_reviewer_agent_mode_is_review(self):
        self.assertEqual(ROLE_AGENT_MODE[WorkerRole.REVIEWER], "review")

    def test_reviewer_mode_not_plan(self):
        self.assertNotEqual(ROLE_AGENT_MODE[WorkerRole.REVIEWER], "plan")


class TestDirectoryOverlapEnforced(unittest.TestCase):
    """Regression: mark_plan_ready must block on directory overlap too."""

    @patch("boss.workers.state._plans_dir")
    def test_mark_ready_blocks_directory_overlap(self, mock_dir):
        mock_dir.return_value = Path(tempfile.mkdtemp())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["src/foo/a.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["src/foo/b.py"])
        report = mark_plan_ready(plan)
        # Directory overlap: both target src/foo/ — must block without force.
        self.assertTrue(report.has_conflicts)
        self.assertNotEqual(plan.status, "ready")

    @patch("boss.workers.state._plans_dir")
    def test_mark_ready_allows_directory_overlap_with_force(self, mock_dir):
        mock_dir.return_value = Path(tempfile.mkdtemp())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["src/foo/a.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["src/foo/b.py"])
        report = mark_plan_ready(plan, force=True)
        self.assertTrue(report.has_conflicts)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.merge_strategy, "manual")

    @patch("boss.workers.state._plans_dir")
    def test_mark_ready_clean_different_directories(self, mock_dir):
        mock_dir.return_value = Path(tempfile.mkdtemp())
        plan = create_work_plan(task="T", project_path="/tmp", session_id="s")
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="A", file_targets=["src/alpha/a.py"])
        add_worker(plan, role=WorkerRole.IMPLEMENTER, scope="B", file_targets=["src/beta/b.py"])
        report = mark_plan_ready(plan)
        self.assertFalse(report.has_conflicts)
        self.assertEqual(plan.status, "ready")


# ── Config ──────────────────────────────────────────────────────────


class TestConfig(unittest.TestCase):

    def test_max_concurrent_workers_default(self):
        from boss.config import settings
        self.assertEqual(settings.max_concurrent_workers, 3)


if __name__ == "__main__":
    unittest.main()
