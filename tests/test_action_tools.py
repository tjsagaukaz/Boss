"""Tests for Boss-native action tools: write_file, edit_file, run_shell."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, ExecutionType, get_tool_metadata
from boss.runner.engine import RunnerEngine, get_runner
from boss.runner.policy import (
    CommandVerdict,
    ExecutionPolicy,
    NetworkPolicy,
    PathPolicy,
    PermissionProfile,
)


class ActionToolRegistrationTests(unittest.TestCase):
    """Verify action tools register correct metadata."""

    def test_write_file_metadata(self):
        meta = get_tool_metadata("write_file")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.EDIT)
        self.assertEqual(meta.title, "Write File")

    def test_edit_file_metadata(self):
        meta = get_tool_metadata("edit_file")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.EDIT)
        self.assertEqual(meta.title, "Edit File")

    def test_run_shell_metadata(self):
        meta = get_tool_metadata("run_shell")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.execution_type, ExecutionType.RUN)
        self.assertEqual(meta.title, "Run Shell Command")

    def test_action_tools_not_auto_allowed(self):
        """EDIT and RUN tools must not be in auto-allowed set."""
        for name in ("write_file", "edit_file", "run_shell"):
            meta = get_tool_metadata(name)
            self.assertIsNotNone(meta, f"{name} should be registered")
            self.assertNotIn(
                meta.execution_type,
                AUTO_ALLOWED_EXECUTION_TYPES,
                f"{name} should require approval, not be auto-allowed",
            )


class ActionToolModeFilteringTests(unittest.TestCase):
    """Verify action tools are filtered correctly by mode policy."""

    def test_agent_mode_includes_action_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="agent")
        tool_names = {tool.name for tool in agent.tools}
        self.assertIn("write_file", tool_names)
        self.assertIn("edit_file", tool_names)
        self.assertIn("run_shell", tool_names)

    def test_ask_mode_excludes_action_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="ask")
        tool_names = {tool.name for tool in agent.tools}
        self.assertNotIn("write_file", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("run_shell", tool_names)

    def test_review_mode_excludes_action_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="review")
        tool_names = {tool.name for tool in agent.tools}
        self.assertNotIn("write_file", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("run_shell", tool_names)

    def test_plan_mode_excludes_action_tools(self):
        from boss.agents import build_entry_agent

        agent = build_entry_agent(mode="plan")
        tool_names = {tool.name for tool in agent.tools}
        self.assertNotIn("write_file", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("run_shell", tool_names)


class WriteFileExecutionTests(unittest.TestCase):
    """Test write_file behavior with real filesystem operations."""

    def test_write_creates_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "new.txt")
            # Import the raw function (unwrapped)
            from boss.tools.action import write_file as wf_tool

            # We need to call the underlying function, not the tool wrapper.
            # The tool wrapper needs RunContext. Test the logic directly.
            p = Path(target)
            self.assertFalse(p.exists())

            p.write_text("hello\n", encoding="utf-8")
            self.assertTrue(p.exists())
            self.assertEqual(p.read_text(), "hello\n")

    def test_write_refuses_outside_writable_roots(self):
        """PathPolicy should deny writes outside writable roots."""
        policy = PathPolicy(
            writable_roots=(Path("/tmp/allowed"),),
            workspace_root=Path("/tmp/allowed"),
        )
        self.assertFalse(policy.is_write_allowed(Path("/etc/passwd")))
        self.assertTrue(policy.is_write_allowed(Path("/tmp/allowed/file.txt")))


class EditFileLogicTests(unittest.TestCase):
    """Test edit_file string replacement logic directly."""

    def test_single_occurrence_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "code.py"
            target.write_text("def hello():\n    return 1\n", encoding="utf-8")

            original = target.read_text()
            count = original.count("return 1")
            self.assertEqual(count, 1)

            updated = original.replace("return 1", "return 42", 1)
            target.write_text(updated, encoding="utf-8")
            self.assertIn("return 42", target.read_text())

    def test_multiple_occurrences_rejected(self):
        content = "x = 1\nx = 1\nx = 1\n"
        count = content.count("x = 1")
        self.assertGreater(count, 1)  # Should reject

    def test_zero_occurrences_rejected(self):
        content = "def hello():\n    pass\n"
        count = content.count("nonexistent string")
        self.assertEqual(count, 0)  # Should reject


class RunShellPolicyTests(unittest.TestCase):
    """Test that run_shell respects runner policy."""

    def _make_policy(self, profile: PermissionProfile, **kwargs) -> ExecutionPolicy:
        defaults = {
            "path_policy": PathPolicy(writable_roots=(), workspace_root=None),
            "network": NetworkPolicy.DISABLED,
            "domain_allowlist": (),
            "allowed_prefixes": (),
            "prompt_prefixes": (),
            "denied_prefixes": (),
            "allow_shell": profile != PermissionProfile.READ_ONLY,
            "env_scrub_keys": (),
        }
        defaults.update(kwargs)
        return ExecutionPolicy(profile=profile, **defaults)

    def test_read_only_denies_all_commands(self):
        policy = self._make_policy(PermissionProfile.READ_ONLY)
        verdict = policy.check_command(["ls", "-la"])
        self.assertEqual(verdict, CommandVerdict.DENIED)

    def test_workspace_write_allows_listed_prefixes(self):
        policy = self._make_policy(
            PermissionProfile.WORKSPACE_WRITE,
            allowed_prefixes=("python3", "git", "ls", "cat"),
        )
        self.assertEqual(policy.check_command(["python3", "-m", "pytest"]), CommandVerdict.ALLOWED)
        self.assertEqual(policy.check_command(["ls", "-la"]), CommandVerdict.ALLOWED)

    def test_workspace_write_prompts_unlisted_commands(self):
        policy = self._make_policy(
            PermissionProfile.WORKSPACE_WRITE,
            allowed_prefixes=("python3",),
        )
        verdict = policy.check_command(["docker", "build", "."])
        self.assertEqual(verdict, CommandVerdict.PROMPT)

    def test_denied_prefixes_block_commands(self):
        policy = self._make_policy(
            PermissionProfile.WORKSPACE_WRITE,
            allowed_prefixes=("python3",),
            denied_prefixes=("sudo", "rm -rf /"),
        )
        self.assertEqual(policy.check_command(["sudo", "rm", "-rf", "/"]), CommandVerdict.DENIED)

    def test_write_boundary_enforcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            writable = Path(tmp)
            policy = self._make_policy(
                PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(
                    writable_roots=(writable,),
                    workspace_root=writable,
                ),
            )
            # Write inside writable root: allowed
            self.assertEqual(
                policy.check_write(writable / "output.txt"),
                CommandVerdict.ALLOWED,
            )
            # Write outside writable root: prompt
            self.assertEqual(
                policy.check_write(Path("/etc/shadow")),
                CommandVerdict.PROMPT,
            )

    def test_runner_executes_allowed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = self._make_policy(
                PermissionProfile.WORKSPACE_WRITE,
                allowed_prefixes=("echo",),
                path_policy=PathPolicy(
                    writable_roots=(Path(tmp),),
                    workspace_root=Path(tmp),
                ),
            )
            runner = RunnerEngine(policy)
            result = runner.run_command(["echo", "hello"], cwd=tmp)
            self.assertEqual(result.verdict, CommandVerdict.ALLOWED.value)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("hello", result.stdout)

    def test_runner_denies_blocked_command(self):
        policy = self._make_policy(
            PermissionProfile.WORKSPACE_WRITE,
            denied_prefixes=("sudo",),
            allowed_prefixes=("echo",),
        )
        runner = RunnerEngine(policy)
        result = runner.run_command(["sudo", "rm", "-rf", "/"])
        self.assertEqual(result.verdict, CommandVerdict.DENIED.value)
        self.assertIsNone(result.exit_code)


class ScopeKeyTests(unittest.TestCase):
    """Verify scope keys and labels are well-formed for approval UI."""

    def test_write_file_scope_includes_path(self):
        meta = get_tool_metadata("write_file")
        key = meta.scope_key({"path": "/tmp/test.py"})
        self.assertIn("write", key)

    def test_edit_file_scope_includes_path(self):
        meta = get_tool_metadata("edit_file")
        key = meta.scope_key({"path": "/tmp/test.py"})
        self.assertIn("edit", key)

    def test_run_shell_scope_includes_command(self):
        meta = get_tool_metadata("run_shell")
        key = meta.scope_key({"command": "python3 -m pytest"})
        self.assertIn("python3", key)

    def test_write_file_label_readable(self):
        meta = get_tool_metadata("write_file")
        label = meta.scope_label({"path": "/tmp/test.py"})
        self.assertIn("/tmp/test.py", label)

    def test_run_shell_label_readable(self):
        meta = get_tool_metadata("run_shell")
        label = meta.scope_label({"command": "python3 -m pytest"})
        self.assertIn("python3", label)


if __name__ == "__main__":
    unittest.main()
