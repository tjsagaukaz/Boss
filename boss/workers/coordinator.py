"""Coordinator: task decomposition, worker assignment, result collection, output merge."""

from __future__ import annotations

import time
from typing import Any

from boss.workers.conflicts import ConflictReport, detect_conflicts, detect_directory_overlap
from boss.workers.roles import WorkerRole
from boss.workers.state import (
    WorkPlan,
    WorkPlanStatus,
    WorkerRecord,
    WorkerState,
    new_plan_id,
    new_worker_id,
    save_work_plan,
)


# ── Plan creation ───────────────────────────────────────────────────


def create_work_plan(
    *,
    task: str,
    project_path: str,
    session_id: str,
    max_concurrent: int = 3,
) -> WorkPlan:
    """Create a new work plan from a high-level task description.

    The coordinator initialises the plan; callers add workers via
    ``add_worker`` and start execution via the engine.
    """
    plan = WorkPlan(
        plan_id=new_plan_id(),
        task=task,
        project_path=project_path,
        session_id=session_id,
        max_concurrent=max(1, max_concurrent),
    )
    save_work_plan(plan)
    return plan


# ── Worker management ───────────────────────────────────────────────


def add_worker(
    plan: WorkPlan,
    *,
    role: WorkerRole,
    scope: str,
    file_targets: list[str] | None = None,
) -> WorkerRecord:
    """Add a worker to the plan (must be in PLANNING or READY status)."""
    if plan.status not in (WorkPlanStatus.PLANNING.value, WorkPlanStatus.READY.value):
        raise ValueError(f"Cannot add workers to plan in {plan.status} state")

    worker = WorkerRecord(
        worker_id=new_worker_id(),
        plan_id=plan.plan_id,
        role=role.value,
        scope=scope,
        file_targets=list(file_targets or []),
    )
    plan.workers.append(worker)
    save_work_plan(plan)
    return worker


def remove_worker(plan: WorkPlan, worker_id: str) -> bool:
    """Remove a pending worker from the plan."""
    before = len(plan.workers)
    plan.workers = [w for w in plan.workers if w.worker_id != worker_id]
    if len(plan.workers) < before:
        save_work_plan(plan)
        return True
    return False


# ── Pre-flight validation ──────────────────────────────────────────


def validate_plan(plan: WorkPlan) -> ConflictReport:
    """Run conflict detection on all workers. Returns a ConflictReport.

    If there are hard file conflicts the caller should resolve them
    before marking the plan as READY.
    """
    return detect_conflicts(plan.workers)


def validate_plan_directory_overlap(plan: WorkPlan) -> ConflictReport:
    """Softer directory-level overlap check (advisory)."""
    return detect_directory_overlap(plan.workers)


def mark_plan_ready(plan: WorkPlan, *, force: bool = False) -> ConflictReport:
    """Validate and promote plan to READY if no hard conflicts.

    Checks both exact file-target conflicts and directory-level overlap.
    If *force* is True, conflicts are stored but the plan is marked
    ready anyway (caller assumes responsibility for merge strategy).
    """
    report = validate_plan(plan)
    if not report.has_conflicts:
        report = validate_plan_directory_overlap(plan)

    if report.has_conflicts and not force:
        plan.error = report.summary()
        save_work_plan(plan)
        return report

    plan.status = WorkPlanStatus.READY.value
    if report.has_conflicts:
        plan.merge_strategy = "manual"
    save_work_plan(plan)
    return report


# ── Result collection ──────────────────────────────────────────────


def collect_worker_result(
    plan: WorkPlan,
    worker_id: str,
    *,
    state: WorkerState,
    result_summary: str = "",
    output_artifacts: list[str] | None = None,
    error: str | None = None,
) -> WorkerRecord | None:
    """Record a worker's outcome."""
    worker = _find_worker(plan, worker_id)
    if worker is None:
        return None

    worker.state = state.value
    worker.finished_at = time.time()
    worker.result_summary = result_summary
    worker.output_artifacts = list(output_artifacts or [])
    worker.error = error

    _maybe_finish_plan(plan)
    save_work_plan(plan)
    return worker


# ── Merge / summarize ──────────────────────────────────────────────


def finalize_plan(plan: WorkPlan, *, merge_summary: str = "") -> WorkPlan:
    """Mark the plan as completed after all workers are done."""
    plan.status = WorkPlanStatus.COMPLETED.value
    plan.merge_summary = merge_summary
    plan.finished_at = time.time()
    save_work_plan(plan)
    return plan


def cancel_plan(plan: WorkPlan, *, reason: str = "cancelled by user") -> WorkPlan:
    """Cancel the plan and any pending workers."""
    for w in plan.workers:
        if w.state in (WorkerState.PENDING.value, WorkerState.RUNNING.value):
            w.state = WorkerState.CANCELLED.value
            w.finished_at = time.time()
            w.error = reason
    plan.status = WorkPlanStatus.CANCELLED.value
    plan.error = reason
    plan.finished_at = time.time()
    save_work_plan(plan)
    return plan


def plan_summary(plan: WorkPlan) -> dict[str, Any]:
    """Build a human-readable summary of the plan and its workers."""
    workers_by_state: dict[str, int] = {}
    for w in plan.workers:
        workers_by_state[w.state] = workers_by_state.get(w.state, 0) + 1

    completed = [w for w in plan.workers if w.state == WorkerState.COMPLETED.value]
    failed = [w for w in plan.workers if w.state == WorkerState.FAILED.value]

    return {
        "plan_id": plan.plan_id,
        "task": plan.task,
        "status": plan.status,
        "worker_count": len(plan.workers),
        "workers_by_state": workers_by_state,
        "completed_summaries": [
            {"worker_id": w.worker_id, "role": w.role, "scope": w.scope, "summary": w.result_summary}
            for w in completed
        ],
        "failed_summaries": [
            {"worker_id": w.worker_id, "role": w.role, "scope": w.scope, "error": w.error}
            for w in failed
        ],
        "merge_strategy": plan.merge_strategy,
        "merge_summary": plan.merge_summary,
    }


# ── Internal helpers ────────────────────────────────────────────────


def _find_worker(plan: WorkPlan, worker_id: str) -> WorkerRecord | None:
    for w in plan.workers:
        if w.worker_id == worker_id:
            return w
    return None


def _maybe_finish_plan(plan: WorkPlan) -> None:
    """Transition plan to MERGING once all workers are terminal.

    Will not override a plan that has already been cancelled.
    """
    if plan.status not in (WorkPlanStatus.RUNNING.value,):
        return
    terminal = {WorkerState.COMPLETED.value, WorkerState.FAILED.value, WorkerState.CANCELLED.value}
    if all(w.state in terminal for w in plan.workers):
        plan.status = WorkPlanStatus.MERGING.value
