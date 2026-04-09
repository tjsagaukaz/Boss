"""Loop state: attempt tracking, persistence, and stop reasons."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from boss.config import settings


class StopReason(StrEnum):
    """Why the loop stopped."""
    SUCCESS = "success"
    MAX_ATTEMPTS = "max_attempts"
    MAX_COMMANDS = "max_commands"
    MAX_WALL_TIME = "max_wall_time"
    MAX_FAILURES = "max_failures"
    APPROVAL_BLOCKED = "approval_blocked"
    CANCELLED = "cancelled"
    ERROR = "error"


class LoopPhase(StrEnum):
    """Current phase of the loop lifecycle."""
    UNDERSTAND = "understand"
    GATHER_CONTEXT = "gather_context"
    PLAN = "plan"
    EDIT = "edit"
    TEST = "test"
    INSPECT = "inspect"
    VERIFY_PREVIEW = "verify_preview"
    DONE = "done"


@dataclass
class AttemptCommand:
    """A command executed during an attempt."""
    command: str
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    verdict: str
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "verdict": self.verdict,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AttemptCommand:
        return cls(
            command=data["command"],
            exit_code=data.get("exit_code"),
            stdout_tail=data.get("stdout_tail", ""),
            stderr_tail=data.get("stderr_tail", ""),
            verdict=data.get("verdict", "unknown"),
            timestamp=data.get("timestamp", 0.0),
        )


@dataclass
class LoopAttempt:
    """Record of a single loop iteration."""
    attempt_number: int
    started_at: float
    finished_at: float | None = None
    phase: str = LoopPhase.UNDERSTAND.value
    commands: list[AttemptCommand] = field(default_factory=list)
    test_passed: bool = False
    test_output_tail: str = ""
    diff_summary: str = ""
    assistant_output: str = ""
    error: str | None = None
    stop_reason: str | None = None
    verification_method: str | None = None
    preview_evidence: dict | None = None

    @property
    def duration_ms(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at) * 1000

    def to_dict(self) -> dict:
        return {
            "attempt_number": self.attempt_number,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "phase": self.phase,
            "commands": [c.to_dict() for c in self.commands],
            "test_passed": self.test_passed,
            "test_output_tail": self.test_output_tail,
            "diff_summary": self.diff_summary,
            "assistant_output": self.assistant_output,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "verification_method": self.verification_method,
            "preview_evidence": self.preview_evidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LoopAttempt:
        return cls(
            attempt_number=data["attempt_number"],
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            phase=data.get("phase", LoopPhase.UNDERSTAND.value),
            commands=[AttemptCommand.from_dict(c) for c in data.get("commands", [])],
            test_passed=data.get("test_passed", False),
            test_output_tail=data.get("test_output_tail", ""),
            diff_summary=data.get("diff_summary", ""),
            assistant_output=data.get("assistant_output", ""),
            error=data.get("error"),
            stop_reason=data.get("stop_reason"),
            verification_method=data.get("verification_method"),
            preview_evidence=data.get("preview_evidence"),
        )


@dataclass
class LoopState:
    """Full state of a loop run, persistable to disk."""
    loop_id: str
    session_id: str
    task_description: str
    budget: dict  # serialized LoopBudget
    execution_style: str
    started_at: float
    finished_at: float | None = None
    current_attempt: int = 0
    total_commands: int = 0
    total_test_failures: int = 0
    attempts: list[LoopAttempt] = field(default_factory=list)
    stop_reason: str | None = None
    phase: str = LoopPhase.UNDERSTAND.value
    micro_plan: list[str] = field(default_factory=list)
    pending_run_id: str | None = None
    job_id: str | None = None
    workspace_root: str | None = None
    log_path: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.stop_reason is not None

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        return {
            "loop_id": self.loop_id,
            "session_id": self.session_id,
            "task_description": self.task_description,
            "budget": self.budget,
            "execution_style": self.execution_style,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_attempt": self.current_attempt,
            "total_commands": self.total_commands,
            "total_test_failures": self.total_test_failures,
            "attempts": [a.to_dict() for a in self.attempts],
            "stop_reason": self.stop_reason,
            "phase": self.phase,
            "micro_plan": self.micro_plan,
            "pending_run_id": self.pending_run_id,
            "job_id": self.job_id,
            "workspace_root": self.workspace_root,
            "log_path": self.log_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LoopState:
        return cls(
            loop_id=data["loop_id"],
            session_id=data["session_id"],
            task_description=data["task_description"],
            budget=data["budget"],
            execution_style=data.get("execution_style", "iterative"),
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            current_attempt=data.get("current_attempt", 0),
            total_commands=data.get("total_commands", 0),
            total_test_failures=data.get("total_test_failures", 0),
            attempts=[LoopAttempt.from_dict(a) for a in data.get("attempts", [])],
            stop_reason=data.get("stop_reason"),
            phase=data.get("phase", LoopPhase.UNDERSTAND.value),
            micro_plan=data.get("micro_plan", []),
            pending_run_id=data.get("pending_run_id"),
            job_id=data.get("job_id"),
            workspace_root=data.get("workspace_root"),
            log_path=data.get("log_path"),
        )


def _loop_runs_dir() -> Path:
    d = settings.app_data_dir / "loop-runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_loop_state(state: LoopState) -> Path:
    """Persist loop state to disk."""
    path = _loop_runs_dir() / f"{state.loop_id}.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return path


def load_loop_state(loop_id: str) -> LoopState | None:
    """Load a persisted loop state."""
    path = _loop_runs_dir() / f"{loop_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LoopState.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def list_loop_states(limit: int = 50) -> list[LoopState]:
    """List recent loop states, newest first."""
    d = _loop_runs_dir()
    states: list[LoopState] = []
    for path in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        state = load_loop_state(path.stem)
        if state is not None:
            states.append(state)
            if len(states) >= limit:
                break
    return states
