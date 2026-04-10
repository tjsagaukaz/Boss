"""Agent construction — builds the Boss runtime agent graph.

Runtime topology (simplified):
- **boss**: the single primary agent with the full tool surface
  (memory, filesystem, code intelligence, action, web search, iOS, preview).
- **mac**: macOS system automation specialist (AppleScript, clipboard,
  notifications, screenshots, file search).

Review mode uses the same boss agent with read-only tool filtering and
review-specific instructions.  A separate ``build_review_agent`` is
available for structured review workflows that need typed output.

Retired agents (research, reasoning, code) are collapsed into the
primary boss agent.  Their capabilities live as direct tools instead of
conversational handoffs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import Agent, set_tracing_disabled

from boss.config import settings
from boss.control import load_boss_control
from boss.execution import AUTO_ALLOWED_EXECUTION_TYPES, get_tool_metadata
from boss.guardrails.safety import safety_check
from boss.tools.action import apply_patch, edit_file, run_shell, write_file
from boss.tools.filesystem import grep_codebase, list_directory
from boss.tools.filesystem import read_file as fs_read_file
from boss.tools.intelligence import (
    find_definition,
    find_importers,
    find_symbol,
    project_graph,
    search_code_semantic,
    search_code_symbolic,
)
from boss.tools.ios import (
    inspect_xcode_project,
    ios_delivery_status,
    list_xcode_schemes,
    start_ios_delivery,
    summarize_ios_project,
)
from boss.tools.mac import (
    get_clipboard,
    open_app,
    run_applescript,
    screenshot,
    search_files,
    send_notification,
    set_clipboard,
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


# ── Work mode policy ────────────────────────────────────────────────

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
    """Filter tools to those allowed by the current work mode policy."""
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


# ── Primary tool lists ──────────────────────────────────────────────

# The full Boss tool surface — everything the primary agent can use.
_BOSS_TOOLS = [
    # Memory
    remember, recall, list_known_projects, get_project_details,
    search_project_content, memory_stats,
    # Filesystem (read)
    fs_read_file, list_directory, grep_codebase,
    # Filesystem (write) — filtered out in read-only modes
    write_file, edit_file, apply_patch,
    # Shell — filtered out in read-only modes
    run_shell,
    # Code intelligence
    find_symbol, find_definition, search_code_symbolic,
    search_code_semantic, project_graph, find_importers,
    # iOS / Xcode
    inspect_xcode_project, list_xcode_schemes, summarize_ios_project,
    start_ios_delivery, ios_delivery_status,
    # Preview
    start_preview_server, stop_preview_server,
    capture_preview, preview_status_tool,
    # Web search (external) — filtered out when no API key
    web_search,
]

_MAC_TOOLS = [
    open_app, run_applescript, search_files,
    get_clipboard, set_clipboard, send_notification, screenshot,
]


# ── Agent builders ──────────────────────────────────────────────────

def build_entry_agent(
    *,
    active_mcp_servers: dict[str, object] | None = None,
    mode: str | None = None,
    workspace_root: Path | None = None,
) -> Agent:
    """Build the Boss runtime agent graph.

    Returns the primary boss agent with a mac specialist handoff.
    """
    active_mcp_servers = active_mcp_servers or {}
    control = load_boss_control(workspace_root)
    resolved_mode = mode or control.config.default_mode
    policy = _mode_policy(resolved_mode)
    ws = control.root

    # Filter tools for the current mode
    boss_tools_raw = _BOSS_TOOLS if settings.cloud_api_key else [t for t in _BOSS_TOOLS if t is not web_search]
    boss_tools = _filter_tools(boss_tools_raw, policy=policy)
    mac_tools = _filter_tools(_MAC_TOOLS, policy=policy)

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

    # In review mode use the full code model for deeper analysis.
    entry_model = settings.code_model if resolved_mode == "review" else settings.general_model

    return Agent(
        name="boss",
        model=entry_model,
        instructions=_build_instructions(
            agent_name="boss",
            mode=resolved_mode,
            workspace_root=ws,
            tool_names=_tool_names(boss_tools),
        ),
        tools=boss_tools,
        handoffs=[mac_agent],
        input_guardrails=[safety_check],
        mcp_servers=[active_mcp_servers["memory"]] if policy.allow_mcp_servers and "memory" in active_mcp_servers else [],
    )


def build_review_agent(*, output_type: type[Any], workspace_root: Path | None = None) -> Agent:
    """Build a structured review agent with typed output.

    Used by the review workflow endpoint for machine-readable findings.
    """
    control = load_boss_control(workspace_root)
    policy = _mode_policy("review")
    review_tools = _filter_tools(
        [recall, list_known_projects, get_project_details, search_project_content,
         memory_stats, fs_read_file, list_directory, grep_codebase],
        policy=policy,
    )
    instructions = _build_instructions(
        agent_name="boss",
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


# Default entry agent (built at import time for backward compat).
entry_agent = build_entry_agent()
