"""Action tools — governed file write, edit, and shell execution for Boss agents.

These tools give Boss the ability to modify files and run shell commands,
all routed through the existing Boss runner/policy system for governance.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from boss.control import is_path_allowed_for_agent
from boss.execution import ExecutionType, display_value, governed_function_tool, scope_value
from boss.runner.engine import RunnerEngine, current_runner, get_runner


def _get_runner() -> RunnerEngine:
    """Return the current runner or create one for agent mode."""
    runner = current_runner()
    if runner is not None:
        return runner
    return get_runner(mode="agent")


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
    if verdict == CommandVerdict.DENIED:
        return f"Write denied by policy: {path} is outside writable roots."
    if verdict == CommandVerdict.PROMPT:
        return f"Write to {path} requires approval (outside writable roots)."

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
    if verdict == CommandVerdict.DENIED:
        return f"Edit denied by policy: {path} is outside writable roots."
    if verdict == CommandVerdict.PROMPT:
        return f"Edit to {path} requires approval (outside writable roots)."

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

    Args:
        command: The shell command to run (e.g. 'python3 -m pytest tests/').
        cwd: Working directory. Defaults to workspace root.
        timeout: Maximum seconds to wait. Default 30, max 300.
    """
    if not command.strip():
        return "Error: empty command."

    timeout = max(5, min(timeout, 300))

    runner = _get_runner()

    # Split command for the runner (simple whitespace split; the runner
    # handles prefix matching on the normalized form).
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

    if result.verdict == CommandVerdict.DENIED.value:
        return f"Command denied: {result.denied_reason or 'blocked by policy'}"
    if result.verdict == CommandVerdict.PROMPT.value:
        return f"Command requires approval: {result.denied_reason or 'needs permission'}"

    # Format output
    output = result.output
    max_output = 50_000
    if len(output) > max_output:
        output = output[:max_output] + f"\n... (truncated, {len(result.output)} bytes total)"

    status = f"exit code {result.exit_code}" if result.exit_code is not None else "completed"
    header = f"[{status}, {result.duration_ms:.0f}ms]"

    if not output:
        return f"{header} (no output)"
    return f"{header}\n{output}"
