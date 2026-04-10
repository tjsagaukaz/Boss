"""Adapter layer for OpenAI Agents SDK runtime tools (ShellTool, ApplyPatchTool).

Boss wraps these SDK tools so that all execution flows through Boss-native
governance first.  The SDK's own approval model is deliberately bypassed:
Boss calls ``governed_function_tool`` for approval, then passes the already-
approved operation to the SDK executor or editor as a plain callback — never
letting the SDK independently gate or silently run anything.

Design:
- ``boss_shell_executor``: a ``ShellExecutor`` callback that routes commands
  through ``RunnerEngine`` before executing.  Used when ``sdk_shell_backend``
  is enabled in Settings.
- ``BossApplyPatchEditor``: an ``ApplyPatchEditor`` implementation that
  enforces writable-root policy via ``RunnerEngine.check_write`` before
  applying diffs.

Neither adapter creates or installs runners into the context-var (learned
from the Phase 1 leak fix).

Why not use SDK built-in approval?
- SDK ``ShellTool`` and ``ApplyPatchTool`` have their own ``needs_approval``
  / ``on_approval`` mechanisms, but those operate inside the SDK runner loop,
  separate from Boss's ``governed_function_tool`` wrapper, permission ledger,
  scope keys, and pending-run persistence.  Using both would create a hidden
  second approval channel.
- Boss keeps one canonical trust boundary: the ``governed_function_tool``
  decorator.  The SDK tools are used only as execution backends, never as
  independent approval surfaces.

Why not use ``LocalShellTool``?
- ``LocalShellTool`` is the older API being replaced by ``ShellTool`` with
  a local executor.  It offers strictly fewer hooks and no approval
  integration.  Using the newer ``ShellTool`` executor protocol is the
  forward-compatible choice.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from boss.runner.engine import RunnerEngine, current_runner
from boss.runner.policy import CommandVerdict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared runner helper (no context-var leak)
# ---------------------------------------------------------------------------

def _get_runner() -> RunnerEngine:
    """Return the current runner or build an ephemeral one without leaking."""
    runner = current_runner()
    if runner is not None:
        return runner
    from boss.runner.policy import runner_config_for_mode

    policy = runner_config_for_mode("agent", None)
    return RunnerEngine(policy)


# ---------------------------------------------------------------------------
# SDK ShellTool executor backend
# ---------------------------------------------------------------------------

def boss_shell_executor(request: Any) -> Any:
    """A ``ShellExecutor`` callback for SDK ``ShellTool``.

    This is called by the SDK runner loop *after* Boss has already approved the
    tool call through the governed wrapper.  It routes each command through
    ``RunnerEngine.run_command`` so that runner-level policy (allowed prefixes,
    denied prefixes, write boundaries) still applies.

    Returns an ``agents.ShellResult`` so the SDK can format the output.
    """
    from agents import ShellResult, ShellCommandOutput, ShellCallOutcome

    action = request.data.action
    commands: list[str] = action.commands
    timeout_ms: int | None = action.timeout_ms
    timeout_s = max(5, min((timeout_ms or 30_000) // 1000, 300))

    runner = _get_runner()
    outputs: list[ShellCommandOutput] = []

    for cmd_str in commands:
        import shlex
        try:
            parts = shlex.split(cmd_str)
        except ValueError as exc:
            outputs.append(ShellCommandOutput(
                stdout="",
                stderr=f"Error parsing command: {exc}",
                outcome=ShellCallOutcome(type="exit", exit_code=1),
                command=cmd_str,
            ))
            continue

        if not parts:
            outputs.append(ShellCommandOutput(
                stdout="",
                stderr="Empty command after parsing.",
                outcome=ShellCallOutcome(type="exit", exit_code=1),
                command=cmd_str,
            ))
            continue

        result = runner.run_command(parts, timeout=timeout_s)

        if result.verdict != CommandVerdict.ALLOWED.value:
            outputs.append(ShellCommandOutput(
                stdout="",
                stderr=f"Denied by runner policy: {result.denied_reason or 'blocked'}",
                outcome=ShellCallOutcome(type="exit", exit_code=1),
                command=cmd_str,
            ))
            continue

        exit_code = result.exit_code if result.exit_code is not None else 0
        outcome_type = "timeout" if result.exit_code is None and result.duration_ms > timeout_s * 1000 else "exit"
        outputs.append(ShellCommandOutput(
            stdout=result.stdout,
            stderr=result.stderr,
            outcome=ShellCallOutcome(type=outcome_type, exit_code=exit_code),
            command=cmd_str,
        ))

    return ShellResult(output=outputs)


# ---------------------------------------------------------------------------
# SDK ApplyPatchTool editor backend
# ---------------------------------------------------------------------------

class BossApplyPatchEditor:
    """An ``ApplyPatchEditor`` that enforces Boss writable-root policy.

    Each operation (create, update, delete) is checked against the runner's
    path policy before being executed.  Diffs are applied using the standard
    ``patch`` utility when available, with a Python fallback.
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = workspace_root

    def _check_write(self, path_str: str) -> tuple[bool, str]:
        """Check runner write policy.  Returns (allowed, reason)."""
        from boss.control import is_path_allowed_for_agent

        p = Path(path_str).expanduser().resolve()
        if not is_path_allowed_for_agent(p):
            return False, f"Access denied: {path_str}"

        runner = _get_runner()
        verdict = runner.check_write(p)
        if verdict != CommandVerdict.ALLOWED:
            return False, f"Write denied by runner policy: {path_str} is outside writable roots."
        return True, ""

    def create_file(self, operation: Any) -> Any:
        from agents import ApplyPatchResult

        path_str = operation.path
        allowed, reason = self._check_write(path_str)
        if not allowed:
            return ApplyPatchResult(status="failed", output=reason)

        p = Path(path_str).expanduser()
        if p.exists():
            return ApplyPatchResult(
                status="failed",
                output=f"File already exists: {path_str}. Use update_file for modifications.",
            )

        # create_file operations carry the full content in the diff field
        content = operation.diff or ""
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ApplyPatchResult(status="failed", output=f"Error creating {path_str}: {exc}")

        return ApplyPatchResult(status="completed", output=f"Created {path_str}")

    def update_file(self, operation: Any) -> Any:
        from agents import ApplyPatchResult

        path_str = operation.path
        allowed, reason = self._check_write(path_str)
        if not allowed:
            return ApplyPatchResult(status="failed", output=reason)

        p = Path(path_str).expanduser()
        if not p.is_file():
            return ApplyPatchResult(
                status="failed", output=f"File not found: {path_str}"
            )

        diff_text = operation.diff
        if not diff_text:
            return ApplyPatchResult(
                status="failed", output="No diff provided for update."
            )

        result = _apply_unified_diff(p, diff_text)
        return result

    def delete_file(self, operation: Any) -> Any:
        from agents import ApplyPatchResult

        path_str = operation.path
        allowed, reason = self._check_write(path_str)
        if not allowed:
            return ApplyPatchResult(status="failed", output=reason)

        p = Path(path_str).expanduser()
        if not p.is_file():
            return ApplyPatchResult(
                status="failed", output=f"File not found: {path_str}"
            )

        try:
            p.unlink()
        except OSError as exc:
            return ApplyPatchResult(status="failed", output=f"Error deleting {path_str}: {exc}")

        return ApplyPatchResult(status="completed", output=f"Deleted {path_str}")


def _apply_unified_diff(target: Path, diff_text: str) -> Any:
    """Apply a unified diff to *target*, trying ``patch`` first with a Python fallback."""
    from agents import ApplyPatchResult

    # Try the system ``patch`` utility for reliable diff application.
    result = _try_system_patch(target, diff_text)
    if result is not None:
        return result

    # Fallback: pure-Python line-level patch.
    return _python_patch_fallback(target, diff_text)


def _try_system_patch(target: Path, diff_text: str) -> Any | None:
    """Attempt to apply a unified diff using the system ``patch`` command.

    Returns an ``ApplyPatchResult`` on success or failure, or ``None`` if the
    ``patch`` command is not available.
    """
    from agents import ApplyPatchResult
    import shutil

    if shutil.which("patch") is None:
        return None

    try:
        proc = subprocess.run(
            ["patch", "--no-backup-if-mismatch", "-u", str(target)],
            input=diff_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return ApplyPatchResult(
                status="completed",
                output=f"Patched {target}" + (f"\n{proc.stdout}" if proc.stdout.strip() else ""),
            )
        return ApplyPatchResult(
            status="failed",
            output=f"patch failed (exit {proc.returncode}):\n{proc.stderr or proc.stdout}",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ApplyPatchResult(status="failed", output=f"patch error: {exc}")


def _python_patch_fallback(target: Path, diff_text: str) -> Any:
    """Apply a unified diff using pure Python when ``patch`` is unavailable.

    This handles the common case of a single-file unified diff.  It is not a
    full patch implementation — complex hunks or context mismatches will be
    reported as failures rather than silently mis-applied.
    """
    from agents import ApplyPatchResult

    try:
        original_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return ApplyPatchResult(status="failed", output=f"Error reading {target}: {exc}")

    try:
        patched_lines = _apply_hunks(original_lines, diff_text)
    except _PatchError as exc:
        return ApplyPatchResult(status="failed", output=f"Patch failed: {exc}")

    try:
        target.write_text("".join(patched_lines), encoding="utf-8")
    except OSError as exc:
        return ApplyPatchResult(status="failed", output=f"Error writing {target}: {exc}")

    return ApplyPatchResult(status="completed", output=f"Patched {target}")


class _PatchError(Exception):
    """Raised when the pure-Python patcher cannot apply a hunk."""


def _apply_hunks(original_lines: list[str], diff_text: str) -> list[str]:
    """Parse unified diff hunks and apply them to *original_lines*.

    Raises ``_PatchError`` on any mismatch.
    """
    import re

    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    lines = diff_text.splitlines(keepends=True)

    # Skip header lines (---, +++, diff lines)
    idx = 0
    while idx < len(lines) and not lines[idx].startswith("@@"):
        idx += 1

    result = list(original_lines)
    offset = 0  # cumulative line offset from previous hunks

    while idx < len(lines):
        m = hunk_header.match(lines[idx])
        if not m:
            idx += 1
            continue

        old_start = int(m.group(1)) - 1  # 0-based
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        idx += 1

        removed: list[str] = []
        added: list[str] = []
        context_before = 0
        in_change = False

        while idx < len(lines) and not lines[idx].startswith("@@"):
            line = lines[idx]
            if line.startswith("-"):
                removed.append(line[1:])
                in_change = True
            elif line.startswith("+"):
                added.append(line[1:])
                in_change = True
            elif line.startswith(" ") or line == "\n":
                content = line[1:] if line.startswith(" ") else line
                if not in_change:
                    context_before += 1
                removed.append(content)
                added.append(content)
            else:
                # End of hunk (e.g. "\ No newline at end of file")
                pass
            idx += 1

        # Apply this hunk
        apply_at = old_start + offset
        # Verify context lines match
        for i, rem_line in enumerate(removed):
            pos = apply_at + i
            if pos >= len(result):
                raise _PatchError(
                    f"Hunk at line {old_start + 1}: file too short "
                    f"(expected {len(removed)} lines from line {apply_at + 1})"
                )
            if result[pos] != rem_line:
                # Tolerate missing trailing newline
                if result[pos].rstrip("\n") != rem_line.rstrip("\n"):
                    raise _PatchError(
                        f"Hunk at line {old_start + 1}: context mismatch at line {pos + 1}.\n"
                        f"  expected: {rem_line!r}\n"
                        f"  got:      {result[pos]!r}"
                    )

        result[apply_at : apply_at + len(removed)] = added
        offset += len(added) - len(removed)

    return result


# ---------------------------------------------------------------------------
# Diagnostics / availability
# ---------------------------------------------------------------------------

def sdk_runtime_status() -> dict[str, Any]:
    """Return diagnostic information about SDK runtime tool availability."""
    from boss.config import settings

    status: dict[str, Any] = {}

    # ShellTool
    try:
        from agents import ShellTool as _ST  # noqa: F401
        status["shell_tool_available"] = True
    except ImportError:
        status["shell_tool_available"] = False

    status["shell_backend"] = (
        "sdk" if settings.sdk_shell_backend else "native"
    )

    # ApplyPatchTool
    try:
        from agents import ApplyPatchTool as _APT  # noqa: F401
        status["apply_patch_tool_available"] = True
    except ImportError:
        status["apply_patch_tool_available"] = False

    status["patch_backend"] = (
        "sdk" if settings.sdk_patch_backend else "native"
    )

    status["sdk_version"] = _sdk_version()

    return status


def _sdk_version() -> str:
    """Return the installed agents SDK version string."""
    try:
        import agents
        return getattr(agents, "__version__", "unknown")
    except ImportError:
        return "not installed"
