"""Tests for boss.preview subsystem."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from boss.preview.session import (
    CaptureRegion,
    CaptureResult,
    DetailMode,
    PreviewCapabilities,
    PreviewSession,
    PreviewStatus,
    VerificationMethod,
    _active_sessions,
    all_sessions,
    detect_preview_capabilities,
    detect_preview_command,
    get_active_session,
    register_session,
    remove_session,
)


class TestPreviewStatus(unittest.TestCase):
    """PreviewStatus enum values."""

    def test_status_values(self):
        self.assertEqual(PreviewStatus.IDLE, "idle")
        self.assertEqual(PreviewStatus.STARTING, "starting")
        self.assertEqual(PreviewStatus.RUNNING, "running")
        self.assertEqual(PreviewStatus.FAILED, "failed")
        self.assertEqual(PreviewStatus.STOPPED, "stopped")


class TestPreviewCapabilities(unittest.TestCase):
    """PreviewCapabilities data and derived properties."""

    def test_defaults(self):
        caps = PreviewCapabilities()
        self.assertFalse(caps.has_browser)
        self.assertIsNone(caps.browser_path)
        self.assertFalse(caps.has_playwright)
        self.assertFalse(caps.has_node)
        self.assertFalse(caps.has_swift_build)
        self.assertFalse(caps.can_screenshot)
        self.assertFalse(caps.can_preview)

    def test_can_screenshot_requires_playwright(self):
        caps = PreviewCapabilities(has_playwright=True)
        self.assertTrue(caps.can_screenshot)

    def test_can_preview_with_browser(self):
        caps = PreviewCapabilities(has_browser=True)
        self.assertTrue(caps.can_preview)

    def test_can_preview_with_node(self):
        caps = PreviewCapabilities(has_node=True)
        self.assertTrue(caps.can_preview)

    def test_to_dict(self):
        caps = PreviewCapabilities(has_browser=True, browser_path="/usr/bin/chrome")
        d = caps.to_dict()
        self.assertTrue(d["has_browser"])
        self.assertEqual(d["browser_path"], "/usr/bin/chrome")
        self.assertFalse(d["has_playwright"])


class TestCaptureResult(unittest.TestCase):
    """CaptureResult data structures."""

    def test_defaults(self):
        result = CaptureResult()
        self.assertIsNone(result.screenshot_path)
        self.assertIsNone(result.dom_summary)
        self.assertEqual(result.console_errors, [])
        self.assertEqual(result.network_errors, [])
        self.assertIsNone(result.page_title)
        self.assertFalse(result.has_errors)

    def test_has_errors_console(self):
        result = CaptureResult(console_errors=["TypeError: x is not a function"])
        self.assertTrue(result.has_errors)

    def test_has_errors_network(self):
        result = CaptureResult(network_errors=["404 /missing.js"])
        self.assertTrue(result.has_errors)

    def test_to_dict(self):
        result = CaptureResult(
            screenshot_path="/tmp/shot.png",
            page_title="My App",
            console_errors=["err1"],
        )
        d = result.to_dict()
        self.assertEqual(d["screenshot_path"], "/tmp/shot.png")
        self.assertEqual(d["page_title"], "My App")
        self.assertEqual(d["console_errors"], ["err1"])


class TestPreviewSession(unittest.TestCase):
    """PreviewSession lifecycle and serialization."""

    def test_defaults(self):
        session = PreviewSession(session_id="s1", project_path="/tmp/proj")
        self.assertEqual(session.status, PreviewStatus.IDLE)
        self.assertIsNone(session.url)
        self.assertIsNone(session.pid)
        self.assertFalse(session.is_running)

    def test_to_dict_basic(self):
        session = PreviewSession(
            session_id="s1",
            project_path="/tmp/proj",
            url="http://localhost:3000",
            status=PreviewStatus.RUNNING,
            pid=12345,
        )
        d = session.to_dict()
        self.assertEqual(d["session_id"], "s1")
        self.assertEqual(d["status"], "running")
        self.assertEqual(d["url"], "http://localhost:3000")
        self.assertEqual(d["pid"], 12345)
        self.assertNotIn("last_capture", d)

    def test_to_dict_with_capture(self):
        capture = CaptureResult(page_title="Hello")
        session = PreviewSession(
            session_id="s2",
            project_path="/tmp/proj",
            last_capture=capture,
        )
        d = session.to_dict()
        self.assertIn("last_capture", d)
        self.assertEqual(d["last_capture"]["page_title"], "Hello")


class TestSessionRegistry(unittest.TestCase):
    """Module-level session registry functions."""

    def setUp(self):
        _active_sessions.clear()

    def tearDown(self):
        _active_sessions.clear()

    def test_register_and_get(self):
        session = PreviewSession(session_id="s1", project_path="/project/a")
        register_session(session)
        self.assertIs(get_active_session("/project/a"), session)

    def test_get_returns_none_when_empty(self):
        self.assertIsNone(get_active_session())

    def test_remove_session(self):
        session = PreviewSession(session_id="s1", project_path="/project/a")
        register_session(session)
        remove_session("/project/a")
        self.assertIsNone(get_active_session("/project/a"))

    def test_all_sessions(self):
        s1 = PreviewSession(session_id="s1", project_path="/a")
        s2 = PreviewSession(session_id="s2", project_path="/b")
        register_session(s1)
        register_session(s2)
        self.assertEqual(len(all_sessions()), 2)

    def test_register_replaces_existing(self):
        s1 = PreviewSession(session_id="s1", project_path="/a")
        s2 = PreviewSession(session_id="s2", project_path="/a")
        register_session(s1)
        register_session(s2)
        self.assertEqual(len(all_sessions()), 1)
        self.assertIs(get_active_session("/a"), s2)


class TestDetectPreviewCommand(unittest.TestCase):
    """Preview command auto-detection from project files."""

    def test_node_dev_script(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = {"scripts": {"dev": "vite"}}
            (Path(d) / "package.json").write_text(json.dumps(pkg))
            self.assertEqual(detect_preview_command(d), "npm run dev")

    def test_node_start_script(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = {"scripts": {"start": "react-scripts start"}}
            (Path(d) / "package.json").write_text(json.dumps(pkg))
            self.assertEqual(detect_preview_command(d), "npm run start")

    def test_python_django(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "manage.py").touch()
            self.assertEqual(detect_preview_command(d), "python manage.py runserver")

    def test_python_flask(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app.py").touch()
            self.assertEqual(detect_preview_command(d), "python app.py")

    def test_swift_package(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Package.swift").touch()
            self.assertEqual(detect_preview_command(d), "swift build && swift run")

    def test_no_project_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(detect_preview_command(d))

    def test_malformed_package_json(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "package.json").write_text("{not json")
            self.assertIsNone(detect_preview_command(d))


class TestDetectPreviewCapabilities(unittest.TestCase):
    """Capability detection — uses mocks to avoid environment dependency."""

    @patch("boss.preview.session.shutil.which")
    @patch("boss.preview.session._check_python_module")
    @patch("boss.preview.session.Path.exists")
    def test_nothing_available(self, mock_exists, mock_module, mock_which):
        mock_exists.return_value = False
        mock_module.return_value = False
        mock_which.return_value = None

        caps = detect_preview_capabilities()
        self.assertFalse(caps.has_browser)
        self.assertFalse(caps.has_playwright)
        self.assertFalse(caps.has_node)
        self.assertFalse(caps.has_swift_build)

    @patch("boss.preview.session.shutil.which")
    @patch("boss.preview.session._check_python_module")
    @patch("boss.preview.session.Path.exists")
    def test_all_available(self, mock_exists, mock_module, mock_which):
        mock_exists.return_value = True
        mock_module.return_value = True
        mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"

        caps = detect_preview_capabilities()
        self.assertTrue(caps.has_browser)
        self.assertTrue(caps.has_playwright)
        self.assertTrue(caps.has_node)
        self.assertTrue(caps.has_swift_build)


class TestCaptureScreenshotGraceful(unittest.TestCase):
    """capture_screenshot graceful degradation."""

    @patch("boss.preview.session._check_python_module", return_value=False)
    def test_no_playwright_returns_error(self, mock_module):
        from boss.preview.session import capture_screenshot

        with tempfile.TemporaryDirectory() as d:
            result = capture_screenshot("http://localhost:3000", Path(d) / "out.png")
            self.assertIsNone(result.screenshot_path)
            self.assertTrue(len(result.console_errors) > 0)
            self.assertIn("Playwright not available", result.console_errors[0])


class TestPreviewServer(unittest.TestCase):
    """Preview server start/stop/status."""

    def setUp(self):
        _active_sessions.clear()

    def tearDown(self):
        _active_sessions.clear()

    def test_start_no_command_detected(self):
        from boss.preview.server import start_preview

        with tempfile.TemporaryDirectory() as d:
            session = start_preview(d)
            self.assertEqual(session.status, PreviewStatus.FAILED)
            self.assertIn("No preview command", session.error_message or "")

    def test_stop_no_session(self):
        from boss.preview.server import stop_preview

        self.assertFalse(stop_preview("/nonexistent"))

    def test_status_empty(self):
        from boss.preview.server import preview_status

        status = preview_status()
        self.assertIn("capabilities", status)
        self.assertIn("sessions", status)
        self.assertEqual(status["active_count"], 0)

    def test_status_filtered_by_path(self):
        from boss.preview.server import preview_status

        s1 = PreviewSession(session_id="s1", project_path="/a")
        s2 = PreviewSession(session_id="s2", project_path="/b")
        register_session(s1)
        register_session(s2)

        status = preview_status("/a")
        self.assertEqual(len(status["sessions"]), 1)
        self.assertEqual(status["sessions"][0]["project_path"], "/a")

    def test_status_includes_vision_available(self):
        from boss.preview.server import preview_status

        status = preview_status()
        self.assertIn("vision_available", status)
        self.assertIsInstance(status["vision_available"], bool)


# ── New tests for Prompt 4b ─────────────────────────────────────────


class TestDetailMode(unittest.TestCase):
    """DetailMode enum values."""

    def test_values(self):
        self.assertEqual(DetailMode.AUTO, "auto")
        self.assertEqual(DetailMode.LOW, "low")
        self.assertEqual(DetailMode.HIGH, "high")
        self.assertEqual(DetailMode.ORIGINAL, "original")


class TestVerificationMethod(unittest.TestCase):
    """VerificationMethod enum values."""

    def test_values(self):
        self.assertEqual(VerificationMethod.VISUAL, "visual")
        self.assertEqual(VerificationMethod.TEXTUAL, "textual")
        self.assertEqual(VerificationMethod.SKIPPED, "skipped")


class TestCaptureRegion(unittest.TestCase):
    """CaptureRegion data structure."""

    def test_defaults(self):
        region = CaptureRegion()
        self.assertFalse(region.is_valid)

    def test_valid_region(self):
        region = CaptureRegion(x=10, y=20, width=100, height=50, label="header")
        self.assertTrue(region.is_valid)

    def test_to_dict(self):
        region = CaptureRegion(x=0, y=0, width=800, height=600)
        d = region.to_dict()
        self.assertEqual(d["width"], 800)
        self.assertEqual(d["height"], 600)

    def test_from_dict(self):
        region = CaptureRegion.from_dict({"x": 5, "y": 10, "width": 200, "height": 100, "label": "btn"})
        self.assertEqual(region.x, 5)
        self.assertEqual(region.label, "btn")
        self.assertTrue(region.is_valid)


class TestCaptureResultEnhancements(unittest.TestCase):
    """New CaptureResult fields and methods."""

    def test_default_detail_mode(self):
        result = CaptureResult()
        self.assertEqual(result.detail_mode, "auto")

    def test_default_verification_method(self):
        result = CaptureResult()
        self.assertEqual(result.verification_method, "skipped")

    def test_textual_summary_empty(self):
        result = CaptureResult()
        summary = result.textual_summary()
        self.assertIn("No capture data", summary)

    def test_textual_summary_with_data(self):
        result = CaptureResult(
            page_title="My App",
            console_errors=["TypeError: x is undefined"],
            dom_summary="Hello World",
        )
        summary = result.textual_summary()
        self.assertIn("My App", summary)
        self.assertIn("TypeError", summary)
        self.assertIn("Hello World", summary)

    def test_to_dict_includes_new_fields(self):
        result = CaptureResult(detail_mode="high", verification_method="visual", policy_enforced=True)
        d = result.to_dict()
        self.assertEqual(d["detail_mode"], "high")
        self.assertEqual(d["verification_method"], "visual")
        self.assertTrue(d["policy_enforced"])


class TestPreviewSessionEnhancements(unittest.TestCase):
    """New PreviewSession fields."""

    def test_default_verification_method(self):
        session = PreviewSession(session_id="s1", project_path="/a")
        self.assertEqual(session.verification_method, "skipped")

    def test_to_dict_includes_new_fields(self):
        session = PreviewSession(
            session_id="s1",
            project_path="/a",
            verification_method="visual",
            policy_enforced=True,
        )
        d = session.to_dict()
        self.assertEqual(d["verification_method"], "visual")
        self.assertTrue(d["policy_enforced"])


class TestPreviewCapabilitiesPolicyField(unittest.TestCase):
    """policy_enforced field in capabilities."""

    def test_default_not_enforced(self):
        caps = PreviewCapabilities()
        self.assertFalse(caps.policy_enforced)

    def test_to_dict_includes_field(self):
        caps = PreviewCapabilities(policy_enforced=True)
        d = caps.to_dict()
        self.assertTrue(d["policy_enforced"])


class TestRunnerIntegration(unittest.TestCase):
    """Runner routing for capture commands."""

    @patch("boss.preview.session._check_python_module", return_value=False)
    def test_capture_without_playwright_sets_skipped(self, _mock):
        from boss.preview.session import capture_screenshot

        with tempfile.TemporaryDirectory() as d:
            result = capture_screenshot("http://localhost:3000", Path(d) / "out.png")
            self.assertEqual(result.verification_method, "skipped")

    @patch("boss.preview.session._check_python_module", return_value=False)
    def test_capture_accepts_detail_mode(self, _mock):
        from boss.preview.session import capture_screenshot

        with tempfile.TemporaryDirectory() as d:
            result = capture_screenshot(
                "http://localhost:3000",
                Path(d) / "out.png",
                detail_mode="high",
            )
            self.assertEqual(result.detail_mode, "high")

    @patch("boss.preview.session._check_python_module", return_value=False)
    def test_capture_accepts_region(self, _mock):
        from boss.preview.session import capture_screenshot

        region = CaptureRegion(x=0, y=0, width=400, height=300, label="top")
        with tempfile.TemporaryDirectory() as d:
            result = capture_screenshot(
                "http://localhost:3000",
                Path(d) / "out.png",
                region=region,
            )
            self.assertIsNotNone(result.region)
            self.assertEqual(result.region["width"], 400)


class TestVisionCapability(unittest.TestCase):
    """Provider vision capability detection."""

    def test_vision_models(self):
        from boss.preview.vision import model_supports_vision

        self.assertTrue(model_supports_vision("gpt-4o"))
        self.assertTrue(model_supports_vision("gpt-4o-mini"))
        self.assertTrue(model_supports_vision("gpt-4-turbo"))
        self.assertTrue(model_supports_vision("gpt-5.4"))
        self.assertTrue(model_supports_vision("gpt-5.4-mini"))
        self.assertTrue(model_supports_vision("o1-preview"))
        self.assertTrue(model_supports_vision("claude-3-opus"))

    def test_non_vision_models(self):
        from boss.preview.vision import model_supports_vision

        self.assertFalse(model_supports_vision("gpt-3.5-turbo"))
        self.assertFalse(model_supports_vision("text-davinci-003"))
        self.assertFalse(model_supports_vision(None))
        self.assertFalse(model_supports_vision(""))

    def test_excluded_models(self):
        from boss.preview.vision import model_supports_vision

        self.assertFalse(model_supports_vision("gpt-4o-mini-audio-preview"))


class TestCaptureToModelInput(unittest.TestCase):
    """capture_to_model_input conversion."""

    def test_skipped_when_no_data(self):
        from boss.preview.vision import capture_to_model_input

        result = CaptureResult()
        output = capture_to_model_input(result, model_name="gpt-4o")
        self.assertEqual(output["method"], "skipped")

    def test_textual_fallback_no_vision(self):
        from boss.preview.vision import capture_to_model_input

        result = CaptureResult(page_title="My App", dom_summary="Hello")
        output = capture_to_model_input(result, model_name="gpt-3.5-turbo")
        self.assertEqual(output["method"], "textual")
        self.assertTrue(len(output["content"]) > 0)

    def test_textual_when_no_screenshot_file(self):
        from boss.preview.vision import capture_to_model_input

        result = CaptureResult(
            screenshot_path="/nonexistent/file.png",
            page_title="App",
            dom_summary="Text",
        )
        output = capture_to_model_input(result, model_name="gpt-4o")
        # File doesn't exist, so it should fall back to textual
        self.assertEqual(output["method"], "textual")

    def test_visual_with_screenshot(self):
        from boss.preview.vision import capture_to_model_input

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            f.flush()
            result = CaptureResult(
                screenshot_path=f.name,
                page_title="Test Page",
                dom_summary="content",
            )
            output = capture_to_model_input(result, model_name="gpt-4o")

        self.assertEqual(output["method"], "visual")
        self.assertTrue(len(output["content"]) >= 2)
        # Should have text and image parts
        types = [p["type"] for p in output["content"]]
        self.assertIn("input_text", types)
        self.assertIn("input_image", types)

        os.unlink(f.name)

    def test_detail_mode_passed_through(self):
        from boss.preview.vision import capture_to_model_input

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            f.flush()
            result = CaptureResult(screenshot_path=f.name, page_title="X")
            output = capture_to_model_input(result, model_name="gpt-4o", detail="high")

        image_parts = [p for p in output["content"] if p["type"] == "input_image"]
        self.assertEqual(len(image_parts), 1)
        self.assertEqual(image_parts[0]["detail"], "high")

        os.unlink(f.name)


class TestLoopPreviewVerification(unittest.TestCase):
    """Loop preview verification behavior."""

    def test_is_frontend_task_positive(self):
        from boss.loop.engine import _is_frontend_task

        self.assertTrue(_is_frontend_task("Fix the SwiftUI ChatView layout"))
        self.assertTrue(_is_frontend_task("Update the frontend component"))
        self.assertTrue(_is_frontend_task("Fix the React render bug"))
        self.assertTrue(_is_frontend_task("Update PreviewView display"))

    def test_is_frontend_task_negative(self):
        from boss.loop.engine import _is_frontend_task

        self.assertFalse(_is_frontend_task("Refactor the database schema"))
        self.assertFalse(_is_frontend_task("Add a new API endpoint"))
        self.assertFalse(_is_frontend_task("Fix the memory leak in scanner"))

    def test_try_preview_skipped_for_backend_task(self):
        from boss.loop.engine import _try_preview_verification

        result = _try_preview_verification(
            task="Optimize the database query performance",
            workspace_root="/tmp/project",
        )
        self.assertEqual(result["method"], "skipped")
        self.assertFalse(result["has_blocking_errors"])
        # model_content should not be present for skipped tasks
        self.assertNotIn("model_content", result)

    def test_try_preview_skipped_no_tooling(self):
        from boss.loop.engine import _try_preview_verification

        with patch("boss.preview.session.Path.exists", return_value=False), \
             patch("boss.preview.session.shutil.which", return_value=None), \
             patch("boss.preview.session._check_python_module", return_value=False):
            result = _try_preview_verification(
                task="Fix the SwiftUI layout",
                workspace_root="/tmp/project",
            )
            self.assertEqual(result["method"], "skipped")

    def test_try_preview_returns_model_content(self):
        """When verification runs, result includes model_content list."""
        from boss.loop.engine import _try_preview_verification
        from boss.preview.session import register_session, remove_session

        session = PreviewSession(
            session_id="test-mc",
            project_path="/tmp/mc",
            url="http://localhost:3000",
            status=PreviewStatus.RUNNING,
            pid=99999,
        )
        register_session(session)

        try:
            with patch("boss.preview.session.Path.exists", return_value=True), \
                 patch("boss.preview.session.shutil.which", return_value="/usr/bin/node"), \
                 patch("boss.preview.session._check_python_module", return_value=True), \
                 patch("boss.preview.session._run_capture_command", return_value={
                     "returncode": 0, "stdout": '{"title":"Test","console_errors":["err"],"network_errors":[],"dom_summary":"Hi"}',
                     "stderr": "", "policy_enforced": False,
                 }), \
                 patch("os.kill"):
                result = _try_preview_verification(
                    task="Fix the SwiftUI layout",
                    workspace_root="/tmp/mc",
                )
                self.assertIn("model_content", result)
                self.assertIsInstance(result["model_content"], list)
                self.assertTrue(result["has_blocking_errors"])
        finally:
            remove_session("/tmp/mc")

    def test_try_preview_writes_back_method(self):
        """Verification method is written back to session.verification_method."""
        from boss.loop.engine import _try_preview_verification
        from boss.preview.session import get_active_session, register_session, remove_session

        session = PreviewSession(
            session_id="test-wb",
            project_path="/tmp/wb",
            url="http://localhost:3000",
            status=PreviewStatus.RUNNING,
            pid=99999,
        )
        register_session(session)

        try:
            with patch("boss.preview.session.Path.exists", return_value=True), \
                 patch("boss.preview.session.shutil.which", return_value="/usr/bin/node"), \
                 patch("boss.preview.session._check_python_module", return_value=True), \
                 patch("boss.preview.session._run_capture_command", return_value={
                     "returncode": 0, "stdout": '{"title":"Test","console_errors":[],"network_errors":[],"dom_summary":"OK"}',
                     "stderr": "", "policy_enforced": False,
                 }), \
                 patch("os.kill"):
                result = _try_preview_verification(
                    task="Fix the SwiftUI layout",
                    workspace_root="/tmp/wb",
                )
                # Method should be written back to session
                s = get_active_session("/tmp/wb")
                self.assertIsNotNone(s)
                self.assertEqual(s.verification_method, result["method"])
                # Also written to capture
                self.assertEqual(s.last_capture.verification_method, result["method"])
        finally:
            remove_session("/tmp/wb")


class TestLoopStateEnhancements(unittest.TestCase):
    """LoopAttempt and LoopPhase enhancements."""

    def test_verify_preview_phase(self):
        from boss.loop.state import LoopPhase

        self.assertEqual(LoopPhase.VERIFY_PREVIEW, "verify_preview")

    def test_attempt_verification_fields(self):
        from boss.loop.state import LoopAttempt

        attempt = LoopAttempt(
            attempt_number=1,
            started_at=0.0,
            verification_method="visual",
            preview_evidence={"screenshot_path": "/tmp/shot.png"},
        )
        d = attempt.to_dict()
        self.assertEqual(d["verification_method"], "visual")
        self.assertEqual(d["preview_evidence"]["screenshot_path"], "/tmp/shot.png")

    def test_attempt_from_dict_with_verification(self):
        from boss.loop.state import LoopAttempt

        data = {
            "attempt_number": 2,
            "started_at": 100.0,
            "verification_method": "textual",
            "preview_evidence": {"page_title": "App"},
        }
        attempt = LoopAttempt.from_dict(data)
        self.assertEqual(attempt.verification_method, "textual")
        self.assertEqual(attempt.preview_evidence["page_title"], "App")


class TestIterationPromptPreviewContext(unittest.TestCase):
    """Preview evidence in iteration prompt."""

    def test_prompt_includes_preview_evidence(self):
        from boss.loop.engine import _build_iteration_prompt
        from boss.loop.state import LoopAttempt

        prior = LoopAttempt(
            attempt_number=1,
            started_at=0.0,
            finished_at=1.0,
            test_passed=False,
            verification_method="visual",
            preview_evidence={
                "page_title": "My App",
                "console_errors": ["TypeError: x is undefined"],
                "network_errors": [],
            },
        )
        prompt = _build_iteration_prompt(
            task="Fix the SwiftUI layout",
            attempt_number=2,
            micro_plan=[],
            prior_attempts=[prior],
            phase="inspect",
        )
        self.assertIn("Preview verification: visual", prompt)
        self.assertIn("My App", prompt)
        self.assertIn("TypeError", prompt)

    def test_prompt_skips_preview_when_skipped(self):
        from boss.loop.engine import _build_iteration_prompt
        from boss.loop.state import LoopAttempt

        prior = LoopAttempt(
            attempt_number=1,
            started_at=0.0,
            finished_at=1.0,
            test_passed=True,
            verification_method="skipped",
        )
        prompt = _build_iteration_prompt(
            task="Fix backend query",
            attempt_number=2,
            micro_plan=[],
            prior_attempts=[prior],
            phase="edit",
        )
        self.assertNotIn("Preview verification:", prompt)


class TestPreviewStartStopRunnerEnforcement(unittest.TestCase):
    """Preview start/stop must route through RunnerEngine when context is active."""

    def setUp(self):
        _active_sessions.clear()

    def tearDown(self):
        _active_sessions.clear()

    @patch("boss.preview.server._get_runner")
    @patch("boss.preview.server._discover_url", return_value=None)
    @patch("boss.preview.server.detect_preview_command", return_value="npm start")
    def test_start_preview_uses_runner_start_managed_process(self, _det, _url, mock_get_runner):
        """start_preview calls runner.start_managed_process instead of raw Popen."""
        from boss.preview.server import start_preview
        from boss.runner.engine import ExecutionResult
        from boss.runner.policy import CommandVerdict, PermissionProfile

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.poll.return_value = 0

        allowed_result = ExecutionResult(
            command=["npm", "start"],
            exit_code=None,
            stdout="",
            stderr="",
            verdict=CommandVerdict.ALLOWED.value,
            policy_profile=PermissionProfile.WORKSPACE_WRITE.value,
            enforcement="boss",
            duration_ms=0.0,
        )

        mock_runner = MagicMock()
        mock_runner.start_managed_process.return_value = (mock_proc, allowed_result)
        mock_runner.policy.check_network.return_value = CommandVerdict.ALLOWED
        mock_get_runner.return_value = mock_runner

        session = start_preview("/tmp/test-project", port=3000)

        mock_runner.start_managed_process.assert_called_once()
        self.assertEqual(session.pid, 12345)
        self.assertEqual(session.status, PreviewStatus.RUNNING)
        self.assertTrue(session.policy_enforced)

    @patch("boss.preview.server._get_runner")
    @patch("boss.preview.server.detect_preview_command", return_value="npm start")
    def test_start_preview_blocked_by_runner_policy(self, _det, mock_get_runner):
        """start_preview sets FAILED when runner policy denies the command."""
        from boss.preview.server import start_preview
        from boss.runner.engine import ExecutionResult
        from boss.runner.policy import CommandVerdict, PermissionProfile

        denied_result = ExecutionResult(
            command=["npm", "start"],
            exit_code=None,
            stdout="",
            stderr="",
            verdict=CommandVerdict.DENIED.value,
            policy_profile=PermissionProfile.READ_ONLY.value,
            enforcement="boss",
            duration_ms=0.0,
            denied_reason="Command denied by read_only policy",
        )

        mock_runner = MagicMock()
        mock_runner.start_managed_process.return_value = (None, denied_result)
        mock_runner.policy.check_network.return_value = CommandVerdict.ALLOWED
        mock_get_runner.return_value = mock_runner

        session = start_preview("/tmp/test-project")

        self.assertEqual(session.status, PreviewStatus.FAILED)
        self.assertIn("denied", session.error_message.lower())
        self.assertTrue(session.policy_enforced)

    @patch("boss.preview.server._get_runner")
    def test_stop_preview_uses_runner_terminate(self, mock_get_runner):
        """stop_preview calls runner.terminate_managed_process instead of raw killpg."""
        from boss.preview.server import stop_preview
        from boss.runner.engine import ExecutionResult
        from boss.runner.policy import CommandVerdict, PermissionProfile

        session = PreviewSession(
            session_id="test-stop-001",
            project_path="/tmp/test-project",
            start_command="npm start",
            status=PreviewStatus.RUNNING,
            pid=99999,
        )
        register_session(session)

        term_result = ExecutionResult(
            command=["kill", "-15", "99999"],
            exit_code=0,
            stdout="",
            stderr="",
            verdict=CommandVerdict.ALLOWED.value,
            policy_profile=PermissionProfile.WORKSPACE_WRITE.value,
            enforcement="boss",
            duration_ms=1.0,
        )

        mock_runner = MagicMock()
        mock_runner.terminate_managed_process.return_value = term_result
        mock_get_runner.return_value = mock_runner

        result = stop_preview("/tmp/test-project")

        self.assertTrue(result)
        mock_runner.terminate_managed_process.assert_called_once_with(99999)

    @patch("boss.preview.server._get_runner", return_value=None)
    @patch("boss.preview.server._discover_url", return_value=None)
    @patch("boss.preview.server.subprocess.Popen")
    @patch("boss.preview.server.detect_preview_command", return_value="npm start")
    def test_start_preview_falls_back_without_runner(self, _det, mock_popen, _url, _get):
        """Without runner context, start_preview uses raw subprocess.Popen."""
        from boss.preview.server import start_preview

        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        session = start_preview("/tmp/test-project", port=3000)

        mock_popen.assert_called_once()
        self.assertEqual(session.pid, 55555)
        self.assertEqual(session.status, PreviewStatus.RUNNING)

    @patch("boss.preview.server._get_runner", return_value=None)
    @patch("boss.preview.server.os.killpg")
    @patch("boss.preview.server.os.getpgid", return_value=88888)
    def test_stop_preview_falls_back_without_runner(self, _pgid, mock_killpg, _get):
        """Without runner context, stop_preview uses raw os.killpg."""
        from boss.preview.server import stop_preview

        session = PreviewSession(
            session_id="test-fallback-stop",
            project_path="/tmp/test-project",
            start_command="npm start",
            status=PreviewStatus.RUNNING,
            pid=88888,
        )
        register_session(session)

        result = stop_preview("/tmp/test-project")

        self.assertTrue(result)
        mock_killpg.assert_called_once()

    @patch("boss.preview.server._get_runner")
    @patch("boss.preview.server.detect_preview_command", return_value="npm start")
    def test_start_preview_network_denied_by_runner(self, _det, mock_get_runner):
        """start_preview returns FAILED when runner network policy denies localhost."""
        from boss.preview.server import start_preview

        mock_runner = MagicMock()
        mock_runner.policy.check_network.return_value = MagicMock(value="denied")
        mock_get_runner.return_value = mock_runner

        session = start_preview("/tmp/test-project")

        self.assertEqual(session.status, PreviewStatus.FAILED)
        self.assertIn("Network", session.error_message)
        mock_runner.start_managed_process.assert_not_called()


class TestCapturePreviewToolParams(unittest.TestCase):
    """Governed capture_preview tool accepts detail and region params."""

    def test_tool_accepts_detail_mode(self):
        """Verify the tool schema includes detail_mode and region params."""
        from boss.tools.preview import capture_preview

        schema = capture_preview.params_json_schema
        props = schema.get("properties", {})
        self.assertIn("detail_mode", props)
        self.assertIn("region_width", props)
        self.assertIn("region_height", props)
        self.assertIn("region_x", props)
        self.assertIn("region_y", props)


if __name__ == "__main__":
    unittest.main()
