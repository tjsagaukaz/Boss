"""Runner engine: routes execution through policy, records results."""

from __future__ import annotations

import contextvars
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boss.runner.policy import (
    CommandVerdict,
    ExecutionPolicy,
    PermissionProfile,
    runner_config_for_mode,
)

# Commands whose first positional argument is a write target.
_WRITE_TARGET_COMMANDS: dict[str, int] = {
    "touch": 1,
    "tee": 1,
    "cp": -1,      # last arg
    "mv": -1,      # last arg
    "install": -1,  # last arg
}
# Commands where the argument after a flag is the write target.
_WRITE_FLAG_COMMANDS: dict[str, tuple[str, ...]] = {
    "screencapture": (),  # last positional arg is the file
}


@dataclass
class ExecutionResult:
    """Result of a command executed through the runner."""
    command: str | list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    verdict: str  # CommandVerdict value
    policy_profile: str  # PermissionProfile value
    enforcement: str  # "boss" or "os"
    duration_ms: float
    working_directory: str | None = None
    env_scrubbed: bool = False
    denied_reason: str | None = None
    task_workspace_id: str | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and self.verdict == CommandVerdict.ALLOWED.value

    @property
    def output(self) -> str:
        return self.stdout.strip() or self.stderr.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_length": len(self.stdout),
            "stderr_length": len(self.stderr),
            "verdict": self.verdict,
            "policy_profile": self.policy_profile,
            "enforcement": self.enforcement,
            "duration_ms": self.duration_ms,
            "working_directory": self.working_directory,
            "env_scrubbed": self.env_scrubbed,
            "denied_reason": self.denied_reason,
            "task_workspace_id": self.task_workspace_id,
            "success": self.success,
        }


class RunnerEngine:
    """Central execution engine that enforces policy on all command execution."""

    def __init__(self, policy: ExecutionPolicy):
        self._policy = policy

    @property
    def policy(self) -> ExecutionPolicy:
        return self._policy

    def run_command(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 30,
        cwd: Path | str | None = None,
        task_workspace_id: str | None = None,
    ) -> ExecutionResult:
        """Execute a command through the policy engine."""
        verdict = self._policy.check_command(command)

        if verdict == CommandVerdict.DENIED:
            return ExecutionResult(
                command=command,
                exit_code=None,
                stdout="",
                stderr="",
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=0.0,
                working_directory=str(cwd) if cwd else None,
                denied_reason=f"Command denied by {self._policy.profile.value} policy",
                task_workspace_id=task_workspace_id,
            )

        if verdict == CommandVerdict.PROMPT:
            return ExecutionResult(
                command=command,
                exit_code=None,
                stdout="",
                stderr="",
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=0.0,
                working_directory=str(cwd) if cwd else None,
                denied_reason=f"Command requires approval under {self._policy.profile.value} policy",
                task_workspace_id=task_workspace_id,
            )

        # Enforce write-path boundaries for commands with detectable write targets
        write_targets = _extract_write_targets(command, cwd)
        for target in write_targets:
            write_verdict = self._policy.check_write(target)
            if write_verdict != CommandVerdict.ALLOWED:
                # Both DENIED and PROMPT block execution here.  PROMPT means
                # "needs approval first" — the governed tool layer will surface
                # the approval request; the runner must not silently execute.
                return ExecutionResult(
                    command=command,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    verdict=write_verdict.value,
                    policy_profile=self._policy.profile.value,
                    enforcement=self._policy.enforcement,
                    duration_ms=0.0,
                    working_directory=str(cwd) if cwd else None,
                    denied_reason=self._policy.path_policy.explain_denial(target),
                    task_workspace_id=task_workspace_id,
                )

        # For workspace_write profile, enforce that the process cwd stays
        # within writable roots.  This is the primary containment for
        # interpreted code whose write targets cannot be statically analysed.
        effective_cwd = cwd
        if self._policy.profile == PermissionProfile.WORKSPACE_WRITE:
            if effective_cwd is None and self._policy.path_policy.workspace_root:
                effective_cwd = self._policy.path_policy.workspace_root
            if effective_cwd is not None and self._policy.path_policy.writable_roots:
                resolved_cwd = Path(effective_cwd).resolve()
                cwd_allowed = any(
                    _is_within(resolved_cwd, root)
                    for root in self._policy.path_policy.writable_roots
                )
                if not cwd_allowed:
                    return ExecutionResult(
                        command=command,
                        exit_code=None,
                        stdout="",
                        stderr="",
                        verdict=CommandVerdict.DENIED.value,
                        policy_profile=self._policy.profile.value,
                        enforcement=self._policy.enforcement,
                        duration_ms=0.0,
                        working_directory=str(effective_cwd),
                        denied_reason=f"Working directory {effective_cwd} is outside writable roots",
                        task_workspace_id=task_workspace_id,
                    )

        working_dir = str(effective_cwd) if effective_cwd else None
        env = self._policy.scrubbed_env() if self._policy.profile != PermissionProfile.FULL_ACCESS else None
        env_scrubbed = env is not None

        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                cwd=working_dir,
                env=env,
            )
            duration_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                command=command,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=duration_ms,
                working_directory=working_dir,
                env_scrubbed=env_scrubbed,
                task_workspace_id=task_workspace_id,
            )
        except subprocess.TimeoutExpired:
            duration_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=duration_ms,
                working_directory=working_dir,
                env_scrubbed=env_scrubbed,
                denied_reason=f"Timeout after {timeout}s",
                task_workspace_id=task_workspace_id,
            )
        except OSError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=duration_ms,
                working_directory=working_dir,
                env_scrubbed=env_scrubbed,
                denied_reason=str(exc),
                task_workspace_id=task_workspace_id,
            )

    def check_write(self, target: Path) -> CommandVerdict:
        """Check whether a write to the given path is allowed under current policy."""
        return self._policy.check_write(target)

    def check_network(self, domain: str | None = None) -> CommandVerdict:
        """Check whether network access is allowed, optionally for a specific domain."""
        return self._policy.check_network(domain)

    # ── Long-lived process management ──────────────────────────────

    def start_managed_process(
        self,
        command: list[str],
        *,
        cwd: Path | str | None = None,
        shell: bool = False,
    ) -> tuple[subprocess.Popen | None, ExecutionResult]:
        """Start a long-lived process under runner policy.

        Runs the same command/path/cwd checks as ``run_command`` but uses
        ``subprocess.Popen`` instead of ``subprocess.run``.  Returns the
        process handle paired with the policy verdict.  If the verdict is
        not ALLOWED the handle is None and the process is never started.
        """
        verdict = self._policy.check_command(command)

        if verdict != CommandVerdict.ALLOWED:
            reason = (
                f"Command denied by {self._policy.profile.value} policy"
                if verdict == CommandVerdict.DENIED
                else f"Command requires approval under {self._policy.profile.value} policy"
            )
            return None, ExecutionResult(
                command=command,
                exit_code=None,
                stdout="",
                stderr="",
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=0.0,
                working_directory=str(cwd) if cwd else None,
                denied_reason=reason,
            )

        # Write-target check
        write_targets = _extract_write_targets(command, cwd)
        for target in write_targets:
            wv = self._policy.check_write(target)
            if wv != CommandVerdict.ALLOWED:
                return None, ExecutionResult(
                    command=command,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    verdict=wv.value,
                    policy_profile=self._policy.profile.value,
                    enforcement=self._policy.enforcement,
                    duration_ms=0.0,
                    working_directory=str(cwd) if cwd else None,
                    denied_reason=self._policy.path_policy.explain_denial(target),
                )

        # CWD boundary check for workspace_write profile
        effective_cwd = cwd
        if self._policy.profile == PermissionProfile.WORKSPACE_WRITE:
            if effective_cwd is None and self._policy.path_policy.workspace_root:
                effective_cwd = self._policy.path_policy.workspace_root
            if effective_cwd is not None and self._policy.path_policy.writable_roots:
                resolved_cwd = Path(effective_cwd).resolve()
                cwd_allowed = any(
                    _is_within(resolved_cwd, root)
                    for root in self._policy.path_policy.writable_roots
                )
                if not cwd_allowed:
                    return None, ExecutionResult(
                        command=command,
                        exit_code=None,
                        stdout="",
                        stderr="",
                        verdict=CommandVerdict.DENIED.value,
                        policy_profile=self._policy.profile.value,
                        enforcement=self._policy.enforcement,
                        duration_ms=0.0,
                        working_directory=str(effective_cwd),
                        denied_reason=f"Working directory {effective_cwd} is outside writable roots",
                    )

        working_dir = str(effective_cwd) if effective_cwd else None
        env = self._policy.scrubbed_env() if self._policy.profile != PermissionProfile.FULL_ACCESS else None

        try:
            proc = subprocess.Popen(
                command if not shell else " ".join(command),
                shell=shell,
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
                env=env,
            )
            return proc, ExecutionResult(
                command=command,
                exit_code=None,
                stdout="",
                stderr="",
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=0.0,
                working_directory=working_dir,
                env_scrubbed=env is not None,
            )
        except OSError as exc:
            return None, ExecutionResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                verdict=verdict.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=0.0,
                working_directory=working_dir,
                env_scrubbed=env is not None,
                denied_reason=str(exc),
            )

    def terminate_managed_process(self, pid: int, *, sig: int = signal.SIGTERM) -> ExecutionResult:
        """Terminate a previously started managed process by PID.

        The kill goes through the runner so that it is recorded and
        governed rather than bypassing the execution layer.
        """
        start = time.monotonic()
        try:
            os.killpg(os.getpgid(pid), sig)
            duration_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                command=["kill", f"-{sig}", str(pid)],
                exit_code=0,
                stdout="",
                stderr="",
                verdict=CommandVerdict.ALLOWED.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=duration_ms,
            )
        except (OSError, ProcessLookupError) as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                command=["kill", f"-{sig}", str(pid)],
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                verdict=CommandVerdict.ALLOWED.value,
                policy_profile=self._policy.profile.value,
                enforcement=self._policy.enforcement,
                duration_ms=duration_ms,
                denied_reason=str(exc),
            )

    def status_payload(self) -> dict[str, Any]:
        """Return diagnostic information about the active runner."""
        return {
            "active": True,
            "policy": self._policy.to_dict(),
        }


# ---- Context-scoped runner (safe for concurrent runs) ----

_current_runner_var: contextvars.ContextVar[RunnerEngine | None] = contextvars.ContextVar(
    "boss_runner", default=None
)


def get_runner(mode: str | None = None, workspace_root: Path | str | None = None) -> RunnerEngine:
    """Create a RunnerEngine for the given mode and workspace, set it on the current context."""
    policy = runner_config_for_mode(mode, workspace_root)
    engine = RunnerEngine(policy)
    _current_runner_var.set(engine)
    return engine


def current_runner() -> RunnerEngine | None:
    """Return the runner for the current async/thread context, or None."""
    return _current_runner_var.get()


def _extract_write_targets(command: list[str], cwd: Path | str | None = None) -> list[Path]:
    """Best-effort extraction of write-target paths from a command."""
    if not command:
        return []

    base = command[0].rsplit("/", 1)[-1]  # strip path to get binary name
    targets: list[Path] = []
    # Collect positional arguments (skip flags)
    positional = [arg for arg in command[1:] if not arg.startswith("-")]

    if base in _WRITE_TARGET_COMMANDS:
        idx = _WRITE_TARGET_COMMANDS[base]
        if idx == -1 and positional:
            targets.append(Path(positional[-1]))
        elif 0 < idx <= len(positional):
            targets.extend(Path(p) for p in positional[idx - 1:])
    elif base in _WRITE_FLAG_COMMANDS:
        # Last positional is the output file (e.g. screencapture)
        if positional:
            targets.append(Path(positional[-1]))

    # Handle shell output redirection in simple cases
    cmd_str = " ".join(command)
    for match in re.finditer(r"(?:>>?)\s*(\S+)", cmd_str):
        targets.append(Path(match.group(1)))

    # Resolve relative paths against cwd
    base_dir = Path(cwd) if cwd else Path.cwd()
    resolved: list[Path] = []
    for t in targets:
        if not t.is_absolute():
            t = base_dir / t
        resolved.append(t)
    return resolved


def _is_within(child: Path, parent: Path) -> bool:
    """Return True if *child* is equal to or nested under *parent*."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
