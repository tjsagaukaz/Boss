"""Worker and work-plan state management."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from boss.config import settings

logger = logging.getLogger(__name__)

# Bump these when the serialized format changes.
WORKER_RECORD_VERSION = 1
WORK_PLAN_VERSION = 1


# ── Worker state ────────────────────────────────────────────────────

class WorkerState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkerRecord:
    """Tracks an individual worker's lifecycle."""

    worker_id: str
    plan_id: str
    role: str  # WorkerRole value
    scope: str  # human-readable description of assigned scope
    file_targets: list[str] = field(default_factory=list)
    state: str = WorkerState.PENDING.value
    workspace_id: str | None = None  # TaskWorkspace id (implementers)
    workspace_path: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result_summary: str = ""
    output_artifacts: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    version: int = WORKER_RECORD_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["version"] = WORKER_RECORD_VERSION
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> WorkerRecord:
        # Strip unknown fields for forward compatibility.
        filtered = {k: v for k, v in data.items() if k in WorkerRecord.__dataclass_fields__}
        return WorkerRecord(**filtered)


# ── Work plan (coordinator orchestration record) ───────────────────

class WorkPlanStatus(StrEnum):
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    MERGING = "merging"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkPlan:
    """A coordinator-created plan that decomposes a task into workers."""

    plan_id: str
    task: str
    project_path: str
    session_id: str
    status: str = WorkPlanStatus.PLANNING.value
    workers: list[WorkerRecord] = field(default_factory=list)
    merge_strategy: str = "sequential"  # sequential or manual
    merge_summary: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    max_concurrent: int = 3
    version: int = WORK_PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["version"] = WORK_PLAN_VERSION
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> WorkPlan:
        workers_data = data.pop("workers", [])
        # Strip unknown fields for forward compatibility.
        plan = WorkPlan(**{k: v for k, v in data.items() if k in WorkPlan.__dataclass_fields__})
        plan.workers = [WorkerRecord.from_dict(w) for w in workers_data]
        return plan


# ── Persistence ─────────────────────────────────────────────────────

def _plans_dir() -> Path:
    d = settings.app_data_dir / "work-plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plan_path(plan_id: str) -> Path:
    return _plans_dir() / f"{plan_id}.json"


def save_work_plan(plan: WorkPlan) -> Path:
    plan.updated_at = time.time()
    path = _plan_path(plan.plan_id)
    temp = path.with_suffix(".json.tmp")
    try:
        temp.write_text(json.dumps(plan.to_dict(), indent=2, default=str), encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        logger.error("Failed to save work plan %s: %s", plan.plan_id, exc)
        temp.unlink(missing_ok=True)
        raise
    return path


def load_work_plan(plan_id: str) -> WorkPlan | None:
    path = _plan_path(plan_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WorkPlan.from_dict(data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def list_work_plans(*, limit: int = 50) -> list[WorkPlan]:
    plans: list[WorkPlan] = []
    for p in sorted(_plans_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        if len(plans) >= limit:
            break
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            plans.append(WorkPlan.from_dict(data))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return plans


def new_plan_id() -> str:
    return uuid.uuid4().hex


def new_worker_id() -> str:
    return uuid.uuid4().hex[:12]
