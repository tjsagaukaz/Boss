from __future__ import annotations

from agents import Agent, set_tracing_disabled

from boss.config import settings
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
from boss.tools.memory import (
    get_project_details,
    list_known_projects,
    memory_stats,
    recall,
    remember,
    search_project_content,
)
from boss.tools.research import web_search

set_tracing_disabled(not settings.tracing_enabled)


def build_entry_agent(*, active_mcp_servers: dict[str, object] | None = None) -> Agent:
    active_mcp_servers = active_mcp_servers or {}

    mac_agent = Agent(
        name="mac",
        model=settings.mac_model,
        instructions=(
            "You are a macOS automation specialist. Use tools and MCP servers when needed."
        ),
        tools=[
            open_app,
            run_applescript,
            search_files,
            get_clipboard,
            set_clipboard,
            send_notification,
            screenshot,
        ],
        mcp_servers=[
            server
            for name, server in active_mcp_servers.items()
            if name in {"apple", "filesystem"}
        ],
    )

    research_agent = Agent(
        name="research",
        model=settings.research_model,
        instructions=(
            "You are a research specialist. Use web search only when current or external information is genuinely required."
        ),
        tools=[web_search] if settings.cloud_api_key else [],
    )

    reasoning_agent = Agent(
        name="reasoning",
        model=settings.reasoning_model,
        instructions="You are an expert analyst. Break down complex problems step by step.",
    )

    code_agent = Agent(
        name="code",
        model=settings.code_model,
        instructions=(
            "You are an expert programmer. Write clean, correct code. "
            "Use 'list_known_projects', 'get_project_details', and 'search_project_content' to understand the user's projects."
        ),
        tools=[recall, list_known_projects, get_project_details, search_project_content],
    )

    # General is the actual entry point. It answers directly when it can
    # and hands off to specialists only when a narrower toolset is useful.
    return Agent(
        name="general",
        model=settings.general_model,
        instructions=(
            "You are Boss, a helpful personal AI assistant. Answer clearly and concisely.\n"
            "You have access to a persistent memory system:\n"
            "- Use 'recall' to look up what you know about the user\n"
            "- Use 'remember' to store important facts the user shares\n"
            "- Use 'list_known_projects' to see projects on this machine\n"
            "- Use 'search_project_content' when the user asks about local project internals, code structure, or entry points\n\n"
            "Execution policy:\n"
            "- Prefer read and search tools before any modifying action\n"
            "- Use edit, run, or external tools only when necessary\n"
            "- State the intent clearly before restricted actions\n"
            "- Avoid chaining multiple restricted actions when one will do\n\n"
            "For MOST requests, answer directly. Hand off ONLY when a specialist is clearly needed:\n"
            "- mac: macOS automation, opening apps, clipboard, files, notifications\n"
            "- research: web searches, current events, real-time information\n"
            "- reasoning: complex multi-step analysis requiring deep thought\n"
            "- code: software engineering, debugging, code generation\n"
            "Be concise. Be direct."
        ),
        tools=[remember, recall, list_known_projects, get_project_details, search_project_content, memory_stats],
        handoffs=[mac_agent, research_agent, reasoning_agent, code_agent],
        input_guardrails=[safety_check],
        mcp_servers=[active_mcp_servers["memory"]] if "memory" in active_mcp_servers else [],
    )


entry_agent = build_entry_agent()