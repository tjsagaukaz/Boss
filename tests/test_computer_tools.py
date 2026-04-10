"""Tests for boss/tools/computer.py — governed computer-use tools."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, ExecutionType, get_tool_metadata


class TestToolRegistration(unittest.TestCase):
    """All 6 computer tools are registered with correct metadata."""

    def test_all_tools_have_metadata(self):
        from boss.tools.computer import (
            computer_session_status,
            computer_take_screenshot,
            pause_computer_session,
            resume_computer_session,
            start_computer_session,
            stop_computer_session,
        )

        tools = [
            start_computer_session,
            computer_session_status,
            pause_computer_session,
            resume_computer_session,
            stop_computer_session,
            computer_take_screenshot,
        ]
        for tool in tools:
            name = getattr(tool, "name", None)
            self.assertIsNotNone(name, f"Tool {tool} missing name attribute")
            meta = get_tool_metadata(name)
            self.assertIsNotNone(meta, f"No metadata registered for {name}")

    def test_start_is_run_type(self):
        meta = get_tool_metadata("start_computer_session")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.RUN)

    def test_status_is_read_type(self):
        meta = get_tool_metadata("computer_session_status")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.READ)

    def test_pause_is_run_type(self):
        meta = get_tool_metadata("pause_computer_session")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.RUN)

    def test_resume_is_run_type(self):
        meta = get_tool_metadata("resume_computer_session")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.RUN)

    def test_stop_is_run_type(self):
        meta = get_tool_metadata("stop_computer_session")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.RUN)

    def test_screenshot_is_read_type(self):
        meta = get_tool_metadata("computer_take_screenshot")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.READ)

    def test_run_tools_need_approval(self):
        """RUN-type computer tools should require approval."""
        run_tools = [
            "start_computer_session",
            "pause_computer_session",
            "resume_computer_session",
            "stop_computer_session",
        ]
        for name in run_tools:
            meta = get_tool_metadata(name)
            self.assertIsNotNone(meta)
            self.assertNotIn(
                meta.execution_type,
                AUTO_ALLOWED_EXECUTION_TYPES,
                f"{name} should not be auto-allowed",
            )

    def test_read_tools_are_auto_allowed(self):
        """READ-type computer tools should be auto-allowed."""
        read_tools = ["computer_session_status", "computer_take_screenshot"]
        for name in read_tools:
            meta = get_tool_metadata(name)
            self.assertIsNotNone(meta)
            self.assertIn(
                meta.execution_type,
                AUTO_ALLOWED_EXECUTION_TYPES,
                f"{name} should be auto-allowed",
            )

    def test_scope_key_functions(self):
        """Each tool's scope_key function should return a valid scope string."""
        tools_and_params = [
            ("start_computer_session", {"url": "https://example.com"}),
            ("computer_session_status", {}),
            ("pause_computer_session", {"session_id": "abc"}),
            ("resume_computer_session", {"session_id": "abc"}),
            ("stop_computer_session", {"session_id": "abc"}),
            ("computer_take_screenshot", {"session_id": "abc"}),
        ]
        for name, params in tools_and_params:
            meta = get_tool_metadata(name)
            scope = meta.scope_key(params)
            self.assertTrue(scope.startswith("computer:"), f"{name} scope should start with 'computer:'")


class TestModeFiltering(unittest.TestCase):
    """Computer RUN tools excluded in read-only modes, included in agent mode."""

    def test_agent_mode_includes_all_computer_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="agent")
        names = {tool.name for tool in agent.tools}
        self.assertIn("start_computer_session", names)
        self.assertIn("computer_session_status", names)
        self.assertIn("pause_computer_session", names)
        self.assertIn("resume_computer_session", names)
        self.assertIn("stop_computer_session", names)
        self.assertIn("computer_take_screenshot", names)

    def test_ask_mode_excludes_run_computer_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="ask")
        names = {tool.name for tool in agent.tools}
        self.assertNotIn("start_computer_session", names)
        self.assertNotIn("pause_computer_session", names)
        self.assertNotIn("resume_computer_session", names)
        self.assertNotIn("stop_computer_session", names)
        # READ tools should still be present
        self.assertIn("computer_session_status", names)
        self.assertIn("computer_take_screenshot", names)

    def test_plan_mode_excludes_run_computer_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="plan")
        names = {tool.name for tool in agent.tools}
        self.assertNotIn("start_computer_session", names)
        self.assertNotIn("pause_computer_session", names)

    def test_review_mode_excludes_run_computer_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="review")
        names = {tool.name for tool in agent.tools}
        self.assertNotIn("start_computer_session", names)
        self.assertNotIn("stop_computer_session", names)

    def test_tool_count_agent_vs_ask(self):
        """Agent mode should have more tools than ask mode (due to RUN tools)."""
        from boss.agents import build_entry_agent

        agent_tools = build_entry_agent(mode="agent")
        ask_tools = build_entry_agent(mode="ask")
        self.assertGreater(len(agent_tools.tools), len(ask_tools.tools))


class TestPromptHints(unittest.TestCase):
    """Prompt hints appear when computer tools are available."""

    def test_hints_present_when_start_tool_available(self):
        from boss.prompting.modes import general_tool_hints

        hints = general_tool_hints({"start_computer_session", "computer_session_status"})
        self.assertIn("start_computer_session", hints)
        self.assertIn("browser automation", hints)

    def test_hints_absent_without_computer_tools(self):
        from boss.prompting.modes import general_tool_hints

        hints = general_tool_hints({"recall", "read_file"})
        self.assertNotIn("start_computer_session", hints)

    def test_read_only_hint_for_status_only(self):
        from boss.prompting.modes import general_tool_hints

        hints = general_tool_hints({"computer_session_status"})
        self.assertIn("computer_session_status", hints)
        self.assertIn("read-only", hints)
        self.assertNotIn("start_computer_session", hints)

    def test_control_tools_mentioned(self):
        from boss.prompting.modes import general_tool_hints

        hints = general_tool_hints({
            "start_computer_session",
            "computer_session_status",
            "pause_computer_session",
            "resume_computer_session",
            "stop_computer_session",
            "computer_take_screenshot",
        })
        self.assertIn("pause_computer_session", hints)
        self.assertIn("resume_computer_session", hints)
        self.assertIn("stop_computer_session", hints)
        self.assertIn("computer_take_screenshot", hints)

    def test_preview_distinction_mentioned(self):
        from boss.prompting.modes import general_tool_hints

        hints = general_tool_hints({"start_computer_session"})
        self.assertIn("capture_preview", hints)
        self.assertIn("start_preview_server", hints)


class TestToolNames(unittest.TestCase):
    """Verify tool names match expected conventions."""

    def test_tool_names_match_function_names(self):
        from boss.tools.computer import (
            computer_session_status,
            computer_take_screenshot,
            pause_computer_session,
            resume_computer_session,
            start_computer_session,
            stop_computer_session,
        )

        expected = {
            "start_computer_session": start_computer_session,
            "computer_session_status": computer_session_status,
            "pause_computer_session": pause_computer_session,
            "resume_computer_session": resume_computer_session,
            "stop_computer_session": stop_computer_session,
            "computer_take_screenshot": computer_take_screenshot,
        }
        for name, tool in expected.items():
            self.assertEqual(tool.name, name)

    def test_all_tools_in_boss_tools_list(self):
        from boss.agents import _BOSS_TOOLS

        boss_names = {getattr(t, "name", "") for t in _BOSS_TOOLS}
        computer_names = {
            "start_computer_session",
            "computer_session_status",
            "pause_computer_session",
            "resume_computer_session",
            "stop_computer_session",
            "computer_take_screenshot",
        }
        for name in computer_names:
            self.assertIn(name, boss_names, f"{name} missing from _BOSS_TOOLS")


# ---------------------------------------------------------------------------
# Bug-fix coverage — the three runtime gaps found in review
# ---------------------------------------------------------------------------


class TestStartReturnsFullSessionId(unittest.TestCase):
    """start_computer_session must return the full session ID so later tools
    can load it by exact filename."""

    @patch("boss.computer.engine.run_session")
    @patch("boss.computer.engine.validate_target_domain", return_value=(True, ""))
    @patch("boss.computer.engine.create_session")
    @patch("boss.config.settings")
    def test_full_id_in_output(self, mock_settings, mock_create, _mock_validate, _mock_run):
        from boss.computer.state import ComputerSession
        from boss.tools.computer import _start_computer_session_impl

        mock_settings.computer_use_enabled = True
        mock_settings.computer_use_max_turns = 50

        fake_session = ComputerSession(
            session_id="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
            target_url="https://example.com",
            status="created",
        )
        mock_create.return_value = fake_session

        result = _start_computer_session_impl(url="https://example.com", task="test")

        # Full 32-char hex ID must appear, not just the first 12
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", result)

    @patch("boss.computer.engine.run_session")
    @patch("boss.computer.engine.validate_target_domain", return_value=(True, ""))
    @patch("boss.computer.engine.create_session")
    @patch("boss.config.settings")
    def test_id_loadable_by_state(self, mock_settings, mock_create, _mock_validate, _mock_run):
        """The ID returned by start should match the filename load_session uses."""
        from boss.computer.state import ComputerSession
        from boss.tools.computer import _start_computer_session_impl

        mock_settings.computer_use_enabled = True
        mock_settings.computer_use_max_turns = 50

        sid = "deadbeef" * 4
        fake_session = ComputerSession(session_id=sid, target_url="https://x.com")
        mock_create.return_value = fake_session

        result = _start_computer_session_impl(url="https://x.com")

        # The full session_id is present and could be fed back to load_session
        self.assertIn(sid, result)

    @patch("boss.config.settings")
    def test_disabled_returns_message(self, mock_settings):
        from boss.tools.computer import _start_computer_session_impl

        mock_settings.computer_use_enabled = False
        result = _start_computer_session_impl(url="https://example.com")
        self.assertIn("disabled", result)

    @patch("boss.computer.engine.validate_target_domain", return_value=(False, "not allowed"))
    @patch("boss.config.settings")
    def test_domain_not_allowed(self, mock_settings, _mock_validate):
        from boss.tools.computer import _start_computer_session_impl

        mock_settings.computer_use_enabled = True
        result = _start_computer_session_impl(url="https://evil.com")
        self.assertIn("not allowed", result)


class TestResumeRelaunches(unittest.TestCase):
    """resume_computer_session must relaunch the background loop, not just
    clear an in-memory flag."""

    def _make_paused_session(self, tmp_dir, sid="abc123def456abc123def456abc123de"):
        from boss.computer.state import ComputerSession, SessionStatus

        session = ComputerSession(
            session_id=sid,
            target_url="https://example.com",
            status=SessionStatus.PAUSED,
            pause_requested=True,
        )
        sessions_dir = os.path.join(tmp_dir, "computer", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        dest = os.path.join(sessions_dir, f"{sid}.json")
        with open(dest, "w") as f:
            json.dump(session.to_dict(), f)
        return session

    @patch("boss.computer.engine.run_session")
    @patch("boss.config.settings")
    def test_resume_starts_background_thread(self, mock_settings, mock_run_session):
        """After resume, run_session should be called on a background thread."""
        from pathlib import Path

        from boss.tools.computer import _resume_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        mock_settings.computer_use_max_turns = 50

        with tempfile.TemporaryDirectory() as tmp_dir:
            self._make_paused_session(tmp_dir, sid)
            mock_settings.app_data_dir = Path(tmp_dir)

            result = _resume_computer_session_impl(session_id=sid)

            self.assertIn("resumed", result)
            # Give the daemon thread a moment to call run_session
            time.sleep(0.15)
            self.assertTrue(
                mock_run_session.called,
                "run_session should be called on the background thread",
            )

    @patch("boss.computer.engine.run_session")
    @patch("boss.config.settings")
    def test_resume_resets_persisted_paused_state(self, mock_settings, mock_run_session):
        """After resume, the saved session should no longer be paused."""
        from pathlib import Path

        from boss.computer.state import load_session
        from boss.tools.computer import _resume_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        mock_settings.computer_use_max_turns = 50

        with tempfile.TemporaryDirectory() as tmp_dir:
            self._make_paused_session(tmp_dir, sid)
            mock_settings.app_data_dir = Path(tmp_dir)

            _resume_computer_session_impl(session_id=sid)

            # Reload from disk and verify
            reloaded = load_session(sid)

            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, "created")
            self.assertFalse(reloaded.pause_requested)

    def test_resume_rejects_terminal_session(self):
        """Resume should refuse to restart a completed session."""
        from pathlib import Path

        from boss.computer.state import ComputerSession, SessionStatus
        from boss.tools.computer import _resume_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = os.path.join(tmp_dir, "computer", "sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            session = ComputerSession(
                session_id=sid,
                status=SessionStatus.COMPLETED,
            )
            with open(os.path.join(sessions_dir, f"{sid}.json"), "w") as f:
                json.dump(session.to_dict(), f)

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                result = _resume_computer_session_impl(session_id=sid)

            self.assertIn("already", result)

    def test_resume_rejects_running_session(self):
        """Resume should refuse a session that is currently running."""
        from pathlib import Path

        from boss.computer.state import ComputerSession, SessionStatus
        from boss.tools.computer import _resume_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = os.path.join(tmp_dir, "computer", "sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            session = ComputerSession(
                session_id=sid,
                status=SessionStatus.RUNNING,
            )
            with open(os.path.join(sessions_dir, f"{sid}.json"), "w") as f:
                json.dump(session.to_dict(), f)

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                result = _resume_computer_session_impl(session_id=sid)

            self.assertIn("running", result)


class TestStopPersistsCancellation(unittest.TestCase):
    """stop_computer_session must persist status=cancelled for sessions not
    inside an active loop (paused, waiting_approval, created)."""

    def _write_session(self, tmp_dir, sid, status):
        from boss.computer.state import ComputerSession

        sessions_dir = os.path.join(tmp_dir, "computer", "sessions")
        events_dir = os.path.join(tmp_dir, "computer", "events")
        os.makedirs(sessions_dir, exist_ok=True)
        os.makedirs(events_dir, exist_ok=True)
        session = ComputerSession(session_id=sid, status=status)
        with open(os.path.join(sessions_dir, f"{sid}.json"), "w") as f:
            json.dump(session.to_dict(), f)
        return session

    @patch("boss.computer.engine.cancel_session")
    def test_stop_paused_persists_cancelled(self, mock_cancel):
        from pathlib import Path

        from boss.computer.state import load_session
        from boss.tools.computer import _stop_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_session(tmp_dir, sid, "paused")

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                result = _stop_computer_session_impl(session_id=sid)

                reloaded = load_session(sid)

            self.assertIn("cancelled", result)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, "cancelled")
            self.assertTrue(reloaded.is_terminal)

    @patch("boss.computer.engine.cancel_session")
    def test_stop_waiting_approval_persists_cancelled(self, mock_cancel):
        from pathlib import Path

        from boss.computer.state import load_session
        from boss.tools.computer import _stop_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_session(tmp_dir, sid, "waiting_approval")

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                result = _stop_computer_session_impl(session_id=sid)

                reloaded = load_session(sid)

            self.assertEqual(reloaded.status, "cancelled")

    @patch("boss.computer.engine.cancel_session")
    def test_stop_created_persists_cancelled(self, mock_cancel):
        from pathlib import Path

        from boss.computer.state import load_session
        from boss.tools.computer import _stop_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_session(tmp_dir, sid, "created")

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                result = _stop_computer_session_impl(session_id=sid)

                reloaded = load_session(sid)

            self.assertEqual(reloaded.status, "cancelled")

    @patch("boss.computer.engine.cancel_session")
    def test_stop_running_defers_to_loop(self, mock_cancel):
        """For a running session, stop should set the flag but NOT persist
        cancellation — that's the loop's job via _check_cancelled."""
        from pathlib import Path

        from boss.computer.state import load_session
        from boss.tools.computer import _stop_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_session(tmp_dir, sid, "running")

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                result = _stop_computer_session_impl(session_id=sid)

                reloaded = load_session(sid)

            # The persisted status should still be running — the loop will cancel it
            self.assertEqual(reloaded.status, "running")
            # But cancel_session must have been called for the in-memory flag
            mock_cancel.assert_called_once_with(sid)

    @patch("boss.computer.engine.cancel_session")
    def test_stop_writes_event(self, mock_cancel):
        """Immediate cancellation should write a cancellation event."""
        from pathlib import Path

        from boss.tools.computer import _stop_computer_session_impl

        sid = "abc123def456abc123def456abc123de"
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_session(tmp_dir, sid, "paused")

            with patch("boss.config.settings") as mock_state_settings:
                mock_state_settings.app_data_dir = Path(tmp_dir)
                _stop_computer_session_impl(session_id=sid)

                # Check event log
                events_path = os.path.join(
                    tmp_dir, "computer", "events", f"{sid}.jsonl"
                )
                self.assertTrue(os.path.exists(events_path))
                with open(events_path) as f:
                    events = [json.loads(line) for line in f]
                self.assertTrue(
                    any(e["event"] == "cancelled" for e in events),
                    "Expected a 'cancelled' event in the log",
                )


if __name__ == "__main__":
    unittest.main()
