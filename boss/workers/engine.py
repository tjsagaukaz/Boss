"""Async worker execution engine with governance and failure handling."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, AsyncIterator

from boss.config import settings
from boss.runner.engine import RunnerEngine, get_runner
from boss.workers.conflicts import detect_conflicts, detect_directory_overlap
from boss.workers.coordinator import (
    cancel_plan,
    collect_worker_result,
    finalize_plan,
)
from boss.workers.isolation import (
    apply_workspace_changes,
    collect_workspace_changes,
    provision_workspace,
    release_workspace,
)
from boss.workers.roles import ROLE_AGENT_MODE, ROLE_PERMISSION_PROFILES, WorkerRole
from boss.workers.state import (
    WorkPlan,
    WorkPlanStatus,
    WorkerRecord,
    WorkerState,
    save_work_plan,
)

logger = logging.getLogger(__name__)

# ── Live task registry (for cancellation) ───────────────────────────

_running_tasks: dict[str, dict[str, asyncio.Task[None]]] = {}
"""plan_id -> {worker_id -> asyncio.Task}  for live cancel support."""

# ── SSE event helpers (mirrors boss.api.sse_event) ──────────────────


def _worker_event(plan_id: str, worker: WorkerRecord, event_kind: str, **extra: Any) -> dict[str, Any]:
    """Build a worker SSE event payload."""
    return {
        "type": "worker_status",
        "plan_id": plan_id,
        "worker_id": worker.worker_id,
        "role": worker.role,
        "scope": worker.scope,
        "state": worker.state,
        "event": event_kind,
        **extra,
    }


def _plan_event(plan: WorkPlan, event_kind: str, **extra: Any) -> dict[str, Any]:
    """Build a plan-level SSE event payload."""
    return {
        "type": "plan_status",
        "plan_id": plan.plan_id,
        "status": plan.status,
        "event": event_kind,
        "worker_count": len(plan.workers),
        **extra,
    }


# ── Single worker execution ────────────────────────────────────────


async def _run_single_worker(
    plan: WorkPlan,
    worker: WorkerRecord,
    event_queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Execute one worker inside its own runner context.

    All tool calls go through the runner which enforces the appropriate
    PermissionProfile for the worker's role.
    """
    role = WorkerRole(worker.role)
    mode = ROLE_AGENT_MODE[role]

    # Determine workspace root for this worker.
    workspace_root = worker.workspace_path or plan.project_path

    # Establish runner context with the role's policy.
    runner = get_runner(mode=mode, workspace_root=workspace_root)

    worker.state = WorkerState.RUNNING.value
    worker.started_at = time.time()
    save_work_plan(plan)
    await event_queue.put(_worker_event(plan.plan_id, worker, "started"))

    try:
        # Build a focused prompt for this worker.
        worker_prompt = _build_worker_prompt(plan.task, worker)

        # Import here to avoid circular imports with agents module.
        from agents import Runner
        from boss.agents import build_entry_agent, build_review_agent

        if role == WorkerRole.REVIEWER:
            # Use the review-specific agent which carries the findings-first
            # review prompt layer and review-specific tool set.
            agent = build_review_agent(output_type=str, workspace_root=workspace_root)
        else:
            agent = build_entry_agent(mode=mode, workspace_root=workspace_root)

        result = await Runner.run(agent, input=worker_prompt)
        result_text = result.final_output or ""

        # Log tool calls from the result.
        for item in getattr(result, "new_items", []):
            name = getattr(item, "name", None) or getattr(item, "tool_name", None)
            if name:
                worker.log_lines.append(f"tool: {name}")
                await event_queue.put(
                    _worker_event(plan.plan_id, worker, "tool_call", tool=name)
                )

        # Record success.
        collect_worker_result(
            plan,
            worker.worker_id,
            state=WorkerState.COMPLETED,
            result_summary=result_text[:4000],
        )
        await event_queue.put(_worker_event(plan.plan_id, worker, "completed"))

    except asyncio.CancelledError:
        collect_worker_result(
            plan,
            worker.worker_id,
            state=WorkerState.CANCELLED,
            error="Worker cancelled",
        )
        await event_queue.put(_worker_event(plan.plan_id, worker, "cancelled"))
        raise

    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        logger.warning("Worker %s failed: %s", worker.worker_id, err_msg)
        collect_worker_result(
            plan,
            worker.worker_id,
            state=WorkerState.FAILED,
            error=err_msg,
        )
        await event_queue.put(
            _worker_event(plan.plan_id, worker, "failed", error=err_msg)
        )


# ── Parallel plan execution ────────────────────────────────────────


async def execute_plan(plan: WorkPlan) -> AsyncIterator[dict[str, Any]]:
    """Execute all workers in a plan, respecting concurrency limits.

    Yields SSE-compatible event dicts as work progresses.
    Workers are executed in parallel up to ``plan.max_concurrent``.
    Partial failure is tolerated — other workers continue.
    """
    # Pre-flight: check for conflicts one more time (exact files + directory overlap).
    report = detect_conflicts(plan.workers)
    if not report.has_conflicts:
        dir_report = detect_directory_overlap(plan.workers)
        if dir_report.has_conflicts and plan.merge_strategy != "manual":
            report = dir_report

    if report.has_conflicts and plan.merge_strategy != "manual":
        plan.status = WorkPlanStatus.FAILED.value
        plan.error = report.summary()
        save_work_plan(plan)
        yield _plan_event(plan, "conflict_error", detail=report.summary())
        return

    plan.status = WorkPlanStatus.RUNNING.value
    save_work_plan(plan)
    yield _plan_event(plan, "started")

    # Provision isolated workspaces for implementers.
    for worker in plan.workers:
        if WorkerRole(worker.role) == WorkerRole.IMPLEMENTER:
            try:
                provision_workspace(worker, plan.project_path)
                save_work_plan(plan)
            except Exception as exc:
                worker.state = WorkerState.FAILED.value
                worker.error = f"Workspace provisioning failed: {exc}"
                save_work_plan(plan)
                yield _worker_event(plan.plan_id, worker, "failed", error=worker.error)

    # Build list of runnable workers.
    runnable = [w for w in plan.workers if w.state == WorkerState.PENDING.value]
    if not runnable:
        plan.status = WorkPlanStatus.FAILED.value
        plan.error = "No runnable workers after provisioning"
        save_work_plan(plan)
        yield _plan_event(plan, "failed", error=plan.error)
        return

    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    semaphore = asyncio.Semaphore(plan.max_concurrent)

    async def _guarded_run(w: WorkerRecord) -> None:
        async with semaphore:
            await _run_single_worker(plan, w, event_queue)

    # Launch all workers as tasks and register them for cancellation.
    tasks: dict[str, asyncio.Task[None]] = {}
    for w in runnable:
        tasks[w.worker_id] = asyncio.create_task(_guarded_run(w), name=f"worker-{w.worker_id}")
    _running_tasks[plan.plan_id] = tasks

    # Drain events as workers complete.
    pending_tasks = set(tasks.values())
    try:
        while pending_tasks:
            # Yield queued events.
            while not event_queue.empty():
                yield event_queue.get_nowait()

            # Wait for any task to finish (with a short timeout to keep draining events).
            done, pending_tasks = await asyncio.wait(
                pending_tasks,
                timeout=0.5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Propagate cancellation errors but don't stop other workers.
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    logger.warning("Worker task exception: %s", exc)
    finally:
        _running_tasks.pop(plan.plan_id, None)

    # Drain remaining events.
    while not event_queue.empty():
        yield event_queue.get_nowait()

    # If the plan was cancelled while running, skip merge/cleanup.
    if plan.status == WorkPlanStatus.CANCELLED.value:
        _cleanup_plan_workspaces(plan)
        yield _plan_event(plan, "cancelled")
        return

    # ── Merge phase: collect diffs and apply to source project ──────
    terminal = {WorkerState.COMPLETED.value, WorkerState.FAILED.value, WorkerState.CANCELLED.value}
    all_done = all(w.state in terminal for w in plan.workers)
    any_success = any(w.state == WorkerState.COMPLETED.value for w in plan.workers)

    if all_done and any_success:
        plan.status = WorkPlanStatus.MERGING.value
        save_work_plan(plan)
        yield _plan_event(plan, "merging")

        # Collect and apply diffs from implementer workspaces BEFORE cleanup.
        merge_results: list[str] = []
        for worker in plan.workers:
            if WorkerRole(worker.role) != WorkerRole.IMPLEMENTER:
                continue
            if worker.state != WorkerState.COMPLETED.value:
                continue

            changes = collect_workspace_changes(worker, plan.project_path)
            if changes is None:
                merge_results.append(f"[{worker.worker_id}] no changes detected")
                continue

            applied = apply_workspace_changes(changes, plan.project_path)
            if applied:
                merge_results.append(
                    f"[{worker.worker_id}] applied {len(changes.files_changed)} file(s): "
                    + ", ".join(changes.files_changed[:10])
                )
                worker.output_artifacts = changes.files_changed
            else:
                merge_results.append(
                    f"[{worker.worker_id}] FAILED to apply changes — "
                    f"diff preserved in worker result"
                )
                worker.error = (worker.error or "") + " (merge failed)"
                worker.result_summary += f"\n\n--- UNMERGED DIFF ---\n{changes.diff_text[:8000]}"

        save_work_plan(plan)

        merge_summary = _build_merge_summary(plan)
        if merge_results:
            merge_summary += "\n\nMerge details:\n" + "\n".join(merge_results)
        finalize_plan(plan, merge_summary=merge_summary)
        yield _plan_event(plan, "completed", merge_summary=merge_summary)

    elif all_done:
        plan.status = WorkPlanStatus.FAILED.value
        plan.error = "All workers failed"
        plan.finished_at = time.time()
        save_work_plan(plan)
        yield _plan_event(plan, "failed", error=plan.error)

    # Clean up isolated workspaces AFTER merge.
    _cleanup_plan_workspaces(plan)


async def cancel_running_plan(plan: WorkPlan) -> WorkPlan:
    """Cancel all running workers and the plan itself."""
    # Cancel live asyncio tasks for this plan so workers actually stop.
    live_tasks = _running_tasks.pop(plan.plan_id, {})
    for task in live_tasks.values():
        task.cancel()

    return cancel_plan(plan, reason="cancelled by user")


# ── Internal helpers ────────────────────────────────────────────────


def _build_worker_prompt(task: str, worker: WorkerRecord) -> str:
    """Build a scoped prompt for a single worker."""
    role = WorkerRole(worker.role)
    parts = [f"You are a {role.value} worker assigned to a parallel task.\n"]

    parts.append(f"Overall task: {task}\n")
    parts.append(f"Your specific scope: {worker.scope}\n")

    if worker.file_targets:
        parts.append("File targets assigned to you:")
        for ft in worker.file_targets:
            parts.append(f"  - {ft}")
        parts.append("")

    if role == WorkerRole.EXPLORER:
        parts.append(
            "You are READ-ONLY. Gather information, read files, search the codebase. "
            "Do NOT edit any files. Report your findings clearly."
        )
    elif role == WorkerRole.IMPLEMENTER:
        parts.append(
            "You are an implementer. Make the required code changes within your assigned "
            "file targets ONLY. Do not edit files outside your scope. Work in your isolated "
            "workspace. After making changes, briefly describe what you did."
        )
    elif role == WorkerRole.REVIEWER:
        parts.append(
            "You are a reviewer. Read the code, check for issues, validate correctness. "
            "Do NOT edit any files. Produce a clear review with findings and recommendations."
        )

    return "\n".join(parts)


def _cleanup_plan_workspaces(plan: WorkPlan) -> None:
    """Best-effort cleanup for all worker workspaces in a plan."""
    for worker in plan.workers:
        try:
            release_workspace(worker)
        except Exception:
            pass


def _build_merge_summary(plan: WorkPlan) -> str:
    """Build a summary from all completed workers' results."""
    parts = [f"Work plan completed: {plan.task}\n"]

    completed = [w for w in plan.workers if w.state == WorkerState.COMPLETED.value]
    failed = [w for w in plan.workers if w.state == WorkerState.FAILED.value]
    cancelled = [w for w in plan.workers if w.state == WorkerState.CANCELLED.value]

    for w in completed:
        parts.append(f"[{w.role}] {w.scope}:")
        if w.result_summary:
            parts.append(f"  {w.result_summary[:500]}")
        parts.append("")

    if failed:
        parts.append("Failed workers:")
        for w in failed:
            parts.append(f"  [{w.role}] {w.scope}: {w.error or 'unknown error'}")
        parts.append("")

    if cancelled:
        parts.append(f"{len(cancelled)} worker(s) cancelled.")

    return "\n".join(parts)
