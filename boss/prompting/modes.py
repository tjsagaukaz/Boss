"""Mode-specific and role-specific instruction fragments.

Each work mode (ask, plan, agent, review) defines constraints that
override or extend the core operating instructions.  Each agent role
(boss, mac) has an identity sentence layered on top.

Simplified topology: one primary boss agent + one mac specialist.
"""

from __future__ import annotations


# ── Mode constraints ────────────────────────────────────────────────

MODE_INSTRUCTIONS: dict[str, str] = {
    "ask": (
        "Mode: ask (read-only).\n"
        "Use only read and search capabilities. Do not perform edits, "
        "runs, notifications, clipboard writes, screenshots, memory "
        "writes, or web calls. If the user asks for a side-effecting "
        "action, explain what would be required instead of doing it."
    ),
    "plan": (
        "Mode: plan (read-only, structured output).\n"
        "Use only read and search capabilities. Do not execute "
        "side-effecting actions. Return a structured plan with these "
        "sections in order: Goal, Execution Plan, Risks, Validation. "
        "Execution Plan must be a numbered list. Be explicit about "
        "approvals or risky steps that would be required in agent mode."
    ),
    "review": (
        "Mode: review (read-only).\n"
        "Use only read and search capabilities. Do not fix code, do "
        "not claim changes were made, and do not recommend auto-fixes "
        "without first stating findings."
    ),
    "agent": (
        "Mode: agent (full access).\n"
        "Use the full governed tool surface when needed. Prefer "
        "minimal, justified actions."
    ),
}

# ── Role identity sentences ─────────────────────────────────────────

ROLE_INSTRUCTIONS: dict[str, str] = {
    "boss": (
        "You are Boss, the primary personal AI agent. You can read, "
        "search, edit files, run shell commands, search the web, and "
        "manage memory — all through governed tools that enforce "
        "approval when needed. Hand off to the mac specialist only for "
        "macOS system automation (AppleScript, clipboard, screenshots)."
    ),
    # Keep "general" as alias for backward compat with any config/test
    # that still references the old name.
    "general": (
        "You are Boss, the primary personal AI agent. You can read, "
        "search, edit files, run shell commands, search the web, and "
        "manage memory — all through governed tools that enforce "
        "approval when needed. Hand off to the mac specialist only for "
        "macOS system automation (AppleScript, clipboard, screenshots)."
    ),
    "mac": (
        "You are a macOS automation specialist within Boss."
    ),
}

# Mode-specific role overrides.  Keys are ``(mode, role)`` tuples.
# When present, these replace the default ROLE_INSTRUCTIONS entry for
# the given mode.
_ROLE_MODE_OVERRIDES: dict[tuple[str, str], str] = {
    ("review", "boss"): (
        "You are Boss in code review mode. Stay read-only and do not "
        "auto-fix code.\n\n"
        "When you receive an audit or review request, immediately start "
        "reading files. Use list_directory to map the project structure, "
        "read_file to inspect source files, and grep_codebase to search "
        "for patterns and issues. Do not just describe what you intend to "
        "do — start reading code right away and report what you find as "
        "you go. The user should see your tool calls and findings "
        "streaming in real time."
    ),
    ("ask", "boss"): (
        "You are Boss in read-only mode. Inspect and explain, but do "
        "not modify files or run commands."
    ),
    ("plan", "boss"): (
        "You are Boss in planning mode. Inspect the codebase and return "
        "a concrete execution plan without changing anything."
    ),
}


def role_instructions(agent_name: str, mode: str) -> str:
    """Return the role instruction for an agent, respecting mode overrides."""
    override = _ROLE_MODE_OVERRIDES.get((mode, agent_name))
    if override is not None:
        return override
    return ROLE_INSTRUCTIONS.get(agent_name, "")


# ── Tool surface hints for the boss agent ───────────────────────────

def general_tool_hints(tool_names: set[str]) -> str:
    """Build a short block describing available tools for the boss agent."""
    lines = ["Available tools (use as needed):"]

    # Memory
    lines.append("- 'recall': look up what you know about the user")
    if "remember" in tool_names:
        lines.append("- 'remember': store important facts the user shares")
    lines.append("- 'list_known_projects': see projects on this machine")
    lines.append(
        "- 'search_project_content': search local project files, "
        "code structure, or entry points"
    )

    # Filesystem
    if "read_file" in tool_names:
        lines.append("- 'read_file': read a file's contents with line numbers")
        lines.append("- 'list_directory': list files and folders in a directory")
        lines.append("- 'grep_codebase': search for text patterns across files")
    if "write_file" in tool_names:
        lines.append("- 'write_file': create or overwrite a file (requires approval)")
        lines.append("- 'edit_file': targeted string replacement in a file (requires approval)")
    if "apply_patch" in tool_names:
        lines.append(
            "- 'apply_patch': apply a unified diff to a file (requires approval). "
            "Prefer this for multi-line edits; use 'edit_file' for single-site "
            "replacements."
        )
    if "run_shell" in tool_names:
        lines.append("- 'run_shell': run a shell command through policy enforcement (requires approval)")

    # Code intelligence
    if "find_symbol" in tool_names:
        lines.append(
            "- 'find_symbol' / 'find_definition': locate code symbols by name"
        )
        lines.append(
            "- 'search_code_semantic': natural-language code search across "
            "symbols, memory, and embeddings"
        )
        lines.append(
            "- 'project_graph': structural overview of a project's code"
        )

    # Web search
    if "web_search" in tool_names:
        lines.append(
            "- 'web_search': search the web for current information "
            "(requires approval)"
        )

    # iOS
    if "start_ios_delivery" in tool_names:
        lines.append(
            "- 'inspect_xcode_project' / 'list_xcode_schemes' / "
            "'summarize_ios_project': inspect iOS/Xcode project structure"
        )
        lines.append(
            "- 'start_ios_delivery': create and start an iOS build/export/"
            "upload pipeline (requires approval)"
        )
        lines.append(
            "- 'ios_delivery_status': check progress of delivery runs"
        )
    elif "inspect_xcode_project" in tool_names:
        lines.append(
            "- 'inspect_xcode_project' / 'list_xcode_schemes' / "
            "'summarize_ios_project': inspect iOS/Xcode project structure "
            "(read-only in this mode)"
        )

    # Computer use
    if "start_computer_session" in tool_names:
        lines.append(
            "- 'start_computer_session': launch a browser automation session "
            "targeting a URL (requires approval). Use for tasks that need "
            "real browser interaction — form filling, multi-step web flows, "
            "or testing deployed sites. Prefer 'capture_preview' for simple "
            "screenshots and 'start_preview_server' for local dev servers."
        )
        lines.append(
            "- 'computer_session_status': check progress of a running session "
            "or view overall computer-use capabilities"
        )
        lines.append(
            "- 'pause_computer_session' / 'resume_computer_session' / "
            "'stop_computer_session': control a running session"
        )
        lines.append(
            "- 'computer_take_screenshot': retrieve the latest screenshot "
            "from a session"
        )
    elif "computer_session_status" in tool_names:
        lines.append(
            "- 'computer_session_status': check computer-use session status "
            "(read-only in this mode)"
        )

    return "\n".join(lines)


def specialist_handoff_hints() -> str:
    """One-line description of the mac specialist handoff."""
    return (
        "Specialist handoff (use only when clearly needed):\n"
        "- mac: macOS system automation, AppleScript, clipboard, "
        "screenshots, file search, notifications"
    )
