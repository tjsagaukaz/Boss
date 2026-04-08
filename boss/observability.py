from __future__ import annotations

import json
import threading
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

from boss.config import settings


_session_id_var: ContextVar[str | None] = ContextVar("boss_session_id", default=None)
_agent_name_var: ContextVar[str | None] = ContextVar("boss_agent_name", default=None)
_run_id_var: ContextVar[str | None] = ContextVar("boss_run_id", default=None)
_write_lock = threading.Lock()


def set_log_context(
    *,
    session_id: str | None = None,
    agent_name: str | None = None,
    run_id: str | None = None,
) -> dict[str, Token[Any]]:
    tokens: dict[str, Token[Any]] = {}
    if session_id is not None:
        tokens["session_id"] = _session_id_var.set(session_id)
    if agent_name is not None:
        tokens["agent_name"] = _agent_name_var.set(agent_name)
    if run_id is not None:
        tokens["run_id"] = _run_id_var.set(run_id)
    return tokens


def reset_log_context(tokens: dict[str, Token[Any]]) -> None:
    session_token = tokens.get("session_id")
    if session_token is not None:
        _session_id_var.reset(session_token)

    agent_token = tokens.get("agent_name")
    if agent_token is not None:
        _agent_name_var.reset(agent_token)

    run_token = tokens.get("run_id")
    if run_token is not None:
        _run_id_var.reset(run_token)


def set_active_agent(agent_name: str | None) -> None:
    _agent_name_var.set(agent_name)


def append_local_event(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }

    session_id = fields.pop("session_id", None) or _session_id_var.get()
    agent_name = fields.pop("agent_name", None) or _agent_name_var.get()
    run_id = fields.pop("run_id", None) or _run_id_var.get()

    if session_id is not None:
        payload["session_id"] = session_id
    if agent_name is not None:
        payload["agent_name"] = agent_name
    if run_id is not None:
        payload["run_id"] = run_id

    payload.update({key: value for key, value in fields.items() if value is not None})

    try:
        settings.event_log_file.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with settings.event_log_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


def log_session_event(
    *,
    session_id: str,
    message_length: int,
    history_items: int,
    resumed_session: bool,
) -> None:
    append_local_event(
        "session.requested",
        session_id=session_id,
        message_length=message_length,
        history_items=history_items,
        resumed_session=resumed_session,
    )


def log_agent_change(*, from_agent: str | None, to_agent: str) -> None:
    append_local_event(
        "agent.changed",
        agent_name=to_agent,
        from_agent=from_agent,
        to_agent=to_agent,
    )


def log_tool_call(
    *,
    call_id: str,
    tool_name: str,
    title: str,
    execution_type: str,
    scope_label: str,
    arguments_length: int,
) -> None:
    append_local_event(
        "tool.called",
        call_id=call_id,
        tool_name=tool_name,
        title=title,
        execution_type=execution_type,
        scope_label=scope_label,
        arguments_length=arguments_length,
    )


def log_tool_result(*, call_id: str, output_length: int) -> None:
    append_local_event(
        "tool.completed",
        call_id=call_id,
        output_length=output_length,
    )


def log_permission_event(
    *,
    stage: str,
    tool_name: str,
    execution_type: str,
    scope_label: str,
    approval_id: str | None = None,
    scope_key: str | None = None,
    decision: str | None = None,
    source: str | None = None,
    run_id: str | None = None,
    approval_time_ms: int | None = None,
) -> None:
    append_local_event(
        f"permission.{stage}",
        run_id=run_id,
        approval_id=approval_id,
        tool_name=tool_name,
        execution_type=execution_type,
        scope_label=scope_label,
        scope_key=scope_key,
        decision=decision,
        source=source,
        approval_time_ms=approval_time_ms,
    )


def log_memory_injection(
    *,
    source: str,
    category: str,
    result_count: int,
    query: str | None = None,
    project_path: str | None = None,
) -> None:
    append_local_event(
        "memory.injected",
        source=source,
        category=category,
        result_count=result_count,
        query=query,
        project_path=project_path,
    )


def log_memory_distillation(
    *,
    source: str,
    category: str,
    key: str,
    value_length: int,
) -> None:
    append_local_event(
        "memory.distilled",
        source=source,
        category=category,
        key=key,
        value_length=value_length,
    )