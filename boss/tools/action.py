"""Action tools — governed file write, edit, patch, and shell execution for Boss agents.

These tools give Boss the ability to modify files and run shell commands,
all routed through the existing Boss runner/policy system for governance.

Tool surface:
- ``write_file``: create/overwrite a file (full content).
- ``edit_file``: exact single-occurrence string replacement.
- ``apply_patch``: apply a unified diff to one or more files.
- ``run_shell``: run a shell command through policy enforcement.

When ``sdk_shell_backend`` is enabled in Settings, ``run_shell`` delegates
to the Boss-adapted SDK ``ShellExecutor`` (``boss.sdk_runtime``) instead of
calling ``RunnerEngine.run_command`` directly.  The SDK path still routes
through the same runner policy — it is not an approval bypass.

``apply_patch`` always uses the Boss-native editor
(``boss.sdk_runtime.BossApplyPatchEditor``) which enforces writable-root
policy and falls back to a Python implementation when the system ``patch``
command is unavailable.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from boss.control import is_path_allowed_for_agent
from boss.execution import ExecutionType, display_value, governed_function_tool, scope_value
from boss.runner.engine import RunnerEngine, current_runner


def _get_runner() -> RunnerEngine:
    """Return the current runner or create a temporary one for this call.

    Important: when no runner exists in the current context, we build one
    directly instead of calling ``get_runner()`` — that helper installs its
    result into the context-var, which would leak a workspace-write runner
    into the caller's context for the rest of the turn.
    """
    runner = current_runner()
    if runner is not None:
        return runner
    # Build a runner without installing it into _current_runner_var.
    from boss.runner.policy import runner_config_for_mode

    policy = runner_config_for_mode("agent", None)
    return RunnerEngine(policy)


# ── write_file ──────────────────────────────────────────────────────

@governed_function_tool(
    execution_type=ExecutionType.EDIT,
    title="Write File",
    describe_call=lambda params: f'Write {params.get("path", "file")}',
    scope_key=lambda params: scope_value("fs", f'write:{params.get("path", "")}'),
    scope_label=lambda params: f'Write {display_value(params.get("path"), fallback="file")}',
)
def write_file(path: str, content: str, create_dirs: bool = False) -> str:
    """Create or overwrite a file with the given content.

    Args:
        path: Absolute path to the file.
        content: The full text content to write.
        create_dirs: If True, create parent directories as needed.
    """
    p = Path(path).expanduser()
    if not is_path_allowed_for_agent(p):
        return f"Access denied: {path}"

    runner = _get_runner()
    from boss.runner.policy import CommandVerdict

    verdict = runner.check_write(p)
    if verdict != CommandVerdict.ALLOWED:
        # Both DENIED and PROMPT are hard stops here.  The governed
        # wrapper already handled the tool-level approval; the runner
        # has no nested approval path, so PROMPT is an honest denial.
        return f"Write denied by runner policy: {path} is outside writable roots."

    if create_dirs:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.parent.is_dir():
        return f"Parent directory does not exist: {p.parent}"

    existed = p.is_file()
    try:
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Error writing {path}: {exc}"

    action = "Overwrote" if existed else "Created"
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"{action} {path} ({lines} lines, {len(content)} bytes)"


# ── edit_file ───────────────────────────────────────────────────────

@governed_function_tool(
    execution_type=ExecutionType.EDIT,
    title="Edit File",
    describe_call=lambda params: f'Edit {params.get("path", "file")}',
    scope_key=lambda params: scope_value("fs", f'edit:{params.get("path", "")}'),
    scope_label=lambda params: f'Edit {display_value(params.get("path"), fallback="file")}',
)
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in a file with a new string.

    The old_string must appear exactly once in the file. Include enough
    surrounding context (3+ lines) to uniquely identify the target.

    Args:
        path: Absolute path to the file.
        old_string: The exact text to find and replace (must appear once).
        new_string: The replacement text.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return f"File not found: {path}"
    if not is_path_allowed_for_agent(p):
        return f"Access denied: {path}"

    runner = _get_runner()
    from boss.runner.policy import CommandVerdict

    verdict = runner.check_write(p)
    if verdict != CommandVerdict.ALLOWED:
        return f"Edit denied by runner policy: {path} is outside writable roots."

    try:
        original = p.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading {path}: {exc}"

    count = original.count(old_string)
    if count == 0:
        return f"old_string not found in {path}. Verify the text matches exactly."
    if count > 1:
        return f"old_string appears {count} times in {path}. Include more context to match exactly once."

    updated = original.replace(old_string, new_string, 1)

    try:
        p.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return f"Error writing {path}: {exc}"

    # Show a compact diff summary
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, n=1))
    diff_summary = "".join(diff[:20]) if diff else "(no visible diff)"

    return f"Edited {path}: replaced {len(old_lines)} line(s) with {len(new_lines)} line(s).\n{diff_summary}"


# ── run_shell ───────────────────────────────────────────────────────

@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Run Shell Command",
    describe_call=lambda params: f'Run: {params.get("command", "")[:80]}',
    scope_key=lambda params: scope_value("shell", params.get("command", "")[:60]),
    scope_label=lambda params: f'Shell: {display_value(params.get("command"), fallback="command")[:80]}',
)
def run_shell(command: str, cwd: str = "", timeout: int = 30) -> str:
    """Execute a shell command through the Boss runner with policy enforcement.

    The command is checked against the active permission profile (allowed
    prefixes, denied prefixes, write boundaries, network policy).

    When the SDK shell backend is enabled (BOSS_SDK_SHELL_BACKEND=true),
    execution is routed through the Boss-adapted SDK ShellExecutor, which
    still enforces the same runner policy.

    Args:
        command: The shell command to run (e.g. 'python3 -m pytest tests/').
        cwd: Working directory. Defaults to workspace root.
        timeout: Maximum seconds to wait. Default 30, max 300.
    """
    if not command.strip():
        return "Error: empty command."

    timeout = max(5, min(timeout, 300))

    from boss.config import settings

    if settings.sdk_shell_backend:
        return _run_shell_sdk(command, cwd, timeout)
    return _run_shell_native(command, cwd, timeout)


def _run_shell_native(command: str, cwd: str, timeout: int) -> str:
    """Boss-native shell execution through RunnerEngine."""
    runner = _get_runner()

    import shlex
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return f"Error parsing command: {exc}"

    if not parts:
        return "Error: empty command after parsing."

    effective_cwd: Path | None = None
    if cwd:
        effective_cwd = Path(cwd).expanduser()
        if not effective_cwd.is_dir():
            return f"Working directory does not exist: {cwd}"

    result = runner.run_command(
        parts,
        timeout=timeout,
        cwd=effective_cwd,
    )

    from boss.runner.policy import CommandVerdict

    if result.verdict != CommandVerdict.ALLOWED.value:
        return f"Command denied by runner policy: {result.denied_reason or 'blocked by policy'}"

    return _format_shell_output(result)


def _run_shell_sdk(command: str, cwd: str, timeout: int) -> str:
    """SDK-backed shell execution via boss_shell_executor."""
    try:
        from agents import ShellActionRequest, ShellCallData, ShellCommandRequest
        from agents import RunContextWrapper
        from boss.sdk_runtime import boss_shell_executor
    except ImportError:
        # SDK not available — fall back to native
        return _run_shell_native(command, cwd, timeout)

    action = ShellActionRequest(
        commands=[command],
        timeout_ms=timeout * 1000,
    )
    call_data = ShellCallData(call_id="boss-native", action=action)
    request = ShellCommandRequest(
        ctx_wrapper=RunContextWrapper(context=None),
        data=call_data,
    )

    try:
        sdk_result = boss_shell_executor(request)
    except Exception as exc:
        return f"SDK shell error: {exc}"

    # Format the SDK result into the same shape as native output
    if not sdk_result.output:
        return "[completed] (no output)"

    parts = []
    for entry in sdk_result.output:
        exit_code = entry.outcome.exit_code
        status = f"exit code {exit_code}" if exit_code is not None else "completed"
        header = f"[{status}]"
        text = entry.stdout
        if entry.stderr:
            text = text + ("\n" if text else "") + entry.stderr
        if text:
            parts.append(f"{header}\n{text}")
        else:
            parts.append(f"{header} (no output)")

    return "\n".join(parts)


def _format_shell_output(result: object) -> str:
    """Format a RunnerEngine ExecutionResult into a human-readable string."""
    output = getattr(result, "output", "")
    max_output = 50_000
    if len(output) > max_output:
        full_len = len(output)
        output = output[:max_output] + f"\n... (truncated, {full_len} bytes total)"

    exit_code = getattr(result, "exit_code", None)
    duration_ms = getattr(result, "duration_ms", 0.0)
    status = f"exit code {exit_code}" if exit_code is not None else "completed"
    header = f"[{status}, {duration_ms:.0f}ms]"

    if not output:
        return f"{header} (no output)"
    return f"{header}\n{output}"


# ── apply_patch ─────────────────────────────────────────────────────

@governed_function_tool(
    execution_type=ExecutionType.EDIT,
    title="Apply Patch",
    describe_call=lambda params: f'Patch {params.get("path", "file")}',
    scope_key=lambda params: scope_value("fs", f'patch:{params.get("path", "")}'),
    scope_label=lambda params: f'Patch {display_value(params.get("path"), fallback="file")}',
)
def apply_patch(path: str, diff: str) -> str:
    """Apply a unified diff to a file.

    Use this for multi-line edits where a unified diff is more natural than
    exact string replacement.  The diff should be in standard unified format
    (output of ``diff -u`` or ``git diff``).

    For single-site string replacements, prefer ``edit_file``.
    For creating a new file from scratch, prefer ``write_file``.

    Args:
        path: Absolute path to the file to patch.
        diff: The unified diff text to apply.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return f"File not found: {path}"
    if not is_path_allowed_for_agent(p):
        return f"Access denied: {path}"

    runner = _get_runner()
    from boss.runner.policy import CommandVerdict

    verdict = runner.check_write(p)
    if verdict != CommandVerdict.ALLOWED:
        return f"Patch denied by runner policy: {path} is outside writable roots."

    if not diff.strip():
        return "Error: empty diff."

    from boss.sdk_runtime import _apply_unified_diff
    result = _apply_unified_diff(p, diff)

    # result is an ApplyPatchResult dataclass
    status = getattr(result, "status", "unknown")
    output = getattr(result, "output", "")
    if status == "completed":
        return output or f"Patched {path}"
    return f"Patch failed: {output or 'unknown error'}"
