"""Governed subprocess execution for iOS delivery.

Mirrors the pattern from ``boss.deploy.static_adapter._run_via_runner``
but specialized for iOS build commands (xcodebuild, fastlane, xcrun).

All subprocess execution flows through the Boss Runner when one is
available, ensuring command policy checks, environment scrubbing, and
write-target enforcement.  Falls back to direct subprocess when no
runner context exists (tests, standalone scripts).

Process handles are registered in a module-level registry so that
``cancel_run()`` can terminate builds immediately rather than waiting
for the next phase boundary.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Process registry for cancellation ──────────────────────────────

_process_lock = threading.Lock()
_live_processes: dict[str, subprocess.Popen] = {}


def register_build_process(run_id: str, proc: subprocess.Popen) -> None:
    with _process_lock:
        _live_processes[run_id] = proc


def unregister_build_process(run_id: str) -> None:
    with _process_lock:
        _live_processes.pop(run_id, None)


def terminate_build_process(run_id: str) -> bool:
    """Terminate a live build process by run_id.  Returns True if killed."""
    with _process_lock:
        proc = _live_processes.pop(run_id, None)
    if proc is None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=5)
        return True
    except (OSError, ProcessLookupError):
        return False


# ── Execution result ───────────────────────────────────────────────


@dataclass
class BuildResult:
    """Result of a governed iOS build command."""

    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    governed: bool  # True if went through Runner
    policy_verdict: str | None = None  # "allowed", "denied", "prompt"
    denied_reason: str | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined output (xcodebuild writes to both stdout and stderr)."""
        return (self.stdout + "\n" + self.stderr).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_length": len(self.stdout),
            "stderr_length": len(self.stderr),
            "duration_ms": self.duration_ms,
            "governed": self.governed,
            "policy_verdict": self.policy_verdict,
            "denied_reason": self.denied_reason,
            "success": self.success,
        }


# ── Governed execution ─────────────────────────────────────────────


def run_build_command(
    command: list[str],
    *,
    cwd: str | Path,
    timeout: int = 600,
    run_id: str | None = None,
) -> BuildResult:
    """Execute a build command through the Boss Runner if available.

    If no runner is in the current context, falls back to direct
    subprocess execution (useful for standalone/test runs).

    When *run_id* is provided the subprocess is registered for
    cancellation via ``terminate_build_process(run_id)``.

    Timeout defaults to 10 minutes — Xcode archive builds can be slow.
    """
    from boss.runner.engine import current_runner

    cwd_str = str(cwd)
    runner = current_runner()

    if runner is not None:
        return _run_governed(command, cwd=cwd_str, timeout=timeout, run_id=run_id, runner=runner)
    else:
        return _run_direct(command, cwd=cwd_str, timeout=timeout, run_id=run_id)


def _run_governed(
    command: list[str],
    *,
    cwd: str,
    timeout: int,
    run_id: str | None,
    runner: Any,
) -> BuildResult:
    """Execute through RunnerEngine.start_managed_process()."""
    proc, result = runner.start_managed_process(command, cwd=cwd)
    if proc is None:
        # Policy denied or prompt required
        return BuildResult(
            command=command,
            exit_code=None,
            stdout="",
            stderr=result.denied_reason or f"Command {result.verdict} by policy",
            duration_ms=0.0,
            governed=True,
            policy_verdict=result.verdict,
            denied_reason=result.denied_reason,
        )

    if run_id:
        register_build_process(run_id, proc)
    start = time.monotonic()
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout)
        duration_ms = (time.monotonic() - start) * 1000
        stdout = raw_out.decode("utf-8", errors="replace") if isinstance(raw_out, bytes) else raw_out
        stderr = raw_err.decode("utf-8", errors="replace") if isinstance(raw_err, bytes) else raw_err
        return BuildResult(
            command=command,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            governed=True,
            policy_verdict="allowed",
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        # Graceful shutdown: SIGTERM first, then SIGKILL after grace period
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=5)
        except (OSError, ProcessLookupError):
            proc.kill()
            proc.wait()
        return BuildResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=f"Build timed out after {timeout}s",
            duration_ms=duration_ms,
            governed=True,
            policy_verdict="allowed",
            denied_reason=f"Timeout after {timeout}s",
        )
    finally:
        if run_id:
            unregister_build_process(run_id)


def _run_direct(
    command: list[str],
    *,
    cwd: str,
    timeout: int,
    run_id: str | None,
) -> BuildResult:
    """Execute directly via subprocess (no runner context)."""
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    if run_id:
        register_build_process(run_id, proc)
    start = time.monotonic()
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout)
        duration_ms = (time.monotonic() - start) * 1000
        stdout = raw_out.decode("utf-8", errors="replace") if isinstance(raw_out, bytes) else raw_out
        stderr = raw_err.decode("utf-8", errors="replace") if isinstance(raw_err, bytes) else raw_err
        return BuildResult(
            command=command,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            governed=False,
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        # Graceful shutdown: SIGTERM first, then SIGKILL after grace period
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=5)
        except (OSError, ProcessLookupError):
            proc.kill()
            proc.wait()
        return BuildResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=f"Build timed out after {timeout}s",
            duration_ms=duration_ms,
            governed=False,
            denied_reason=f"Timeout after {timeout}s",
        )
    finally:
        if run_id:
            unregister_build_process(run_id)
