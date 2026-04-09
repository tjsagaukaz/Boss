from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from boss.config import settings

logger = logging.getLogger(__name__)


SESSION_STATE_VERSION = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionState:
    session_id: str
    version: int = SESSION_STATE_VERSION
    summary: str = ""
    recent_items: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=_utcnow)
    total_turns: int = 0
    archived_turns: int = 0


def ensure_history_dir() -> Path:
    settings.history_dir.mkdir(parents=True, exist_ok=True)
    return settings.history_dir


def session_path(session_id: str) -> Path:
    history_dir = ensure_history_dir()
    return history_dir / f"{session_id}.json"


def count_user_turns(items: list[Any]) -> int:
    return sum(1 for item in items if isinstance(item, dict) and item.get("role") == "user")


def extract_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def extract_display_messages(state: SessionState) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if state.summary:
        messages.append(
            {
                "role": "assistant",
                "content": f"Earlier conversation summary:\n{state.summary}",
            }
        )

    for item in state.recent_items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = extract_message_text(item)
        if text:
            messages.append({"role": role, "content": text})
    return messages


def save_session_state(state: SessionState) -> Path:
    path = session_path(state.session_id)
    state.version = SESSION_STATE_VERSION
    state.updated_at = _utcnow()
    path.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False))
    return path


def load_session_state(session_id: str) -> SessionState:
    path = session_path(session_id)
    if not path.exists():
        return SessionState(session_id=session_id)

    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Corrupt session file %s: %s — returning empty state", path, exc)
        return SessionState(session_id=session_id)

    if isinstance(payload, list):
        state = SessionState(
            session_id=session_id,
            recent_items=payload,
            total_turns=count_user_turns(payload),
        )
        save_session_state(state)
        return state

    if not isinstance(payload, dict):
        return SessionState(session_id=session_id)

    recent_items = payload.get("recent_items")
    if not isinstance(recent_items, list):
        legacy_history = payload.get("history")
        recent_items = legacy_history if isinstance(legacy_history, list) else []

    total_turns = payload.get("total_turns")
    archived_turns = payload.get("archived_turns")

    state = SessionState(
        session_id=payload.get("session_id", session_id),
        version=int(payload.get("version", SESSION_STATE_VERSION)),
        summary=str(payload.get("summary", "")),
        recent_items=recent_items,
        updated_at=str(payload.get("updated_at", _utcnow())),
        total_turns=int(total_turns) if isinstance(total_turns, int) else count_user_turns(recent_items),
        archived_turns=int(archived_turns) if isinstance(archived_turns, int) else 0,
    )

    if payload.get("version") != SESSION_STATE_VERSION or "recent_items" not in payload:
        save_session_state(state)

    return state


def clear_session_state(session_id: str) -> Path:
    return save_session_state(SessionState(session_id=session_id))


def save_history(session_id: str, history: list) -> Path:
    state = SessionState(
        session_id=session_id,
        recent_items=history,
        total_turns=count_user_turns(history),
    )
    return save_session_state(state)


def load_history(session_id: str) -> list:
    return load_session_state(session_id).recent_items