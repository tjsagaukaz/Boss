from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from boss.config import settings
from boss.memory.knowledge import MemorySearchResult, get_knowledge_store


@dataclass
class MemoryInjection:
    text: str
    results: list[MemorySearchResult]
    project_path: str | None
    query: str


def build_memory_injection(
    *,
    user_message: str,
    session_summary: str = "",
    referenced_project_path: str | None = None,
) -> MemoryInjection:
    if not settings.auto_memory_enabled:
        return MemoryInjection(text="", results=[], project_path=referenced_project_path, query=user_message)

    store = get_knowledge_store()
    project_path = referenced_project_path or resolve_project_reference(
        user_message=user_message,
        session_summary=session_summary,
    )
    query = _combined_query(user_message, session_summary, project_path)
    limit = settings.auto_memory_injection_limit
    kinds = {
        "user_profile",
        "preference",
        "ongoing_goal",
        "workflow",
        "project_note",
        "project_constraint",
        "durable_memory",
        "session_summary",
    }

    results = store.search_memories(query, limit=limit, project_path=project_path, kinds=kinds)
    if project_path and not results:
        results = store.search_memories(query, limit=limit, kinds=kinds)

    text = _format_injection(results, project_path=project_path)
    return MemoryInjection(text=text, results=results, project_path=project_path, query=query)


def resolve_project_reference(*, user_message: str, session_summary: str = "") -> str | None:
    store = get_knowledge_store()
    haystack = f"{user_message}\n{session_summary}".lower()

    path_matches = re.findall(r"(?:/Users/[^\s,:;]+|~/[^\s,:;]+|\./[^\s,:;]+|\.\./[^\s,:;]+)", haystack)
    projects = store.list_projects()
    for raw_path in path_matches:
        normalized = str(Path(raw_path).expanduser())
        exact = next((project for project in projects if project.path.lower() == normalized.lower()), None)
        if exact is not None:
            return exact.path
        ancestor = next((project for project in projects if normalized.lower().startswith(project.path.lower() + "/")), None)
        if ancestor is not None:
            return ancestor.path

    for project in projects:
        name = project.name.strip().lower()
        if not name:
            continue
        if re.search(rf"\b{re.escape(name)}\b", haystack):
            return project.path

    return None


def _combined_query(user_message: str, session_summary: str, project_path: str | None) -> str:
    parts = [user_message.strip()]
    if session_summary.strip():
        parts.append(session_summary.strip()[-500:])
    if project_path:
        parts.append(project_path)
    return "\n".join(part for part in parts if part)


def _format_injection(results: list[MemorySearchResult], *, project_path: str | None) -> str:
    if not results:
        return ""

    labels = {
        "user_profile": "Profile",
        "preference": "Preferences",
        "ongoing_goal": "Goals",
        "workflow": "Workflows",
        "project_note": "Project Notes",
        "project_constraint": "Project Constraints",
        "project_profile": "Project Notes",
        "durable_memory": "Durable Facts",
        "session_summary": "Past Session Summaries",
    }
    group_order = [
        "user_profile",
        "preference",
        "ongoing_goal",
        "workflow",
        "project_constraint",
        "project_profile",
        "project_note",
        "durable_memory",
        "session_summary",
    ]

    grouped: dict[str, list[MemorySearchResult]] = {}
    for result in results:
        group_key = result.category if result.category in {"project_constraint", "project_profile"} else result.memory_kind
        grouped.setdefault(group_key, []).append(result)

    sections: list[str] = []
    if project_path:
        sections.append(f"Referenced project: {project_path}")

    for kind in group_order:
        items = grouped.get(kind)
        if not items:
            continue
        lines = []
        for result in items[:2]:
            lines.append(_format_result_line(result))
        label = labels.get(kind, kind.replace("_", " ").title())
        sections.append(f"{label}:\n" + "\n".join(lines))

    return "\n\n".join(sections)


def _format_result_line(result: MemorySearchResult) -> str:
    key = result.key.replace("_", " ")
    if result.memory_kind in {"project_note", "project_constraint", "session_summary"}:
        return f"- {key}: {_clip(result.text, 180)}"
    return f"- {key}: {_clip(result.text, 120)}"


def _clip(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."
