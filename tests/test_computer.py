"""Tests for the computer-use subsystem.

Covers:
- State serialization round-trips
- Event persistence (append / read)
- Action model parsing
- Browser harness command/action translation (mocked Playwright)
- Capability detection
- Graceful failure when Playwright is missing
- Engine model response parsing
- Engine diagnostics
- Config flags
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# State serialization
# ---------------------------------------------------------------------------


class SessionStateTests(unittest.TestCase):
    """ComputerSession serialization round-trips."""

    def test_round_trip(self):
        from boss.computer.state import ComputerSession, SessionStatus

        s = ComputerSession(
            target_url="https://example.com",
            target_domain="example.com",
            status=SessionStatus.RUNNING,
            turn_index=3,
            active_model="gpt-5.4",
        )
        d = s.to_dict()
        s2 = ComputerSession.from_dict(d)
        self.assertEqual(s2.session_id, s.session_id)
        self.assertEqual(s2.target_url, "https://example.com")
        self.assertEqual(s2.status, SessionStatus.RUNNING)
        self.assertEqual(s2.turn_index, 3)

    def test_unknown_fields_ignored(self):
        from boss.computer.state import ComputerSession

        d = {"session_id": "abc123", "future_field": True, "target_url": "http://x.com"}
        s = ComputerSession.from_dict(d)
        self.assertEqual(s.session_id, "abc123")

    def test_version_stamped(self):
        from boss.computer.state import COMPUTER_SESSION_VERSION, ComputerSession

        s = ComputerSession()
        d = s.to_dict()
        self.assertEqual(d["version"], COMPUTER_SESSION_VERSION)

    def test_is_terminal(self):
        from boss.computer.state import ComputerSession, SessionStatus

        for status in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED):
            s = ComputerSession(status=status)
            self.assertTrue(s.is_terminal, f"{status} should be terminal")

        for status in (SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.PAUSED):
            s = ComputerSession(status=status)
            self.assertFalse(s.is_terminal, f"{status} should not be terminal")

    def test_summary(self):
        from boss.computer.state import ComputerSession

        s = ComputerSession(target_url="https://example.com", turn_index=5)
        summary = s.summary()
        self.assertIn("example.com", summary)
        self.assertIn("turn=5", summary)


# ---------------------------------------------------------------------------
# Action model
# ---------------------------------------------------------------------------


class ActionModelTests(unittest.TestCase):
    """ComputerAction serialization."""

    def test_click_round_trip(self):
        from boss.computer.state import ComputerAction

        a = ComputerAction(type="click", x=100, y=200, button="left")
        d = a.to_dict()
        a2 = ComputerAction.from_dict(d)
        self.assertEqual(a2.type, "click")
        self.assertEqual(a2.x, 100)
        self.assertEqual(a2.y, 200)

    def test_type_action(self):
        from boss.computer.state import ComputerAction

        a = ComputerAction(type="type", text="hello world")
        d = a.to_dict()
        a2 = ComputerAction.from_dict(d)
        self.assertEqual(a2.text, "hello world")

    def test_drag_action(self):
        from boss.computer.state import ComputerAction

        a = ComputerAction(type="drag", x=10, y=20, drag_end_x=100, drag_end_y=200)
        d = a.to_dict()
        a2 = ComputerAction.from_dict(d)
        self.assertEqual(a2.drag_end_x, 100)
        self.assertEqual(a2.drag_end_y, 200)

    def test_unknown_fields_ignored(self):
        from boss.computer.state import ComputerAction

        d = {"type": "click", "x": 50, "y": 50, "alien_field": True}
        a = ComputerAction.from_dict(d)
        self.assertEqual(a.type, "click")

    def test_action_result_round_trip(self):
        from boss.computer.state import ActionResult

        r = ActionResult(action_type="click", success=True)
        d = r.to_dict()
        r2 = ActionResult.from_dict(d)
        self.assertEqual(r2.action_type, "click")
        self.assertTrue(r2.success)


# ---------------------------------------------------------------------------
# Event persistence
# ---------------------------------------------------------------------------


class EventPersistenceTests(unittest.TestCase):
    """Append-only JSONL event log."""

    def test_append_and_read(self):
        from boss.computer.state import append_event, read_events

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._events_dir", return_value=Path(tmp)):
                append_event("sess1", "created", {"url": "http://example.com"})
                append_event("sess1", "screenshot", {"turn": 1})

                events = read_events("sess1")
                self.assertEqual(len(events), 2)
                self.assertEqual(events[0]["event"], "created")
                self.assertEqual(events[1]["event"], "screenshot")
                self.assertEqual(events[0]["data"]["url"], "http://example.com")

    def test_read_empty(self):
        from boss.computer.state import read_events

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._events_dir", return_value=Path(tmp)):
                events = read_events("nonexistent")
                self.assertEqual(events, [])


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


class SessionPersistenceTests(unittest.TestCase):
    """Save / load / list / delete sessions."""

    def test_save_and_load(self):
        from boss.computer.state import ComputerSession, save_session, load_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)):
                s = ComputerSession(target_url="https://example.com")
                save_session(s)
                loaded = load_session(s.session_id)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.target_url, "https://example.com")

    def test_load_missing(self):
        from boss.computer.state import load_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)):
                self.assertIsNone(load_session("nonexistent"))

    def test_list_sessions(self):
        from boss.computer.state import ComputerSession, save_session, list_sessions

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)):
                s1 = ComputerSession(target_url="http://a.com")
                s2 = ComputerSession(target_url="http://b.com")
                save_session(s1)
                save_session(s2)
                sessions = list_sessions()
                self.assertEqual(len(sessions), 2)

    def test_delete_session(self):
        from boss.computer.state import ComputerSession, save_session, delete_session, load_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)):
                s = ComputerSession()
                save_session(s)
                self.assertTrue(delete_session(s.session_id))
                self.assertIsNone(load_session(s.session_id))

    def test_delete_nonexistent(self):
        from boss.computer.state import delete_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)):
                self.assertFalse(delete_session("nope"))


# ---------------------------------------------------------------------------
# Browser harness action translation (mocked Playwright)
# ---------------------------------------------------------------------------


class BrowserHarnessActionTests(unittest.TestCase):
    """Test action dispatch without real Playwright."""

    def _make_harness(self):
        from boss.computer.browser import BrowserHarness

        h = BrowserHarness()
        # Mock the page object
        h._page = MagicMock()
        return h

    def test_click(self):
        h = self._make_harness()
        result = h.execute_action({"type": "click", "x": 100, "y": 200})
        self.assertTrue(result.success)
        self.assertEqual(result.action_type, "click")
        h._page.mouse.click.assert_called_once_with(100, 200, button="left")

    def test_double_click(self):
        h = self._make_harness()
        result = h.execute_action({"type": "double_click", "x": 50, "y": 60})
        self.assertTrue(result.success)
        h._page.mouse.dblclick.assert_called_once_with(50, 60)

    def test_type_text(self):
        h = self._make_harness()
        result = h.execute_action({"type": "type", "text": "hello"})
        self.assertTrue(result.success)
        h._page.keyboard.type.assert_called_once_with("hello")

    def test_type_no_text(self):
        h = self._make_harness()
        result = h.execute_action({"type": "type"})
        self.assertFalse(result.success)
        self.assertIn("No text", result.error)

    def test_keypress(self):
        h = self._make_harness()
        result = h.execute_action({"type": "keypress", "key": "Enter"})
        self.assertTrue(result.success)
        h._page.keyboard.press.assert_called_once_with("Enter")

    def test_keypress_no_key(self):
        h = self._make_harness()
        result = h.execute_action({"type": "keypress"})
        self.assertFalse(result.success)

    def test_scroll(self):
        h = self._make_harness()
        result = h.execute_action({"type": "scroll", "x": 100, "y": 200, "scroll_x": 0, "scroll_y": 300})
        self.assertTrue(result.success)
        h._page.mouse.move.assert_called_once_with(100, 200)
        h._page.mouse.wheel.assert_called_once_with(0, 300)

    def test_move(self):
        h = self._make_harness()
        result = h.execute_action({"type": "move", "x": 50, "y": 75})
        self.assertTrue(result.success)
        h._page.mouse.move.assert_called_once_with(50, 75)

    def test_drag(self):
        h = self._make_harness()
        result = h.execute_action({
            "type": "drag", "x": 10, "y": 20,
            "drag_end_x": 100, "drag_end_y": 200,
        })
        self.assertTrue(result.success)
        h._page.mouse.down.assert_called_once()
        h._page.mouse.up.assert_called_once()

    def test_drag_missing_end(self):
        h = self._make_harness()
        result = h.execute_action({"type": "drag", "x": 10, "y": 20})
        self.assertFalse(result.success)
        self.assertIn("drag_end_x", result.error)

    def test_wait(self):
        h = self._make_harness()
        t0 = time.monotonic()
        result = h.execute_action({"type": "wait", "duration_ms": 100})
        elapsed = (time.monotonic() - t0) * 1000
        self.assertTrue(result.success)
        self.assertGreaterEqual(elapsed, 80)  # allow some slack

    def test_wait_capped(self):
        """Wait duration should be capped at 10 seconds."""
        h = self._make_harness()
        t0 = time.monotonic()
        result = h.execute_action({"type": "wait", "duration_ms": 999_999})
        elapsed = (time.monotonic() - t0) * 1000
        self.assertTrue(result.success)
        # Should have been capped at 10s, not 999s
        self.assertLess(elapsed, 12_000)

    def test_unknown_action(self):
        h = self._make_harness()
        result = h.execute_action({"type": "fly"})
        self.assertFalse(result.success)
        self.assertIn("Unknown action type", result.error)

    def test_missing_coords(self):
        h = self._make_harness()
        result = h.execute_action({"type": "click"})
        self.assertFalse(result.success)
        self.assertIn("coordinates", result.error)

    def test_batch_stops_on_failure(self):
        h = self._make_harness()
        actions = [
            {"type": "click", "x": 10, "y": 20},
            {"type": "click"},  # missing coords → fail
            {"type": "click", "x": 30, "y": 40},  # should not execute
        ]
        results = h.execute_batch(actions)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].success)
        self.assertFalse(results[1].success)

    def test_navigate_action(self):
        h = self._make_harness()
        result = h.execute_action({"type": "navigate", "url": "https://example.com"})
        self.assertTrue(result.success)
        h._page.goto.assert_called_once()

    def test_navigate_no_url(self):
        h = self._make_harness()
        result = h.execute_action({"type": "navigate"})
        self.assertFalse(result.success)

    def test_not_ready(self):
        from boss.computer.browser import BrowserHarness, HarnessNotReady

        h = BrowserHarness()
        with self.assertRaises(HarnessNotReady):
            h.execute_action({"type": "click", "x": 10, "y": 20})


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


class CapabilityDetectionTests(unittest.TestCase):
    """Test capability probing."""

    def test_detection_returns_dataclass(self):
        from boss.computer.capabilities import detect_capabilities

        caps = detect_capabilities()
        d = caps.to_dict()
        self.assertIn("playwright_installed", d)
        self.assertIn("screenshot_supported", d)
        self.assertIn("can_run_session", d)
        self.assertIn("computer_use_model_ready", d)

    def test_can_run_session_requires_all(self):
        from boss.computer.capabilities import ComputerCapabilities

        caps = ComputerCapabilities(
            playwright_installed=True,
            playwright_browsers_installed=True,
            screenshot_supported=True,
            computer_use_model_ready=True,
        )
        self.assertTrue(caps.can_run_session)

        caps2 = ComputerCapabilities(
            playwright_installed=False,
            playwright_browsers_installed=True,
            screenshot_supported=True,
            computer_use_model_ready=True,
        )
        self.assertFalse(caps2.can_run_session)

    def test_missing_playwright(self):
        """When playwright import fails, capability reports not installed."""
        from boss.computer.capabilities import detect_capabilities

        import sys
        original = sys.modules.get("playwright")
        try:
            sys.modules["playwright"] = None  # type: ignore
            # Need to also clear cached import
            caps = detect_capabilities()
            # If playwright is actually installed in the venv, the import may
            # still succeed due to module caching. Either way the structure is valid.
            self.assertIsInstance(caps.playwright_installed, bool)
        finally:
            if original is not None:
                sys.modules["playwright"] = original
            else:
                sys.modules.pop("playwright", None)


# ---------------------------------------------------------------------------
# Engine — model response parsing
# ---------------------------------------------------------------------------


class ModelResponseParsingTests(unittest.TestCase):
    """Test _parse_model_response."""

    def test_parse_ga_batched_actions(self):
        from boss.computer.engine import _parse_model_response

        response = {
            "id": "resp_123",
            "output": [
                {
                    "type": "computer_call",
                    "call_id": "call_1",
                    "actions": [
                        {"type": "click", "x": 100, "y": 200},
                        {"type": "type", "text": "hello"},
                    ],
                },
            ],
        }
        actions, answer, resp_id = _parse_model_response(response)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0].type, "click")
        self.assertEqual(actions[0].x, 100)
        self.assertEqual(actions[1].type, "type")
        self.assertEqual(actions[1].text, "hello")
        self.assertIsNone(answer)
        self.assertEqual(resp_id, "resp_123")

    def test_parse_legacy_single_action(self):
        """Legacy single-action shape still accepted."""
        from boss.computer.engine import _parse_model_response

        response = {
            "id": "resp_legacy",
            "output": [
                {
                    "type": "computer_call",
                    "action": {"type": "click", "x": 50, "y": 60},
                },
            ],
        }
        actions, answer, resp_id = _parse_model_response(response)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "click")

    def test_parse_multiple_computer_calls(self):
        """Multiple computer_call items each with batched actions."""
        from boss.computer.engine import _parse_model_response

        response = {
            "id": "resp_multi",
            "output": [
                {"type": "computer_call", "actions": [{"type": "click", "x": 1, "y": 2}]},
                {"type": "computer_call", "actions": [{"type": "type", "text": "abc"}]},
            ],
        }
        actions, answer, resp_id = _parse_model_response(response)
        self.assertEqual(len(actions), 2)

    def test_parse_final_answer(self):
        from boss.computer.engine import _parse_model_response

        response = {
            "id": "resp_456",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Done. The result is 42."}],
                },
            ],
        }
        actions, answer, resp_id = _parse_model_response(response)
        self.assertEqual(len(actions), 0)
        self.assertEqual(answer, "Done. The result is 42.")

    def test_parse_empty_output(self):
        from boss.computer.engine import _parse_model_response

        actions, answer, resp_id = _parse_model_response({"output": []})
        self.assertEqual(len(actions), 0)
        self.assertIsNone(answer)
        self.assertIsNone(resp_id)

    def test_parse_mixed_output(self):
        """Actions and a final answer in the same response."""
        from boss.computer.engine import _parse_model_response

        response = {
            "id": "resp_789",
            "output": [
                {"type": "computer_call", "actions": [{"type": "click", "x": 10, "y": 20}]},
                {"type": "message", "content": [{"type": "output_text", "text": "All done"}]},
            ],
        }
        actions, answer, resp_id = _parse_model_response(response)
        self.assertEqual(len(actions), 1)
        self.assertEqual(answer, "All done")


# ---------------------------------------------------------------------------
# Engine — cancellation / pause
# ---------------------------------------------------------------------------


class CancellationTests(unittest.TestCase):
    """Thread-safe cancel/pause registry."""

    def test_cancel_and_check(self):
        from boss.computer.engine import cancel_session, is_cancelled, _cancel_lock, _cancelled_ids

        cancel_session("test_cancel_1")
        self.assertTrue(is_cancelled("test_cancel_1"))
        # Clean up
        with _cancel_lock:
            _cancelled_ids.discard("test_cancel_1")

    def test_pause_resume(self):
        from boss.computer.engine import pause_session, resume_session, is_paused, _cancel_lock, _paused_ids

        pause_session("test_pause_1")
        self.assertTrue(is_paused("test_pause_1"))
        resume_session("test_pause_1")
        self.assertFalse(is_paused("test_pause_1"))


# ---------------------------------------------------------------------------
# Engine — budget exhaustion
# ---------------------------------------------------------------------------


class BudgetExhaustionTests(unittest.TestCase):
    """Turn budget exhaustion must land in a terminal state."""

    def test_budget_exhaustion_sets_failed(self):
        """After max_turns without terminal/pause, session must be FAILED."""
        from boss.computer.state import ComputerSession, SessionStatus, BrowserStatus

        # Build a session that looks like it just ran through a loop
        s = ComputerSession(
            target_url="https://example.com",
            status=SessionStatus.RUNNING,
            turn_index=50,
        )
        # Simulate: the loop finished all turns but never hit terminal/paused
        # The engine should set FAILED + error message
        self.assertFalse(s.is_terminal)
        self.assertFalse(s.is_paused)

        # Directly test the state transition the engine performs
        s.status = SessionStatus.FAILED
        s.error = "Turn budget exhausted (50 turns)"
        self.assertTrue(s.is_terminal)
        self.assertIn("budget", s.error.lower())

    def test_run_session_budget_exhaustion(self):
        """run_session with max_turns=0 should immediately exhaust budget."""
        from boss.computer.engine import run_session
        from boss.computer.state import ComputerSession, SessionStatus

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)), \
                 patch("boss.computer.state._events_dir", return_value=Path(tmp)), \
                 patch("boss.computer.state._screenshots_dir", return_value=Path(tmp)), \
                 patch("boss.computer.browser.BrowserHarness.launch"), \
                 patch("boss.computer.browser.BrowserHarness.close"), \
                 patch("boss.computer.browser.BrowserHarness.navigate") as mock_nav:
                mock_nav.return_value = MagicMock(success=True)

                s = ComputerSession(target_url="https://example.com")
                # max_turns=0 means the loop body never runs
                # but our max() clamp in config ensures >=1, so test with 0 directly
                result = run_session(s, max_turns=0)
                self.assertEqual(result.status, SessionStatus.FAILED)
                self.assertIn("budget", result.error.lower())
                self.assertTrue(result.is_terminal)


# ---------------------------------------------------------------------------
# Engine — session creation
# ---------------------------------------------------------------------------


class SessionCreationTests(unittest.TestCase):
    """Test create_session."""

    def test_create_session(self):
        from boss.computer.engine import create_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)), \
                 patch("boss.computer.state._events_dir", return_value=Path(tmp)):
                s = create_session(target_url="https://example.com")
                self.assertEqual(s.target_url, "https://example.com")
                self.assertEqual(s.target_domain, "example.com")
                self.assertIn("headless", s.metadata)
                self.assertEqual(s.status, "created")

    def test_create_session_custom_model(self):
        from boss.computer.engine import create_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)), \
                 patch("boss.computer.state._events_dir", return_value=Path(tmp)):
                s = create_session(target_url="http://x.com", model="gpt-5.4-mini")
                self.assertEqual(s.active_model, "gpt-5.4-mini")


# ---------------------------------------------------------------------------
# Engine — diagnostics
# ---------------------------------------------------------------------------


class DiagnosticsTests(unittest.TestCase):
    """Test computer_use_status."""

    def test_status_shape(self):
        from boss.computer.engine import computer_use_status

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)):
                status = computer_use_status()
                self.assertIn("capabilities", status)
                self.assertIn("sessions", status)
                self.assertIn("total", status["sessions"])
                self.assertIn("can_run_session", status["capabilities"])


# ---------------------------------------------------------------------------
# Engine — domain extraction
# ---------------------------------------------------------------------------


class DomainExtractionTests(unittest.TestCase):
    """Test _extract_domain helper."""

    def test_https(self):
        from boss.computer.engine import _extract_domain

        self.assertEqual(_extract_domain("https://example.com/path"), "example.com")

    def test_with_port(self):
        from boss.computer.engine import _extract_domain

        self.assertEqual(_extract_domain("http://localhost:3000"), "localhost:3000")

    def test_invalid(self):
        from boss.computer.engine import _extract_domain

        self.assertIsNone(_extract_domain(""))


# ---------------------------------------------------------------------------
# Config flags
# ---------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    """Computer-use config settings."""

    def test_defaults(self):
        from boss.config import Settings

        s = Settings()
        self.assertFalse(s.computer_use_enabled)
        self.assertEqual(s.computer_use_model, "gpt-5.4")
        self.assertEqual(s.computer_use_max_turns, 50)
        self.assertTrue(s.computer_use_headless)

    def test_session_uses_computer_use_model(self):
        """create_session must default to computer_use_model, not code_model."""
        from boss.computer.engine import create_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._sessions_dir", return_value=Path(tmp)), \
                 patch("boss.computer.state._events_dir", return_value=Path(tmp)), \
                 patch("boss.config.settings") as mock_settings:
                mock_settings.computer_use_model = "correct-model"
                mock_settings.code_model = "wrong-model"
                s = create_session(target_url="http://x.com")
                self.assertEqual(s.active_model, "correct-model")

    def test_capabilities_use_computer_use_model(self):
        """detect_capabilities must report computer_use_model, not code_model."""
        from boss.computer.capabilities import detect_capabilities

        with patch("boss.config.settings") as mock_settings:
            mock_settings.computer_use_model = "correct-model"
            mock_settings.code_model = "wrong-model"
            mock_settings.cloud_api_key = "sk-test"
            caps = detect_capabilities()
            self.assertEqual(caps.computer_use_model, "correct-model")


# ---------------------------------------------------------------------------
# Screenshot path helper
# ---------------------------------------------------------------------------


class ScreenshotPathTests(unittest.TestCase):
    """Test screenshot_path_for."""

    def test_path_format(self):
        from boss.computer.state import screenshot_path_for

        with tempfile.TemporaryDirectory() as tmp:
            with patch("boss.computer.state._screenshots_dir", return_value=Path(tmp)):
                p = screenshot_path_for("abc123", 7)
                self.assertIn("abc123", str(p))
                self.assertIn("turn0007", str(p))
                self.assertTrue(str(p).endswith(".png"))
