"""Tests for Boss-native action tools: write_file, edit_file, run_shell."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, ExecutionType, get_tool_metadata
from boss.runner.engine import RunnerEngine, current_runner, get_runner
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
    """Test write_file through the FunctionTool invoke path."""

    def _invoke(self, args: dict) -> str:
        """Invoke write_file through the FunctionTool.on_invoke_tool path."""
        import asyncio
        import json
        from agents.tool import ToolContext
        from boss.tools.action import write_file as wf_tool

        args_json = json.dumps(args)
        ctx = ToolContext(
            context=None,
            tool_name="write_file",
            tool_call_id="test",
            tool_arguments=args_json,
        )
        return asyncio.run(wf_tool.on_invoke_tool(ctx, args_json))

    def test_write_creates_file_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "new.txt")
            # Install a runner whose writable roots include tmp
            from boss.runner.engine import _current_runner_var
            from boss.runner.policy import runner_config_for_mode

            policy = runner_config_for_mode("agent", tmp)
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({"path": target, "content": "hello\n"})
                self.assertIn("Created", result)
                self.assertTrue(Path(target).exists())
                self.assertEqual(Path(target).read_text(), "hello\n")
            finally:
                _current_runner_var.set(None)

    def test_write_denied_outside_writable_roots(self):
        """PathPolicy should deny writes outside writable roots via the tool."""
        with tempfile.TemporaryDirectory() as tmp:
            from boss.runner.engine import _current_runner_var

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(
                    writable_roots=(Path(tmp),),
                    workspace_root=Path(tmp),
                ),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=(),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({"path": "/etc/shadow", "content": "nope"})
                self.assertIn("denied", result.lower())
                self.assertNotIn("requires approval", result.lower())
            finally:
                _current_runner_var.set(None)


class EditFileLogicTests(unittest.TestCase):
    """Test edit_file through the FunctionTool invoke path."""

    def _invoke(self, args: dict) -> str:
        import asyncio
        import json
        from agents.tool import ToolContext
        from boss.tools.action import edit_file as ef_tool

        args_json = json.dumps(args)
        ctx = ToolContext(
            context=None,
            tool_name="edit_file",
            tool_call_id="test",
            tool_arguments=args_json,
        )
        return asyncio.run(ef_tool.on_invoke_tool(ctx, args_json))

    def test_single_occurrence_replacement_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "code.py"
            target.write_text("def hello():\n    return 1\n", encoding="utf-8")

            from boss.runner.engine import _current_runner_var
            from boss.runner.policy import runner_config_for_mode

            policy = runner_config_for_mode("agent", tmp)
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({
                    "path": str(target),
                    "old_string": "return 1",
                    "new_string": "return 42",
                })
                self.assertIn("Edited", result)
                self.assertIn("return 42", target.read_text())
            finally:
                _current_runner_var.set(None)

    def test_multiple_occurrences_rejected_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dup.py"
            target.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")

            from boss.runner.engine import _current_runner_var
            from boss.runner.policy import runner_config_for_mode

            policy = runner_config_for_mode("agent", tmp)
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({
                    "path": str(target),
                    "old_string": "x = 1",
                    "new_string": "x = 2",
                })
                self.assertIn("appears", result.lower())
            finally:
                _current_runner_var.set(None)

    def test_zero_occurrences_rejected_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clean.py"
            target.write_text("def hello():\n    pass\n", encoding="utf-8")

            from boss.runner.engine import _current_runner_var
            from boss.runner.policy import runner_config_for_mode

            policy = runner_config_for_mode("agent", tmp)
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({
                    "path": str(target),
                    "old_string": "nonexistent string",
                    "new_string": "replacement",
                })
                self.assertIn("not found", result.lower())
            finally:
                _current_runner_var.set(None)


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


class RunnerTokenLeakTests(unittest.TestCase):
    """Verify _get_runner does not leak a runner into the caller context."""

    def test_get_runner_does_not_install_into_context_var(self):
        """When no runner exists, _get_runner must not install one in the context var."""
        from boss.runner.engine import _current_runner_var
        from boss.tools.action import _get_runner

        _current_runner_var.set(None)
        runner = _get_runner()
        self.assertIsNotNone(runner, "_get_runner should return a valid runner")
        self.assertIsNone(
            current_runner(),
            "_get_runner created a fallback but leaked it into the context var",
        )

    def test_get_runner_returns_existing_runner_without_replacement(self):
        """When a runner exists, _get_runner should return it unchanged."""
        from boss.runner.engine import _current_runner_var
        from boss.tools.action import _get_runner

        existing = RunnerEngine(
            ExecutionPolicy(
                profile=PermissionProfile.READ_ONLY,
                path_policy=PathPolicy(writable_roots=(), workspace_root=None),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=(),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=False,
                env_scrub_keys=(),
            )
        )
        _current_runner_var.set(existing)
        try:
            runner = _get_runner()
            self.assertIs(runner, existing)
        finally:
            _current_runner_var.set(None)


class RunShellIntegrationTests(unittest.TestCase):
    """Test run_shell through the actual FunctionTool invoke path."""

    def _invoke(self, args: dict) -> str:
        import asyncio
        import json
        from agents.tool import ToolContext
        from boss.tools.action import run_shell as rs_tool

        args_json = json.dumps(args)
        ctx = ToolContext(
            context=None,
            tool_name="run_shell",
            tool_call_id="test",
            tool_arguments=args_json,
        )
        return asyncio.run(rs_tool.on_invoke_tool(ctx, args_json))

    def test_allowed_command_executes_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            from boss.runner.engine import _current_runner_var

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(
                    writable_roots=(Path(tmp),),
                    workspace_root=Path(tmp),
                ),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("echo",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({"command": "echo hello", "cwd": tmp})
                self.assertIn("hello", result)
                self.assertIn("exit code 0", result)
            finally:
                _current_runner_var.set(None)

    def test_prompt_verdict_becomes_denial_through_tool(self):
        """Commands that trigger PROMPT should be denied, not left in limbo."""
        with tempfile.TemporaryDirectory() as tmp:
            from boss.runner.engine import _current_runner_var

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(
                    writable_roots=(Path(tmp),),
                    workspace_root=Path(tmp),
                ),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("echo",),
                prompt_prefixes=(),
                denied_prefixes=(),
                allow_shell=True,
                env_scrub_keys=(),
            )
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                # 'docker' is not in allowed_prefixes → PROMPT
                result = self._invoke({"command": "docker build .", "cwd": tmp})
                self.assertIn("denied", result.lower())
                # Must not pretend it's a live approval — it's a hard stop
                self.assertTrue(
                    result.startswith("Command denied"),
                    f"PROMPT should be surfaced as a denial, got: {result}",
                )
            finally:
                _current_runner_var.set(None)

    def test_denied_command_blocked_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            from boss.runner.engine import _current_runner_var

            policy = ExecutionPolicy(
                profile=PermissionProfile.WORKSPACE_WRITE,
                path_policy=PathPolicy(
                    writable_roots=(Path(tmp),),
                    workspace_root=Path(tmp),
                ),
                network=NetworkPolicy.DISABLED,
                domain_allowlist=(),
                allowed_prefixes=("echo",),
                prompt_prefixes=(),
                denied_prefixes=("sudo",),
                allow_shell=True,
                env_scrub_keys=(),
            )
            runner = RunnerEngine(policy)
            _current_runner_var.set(runner)
            try:
                result = self._invoke({"command": "sudo rm -rf /", "cwd": tmp})
                self.assertIn("denied", result.lower())
            finally:
                _current_runner_var.set(None)


if __name__ == "__main__":
    unittest.main()
