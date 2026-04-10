"""Tests for Phase 2: SDK runtime tool integration and apply_patch.

Coverage:
- apply_patch tool registration, mode filtering, approval metadata
- apply_patch execution through FunctionTool path (writable-root enforcement)
- Boss-first governance preserved when SDK backend is enabled
- Fallback to Boss-native backend when SDK path is disabled
- No runner-context leakage from sdk_runtime helpers
- Shell command denial/prompt behavior stays honest with SDK backend
- SDK runtime diagnostics
- Python patch fallback correctness
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.tool import ToolContext

from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, ExecutionType, get_tool_metadata
from boss.runner.engine import RunnerEngine, current_runner, _current_runner_var
from boss.runner.policy import (
    CommandVerdict,
    ExecutionPolicy,
    NetworkPolicy,
    PathPolicy,
    PermissionProfile,
)


def _make_policy(
    profile: PermissionProfile = PermissionProfile.WORKSPACE_WRITE,
    writable_roots: tuple[Path, ...] = (),
    workspace_root: Path | None = None,
    **kwargs,
) -> ExecutionPolicy:
    defaults = {
        "path_policy": PathPolicy(writable_roots=writable_roots, workspace_root=workspace_root),
        "network": NetworkPolicy.DISABLED,
        "domain_allowlist": (),
        "allowed_prefixes": ("echo", "cat", "ls", "python3"),
        "prompt_prefixes": (),
        "denied_prefixes": ("sudo",),
        "allow_shell": profile != PermissionProfile.READ_ONLY,
        "env_scrub_keys": (),
    }
    defaults.update(kwargs)
    return ExecutionPolicy(profile=profile, **defaults)


def _invoke_tool(tool, args: dict) -> str:
    """Invoke a FunctionTool through the on_invoke_tool path."""
    args_json = json.dumps(args)
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="test",
        tool_arguments=args_json,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args_json))


def _with_runner(func, tmp_dir: str, **policy_kwargs):
    """Run func with a runner set up for tmp_dir as writable root."""
    policy = _make_policy(
        writable_roots=(Path(tmp_dir),),
        workspace_root=Path(tmp_dir),
        **policy_kwargs,
    )
    runner = RunnerEngine(policy)
    _current_runner_var.set(runner)
    try:
        return func()
    finally:
        _current_runner_var.set(None)


# ── apply_patch registration ────────────────────────────────────────


class ApplyPatchRegistrationTests(unittest.TestCase):
    """Verify apply_patch registers correct metadata."""

    def test_metadata_registered(self):
        meta = get_tool_metadata("apply_patch")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.EDIT)
        self.assertEqual(meta.title, "Apply Patch")

    def test_not_auto_allowed(self):
        meta = get_tool_metadata("apply_patch")
        self.assertNotIn(meta.execution_type, AUTO_ALLOWED_EXECUTION_TYPES)

    def test_scope_key_includes_path(self):
        meta = get_tool_metadata("apply_patch")
        key = meta.scope_key({"path": "/tmp/test.py"})
        self.assertIn("patch", key)

    def test_scope_label_readable(self):
        meta = get_tool_metadata("apply_patch")
        label = meta.scope_label({"path": "/tmp/test.py"})
        self.assertIn("/tmp/test.py", label)


# ── apply_patch mode filtering ──────────────────────────────────────


class ApplyPatchModeFilteringTests(unittest.TestCase):
    """Verify apply_patch is filtered correctly by mode policy."""

    def test_agent_mode_includes_apply_patch(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="agent")
        tool_names = {tool.name for tool in agent.tools}
        self.assertIn("apply_patch", tool_names)

    def test_ask_mode_excludes_apply_patch(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="ask")
        tool_names = {tool.name for tool in agent.tools}
        self.assertNotIn("apply_patch", tool_names)

    def test_review_mode_excludes_apply_patch(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="review")
        tool_names = {tool.name for tool in agent.tools}
        self.assertNotIn("apply_patch", tool_names)

    def test_plan_mode_excludes_apply_patch(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="plan")
        tool_names = {tool.name for tool in agent.tools}
        self.assertNotIn("apply_patch", tool_names)


# ── apply_patch execution through FunctionTool ──────────────────────


class ApplyPatchExecutionTests(unittest.TestCase):
    """Test apply_patch through the actual FunctionTool invoke path."""

    def test_successful_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "hello.py"
            target.write_text("def hello():\n    return 1\n", encoding="utf-8")

            diff = (
                "--- a/hello.py\n"
                "+++ b/hello.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def hello():\n"
                "-    return 1\n"
                "+    return 42\n"
            )

            from boss.tools.action import apply_patch as ap_tool

            result = _with_runner(
                lambda: _invoke_tool(ap_tool, {"path": str(target), "diff": diff}),
                tmp,
            )
            self.assertIn("Patched", result)
            self.assertIn("return 42", target.read_text())

    def test_denied_outside_writable_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "hello.py"
            target.write_text("x = 1\n", encoding="utf-8")

            from boss.tools.action import apply_patch as ap_tool

            # Set up runner with /tmp/other as writable — target is outside
            other = Path(tmp) / "other"
            other.mkdir()
            policy = _make_policy(
                writable_roots=(other,),
                workspace_root=other,
            )
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = _invoke_tool(ap_tool, {
                    "path": str(target),
                    "diff": "@@ -1 +1 @@\n-x = 1\n+x = 2\n",
                })
                self.assertIn("denied", result.lower())
            finally:
                _current_runner_var.set(None)

    def test_empty_diff_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "hello.py"
            target.write_text("x = 1\n", encoding="utf-8")

            from boss.tools.action import apply_patch as ap_tool

            result = _with_runner(
                lambda: _invoke_tool(ap_tool, {"path": str(target), "diff": "   "}),
                tmp,
            )
            self.assertIn("empty diff", result.lower())

    def test_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            from boss.tools.action import apply_patch as ap_tool

            result = _with_runner(
                lambda: _invoke_tool(ap_tool, {
                    "path": str(Path(tmp) / "nonexistent.py"),
                    "diff": "@@ -1 +1 @@\n-x\n+y\n",
                }),
                tmp,
            )
            self.assertIn("not found", result.lower())


# ── Python patch fallback ───────────────────────────────────────────


class PythonPatchFallbackTests(unittest.TestCase):
    """Test the pure-Python unified diff application."""

    def test_simple_hunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.txt"
            target.write_text("line1\nline2\nline3\n", encoding="utf-8")

            diff = (
                "--- a/test.txt\n"
                "+++ b/test.txt\n"
                "@@ -1,3 +1,3 @@\n"
                " line1\n"
                "-line2\n"
                "+LINE_TWO\n"
                " line3\n"
            )

            from boss.sdk_runtime import _apply_unified_diff
            result = _apply_unified_diff(target, diff)
            self.assertEqual(result.status, "completed")
            self.assertIn("LINE_TWO", target.read_text())

    def test_add_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.txt"
            target.write_text("A\nB\nC\n", encoding="utf-8")

            diff = (
                "--- a/test.txt\n"
                "+++ b/test.txt\n"
                "@@ -1,3 +1,5 @@\n"
                " A\n"
                "+A1\n"
                "+A2\n"
                " B\n"
                " C\n"
            )

            from boss.sdk_runtime import _apply_unified_diff
            result = _apply_unified_diff(target, diff)
            self.assertEqual(result.status, "completed")
            content = target.read_text()
            self.assertIn("A1", content)
            self.assertIn("A2", content)

    def test_remove_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.txt"
            target.write_text("keep\nremove_me\nalso_keep\n", encoding="utf-8")

            diff = (
                "--- a/test.txt\n"
                "+++ b/test.txt\n"
                "@@ -1,3 +1,2 @@\n"
                " keep\n"
                "-remove_me\n"
                " also_keep\n"
            )

            from boss.sdk_runtime import _apply_unified_diff
            result = _apply_unified_diff(target, diff)
            self.assertEqual(result.status, "completed")
            content = target.read_text()
            self.assertNotIn("remove_me", content)
            self.assertIn("keep", content)

    def test_context_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.txt"
            target.write_text("actual_line\n", encoding="utf-8")

            diff = (
                "--- a/test.txt\n"
                "+++ b/test.txt\n"
                "@@ -1 +1 @@\n"
                "-wrong_content\n"
                "+new_content\n"
            )

            from boss.sdk_runtime import _apply_unified_diff
            result = _apply_unified_diff(target, diff)
            self.assertEqual(result.status, "failed")
            # Original should be unchanged
            self.assertEqual(target.read_text(), "actual_line\n")


# ── SDK shell backend ──────────────────────────────────────────────


class SDKShellBackendTests(unittest.TestCase):
    """Test run_shell with SDK backend enabled/disabled."""

    def test_native_backend_default(self):
        """With sdk_shell_backend=False, run_shell uses the native path."""
        with tempfile.TemporaryDirectory() as tmp:
            from boss.tools.action import run_shell as rs_tool

            result = _with_runner(
                lambda: _invoke_tool(rs_tool, {"command": "echo native_test"}),
                tmp,
            )
            self.assertIn("native_test", result)
            self.assertIn("exit code 0", result)

    def test_sdk_backend_when_enabled(self):
        """With sdk_shell_backend=True, run_shell uses the SDK path."""
        with tempfile.TemporaryDirectory() as tmp:
            from boss.tools.action import run_shell as rs_tool

            with patch("boss.config.settings") as mock_settings:
                mock_settings.sdk_shell_backend = True

                result = _with_runner(
                    lambda: _invoke_tool(rs_tool, {"command": "echo sdk_test"}),
                    tmp,
                )
                self.assertIn("sdk_test", result)

    def test_sdk_backend_denied_command(self):
        """SDK backend still denies commands blocked by runner policy."""
        with tempfile.TemporaryDirectory() as tmp:
            from boss.tools.action import run_shell as rs_tool

            with patch("boss.config.settings") as mock_settings:
                mock_settings.sdk_shell_backend = True

                result = _with_runner(
                    lambda: _invoke_tool(rs_tool, {"command": "sudo rm -rf /"}),
                    tmp,
                )
                self.assertIn("denied", result.lower())

    def test_native_fallback_on_import_error(self):
        """If SDK imports fail, run_shell falls back to native."""
        with tempfile.TemporaryDirectory() as tmp:
            from boss.tools.action import run_shell as rs_tool

            with patch("boss.config.settings") as mock_settings:
                mock_settings.sdk_shell_backend = True

                # Simulate SDK import failure
                import sys
                original = sys.modules.get("agents")
                try:
                    sys.modules["agents"] = None  # type: ignore
                    result = _with_runner(
                        lambda: _invoke_tool(rs_tool, {"command": "echo fallback_test"}),
                        tmp,
                    )
                    self.assertIn("fallback_test", result)
                finally:
                    if original is not None:
                        sys.modules["agents"] = original


# ── Runner context leak prevention ──────────────────────────────────


class SDKRuntimeRunnerLeakTests(unittest.TestCase):
    """Verify sdk_runtime helpers do not leak runners into the context var."""

    def test_get_runner_in_sdk_runtime_no_leak(self):
        from boss.sdk_runtime import _get_runner

        _current_runner_var.set(None)
        runner = _get_runner()
        self.assertIsNotNone(runner)
        self.assertIsNone(current_runner(), "sdk_runtime._get_runner leaked into context var")

    def test_boss_shell_executor_no_leak(self):
        from boss.sdk_runtime import boss_shell_executor
        from agents import ShellActionRequest, ShellCallData, ShellCommandRequest, RunContextWrapper

        _current_runner_var.set(None)
        action = ShellActionRequest(commands=["echo test"], timeout_ms=5000)
        call_data = ShellCallData(call_id="leak-test", action=action)
        request = ShellCommandRequest(
            ctx_wrapper=RunContextWrapper(context=None),
            data=call_data,
        )
        boss_shell_executor(request)
        self.assertIsNone(current_runner(), "boss_shell_executor leaked a runner")


# ── BossApplyPatchEditor ────────────────────────────────────────────


class BossApplyPatchEditorTests(unittest.TestCase):
    """Test the editor implementation used by SDK ApplyPatchTool."""

    def test_create_file(self):
        from agents import ApplyPatchOperation
        from boss.sdk_runtime import BossApplyPatchEditor

        with tempfile.TemporaryDirectory() as tmp:
            editor = BossApplyPatchEditor(workspace_root=Path(tmp))

            target = str(Path(tmp) / "new_file.py")
            op = ApplyPatchOperation(
                type="create_file",
                path=target,
                diff="print('hello')\n",
            )

            policy = _make_policy(writable_roots=(Path(tmp),), workspace_root=Path(tmp))
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = editor.create_file(op)
                self.assertEqual(result.status, "completed")
                self.assertTrue(Path(target).exists())
            finally:
                _current_runner_var.set(None)

    def test_update_file(self):
        from agents import ApplyPatchOperation
        from boss.sdk_runtime import BossApplyPatchEditor

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "existing.py"
            target.write_text("old_content\n", encoding="utf-8")

            editor = BossApplyPatchEditor(workspace_root=Path(tmp))
            op = ApplyPatchOperation(
                type="update_file",
                path=str(target),
                diff=(
                    "--- a/existing.py\n"
                    "+++ b/existing.py\n"
                    "@@ -1 +1 @@\n"
                    "-old_content\n"
                    "+new_content\n"
                ),
            )

            policy = _make_policy(writable_roots=(Path(tmp),), workspace_root=Path(tmp))
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = editor.update_file(op)
                self.assertEqual(result.status, "completed")
                self.assertIn("new_content", target.read_text())
            finally:
                _current_runner_var.set(None)

    def test_delete_file(self):
        from agents import ApplyPatchOperation
        from boss.sdk_runtime import BossApplyPatchEditor

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "to_delete.txt"
            target.write_text("bye\n", encoding="utf-8")

            editor = BossApplyPatchEditor(workspace_root=Path(tmp))
            op = ApplyPatchOperation(type="delete_file", path=str(target))

            policy = _make_policy(writable_roots=(Path(tmp),), workspace_root=Path(tmp))
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = editor.delete_file(op)
                self.assertEqual(result.status, "completed")
                self.assertFalse(target.exists())
            finally:
                _current_runner_var.set(None)

    def test_write_denied_outside_roots(self):
        from agents import ApplyPatchOperation
        from boss.sdk_runtime import BossApplyPatchEditor

        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            editor = BossApplyPatchEditor(workspace_root=allowed)
            op = ApplyPatchOperation(
                type="create_file",
                path="/etc/shadow",
                diff="nope\n",
            )

            policy = _make_policy(writable_roots=(allowed,), workspace_root=allowed)
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = editor.create_file(op)
                self.assertEqual(result.status, "failed")
                self.assertIn("denied", result.output.lower())
            finally:
                _current_runner_var.set(None)


# ── SDK runtime diagnostics ─────────────────────────────────────────


class SDKRuntimeDiagnosticsTests(unittest.TestCase):
    """Test the diagnostics surface."""

    def test_status_returns_expected_keys(self):
        from boss.sdk_runtime import sdk_runtime_status

        status = sdk_runtime_status()
        self.assertIn("shell_tool_available", status)
        self.assertIn("apply_patch_tool_available", status)
        self.assertIn("shell_backend", status)
        self.assertIn("patch_backend", status)
        self.assertIn("sdk_version", status)

    def test_default_backends_are_native(self):
        from boss.sdk_runtime import sdk_runtime_status

        status = sdk_runtime_status()
        self.assertEqual(status["shell_backend"], "native")
        self.assertEqual(status["patch_backend"], "native")

    def test_sdk_version_present(self):
        from boss.sdk_runtime import sdk_runtime_status

        status = sdk_runtime_status()
        self.assertNotEqual(status["sdk_version"], "not installed")


# ── Config flags ────────────────────────────────────────────────────


class ConfigFlagTests(unittest.TestCase):
    """Verify feature flags exist with safe defaults."""

    def test_sdk_shell_backend_defaults_false(self):
        from boss.config import Settings

        s = Settings()
        self.assertFalse(s.sdk_shell_backend)

    def test_sdk_patch_backend_defaults_false(self):
        from boss.config import Settings

        s = Settings()
        self.assertFalse(s.sdk_patch_backend)


# ── Regression: existing tools still work ───────────────────────────


class ExistingToolsRegressionTests(unittest.TestCase):
    """Verify write_file and edit_file are unbroken by Phase 2 changes."""

    def test_write_file_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "regression.txt"
            from boss.tools.action import write_file as wf_tool

            result = _with_runner(
                lambda: _invoke_tool(wf_tool, {"path": str(target), "content": "ok\n"}),
                tmp,
            )
            self.assertIn("Created", result)
            self.assertEqual(target.read_text(), "ok\n")

    def test_edit_file_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "regression.py"
            target.write_text("x = 1\n", encoding="utf-8")
            from boss.tools.action import edit_file as ef_tool

            result = _with_runner(
                lambda: _invoke_tool(ef_tool, {
                    "path": str(target),
                    "old_string": "x = 1",
                    "new_string": "x = 2",
                }),
                tmp,
            )
            self.assertIn("Edited", result)
            self.assertIn("x = 2", target.read_text())


if __name__ == "__main__":
    unittest.main()
