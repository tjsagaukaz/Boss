from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from boss.agents import build_entry_agent
from boss.config import settings
from boss.control import (
    boss_control_status_payload,
    is_path_allowed_for_agent,
    jobs_branch_behavior,
    load_boss_control,
    memory_auto_approve_enabled,
    memory_auto_approve_min_confidence,
    resolve_request_mode,
)
from boss.context.manager import SessionContextManager
from boss.memory.distillation import distill_latest_turn
from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, get_tool_metadata
from boss.execution import (
    PendingApproval,
    PendingStatus,
    load_expired_pending_run,
    load_pending_run,
    save_pending_run,
)
from boss.memory import knowledge as knowledge_module
from boss.memory.injection import build_memory_injection
from boss.memory.knowledge import KnowledgeStore
from boss.jobs import (
    BackgroundJobStatus,
    append_background_job_log,
    create_background_job,
    list_background_jobs,
    load_background_job,
    prepare_task_branch,
    recover_interrupted_background_jobs,
    tail_background_job_log,
    update_background_job,
)
from boss.review import (
    ReviewFinding,
    ReviewReport,
    ReviewRequest,
    collect_review_material,
    list_review_history,
    load_review_record,
    normalize_review_record,
    review_capabilities,
    save_review_record,
)
from boss.runtime import git_status_payload, runtime_status_payload, workspace_root
from boss.memory.scanner import full_scan
from boss.persistence.history import SessionState, save_session_state


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
def isolated_knowledge_store(db_path: Path):
    original_store = knowledge_module._store
    store = KnowledgeStore(db_path)
    knowledge_module._store = store
    try:
        yield store
    finally:
        store.close()
        knowledge_module._store = original_store


def import_api_module():
    existing = sys.modules.get("boss.api")
    if existing is not None:
        return existing
    with patch("boss.runtime.ensure_api_server_lock", return_value={"pid": os.getpid()}):
        return importlib.import_module("boss.api")


class RegressionHarnessTests(unittest.TestCase):
    def test_default_agent_mode_keeps_full_governed_behavior(self):
        entry_agent = build_entry_agent()

        tool_names = [tool.name for tool in entry_agent.tools]
        self.assertIn("remember", tool_names)
        self.assertIn("web_search", tool_names)

    def test_ask_mode_filters_side_effect_tools(self):
        entry_agent = build_entry_agent(mode="ask")

        self.assertNotIn("remember", [tool.name for tool in entry_agent.tools])
        self.assertIn("recall", [tool.name for tool in entry_agent.tools])

        for agent in [entry_agent, *entry_agent.handoffs]:
            for tool in agent.tools:
                metadata = get_tool_metadata(tool.name)
                self.assertIsNotNone(metadata)
                self.assertIn(metadata.execution_type, AUTO_ALLOWED_EXECUTION_TYPES)

    def test_plan_mode_is_read_only_and_plan_oriented(self):
        entry_agent = build_entry_agent(mode="plan")

        self.assertIn("Goal, Execution Plan, Risks, Validation", entry_agent.instructions)
        self.assertNotIn("remember", [tool.name for tool in entry_agent.tools])
        self.assertNotIn("web_search", [tool.name for tool in entry_agent.tools])

    def test_review_mode_is_read_only_and_findings_first(self):
        entry_agent = build_entry_agent(mode="review")

        self.assertIn("Do not fix code", entry_agent.instructions)
        self.assertNotIn("remember", [tool.name for tool in entry_agent.tools])

        # Primary boss agent in review mode should have review instructions
        self.assertIn("review", entry_agent.instructions.lower())
        for tool in entry_agent.tools:
            metadata = get_tool_metadata(tool.name)
            self.assertIsNotNone(metadata)
            self.assertIn(metadata.execution_type, AUTO_ALLOWED_EXECUTION_TYPES)

    def test_mode_resolution_defaults_to_agent_for_invalid_explicit_mode(self):
        self.assertEqual(resolve_request_mode("hello", explicit_mode="something-unknown"), "agent")
        self.assertEqual(resolve_request_mode("hello", explicit_mode="agent"), "agent")

    def test_explicit_mode_override_beats_review_keyword_auto_switch(self):
        self.assertEqual(resolve_request_mode("please review this diff", explicit_mode="plan"), "plan")

    def test_boss_control_default_mode_applies_without_keyword_or_explicit_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".boss").mkdir()
            (root / ".boss" / "rules").mkdir()
            (root / ".boss" / "config.toml").write_text(
                "[mode]\ndefault = \"ask\"\n",
                encoding="utf-8",
            )

            self.assertEqual(resolve_request_mode("hello there", workspace_root=root), "ask")

    def test_boss_control_loader_reads_repo_native_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "BOSS.md").write_text("Top-level Boss instructions", encoding="utf-8")
            (root / ".boss").mkdir()
            (root / ".boss" / "rules").mkdir()
            (root / ".boss" / "config.toml").write_text(
                "[mode]\ndefault = \"review\"\n\n[memory]\nauto_injection = true\n",
                encoding="utf-8",
            )
            (root / ".boss" / "review.md").write_text("Findings first review guide", encoding="utf-8")
            (root / ".boss" / "environment.json").write_text(
                '{"platform": "macOS", "constraints": ["local validation only"]}',
                encoding="utf-8",
            )
            (root / ".boss" / "rules" / "00-core.md").write_text(
                "+++\ntitle = \"Core\"\ntargets = [\"all\"]\nmodes = [\"default\", \"review\"]\nalways = true\n+++\n\nAlways be additive.",
                encoding="utf-8",
            )
            (root / ".bossignore").write_text("secret.txt\n", encoding="utf-8")
            (root / ".bossindexignore").write_text("ignored.py\n", encoding="utf-8")

            control = load_boss_control(root, refresh=True)
            self.assertTrue(control.is_configured())
            self.assertEqual(control.config.default_mode, "review")
            self.assertEqual(control.rules[0].title, "Core")
            self.assertIn("Top-level Boss instructions", control.boss_md)
            self.assertEqual(control.environment.get("platform"), "macOS")

            status = boss_control_status_payload(root)
            self.assertTrue(status["configured"])
            self.assertEqual(status["default_mode"], "review")
            self.assertTrue(status["files"]["BOSS.md"]["exists"])

    def test_review_mode_and_instructions_use_boss_control(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "BOSS.md").write_text("Use Boss-native review behavior.", encoding="utf-8")
            (root / ".boss").mkdir()
            (root / ".boss" / "rules").mkdir()
            (root / ".boss" / "config.toml").write_text(
                "[review]\nauto_activate = true\nauto_mode_keywords = [\"audit\"]\n",
                encoding="utf-8",
            )
            (root / ".boss" / "review.md").write_text("List findings before summaries.", encoding="utf-8")
            (root / ".boss" / "rules" / "00-core.md").write_text(
                "+++\ntitle = \"Core\"\ntargets = [\"all\"]\nmodes = [\"default\", \"review\"]\nalways = true\n+++\n\nAlways keep changes incremental.",
                encoding="utf-8",
            )
            (root / ".boss" / "rules" / "30-review-mode.md").write_text(
                "+++\ntitle = \"Review Mode\"\ntargets = [\"general\"]\nmodes = [\"review\"]\n+++\n\nStart with findings.",
                encoding="utf-8",
            )

            mode = resolve_request_mode("please audit this codebase", workspace_root=root)
            self.assertEqual(mode, "review")

            agent = build_entry_agent(mode=mode, workspace_root=root)
            self.assertIn("Use Boss-native review behavior.", agent.instructions)
            self.assertIn("List findings before summaries.", agent.instructions)
            self.assertIn("Start with findings.", agent.instructions)

    def test_review_findings_are_normalized_and_sorted_by_severity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file_path = root / "sample.py"
            file_path.write_text("def compute():\n    return 1\n", encoding="utf-8")

            material = collect_review_material(
                ReviewRequest(target="files", project_path=str(root), file_paths=(str(file_path),))
            )
            report = ReviewReport(
                findings=[
                    ReviewFinding(
                        severity="low",
                        file_path="sample.py",
                        evidence="Return value is hard-coded.",
                        risk="Low risk if callers already tolerate it.",
                        recommended_fix="Make the value configurable.",
                    ),
                    ReviewFinding(
                        severity="high",
                        file_path="sample.py",
                        evidence="No guard exists for invalid input.",
                        risk="High risk of runtime failure on bad input.",
                        recommended_fix="Validate arguments before use.",
                    ),
                ]
            )

            normalized = normalize_review_record(report, material)
            self.assertEqual([finding.severity for finding in normalized.findings], ["high", "low"])
            self.assertEqual(normalized.findings[0].file_path, "sample.py")
            self.assertTrue(normalized.summary)

    def test_review_git_working_tree_flow_prefers_diff_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tracked = root / "module.py"
            tracked.write_text("def compute():\n    return 1\n", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Boss Tests"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "boss@example.com"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "add", "module.py"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)

            tracked.write_text("def compute(flag):\n    if flag:\n        return 2\n    return 1\n", encoding="utf-8")

            capabilities = review_capabilities(str(root))
            material = collect_review_material(ReviewRequest(target="working_tree", project_path=str(root)))

            self.assertTrue(capabilities["git_available"])
            self.assertEqual(material.target_kind, "working_tree")
            self.assertIn("module.py", material.changed_files)
            self.assertIn("return 2", material.diff_text)

    def test_review_non_git_auto_falls_back_to_project_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "knowledge.sqlite"
            with isolated_knowledge_store(db_path):
                store = knowledge_module.get_knowledge_store()
                store.upsert_project(
                    path=str(root),
                    name="review-fixture",
                    project_type="python",
                    metadata={
                        "stack": ["Python"],
                        "entry_points": ["main.py"],
                        "useful_commands": ["python main.py"],
                    },
                )

                capabilities = review_capabilities(str(root))
                material = collect_review_material(ReviewRequest(target="auto", project_path=str(root)))

                self.assertFalse(capabilities["git_available"])
                self.assertEqual(capabilities["default_target"], "project_summary")
                self.assertEqual(material.target_kind, "project_summary")
                self.assertTrue(material.project_summaries)

    def test_review_history_round_trip_persists_normalized_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            review_dir = root / "reviews"
            file_path = root / "module.py"
            file_path.write_text("def run():\n    return 1\n", encoding="utf-8")

            material = collect_review_material(
                ReviewRequest(target="files", project_path=str(root), file_paths=(str(file_path),))
            )
            report = ReviewReport(
                summary="One issue found.",
                residual_risk="No additional residual risk.",
                findings=[
                    ReviewFinding(
                        severity="medium",
                        file_path="module.py",
                        evidence="The return value is hard-coded.",
                        risk="Medium risk if callers expect a configurable result.",
                        recommended_fix="Read the value from configuration.",
                    )
                ],
            )
            record = normalize_review_record(report, material)

            with override_settings(review_history_dir=review_dir):
                save_review_record(record)
                loaded = load_review_record(record.review_id)
                history = list_review_history(limit=5)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.review_id, record.review_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].findings[0].file_path, "module.py")

    def test_memory_governance_config_reads_auto_approve_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".boss").mkdir()
            (root / ".boss" / "rules").mkdir()
            (root / ".boss" / "config.toml").write_text(
                "[memory]\nauto_approve = true\nauto_approve_min_confidence = 0.91\n",
                encoding="utf-8",
            )

            self.assertTrue(memory_auto_approve_enabled(root))
            self.assertAlmostEqual(memory_auto_approve_min_confidence(root), 0.91)

    def test_jobs_config_reads_branch_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".boss").mkdir()
            (root / ".boss" / "rules").mkdir()
            (root / ".boss" / "config.toml").write_text(
                "[jobs]\nbranch_behavior = \"create\"\ntakeover_cancels_background = false\n",
                encoding="utf-8",
            )

            self.assertEqual(jobs_branch_behavior(root), "create")

    def test_background_job_round_trip_and_log_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_dir = root / "jobs"
            logs_dir = root / "job-logs"

            with override_settings(jobs_dir=jobs_dir, job_logs_dir=logs_dir):
                record = create_background_job(
                    prompt="Inspect the latest local changes",
                    mode="agent",
                    session_id="session-job-1",
                    project_path=str(root),
                    initial_input_kind="prepared_input",
                    initial_input_payload=[{"role": "user", "content": "Inspect the latest local changes"}],
                    branch_mode="suggest",
                    branch_name="boss/inspect-latest-local-changes",
                    task_slug="inspect-latest-local-changes",
                    branch_status="suggested",
                    branch_message="Suggested task branch: boss/inspect-latest-local-changes",
                    branch_helper_path=str(root / "scripts" / "task_branch.sh"),
                )
                append_background_job_log(
                    record.job_id,
                    event_type="text",
                    message="Reviewing the changed files.",
                )

                loaded = load_background_job(record.job_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.status, BackgroundJobStatus.QUEUED.value)

                listed = list_background_jobs(limit=10)
                self.assertEqual(len(listed), 1)
                tail = tail_background_job_log(record.job_id, limit=10)
                self.assertIn("Reviewing the changed files.", tail["text"])
                self.assertEqual(tail["entries"][-1]["type"], "text")

    def test_recover_interrupted_background_jobs_marks_running_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_dir = root / "jobs"
            logs_dir = root / "job-logs"

            with override_settings(jobs_dir=jobs_dir, job_logs_dir=logs_dir):
                record = create_background_job(
                    prompt="Long running task",
                    mode="agent",
                    session_id="session-job-2",
                    project_path=str(root),
                    initial_input_kind="prepared_input",
                    initial_input_payload=[{"role": "user", "content": "Long running task"}],
                )
                update_background_job(record.job_id, status=BackgroundJobStatus.RUNNING.value)

                recovered = recover_interrupted_background_jobs()
                self.assertEqual(recovered, 1)

                loaded = load_background_job(record.job_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.status, BackgroundJobStatus.INTERRUPTED.value)
                self.assertIn("Resume to continue", loaded.error_message or "")

    def test_prepare_task_branch_suggests_branch_for_git_repo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Boss Tests"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "boss@example.com"], cwd=root, check=True, capture_output=True)
            (root / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)

            branch = prepare_task_branch(
                prompt="Refactor the background job detail view",
                project_path=str(root),
                branch_mode="suggest",
            )

            self.assertEqual(branch["branch_status"], "suggested")
            self.assertEqual(branch["branch_name"], "boss/refactor-background-job-detail-view")

    def test_background_job_updates_preserve_resume_and_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_dir = root / "jobs"
            logs_dir = root / "job-logs"

            with override_settings(jobs_dir=jobs_dir, job_logs_dir=logs_dir):
                record = create_background_job(
                    prompt="Finish the checklist",
                    mode="agent",
                    session_id="session-job-3",
                    project_path=str(root),
                    initial_input_kind="prepared_input",
                    initial_input_payload=[{"role": "user", "content": "Finish the checklist"}],
                )
                waiting = update_background_job(
                    record.job_id,
                    status=BackgroundJobStatus.WAITING_PERMISSION.value,
                    pending_run_id="pending-job-3",
                    resume_count=1,
                )
                completed = update_background_job(
                    record.job_id,
                    status=BackgroundJobStatus.COMPLETED.value,
                    pending_run_id=None,
                    session_persisted=True,
                    finished_at="2026-04-08T00:00:00+00:00",
                )

                self.assertEqual(waiting.resume_count, 1)
                self.assertEqual(completed.status, BackgroundJobStatus.COMPLETED.value)
                self.assertTrue(completed.session_persisted)

    def test_takeover_clears_pending_run_for_waiting_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_dir = root / "jobs"
            logs_dir = root / "job-logs"
            pending_dir = root / "pending-runs"
            history_dir = root / "history"

            with override_settings(
                jobs_dir=jobs_dir,
                job_logs_dir=logs_dir,
                pending_runs_dir=pending_dir,
                history_dir=history_dir,
            ):
                for cancels_background in (True, False):
                    session_id = f"session-job-takeover-{int(cancels_background)}"
                    job = create_background_job(
                        prompt="Wait for approval",
                        mode="agent",
                        session_id=session_id,
                        project_path=str(root),
                        initial_input_kind="prepared_input",
                        initial_input_payload=[{"role": "user", "content": "Wait for approval"}],
                    )
                    approval = PendingApproval(
                        approval_id=f"approval-{job.job_id}",
                        tool_name="run_command",
                        title="Run command",
                        description="Needs approval",
                        execution_type="run",
                        scope_key=f"command:{job.job_id}",
                        scope_label="Terminal command",
                        requested_at=1_700_000_000.0,
                    )
                    run_id = save_pending_run(
                        session_id=session_id,
                        state={"job_id": job.job_id},
                        approvals=[approval],
                        run_id=f"run-{job.job_id}",
                    )
                    save_session_state(SessionState(session_id=session_id, recent_items=[], total_turns=0))
                    update_background_job(
                        job.job_id,
                        status=BackgroundJobStatus.WAITING_PERMISSION.value,
                        pending_run_id=run_id,
                    )

                    with self.subTest(cancels_background=cancels_background):
                        api = import_api_module()
                        if cancels_background:
                            payload = asyncio.run(api.takeover_job_endpoint(job.job_id))
                        else:
                            with patch("boss.api.jobs_takeover_cancels_background", return_value=False):
                                payload = asyncio.run(api.takeover_job_endpoint(job.job_id))

                        current = load_background_job(job.job_id)
                        self.assertIsNotNone(current)
                        assert current is not None
                        self.assertEqual(current.status, BackgroundJobStatus.TAKEN_OVER.value)
                        self.assertIsNone(current.pending_run_id)
                        self.assertEqual(payload["job"]["status"], BackgroundJobStatus.TAKEN_OVER.value)
                        self.assertIsNone(payload["job"]["pending_run_id"])
                        self.assertIsNone(load_pending_run(run_id))
                        self.assertFalse((pending_dir / f"{run_id}.json").exists())

    def test_git_status_payload_summarizes_clean_repo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tracked = root / "README.md"
            tracked.write_text("hello\n", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Boss Tests"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "boss@example.com"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)

            git = git_status_payload(root)
            self.assertTrue(git["available"])
            self.assertTrue(git["is_repo"])
            self.assertTrue(git["clean"])
            self.assertIn("clean", git["summary"])

    def test_runtime_status_payload_reports_git_and_clean_lock_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_file = Path(temp_dir) / "api.lock"
            payload = {
                "pid": os.getpid(),
                "port": settings.api_port,
                "started_at": 1_700_000_000.0,
                "ready_at": 1_700_000_010.0,
                "status": "running",
                "workspace_path": str(workspace_root()),
                "current_working_directory": str(workspace_root()),
                "interpreter_path": sys.executable,
                "app_version": "test",
                "build_marker": "test-build",
            }
            lock_file.write_text(json.dumps(payload), encoding="utf-8")

            with override_settings(api_lock_file=lock_file), \
                 patch("boss.runtime._local_port_is_in_use", return_value=True), \
                 patch("boss.runtime._listeners_for_local_port", return_value=[os.getpid()]), \
                 patch("boss.runtime._process_snapshot", return_value={
                     "cwd": str(workspace_root()),
                     "executable": sys.executable,
                     "command": sys.executable,
                 }):
                status = runtime_status_payload()

            self.assertIn("git", status)
            self.assertIn("summary", status["git"])
            self.assertIn("boss_control", status)
            self.assertEqual(status["runtime_trust"]["warnings"], [])

    def test_bossignore_blocks_agent_access_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "BOSS.md").write_text("Boss", encoding="utf-8")
            (root / ".bossignore").write_text("secret.txt\n", encoding="utf-8")
            secret = root / "secret.txt"
            public = root / "notes.txt"
            secret.write_text("do not expose", encoding="utf-8")
            public.write_text("okay to read", encoding="utf-8")

            self.assertFalse(is_path_allowed_for_agent(secret))
            self.assertTrue(is_path_allowed_for_agent(public))

    def test_entry_agent_uses_boss_entrypoint_and_mac_handoff(self):
        entry_agent = build_entry_agent(active_mcp_servers={})

        self.assertEqual(entry_agent.name, "boss")
        self.assertEqual([agent.name for agent in entry_agent.handoffs], ["mac"])

        tool_names = [tool.name for tool in entry_agent.tools]
        self.assertIn("remember", tool_names)
        self.assertIn("recall", tool_names)
        self.assertIn("search_project_content", tool_names)
        # Action tools present in agent mode
        self.assertIn("write_file", tool_names)
        self.assertIn("edit_file", tool_names)
        self.assertIn("run_shell", tool_names)

    def test_memory_round_trip_and_injection_relevance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "knowledge.sqlite"
            with isolated_knowledge_store(db_path), override_settings(auto_memory_enabled=True):
                store = knowledge_module.get_knowledge_store()
                preference = store.upsert_durable_memory(
                    memory_kind="preference",
                    category="preference",
                    key="response_style",
                    value="Prefer concise technical answers with no fluff.",
                    tags=["style", "response"],
                    source="test",
                )
                store.upsert_durable_memory(
                    memory_kind="user_profile",
                    category="user",
                    key="editor",
                    value="VS Code on macOS",
                    tags=["editor"],
                    source="test",
                )

                listed = store.list_durable_memories(memory_kind="preference")
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0].key, "response_style")

                injection = build_memory_injection(
                    user_message="Keep the reply concise and technical for this change.",
                )
                self.assertTrue(any(result.key == "response_style" for result in injection.results))
                self.assertIn("Prefer concise technical answers", injection.text)

                self.assertTrue(store.delete_durable_memory(preference.id))

                after_delete = build_memory_injection(
                    user_message="Keep the reply concise and technical for this change.",
                )
                self.assertFalse(any(result.key == "response_style" for result in after_delete.results))

    def test_pending_memory_candidate_is_session_scoped_until_approved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "knowledge.sqlite"
            with isolated_knowledge_store(db_path), override_settings(auto_memory_enabled=True):
                store = knowledge_module.get_knowledge_store()
                candidate = store.queue_memory_candidate(
                    session_id="session-a",
                    memory_kind="preference",
                    category="preference",
                    key="reply_style",
                    value="Prefer terse daily summaries.",
                    source="test",
                )

                same_session = build_memory_injection(
                    user_message="Give me a terse daily summary.",
                    session_id="session-a",
                )
                other_session = build_memory_injection(
                    user_message="Give me a terse daily summary.",
                    session_id="session-b",
                )

                self.assertTrue(any(result.source_table == "memory_candidates" for result in same_session.results))
                self.assertFalse(any(result.source_table == "memory_candidates" for result in other_session.results))

                store.approve_memory_candidate(candidate.id)
                approved = build_memory_injection(
                    user_message="Give me a terse daily summary.",
                    session_id="session-b",
                )
                self.assertTrue(any(result.source_table == "durable_memories" for result in approved.results))

    def test_auto_distilled_memory_requires_approval_for_cross_session_use(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "knowledge.sqlite"
            with isolated_knowledge_store(db_path), override_settings(auto_memory_enabled=True):
                store = knowledge_module.get_knowledge_store()
                distill_latest_turn(
                    session_id="session-a",
                    session_summary="",
                    recent_items=[
                        {"role": "user", "type": "message", "content": "I prefer concise technical answers."},
                        {"role": "assistant", "type": "message", "content": "Understood."},
                    ],
                )

                pending = store.list_memory_candidates(status="pending")
                self.assertEqual(len(pending), 1)
                self.assertEqual(store.list_durable_memories(memory_kind="preference"), [])

                same_session = build_memory_injection(
                    user_message="Keep the response concise and technical.",
                    session_id="session-a",
                )
                self.assertTrue(any(result.source_table == "memory_candidates" for result in same_session.results))

                other_session = build_memory_injection(
                    user_message="Keep the response concise and technical.",
                    session_id="session-b",
                )
                self.assertFalse(any(result.key == "response_style" for result in other_session.results))

                approved = store.approve_memory_candidate(pending[0].id)
                self.assertEqual(approved.memory_kind, "preference")

                cross_session = build_memory_injection(
                    user_message="Keep the response concise and technical.",
                )
                self.assertTrue(any(result.source_table == "durable_memories" for result in cross_session.results))

    def test_full_scan_generates_project_summary_and_entry_points(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "sample_app"
            package_dir = project / "sample_app"
            package_dir.mkdir(parents=True)
            (project / "pyproject.toml").write_text(
                "[project]\nname = \"sample-app\"\ndescription = \"Scanner fixture\"\n",
                encoding="utf-8",
            )
            (project / "main.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n",
                encoding="utf-8",
            )
            (package_dir / "__init__.py").write_text("", encoding="utf-8")
            (package_dir / "api.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n",
                encoding="utf-8",
            )

            db_path = root / "knowledge.sqlite"
            with isolated_knowledge_store(db_path), override_settings(
                project_scan_roots=(root,),
                project_scan_discovery_depth=2,
                project_scan_max_files_per_project=100,
                project_scan_summary_file_limit=40,
            ):
                store = knowledge_module.get_knowledge_store()
                result = full_scan(store=store)

                self.assertEqual(result["projects_found"], 1)
                self.assertEqual(result["projects_updated"], 1)
                self.assertEqual(result["summaries_refreshed"], 1)
                self.assertGreaterEqual(result["files_indexed"], 3)

                projects = store.list_projects()
                self.assertEqual(len(projects), 1)
                metadata = projects[0].metadata
                self.assertIn("Python", metadata.get("stack", []))
                self.assertIn("FastAPI", metadata.get("stack", []))
                self.assertIn("main.py", metadata.get("entry_points", []))

                summaries = store.list_project_summary_notes(limit=5)
                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0].note_key, "overview")
                self.assertIn("Likely entry points", summaries[0].body)

    def test_full_scan_respects_bossindexignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "sample_app"
            package_dir = project / "sample_app"
            package_dir.mkdir(parents=True)
            (project / "pyproject.toml").write_text(
                "[project]\nname = \"sample-app\"\ndescription = \"Scanner fixture\"\n",
                encoding="utf-8",
            )
            (project / ".bossindexignore").write_text("ignored.py\n", encoding="utf-8")
            (project / "main.py").write_text("print('main')\n", encoding="utf-8")
            (project / "ignored.py").write_text("print('ignore me')\n", encoding="utf-8")
            (package_dir / "__init__.py").write_text("", encoding="utf-8")

            db_path = root / "knowledge.sqlite"
            with isolated_knowledge_store(db_path), override_settings(
                project_scan_roots=(root,),
                project_scan_discovery_depth=2,
                project_scan_max_files_per_project=100,
                project_scan_summary_file_limit=40,
            ):
                store = knowledge_module.get_knowledge_store()
                result = full_scan(store=store)

                self.assertEqual(result["projects_found"], 1)
                projects = store.list_projects()
                self.assertEqual(len(projects), 1)
                metadata = projects[0].metadata
                self.assertIn("boss_control", metadata)

                indexed_paths = store.get_project_file_index(projects[0].id)
                self.assertIn(str(project / "main.py"), indexed_paths)
                self.assertNotIn(str(project / "ignored.py"), indexed_paths)

    def test_pending_run_round_trip_and_expiry_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_dir = Path(temp_dir) / "pending_runs"
            requested_at = 1_700_000_000.0
            approval = PendingApproval(
                approval_id="approval-1",
                tool_name="run_applescript",
                title="Run AppleScript",
                description="Run a scripted action",
                execution_type="run",
                scope_key="applescript:any:test",
                scope_label="AppleScript",
                requested_at=requested_at,
            )

            with override_settings(pending_runs_dir=pending_dir):
                with patch("boss.execution.time.time", return_value=requested_at):
                    run_id = save_pending_run(
                        session_id="session-1",
                        state={"step": "waiting"},
                        approvals=[approval],
                        run_id="run-1",
                    )

                with patch("boss.execution.time.time", return_value=requested_at):
                    record = load_pending_run(run_id)
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(record.status, PendingStatus.PENDING.value)
                self.assertEqual(len(record.approvals), 1)

                expired_at = requested_at + settings.pending_run_expiration_seconds + 5
                with patch("boss.execution.time.time", return_value=expired_at):
                    self.assertIsNone(load_pending_run(run_id))

                archived = load_expired_pending_run(run_id)
                self.assertIsNotNone(archived)
                assert archived is not None
                self.assertEqual(archived.status, PendingStatus.EXPIRED.value)
                self.assertEqual(archived.approvals[0].status, PendingStatus.EXPIRED.value)

    def test_preview_memory_injection_does_not_modify_session_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history_dir = root / "history"
            db_path = root / "knowledge.sqlite"
            session_id = "preview-read-only"
            recent_items = [
                {"role": "developer", "type": "message", "content": "BOSS_CONTEXT:memory\nignore me"},
                {"role": "user", "type": "message", "content": "Turn one request"},
                {"role": "assistant", "type": "message", "content": "Turn one answer"},
                {"role": "user", "type": "message", "content": "Turn two request"},
                {"role": "assistant", "type": "message", "content": "Turn two answer"},
                {"role": "user", "type": "message", "content": "Turn three request"},
                {"role": "assistant", "type": "message", "content": "Turn three answer"},
            ]

            with isolated_knowledge_store(db_path), override_settings(
                history_dir=history_dir,
                session_summary_threshold=2,
                session_max_recent_turns=2,
                session_max_serialized_size=512,
                auto_memory_enabled=False,
            ):
                save_session_state(
                    SessionState(
                        session_id=session_id,
                        recent_items=recent_items,
                        total_turns=3,
                    )
                )
                session_path = history_dir / f"{session_id}.json"
                before = session_path.read_text(encoding="utf-8")

                manager = SessionContextManager()
                manager.preview_memory_injection(session_id, "Preview this memory context")

                after = session_path.read_text(encoding="utf-8")
                self.assertEqual(after, before)

    def test_preview_memory_injection_does_not_mutate_conversation_episodes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history_dir = root / "history"
            db_path = root / "knowledge.sqlite"
            unchanged_timestamp = "2026-01-01T00:00:00+00:00"

            with isolated_knowledge_store(db_path), override_settings(
                history_dir=history_dir,
                auto_memory_enabled=False,
            ):
                store = knowledge_module.get_knowledge_store()
                store.store_conversation_episode(
                    session_id="preview-delete-guard",
                    title="Keep existing episode",
                    summary="Existing summary should survive preview.",
                    source="test",
                    created_at=unchanged_timestamp,
                    updated_at=unchanged_timestamp,
                    last_used_at=unchanged_timestamp,
                )
                store.store_conversation_episode(
                    session_id="preview-update-guard",
                    title="Existing summary",
                    summary="Episode should not be updated during preview.",
                    source="test",
                    created_at=unchanged_timestamp,
                    updated_at=unchanged_timestamp,
                    last_used_at=unchanged_timestamp,
                )

                save_session_state(
                    SessionState(
                        session_id="preview-delete-guard",
                        summary="",
                        recent_items=[{"role": "user", "type": "message", "content": "No summary here"}],
                        total_turns=1,
                    )
                )
                save_session_state(
                    SessionState(
                        session_id="preview-update-guard",
                        summary="New summary from session file",
                        recent_items=[{"role": "user", "type": "message", "content": "Keep this read-only"}],
                        total_turns=1,
                    )
                )
                save_session_state(
                    SessionState(
                        session_id="preview-create-guard",
                        summary="Would have created an episode before this fix",
                        recent_items=[{"role": "user", "type": "message", "content": "Do not create episode"}],
                        total_turns=1,
                    )
                )

                manager = SessionContextManager()
                manager.preview_memory_injection("preview-delete-guard", "Preview only")
                manager.preview_memory_injection("preview-update-guard", "Preview only")
                manager.preview_memory_injection("preview-create-guard", "Preview only")

                episodes = {episode.session_id: episode for episode in store.list_conversation_episodes(limit=10)}
                self.assertEqual(len(episodes), 2)
                self.assertIn("preview-delete-guard", episodes)
                self.assertEqual(
                    episodes["preview-delete-guard"].summary,
                    "Existing summary should survive preview.",
                )
                self.assertIn("preview-update-guard", episodes)
                self.assertEqual(
                    episodes["preview-update-guard"].summary,
                    "Episode should not be updated during preview.",
                )
                self.assertEqual(episodes["preview-update-guard"].updated_at, unchanged_timestamp)
                self.assertNotIn("preview-create-guard", episodes)

    def test_memory_overview_preview_does_not_touch_last_used_at(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history_dir = root / "history"
            db_path = root / "knowledge.sqlite"
            unchanged_timestamp = "2026-01-01T00:00:00+00:00"

            with isolated_knowledge_store(db_path), override_settings(
                history_dir=history_dir,
                auto_memory_enabled=True,
            ):
                store = knowledge_module.get_knowledge_store()
                durable = store.upsert_durable_memory(
                    memory_kind="workflow",
                    category="workflow",
                    key="response_style",
                    value="Prefer concise technical answers.",
                    tags=["style"],
                    source="test",
                    created_at=unchanged_timestamp,
                    updated_at=unchanged_timestamp,
                    last_used_at=unchanged_timestamp,
                )
                candidate = store.queue_memory_candidate(
                    session_id="session-a",
                    memory_kind="preference",
                    category="preference",
                    key="summary_style",
                    value="Prefer terse daily summaries.",
                    source="test",
                )
                store._conn.execute(
                    "UPDATE memory_candidates SET last_used_at = ?, updated_at = ? WHERE id = ?",
                    (unchanged_timestamp, unchanged_timestamp, candidate.id),
                )
                store._conn.commit()

                save_session_state(
                    SessionState(
                        session_id="session-a",
                        recent_items=[{"role": "user", "type": "message", "content": "Preview message"}],
                        total_turns=1,
                    )
                )

                api = import_api_module()
                session_payload = api._memory_overview_payload(
                    session_id="session-a",
                    message="Use concise technical answers and terse daily summaries.",
                )
                stateless_payload = api._memory_overview_payload(
                    message="Use concise technical answers.",
                )

                self.assertGreaterEqual(len(session_payload["current_turn_memory"]["reasons"]), 1)
                self.assertGreaterEqual(len(stateless_payload["current_turn_memory"]["reasons"]), 1)

                refreshed_durable = store.get_durable_memory(durable.id)
                refreshed_candidate = store.get_memory_candidate(candidate.id)
                self.assertIsNotNone(refreshed_durable)
                self.assertIsNotNone(refreshed_candidate)
                assert refreshed_durable is not None
                assert refreshed_candidate is not None
                self.assertEqual(refreshed_durable.last_used_at, unchanged_timestamp)
                self.assertEqual(refreshed_candidate.last_used_at, unchanged_timestamp)

    def test_persist_result_still_compacts_and_syncs_episode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history_dir = root / "history"
            db_path = root / "knowledge.sqlite"
            session_id = "session-compaction"
            run_history = [
                {"role": "developer", "type": "message", "content": "BOSS_CONTEXT:memory\nignore me"},
                {"role": "user", "type": "message", "content": "Turn one request"},
                {"role": "assistant", "type": "message", "content": "Turn one answer"},
                {"role": "user", "type": "message", "content": "Turn two request"},
                {"role": "assistant", "type": "message", "content": "Turn two answer"},
                {"role": "user", "type": "message", "content": "Turn three request"},
                {"role": "assistant", "type": "message", "content": "Turn three answer"},
                {"role": "user", "type": "message", "content": "Turn four request"},
                {"role": "assistant", "type": "message", "content": "Turn four answer"},
            ]

            with isolated_knowledge_store(db_path), override_settings(
                history_dir=history_dir,
                session_summary_threshold=2,
                session_max_recent_turns=2,
                session_max_serialized_size=512,
                auto_memory_enabled=False,
            ):
                manager = SessionContextManager()
                compacted = manager.persist_result(session_id, run_history)

                self.assertEqual(compacted.archived_turns, 2)
                self.assertEqual(sum(1 for item in compacted.recent_items if item.get("role") == "user"), 2)
                self.assertFalse(any(item.get("role") == "developer" for item in compacted.recent_items))
                self.assertIn("Turn one request", compacted.summary)
                self.assertTrue((history_dir / f"{session_id}.json").exists())

                store = knowledge_module.get_knowledge_store()
                episodes = store.list_conversation_episodes(limit=5)
                self.assertEqual(len(episodes), 1)
                self.assertEqual(episodes[0].session_id, session_id)
                self.assertEqual(episodes[0].summary, compacted.summary)

    # ---- Runner subsystem tests ----

    def test_runner_denied_command_prefixes_block_execution(self):
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )
        from boss.runner.engine import RunnerEngine

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=("git", "python"),
            prompt_prefixes=("curl",),
            denied_prefixes=("sudo", "rm -rf /"),
            allow_shell=True,
            env_scrub_keys=(),
        )

        self.assertEqual(policy.check_command("sudo apt install foo"), CommandVerdict.DENIED)
        self.assertEqual(policy.check_command("rm -rf /etc"), CommandVerdict.DENIED)
        self.assertEqual(policy.check_command(["sudo", "reboot"]), CommandVerdict.DENIED)
        self.assertEqual(policy.check_command("git status"), CommandVerdict.ALLOWED)
        self.assertEqual(policy.check_command("python test.py"), CommandVerdict.ALLOWED)
        self.assertEqual(policy.check_command("curl https://example.com"), CommandVerdict.PROMPT)
        self.assertEqual(policy.check_command("npm install"), CommandVerdict.PROMPT)

        # Verify engine refuses denied commands
        engine = RunnerEngine(policy)
        result = engine.run_command(["sudo", "rm", "-rf", "/"])
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.verdict, CommandVerdict.DENIED.value)
        self.assertIn("denied", result.denied_reason.lower())

    def test_runner_read_only_profile_denies_all_shell(self):
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.READ_ONLY,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=False,
            env_scrub_keys=(),
        )

        self.assertEqual(policy.check_command("echo hello"), CommandVerdict.DENIED)
        self.assertEqual(policy.check_command("git status"), CommandVerdict.DENIED)
        self.assertEqual(policy.check_command("cat file.txt"), CommandVerdict.DENIED)

    def test_runner_write_outside_allowed_roots_denied(self):
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            outside = Path(temp_dir) / "outside"
            outside.mkdir()

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(workspace,), workspace_root=workspace),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=(),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )

            self.assertEqual(policy.check_write(workspace / "file.txt"), CommandVerdict.ALLOWED)
            self.assertEqual(policy.check_write(workspace / "sub" / "file.txt"), CommandVerdict.ALLOWED)
            self.assertEqual(policy.check_write(outside / "file.txt"), CommandVerdict.PROMPT)
            self.assertEqual(
                ExecutionPolicy(
                    profile=PermissionProfile.READ_ONLY,
                    path_policy=PathPolicy(writable_roots=()),
                    network=NetworkPolicy.DISABLED,
                    domain_allowlist=(),
                    allowed_prefixes=(),
                    prompt_prefixes=(),
                    denied_prefixes=(),
                    allow_shell=False,
                    env_scrub_keys=(),
                ).check_write(workspace / "file.txt"),
                CommandVerdict.DENIED,
            )

    def test_runner_network_disabled_denies_access(self):
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        disabled_policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        self.assertEqual(disabled_policy.check_network("api.openai.com"), CommandVerdict.DENIED)
        self.assertEqual(disabled_policy.check_network(), CommandVerdict.DENIED)

        enabled_policy = ExecutionPolicy(
            profile=PermissionProfile.FULL_ACCESS,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.ENABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        self.assertEqual(enabled_policy.check_network("example.com"), CommandVerdict.ALLOWED)

        allowlist_policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.ALLOWLIST,
            domain_allowlist=("api.openai.com", "github.com"),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        self.assertEqual(allowlist_policy.check_network("api.openai.com"), CommandVerdict.ALLOWED)
        self.assertEqual(allowlist_policy.check_network("sub.api.openai.com"), CommandVerdict.ALLOWED)
        self.assertEqual(allowlist_policy.check_network("evil.com"), CommandVerdict.DENIED)

    def test_runner_task_workspace_creation_and_cleanup_git(self):
        from boss.runner.workspace import (
            WorkspaceStrategy,
            WorkspaceState,
            TaskWorkspace,
            create_task_workspace,
            cleanup_task_workspace,
            load_task_workspace,
            list_task_workspaces,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Boss Tests"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "boss@example.com"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)

            ws_dir = Path(temp_dir) / "boss-data" / "task-workspaces"
            worktree_dir = Path(temp_dir) / "boss-data" / "worktrees"

            with override_settings(app_data_dir=Path(temp_dir) / "boss-data"):
                ws = create_task_workspace(
                    source_path=str(root),
                    task_slug="test-task",
                    branch_name="boss/test-task",
                )

                self.assertEqual(ws.strategy, WorkspaceStrategy.GIT_WORKTREE.value)
                self.assertEqual(ws.state, WorkspaceState.CREATED.value)
                self.assertTrue(Path(ws.workspace_path).exists())
                self.assertEqual(ws.branch_name, "boss/test-task")

                loaded = load_task_workspace(ws.workspace_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.workspace_id, ws.workspace_id)

                workspaces = list_task_workspaces()
                self.assertEqual(len(workspaces), 1)

                cleaned = cleanup_task_workspace(ws.workspace_id)
                self.assertTrue(cleaned)

                after_cleanup = load_task_workspace(ws.workspace_id)
                self.assertIsNotNone(after_cleanup)
                assert after_cleanup is not None
                self.assertEqual(after_cleanup.state, WorkspaceState.CLEANED_UP.value)

    def test_runner_task_workspace_creation_temp_directory(self):
        from boss.runner.workspace import (
            WorkspaceStrategy,
            WorkspaceState,
            create_task_workspace,
            cleanup_task_workspace,
            load_task_workspace,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "non-git-project"
            root.mkdir()

            with override_settings(app_data_dir=Path(temp_dir) / "boss-data"):
                ws = create_task_workspace(
                    source_path=str(root),
                    task_slug="temp-task",
                )

                self.assertEqual(ws.strategy, WorkspaceStrategy.TEMP_DIRECTORY.value)
                self.assertEqual(ws.state, WorkspaceState.CREATED.value)
                self.assertTrue(Path(ws.workspace_path).exists())
                self.assertIsNone(ws.branch_name)

                cleaned = cleanup_task_workspace(ws.workspace_id)
                self.assertTrue(cleaned)

                after = load_task_workspace(ws.workspace_id)
                self.assertIsNotNone(after)
                assert after is not None
                self.assertEqual(after.state, WorkspaceState.CLEANED_UP.value)

    def test_runner_escalation_triggers_prompt_verdict(self):
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        # workspace_write profile with specific allowed prefixes
        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=("git", "python"),
            prompt_prefixes=("curl",),
            denied_prefixes=("sudo",),
            allow_shell=True,
            env_scrub_keys=(),
        )

        # Commands not in allowed set but not denied should escalate to PROMPT
        self.assertEqual(policy.check_command("npm run build"), CommandVerdict.PROMPT)
        self.assertEqual(policy.check_command("rsync files"), CommandVerdict.PROMPT)

        # Network disabled triggers denial
        self.assertEqual(policy.check_network("example.com"), CommandVerdict.DENIED)

        # Write outside roots triggers prompt
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir) / "ws"
            ws.mkdir()
            outside = Path(temp_dir) / "outside"
            outside.mkdir()

            bounded_policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(ws,), workspace_root=ws),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=(),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            self.assertEqual(bounded_policy.check_write(outside / "file.txt"), CommandVerdict.PROMPT)

    def test_runner_config_loads_from_boss_config_toml(self):
        from boss.runner.policy import (
            PermissionProfile,
            load_runner_config,
            runner_config_for_mode,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".boss").mkdir()
            (root / ".boss" / "rules").mkdir()
            (root / ".boss" / "config.toml").write_text(
                "[mode]\ndefault = \"agent\"\n\n"
                "[runner]\n"
                "default_profile = \"workspace_write\"\n"
                "network_enabled = false\n\n"
                "[runner.mode_profiles]\n"
                "ask = \"read_only\"\n"
                "agent = \"workspace_write\"\n\n"
                "[runner.commands]\n"
                "denied_prefixes = [\"sudo\", \"rm -rf /\"]\n",
                encoding="utf-8",
            )

            config = load_runner_config(root)
            self.assertEqual(config.default_profile, PermissionProfile.WORKSPACE_WRITE)
            self.assertFalse(config.network_enabled)
            self.assertIn("sudo", config.denied_prefixes)

            ask_policy = runner_config_for_mode("ask", root)
            self.assertEqual(ask_policy.profile, PermissionProfile.READ_ONLY)
            self.assertFalse(ask_policy.allow_shell)

            agent_policy = runner_config_for_mode("agent", root)
            self.assertEqual(agent_policy.profile, PermissionProfile.WORKSPACE_WRITE)
            self.assertTrue(agent_policy.allow_shell)

    def test_runner_env_scrubbing_removes_secrets(self):
        from boss.runner.policy import (
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=("MY_CUSTOM_SECRET",),
        )

        with patch.dict(os.environ, {
            "MY_CUSTOM_SECRET": "secret-value",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "NORMAL_VAR": "okay",
        }):
            scrubbed = policy.scrubbed_env()
            self.assertNotIn("MY_CUSTOM_SECRET", scrubbed)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", scrubbed)
            self.assertIn("NORMAL_VAR", scrubbed)

    def test_runner_sandbox_detection_returns_honest_report(self):
        from boss.runner.sandbox import detect_sandbox_capabilities

        report = detect_sandbox_capabilities()
        self.assertEqual(report.enforcement_level, "boss_policy")
        self.assertIn("boss_policy", report.enforcement_level)
        self.assertTrue(len(report.recommendations) > 0)
        # Should not claim kernel enforcement
        self.assertNotEqual(report.enforcement_level, "os_sandbox")

    def test_runner_full_access_profile_allows_most_commands(self):
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.FULL_ACCESS,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.ENABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=("sudo", "rm -rf /"),
            allow_shell=True,
            env_scrub_keys=(),
        )

        # full_access still blocks denied prefixes
        self.assertEqual(policy.check_command("sudo reboot"), CommandVerdict.DENIED)
        self.assertEqual(policy.check_command("rm -rf /etc"), CommandVerdict.DENIED)
        # but allows everything else
        self.assertEqual(policy.check_command("npm run build"), CommandVerdict.ALLOWED)
        self.assertEqual(policy.check_command("git push"), CommandVerdict.ALLOWED)
        # and allows network
        self.assertEqual(policy.check_network("example.com"), CommandVerdict.ALLOWED)
        # and allows writes anywhere
        self.assertEqual(policy.check_write(Path("/etc/passwd")), CommandVerdict.ALLOWED)

    def test_runner_policy_serializes_for_diagnostics(self):
        from boss.runner.policy import runner_config_for_mode

        policy = runner_config_for_mode("agent")
        payload = policy.to_dict()

        self.assertIn("profile", payload)
        self.assertIn("enforcement", payload)
        self.assertEqual(payload["enforcement"], "boss")
        self.assertIn("writable_roots", payload)
        self.assertIn("network", payload)
        self.assertIn("allowed_prefixes", payload)
        self.assertIn("denied_prefixes", payload)

    def test_runner_engine_enforces_write_path_on_touch(self):
        """run_command must deny 'touch' (and similar) that target paths outside writable roots."""
        from boss.runner.engine import RunnerEngine
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            outside_file = outside / "should_not_exist.txt"

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(workspace,), workspace_root=workspace),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("touch",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            engine = RunnerEngine(policy)

            # touch inside workspace succeeds
            inside_file = workspace / "ok.txt"
            result_ok = engine.run_command(["touch", str(inside_file)])
            self.assertEqual(result_ok.verdict, CommandVerdict.ALLOWED.value)

            # touch outside workspace is blocked by write-path check
            result_bad = engine.run_command(["touch", str(outside_file)])
            self.assertIn(result_bad.verdict, (CommandVerdict.DENIED.value, CommandVerdict.PROMPT.value))
            self.assertFalse(outside_file.exists(), "File outside writable roots must NOT be created")

    def test_runner_engine_enforces_write_path_on_cp(self):
        from boss.runner.engine import RunnerEngine
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            src_file = workspace / "src.txt"
            src_file.write_text("hello", encoding="utf-8")
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            outside_dest = outside / "copied.txt"

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(workspace,), workspace_root=workspace),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("cp",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            engine = RunnerEngine(policy)
            result = engine.run_command(["cp", str(src_file), str(outside_dest)])
            self.assertIn(result.verdict, (CommandVerdict.DENIED.value, CommandVerdict.PROMPT.value))
            self.assertFalse(outside_dest.exists(), "cp must not write outside writable roots")

    def test_runner_screenshot_routed_through_runner_write_check(self):
        """screenshot() must go through _run_command path, not raw subprocess.run."""
        from boss.runner.engine import RunnerEngine, _current_runner_var
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            outside_file = Path(temp_dir) / "outside" / "shot.png"

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(workspace,), workspace_root=workspace),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("screencapture",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            engine = RunnerEngine(policy)
            token = _current_runner_var.set(engine)
            try:
                # The write target is outside writable roots, so the runner
                # should block or escalate the command (not silently allow it)
                result = engine.run_command(["screencapture", "-x", str(outside_file)])
                self.assertIn(result.verdict, (CommandVerdict.DENIED.value, CommandVerdict.PROMPT.value))
            finally:
                _current_runner_var.reset(token)

    def test_runner_context_scoped_not_global(self):
        """Runner must use contextvars so concurrent contexts get independent runners."""
        import contextvars
        from boss.runner.engine import RunnerEngine, get_runner, current_runner, _current_runner_var
        from boss.runner.policy import (
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        ctx_a = contextvars.copy_context()
        ctx_b = contextvars.copy_context()

        def set_and_read_profile(mode: str) -> str:
            runner = get_runner(mode=mode)
            return runner.policy.profile.value

        profile_a = ctx_a.run(set_and_read_profile, "ask")
        profile_b = ctx_b.run(set_and_read_profile, "agent")

        self.assertEqual(profile_a, PermissionProfile.READ_ONLY.value)
        self.assertEqual(profile_b, PermissionProfile.WORKSPACE_WRITE.value)

        # Each context retains its own runner independently
        def read_profile() -> str | None:
            runner = current_runner()
            return runner.policy.profile.value if runner else None

        self.assertEqual(ctx_a.run(read_profile), PermissionProfile.READ_ONLY.value)
        self.assertEqual(ctx_b.run(read_profile), PermissionProfile.WORKSPACE_WRITE.value)

    def test_runner_temp_workspace_copies_source_tree(self):
        """Non-git temp workspaces must contain source files."""
        from boss.runner.workspace import (
            WorkspaceStrategy,
            create_task_workspace,
            cleanup_task_workspace,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "src-project"
            root.mkdir()
            (root / "main.py").write_text("print('hello')\n", encoding="utf-8")
            sub = root / "pkg"
            sub.mkdir()
            (sub / "mod.py").write_text("x = 1\n", encoding="utf-8")
            # add a directory that should be ignored
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "cached.pyc").write_text("nope", encoding="utf-8")

            with override_settings(app_data_dir=Path(temp_dir) / "boss-data"):
                ws = create_task_workspace(source_path=str(root), task_slug="copy-test")

                self.assertEqual(ws.strategy, WorkspaceStrategy.TEMP_DIRECTORY.value)
                ws_path = Path(ws.workspace_path)
                self.assertTrue((ws_path / "main.py").exists(), "main.py must exist in workspace")
                self.assertTrue((ws_path / "pkg" / "mod.py").exists(), "pkg/mod.py must exist in workspace")
                self.assertFalse(
                    (ws_path / "__pycache__").exists(), "__pycache__ should be excluded from copy"
                )

                cleanup_task_workspace(ws.workspace_id)

    # ---- Prompt 3 regression tests ----

    def test_runner_prompt_verdict_blocks_execution(self):
        """Commands that receive PROMPT verdict must not execute."""
        from boss.runner.engine import RunnerEngine
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=("git",),
            prompt_prefixes=("curl",),
            denied_prefixes=("sudo",),
            allow_shell=True,
            env_scrub_keys=(),
        )
        engine = RunnerEngine(policy)

        # curl is in prompt_prefixes — should be blocked, not executed
        result = engine.run_command(["curl", "https://example.com"])
        self.assertIsNone(result.exit_code, "PROMPT command should not execute")
        self.assertEqual(result.verdict, CommandVerdict.PROMPT.value)
        self.assertIn("approval", result.denied_reason or "")

        # unknown command triggers PROMPT because allowed_prefixes are set
        result2 = engine.run_command(["rsync", "files"])
        self.assertIsNone(result2.exit_code, "Unknown command should not execute")
        self.assertEqual(result2.verdict, CommandVerdict.PROMPT.value)

    def test_runner_interpreter_inline_flag_escalates_to_prompt(self):
        """python -c, sh -c, node -e must be escalated to PROMPT."""
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=("python", "python3", "sh", "bash", "node"),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )

        # python without -c is allowed
        self.assertEqual(policy.check_command(["python", "script.py"]), CommandVerdict.ALLOWED)
        # python with -c is escalated
        self.assertEqual(policy.check_command(["python", "-c", "print('hi')"]), CommandVerdict.PROMPT)
        self.assertEqual(policy.check_command(["python3", "-c", "import os"]), CommandVerdict.PROMPT)
        # sh -c
        self.assertEqual(policy.check_command(["sh", "-c", "touch /tmp/x"]), CommandVerdict.PROMPT)
        self.assertEqual(policy.check_command(["bash", "-c", "echo hi"]), CommandVerdict.PROMPT)
        # node -e
        self.assertEqual(policy.check_command(["node", "-e", "process.exit()"]), CommandVerdict.PROMPT)
        # node --eval
        self.assertEqual(policy.check_command(["node", "--eval", "1+1"]), CommandVerdict.PROMPT)
        # absolute paths should also be detected
        self.assertEqual(policy.check_command(["/usr/bin/python3", "-c", "x=1"]), CommandVerdict.PROMPT)

    def test_runner_interpreter_without_inline_flag_allowed(self):
        """Interpreters running script files should stay ALLOWED."""
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=("python", "python3", "node"),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        self.assertEqual(policy.check_command(["python", "manage.py", "migrate"]), CommandVerdict.ALLOWED)
        self.assertEqual(policy.check_command(["node", "index.js"]), CommandVerdict.ALLOWED)
        self.assertEqual(policy.check_command(["python3", "-m", "pytest"]), CommandVerdict.ALLOWED)

    def test_runner_cwd_enforcement_blocks_outside_writable_roots(self):
        """Engine must block execution when cwd is outside writable roots."""
        from boss.runner.engine import RunnerEngine
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir) / "workspace"
            ws.mkdir()
            outside = Path(temp_dir) / "outside"
            outside.mkdir()

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(ws,), workspace_root=ws),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("echo",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            engine = RunnerEngine(policy)

            # cwd within workspace — should work
            result_ok = engine.run_command(["echo", "hi"], cwd=ws)
            self.assertEqual(result_ok.exit_code, 0)

            # cwd outside workspace — should be denied
            result_bad = engine.run_command(["echo", "hi"], cwd=outside)
            self.assertIsNone(result_bad.exit_code)
            self.assertEqual(result_bad.verdict, CommandVerdict.DENIED.value)
            self.assertIn("outside writable roots", result_bad.denied_reason or "")

    def test_runner_cwd_defaults_to_workspace_root(self):
        """When no cwd is supplied, engine should default to workspace_root."""
        from boss.runner.engine import RunnerEngine
        from boss.runner.policy import (
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir) / "workspace"
            ws.mkdir()

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(writable_roots=(ws,), workspace_root=ws),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("pwd",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            engine = RunnerEngine(policy)

            result = engine.run_command(["pwd"])
            self.assertEqual(result.exit_code, 0)
            # The cwd should be the workspace root
            self.assertEqual(result.working_directory, str(ws))

    def test_pending_run_stores_project_path(self):
        """PendingRun must round-trip project_path through save/load."""
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_dir = Path(temp_dir) / "pending_runs"
            pending_dir.mkdir()
            with override_settings(pending_runs_dir=pending_dir, pending_run_expiration_seconds=3600):
                run_id = save_pending_run(
                    session_id="test-sess",
                    state={"test": True},
                    approvals=[],
                    mode="agent",
                    project_path="/some/project",
                )
                loaded = load_pending_run(run_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.project_path, "/some/project")

    def test_chat_request_accepts_project_path(self):
        """ChatRequest model should accept and preserve project_path."""
        api = import_api_module()
        req = api.ChatRequest(message="hello", project_path="/foo/bar")
        self.assertEqual(req.project_path, "/foo/bar")

        req_none = api.ChatRequest(message="hello")
        self.assertIsNone(req_none.project_path)

    # ---- Prompt 3b regression tests ----

    def test_mac_run_command_raises_on_prompt_verdict(self):
        """_run_command in mac.py must raise on PROMPT, not return empty string."""
        from boss.runner.engine import RunnerEngine, _current_runner_var
        from boss.runner.policy import (
            CommandVerdict,
            ExecutionPolicy,
            NetworkPolicy,
            PathPolicy,
            PermissionProfile,
        )

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=("open",),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        engine = RunnerEngine(policy)
        token = _current_runner_var.set(engine)
        try:
            from boss.tools.mac import _run_command
            with self.assertRaises(RuntimeError) as ctx:
                _run_command(["open", "-a", "TextEdit"])
            self.assertIn("approval", str(ctx.exception).lower())
        finally:
            _current_runner_var.reset(token)

    def test_background_job_record_persists_task_workspace_path(self):
        """BackgroundJobRecord must round-trip task_workspace_path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs_dir = Path(temp_dir) / "jobs"
            logs_dir = Path(temp_dir) / "logs"
            jobs_dir.mkdir()
            logs_dir.mkdir()
            with override_settings(jobs_dir=jobs_dir, job_logs_dir=logs_dir):
                job = create_background_job(
                    prompt="test workspace path",
                    mode="agent",
                    session_id="s1",
                    project_path="/orig/project",
                    initial_input_kind="prepared_input",
                    initial_input_payload=[],
                )
                self.assertIsNone(job.task_workspace_path)

                updated = update_background_job(
                    job.job_id,
                    task_workspace_path="/isolated/workspace/path",
                )
                self.assertEqual(updated.task_workspace_path, "/isolated/workspace/path")

                reloaded = load_background_job(job.job_id)
                self.assertIsNotNone(reloaded)
                assert reloaded is not None
                self.assertEqual(reloaded.task_workspace_path, "/isolated/workspace/path")

    def test_pending_run_stores_and_loads_project_path_for_resume(self):
        """PendingRun project_path must survive save → load for permission resume."""
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_dir = Path(temp_dir) / "pending_runs"
            pending_dir.mkdir()
            with override_settings(pending_runs_dir=pending_dir, pending_run_expiration_seconds=3600):
                run_id = save_pending_run(
                    session_id="resume-sess",
                    state={"test": True},
                    approvals=[],
                    mode="agent",
                    project_path="/my/project/root",
                )
                loaded = load_pending_run(run_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.project_path, "/my/project/root")

                # Without project_path it should be None (backward compat)
                run_id2 = save_pending_run(
                    session_id="no-path",
                    state={},
                    approvals=[],
                    mode="agent",
                )
                loaded2 = load_pending_run(run_id2)
                assert loaded2 is not None
                self.assertIsNone(loaded2.project_path)


# ---- Code Intelligence Tests ----

class TestCodeIntelligenceParsers(unittest.TestCase):
    """Test language parsers produce correct symbol graphs."""

    def test_parse_python_basic(self):
        from boss.intelligence.parsers import parse_python, SymbolKind
        source = '''
"""Module doc."""
import os
from pathlib import Path

TIMEOUT = 30

class MyClass:
    """A sample class."""
    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        return f"Hello {self.name}"

def helper(x: int) -> int:
    """Compute something."""
    return x * 2

if __name__ == "__main__":
    print("hello")
'''
        graph = parse_python(source, "sample.py")
        self.assertEqual(graph.language, "python")
        self.assertTrue(graph.entry_point)

        names = {s.name for s in graph.symbols}
        self.assertIn("MyClass", names)
        self.assertIn("__init__", names)
        self.assertIn("greet", names)
        self.assertIn("helper", names)
        self.assertIn("TIMEOUT", names)

        # Check class has correct kind
        class_sym = [s for s in graph.symbols if s.name == "MyClass"][0]
        self.assertEqual(class_sym.kind, SymbolKind.CLASS)
        self.assertEqual(class_sym.docstring, "A sample class.")

        # Check method parent
        init_sym = [s for s in graph.symbols if s.name == "__init__"][0]
        self.assertEqual(init_sym.parent, "MyClass")
        self.assertEqual(init_sym.kind, SymbolKind.METHOD)

        # Check imports
        modules = {imp.module for imp in graph.imports}
        self.assertIn("os", modules)
        self.assertIn("pathlib", modules)

    def test_parse_swift_basic(self):
        from boss.intelligence.parsers import parse_swift, SymbolKind
        source = '''
import Foundation
import SwiftUI

/// A sample view model.
class ChatViewModel: ObservableObject {
    @Published var messages: [String] = []

    /// Send a message.
    func send(text: String) {
        messages.append(text)
    }
}

struct ContentView: View {
    var body: some View {
        Text("Hello")
    }
}

protocol DataService {
    func fetch() async throws -> Data
}
'''
        graph = parse_swift(source, "Views.swift")
        self.assertEqual(graph.language, "swift")

        names = {s.name for s in graph.symbols}
        self.assertIn("ChatViewModel", names)
        self.assertIn("send", names)
        self.assertIn("ContentView", names)
        self.assertIn("DataService", names)

        class_sym = [s for s in graph.symbols if s.name == "ChatViewModel"][0]
        self.assertEqual(class_sym.kind, SymbolKind.CLASS)
        self.assertEqual(class_sym.docstring, "A sample view model.")

        protocol_sym = [s for s in graph.symbols if s.name == "DataService"][0]
        self.assertEqual(protocol_sym.kind, SymbolKind.PROTOCOL)

        modules = {imp.module for imp in graph.imports}
        self.assertIn("Foundation", modules)
        self.assertIn("SwiftUI", modules)

    def test_parse_typescript_basic(self):
        from boss.intelligence.parsers import parse_typescript, SymbolKind
        source = '''
import { useState } from 'react';
import axios from 'axios';

/** Configuration options. */
interface AppConfig {
    title: string;
    debug: boolean;
}

export class ApiClient {
    private baseUrl: string;

    constructor(url: string) {
        this.baseUrl = url;
    }

    /** Fetch data from API. */
    async fetchData(endpoint: string): Promise<any> {
        return axios.get(`${this.baseUrl}/${endpoint}`);
    }
}

export const VERSION = "1.0.0";

const helper = (x: number): number => x * 2;
'''
        graph = parse_typescript(source, "api.ts")
        self.assertEqual(graph.language, "typescript")

        names = {s.name for s in graph.symbols}
        self.assertIn("AppConfig", names)
        self.assertIn("ApiClient", names)
        self.assertIn("fetchData", names)
        self.assertIn("VERSION", names)
        self.assertIn("helper", names)

        iface = [s for s in graph.symbols if s.name == "AppConfig"][0]
        self.assertEqual(iface.kind, SymbolKind.INTERFACE)
        self.assertEqual(iface.docstring, "Configuration options.")

        client = [s for s in graph.symbols if s.name == "ApiClient"][0]
        self.assertEqual(client.kind, SymbolKind.CLASS)
        self.assertTrue(client.exported)

        modules = {imp.module for imp in graph.imports}
        self.assertIn("react", modules)
        self.assertIn("axios", modules)

    def test_detect_language(self):
        from boss.intelligence.parsers import detect_language
        self.assertEqual(detect_language("foo.py"), "python")
        self.assertEqual(detect_language("bar.swift"), "swift")
        self.assertEqual(detect_language("baz.ts"), "typescript")
        self.assertEqual(detect_language("baz.tsx"), "typescript")
        self.assertEqual(detect_language("baz.js"), "javascript")
        self.assertIsNone(detect_language("README.md"))
        self.assertIsNone(detect_language("data.csv"))

    def test_parse_file_dispatcher(self):
        from boss.intelligence.parsers import parse_file
        source = 'def foo(): pass'
        graph = parse_file("test.py", source)
        self.assertIsNotNone(graph)
        self.assertEqual(graph.language, "python")
        names = {s.name for s in graph.symbols}
        self.assertIn("foo", names)

        # Unknown extension returns None
        result = parse_file("data.csv", "a,b,c")
        self.assertIsNone(result)

    def test_symbol_graph_to_dict(self):
        from boss.intelligence.parsers import parse_python
        graph = parse_python("class Foo:\n    pass\n", "x.py")
        d = graph.to_dict()
        self.assertEqual(d["language"], "python")
        self.assertIn("symbols", d)
        self.assertIsInstance(d["symbols"], list)

    def test_python_test_file_detection(self):
        from boss.intelligence.parsers import parse_python
        source = '''
import unittest

class TestFoo(unittest.TestCase):
    def test_bar(self):
        self.assertTrue(True)
'''
        graph = parse_python(source, "test_foo.py")
        self.assertTrue(graph.test_file)

    def test_python_decorator_capture(self):
        from boss.intelligence.parsers import parse_python
        source = '''
@app.route("/api")
@login_required
def api_handler():
    pass
'''
        graph = parse_python(source, "handlers.py")
        handler = [s for s in graph.symbols if s.name == "api_handler"][0]
        self.assertIn("app.route", handler.decorators[0])


class TestCodeIndex(unittest.TestCase):
    """Test the SQLite code index with synthetic repos."""

    def _make_index(self):
        from boss.intelligence.index import CodeIndex
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test_code_index.db"
        return CodeIndex(db_path=db_path), tmp

    def test_index_python_file(self):
        idx, tmp = self._make_index()
        try:
            source = 'class MyService:\n    def handle(self, req): pass\n'
            py_file = Path(tmp) / "service.py"
            py_file.write_text(source)

            graph = idx.index_file(str(py_file), project_path=tmp)
            idx.commit()
            self.assertIsNotNone(graph)
            self.assertEqual(graph.language, "python")

            # Find by name
            results = idx.find_symbol("MyService")
            self.assertTrue(len(results) >= 1)
            self.assertEqual(results[0].name, "MyService")
            self.assertEqual(results[0].kind, "class")

            # Find definition
            defs = idx.find_definition("handle")
            self.assertTrue(len(defs) >= 1)
            self.assertEqual(defs[0].parent, "MyService")
        finally:
            idx.close()

    def test_incremental_skip(self):
        idx, tmp = self._make_index()
        try:
            py_file = Path(tmp) / "mod.py"
            py_file.write_text("def foo(): pass\n")
            result1 = idx.index_file(str(py_file), project_path=tmp)
            idx.commit()
            self.assertIsNotNone(result1)

            # Second index with same content skips
            result2 = idx.index_file(str(py_file), project_path=tmp)
            self.assertIsNone(result2)

            # Change content triggers re-index
            py_file.write_text("def foo(): pass\ndef bar(): pass\n")
            result3 = idx.index_file(str(py_file), project_path=tmp)
            idx.commit()
            self.assertIsNotNone(result3)
        finally:
            idx.close()

    def test_search_symbols(self):
        idx, tmp = self._make_index()
        try:
            source = '''
class UserManager:
    """Manages user lifecycle."""
    def create_user(self, name: str):
        pass
    def delete_user(self, uid: int):
        pass

def validate_email(email: str) -> bool:
    """Check email format."""
    pass
'''
            py_file = Path(tmp) / "users.py"
            py_file.write_text(source)
            idx.index_file(str(py_file), project_path=tmp)
            idx.commit()

            results = idx.search_symbols("user")
            names = {r.name for r in results}
            self.assertIn("UserManager", names)
            self.assertIn("create_user", names)
            self.assertIn("delete_user", names)

            results = idx.search_symbols("email")
            names = {r.name for r in results}
            self.assertIn("validate_email", names)
        finally:
            idx.close()

    def test_find_importers(self):
        idx, tmp = self._make_index()
        try:
            source1 = "from pathlib import Path\nimport os\n\ndef helper(): pass\n"
            source2 = "from . import helper\nimport json\n\ndef main(): pass\n"
            f1 = Path(tmp) / "utils.py"
            f2 = Path(tmp) / "main.py"
            f1.write_text(source1)
            f2.write_text(source2)
            idx.index_file(str(f1), project_path=tmp)
            idx.index_file(str(f2), project_path=tmp)
            idx.commit()

            results = idx.find_importers("pathlib")
            self.assertTrue(len(results) >= 1)
            self.assertTrue(any(r.file_path == str(f1) for r in results))

            results = idx.find_importers("json")
            self.assertTrue(len(results) >= 1)
            self.assertTrue(any(r.file_path == str(f2) for r in results))
        finally:
            idx.close()

    def test_project_graph(self):
        idx, tmp = self._make_index()
        try:
            f1 = Path(tmp) / "app.py"
            f1.write_text('if __name__ == "__main__":\n    pass\n')
            f2 = Path(tmp) / "test_app.py"
            f2.write_text("import pytest\ndef test_main(): pass\n")
            f3 = Path(tmp) / "models.py"
            f3.write_text("class User:\n    pass\nclass Post:\n    pass\n")

            for f in [f1, f2, f3]:
                idx.index_file(str(f), project_path=tmp)
            idx.commit()

            graph = idx.project_graph(tmp)
            self.assertEqual(graph["files_indexed"], 3)
            self.assertIn("python", graph["languages"])
            self.assertTrue(len(graph["entry_points"]) >= 1)
            self.assertTrue(len(graph["test_files"]) >= 1)
            self.assertTrue(graph["total_lines"] > 0)
        finally:
            idx.close()

    def test_prune_project(self):
        idx, tmp = self._make_index()
        try:
            f1 = Path(tmp) / "keep.py"
            f2 = Path(tmp) / "remove.py"
            f1.write_text("x = 1\n")
            f2.write_text("y = 2\n")
            idx.index_file(str(f1), project_path=tmp)
            idx.index_file(str(f2), project_path=tmp)
            idx.commit()

            removed = idx.prune_project(tmp, {str(f1)})
            self.assertEqual(removed, 1)

            # Only f1 symbols should remain
            stats = idx.stats()
            self.assertEqual(stats["files_indexed"], 1)
        finally:
            idx.close()

    def test_entry_points_and_test_files(self):
        idx, tmp = self._make_index()
        try:
            f1 = Path(tmp) / "main.py"
            f1.write_text('if __name__ == "__main__":\n    main()\n')
            f2 = Path(tmp) / "test_foo.py"
            f2.write_text("def test_bar(): assert True\n")
            f3 = Path(tmp) / "lib.py"
            f3.write_text("def util(): pass\n")
            for f in [f1, f2, f3]:
                idx.index_file(str(f), project_path=tmp)
            idx.commit()

            eps = idx.entry_points(project_path=tmp)
            self.assertTrue(any(str(f1) in e["file_path"] for e in eps))

            tfs = idx.test_files(project_path=tmp)
            self.assertTrue(any(str(f2) in t["file_path"] for t in tfs))
        finally:
            idx.close()

    def test_stats(self):
        idx, tmp = self._make_index()
        try:
            f = Path(tmp) / "mod.py"
            f.write_text("class A:\n    pass\nclass B:\n    pass\n")
            idx.index_file(str(f), project_path=tmp)
            idx.commit()

            stats = idx.stats()
            self.assertEqual(stats["files_indexed"], 1)
            self.assertTrue(stats["symbols"] >= 2)
            self.assertIn("python", stats["languages"])
        finally:
            idx.close()


class TestEmbeddingsStore(unittest.TestCase):
    """Test the embeddings store with synthetic vectors."""

    def _make_store(self):
        from boss.intelligence.embeddings import EmbeddingsStore
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test_embeddings.db"
        return EmbeddingsStore(db_path=db_path), tmp

    def test_store_and_search(self):
        from boss.intelligence.embeddings import EmbeddingRecord
        store, tmp = self._make_store()
        try:
            records = [
                EmbeddingRecord(
                    chunk_id="c1", file_path="/test/a.py", project_path="/test",
                    content="def hello(): pass", line_start=1, line_end=1,
                    vector=[1.0, 0.0, 0.0],
                ),
                EmbeddingRecord(
                    chunk_id="c2", file_path="/test/b.py", project_path="/test",
                    content="class World: pass", line_start=1, line_end=1,
                    vector=[0.0, 1.0, 0.0],
                ),
            ]
            stored = store.store_embeddings(records)
            self.assertEqual(stored, 2)

            # Search with a vector close to c1
            results = store.search([0.9, 0.1, 0.0], limit=5)
            self.assertTrue(len(results) >= 1)
            self.assertEqual(results[0]["chunk_id"], "c1")
            self.assertTrue(results[0]["similarity"] > results[1]["similarity"])
        finally:
            store.close()

    def test_incremental_skip(self):
        from boss.intelligence.embeddings import EmbeddingRecord
        store, tmp = self._make_store()
        try:
            rec = EmbeddingRecord(
                chunk_id="c1", file_path="/test/a.py", project_path="/test",
                content="def hello(): pass", line_start=1, line_end=1,
                vector=[1.0, 0.0],
            )
            stored1 = store.store_embeddings([rec])
            self.assertEqual(stored1, 1)

            # Same content skips
            stored2 = store.store_embeddings([rec])
            self.assertEqual(stored2, 0)
        finally:
            store.close()

    def test_remove_file(self):
        from boss.intelligence.embeddings import EmbeddingRecord
        store, tmp = self._make_store()
        try:
            rec = EmbeddingRecord(
                chunk_id="c1", file_path="/test/a.py", project_path="/test",
                content="x", line_start=1, line_end=1, vector=[1.0],
            )
            store.store_embeddings([rec])
            store.remove_file("/test/a.py")
            stats = store.stats()
            self.assertEqual(stats["embeddings_stored"], 0)
        finally:
            store.close()


class TestHybridRetrieval(unittest.TestCase):
    """Test the hybrid retrieval layer with code index backing."""

    def _setup_index(self):
        from boss.intelligence.index import CodeIndex
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test_retrieval.db"
        idx = CodeIndex(db_path=db_path)

        source = '''
class UserService:
    """Handles user operations."""
    def create_user(self, name: str):
        """Create a new user."""
        pass
    def delete_user(self, uid: int):
        pass

def authenticate(token: str) -> bool:
    """Verify auth token."""
    pass
'''
        f = Path(tmp) / "service.py"
        f.write_text(source)
        idx.index_file(str(f), project_path=tmp)
        idx.commit()
        return idx, tmp

    def test_symbol_search_via_retrieval(self):
        from boss.intelligence.retrieval import _search_symbols, ResultKind
        from boss.intelligence import index as index_mod

        idx, tmp = self._setup_index()
        old_fn = index_mod.get_code_index

        try:
            index_mod.get_code_index = lambda: idx
            results = _search_symbols("UserService", project_path=tmp, weight=1.0)
            self.assertTrue(len(results) >= 1)
            self.assertEqual(results[0].kind, ResultKind.symbol)
            self.assertTrue(results[0].score > 0)
        finally:
            index_mod.get_code_index = old_fn
            idx.close()

    def test_keyword_search_via_retrieval(self):
        from boss.intelligence.retrieval import _search_keywords, ResultKind
        from boss.intelligence import index as index_mod

        idx, tmp = self._setup_index()
        old_fn = index_mod.get_code_index

        try:
            index_mod.get_code_index = lambda: idx
            results = _search_keywords("auth token", project_path=tmp, weight=0.8)
            self.assertTrue(len(results) >= 1)
            names = {r.metadata.get("name") for r in results}
            self.assertIn("authenticate", names)
        finally:
            index_mod.get_code_index = old_fn
            idx.close()

    def test_deduplication(self):
        from boss.intelligence.retrieval import _deduplicate, RetrievalResult, ResultKind
        results = [
            RetrievalResult(kind=ResultKind.symbol, score=0.9, file_path="/a.py", line=10, end_line=15, content="def foo()"),
            RetrievalResult(kind=ResultKind.symbol, score=0.7, file_path="/a.py", line=10, end_line=15, content="def foo()"),
            RetrievalResult(kind=ResultKind.memory, score=0.5, file_path=None, line=None, end_line=None, content="some memory"),
        ]
        merged = _deduplicate(results)
        # Two unique entries: /a.py:10 (keeping score=0.9) and the memory
        self.assertEqual(len(merged), 2)
        file_result = [r for r in merged if r.file_path == "/a.py"][0]
        self.assertAlmostEqual(file_result.score, 0.9)


class TestIntelligenceToolsRegistered(unittest.TestCase):
    """Verify intelligence tools are properly registered in the execution system."""

    def test_tools_have_metadata(self):
        from boss.tools.intelligence import (
            find_symbol, find_definition, search_code_symbolic,
            search_code_semantic, project_graph, find_importers,
        )
        expected_names = [
            "find_symbol", "find_definition", "search_code_symbolic",
            "search_code_semantic", "project_graph", "find_importers",
        ]
        for name in expected_names:
            metadata = get_tool_metadata(name)
            self.assertIsNotNone(metadata, f"Tool '{name}' should be registered")

    def test_tools_in_boss_agent(self):
        agent = build_entry_agent(mode="agent")
        # The boss agent should have action tools directly
        tool_names = {tool.name for tool in agent.tools}
        self.assertIn("write_file", tool_names)
        self.assertIn("edit_file", tool_names)
        self.assertIn("run_shell", tool_names)
        self.assertIn("read_file", tool_names)

    def test_find_symbol_search_type(self):
        from boss.execution import ExecutionType
        meta = get_tool_metadata("find_symbol")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.SEARCH)

    def test_project_graph_read_type(self):
        from boss.execution import ExecutionType
        meta = get_tool_metadata("project_graph")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.READ)


class TestFindSymbolExportedOnly(unittest.TestCase):
    """Regression: find_symbol(exported_only=True) must not raise a SQLite binding error."""

    def test_exported_only_no_crash(self):
        from boss.intelligence.index import CodeIndex
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test_exported.db"
        idx = CodeIndex(db_path=db_path)
        try:
            source = "class Foo:\n    pass\n\ndef _private(): pass\n\ndef public_fn(): pass\n"
            f = Path(tmp) / "mod.py"
            f.write_text(source)
            idx.index_file(str(f), project_path=tmp)
            idx.commit()

            # This used to raise ProgrammingError: Incorrect number of bindings
            results = idx.find_symbol("Foo", exported_only=True)
            self.assertTrue(len(results) >= 1)
            self.assertEqual(results[0].name, "Foo")
        finally:
            idx.close()

    def test_exported_only_filters_private(self):
        from boss.intelligence.index import CodeIndex
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test_exported2.db"
        idx = CodeIndex(db_path=db_path)
        try:
            source = "class _Internal:\n    pass\n\nclass Public:\n    pass\n"
            f = Path(tmp) / "mod.py"
            f.write_text(source)
            idx.index_file(str(f), project_path=tmp)
            idx.commit()

            results = idx.find_symbol("_Internal", exported_only=True)
            self.assertEqual(len(results), 0)

            results = idx.find_symbol("Public", exported_only=True)
            self.assertTrue(len(results) >= 1)
        finally:
            idx.close()

    def test_exported_only_with_kind_and_project(self):
        """All optional filters together must not break binding count."""
        from boss.intelligence.index import CodeIndex
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test_exported3.db"
        idx = CodeIndex(db_path=db_path)
        try:
            source = "class Widget:\n    pass\ndef helper(): pass\n"
            f = Path(tmp) / "widgets.py"
            f.write_text(source)
            idx.index_file(str(f), project_path=tmp)
            idx.commit()

            results = idx.find_symbol(
                "Widget", kind="class", project_path=tmp, exported_only=True,
            )
            self.assertTrue(len(results) >= 1)
        finally:
            idx.close()


class TestSemanticSearchNetworkGating(unittest.TestCase):
    """Regression: semantic search must respect runner network policy."""

    def test_semantic_skipped_when_network_disabled(self):
        """When runner has network=DISABLED, _search_semantic must return None."""
        import contextvars
        from boss.intelligence.retrieval import _search_semantic
        from boss.runner.engine import RunnerEngine, _current_runner_var
        from boss.runner.policy import ExecutionPolicy, NetworkPolicy, PathPolicy, PermissionProfile

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        engine = RunnerEngine(policy)
        token = _current_runner_var.set(engine)
        try:
            result = _search_semantic("test query", project_path=None, weight=1.0)
            self.assertIsNone(result)
        finally:
            _current_runner_var.reset(token)

    def test_embed_texts_blocked_when_network_disabled(self):
        """OpenAIEmbeddingsBackend.embed_texts must raise when runner blocks network."""
        import contextvars
        from boss.intelligence.embeddings import OpenAIEmbeddingsBackend
        from boss.runner.engine import RunnerEngine, _current_runner_var
        from boss.runner.policy import ExecutionPolicy, NetworkPolicy, PathPolicy, PermissionProfile

        policy = ExecutionPolicy(
            profile=PermissionProfile.WORKSPACE_WRITE,
            path_policy=PathPolicy(writable_roots=()),
            network=NetworkPolicy.DISABLED,
            domain_allowlist=(),
            allowed_prefixes=(),
            prompt_prefixes=(),
            denied_prefixes=(),
            allow_shell=True,
            env_scrub_keys=(),
        )
        engine = RunnerEngine(policy)
        token = _current_runner_var.set(engine)
        try:
            backend = OpenAIEmbeddingsBackend()
            with self.assertRaises(RuntimeError) as ctx:
                backend.embed_texts(["hello world"])
            self.assertIn("blocked by runner", str(ctx.exception))
        finally:
            _current_runner_var.reset(token)

    def test_semantic_allowed_without_runner(self):
        """Without an active runner, _check_network_allowed should return True."""
        from boss.intelligence.embeddings import _check_network_allowed
        self.assertTrue(_check_network_allowed())


class TestEmbeddingPopulation(unittest.TestCase):
    """Regression: scanning must populate the embeddings store when a backend is available."""

    def test_embed_indexed_files_calls_backend(self):
        """_embed_indexed_files should generate and store embeddings for source files."""
        from boss.memory.scanner import _embed_indexed_files
        from boss.intelligence.embeddings import EmbeddingsStore

        tmp = tempfile.mkdtemp()
        emb_db = Path(tmp) / "test_emb.db"

        # Write a small Python file
        src = Path(tmp) / "example.py"
        src.write_text("class Foo:\n    def bar(self): pass\n")

        # Create a fake backend that returns fixed vectors
        class FakeBackend:
            name = "fake"
            dimensions = 3
            available = True
            def embed_texts(self, texts):
                return [[0.1, 0.2, 0.3]] * len(texts)
            def embed_query(self, q):
                return [0.1, 0.2, 0.3]

        store = EmbeddingsStore(db_path=emb_db)
        fake_backend = FakeBackend()

        # Patch the singletons
        import boss.intelligence.embeddings as emb_mod
        old_backend_fn = emb_mod.get_embeddings_backend
        old_store_fn = emb_mod.get_embeddings_store
        emb_mod.get_embeddings_backend = lambda: fake_backend
        emb_mod.get_embeddings_store = lambda: store

        try:
            _embed_indexed_files(tmp, {str(src)})
            stats = store.stats()
            self.assertGreater(stats["embeddings_stored"], 0,
                               "Embeddings store should be populated after scan")
        finally:
            emb_mod.get_embeddings_backend = old_backend_fn
            emb_mod.get_embeddings_store = old_store_fn
            store.close()

    def test_rescan_unchanged_file_skips_embed_call(self):
        """Rescanning an unchanged file must not call embed_texts again."""
        from boss.memory.scanner import _embed_indexed_files
        from boss.intelligence.embeddings import EmbeddingsStore

        tmp = tempfile.mkdtemp()
        emb_db = Path(tmp) / "test_emb.db"

        src = Path(tmp) / "hello.py"
        src.write_text("def hello():\n    return 'hi'\n")

        call_count = 0

        class CountingBackend:
            name = "counting"
            dimensions = 3
            available = True
            def embed_texts(self, texts):
                nonlocal call_count
                call_count += 1
                return [[0.1, 0.2, 0.3]] * len(texts)
            def embed_query(self, q):
                return [0.1, 0.2, 0.3]

        store = EmbeddingsStore(db_path=emb_db)
        backend = CountingBackend()

        import boss.intelligence.embeddings as emb_mod
        old_b = emb_mod.get_embeddings_backend
        old_s = emb_mod.get_embeddings_store
        emb_mod.get_embeddings_backend = lambda: backend
        emb_mod.get_embeddings_store = lambda: store

        try:
            _embed_indexed_files(tmp, {str(src)})
            self.assertEqual(call_count, 1, "First scan should call embed_texts once")

            call_count = 0
            _embed_indexed_files(tmp, {str(src)})
            self.assertEqual(call_count, 0,
                             "Rescan of unchanged file must skip embed_texts entirely")
        finally:
            emb_mod.get_embeddings_backend = old_b
            emb_mod.get_embeddings_store = old_s
            store.close()

    def test_shrinking_file_removes_stale_chunks(self):
        """When a file shrinks, old chunks with stale line ranges must be removed."""
        from boss.memory.scanner import _embed_indexed_files
        from boss.intelligence.embeddings import EmbeddingsStore

        tmp = tempfile.mkdtemp()
        emb_db = Path(tmp) / "test_emb.db"

        src = Path(tmp) / "big.py"
        # Write a file big enough to produce multiple chunks (>800 chars each)
        src.write_text("x = 1\n" * 300)  # ~1800 chars → multiple chunks

        class FakeBackend:
            name = "fake"
            dimensions = 3
            available = True
            def embed_texts(self, texts):
                return [[0.1, 0.2, 0.3]] * len(texts)
            def embed_query(self, q):
                return [0.1, 0.2, 0.3]

        store = EmbeddingsStore(db_path=emb_db)
        backend = FakeBackend()

        import boss.intelligence.embeddings as emb_mod
        old_b = emb_mod.get_embeddings_backend
        old_s = emb_mod.get_embeddings_store
        emb_mod.get_embeddings_backend = lambda: backend
        emb_mod.get_embeddings_store = lambda: store

        try:
            _embed_indexed_files(tmp, {str(src)})
            stats_before = store.stats()
            chunks_before = stats_before["embeddings_stored"]
            self.assertGreater(chunks_before, 1, "Big file should produce multiple chunks")

            # Shrink the file to 1 chunk
            src.write_text("x = 1\n")
            _embed_indexed_files(tmp, {str(src)})
            stats_after = store.stats()
            chunks_after = stats_after["embeddings_stored"]
            self.assertEqual(chunks_after, 1,
                             f"After shrinking, stale chunks should be removed "
                             f"(was {chunks_before}, now {chunks_after})")
        finally:
            emb_mod.get_embeddings_backend = old_b
            emb_mod.get_embeddings_store = old_s
            store.close()


# ========================
# Loop subsystem tests
# ========================

class TestLoopBudget(unittest.TestCase):
    """Budget dataclass sanity."""

    def test_defaults(self):
        from boss.loop.policy import LoopBudget
        b = LoopBudget()
        self.assertEqual(b.max_attempts, 5)
        self.assertEqual(b.max_commands, 30)
        self.assertEqual(b.max_wall_seconds, 300.0)
        self.assertIsNone(b.max_test_failures)

    def test_floor_clamp(self):
        from boss.loop.policy import LoopBudget
        b = LoopBudget(max_attempts=0, max_commands=-1, max_wall_seconds=-5)
        self.assertEqual(b.max_attempts, 1)
        self.assertEqual(b.max_commands, 1)
        self.assertGreater(b.max_wall_seconds, 0)

    def test_round_trip(self):
        from boss.loop.policy import LoopBudget
        b = LoopBudget(max_attempts=3, max_commands=10, max_wall_seconds=60.0, max_test_failures=2)
        d = b.to_dict()
        b2 = LoopBudget.from_dict(d)
        self.assertEqual(b, b2)


class TestLoopStatePersistence(unittest.TestCase):
    """Loop state save/load round trip."""

    def test_save_and_load(self):
        import time
        from boss.loop.state import LoopState, LoopAttempt, AttemptCommand, save_loop_state, load_loop_state

        with tempfile.TemporaryDirectory() as td:
            with override_settings(app_data_dir=Path(td)):
                state = LoopState(
                    loop_id="test-loop-001",
                    session_id="sess-001",
                    task_description="Fix the bug",
                    budget={"max_attempts": 3, "max_commands": 10, "max_wall_seconds": 60.0},
                    execution_style="iterative",
                    started_at=time.time(),
                    current_attempt=1,
                    total_commands=2,
                    total_test_failures=1,
                    attempts=[
                        LoopAttempt(
                            attempt_number=1,
                            started_at=time.time(),
                            finished_at=time.time(),
                            phase="edit",
                            commands=[
                                AttemptCommand(
                                    command="python test.py",
                                    exit_code=1,
                                    stdout_tail="FAILED",
                                    stderr_tail="",
                                    verdict="allowed",
                                    timestamp=time.time(),
                                )
                            ],
                            test_passed=False,
                            test_output_tail="1 failed",
                        )
                    ],
                    stop_reason=None,
                    micro_plan=["edit foo.py", "run tests"],
                )

                path = save_loop_state(state)
                self.assertTrue(path.exists())

                loaded = load_loop_state("test-loop-001")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.loop_id, "test-loop-001")
                self.assertEqual(loaded.task_description, "Fix the bug")
                self.assertEqual(loaded.current_attempt, 1)
                self.assertEqual(loaded.total_test_failures, 1)
                self.assertEqual(len(loaded.attempts), 1)
                self.assertEqual(loaded.attempts[0].commands[0].command, "python test.py")
                self.assertEqual(loaded.micro_plan, ["edit foo.py", "run tests"])

    def test_load_nonexistent(self):
        from boss.loop.state import load_loop_state
        with tempfile.TemporaryDirectory() as td:
            with override_settings(app_data_dir=Path(td)):
                self.assertIsNone(load_loop_state("does-not-exist-xyz"))


class TestLoopBudgetExhaustion(unittest.TestCase):
    """The engine must stop when budget is exhausted."""

    def test_max_attempts_stops_loop(self):
        from boss.loop.engine import LoopEngine, _sse_event, _try_parse_sse
        from boss.loop.policy import LoopBudget

        budget = LoopBudget(max_attempts=1, max_commands=100, max_wall_seconds=600.0)
        engine = LoopEngine(
            task="dummy task",
            session_id="test-sess",
            budget=budget,
            mode="agent",
        )

        # Manually set current_attempt to budget limit
        engine._state.current_attempt = 1
        stop = engine._check_budget()
        self.assertIsNotNone(stop)
        self.assertEqual(stop.value, "max_attempts")

    def test_max_commands_stops_loop(self):
        from boss.loop.engine import LoopEngine
        from boss.loop.policy import LoopBudget

        budget = LoopBudget(max_attempts=10, max_commands=5, max_wall_seconds=600.0)
        engine = LoopEngine(
            task="dummy task",
            session_id="test-sess",
            budget=budget,
        )
        engine._state.total_commands = 5
        stop = engine._check_budget()
        self.assertIsNotNone(stop)
        self.assertEqual(stop.value, "max_commands")

    def test_max_failures_stops_loop(self):
        from boss.loop.engine import LoopEngine
        from boss.loop.policy import LoopBudget

        budget = LoopBudget(max_attempts=10, max_test_failures=3)
        engine = LoopEngine(
            task="dummy task",
            session_id="test-sess",
            budget=budget,
        )
        engine._state.total_test_failures = 3
        stop = engine._check_budget()
        self.assertIsNotNone(stop)
        self.assertEqual(stop.value, "max_failures")

    def test_no_stop_within_budget(self):
        from boss.loop.engine import LoopEngine
        from boss.loop.policy import LoopBudget

        budget = LoopBudget(max_attempts=5, max_commands=30)
        engine = LoopEngine(
            task="dummy task",
            session_id="test-sess",
            budget=budget,
        )
        stop = engine._check_budget()
        self.assertIsNone(stop)


class TestLoopResultParsing(unittest.TestCase):
    """Parsing LOOP_RESULT directives from assistant output."""

    def test_success(self):
        from boss.loop.engine import _parse_loop_result
        self.assertEqual(_parse_loop_result("Everything works!\nLOOP_RESULT:SUCCESS"), "success")

    def test_retry(self):
        from boss.loop.engine import _parse_loop_result
        self.assertEqual(_parse_loop_result("Still failing\nLOOP_RESULT:RETRY"), "retry")

    def test_stop(self):
        from boss.loop.engine import _parse_loop_result
        self.assertEqual(_parse_loop_result("Can't fix this\nLOOP_RESULT:STOP"), "stop")

    def test_none(self):
        from boss.loop.engine import _parse_loop_result
        self.assertIsNone(_parse_loop_result("Just some regular text without a result"))

    def test_case_insensitive(self):
        from boss.loop.engine import _parse_loop_result
        self.assertEqual(_parse_loop_result("loop_result:success"), "success")


class TestLoopMicroPlanExtraction(unittest.TestCase):
    """Extract numbered steps from assistant output."""

    def test_extract_steps(self):
        from boss.loop.engine import _extract_micro_plan
        text = "Here's my plan:\n1. Edit foo.py\n2. Run tests\n3. Check output"
        steps = _extract_micro_plan(text)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0], "Edit foo.py")
        self.assertEqual(steps[1], "Run tests")
        self.assertEqual(steps[2], "Check output")

    def test_no_steps(self):
        from boss.loop.engine import _extract_micro_plan
        steps = _extract_micro_plan("Just some text without numbered steps.")
        self.assertEqual(steps, [])

    def test_caps_at_ten(self):
        from boss.loop.engine import _extract_micro_plan
        text = "\n".join(f"{i}. step {i}" for i in range(1, 20))
        steps = _extract_micro_plan(text)
        self.assertEqual(len(steps), 10)


class TestLoopExecutionStyle(unittest.TestCase):
    """ExecutionStyle enum."""

    def test_values(self):
        from boss.loop.policy import ExecutionStyle
        self.assertEqual(ExecutionStyle.SINGLE_PASS.value, "single_pass")
        self.assertEqual(ExecutionStyle.ITERATIVE.value, "iterative")

    def test_default_is_single_pass(self):
        """ChatRequest defaults to single_pass for backward compatibility."""
        from boss.loop.policy import ExecutionStyle
        self.assertEqual(ExecutionStyle.SINGLE_PASS, ExecutionStyle("single_pass"))


class TestLoopIterationPrompt(unittest.TestCase):
    """The iteration prompt builder."""

    def test_first_attempt_includes_instructions(self):
        from boss.loop.engine import _build_iteration_prompt
        prompt = _build_iteration_prompt(
            task="Fix the tests",
            attempt_number=1,
            micro_plan=[],
            prior_attempts=[],
            phase="plan",
        )
        self.assertIn("LOOP_RESULT:SUCCESS", prompt)
        self.assertIn("Fix the tests", prompt)

    def test_retry_includes_prior_attempt_context(self):
        from boss.loop.engine import _build_iteration_prompt
        from boss.loop.state import LoopAttempt
        prior = LoopAttempt(
            attempt_number=1,
            started_at=0,
            finished_at=1,
            test_output_tail="AssertionError: 1 != 2",
        )
        prompt = _build_iteration_prompt(
            task="Fix the tests",
            attempt_number=2,
            micro_plan=["edit foo.py"],
            prior_attempts=[prior],
            phase="edit",
        )
        self.assertIn("LOOP_RESULT:RETRY", prompt)
        self.assertIn("AssertionError: 1 != 2", prompt)
        self.assertIn("edit foo.py", prompt)


class TestLoopStateResume(unittest.TestCase):
    """LoopEngine resume from saved state."""

    def test_resume_clears_pending_run_id(self):
        import time
        from boss.loop.engine import LoopEngine
        from boss.loop.policy import LoopBudget
        from boss.loop.state import LoopState

        state = LoopState(
            loop_id="resume-test",
            session_id="s1",
            task_description="Fix bug",
            budget=LoopBudget().to_dict(),
            execution_style="iterative",
            started_at=time.time(),
            pending_run_id="old-run-id",
        )

        engine = LoopEngine(
            task="Fix bug",
            session_id="s1",
            budget=LoopBudget(),
            resume_state=state,
        )
        self.assertIsNone(engine.state.pending_run_id)
        self.assertEqual(engine.state.loop_id, "resume-test")


class TestLoopDoneEventSuppression(unittest.TestCase):
    """Verify done events are suppressed during loop iterations."""

    def test_engine_suppresses_done_keyword(self):
        """The inner loop uses 'continue' for done events, not 'pass'."""
        import inspect
        from boss.loop.engine import LoopEngine

        source = inspect.getsource(LoopEngine.run)
        # Find the done event branch — it must use 'continue' to skip yield
        lines = source.split("\n")
        in_done_branch = False
        for line in lines:
            stripped = line.strip()
            if 'event_type == "done"' in stripped:
                in_done_branch = True
                continue
            if in_done_branch:
                if stripped == "continue":
                    break
                if stripped.startswith("elif ") or stripped.startswith("else:"):
                    self.fail("done event branch does not use 'continue'")
        else:
            self.fail("Could not find done event branch in LoopEngine.run")


class TestPendingRunLoopId(unittest.TestCase):
    """PendingRun persists and restores loop_id."""

    def test_save_and_load_with_loop_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(pending_runs_dir=Path(tmpdir)):
                run_id = save_pending_run(
                    session_id="s-loop",
                    state={"test": True},
                    approvals=[],
                    mode="agent",
                    loop_id="loop-abc123",
                )
                record = load_pending_run(run_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.loop_id, "loop-abc123")

    def test_save_and_load_without_loop_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(pending_runs_dir=Path(tmpdir)):
                run_id = save_pending_run(
                    session_id="s-no-loop",
                    state={"test": True},
                    approvals=[],
                    mode="agent",
                )
                record = load_pending_run(run_id)
                self.assertIsNotNone(record)
                self.assertIsNone(record.loop_id)


class TestBackgroundJobRecordLoopFields(unittest.TestCase):
    """BackgroundJobRecord persists execution_style, loop_budget, loop_id."""

    def test_create_and_load_with_loop_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            logs_dir = Path(tmpdir) / "logs"
            with override_settings(jobs_dir=jobs_dir, job_logs_dir=logs_dir):
                budget = {"max_attempts": 5, "max_commands": 30}
                job = create_background_job(
                    prompt="Fix the tests",
                    mode="agent",
                    session_id="s-loop-job",
                    project_path="/tmp/proj",
                    initial_input_kind="prepared_input",
                    initial_input_payload=[],
                    execution_style="iterative",
                    loop_budget=budget,
                    loop_id="loop-xyz",
                )
                loaded = load_background_job(job.job_id)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.execution_style, "iterative")
                self.assertEqual(loaded.loop_budget, budget)
                self.assertEqual(loaded.loop_id, "loop-xyz")

    def test_backward_compat_no_loop_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            logs_dir = Path(tmpdir) / "logs"
            with override_settings(jobs_dir=jobs_dir, job_logs_dir=logs_dir):
                job = create_background_job(
                    prompt="Simple task",
                    mode="agent",
                    session_id="s-simple",
                    project_path="/tmp/proj",
                    initial_input_kind="prepared_input",
                    initial_input_payload=[],
                )
                loaded = load_background_job(job.job_id)
                self.assertIsNotNone(loaded)
                self.assertIsNone(loaded.execution_style)
                self.assertIsNone(loaded.loop_budget)
                self.assertIsNone(loaded.loop_id)


# ---------------------------------------------------------------------------
# CORS configuration
# ---------------------------------------------------------------------------


class TestCORSConfiguration(unittest.TestCase):
    """Verify CORS is no longer wildcarded and is configurable."""

    def test_default_cors_origins_are_local(self):
        origins = settings.cors_allowed_origins
        self.assertIsInstance(origins, tuple)
        self.assertGreater(len(origins), 0)
        for origin in origins:
            self.assertTrue(
                origin.startswith("http://localhost")
                or origin.startswith("http://127.0.0.1")
                or origin.startswith("tauri://"),
                f"Unexpected default origin: {origin}",
            )

    def test_wildcard_not_in_defaults(self):
        self.assertNotIn("*", settings.cors_allowed_origins)

    def test_cors_origins_override_via_env(self):
        with override_settings(
            cors_allowed_origins=("http://custom:9000", "http://other:3000"),
        ):
            self.assertEqual(
                settings.cors_allowed_origins,
                ("http://custom:9000", "http://other:3000"),
            )

    def test_cors_in_system_status(self):
        """The system status endpoint should surface resolved CORS origins."""
        try:
            from boss.api import system_status
        except RuntimeError:
            self.skipTest("API server lock held; cannot import boss.api in-process")

        result = asyncio.run(system_status())
        self.assertIn("cors_allowed_origins", result)
        self.assertIsInstance(result["cors_allowed_origins"], list)
        self.assertNotIn("*", result["cors_allowed_origins"])


# ---------------------------------------------------------------------------
# MCP server version pinning
# ---------------------------------------------------------------------------


class TestMCPServerPinning(unittest.TestCase):
    """Verify MCP server commands use pinned versions, not @latest."""

    def test_apple_mcp_pinned(self):
        from boss.mcp.servers import create_apple_mcp, _APPLE_MCP_VERSION

        server = create_apple_mcp()
        args = server.params.args
        pkg_arg = args[1]
        self.assertNotIn("@latest", pkg_arg)
        self.assertIn(f"@{_APPLE_MCP_VERSION}", pkg_arg)

    def test_filesystem_mcp_pinned(self):
        from boss.mcp.servers import create_filesystem_mcp, _MCP_FILESYSTEM_VERSION

        server = create_filesystem_mcp()
        args = server.params.args
        pkg_arg = args[1]
        self.assertNotIn("@latest", pkg_arg)
        self.assertIn(f"@{_MCP_FILESYSTEM_VERSION}", pkg_arg)

    def test_memory_mcp_pinned(self):
        from boss.mcp.servers import create_memory_mcp, _MCP_MEMORY_VERSION

        server = create_memory_mcp()
        args = server.params.args
        pkg_arg = args[1]
        self.assertNotIn("@latest", pkg_arg)
        self.assertIn(f"@{_MCP_MEMORY_VERSION}", pkg_arg)

    def test_no_latest_in_any_server(self):
        from boss.mcp.servers import create_mcp_servers

        servers = create_mcp_servers()
        for name, server in servers.items():
            for arg in server.params.args:
                self.assertNotIn(
                    "@latest", arg,
                    f"MCP server '{name}' still uses @latest: {arg}",
                )


# ---------------------------------------------------------------------------
# Tool parameter extraction
# ---------------------------------------------------------------------------


class TestExtractToolParameters(unittest.TestCase):
    """Verify extract_tool_parameters raises on unparseable input."""

    def _import(self):
        from boss.execution import (
            extract_tool_parameters,
            ToolParameterExtractionError,
        )
        return extract_tool_parameters, ToolParameterExtractionError

    def test_dict_arguments(self):
        extract, _ = self._import()
        result = extract({"arguments": {"path": "/tmp", "recursive": True}})
        self.assertEqual(result, {"path": "/tmp", "recursive": True})

    def test_json_string_arguments(self):
        extract, _ = self._import()
        result = extract({"arguments": '{"name": "test"}'})
        self.assertEqual(result, {"name": "test"})

    def test_plain_string_arguments(self):
        extract, _ = self._import()
        result = extract({"arguments": "some plain text"})
        self.assertEqual(result, {"value": "some plain text"})

    def test_query_field(self):
        extract, _ = self._import()
        result = extract({"query": "search term"})
        self.assertEqual(result, {"query": "search term"})

    def test_empty_dict_raises(self):
        extract, Error = self._import()
        with self.assertRaises(Error):
            extract({})

    def test_dict_with_irrelevant_keys_raises(self):
        extract, Error = self._import()
        with self.assertRaises(Error):
            extract({"foo": "bar", "baz": 42})

    def test_none_raises(self):
        extract, Error = self._import()
        with self.assertRaises(Error):
            extract(None)

    def test_plain_object_no_attrs_raises(self):
        extract, Error = self._import()
        with self.assertRaises(Error):
            extract(object())

    def test_error_is_value_error_subclass(self):
        _, Error = self._import()
        self.assertTrue(issubclass(Error, ValueError))

    def test_object_with_dict_arguments(self):
        extract, _ = self._import()

        class FakeItem:
            arguments = {"key": "value"}

        result = extract(FakeItem())
        self.assertEqual(result, {"key": "value"})

    def test_object_with_query(self):
        extract, _ = self._import()

        class FakeItem:
            query = "hello"

        result = extract(FakeItem())
        self.assertEqual(result, {"query": "hello"})

    def test_object_without_args_or_query_raises(self):
        extract, Error = self._import()

        class FakeItem:
            name = "irrelevant"

        with self.assertRaises(Error):
            extract(FakeItem())


class TestBuildToolDisplayHandlesExtractionFailure(unittest.TestCase):
    """Verify build_tool_display degrades gracefully on extraction failure."""

    def test_unknown_tool_with_bad_item_does_not_crash(self):
        from boss.execution import build_tool_display

        title, desc, exec_type, scope_key, scope_label = build_tool_display(
            "some_unknown_tool", object()
        )
        self.assertIsInstance(title, str)
        self.assertIsInstance(desc, str)
        self.assertEqual(scope_key, "any")

    def test_transfer_tool_with_bad_item_does_not_crash(self):
        from boss.execution import build_tool_display

        title, desc, exec_type, scope_key, scope_label = build_tool_display(
            "transfer_to_mac_agent", object()
        )
        self.assertEqual(title, "Route")
        self.assertIn("Mac Agent", desc)


if __name__ == "__main__":
    unittest.main()