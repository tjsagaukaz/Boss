from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from boss.config import settings
from boss.memory.distillation import distill_latest_turn
from boss.memory.injection import MemoryInjection, build_memory_injection
from boss.memory.knowledge import get_knowledge_store
from boss.observability import log_memory_distillation, log_memory_injection
from boss.persistence.history import (
    SessionState,
    count_user_turns,
    extract_message_text,
    load_session_state,
    save_session_state,
)


_CONTEXT_PREFIX = "BOSS_CONTEXT:"
_MAX_FACT_INJECTIONS = 5
_MAX_PROJECT_INJECTIONS = 3


@dataclass
class PreparedSessionInput:
    session: SessionState
    model_input: list[dict[str, Any]]


class SessionContextManager:
    def load_session_read_only(self, session_id: str) -> SessionState:
        return load_session_state(session_id)

    def load_session_for_maintenance(self, session_id: str) -> SessionState:
        state = self.load_session_read_only(session_id)
        return self._maintain_session_state(state, persist=False)

    def prepare_input(self, session_id: str, user_message: str) -> PreparedSessionInput:
        session = self.load_session_read_only(session_id)
        injection = build_memory_injection(
            user_message=user_message,
            session_summary=session.summary,
        )
        memory_text = injection.text

        model_input: list[dict[str, Any]] = []
        if session.summary and session.total_turns >= settings.session_summary_threshold:
            model_input.append(
                self._context_message(
                    "session_summary",
                    "Earlier conversation summary. Prefer the recent turns below if anything conflicts.\n"
                    f"{session.summary}",
                )
            )
            log_memory_injection(
                source="session_context_manager",
                category="session_summary",
                result_count=1,
            )

        if memory_text:
            log_memory_injection(
                source="session_context_manager",
                category="memory_results",
                result_count=len(injection.results),
                query=injection.query,
                project_path=injection.project_path,
            )
            model_input.append(
                self._context_message(
                    "memory",
                    "Relevant persisted memory for this request. Use it only when it helps answer accurately.\n"
                    f"{memory_text}",
                )
            )

        model_input.extend(session.recent_items)
        model_input.append({"role": "user", "content": user_message, "type": "message"})

        return PreparedSessionInput(session=session, model_input=model_input)

    def preview_memory_injection(self, session_id: str, user_message: str) -> MemoryInjection:
        session = self.load_session_read_only(session_id)
        return build_memory_injection(
            user_message=user_message,
            session_summary=session.summary,
        )

    def persist_result(self, session_id: str, run_history: list[Any]) -> SessionState:
        state = self.load_session_read_only(session_id)
        previous_recent_turns = count_user_turns(state.recent_items)

        state.recent_items = self._sanitize_items(self._strip_context_items(run_history))

        current_recent_turns = count_user_turns(state.recent_items)
        new_turns = max(0, current_recent_turns - previous_recent_turns)
        state.total_turns = max(
            state.total_turns + new_turns,
            state.archived_turns + current_recent_turns,
        )

        self._maintain_session_state(state, persist=True)
        self._distill_memories(state)
        return state

    def _maintain_session_state(self, state: SessionState, *, persist: bool) -> SessionState:
        changed = self._compact_session(state)
        if persist or changed:
            save_session_state(state)
        self._sync_session_episode(state)
        return state

    def _compact_session(self, state: SessionState) -> bool:
        changed = False
        filtered_items = self._sanitize_items(self._strip_context_items(state.recent_items))
        if filtered_items != state.recent_items:
            state.recent_items = filtered_items
            changed = True

        turns = self._split_turns(state.recent_items)
        archived_turns: list[list[dict[str, Any]]] = []

        if len(turns) > settings.session_summary_threshold:
            while len(turns) > settings.session_max_recent_turns:
                archived_turns.append(turns.pop(0))

        while len(turns) > settings.session_max_recent_turns:
            archived_turns.append(turns.pop(0))

        while self._serialized_size(state.summary, self._flatten_turns(turns)) > settings.session_max_serialized_size and len(turns) > 1:
            archived_turns.append(turns.pop(0))

        if archived_turns:
            fragment = self._summarize_turns(archived_turns)
            if fragment:
                merged_summary = self._merge_summary(state.summary, fragment)
                if merged_summary != state.summary:
                    state.summary = merged_summary
                    changed = True
                    log_memory_distillation(
                        source="session_context_manager",
                        category="session_summary",
                        key=state.session_id,
                        value_length=len(fragment),
                    )
            state.archived_turns += len(archived_turns)
            changed = True

        flattened = self._flatten_turns(turns)
        if flattened != state.recent_items:
            state.recent_items = flattened
            changed = True

        total_turns = state.archived_turns + count_user_turns(state.recent_items)
        if total_turns != state.total_turns:
            state.total_turns = total_turns
            changed = True

        summary = self._trim_summary(state.summary)
        if summary != state.summary:
            state.summary = summary
            changed = True

        return changed

    def _build_memory_context(self, user_message: str) -> str:
        injection = build_memory_injection(user_message=user_message)
        return injection.text

    def _sync_session_episode(self, state: SessionState) -> None:
        store = get_knowledge_store()
        if not state.summary:
            store.delete_conversation_episode(state.session_id)
            return

        store.store_conversation_episode(
            session_id=state.session_id,
            title=self._session_title(state),
            summary=state.summary,
            source="session_context_manager",
            confidence=0.7,
            salience=0.6,
            tags=["session_summary", "session_context_manager"],
            created_at=state.updated_at,
            updated_at=state.updated_at,
            last_used_at=state.updated_at,
        )

    def _distill_memories(self, state: SessionState) -> None:
        if not settings.auto_memory_enabled:
            return
        distill_latest_turn(
            session_id=state.session_id,
            session_summary=state.summary,
            recent_items=state.recent_items,
        )

    def _session_title(self, state: SessionState) -> str:
        for item in state.recent_items:
            if isinstance(item, dict) and item.get("role") == "user":
                text = extract_message_text(item)
                if text:
                    return self._clip(text, 80)
        return f"Session {state.session_id}"

    def _select_relevant_facts(self, facts: list[Fact], user_message: str) -> list[Fact]:
        message = user_message.lower()
        tokens = self._keywords(user_message)
        scored: list[tuple[int, Fact]] = []

        for fact in facts:
            haystack = f"{fact.category} {fact.key} {fact.value}".lower()
            score = 0
            if fact.key.lower() in message:
                score += 5
            if fact.category.lower() in message:
                score += 2
            for token in tokens:
                if token in haystack:
                    score += 1
            if score > 0:
                scored.append((score, fact))

        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [fact for _score, fact in scored[:_MAX_FACT_INJECTIONS]]

    def _select_relevant_projects(self, projects: list[Project], user_message: str) -> list[Project]:
        message = user_message.lower()
        scored: list[tuple[int, Project]] = []

        for project in projects:
            score = 0
            name = project.name.lower()
            path = project.path.lower()
            if name and name in message:
                score += 4
            if path and path in message:
                score += 3
            if score > 0:
                scored.append((score, project))

        scored.sort(key=lambda item: (item[0], item[1].last_scanned), reverse=True)
        return [project for _score, project in scored[:_MAX_PROJECT_INJECTIONS]]

    def _strip_context_items(self, items: list[Any]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("role") == "developer":
                content = item.get("content")
                if isinstance(content, str) and content.startswith(_CONTEXT_PREFIX):
                    continue
            filtered.append(item)
        return filtered

    def _sanitize_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_string_length = max(2048, settings.session_max_serialized_size // 8)
        return [self._sanitize_value(item, max_string_length) for item in items]

    def _sanitize_value(self, value: Any, max_string_length: int) -> Any:
        if isinstance(value, str):
            return self._clip(value, max_string_length)
        if isinstance(value, list):
            return [self._sanitize_value(item, max_string_length) for item in value]
        if isinstance(value, dict):
            return {key: self._sanitize_value(item, max_string_length) for key, item in value.items()}
        return value

    def _split_turns(self, items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        turns: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []

        for item in items:
            if self._is_user_message(item):
                if current:
                    turns.append(current)
                current = [item]
            else:
                current.append(item)

        if current:
            turns.append(current)

        return turns

    @staticmethod
    def _flatten_turns(turns: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return [item for turn in turns for item in turn]

    def _summarize_turns(self, turns: list[list[dict[str, Any]]]) -> str:
        lines: list[str] = []
        for turn in turns:
            user_text = self._clip(self._collect_role_text(turn, "user"), 140)
            assistant_text = self._clip(self._collect_role_text(turn, "assistant"), 180)
            tool_names = self._collect_tool_names(turn)

            parts = []
            if user_text:
                parts.append(f"User: {user_text}")
            if assistant_text:
                parts.append(f"Assistant: {assistant_text}")
            if tool_names:
                parts.append(f"Tools: {', '.join(tool_names[:4])}")
            if parts:
                lines.append("- " + " | ".join(parts))

        return "\n".join(lines)

    def _merge_summary(self, existing: str, fragment: str) -> str:
        if not existing:
            return self._trim_summary(fragment)
        return self._trim_summary(existing + "\n" + fragment)

    def _trim_summary(self, summary: str) -> str:
        max_summary_length = max(1024, settings.session_max_serialized_size // 2)
        if len(summary) <= max_summary_length:
            return summary
        clipped = summary[-max_summary_length:]
        first_newline = clipped.find("\n")
        if first_newline > 0:
            clipped = clipped[first_newline + 1 :]
        return "[Earlier context condensed]\n" + clipped

    def _serialized_size(self, summary: str, recent_items: list[dict[str, Any]]) -> int:
        payload = {
            "summary": summary,
            "recent_items": recent_items,
        }
        return len(json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _collect_role_text(turn: list[dict[str, Any]], role: str) -> str:
        texts = []
        for item in turn:
            if item.get("role") == role:
                text = extract_message_text(item)
                if text:
                    texts.append(text)
        return "\n".join(texts)

    @staticmethod
    def _collect_tool_names(turn: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for item in turn:
            name = item.get("name") if isinstance(item, dict) else None
            item_type = item.get("type") if isinstance(item, dict) else None
            if isinstance(name, str) and isinstance(item_type, str) and "call" in item_type:
                if name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _is_user_message(item: dict[str, Any]) -> bool:
        return item.get("role") == "user"

    @staticmethod
    def _context_message(kind: str, body: str) -> dict[str, Any]:
        return {
            "role": "developer",
            "type": "message",
            "content": f"{_CONTEXT_PREFIX}{kind}\n{body}",
        }

    @staticmethod
    def _keywords(user_message: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9._/-]{4,}", user_message.lower())
        seen: set[str] = set()
        ordered: list[str] = []
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[:8]

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: limit - 3].rstrip() + "..."