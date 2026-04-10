"""FastAPI API layer for Boss Assistant — serves the SwiftUI frontend via SSE streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents import Runner
from agents.exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
)
from agents.items import (
    HandoffOutputItem,
    MessageOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.run_state import RunState
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)
from openai.types.responses import ResponseTextDeltaEvent

from boss import __version__
from boss.agents import build_entry_agent
from boss.config import settings
from boss.control import (
    default_workspace_root,
    jobs_branch_behavior,
    jobs_takeover_cancels_background,
    load_boss_control,
    memory_auto_approve_enabled,
    memory_auto_approve_min_confidence,
    resolve_request_mode,
)
from boss.context.manager import SessionContextManager
from boss.execution import (
    ExecutionType,
    PermissionDecision,
    append_permission_log,
    build_tool_display,
    cleanup_stale_pending_runs,
    delete_permission_rule,
    delete_pending_run,
    get_permission_rule,
    get_tool_call_id,
    list_permission_rules,
    load_expired_pending_run,
    load_pending_run,
    pending_run_metrics,
    pending_approval_from_item,
    record_permission_rule_use,
    save_pending_run,
    store_permission_rule,
)
from boss.jobs import (
    BackgroundJobStatus,
    append_background_job_log,
    create_background_job,
    is_background_job_terminal,
    list_background_jobs,
    load_background_job,
    prepare_task_branch,
    recover_interrupted_background_jobs,
    summarize_background_job,
    tail_background_job_log,
    update_background_job,
)
from boss.memory.injection import build_memory_injection
from boss.memory.knowledge import get_knowledge_store
from boss.review import (
    ReviewRequest as ReviewRunRequestPayload,
    list_review_history,
    load_review_record,
    review_capabilities,
    run_review,
)
from boss.observability import (
    log_agent_change,
    log_memory_distillation,
    log_permission_event,
    log_session_event,
    log_tool_call,
    log_tool_result,
    reset_log_context,
    set_active_agent,
    set_log_context,
)
from boss.memory.scanner import full_scan
from boss.models import build_run_execution_options, resolve_provider_mode, resolve_provider_session_mode, supports_responses_mode
from boss.persistence.history import clear_session_state, extract_display_messages, extract_message_text, load_session_state
from boss.runtime import (
    dependency_availability,
    ensure_api_server_lock,
    mark_api_server_ready,
    release_api_server_lock,
    runtime_status_payload,
)
from boss.utils import ThinkingFilter, is_obviously_dangerous

logger = logging.getLogger("boss.api")

ensure_api_server_lock()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    expired_runs = cleanup_stale_pending_runs()
    recovered_jobs = recover_interrupted_background_jobs()
    mark_api_server_ready()
    logger.info(
        "Boss API started (provider_mode=%s, expired_pending_runs=%s, recovered_background_jobs=%s)",
        resolve_provider_mode(),
        expired_runs,
        recovered_jobs,
    )
    yield
    release_api_server_lock()


app = FastAPI(title="Boss Assistant API", version=__version__, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Boss-Session"],
)

session_context_manager = SessionContextManager()
background_job_tasks: dict[str, asyncio.Task[Any]] = {}


# --- Request / Response models ---

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    mode: str = "agent"
    project_path: str | None = None
    execution_style: str = "single_pass"
    loop_budget: dict | None = None


class FactRequest(BaseModel):
    category: str
    key: str
    value: str


class PermissionDecisionRequest(BaseModel):
    run_id: str
    approval_id: str
    decision: PermissionDecision


class MemoryCandidateUpdateRequest(BaseModel):
    key: str | None = None
    value: str | None = None
    evidence: str | None = None


class MemoryCandidateApproveRequest(BaseModel):
    key: str | None = None
    value: str | None = None
    evidence: str | None = None
    pin: bool = False
    review_note: str | None = None


class MemoryCandidateStatusRequest(BaseModel):
    review_note: str | None = None


class ReviewRunRequest(BaseModel):
    target: str = "auto"
    project_path: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    file_paths: list[str] = Field(default_factory=list)


class BackgroundJobCreateRequest(BaseModel):
    message: str
    session_id: str | None = None
    mode: str = "agent"
    project_path: str | None = None
    branch_mode: str | None = None
    execution_style: str = "single_pass"
    loop_budget: dict | None = None


def _iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


# --- SSE helper ---

def sse_event(data: dict, event: str | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {payload}\n\n"
    return f"data: {payload}\n\n"


def _decode_sse_payload(chunk: str) -> dict[str, Any] | None:
    for line in chunk.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _error_stream_response(message: str, *, session_id: str | None = None) -> StreamingResponse:
    async def _emit_error():
        yield sse_event({"type": "error", "message": message})
        if session_id:
            yield sse_event({"type": "done", "session_id": session_id})
        else:
            yield sse_event({"type": "done"})

    return StreamingResponse(_emit_error(), media_type="text/event-stream")


def _clip_text(text: str, limit: int = 220) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."


def _job_log_message(payload: dict[str, Any]) -> str:
    event_type = payload.get("type")
    if event_type == "session":
        return f"Attached to session {payload.get('session_id', '')}".strip()
    if event_type == "agent":
        return f"Active agent: {payload.get('name', 'unknown')}"
    if event_type == "handoff":
        return f"Handoff {payload.get('from', '?')} -> {payload.get('to', '?')}"
    if event_type == "tool_call":
        title = payload.get("title") or payload.get("name") or "Tool"
        description = payload.get("description") or ""
        return f"{title}: {description}" if description else str(title)
    if event_type == "tool_result":
        return _clip_text(str(payload.get("output", "")) or "Tool completed.", 240)
    if event_type == "permission_request":
        return f"Waiting for permission: {payload.get('title', payload.get('tool', 'tool'))}"
    if event_type == "permission_result":
        return f"Permission decision: {payload.get('decision', 'unknown')}"
    if event_type == "thinking":
        return _clip_text(str(payload.get("content", "")), 240)
    if event_type == "text":
        return _clip_text(str(payload.get("content", "")), 240)
    if event_type == "error":
        return str(payload.get("message", "Unexpected error"))
    if event_type == "done":
        return "Background job completed."
    return _clip_text(json.dumps(payload, ensure_ascii=False), 240)


def _serialize_job_approval(approval: Any) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "tool_name": approval.tool_name,
        "title": approval.title,
        "description": approval.description,
        "execution_type": approval.execution_type,
        "scope_label": approval.scope_label,
        "requested_at": _iso_timestamp(approval.requested_at),
        "expires_at": _iso_timestamp(approval.expires_at),
        "status": approval.status,
    }


def _job_approvals_payload(job: Any) -> list[dict[str, Any]]:
    if not job.pending_run_id:
        return []
    pending = load_pending_run(job.pending_run_id) or load_expired_pending_run(job.pending_run_id)
    if pending is None:
        return []
    return [_serialize_job_approval(approval) for approval in pending.approvals]


def _job_detail_payload(job: Any) -> dict[str, Any]:
    payload = summarize_background_job(job)
    payload["active"] = job.job_id in background_job_tasks
    payload["approvals"] = _job_approvals_payload(job)
    return payload


def _takeover_messages_for_job(job: Any) -> list[dict[str, str]]:
    session = session_context_manager.load_session_read_only(job.session_id)
    messages = extract_display_messages(session)
    if job.session_persisted:
        return messages

    if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != job.prompt:
        messages.append({"role": "user", "content": job.prompt})

    preview = job.assistant_preview.strip()
    if preview and (not messages or messages[-1].get("role") != "assistant" or messages[-1].get("content") != preview):
        messages.append({"role": "assistant", "content": preview})

    return messages


def _build_takeover_payload(job: Any) -> dict[str, Any]:
    return {
        "job": _job_detail_payload(job),
        "session_id": job.session_id,
        "mode": job.mode,
        "project_path": job.project_path,
        "messages": _takeover_messages_for_job(job),
    }


def _clear_background_job_pending_run(job: Any) -> Any:
    if not job.pending_run_id:
        return job
    delete_pending_run(job.pending_run_id)
    return update_background_job(job.job_id, pending_run_id=None)


def _tokenize_query(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9._/-]{3,}", text.lower())
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered[:10]


def _serialize_memory_record(
    *,
    source_table: str,
    memory_id: int,
    memory_kind: str,
    category: str,
    label: str,
    text: str,
    source: str,
    project_path: str | None,
    updated_at: str | None,
    last_used_at: str | None,
    confidence: float,
    salience: float,
    tags: list[str],
    deletable: bool,
    pinned: bool = False,
) -> dict[str, Any]:
    return {
        "source_table": source_table,
        "memory_id": memory_id,
        "memory_kind": memory_kind,
        "category": category,
        "label": label,
        "text": text,
        "source": source,
        "project_path": project_path,
        "updated_at": updated_at,
        "last_used_at": last_used_at,
        "confidence": confidence,
        "salience": salience,
        "tags": tags,
        "deletable": deletable,
        "pinned": pinned,
    }


def _serialize_memory_candidate_record(item: Any, existing_memory: Any | None = None) -> dict[str, Any]:
    return {
        "candidate_id": item.id,
        "status": item.status,
        "memory_kind": item.memory_kind,
        "category": item.category,
        "label": item.key,
        "text": item.value,
        "evidence": item.evidence,
        "source": item.source,
        "project_path": item.project_path,
        "session_id": item.session_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "expires_at": item.expires_at,
        "confidence": item.confidence,
        "salience": item.salience,
        "tags": item.tags,
        "existing_memory_id": item.existing_memory_id,
        "promoted_memory_id": item.promoted_memory_id,
        "proposed_action": "update" if item.existing_memory_id else "create",
        "existing_label": existing_memory.key if existing_memory is not None else None,
        "existing_text": existing_memory.value if existing_memory is not None else None,
    }


def _serialize_injection_reason(result: Any, query: str) -> dict[str, Any]:
    query_lower = query.lower()
    tokens = _tokenize_query(query)
    reasons: list[str] = []
    key_lower = result.key.lower()
    text_lower = result.text.lower()
    kind_lower = result.memory_kind.lower()
    project_path_lower = (result.project_path or "").lower()

    if query_lower and query_lower in key_lower:
        reasons.append("Exact label match")
    elif any(token in key_lower for token in tokens):
        reasons.append("Label keyword match")

    if query_lower and query_lower in text_lower:
        reasons.append("Exact content match")
    elif any(token in text_lower for token in tokens):
        reasons.append("Content keyword match")

    if project_path_lower and any(token in project_path_lower for token in tokens):
        reasons.append("Project-scoped match")

    if kind_lower == "preference":
        reasons.append("Stable preference")
    elif kind_lower == "user_profile":
        reasons.append("User profile fact")
    elif kind_lower == "ongoing_goal":
        reasons.append("Ongoing goal")
    elif kind_lower == "workflow":
        reasons.append("Workflow memory")
    elif kind_lower == "project_constraint":
        reasons.append("Project constraint")
    elif kind_lower == "session_summary":
        reasons.append("Prior session summary")

    if result.last_used_at:
        reasons.append("Recently used")
    if result.review_state == "pending":
        reasons.append("Session-local pending memory")
    if result.pinned:
        reasons.append("Pinned memory")

    summary = ", ".join(dict.fromkeys(reasons[:4])) or "Relevant ranked memory"
    return {
        "source_table": result.source_table,
        "memory_id": result.id,
        "memory_kind": result.memory_kind,
        "category": result.category,
        "key": result.key,
        "text": _clip_text(result.text, 180),
        "project_path": result.project_path,
        "score": round(float(result.score), 2),
        "why": f"{summary} (score {result.score:.2f})",
        "deletable": result.source_table in {"durable_memories", "project_notes", "conversation_episodes", "memory_candidates"},
        "review_state": result.review_state,
        "pinned": result.pinned,
    }


def _latest_user_message_for_session(session_id: str | None) -> str:
    if not session_id:
        return ""
    session = session_context_manager.load_session_read_only(session_id)
    for item in reversed(session.recent_items):
        if isinstance(item, dict) and item.get("role") == "user":
            text = extract_message_text(item)
            if text:
                return text
    return ""


def _memory_overview_payload(*, session_id: str | None = None, message: str | None = None) -> dict[str, Any]:
    store = get_knowledge_store()
    stats = store.stats()

    profile = [
        _serialize_memory_record(
            source_table="durable_memories",
            memory_id=item.id,
            memory_kind=item.memory_kind,
            category=item.category,
            label=item.key,
            text=item.value,
            source=item.source,
            project_path=item.project_path,
            updated_at=item.updated_at,
            last_used_at=item.last_used_at,
            confidence=item.confidence,
            salience=item.salience,
            tags=item.tags,
            deletable=True,
            pinned=item.pinned,
        )
        for item in store.list_durable_memories(memory_kind="user_profile", limit=12)
    ]

    preferences = [
        _serialize_memory_record(
            source_table="durable_memories",
            memory_id=item.id,
            memory_kind=item.memory_kind,
            category=item.category,
            label=item.key,
            text=item.value,
            source=item.source,
            project_path=item.project_path,
            updated_at=item.updated_at,
            last_used_at=item.last_used_at,
            confidence=item.confidence,
            salience=item.salience,
            tags=item.tags,
            deletable=True,
            pinned=item.pinned,
        )
        for item in store.list_durable_memories(memory_kind="preference", limit=12)
    ]

    recent_memories = [
        _serialize_memory_record(
            source_table="durable_memories",
            memory_id=item.id,
            memory_kind=item.memory_kind,
            category=item.category,
            label=item.key,
            text=item.value,
            source=item.source,
            project_path=item.project_path,
            updated_at=item.updated_at,
            last_used_at=item.last_used_at,
            confidence=item.confidence,
            salience=item.salience,
            tags=item.tags,
            deletable=True,
            pinned=item.pinned,
        )
        for item in store.list_durable_memories(limit=16)
    ]

    pending_candidates_raw = store.list_memory_candidates(status="pending", limit=24)
    existing_memories = {
        item.id: item
        for item in store.list_durable_memories(limit=200)
    }
    pending_candidates = [
        _serialize_memory_candidate_record(item, existing_memories.get(item.existing_memory_id))
        for item in pending_candidates_raw
    ]

    conversation_summaries = [
        _serialize_memory_record(
            source_table="conversation_episodes",
            memory_id=item.id,
            memory_kind=item.memory_kind,
            category=item.category,
            label=item.title or item.session_id,
            text=item.summary,
            source=item.source,
            project_path=item.project_path,
            updated_at=item.updated_at,
            last_used_at=item.last_used_at,
            confidence=item.confidence,
            salience=item.salience,
            tags=item.tags,
            deletable=True,
        )
        for item in store.list_conversation_episodes(limit=8)
    ]

    projects_by_path = {project.path: project for project in store.list_projects()}
    project_summaries = []
    for note in store.list_project_summary_notes(limit=24):
        project = projects_by_path.get(note.project_path)
        if project is None:
            continue
        project_summaries.append(
            {
                "source_table": "project_notes",
                "memory_id": note.id,
                "project_id": project.id,
                "project_path": project.path,
                "project_name": project.name,
                "project_type": project.project_type,
                "git_remote": project.git_remote,
                "git_branch": project.git_branch,
                "last_scanned": project.last_scanned,
                "summary_title": note.title,
                "summary_text": note.body,
                "note_key": note.note_key,
                "memory_kind": note.memory_kind,
                "updated_at": note.updated_at,
                "source": note.source,
                "metadata": project.metadata,
                "deletable": True,
            }
        )

    preview_message = (message or "").strip() or _latest_user_message_for_session(session_id)
    current_turn_memory: dict[str, Any] | None = None
    if preview_message:
        injection = (
            session_context_manager.preview_memory_injection(session_id, preview_message)
            if session_id
            else build_memory_injection(user_message=preview_message, read_only=True)
        )
        current_turn_memory = {
            "message": preview_message,
            "query": injection.query,
            "project_path": injection.project_path,
            "text": injection.text,
            "reasons": [_serialize_injection_reason(result, injection.query) for result in injection.results],
        }

    return {
        "user_profile": profile,
        "preferences": preferences,
        "recent_memories": recent_memories,
        "pending_candidates": pending_candidates,
        "governance": {
            "pending_candidates": stats.get("pending_memory_candidates", 0),
            "pinned_memories": stats.get("pinned_durable_memories", 0),
            "auto_approve_enabled": memory_auto_approve_enabled(),
            "auto_approve_min_confidence": memory_auto_approve_min_confidence(),
        },
        "conversation_summaries": conversation_summaries,
        "project_summaries": project_summaries,
        "scan_status": {
            "last_scan_at": stats.get("last_project_scan_at"),
            "projects_indexed": stats.get("projects", 0),
            "files_indexed": stats.get("files_indexed", 0),
            "durable_memories": stats.get("durable_memories", 0),
            "conversation_episodes": stats.get("conversation_episodes", 0),
            "project_notes": stats.get("project_notes", 0),
            "file_chunks": stats.get("file_chunks", 0),
        },
        "current_turn_memory": current_turn_memory,
    }


def _tool_call_payload(item: ToolCallItem) -> dict[str, Any]:
    raw = item.raw_item
    name = getattr(raw, "name", None)
    if not isinstance(name, str) and isinstance(raw, dict):
        name = raw.get("name", "tool")
    name = name or "tool"
    title, description, execution_type, _scope_key, scope_label = build_tool_display(name, raw)
    arguments = getattr(raw, "arguments", None)
    if not isinstance(arguments, str) and isinstance(raw, dict):
        raw_arguments = raw.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments or {})
    return {
        "type": "tool_call",
        "call_id": get_tool_call_id(raw),
        "name": name,
        "title": title,
        "description": description,
        "execution_type": execution_type.value if execution_type else "run",
        "scope_label": scope_label,
        "arguments": arguments or "",
    }


def _tool_result_payload(item: ToolCallOutputItem) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "call_id": get_tool_call_id(item.raw_item),
        "output": str(item.output) if item.output else "",
    }


def _permission_request_payload(run_id: str, approval: Any) -> dict[str, Any]:
    return {
        "type": "permission_request",
        "run_id": run_id,
        "approval_id": approval.approval_id,
        "tool": approval.tool_name,
        "title": approval.title,
        "description": approval.description,
        "execution_type": approval.execution_type,
        "scope_label": approval.scope_label,
    }


def _find_interruption(state: RunState[Any, Any], approval_id: str):
    for item in state.get_interruptions():
        if get_tool_call_id(item.raw_item) == approval_id:
            return item
    return None


def _resolve_stored_permissions(state: RunState[Any, Any]):
    emitted: list[dict[str, Any]] = []
    unresolved = []

    for interruption in state.get_interruptions():
        approval = pending_approval_from_item(interruption)
        rule = get_permission_rule(approval.tool_name, approval.scope_key)
        if rule and rule.decision == PermissionDecision.DENY.value:
            record_permission_rule_use(approval.tool_name, approval.scope_key)
            state.reject(
                interruption,
                always_reject=True,
                rejection_message="Okay — I won't proceed with that.",
            )
            log_permission_event(
                stage="resolved",
                approval_id=approval.approval_id,
                tool_name=approval.tool_name,
                execution_type=approval.execution_type,
                scope_label=approval.scope_label,
                scope_key=approval.scope_key,
                decision=PermissionDecision.DENY.value,
                source="stored_rule",
            )
            append_permission_log(
                tool_name=approval.tool_name,
                execution_type=ExecutionType(approval.execution_type),
                decision=PermissionDecision.DENY,
                approval_time_ms=0,
                scope_key=approval.scope_key,
                source="stored_rule",
            )
            emitted.append(
                {
                    "type": "permission_result",
                    "approval_id": approval.approval_id,
                    "decision": PermissionDecision.DENY.value,
                    "source": "stored_rule",
                }
            )
            continue

        if rule and rule.decision == PermissionDecision.ALWAYS_ALLOW.value:
            record_permission_rule_use(approval.tool_name, approval.scope_key)
            state.approve(interruption, always_approve=True)
            log_permission_event(
                stage="resolved",
                approval_id=approval.approval_id,
                tool_name=approval.tool_name,
                execution_type=approval.execution_type,
                scope_label=approval.scope_label,
                scope_key=approval.scope_key,
                decision=PermissionDecision.ALWAYS_ALLOW.value,
                source="stored_rule",
            )
            emitted.append(
                {
                    "type": "permission_result",
                    "approval_id": approval.approval_id,
                    "decision": PermissionDecision.ALWAYS_ALLOW.value,
                    "source": "stored_rule",
                }
            )
            continue

        unresolved.append(approval)

    return emitted, unresolved


async def _stream_chat_run(
    *,
    run_input: Any,
    session_id: str,
    emit_session: bool,
    pending_run_id: str | None = None,
    initial_event: dict[str, Any] | None = None,
    mode: str | None = None,
    workspace_root: str | None = None,
    loop_id: str | None = None,
):
    from boss.runner.engine import get_runner
    get_runner(mode=mode, workspace_root=workspace_root)

    agent = build_entry_agent(mode=mode)
    current_input = run_input
    sent_session = False
    emitted_initial = False
    current_agent_name = agent.name
    context_tokens = set_log_context(
        session_id=session_id,
        agent_name=current_agent_name,
        run_id=pending_run_id,
    )
    execution_options = build_run_execution_options(
        session_id=session_id,
        workflow_name="Boss API Chat",
        trace_metadata={"surface": "api", "session_id": session_id, "mode": mode or "default"},
    )

    try:
        while True:
            think_filter = ThinkingFilter()
            result = Runner.run_streamed(
                agent,
                input=current_input,
                run_config=execution_options.run_config,
                session=execution_options.session,
            )

            if emit_session and not sent_session:
                yield sse_event({"type": "session", "session_id": session_id})
                sent_session = True

            if initial_event and not emitted_initial:
                yield sse_event(initial_event)
                emitted_initial = True

            async for event in result.stream_events():
                if isinstance(event, AgentUpdatedStreamEvent):
                    log_agent_change(from_agent=current_agent_name, to_agent=event.new_agent.name)
                    current_agent_name = event.new_agent.name
                    set_active_agent(current_agent_name)
                    agent_model = getattr(event.new_agent, "model", None)
                    agent_event: dict[str, object] = {"type": "agent", "name": event.new_agent.name}
                    if agent_model:
                        agent_event["model"] = str(agent_model)
                    yield sse_event(agent_event)

                elif isinstance(event, RunItemStreamEvent):
                    item = event.item

                    if isinstance(item, HandoffOutputItem):
                        source = item.source_agent.name if item.source_agent else "unknown"
                        target = item.target_agent.name if item.target_agent else "unknown"
                        yield sse_event({"type": "handoff", "from": source, "to": target})

                    elif isinstance(item, ToolCallItem):
                        payload = _tool_call_payload(item)
                        log_tool_call(
                            call_id=payload["call_id"],
                            tool_name=payload["name"],
                            title=payload["title"],
                            execution_type=payload["execution_type"],
                            scope_label=payload["scope_label"],
                            arguments_length=len(payload["arguments"]),
                        )
                        yield sse_event(payload)

                    elif isinstance(item, ToolCallOutputItem):
                        payload = _tool_result_payload(item)
                        log_tool_result(
                            call_id=payload["call_id"],
                            output_length=len(payload["output"]),
                        )
                        yield sse_event(payload)

                    elif isinstance(item, MessageOutputItem):
                        pass

                elif isinstance(event, RawResponsesStreamEvent):
                    data = event.data
                    if isinstance(data, ResponseTextDeltaEvent):
                        visible = think_filter.feed(data.delta)
                        if visible:
                            yield sse_event({"type": "text", "content": visible})

            remaining = think_filter.flush()
            if remaining:
                yield sse_event({"type": "text", "content": remaining})

            if think_filter.thinking_text:
                yield sse_event({"type": "thinking", "content": think_filter.thinking_text})

            if result.interruptions:
                state = result.to_state()
                permission_results, unresolved = _resolve_stored_permissions(state)
                for payload in permission_results:
                    yield sse_event(payload)

                if unresolved:
                    pending_run_id = save_pending_run(
                        session_id=session_id,
                        state=state.to_json(),
                        approvals=unresolved,
                        run_id=pending_run_id,
                        mode=mode,
                        project_path=workspace_root,
                        loop_id=loop_id,
                    )
                    for approval in unresolved:
                        log_permission_event(
                            stage="interrupted",
                            approval_id=approval.approval_id,
                            tool_name=approval.tool_name,
                            execution_type=approval.execution_type,
                            scope_label=approval.scope_label,
                            scope_key=approval.scope_key,
                            source="needs_approval",
                            run_id=pending_run_id,
                        )
                        yield sse_event(_permission_request_payload(pending_run_id, approval))
                    return

                current_input = state
                emit_session = False
                initial_event = None
                continue

            if pending_run_id:
                delete_pending_run(pending_run_id)

            session_context_manager.persist_result(session_id, result.to_input_list())
            yield sse_event({"type": "done", "session_id": session_id})
            return

    except InputGuardrailTripwireTriggered:
        yield sse_event({"type": "error", "message": "Request blocked by safety guardrail."})
        yield sse_event({"type": "done", "session_id": session_id})

    except MaxTurnsExceeded:
        yield sse_event({"type": "error", "message": "Agent exceeded maximum turns."})
        yield sse_event({"type": "done", "session_id": session_id})

    except AgentsException as exc:
        yield sse_event({"type": "error", "message": str(exc)})
        yield sse_event({"type": "done", "session_id": session_id})

    except Exception as exc:
        logger.exception("Unexpected error in chat stream")
        yield sse_event({"type": "error", "message": str(exc)})
        yield sse_event({"type": "done", "session_id": session_id})
    finally:
        reset_log_context(context_tokens)


async def _cancel_background_job(job_id: str, *, final_status: str, reason: str) -> Any:
    job = load_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found")

    job = update_background_job(
        job_id,
        status=final_status,
        latest_event=reason,
        cancellation_requested_at=datetime.now(timezone.utc).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat() if job.status == BackgroundJobStatus.WAITING_PERMISSION.value else job.finished_at,
    )
    append_background_job_log(job_id, event_type=final_status, message=reason)

    if job.pending_run_id and final_status in {
        BackgroundJobStatus.CANCELLED.value,
        BackgroundJobStatus.TAKEN_OVER.value,
    }:
        job = _clear_background_job_pending_run(job)

    task = background_job_tasks.get(job_id)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        job = load_background_job(job_id) or job

    return job


async def _run_background_job(
    *,
    job_id: str,
    run_input: Any,
    session_id: str,
    mode: str,
    emit_session: bool,
    pending_run_id: str | None = None,
    is_resume: bool = False,
    workspace_root: str | None = None,
) -> None:
    job = load_background_job(job_id)
    if job is None:
        return

    started_at = job.started_at or datetime.now(timezone.utc).isoformat()
    job = update_background_job(
        job_id,
        status=BackgroundJobStatus.RUNNING.value,
        started_at=started_at,
        finished_at=None,
        error_message=None,
        pending_run_id=pending_run_id,
        latest_event="Background job is running.",
        resume_count=job.resume_count + (1 if is_resume else 0),
    )
    append_background_job_log(job_id, event_type="job_started", message="Background job execution started.")

    assistant_preview = job.assistant_preview
    last_saved_preview = assistant_preview
    encountered_error: str | None = None
    waiting_for_permission = False
    completed = False

    try:
        async for chunk in _stream_chat_run(
            run_input=run_input,
            session_id=session_id,
            emit_session=emit_session,
            pending_run_id=pending_run_id,
            mode=mode,
            workspace_root=workspace_root,
        ):
            payload = _decode_sse_payload(chunk)
            if payload is None:
                continue

            event_type = str(payload.get("type", "event"))
            append_background_job_log(
                job_id,
                event_type=event_type,
                message=_job_log_message(payload),
                payload=payload,
            )

            now_iso = datetime.now(timezone.utc).isoformat()
            if event_type == "session" and payload.get("session_id"):
                session_id = str(payload.get("session_id"))
                update_background_job(job_id, session_id=session_id, last_event_at=now_iso)
                continue

            if event_type == "text":
                assistant_preview = _clip_text(assistant_preview + str(payload.get("content", "")), 12_000)
                should_flush = abs(len(assistant_preview) - len(last_saved_preview)) >= 240 or assistant_preview.endswith(("\n", ".", "!", "?"))
                if should_flush:
                    update_background_job(
                        job_id,
                        assistant_preview=assistant_preview,
                        last_event_at=now_iso,
                        latest_event="Streaming assistant output.",
                    )
                    last_saved_preview = assistant_preview
                continue

            if event_type == "permission_request":
                waiting_for_permission = True
                update_background_job(
                    job_id,
                    status=BackgroundJobStatus.WAITING_PERMISSION.value,
                    pending_run_id=str(payload.get("run_id") or pending_run_id or ""),
                    assistant_preview=assistant_preview,
                    last_event_at=now_iso,
                    latest_event=_job_log_message(payload),
                )
                return

            if event_type == "error":
                encountered_error = str(payload.get("message", "Unexpected error"))
                update_background_job(
                    job_id,
                    assistant_preview=assistant_preview,
                    last_event_at=now_iso,
                    latest_event=encountered_error,
                    error_message=encountered_error,
                )
                continue

            if event_type == "done":
                completed = True
                continue

            update_background_job(
                job_id,
                assistant_preview=assistant_preview,
                last_event_at=now_iso,
                latest_event=_job_log_message(payload),
            )

        if waiting_for_permission:
            return

        finished_at = datetime.now(timezone.utc).isoformat()
        if encountered_error:
            update_background_job(
                job_id,
                status=BackgroundJobStatus.FAILED.value,
                finished_at=finished_at,
                assistant_preview=assistant_preview,
                latest_event=encountered_error,
                error_message=encountered_error,
            )
            return

        if completed:
            update_background_job(
                job_id,
                status=BackgroundJobStatus.COMPLETED.value,
                finished_at=finished_at,
                pending_run_id=None,
                assistant_preview=assistant_preview,
                latest_event="Background job completed.",
                session_persisted=True,
            )
            return

        update_background_job(
            job_id,
            status=BackgroundJobStatus.INTERRUPTED.value,
            finished_at=finished_at,
            assistant_preview=assistant_preview,
            latest_event="Background job stopped unexpectedly.",
            error_message="Background job stopped without a terminal event.",
        )
    except asyncio.CancelledError:
        current = load_background_job(job_id)
        if current is not None:
            final_status = current.status if current.status in {BackgroundJobStatus.CANCELLED.value, BackgroundJobStatus.TAKEN_OVER.value} else BackgroundJobStatus.CANCELLED.value
            latest_event = "Taken over into the foreground chat." if final_status == BackgroundJobStatus.TAKEN_OVER.value else "Background job cancelled."
            update_background_job(
                job_id,
                status=final_status,
                finished_at=datetime.now(timezone.utc).isoformat(),
                assistant_preview=current.assistant_preview,
                latest_event=latest_event,
                pending_run_id=None if final_status in {
                    BackgroundJobStatus.CANCELLED.value,
                    BackgroundJobStatus.TAKEN_OVER.value,
                } else current.pending_run_id,
            )
            append_background_job_log(job_id, event_type=final_status, message=latest_event)
        raise
    except Exception as exc:
        logger.exception("Unexpected error in background job %s", job_id)
        update_background_job(
            job_id,
            status=BackgroundJobStatus.FAILED.value,
            finished_at=datetime.now(timezone.utc).isoformat(),
            latest_event=str(exc),
            error_message=str(exc),
        )
        append_background_job_log(job_id, event_type="error", message=str(exc))
    finally:
        background_job_tasks.pop(job_id, None)


async def _run_background_loop_job(
    *,
    job_id: str,
    message: str,
    session_id: str,
    mode: str,
    workspace_root: str | None = None,
    loop_budget: dict | None = None,
    resume_state: "LoopState | None" = None,
    pending_run_input=None,
    pending_run_id: str | None = None,
) -> None:
    """Run a background job using the iterative loop engine."""
    from boss.loop.engine import LoopEngine
    from boss.loop.policy import LoopBudget
    from boss.loop.state import LoopState

    job = load_background_job(job_id)
    if job is None:
        return

    budget = LoopBudget.from_dict(loop_budget) if loop_budget else LoopBudget()
    engine = LoopEngine(
        task=message,
        session_id=session_id,
        budget=budget,
        mode=mode,
        workspace_root=workspace_root,
        job_id=job_id,
        resume_state=resume_state,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    update_background_job(
        job_id,
        status=BackgroundJobStatus.RUNNING.value,
        started_at=started_at,
        finished_at=None,
        error_message=None,
        pending_run_id=None,
        latest_event="Iterative loop started.",
        loop_id=engine.state.loop_id,
    )
    append_background_job_log(
        job_id,
        event_type="loop_started",
        message="Iterative loop execution started.",
        payload={"budget": budget.to_dict(), "loop_id": engine.state.loop_id},
    )

    assistant_preview = ""
    encountered_error: str | None = None

    try:
        # If resuming from a pending approval, replay the interrupted pass
        # and evaluate its result before entering the engine loop.
        if pending_run_input is not None and resume_state is not None:
            from boss.loop.engine import _parse_loop_result
            from boss.loop.state import save_loop_state as _save_ls

            interrupted_text = ""
            re_paused = False
            async for chunk in _stream_chat_run(
                run_input=pending_run_input,
                session_id=session_id,
                emit_session=False,
                pending_run_id=pending_run_id,
                mode=mode,
                workspace_root=workspace_root,
                loop_id=resume_state.loop_id,
            ):
                payload = _decode_sse_payload(chunk)
                if payload:
                    if payload.get("type") == "text":
                        content = str(payload.get("content", ""))
                        interrupted_text += content
                        assistant_preview = (assistant_preview + content)[-12000:]
                    if payload.get("type") == "permission_request":
                        # The resumed pass needs another approval — re-pause.
                        new_run_id = str(payload.get("run_id", ""))
                        resume_state.pending_run_id = new_run_id
                        resume_state.phase = "edit"
                        resume_state.stop_reason = "approval_blocked"
                        _save_ls(resume_state)
                        now_iso = datetime.now(timezone.utc).isoformat()
                        update_background_job(
                            job_id,
                            status=BackgroundJobStatus.WAITING_PERMISSION.value,
                            pending_run_id=new_run_id,
                            assistant_preview=assistant_preview,
                            last_event_at=now_iso,
                            latest_event=_job_log_message(payload),
                        )
                        append_background_job_log(
                            job_id,
                            event_type="permission_request",
                            message=_job_log_message(payload),
                            payload=payload,
                        )
                        re_paused = True
                        break

            if re_paused:
                return

            resumed_result = _parse_loop_result(interrupted_text)
            now_iso = datetime.now(timezone.utc).isoformat()

            # Update the interrupted attempt record
            if resume_state.attempts:
                last_att = resume_state.attempts[-1]
                last_att.finished_at = time.time()
                last_att.assistant_output = interrupted_text[-4000:]
                if resumed_result == "success":
                    last_att.test_passed = True

            if resumed_result == "success":
                resume_state.stop_reason = "success"
                resume_state.finished_at = time.time()
                resume_state.phase = "done"
                _save_ls(resume_state)
                update_background_job(
                    job_id,
                    status=BackgroundJobStatus.COMPLETED.value,
                    finished_at=now_iso,
                    pending_run_id=None,
                    assistant_preview=assistant_preview,
                    latest_event="Loop completed: success (resumed pass)",
                )
                return

            if resumed_result == "stop":
                resume_state.stop_reason = "agent_stopped"
                resume_state.finished_at = time.time()
                resume_state.phase = "done"
                _save_ls(resume_state)
                update_background_job(
                    job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    finished_at=now_iso,
                    pending_run_id=None,
                    assistant_preview=assistant_preview,
                    latest_event="Loop stopped: agent_stopped (resumed pass)",
                    error_message="agent_stopped",
                )
                return

            # Retry or no directive — clear pause state and continue loop
            resume_state.stop_reason = None
            resume_state.pending_run_id = None
            _save_ls(resume_state)

        async for chunk in engine.run():
            payload = _decode_sse_payload(chunk)
            if payload is None:
                continue

            event_type = str(payload.get("type", "event"))
            now_iso = datetime.now(timezone.utc).isoformat()

            append_background_job_log(
                job_id,
                event_type=event_type,
                message=_job_log_message(payload),
                payload=payload,
            )

            if event_type == "text":
                content = str(payload.get("content", ""))
                assistant_preview = (assistant_preview + content)[-12000:]
                if len(content) > 100 or content.endswith(("\n", ".", "!", "?")):
                    update_background_job(
                        job_id,
                        assistant_preview=assistant_preview,
                        last_event_at=now_iso,
                        latest_event="Loop streaming output.",
                    )

            elif event_type == "loop_status":
                status_val = payload.get("status", "")
                stop_reason = payload.get("stop_reason", "")
                latest = f"Loop {status_val}"
                if stop_reason:
                    latest += f": {stop_reason}"

                if status_val in ("completed", "stopped"):
                    final_job_status = (
                        BackgroundJobStatus.COMPLETED.value
                        if stop_reason == "success"
                        else BackgroundJobStatus.FAILED.value
                    )
                    update_background_job(
                        job_id,
                        status=final_job_status,
                        finished_at=now_iso,
                        pending_run_id=None,
                        assistant_preview=assistant_preview,
                        latest_event=latest,
                        error_message=stop_reason if final_job_status == BackgroundJobStatus.FAILED.value else None,
                    )
                    return

                elif status_val == "paused":
                    update_background_job(
                        job_id,
                        status=BackgroundJobStatus.WAITING_PERMISSION.value,
                        pending_run_id=engine.state.pending_run_id,
                        assistant_preview=assistant_preview,
                        last_event_at=now_iso,
                        latest_event=latest,
                    )
                    return

                else:
                    update_background_job(
                        job_id,
                        last_event_at=now_iso,
                        latest_event=latest,
                    )

            elif event_type == "loop_attempt":
                attempt_num = payload.get("attempt_number", "?")
                update_background_job(
                    job_id,
                    last_event_at=now_iso,
                    latest_event=f"Loop attempt {attempt_num}",
                )

            elif event_type == "error":
                encountered_error = str(payload.get("message", "Unexpected error"))

            elif event_type == "permission_request":
                update_background_job(
                    job_id,
                    status=BackgroundJobStatus.WAITING_PERMISSION.value,
                    pending_run_id=str(payload.get("run_id", "")),
                    assistant_preview=assistant_preview,
                    last_event_at=now_iso,
                    latest_event=_job_log_message(payload),
                )
                return

        # If we exit the loop without a terminal event
        if encountered_error:
            update_background_job(
                job_id,
                status=BackgroundJobStatus.FAILED.value,
                finished_at=datetime.now(timezone.utc).isoformat(),
                pending_run_id=None,
                assistant_preview=assistant_preview,
                latest_event=encountered_error,
                error_message=encountered_error,
            )
        else:
            update_background_job(
                job_id,
                status=BackgroundJobStatus.COMPLETED.value,
                finished_at=datetime.now(timezone.utc).isoformat(),
                pending_run_id=None,
                assistant_preview=assistant_preview,
                latest_event="Iterative loop finished.",
            )

    except asyncio.CancelledError:
        update_background_job(
            job_id,
            status=BackgroundJobStatus.CANCELLED.value,
            finished_at=datetime.now(timezone.utc).isoformat(),
            pending_run_id=None,
            latest_event="Iterative loop cancelled.",
        )
    except Exception as exc:
        logger.exception("Loop job %s failed", job_id)
        update_background_job(
            job_id,
            status=BackgroundJobStatus.FAILED.value,
            finished_at=datetime.now(timezone.utc).isoformat(),
            pending_run_id=None,
            latest_event=str(exc),
            error_message=str(exc),
        )
    finally:
        background_job_tasks.pop(job_id, None)


# --- Chat endpoint with SSE streaming ---

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    resolved_mode = resolve_request_mode(req.message, explicit_mode=req.mode)

    danger = is_obviously_dangerous(req.message)
    if danger:
        async def blocked():
            yield sse_event({"type": "error", "message": danger})
            yield sse_event({"type": "done", "session_id": session_id})
        return StreamingResponse(blocked(), media_type="text/event-stream")

    session = session_context_manager.load_session_read_only(session_id) if req.session_id else load_session_state(session_id)
    log_session_event(
        session_id=session_id,
        message_length=len(req.message),
        history_items=len(session.recent_items),
        resumed_session=bool(req.session_id),
    )

    # Iterative loop flow
    if req.execution_style == "iterative":
        from boss.loop.engine import LoopEngine
        from boss.loop.policy import LoopBudget

        budget = LoopBudget.from_dict(req.loop_budget) if req.loop_budget else LoopBudget()
        engine = LoopEngine(
            task=req.message,
            session_id=session_id,
            budget=budget,
            mode=resolved_mode,
            workspace_root=req.project_path,
        )

        async def loop_stream():
            yield sse_event({"type": "session", "session_id": session_id})
            async for chunk in engine.run():
                yield chunk
            yield sse_event({"type": "done", "session_id": session_id})

        return StreamingResponse(loop_stream(), media_type="text/event-stream")

    prepared_input = session_context_manager.prepare_input(session_id, req.message).model_input

    return StreamingResponse(
        _stream_chat_run(
            run_input=prepared_input,
            session_id=session_id,
            emit_session=True,
            mode=resolved_mode,
            workspace_root=req.project_path,
        ),
        media_type="text/event-stream",
    )


@app.post("/api/chat/permissions")
async def permission_decision_endpoint(req: PermissionDecisionRequest):
    record = load_pending_run(req.run_id)
    if record is None:
        expired = load_expired_pending_run(req.run_id)
        if expired is not None:
            return _error_stream_response(
                "This approval expired. Please retry the action so Boss can request fresh approval.",
                session_id=expired.session_id,
            )
        return _error_stream_response("Pending run not found.")

    agent = build_entry_agent(mode=record.mode)
    state = await RunState.from_json(agent, record.state)
    interruption = _find_interruption(state, req.approval_id)
    if interruption is None:
        return _error_stream_response(
            "Approval item not found. Please retry the action so Boss can request a fresh approval.",
            session_id=record.session_id,
        )

    approval = next((item for item in record.approvals if item.approval_id == req.approval_id), None)
    if approval is None:
        approval = pending_approval_from_item(interruption)

    approval_time_ms = int(max(0.0, time.time() - approval.requested_at) * 1000)

    if req.decision == PermissionDecision.ALLOW_ONCE:
        state.approve(interruption)
    elif req.decision == PermissionDecision.ALWAYS_ALLOW:
        state.approve(interruption, always_approve=True)
        store_permission_rule(
            tool_name=approval.tool_name,
            scope_key=approval.scope_key,
            scope_label=approval.scope_label,
            execution_type=ExecutionType(approval.execution_type),
            decision=req.decision,
        )
    else:
        state.reject(
            interruption,
            always_reject=True,
            rejection_message="Okay — I won't proceed with that.",
        )
        store_permission_rule(
            tool_name=approval.tool_name,
            scope_key=approval.scope_key,
            scope_label=approval.scope_label,
            execution_type=ExecutionType(approval.execution_type),
            decision=req.decision,
        )

    append_permission_log(
        tool_name=approval.tool_name,
        execution_type=ExecutionType(approval.execution_type),
        decision=req.decision,
        approval_time_ms=approval_time_ms,
        scope_key=approval.scope_key,
        source="user",
    )
    log_permission_event(
        stage="resolved",
        approval_id=approval.approval_id,
        tool_name=approval.tool_name,
        execution_type=approval.execution_type,
        scope_label=approval.scope_label,
        scope_key=approval.scope_key,
        decision=req.decision.value,
        source="user",
        run_id=req.run_id,
        approval_time_ms=approval_time_ms,
    )

    if record.loop_id:
        # Resume via the loop engine so remaining iterations continue.
        from boss.loop.engine import LoopEngine
        from boss.loop.policy import LoopBudget
        from boss.loop.state import load_loop_state

        loop_state = load_loop_state(record.loop_id)

        async def _resume_loop_after_permission():
            permission_event = {
                "type": "permission_result",
                "approval_id": approval.approval_id,
                "decision": req.decision.value,
                "source": "user",
            }
            yield sse_event(permission_event)

            if loop_state is None:
                yield sse_event({"type": "error", "message": "Loop state not found for resume."})
                yield sse_event({"type": "done", "session_id": record.session_id})
                return

            budget = LoopBudget.from_dict(loop_state.budget)
            engine = LoopEngine(
                task=loop_state.task_description,
                session_id=record.session_id,
                budget=budget,
                mode=record.mode or "agent",
                workspace_root=record.project_path,
                job_id=loop_state.job_id,
                resume_state=loop_state,
            )
            # First, finish the interrupted agent pass with the approved state
            from boss.loop.engine import _parse_loop_result
            from boss.loop.state import save_loop_state as _save_ls

            interrupted_text = ""
            re_paused = False
            async for chunk in _stream_chat_run(
                run_input=state,
                session_id=record.session_id,
                emit_session=False,
                pending_run_id=req.run_id,
                mode=record.mode,
                workspace_root=record.project_path,
                loop_id=record.loop_id,
            ):
                payload = _decode_sse_payload(chunk)
                if payload:
                    if payload.get("type") == "done":
                        continue  # suppress inner done
                    if payload.get("type") == "text":
                        interrupted_text += payload.get("content", "")
                    if payload.get("type") == "permission_request":
                        # The resumed pass needs another approval — save
                        # the new pending state and stop.
                        new_run_id = payload.get("run_id")
                        if loop_state:
                            loop_state.pending_run_id = new_run_id
                            loop_state.phase = "edit"
                            loop_state.stop_reason = "approval_blocked"
                            _save_ls(loop_state)
                        yield chunk
                        yield sse_event({
                            "type": "loop_status",
                            "loop_id": record.loop_id,
                            "status": "paused",
                            "stop_reason": "approval_blocked",
                            "attempt": loop_state.current_attempt if loop_state else 1,
                        })
                        yield sse_event({"type": "done", "session_id": record.session_id})
                        re_paused = True
                        return
                yield chunk

            if re_paused:
                return

            # Evaluate the interrupted pass before deciding whether to
            # continue the loop or declare completion.
            resumed_result = _parse_loop_result(interrupted_text)

            # Update the interrupted attempt record in loop state
            if loop_state and loop_state.attempts:
                last_att = loop_state.attempts[-1]
                last_att.finished_at = time.time()
                last_att.assistant_output = interrupted_text[-4000:]
                if resumed_result == "success":
                    last_att.test_passed = True

            if resumed_result == "success":
                if loop_state:
                    loop_state.stop_reason = "success"
                    loop_state.finished_at = time.time()
                    loop_state.phase = "done"
                    _save_ls(loop_state)
                yield sse_event({
                    "type": "loop_status",
                    "loop_id": record.loop_id,
                    "status": "completed",
                    "stop_reason": "success",
                    "attempt": loop_state.current_attempt if loop_state else 1,
                })
                yield sse_event({"type": "done", "session_id": record.session_id})
                return

            if resumed_result == "stop":
                if loop_state:
                    loop_state.stop_reason = "agent_stopped"
                    loop_state.finished_at = time.time()
                    loop_state.phase = "done"
                    _save_ls(loop_state)
                yield sse_event({
                    "type": "loop_status",
                    "loop_id": record.loop_id,
                    "status": "stopped",
                    "stop_reason": "agent_stopped",
                    "attempt": loop_state.current_attempt if loop_state else 1,
                })
                yield sse_event({"type": "done", "session_id": record.session_id})
                return

            # Result was retry or no directive — save state and continue loop
            if loop_state:
                loop_state.stop_reason = None
                loop_state.pending_run_id = None
                _save_ls(loop_state)

            async for chunk in engine.run():
                yield chunk

            yield sse_event({"type": "done", "session_id": record.session_id})

        return StreamingResponse(
            _resume_loop_after_permission(),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        _stream_chat_run(
            run_input=state,
            session_id=record.session_id,
            emit_session=False,
            pending_run_id=req.run_id,
            mode=record.mode,
            workspace_root=record.project_path,
            loop_id=record.loop_id,
            initial_event={
                "type": "permission_result",
                "approval_id": approval.approval_id,
                "decision": req.decision.value,
                "source": "user",
            },
        ),
        media_type="text/event-stream",
    )


@app.get("/api/permissions")
async def list_permissions():
    return [
        {
            "tool": rule.tool_name,
            "scope_key": rule.scope_key,
            "scope_label": rule.scope_label,
            "decision": "allow" if rule.decision == PermissionDecision.ALWAYS_ALLOW.value else "deny",
            "execution_type": rule.execution_type,
            "last_used_at": _iso_timestamp(rule.last_used_at),
            "updated_at": _iso_timestamp(rule.updated_at),
        }
        for rule in list_permission_rules()
    ]


@app.delete("/api/permissions")
async def revoke_permission(tool: str, scope_key: str):
    if not delete_permission_rule(tool, scope_key):
        raise HTTPException(status_code=404, detail="Permission rule not found")
    return {"status": "deleted"}


# --- Session management ---

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = session_context_manager.load_session_read_only(session_id)
    return {"session_id": session_id, "messages": extract_display_messages(session)}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    clear_session_state(session_id)
    get_knowledge_store().delete_conversation_episode(session_id)
    return {"status": "cleared"}


# --- Memory endpoints ---

@app.get("/api/memory/facts")
async def get_facts(category: str | None = None):
    store = get_knowledge_store()
    facts = store.get_facts(category)
    return [{"id": f.id, "category": f.category, "key": f.key, "value": f.value, "source": f.source} for f in facts]


@app.post("/api/memory/facts")
async def add_fact(req: FactRequest):
    store = get_knowledge_store()
    fact = store.store_fact(req.category, req.key, req.value, source="user")
    log_memory_distillation(
        source="memory_api",
        category=req.category,
        key=req.key,
        value_length=len(req.value),
    )
    return {"id": fact.id, "category": fact.category, "key": fact.key, "value": fact.value}


@app.delete("/api/memory/facts/{fact_id}")
async def delete_fact(fact_id: int):
    store = get_knowledge_store()
    if not store.delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"status": "deleted"}


@app.get("/api/memory/projects")
async def get_projects(project_type: str | None = None):
    store = get_knowledge_store()
    projects = store.list_projects(project_type)
    return [
        {
            "id": p.id,
            "path": p.path,
            "name": p.name,
            "type": p.project_type,
            "git_remote": p.git_remote,
            "git_branch": p.git_branch,
            "metadata": p.metadata,
        }
        for p in projects
    ]


@app.get("/api/memory/stats")
async def memory_stats():
    store = get_knowledge_store()
    return store.stats()


@app.get("/api/memory/overview")
async def memory_overview(session_id: str | None = None, message: str | None = None):
    return _memory_overview_payload(session_id=session_id, message=message)


@app.get("/api/memory/candidates")
async def list_memory_candidates(status: str | None = "pending"):
    store = get_knowledge_store()
    items = store.list_memory_candidates(status=status, limit=100)
    existing_memories = {
        item.id: item
        for item in store.list_durable_memories(limit=200)
    }
    return [
        _serialize_memory_candidate_record(item, existing_memories.get(item.existing_memory_id))
        for item in items
    ]


@app.patch("/api/memory/candidates/{candidate_id}")
async def update_memory_candidate(candidate_id: int, req: MemoryCandidateUpdateRequest):
    store = get_knowledge_store()
    candidate = store.update_memory_candidate(
        candidate_id,
        key=req.key,
        value=req.value,
        evidence=req.evidence,
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="Pending memory candidate not found")
    existing_memory = store.get_durable_memory(candidate.existing_memory_id) if candidate.existing_memory_id else None
    return _serialize_memory_candidate_record(candidate, existing_memory)


@app.post("/api/memory/candidates/{candidate_id}/approve")
async def approve_memory_candidate(candidate_id: int, req: MemoryCandidateApproveRequest):
    store = get_knowledge_store()
    try:
        memory = store.approve_memory_candidate(
            candidate_id,
            key=req.key,
            value=req.value,
            evidence=req.evidence,
            pin=req.pin,
            review_note=req.review_note,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return _serialize_memory_record(
        source_table="durable_memories",
        memory_id=memory.id,
        memory_kind=memory.memory_kind,
        category=memory.category,
        label=memory.key,
        text=memory.value,
        source=memory.source,
        project_path=memory.project_path,
        updated_at=memory.updated_at,
        last_used_at=memory.last_used_at,
        confidence=memory.confidence,
        salience=memory.salience,
        tags=memory.tags,
        deletable=True,
        pinned=memory.pinned,
    )


@app.post("/api/memory/candidates/{candidate_id}/reject")
async def reject_memory_candidate(candidate_id: int, req: MemoryCandidateStatusRequest):
    store = get_knowledge_store()
    candidate = store.reject_memory_candidate(candidate_id, review_note=req.review_note)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Pending memory candidate not found")
    existing_memory = store.get_durable_memory(candidate.existing_memory_id) if candidate.existing_memory_id else None
    return _serialize_memory_candidate_record(candidate, existing_memory)


@app.post("/api/memory/candidates/{candidate_id}/expire")
async def expire_memory_candidate(candidate_id: int, req: MemoryCandidateStatusRequest):
    store = get_knowledge_store()
    candidate = store.expire_memory_candidate(candidate_id, review_note=req.review_note)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Pending memory candidate not found")
    existing_memory = store.get_durable_memory(candidate.existing_memory_id) if candidate.existing_memory_id else None
    return _serialize_memory_candidate_record(candidate, existing_memory)


@app.post("/api/memory/items/durable_memories/{item_id}/pin")
async def pin_memory_item(item_id: int):
    store = get_knowledge_store()
    memory = store.set_durable_memory_pinned(item_id, pinned=True)
    if memory is None:
        raise HTTPException(status_code=404, detail="Durable memory not found")
    return {"status": "pinned", "memory_id": memory.id}


@app.post("/api/memory/items/durable_memories/{item_id}/unpin")
async def unpin_memory_item(item_id: int):
    store = get_knowledge_store()
    memory = store.set_durable_memory_pinned(item_id, pinned=False)
    if memory is None:
        raise HTTPException(status_code=404, detail="Durable memory not found")
    return {"status": "unpinned", "memory_id": memory.id}


# --- Review endpoints ---

@app.get("/api/review/capabilities")
async def get_review_capabilities(project_path: str | None = None):
    return review_capabilities(project_path)


@app.get("/api/review/history")
async def get_review_history(limit: int = 30):
    safe_limit = max(1, min(limit, 100))
    return [record.model_dump(mode="json") for record in list_review_history(limit=safe_limit)]


@app.get("/api/review/history/{review_id}")
async def get_review_record(review_id: str):
    record = load_review_record(review_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Review record not found")
    return record.model_dump(mode="json")


@app.post("/api/review/run")
async def run_review_endpoint(req: ReviewRunRequest):
    try:
        record = await run_review(
            ReviewRunRequestPayload(
                target=req.target,
                project_path=req.project_path,
                base_ref=req.base_ref,
                head_ref=req.head_ref,
                file_paths=tuple(req.file_paths),
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return record.model_dump(mode="json")


# --- Background job endpoints ---

@app.get("/api/jobs")
async def list_jobs(limit: int = 50):
    safe_limit = max(1, min(limit, 200))
    return [_job_detail_payload(job) for job in list_background_jobs(limit=safe_limit)]


@app.post("/api/jobs")
async def create_job_endpoint(req: BackgroundJobCreateRequest):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Background jobs require a prompt.")

    danger = is_obviously_dangerous(message)
    if danger:
        raise HTTPException(status_code=400, detail=danger)

    session_id = req.session_id or str(uuid.uuid4())
    resolved_mode = resolve_request_mode(message, explicit_mode=req.mode)
    project_path = req.project_path or str(load_boss_control().root)
    prepared_input = session_context_manager.prepare_input(session_id, message).model_input

    branch_mode = (req.branch_mode or jobs_branch_behavior(project_path)).strip().lower()
    branch_info = prepare_task_branch(prompt=message, project_path=project_path, branch_mode=branch_mode)

    job = create_background_job(
        prompt=message,
        mode=resolved_mode,
        session_id=session_id,
        project_path=project_path,
        initial_input_kind="prepared_input",
        initial_input_payload=prepared_input,
        branch_mode=branch_info.get("branch_mode"),
        branch_name=branch_info.get("branch_name"),
        task_slug=branch_info.get("task_slug"),
        branch_status=branch_info.get("branch_status"),
        branch_message=branch_info.get("branch_message"),
        branch_helper_path=branch_info.get("branch_helper_path"),
        execution_style=req.execution_style,
        loop_budget=req.loop_budget,
    )

    # Create an isolated task workspace for the background job.
    from boss.runner.workspace import create_task_workspace
    task_slug = branch_info.get("task_slug") or job.job_id[:8]
    try:
        task_ws = create_task_workspace(
            source_path=project_path,
            task_slug=task_slug,
            branch_name=branch_info.get("branch_name"),
        )
        effective_workspace = task_ws.workspace_path
        update_background_job(job.job_id, task_workspace_path=effective_workspace)
    except Exception:
        logger.warning("Failed to create task workspace for job %s, using project_path", job.job_id, exc_info=True)
        effective_workspace = project_path

    if req.execution_style == "iterative":
        task = asyncio.create_task(
            _run_background_loop_job(
                job_id=job.job_id,
                message=message,
                session_id=session_id,
                mode=resolved_mode,
                workspace_root=effective_workspace,
                loop_budget=req.loop_budget,
            ),
            name=f"boss-loop-{job.job_id}",
        )
    else:
        task = asyncio.create_task(
            _run_background_job(
                job_id=job.job_id,
                run_input=prepared_input,
                session_id=session_id,
                mode=resolved_mode,
                emit_session=True,
                workspace_root=effective_workspace,
            ),
            name=f"boss-background-{job.job_id}",
        )
    background_job_tasks[job.job_id] = task
    return _job_detail_payload(load_background_job(job.job_id) or job)


@app.get("/api/jobs/{job_id}")
async def get_job_endpoint(job_id: str):
    job = load_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found")
    return _job_detail_payload(job)


@app.get("/api/jobs/{job_id}/logs")
async def tail_job_logs(job_id: str, limit: int = 200):
    job = load_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found")
    safe_limit = max(20, min(limit, 500))
    return tail_background_job_log(job_id, limit=safe_limit)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str):
    job = load_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found")
    if is_background_job_terminal(job.status):
        return _job_detail_payload(job)

    cancelled = await _cancel_background_job(
        job_id,
        final_status=BackgroundJobStatus.CANCELLED.value,
        reason="Background job cancelled by the user.",
    )
    return _job_detail_payload(cancelled)


@app.post("/api/jobs/{job_id}/resume")
async def resume_job_endpoint(job_id: str):
    job = load_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found")
    if job.job_id in background_job_tasks:
        raise HTTPException(status_code=409, detail="Background job is already running")
    if job.status == BackgroundJobStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="Completed jobs cannot be resumed")

    if job.pending_run_id:
        pending = load_pending_run(job.pending_run_id)
        if pending is None:
            expired = load_expired_pending_run(job.pending_run_id)
            if expired is not None:
                message = "This background job was waiting on an approval that has expired. Start a new job or relaunch the task."
            else:
                message = "The pending approval state for this background job could not be found."
            update_background_job(
                job_id,
                status=BackgroundJobStatus.INTERRUPTED.value,
                pending_run_id=None,
                latest_event=message,
                error_message=message,
            )
            raise HTTPException(status_code=409, detail=message)

        agent = build_entry_agent(mode=job.mode)
        run_input = await RunState.from_json(agent, pending.state)

        if job.execution_style == "iterative" and job.loop_id:
            from boss.loop.state import load_loop_state
            loop_state = load_loop_state(job.loop_id)
            task = asyncio.create_task(
                _run_background_loop_job(
                    job_id=job.job_id,
                    message=job.prompt,
                    session_id=job.session_id,
                    mode=job.mode,
                    workspace_root=job.task_workspace_path or job.project_path,
                    loop_budget=job.loop_budget,
                    resume_state=loop_state,
                    pending_run_input=run_input,
                    pending_run_id=job.pending_run_id,
                ),
                name=f"boss-loop-{job.job_id}",
            )
        else:
            task = asyncio.create_task(
                _run_background_job(
                    job_id=job.job_id,
                    run_input=run_input,
                    session_id=job.session_id,
                    mode=job.mode,
                    emit_session=False,
                    pending_run_id=job.pending_run_id,
                    is_resume=True,
                    workspace_root=job.task_workspace_path or job.project_path,
                ),
                name=f"boss-background-{job.job_id}",
            )
        background_job_tasks[job.job_id] = task
        return _job_detail_payload(load_background_job(job.job_id) or job)

    if job.execution_style == "iterative":
        from boss.loop.state import load_loop_state
        loop_state = load_loop_state(job.loop_id) if job.loop_id else None
        task = asyncio.create_task(
            _run_background_loop_job(
                job_id=job.job_id,
                message=job.prompt,
                session_id=job.session_id,
                mode=job.mode,
                workspace_root=job.task_workspace_path or job.project_path,
                loop_budget=job.loop_budget,
                resume_state=loop_state,
            ),
            name=f"boss-loop-{job.job_id}",
        )
    else:
        task = asyncio.create_task(
            _run_background_job(
                job_id=job.job_id,
                run_input=job.initial_input_payload,
                session_id=job.session_id,
                mode=job.mode,
                emit_session=not job.session_persisted,
                is_resume=True,
                workspace_root=job.task_workspace_path or job.project_path,
            ),
            name=f"boss-background-{job.job_id}",
        )
    background_job_tasks[job.job_id] = task
    return _job_detail_payload(load_background_job(job.job_id) or job)


@app.post("/api/jobs/{job_id}/takeover")
async def takeover_job_endpoint(job_id: str):
    job = load_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found")

    if not is_background_job_terminal(job.status) and jobs_takeover_cancels_background(job.project_path):
        job = await _cancel_background_job(
            job_id,
            final_status=BackgroundJobStatus.TAKEN_OVER.value,
            reason="Background job taken over into the foreground chat.",
        )
    elif not is_background_job_terminal(job.status) and job.status != BackgroundJobStatus.TAKEN_OVER.value:
        job = update_background_job(
            job_id,
            status=BackgroundJobStatus.TAKEN_OVER.value,
            latest_event="Background job taken over into the foreground chat.",
        )
        job = _clear_background_job_pending_run(job)
        append_background_job_log(job_id, event_type="taken_over", message="Background job taken over into the foreground chat.")

    return _build_takeover_payload(load_background_job(job_id) or job)


@app.delete("/api/memory/items/{source_table}/{item_id}")
async def delete_memory_item(source_table: str, item_id: int):
    store = get_knowledge_store()
    normalized = source_table.strip().lower()

    if normalized == "facts":
        deleted = store.delete_fact(item_id)
    elif normalized == "durable_memories":
        deleted = store.delete_durable_memory(item_id)
    elif normalized == "memory_candidates":
        deleted = store.delete_memory_candidate(item_id)
    elif normalized == "project_notes":
        deleted = store.delete_project_note(item_id)
    elif normalized == "conversation_episodes":
        deleted = store.delete_conversation_episode_by_id(item_id)
    else:
        raise HTTPException(status_code=400, detail="Unsupported memory source")

    if not deleted:
        raise HTTPException(status_code=404, detail="Memory item not found")

    return {"status": "deleted"}


# --- System ---

def _sdk_runtime_diagnostics() -> dict[str, Any]:
    """Return SDK runtime tool diagnostics for the status endpoint."""
    try:
        from boss.sdk_runtime import sdk_runtime_status
        return sdk_runtime_status()
    except Exception:
        return {"error": "sdk_runtime module unavailable"}


def _boss_control_health(control: dict[str, Any]) -> dict[str, Any]:
    files = control.get("files") or {}
    required = {
        "BOSS.md": files.get("BOSS.md") or {},
        ".boss/config.toml": files.get("config") or {},
    }
    missing_files = [label for label, item in required.items() if not item.get("exists")]
    rules_count = len(control.get("rules") or [])
    rules_healthy = rules_count > 0
    healthy = bool(control.get("configured")) and not missing_files and rules_healthy
    return {
        "configured": bool(control.get("configured")),
        "healthy": healthy,
        "rules_count": rules_count,
        "rules_healthy": rules_healthy,
        "missing_files": missing_files,
        "default_mode": control.get("default_mode"),
        "review_mode_name": control.get("review_mode_name"),
    }


def _diagnostics_summary(
    *,
    provider_mode: str,
    stats: dict[str, Any],
    runtime: dict[str, Any],
    pending_runs_count: int,
    jobs: list[Any],
) -> dict[str, Any]:
    runtime_trust = runtime.get("runtime_trust") or {}
    warnings = list(runtime_trust.get("warnings") or [])
    git = runtime.get("git") or {}
    control = runtime.get("boss_control") or {}
    control_health = _boss_control_health(control)
    lock_consistent = bool(runtime_trust.get("lock_exists")) and not warnings and runtime_trust.get("lock_pid") == runtime.get("process_id")

    return {
        "provider_mode": provider_mode,
        "git_available": bool(git.get("available")),
        "git_summary": git.get("summary") or "Git status unavailable.",
        "repo_clean": git.get("clean"),
        "pending_memory_count": int(stats.get("pending_memory_candidates", 0) or 0),
        "pending_jobs_count": len(jobs),
        "pending_runs_count": pending_runs_count,
        "lock_consistent": lock_consistent,
        "status_warnings": warnings,
        "boss_control_configured": bool(control.get("configured")),
        "boss_control_healthy": bool(control_health.get("healthy")),
        "rules_count": int(control_health.get("rules_count", 0) or 0),
    }


# --- Loop endpoints ---

@app.get("/api/loop/{loop_id}")
async def get_loop_state_endpoint(loop_id: str):
    from boss.loop.state import load_loop_state
    state = load_loop_state(loop_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Loop run not found")
    return state.to_dict()


@app.get("/api/loop")
async def list_loop_states_endpoint(limit: int = 20):
    from boss.loop.state import list_loop_states
    states = list_loop_states(limit=limit)
    return [s.to_dict() for s in states]


@app.post("/api/system/scan")
async def trigger_scan():
    result = full_scan()
    return result


@app.get("/api/system/status")
async def system_status():
    store = get_knowledge_store()
    stats = store.stats()
    pending_runs_count, pending_approvals_count, stale_runs_count = pending_run_metrics()
    jobs = list_background_jobs(limit=500)
    runtime = runtime_status_payload()
    provider_mode = resolve_provider_mode()
    control_health = _boss_control_health(runtime["boss_control"])
    diagnostics = _diagnostics_summary(
        provider_mode=provider_mode,
        stats=stats,
        runtime=runtime,
        pending_runs_count=pending_runs_count,
        jobs=jobs,
    )

    # Provider registry diagnostics
    try:
        from boss.providers.registry import provider_diagnostics as _provider_diag

        provider_registry = _provider_diag()
    except Exception:
        provider_registry = None

    return {
        "provider": "openai",
        "provider_mode": provider_mode,
        "provider_session_mode": resolve_provider_session_mode(),
        "responses_supported": supports_responses_mode(),
        "provider_registry": provider_registry,
        "app_version": runtime["app_version"],
        "build_marker": runtime["build_marker"],
        "models": {
            "boss": settings.general_model,
            "boss_review": settings.code_model,
            "mac": settings.mac_model,
            "web_search": settings.research_model,
            "guardrail": settings.guardrail_model,
        },
        "dependencies": dependency_availability(),
        "api_key_set": bool(settings.cloud_api_key),
        "memory": stats,
        "tracing_enabled": settings.tracing_enabled,
        "api_port": settings.api_port,
        "process_id": runtime["process_id"],
        "started_at": _iso_timestamp(runtime["started_at"]),
        "ready_at": _iso_timestamp(runtime["ready_at"]),
        "interpreter_path": runtime["interpreter_path"],
        "workspace_path": runtime["workspace_path"],
        "current_working_directory": runtime["current_working_directory"],
        "git": runtime["git"],
        "runtime_trust": runtime["runtime_trust"],
        "boss_control": runtime["boss_control"],
        "boss_control_health": control_health,
        "diagnostics": diagnostics,
        "api_lock_file": str(settings.api_lock_file),
        "log_path": str(settings.event_log_file),
        "pending_approvals_count": pending_approvals_count,
        "pending_runs_count": pending_runs_count,
        "stale_pending_runs_count": stale_runs_count,
        "background_jobs_count": len(jobs),
        "active_background_jobs_count": sum(1 for job in jobs if job.status == BackgroundJobStatus.RUNNING.value),
        "waiting_background_jobs_count": sum(1 for job in jobs if job.status == BackgroundJobStatus.WAITING_PERMISSION.value),
        "knowledge_db_path": str(settings.knowledge_db_file),
        "pending_runs_path": str(settings.pending_runs_dir),
        "background_jobs_path": str(settings.jobs_dir),
        "background_job_logs_path": str(settings.job_logs_dir),
        "runner": runtime.get("runner"),
        "sdk_runtime": _sdk_runtime_diagnostics(),
        "cors_allowed_origins": list(settings.cors_allowed_origins),
    }


@app.get("/api/system/prompt-diagnostics")
async def prompt_diagnostics(mode: str = "agent", agent_name: str = "boss", task_hint: str | None = None):
    """Inspect the layered prompt that would be assembled for a given mode and agent."""
    from boss.prompting.builder import PromptBuilder
    result = (
        PromptBuilder(mode=mode, agent_name=agent_name)
        .with_workspace(default_workspace_root())
        .with_task_hint(task_hint)
        .build()
    )
    summary = result.safe_summary()
    return {
        "mode": mode,
        "agent_name": agent_name,
        "task_hint": task_hint,
        **result.diagnostics(),
        **summary,
        "instructions_preview": result.text[:2000],
    }


@app.get("/api/system/ios-project")
async def ios_project_info(project_path: str | None = None):
    """Inspect an Xcode / iOS project and return structured intelligence."""
    from boss.intelligence.xcode import inspect_xcode_project

    path = project_path or str(default_workspace_root())
    info = inspect_xcode_project(path)
    return info.to_dict()


@app.get("/api/system/ios-toolchain")
async def ios_toolchain_status(refresh: bool = False):
    """Return the detected iOS toolchain availability."""
    from boss.ios_delivery.toolchain import get_toolchain

    toolchain = get_toolchain(refresh=refresh)
    return toolchain.to_dict()


@app.get("/api/system/ios-signing")
async def ios_signing_readiness():
    """Return signing credential readiness without leaking secrets."""
    from boss.ios_delivery.signing import check_signing_readiness

    return check_signing_readiness().to_dict()


@app.get("/api/system/providers")
async def provider_status():
    """Return provider registry: providers, capabilities, routing, and health."""
    from boss.providers.registry import get_registry, check_provider_health

    registry = get_registry()
    # Run health checks
    for provider in registry.providers:
        health = check_provider_health(provider)
        provider.health = health

    return registry.diagnostics()


# --- Preview endpoints ---

@app.get("/api/preview/status")
async def get_preview_status(project_path: str | None = None):
    """Return preview session status and capabilities."""
    from boss.preview.server import preview_status
    return preview_status(project_path)


@app.get("/api/preview/capabilities")
async def get_preview_capabilities():
    """Return available preview tooling on this machine."""
    from boss.preview.session import detect_preview_capabilities
    return detect_preview_capabilities().to_dict()


@app.post("/api/preview/start")
async def start_preview_endpoint(req: dict):
    """Start a preview server for a project."""
    from boss.preview.server import start_preview
    from boss.runner.engine import get_runner

    project_path = req.get("project_path", "")
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path is required")
    command = req.get("command")
    port = req.get("port")

    # Establish runner context so start_preview() enforces policy
    get_runner(mode="agent", workspace_root=project_path)

    session = start_preview(project_path, command=command, port=port)
    return session.to_dict()


@app.post("/api/preview/stop")
async def stop_preview_endpoint(req: dict):
    """Stop a running preview server."""
    from boss.preview.server import stop_preview
    project_path = req.get("project_path", "")
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path is required")
    stopped = stop_preview(project_path)
    return {"stopped": stopped}


@app.post("/api/preview/capture")
async def capture_preview_endpoint(req: dict):
    """Capture a screenshot and diagnostics from a preview URL."""
    import time
    from boss.preview.session import capture_screenshot, get_active_session
    from boss.runner.engine import get_runner

    url = req.get("url", "")
    project_path = req.get("project_path", "")
    detail_mode = req.get("detail_mode", "auto")
    region = req.get("region")

    if not url:
        session = get_active_session(project_path or None)
        if session and session.url:
            url = session.url
        else:
            raise HTTPException(status_code=400, detail="No URL provided and no active preview session")

    # Establish runner context so capture enforces policy
    get_runner(mode="agent", workspace_root=project_path or None)

    captures_dir = settings.app_data_dir / "preview_captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    output_path = captures_dir / f"capture_{timestamp}.png"

    capture_kwargs: dict = {"detail_mode": detail_mode}
    if region and isinstance(region, dict):
        from boss.preview.session import CaptureRegion

        capture_kwargs["region"] = CaptureRegion.from_dict(region)

    result = capture_screenshot(url, output_path, **capture_kwargs)

    # Update session metadata
    if project_path:
        session = get_active_session(project_path)
        if session:
            session.last_capture = result

    return result.to_dict()


# --- Runner / Task Workspace endpoints ---

@app.get("/api/runner/policy")
async def runner_policy(mode: str | None = None):
    from boss.runner.policy import runner_config_for_mode
    policy = runner_config_for_mode(mode)
    return policy.to_dict()


@app.get("/api/runner/workspaces")
async def runner_workspaces(state: str | None = None, limit: int = 50):
    from boss.runner.workspace import list_task_workspaces
    workspaces = list_task_workspaces(state=state, limit=limit)
    return {"workspaces": [ws.to_dict() for ws in workspaces]}


@app.get("/api/runner/sandbox")
async def runner_sandbox():
    from boss.runner.sandbox import sandbox_status_payload
    return sandbox_status_payload()


# --- Code Intelligence endpoints ---

@app.get("/api/intelligence/symbols")
async def intelligence_symbols(
    name: str,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 20,
):
    from boss.intelligence.index import get_code_index
    idx = get_code_index()
    results = idx.find_symbol(name, kind=kind, project_path=project, limit=limit)
    return {"symbols": [r.to_dict() for r in results]}


@app.get("/api/intelligence/definition")
async def intelligence_definition(
    name: str,
    project: str | None = None,
):
    from boss.intelligence.index import get_code_index
    idx = get_code_index()
    results = idx.find_definition(name, project_path=project, limit=10)
    return {"definitions": [r.to_dict() for r in results]}


@app.get("/api/intelligence/search")
async def intelligence_search(
    query: str,
    project: str | None = None,
    limit: int = 15,
):
    from boss.intelligence.retrieval import hybrid_search
    results, caps = hybrid_search(query, project_path=project, limit=limit)
    return {
        "results": [r.to_dict() for r in results],
        "capabilities": {
            "symbol_search": caps.symbol_search,
            "keyword_search": caps.keyword_search,
            "memory_search": caps.memory_search,
            "semantic_search": caps.semantic_search,
        },
    }


@app.get("/api/intelligence/graph")
async def intelligence_graph(project: str):
    from boss.intelligence.index import get_code_index
    idx = get_code_index()
    return idx.project_graph(project)


@app.get("/api/intelligence/importers")
async def intelligence_importers(
    module_or_symbol: str,
    project: str | None = None,
    limit: int = 20,
):
    from boss.intelligence.index import get_code_index
    idx = get_code_index()
    results = idx.find_importers(module_or_symbol, project_path=project, limit=limit)
    return {"importers": [r.to_dict() for r in results]}


@app.get("/api/intelligence/stats")
async def intelligence_stats():
    from boss.intelligence.index import get_code_index
    idx = get_code_index()
    stats = idx.stats()
    try:
        from boss.intelligence.embeddings import get_embeddings_store
        emb_stats = get_embeddings_store().stats()
        stats.update(emb_stats)
    except Exception:
        pass
    try:
        from boss.intelligence.retrieval import capabilities
        caps = capabilities()
        stats["capabilities"] = {
            "symbol_search": caps.symbol_search,
            "keyword_search": caps.keyword_search,
            "memory_search": caps.memory_search,
            "semantic_search": caps.semantic_search,
        }
    except Exception:
        pass
    return stats


# ── Workers API ─────────────────────────────────────────────────────


@app.get("/api/workers/plans")
async def list_work_plans_endpoint(limit: int = 50):
    from boss.workers.state import list_work_plans
    safe_limit = max(1, min(limit, 200))
    return [p.to_dict() for p in list_work_plans(limit=safe_limit)]


@app.get("/api/workers/plans/{plan_id}")
async def get_work_plan_endpoint(plan_id: str):
    from boss.workers.state import load_work_plan
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    return plan.to_dict()


@app.post("/api/workers/plans")
async def create_work_plan_endpoint(request: Request):
    from boss.workers.coordinator import create_work_plan
    body = await request.json()
    task = body.get("task", "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="task is required")
    project_path = body.get("project_path") or str(load_boss_control().root)
    session_id = body.get("session_id") or str(uuid.uuid4())
    max_concurrent = body.get("max_concurrent", settings.max_concurrent_workers)
    plan = create_work_plan(
        task=task,
        project_path=project_path,
        session_id=session_id,
        max_concurrent=max_concurrent,
    )
    return plan.to_dict()


@app.post("/api/workers/plans/{plan_id}/workers")
async def add_worker_endpoint(plan_id: str, request: Request):
    from boss.workers.coordinator import add_worker
    from boss.workers.roles import WorkerRole
    from boss.workers.state import load_work_plan
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    body = await request.json()
    role_str = body.get("role", "").strip()
    scope = body.get("scope", "").strip()
    if not role_str or not scope:
        raise HTTPException(status_code=400, detail="role and scope are required")
    try:
        role = WorkerRole(role_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role_str}")
    file_targets = body.get("file_targets", [])
    worker = add_worker(plan, role=role, scope=scope, file_targets=file_targets)
    return worker.to_dict()


@app.post("/api/workers/plans/{plan_id}/validate")
async def validate_plan_endpoint(plan_id: str):
    from boss.workers.coordinator import validate_plan, validate_plan_directory_overlap
    from boss.workers.state import load_work_plan
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    file_report = validate_plan(plan)
    dir_report = validate_plan_directory_overlap(plan)
    return {
        "file_conflicts": {
            "has_conflicts": file_report.has_conflicts,
            "detail": file_report.summary(),
        },
        "directory_overlap": {
            "has_conflicts": dir_report.has_conflicts,
            "detail": dir_report.summary(),
        },
    }


@app.post("/api/workers/plans/{plan_id}/ready")
async def mark_plan_ready_endpoint(plan_id: str, request: Request):
    from boss.workers.coordinator import mark_plan_ready
    from boss.workers.state import load_work_plan
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    body = await request.json() if await request.body() else {}
    force = body.get("force", False)
    report = mark_plan_ready(plan, force=force)
    return {
        "status": plan.status,
        "conflict_report": report.summary(),
    }


@app.post("/api/workers/plans/{plan_id}/execute")
async def execute_plan_endpoint(plan_id: str):
    from boss.workers.engine import execute_plan
    from boss.workers.state import load_work_plan, WorkPlanStatus
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    if plan.status != WorkPlanStatus.READY.value:
        raise HTTPException(status_code=400, detail=f"Plan must be in ready state, got {plan.status}")

    async def _stream():
        async for event in execute_plan(plan):
            yield sse_event(event)
        yield sse_event({"type": "done", "plan_id": plan.plan_id})

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.post("/api/workers/plans/{plan_id}/cancel")
async def cancel_plan_endpoint(plan_id: str):
    from boss.workers.engine import cancel_running_plan
    from boss.workers.state import load_work_plan
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    updated = await cancel_running_plan(plan)
    return updated.to_dict()


@app.get("/api/workers/plans/{plan_id}/summary")
async def plan_summary_endpoint(plan_id: str):
    from boss.workers.coordinator import plan_summary
    from boss.workers.state import load_work_plan
    plan = load_work_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    return plan_summary(plan)


# --- Deploy endpoints ---


@app.get("/api/deploy/status")
async def deploy_status_endpoint():
    from boss.deploy.engine import deploy_status
    return deploy_status()


@app.get("/api/deploy/deployments")
async def list_deployments_endpoint(limit: int = 50):
    from boss.deploy.state import list_deployments
    safe_limit = max(1, min(limit, 200))
    return [d.to_dict() for d in list_deployments(limit=safe_limit)]


@app.get("/api/deploy/deployments/{deployment_id}")
async def get_deployment_endpoint(deployment_id: str):
    from boss.deploy.state import load_deployment
    deploy = load_deployment(deployment_id)
    if deploy is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deploy.to_dict()


@app.post("/api/deploy/deployments")
async def create_deployment_endpoint(request: Request):
    from boss.deploy.engine import create_deployment
    body = await request.json()
    project_path = body.get("project_path", "").strip()
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path is required")
    if not settings.deploy_enabled:
        raise HTTPException(status_code=403, detail="Deployment is not enabled. Set BOSS_DEPLOY_ENABLED=true.")
    if not body.get("approved"):
        raise HTTPException(
            status_code=403,
            detail="Deploy actions require explicit approval. Set approved=true to confirm.",
        )
    try:
        deploy = create_deployment(
            project_path=project_path,
            session_id=body.get("session_id", "api"),
            adapter_name=body.get("adapter") or None,
            target=body.get("target", "preview"),
        )
        logger.info(
            "Deployment created via API: deployment_id=%s adapter=%s project=%s",
            deploy.deployment_id, deploy.adapter, project_path,
        )
        return deploy.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/deploy/deployments/{deployment_id}/run")
async def run_deployment_endpoint(request: Request, deployment_id: str):
    from boss.deploy.engine import run_deployment
    if not settings.deploy_enabled:
        raise HTTPException(status_code=403, detail="Deployment is not enabled.")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("approved"):
        raise HTTPException(
            status_code=403,
            detail="Deploy execution requires explicit approval. Set approved=true to confirm.",
        )
    try:
        logger.info("Deployment run approved via API: deployment_id=%s", deployment_id)
        deploy = run_deployment(deployment_id)
        return deploy.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/deploy/deployments/{deployment_id}/teardown")
async def teardown_deployment_endpoint(request: Request, deployment_id: str):
    from boss.deploy.engine import teardown_deployment
    if not settings.deploy_enabled:
        raise HTTPException(status_code=403, detail="Deployment is not enabled.")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("approved"):
        raise HTTPException(
            status_code=403,
            detail="Teardown requires explicit approval. Set approved=true to confirm.",
        )
    try:
        logger.info("Deployment teardown approved via API: deployment_id=%s", deployment_id)
        deploy = teardown_deployment(deployment_id)
        return deploy.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/deploy/deployments/{deployment_id}/cancel")
async def cancel_deployment_endpoint(deployment_id: str):
    from boss.deploy.engine import cancel_deployment
    if not settings.deploy_enabled:
        raise HTTPException(status_code=403, detail="Deployment is not enabled.")
    try:
        deploy = cancel_deployment(deployment_id)
        return deploy.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --- iOS delivery endpoints ---


@app.get("/api/ios-delivery/status")
async def ios_delivery_status_endpoint():
    from boss.ios_delivery.engine import delivery_status
    return delivery_status()


@app.get("/api/ios-delivery/runs")
async def ios_delivery_list_runs(limit: int = 50):
    from boss.ios_delivery.state import list_runs
    safe_limit = max(1, min(limit, 200))
    return [r.to_dict() for r in list_runs(limit=safe_limit)]


@app.get("/api/ios-delivery/runs/{run_id}")
async def ios_delivery_get_run(run_id: str):
    from boss.ios_delivery.state import load_run
    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="iOS delivery run not found")
    return run.to_dict()


@app.get("/api/ios-delivery/runs/{run_id}/events")
async def ios_delivery_get_events(run_id: str):
    from boss.ios_delivery.state import load_run, read_events
    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="iOS delivery run not found")
    return read_events(run_id)


@app.post("/api/ios-delivery/runs")
async def ios_delivery_create_run(request: Request):
    from boss.ios_delivery.engine import create_run
    body = await request.json()
    project_path = body.get("project_path", "").strip()
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path is required")
    try:
        run = create_run(
            project_path=project_path,
            scheme=body.get("scheme") or None,
            configuration=body.get("configuration", "Release"),
            export_method=body.get("export_method", "app-store"),
            upload_target=body.get("upload_target", "none"),
        )
        return run.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/ios-delivery/runs/{run_id}/cancel")
async def ios_delivery_cancel_run(run_id: str):
    from boss.ios_delivery.engine import cancel_run
    from boss.ios_delivery.state import load_run
    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="iOS delivery run not found")
    run = cancel_run(run)
    return run.to_dict()


@app.post("/api/ios-delivery/runs/{run_id}/start")
async def ios_delivery_start_run(run_id: str):
    """Start executing the delivery pipeline for a pending run.

    The pipeline (inspect → archive → export → optional upload) runs on a
    background thread because xcodebuild can take minutes.  The endpoint
    returns the run immediately so the client can poll progress.
    """
    import contextvars
    import threading

    from boss.ios_delivery.engine import run_full_pipeline
    from boss.ios_delivery.state import DeliveryPhase, load_run
    from boss.runner.engine import get_runner

    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="iOS delivery run not found")
    if run.phase != DeliveryPhase.PENDING.value:
        raise HTTPException(
            status_code=409,
            detail=f"Run is in phase '{run.phase}', expected 'pending'",
        )

    # Establish runner context in the request thread so it can be
    # propagated to the background thread via copy_context().
    get_runner(mode="deploy", workspace_root=run.project_path)

    # copy_context() snapshots all ContextVars (including the runner)
    # so that run_full_pipeline sees the governed runner on the child thread.
    ctx = contextvars.copy_context()

    def _run_pipeline() -> None:
        try:
            run_full_pipeline(run)
        except Exception:
            import logging

            logging.getLogger("boss.api").exception(
                "Pipeline execution failed for run %s", run_id
            )

    threading.Thread(
        target=ctx.run, args=(_run_pipeline,),
        daemon=True, name=f"ios-delivery-{run_id}",
    ).start()

    return run.to_dict()


@app.post("/api/ios-delivery/runs/{run_id}/upload")
async def ios_delivery_start_upload(run_id: str):
    """Trigger upload of a completed export to TestFlight / App Store Connect.

    The run must have an IPA path and an upload target other than 'none'.
    Upload is executed through the governed runner.
    """
    from boss.ios_delivery.engine import upload_artifact
    from boss.ios_delivery.state import UploadTarget, load_run
    from boss.runner.engine import get_runner

    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="iOS delivery run not found")
    if not run.ipa_path:
        raise HTTPException(status_code=400, detail="Run has no IPA — export must complete first")
    if run.upload_target == UploadTarget.NONE.value:
        raise HTTPException(status_code=400, detail="Run has no upload target configured")

    # Establish runner context so upload subprocess goes through governance.
    # Use deploy mode — uploads are external actions (App Store Connect),
    # not workspace edits, so they need FULL_ACCESS rather than
    # WORKSPACE_WRITE which does not allow xcrun/fastlane prefixes.
    get_runner(mode="deploy", workspace_root=run.project_path)

    run = upload_artifact(run)
    return run.to_dict()


@app.get("/api/ios-delivery/runs/{run_id}/upload-status")
async def ios_delivery_upload_status(run_id: str):
    """Check the processing status of an uploaded build."""
    from boss.ios_delivery.state import load_run
    from boss.ios_delivery.upload import check_processing_status
    from boss.runner.engine import get_runner

    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="iOS delivery run not found")

    # Establish runner context so pilot status query goes through governance.
    # Deploy mode — querying App Store Connect is an external action.
    get_runner(mode="deploy", workspace_root=run.project_path)

    status = check_processing_status(run)
    return status.to_dict()
