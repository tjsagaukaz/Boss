from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from boss.agents import build_entry_agent
from boss.config import settings
from boss.context.manager import SessionContextManager
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


class RegressionHarnessTests(unittest.TestCase):
    def test_entry_agent_uses_general_entrypoint_and_expected_handoffs(self):
        entry_agent = build_entry_agent(active_mcp_servers={})

        self.assertEqual(entry_agent.name, "general")
        self.assertEqual([agent.name for agent in entry_agent.handoffs], ["mac", "research", "reasoning", "code"])

        tool_names = [tool.name for tool in entry_agent.tools]
        self.assertIn("remember", tool_names)
        self.assertIn("recall", tool_names)
        self.assertIn("search_project_content", tool_names)

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


if __name__ == "__main__":
    unittest.main()