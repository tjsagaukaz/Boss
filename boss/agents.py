from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import Agent, set_tracing_disabled

from boss.config import settings
from boss.control import load_boss_control
from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, get_tool_metadata
from boss.guardrails.safety import safety_check
from boss.tools.mac import (
    get_clipboard,
    open_app,
    run_applescript,
    screenshot,
    search_files,
    send_notification,
    set_clipboard,
)
from boss.tools.intelligence import (
    find_definition,
    find_importers,
    find_symbol,
    project_graph,
    search_code_semantic,
    search_code_symbolic,
)
from boss.tools.memory import (
    get_project_details,
    list_known_projects,
    memory_stats,
    recall,
    remember,
    search_project_content,
)
from boss.tools.preview import (
    capture_preview,
    preview_status_tool,
    start_preview_server,
    stop_preview_server,
)
from boss.tools.research import web_search

set_tracing_disabled(not settings.tracing_enabled)


@dataclass(frozen=True)
class WorkModePolicy:
    name: str
    allow_restricted_tools: bool = False
    allow_external_tools: bool = False
    allow_mcp_servers: bool = False


def _mode_policy(mode: str) -> WorkModePolicy:
    if mode in ("ask", "plan", "review"):
        return WorkModePolicy(name=mode)
    return WorkModePolicy(
        name="agent",
        allow_restricted_tools=True,
        allow_external_tools=True,
        allow_mcp_servers=True,
    )


def _filter_tools(tools: list[object], *, policy: WorkModePolicy) -> list[object]:
    if policy.allow_restricted_tools and policy.allow_external_tools:
        return tools

    filtered: list[object] = []
    for tool in tools:
        name = getattr(tool, "name", "")
        metadata = get_tool_metadata(name) if name else None
        if metadata is None:
            continue
        if metadata.execution_type in AUTO_ALLOWED_EXECUTION_TYPES:
            filtered.append(tool)
            continue
        if policy.allow_restricted_tools and metadata.execution_type.value in {"edit", "run"}:
            filtered.append(tool)
            continue
        if policy.allow_external_tools and metadata.execution_type.value == "external":
            filtered.append(tool)
    return filtered


def _tool_names(tools: list[object]) -> set[str]:
    return {getattr(tool, "name", "") for tool in tools if getattr(tool, "name", "")}


def _build_instructions(
    *,
    agent_name: str,
    mode: str,
    workspace_root: Path | None = None,
    tool_names: set[str] | None = None,
    task_hint: str | None = None,
) -> str:
    """Build layered instructions for an agent using the prompt builder."""
    from boss.prompting.builder import PromptBuilder

    return (
        PromptBuilder(mode=mode, agent_name=agent_name)
        .with_workspace(workspace_root)
        .with_tool_names(tool_names or set())
        .with_task_hint(task_hint)
        .build()
        .text
    )


def build_entry_agent(
    *,
    active_mcp_servers: dict[str, object] | None = None,
    mode: str | None = None,
    workspace_root: Path | None = None,
) -> Agent:
    active_mcp_servers = active_mcp_servers or {}
    control = load_boss_control(workspace_root)
    resolved_mode = mode or control.config.default_mode
    policy = _mode_policy(resolved_mode)

    mac_tools = _filter_tools(
        [open_app, run_applescript, search_files, get_clipboard, set_clipboard, send_notification, screenshot],
        policy=policy,
    )
    research_tools = _filter_tools([web_search], policy=policy) if settings.cloud_api_key else []
    general_tools = _filter_tools(
        [remember, recall, list_known_projects, get_project_details, search_project_content, memory_stats,
         find_symbol, find_definition, search_code_symbolic, search_code_semantic, project_graph, find_importers,
         start_preview_server, stop_preview_server, capture_preview, preview_status_tool],
        policy=policy,
    )
    code_tools = _filter_tools(
        [recall, list_known_projects, get_project_details, search_project_content,
         find_symbol, find_definition, search_code_symbolic, search_code_semantic, project_graph, find_importers,
         start_preview_server, stop_preview_server, capture_preview, preview_status_tool],
        policy=policy,
    )

    ws = control.root

    mac_agent = Agent(
        name="mac",
        model=settings.mac_model,
        instructions=_build_instructions(
            agent_name="mac", mode=resolved_mode, workspace_root=ws,
        ),
        tools=mac_tools,
        mcp_servers=[
            server
            for name, server in active_mcp_servers.items()
            if name in {"apple", "filesystem"}
        ] if policy.allow_mcp_servers else [],
    )

    research_agent = Agent(
        name="research",
        model=settings.research_model,
        instructions=_build_instructions(
            agent_name="research", mode=resolved_mode, workspace_root=ws,
        ),
        tools=research_tools,
    )

    reasoning_agent = Agent(
        name="reasoning",
        model=settings.reasoning_model,
        instructions=_build_instructions(
            agent_name="reasoning", mode=resolved_mode, workspace_root=ws,
        ),
    )

    code_agent = Agent(
        name="code",
        model=settings.code_model,
        instructions=_build_instructions(
            agent_name="code", mode=resolved_mode, workspace_root=ws,
        ),
        tools=code_tools,
    )

    # General is the actual entry point. It answers directly when it can
    # and hands off to specialists only when a narrower toolset is useful.
    return Agent(
        name="general",
        model=settings.general_model,
        instructions=_build_instructions(
            agent_name="general",
            mode=resolved_mode,
            workspace_root=ws,
            tool_names=_tool_names(general_tools),
        ),
        tools=general_tools,
        handoffs=[mac_agent, research_agent, reasoning_agent, code_agent],
        input_guardrails=[safety_check],
        mcp_servers=[active_mcp_servers["memory"]] if policy.allow_mcp_servers and "memory" in active_mcp_servers else [],
    )


def build_review_agent(*, output_type: type[Any], workspace_root: Path | None = None) -> Agent:
    control = load_boss_control(workspace_root)
    policy = _mode_policy("review")
    review_tools = _filter_tools(
        [recall, list_known_projects, get_project_details, search_project_content, memory_stats],
        policy=policy,
    )
    instructions = _build_instructions(
        agent_name="code",
        mode="review",
        workspace_root=control.root,
    )
    return Agent(
        name="review_workflow",
        model=settings.code_model,
        instructions=instructions,
        tools=review_tools,
        output_type=output_type,
    )


entry_agent = build_entry_agent()