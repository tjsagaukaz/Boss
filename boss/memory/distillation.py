from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from boss.config import settings
from boss.memory.injection import resolve_project_reference
from boss.memory.knowledge import DurableMemory, ProjectNote, get_knowledge_store
from boss.observability import log_memory_distillation
from boss.persistence.history import extract_message_text


@dataclass
class MemoryCandidate:
    storage: str
    memory_kind: str
    category: str
    key: str
    value: str
    project_path: str | None = None
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.75
    salience: float = 0.7
    title: str | None = None
    source: str = "auto_distillation"


_IDENTITY_PATTERNS = [
    (re.compile(r"\bmy name is (?P<value>[^.!?\n]{1,60})", re.IGNORECASE), "name", 0.96, 0.95),
    (re.compile(r"\bcall me (?P<value>[^.!?\n]{1,60})", re.IGNORECASE), "name", 0.96, 0.95),
    (re.compile(r"\bmy pronouns are (?P<value>[^.!?\n]{1,40})", re.IGNORECASE), "pronouns", 0.92, 0.85),
    (re.compile(r"\bmy timezone is (?P<value>[^.!?\n]{1,40})", re.IGNORECASE), "timezone", 0.9, 0.75),
]
_PREFERENCE_PATTERNS = [
    (re.compile(r"\b(?:i|we)\s+(?:prefer|like|love|enjoy)\s+(?P<value>[^.!?\n]+)", re.IGNORECASE), "positive"),
    (re.compile(r"\b(?:i|we)\s+(?:dislike|hate)\s+(?P<value>[^.!?\n]+)", re.IGNORECASE), "negative"),
    (re.compile(r"\b(?:i|we)\s+do not like\s+(?P<value>[^.!?\n]+)", re.IGNORECASE), "negative"),
    (re.compile(r"\b(?:i|we)\s+don't like\s+(?P<value>[^.!?\n]+)", re.IGNORECASE), "negative"),
]
_GOAL_PATTERNS = [
    re.compile(r"\b(?:i am|i'm|im)\s+(?:working on|building|trying to)\s+(?P<value>[^.!?\n]+)", re.IGNORECASE),
    re.compile(r"\bi need to\s+(?P<value>[^.!?\n]+)", re.IGNORECASE),
    re.compile(r"\b(?:my|the)\s+goal is to\s+(?P<value>[^.!?\n]+)", re.IGNORECASE),
]
_WORKFLOW_PATTERNS = [
    re.compile(r"\b(?:i|we)\s+(?:usually|always|typically)\s+(?P<value>[^.!?\n]+)", re.IGNORECASE),
    re.compile(r"\bmy workflow is\s+(?P<value>[^.!?\n]+)", re.IGNORECASE),
    re.compile(r"\b(?:i|we)\s+tend to\s+(?P<value>[^.!?\n]+)", re.IGNORECASE),
]
_PROJECT_CONSTRAINT_HINTS = {
    "must",
    "should",
    "need to",
    "keep",
    "preserve",
    "avoid",
    "do not",
    "don't",
    "cannot",
    "can't",
    "backward compatible",
    "sse",
    "api contract",
}
_DURABLE_FACT_FIELDS = {
    "editor",
    "shell",
    "os",
    "operating system",
    "machine",
    "laptop",
    "email",
    "github",
}


def distill_latest_turn(*, session_id: str, session_summary: str, recent_items: list[dict]) -> list[MemoryCandidate]:
    if not settings.auto_memory_enabled:
        return []

    user_text, assistant_text = _latest_turn_text(recent_items)
    if not user_text:
        return []

    store = get_knowledge_store()
    project_path = resolve_project_reference(user_message=user_text, session_summary=session_summary)
    candidates: list[MemoryCandidate] = []

    for sentence in _split_sentences(user_text):
        if _skip_sentence(sentence):
            continue
        candidates.extend(_extract_identity(sentence))
        candidates.extend(_extract_preferences(sentence))
        candidates.extend(_extract_goals(sentence, project_path=project_path))
        candidates.extend(_extract_workflows(sentence, project_path=project_path))
        candidates.extend(_extract_project_constraints(sentence, project_path=project_path))
        candidates.extend(_extract_durable_facts(sentence))

    if assistant_text and project_path:
        for sentence in _split_sentences(assistant_text):
            if _skip_sentence(sentence):
                continue
            candidates.extend(_extract_project_constraints(sentence, project_path=project_path, source="assistant_confirmation"))

    candidates = _dedupe_candidates(candidates)
    persisted: list[MemoryCandidate] = []
    for candidate in candidates[: settings.auto_memory_distillation_limit]:
        if candidate.storage == "project_note":
            _persist_project_candidate(store, candidate)
        else:
            _persist_durable_candidate(store, candidate)
        log_memory_distillation(
            source=candidate.source,
            category=candidate.category,
            key=candidate.key,
            value_length=len(candidate.value),
        )
        persisted.append(candidate)

    return persisted


def _extract_identity(sentence: str) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for pattern, key, confidence, salience in _IDENTITY_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        value = _clean_value(match.group("value"))
        if not value:
            continue
        candidates.append(
            MemoryCandidate(
                storage="durable_memory",
                memory_kind="user_profile",
                category="user",
                key=key,
                value=value,
                tags=["identity", key],
                confidence=confidence,
                salience=salience,
            )
        )
    return candidates


def _extract_preferences(sentence: str) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for pattern, polarity in _PREFERENCE_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        value = _clean_value(match.group("value"))
        if not value or _looks_ephemeral(value):
            continue
        key, tags = _preference_key_and_tags(value)
        normalized_value = value if polarity == "positive" else f"avoid {value}"
        candidates.append(
            MemoryCandidate(
                storage="durable_memory",
                memory_kind="preference",
                category="preference",
                key=key,
                value=normalized_value,
                tags=tags + ["preference", polarity],
                confidence=0.84,
                salience=0.9,
            )
        )
    return candidates


def _extract_goals(sentence: str, *, project_path: str | None) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for pattern in _GOAL_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        value = _clean_value(match.group("value"))
        if not value or _looks_ephemeral(value):
            continue
        key = _semantic_key(value, prefix="goal")
        candidates.append(
            MemoryCandidate(
                storage="durable_memory",
                memory_kind="ongoing_goal",
                category="ongoing_goal",
                key=key,
                value=value,
                project_path=project_path,
                tags=["goal", *( [Path(project_path).name.lower()] if project_path else [] )],
                confidence=0.78,
                salience=0.8,
            )
        )
    return candidates


def _extract_workflows(sentence: str, *, project_path: str | None) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for pattern in _WORKFLOW_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        value = _clean_value(match.group("value"))
        if not value or _looks_ephemeral(value):
            continue
        key = _semantic_key(value, prefix="workflow")
        candidates.append(
            MemoryCandidate(
                storage="durable_memory",
                memory_kind="workflow",
                category="workflow",
                key=key,
                value=value,
                project_path=project_path,
                tags=["workflow", *( [Path(project_path).name.lower()] if project_path else [] )],
                confidence=0.8,
                salience=0.78,
            )
        )
    return candidates


def _extract_project_constraints(
    sentence: str,
    *,
    project_path: str | None,
    source: str = "auto_distillation",
) -> list[MemoryCandidate]:
    if not project_path:
        return []
    lower = sentence.lower()
    if not any(hint in lower for hint in _PROJECT_CONSTRAINT_HINTS):
        return []
    value = _clean_value(sentence)
    if not value or _looks_ephemeral(value):
        return []
    return [
        MemoryCandidate(
            storage="project_note",
            memory_kind="project_constraint",
            category="project_constraint",
            key=_semantic_key(value, prefix="constraint"),
            title=f"Constraint for {Path(project_path).name}",
            value=value,
            project_path=project_path,
            tags=["project_constraint", Path(project_path).name.lower()],
            confidence=0.82,
            salience=0.85,
            source=source,
        )
    ]


def _extract_durable_facts(sentence: str) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    match = re.search(r"\bmy (?P<field>[a-z][a-z0-9 _-]{2,30}) is (?P<value>[^.!?\n]+)", sentence, re.IGNORECASE)
    if not match:
        return candidates

    field = _clean_value(match.group("field")).lower()
    value = _clean_value(match.group("value"))
    if field in {"name", "pronouns", "goal"} or field not in _DURABLE_FACT_FIELDS:
        return candidates
    if not value or _looks_ephemeral(value):
        return candidates

    candidates.append(
        MemoryCandidate(
            storage="durable_memory",
            memory_kind="durable_memory",
            category="durable_fact",
            key=field.replace(" ", "_"),
            value=value,
            tags=["durable_fact", field.replace(" ", "_")],
            confidence=0.82,
            salience=0.72,
        )
    )
    return candidates


def _persist_durable_candidate(store, candidate: MemoryCandidate) -> None:
    existing = _find_matching_durable_memory(store.list_durable_memories(
        memory_kind=candidate.memory_kind,
        project_path=candidate.project_path,
        limit=50,
    ), candidate)
    key = candidate.key
    value = candidate.value
    confidence = candidate.confidence
    salience = candidate.salience
    tags = candidate.tags

    if existing is not None:
        key = existing.key
        value = _merge_value(existing.value, candidate.value)
        confidence = min(1.0, max(existing.confidence, candidate.confidence) + 0.08)
        salience = min(1.0, max(existing.salience, candidate.salience) + 0.04)
        tags = _merge_tags(existing.tags, candidate.tags)

    store.upsert_durable_memory(
        memory_kind=candidate.memory_kind,
        category=candidate.category,
        key=key,
        value=value,
        tags=tags,
        confidence=confidence,
        salience=salience,
        source=candidate.source,
        project_path=candidate.project_path,
    )


def _persist_project_candidate(store, candidate: MemoryCandidate) -> None:
    if not candidate.project_path:
        return
    existing = _find_matching_project_note(store.list_project_notes(candidate.project_path, limit=50), candidate)
    note_key = existing.note_key if existing is not None else candidate.key
    body = _merge_value(existing.body, candidate.value) if existing is not None else candidate.value
    confidence = min(1.0, max(existing.confidence, candidate.confidence) + 0.08) if existing is not None else candidate.confidence
    salience = min(1.0, max(existing.salience, candidate.salience) + 0.04) if existing is not None else candidate.salience
    tags = _merge_tags(existing.tags, candidate.tags) if existing is not None else candidate.tags

    store.upsert_project_note(
        project_path=candidate.project_path,
        memory_kind=candidate.memory_kind,
        note_key=note_key,
        title=candidate.title or f"Note for {Path(candidate.project_path).name}",
        body=body,
        category=candidate.category,
        tags=tags,
        confidence=confidence,
        salience=salience,
        source=candidate.source,
    )


def _find_matching_durable_memory(memories: Iterable[DurableMemory], candidate: MemoryCandidate) -> DurableMemory | None:
    best: DurableMemory | None = None
    best_score = 0.0
    for memory in memories:
        score = 0.0
        if memory.key == candidate.key:
            score += 1.0
        score += _similarity(memory.value, candidate.value)
        if memory.category == candidate.category:
            score += 0.15
        if score > best_score:
            best_score = score
            best = memory
    return best if best_score >= 0.9 else None


def _find_matching_project_note(notes: Iterable[ProjectNote], candidate: MemoryCandidate) -> ProjectNote | None:
    best: ProjectNote | None = None
    best_score = 0.0
    for note in notes:
        score = 0.0
        if note.note_key == candidate.key:
            score += 1.0
        score += _similarity(note.body, candidate.value)
        if note.category == candidate.category:
            score += 0.15
        if score > best_score:
            best_score = score
            best = note
    return best if best_score >= 0.9 else None


def _latest_turn_text(items: list[dict]) -> tuple[str, str]:
    current: list[dict] = []
    turns: list[list[dict]] = []
    for item in items:
        if isinstance(item, dict) and item.get("role") == "user":
            if current:
                turns.append(current)
            current = [item]
        elif current:
            current.append(item)
    if current:
        turns.append(current)
    if not turns:
        return "", ""
    last_turn = turns[-1]
    user_parts = []
    assistant_parts = []
    for item in last_turn:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            text = extract_message_text(item)
            if text:
                user_parts.append(text)
        elif item.get("role") == "assistant":
            text = extract_message_text(item)
            if text:
                assistant_parts.append(text)
    return "\n".join(user_parts).strip(), "\n".join(assistant_parts).strip()


def _split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [piece.strip() for piece in pieces if piece.strip()]


def _skip_sentence(sentence: str) -> bool:
    lowered = sentence.strip().lower()
    if not lowered:
        return True
    if lowered.endswith("?"):
        return True
    if lowered.startswith(("can you", "could you", "would you", "please ", "pls ")):
        return True
    return len(lowered.split()) < 3


def _looks_ephemeral(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if len(lowered) < 4:
        return True
    if lowered in {"this", "that", "it", "things", "stuff"}:
        return True
    return False


def _clean_value(value: str) -> str:
    cleaned = value.strip().strip('"').strip("'")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(".,!?:;")


def _semantic_key(value: str, *, prefix: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    core = "_".join(tokens[:6]) if tokens else prefix
    return f"{prefix}_{core}"[:72]


def _preference_key_and_tags(value: str) -> tuple[str, list[str]]:
    lowered = value.lower()
    if any(token in lowered for token in {"dark mode", "light mode", "theme"}):
        return "theme", ["theme"]
    if any(token in lowered for token in {"reply", "response", "responses", "answer", "preamble"}):
        return "response_style", ["response_style"]
    if "markdown" in lowered:
        return "markdown_style", ["markdown"]
    if "tabs" in lowered or "spaces" in lowered:
        return "indentation_style", ["indentation"]
    if "swift" in lowered or "swiftui" in lowered:
        return "preferred_stack", ["stack"]
    return _semantic_key(value, prefix="preference"), re.findall(r"[a-z0-9]+", lowered)[:4]


def _similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9]+", left.lower()))
    right_tokens = set(re.findall(r"[a-z0-9]+", right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union


def _merge_value(existing: str, candidate: str) -> str:
    existing_lower = existing.lower().strip()
    candidate_lower = candidate.lower().strip()
    if candidate_lower == existing_lower or candidate_lower in existing_lower:
        return existing
    if existing_lower in candidate_lower:
        return candidate
    return candidate if len(candidate) >= len(existing) else existing


def _merge_tags(existing: Iterable[str], candidate: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for tag in [*existing, *candidate]:
        text = str(tag).strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def _dedupe_candidates(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    deduped: dict[tuple[str, str | None, str], MemoryCandidate] = {}
    for candidate in candidates:
        key = (candidate.storage, candidate.project_path, candidate.key)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = candidate
            continue
        existing.value = _merge_value(existing.value, candidate.value)
        existing.confidence = max(existing.confidence, candidate.confidence)
        existing.salience = max(existing.salience, candidate.salience)
        existing.tags = _merge_tags(existing.tags, candidate.tags)
    return list(deduped.values())
