"""Memory tools — let agents remember facts and recall knowledge."""

from __future__ import annotations

from boss.execution import ExecutionType, display_value, governed_function_tool, scope_value
from boss.memory.knowledge import get_knowledge_store
from boss.observability import log_memory_distillation, log_memory_injection


def _format_metadata_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _clip_text(value: str, limit: int = 280) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _resolve_project(store, project_hint: str | None):
    if not project_hint:
        projects = store.list_projects()
        return projects[0] if len(projects) == 1 else None

    exact = store.get_project(project_hint)
    if exact is not None:
        return exact

    normalized = project_hint.strip().lower()
    projects = store.list_projects()
    for project in projects:
        if project.name.lower() == normalized:
            return project

    matches = [
        project
        for project in projects
        if normalized in project.name.lower() or normalized in project.path.lower()
    ]
    return matches[0] if len(matches) == 1 else None


def _project_summary_lines(project) -> list[str]:
    lines = [
        f"Project: {project.name}",
        f"Path: {project.path}",
        f"Type: {project.project_type}",
    ]
    stack = project.metadata.get("stack") if project.metadata else None
    if isinstance(stack, list) and stack:
        lines.append(f"Stack: {', '.join(str(item) for item in stack[:8])}")
    entry_points = project.metadata.get("entry_points") if project.metadata else None
    if isinstance(entry_points, list) and entry_points:
        lines.append(f"Likely entry points: {', '.join(str(item) for item in entry_points[:6])}")
    useful_commands = project.metadata.get("useful_commands") if project.metadata else None
    if isinstance(useful_commands, list) and useful_commands:
        lines.append(f"Useful commands: {', '.join(str(item) for item in useful_commands[:4])}")
    return lines


@governed_function_tool(
    execution_type=ExecutionType.EDIT,
    title="Store Memory",
    describe_call=lambda params: f'Remember {params.get("key", "value")} in {params.get("category", "memory")}',
    scope_key=lambda params: scope_value(
        "memory", f'{params.get("category", "")}-{params.get("key", "")}'
    ),
    scope_label=lambda params: f'{display_value(params.get("category"), fallback="memory")} / {display_value(params.get("key"), fallback="value")}',
)
def remember(category: str, key: str, value: str) -> str:
    """Remember a fact about the user or their world.

    Categories: 'user' (personal info), 'preference' (likes/dislikes),
    'project' (project notes), 'learning' (things learned in conversation).
    """
    store = get_knowledge_store()
    store.store_fact(category, key, value, source="agent")
    log_memory_distillation(
        source="remember_tool",
        category=category,
        key=key,
        value_length=len(value),
    )
    return f"Remembered: [{category}] {key} = {value}"


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Search Memory",
    describe_call=lambda params: f'Search memory for "{params.get("query", "")}"',
    scope_key=lambda _params: scope_value("memory", "read"),
    scope_label=lambda _params: "Memory read",
)
def recall(query: str) -> str:
    """Search memory for facts matching a query. Use this to personalize responses."""
    store = get_knowledge_store()
    facts = store.search_facts(query)
    log_memory_injection(
        source="recall_tool",
        category="facts",
        result_count=len(facts),
        query=query,
    )
    if not facts:
        return "No relevant memories found."
    lines = [f"- [{f.category}] {f.key}: {f.value}" for f in facts]
    return "\n".join(lines)


@governed_function_tool(
    execution_type=ExecutionType.SEARCH,
    title="List Known Projects",
    describe_call=lambda _params: "List known projects",
    scope_key=lambda _params: scope_value("projects", "known"),
    scope_label=lambda _params: "Known projects",
)
def list_known_projects() -> str:
    """List all indexed projects on this machine."""
    store = get_knowledge_store()
    projects = store.list_projects()
    log_memory_injection(
        source="list_known_projects_tool",
        category="projects",
        result_count=len(projects),
    )
    if not projects:
        return "No projects indexed yet. The system scanner may not have run."
    lines = []
    for p in projects:
        remote = f" ({p.git_remote})" if p.git_remote else ""
        branch = f" [{p.git_branch}]" if p.git_branch else ""
        lines.append(f"- {p.name} ({p.project_type}) at {p.path}{branch}{remote}")
    return "\n".join(lines)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Read Project Details",
    describe_call=lambda params: f'Read project details for {params.get("project_path", "the project")}',
    scope_key=lambda params: scope_value("project", params.get("project_path", "unknown")),
    scope_label=lambda params: display_value(params.get("project_path"), fallback="Project"),
)
def get_project_details(project_path: str) -> str:
    """Get detailed info about a specific project by path."""
    store = get_knowledge_store()
    project = store.get_project(project_path)
    log_memory_injection(
        source="get_project_details_tool",
        category="project",
        result_count=1 if project else 0,
        project_path=project_path,
    )
    if not project:
        return f"No project found at {project_path}"
    lines = [
        f"Name: {project.name}",
        f"Type: {project.project_type}",
        f"Path: {project.path}",
    ]
    if project.git_remote:
        lines.append(f"Remote: {project.git_remote}")
    if project.git_branch:
        lines.append(f"Branch: {project.git_branch}")
    if project.metadata:
        for k, v in project.metadata.items():
            lines.append(f"{k}: {_format_metadata_value(v)}")
    return "\n".join(lines)


@governed_function_tool(
    execution_type=ExecutionType.SEARCH,
    title="Search Project Content",
    describe_call=lambda params: f'Search indexed project content for "{params.get("query", "")}"',
    scope_key=lambda params: scope_value("project-search", params.get("project_hint", "all-projects")),
    scope_label=lambda params: display_value(params.get("project_hint"), fallback="All indexed projects"),
)
def search_project_content(query: str, project_hint: str | None = None, limit: int = 6) -> str:
    """Search indexed project summaries and file snippets for questions about project internals."""
    store = get_knowledge_store()
    project = _resolve_project(store, project_hint)
    if project_hint and project is None:
        return f"No indexed project matched '{project_hint}'. Use list_known_projects to inspect available projects."

    limit = max(1, min(limit, 8))
    project_path = project.path if project else None
    note_hits = store.search_memories(
        query,
        limit=limit,
        project_path=project_path,
        kinds={"project_note", "project_constraint"},
    )
    chunk_hits = store.search_file_chunks(query, limit=limit, project_path=project_path)
    total_hits = len(note_hits) + len(chunk_hits)
    log_memory_injection(
        source="search_project_content_tool",
        category="project_search",
        result_count=total_hits,
        query=query,
        project_path=project_path,
    )

    lines: list[str] = []
    if project is not None:
        lines.extend(_project_summary_lines(project))

    if total_hits == 0:
        lines.append(f"No indexed matches found for '{query}'.")
        if project is None:
            lines.append("Run a system scan if the project has not been indexed yet.")
        return "\n".join(lines)

    if note_hits:
        lines.append("Summary hits:")
        for hit in note_hits[: max(1, min(3, limit))]:
            project_suffix = ""
            if project is None and hit.project_path:
                project_suffix = f" ({hit.project_path})"
            lines.append(f"- {hit.key}{project_suffix}: {_clip_text(hit.text)}")

    if chunk_hits:
        lines.append("Snippet hits:")
        for chunk in chunk_hits[:limit]:
            location = f"{chunk.file_path}:{chunk.line_start}-{chunk.line_end}"
            lines.append(f"- {location}: {_clip_text(chunk.content)}")

    return "\n".join(lines)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Read Memory Stats",
    describe_call=lambda _params: "Read memory system stats",
    scope_key=lambda _params: scope_value("memory", "stats"),
    scope_label=lambda _params: "Memory stats",
)
def memory_stats() -> str:
    """Show memory system statistics."""
    store = get_knowledge_store()
    stats = store.stats()
    log_memory_injection(
        source="memory_stats_tool",
        category="memory_stats",
        result_count=stats["facts"],
    )
    lines = [
        f"Facts stored: {stats['facts']}",
        f"Projects indexed: {stats['projects']}",
        f"Files indexed: {stats['files_indexed']}",
    ]
    if stats.get("last_project_scan_at"):
        lines.append(f"Last project scan: {stats['last_project_scan_at']}")
    if stats["fact_categories"]:
        cats = ", ".join(f"{k}: {v}" for k, v in stats["fact_categories"].items())
        lines.append(f"Fact categories: {cats}")
    if stats.get("memory_types"):
        memory_types = ", ".join(f"{k}: {v}" for k, v in stats["memory_types"].items())
        lines.append(f"Memory types: {memory_types}")
    return "\n".join(lines)
