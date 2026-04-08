"""FastAPI API layer for Boss Assistant — serves the SwiftUI frontend via SSE streaming."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
from boss.memory.injection import build_memory_injection
from boss.memory.knowledge import get_knowledge_store
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

app = FastAPI(title="Boss Assistant API", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

session_context_manager = SessionContextManager()


# --- Request / Response models ---

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class FactRequest(BaseModel):
    category: str
    key: str
    value: str


class PermissionDecisionRequest(BaseModel):
    run_id: str
    approval_id: str
    decision: PermissionDecision


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
        "deletable": result.source_table in {"durable_memories", "project_notes", "conversation_episodes"},
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
        )
        for item in store.list_durable_memories(limit=16)
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
            else build_memory_injection(user_message=preview_message)
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
):
    agent = build_entry_agent()
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
        trace_metadata={"surface": "api", "session_id": session_id},
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
                    yield sse_event({"type": "agent", "name": event.new_agent.name})

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


# --- Chat endpoint with SSE streaming ---

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())

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
    prepared_input = session_context_manager.prepare_input(session_id, req.message).model_input

    return StreamingResponse(
        _stream_chat_run(
            run_input=prepared_input,
            session_id=session_id,
            emit_session=True,
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

    agent = build_entry_agent()
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

    return StreamingResponse(
        _stream_chat_run(
            run_input=state,
            session_id=record.session_id,
            emit_session=False,
            pending_run_id=req.run_id,
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


@app.delete("/api/memory/items/{source_table}/{item_id}")
async def delete_memory_item(source_table: str, item_id: int):
    store = get_knowledge_store()
    normalized = source_table.strip().lower()

    if normalized == "facts":
        deleted = store.delete_fact(item_id)
    elif normalized == "durable_memories":
        deleted = store.delete_durable_memory(item_id)
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

@app.post("/api/system/scan")
async def trigger_scan():
    result = full_scan()
    return result


@app.get("/api/system/status")
async def system_status():
    store = get_knowledge_store()
    stats = store.stats()
    pending_runs_count, pending_approvals_count, stale_runs_count = pending_run_metrics()
    runtime = runtime_status_payload()
    return {
        "provider": "openai",
        "provider_mode": resolve_provider_mode(),
        "provider_session_mode": resolve_provider_session_mode(),
        "responses_supported": supports_responses_mode(),
        "app_version": runtime["app_version"],
        "build_marker": runtime["build_marker"],
        "models": {
            "general": settings.general_model,
            "mac": settings.mac_model,
            "research": settings.research_model,
            "reasoning": settings.reasoning_model,
            "code": settings.code_model,
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
        "runtime_trust": runtime["runtime_trust"],
        "api_lock_file": str(settings.api_lock_file),
        "log_path": str(settings.event_log_file),
        "pending_run_count": pending_runs_count,
        "pending_approvals_count": pending_approvals_count,
        "pending_runs_count": pending_runs_count,
        "stale_pending_run_count": stale_runs_count,
        "stale_run_count": stale_runs_count,
        "knowledge_db_path": str(settings.knowledge_db_file),
        "pending_runs_path": str(settings.pending_runs_dir),
    }


# --- Warm-up on startup ---

@app.on_event("startup")
async def startup():
    expired_runs = cleanup_stale_pending_runs()
    mark_api_server_ready()
    logger.info(
        "Boss API started (provider_mode=%s, expired_pending_runs=%s)",
        resolve_provider_mode(),
        expired_runs,
    )


@app.on_event("shutdown")
async def shutdown():
    release_api_server_lock()
