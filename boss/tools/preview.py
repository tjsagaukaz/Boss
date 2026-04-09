"""Preview tools — governed tools for starting, capturing, and inspecting previews."""

from __future__ import annotations

from boss.config import settings
from boss.execution import (
    ExecutionType,
    display_value,
    governed_function_tool,
    scope_value,
)


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Start Preview",
    describe_call=lambda params: f'Start preview server for {params.get("project_path", "project")}',
    scope_key=lambda params: scope_value("preview", "start"),
    scope_label=lambda params: display_value(
        params.get("project_path"), fallback="Start preview server"
    ),
)
def start_preview_server(
    project_path: str,
    command: str = "",
    port: int = 0,
) -> str:
    """Start a local dev server or preview process for a project.

    Args:
        project_path: Root directory of the project to preview.
        command: Explicit start command. Auto-detected if empty.
        port: Port hint. Auto-detected from output if 0.
    """
    from boss.preview.server import start_preview

    session = start_preview(
        project_path,
        command=command or None,
        port=port or None,
    )

    if session.status == "failed":
        return f"Preview failed to start: {session.error_message}"

    parts = [f"Preview started (pid={session.pid})"]
    if session.url:
        parts.append(f"URL: {session.url}")
    if session.start_command:
        parts.append(f"Command: {session.start_command}")
    return "\n".join(parts)


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Stop Preview",
    describe_call=lambda params: f'Stop preview for {params.get("project_path", "project")}',
    scope_key=lambda params: scope_value("preview", "stop"),
    scope_label=lambda _params: "Stop preview server",
)
def stop_preview_server(project_path: str) -> str:
    """Stop a running preview server for the given project."""
    from boss.preview.server import stop_preview

    stopped = stop_preview(project_path)
    return "Preview stopped." if stopped else "No active preview found for this project."


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Capture Preview",
    describe_call=lambda params: f'Capture screenshot of {params.get("url", "preview")}',
    scope_key=lambda params: scope_value("preview", "capture"),
    scope_label=lambda params: display_value(
        params.get("url"), fallback="Capture preview screenshot"
    ),
)
def capture_preview(
    url: str = "",
    project_path: str = "",
    detail_mode: str = "auto",
    region_x: int = 0,
    region_y: int = 0,
    region_width: int = 0,
    region_height: int = 0,
) -> str:
    """Capture a screenshot and diagnostic info from a preview URL.

    If no URL is provided, uses the active preview session's URL.
    Returns screenshot path, console errors, network errors, and DOM summary.

    Args:
        url: URL to capture. Uses active preview URL if empty.
        project_path: Project to look up active session for.
        detail_mode: Image detail level — auto, low, high, or original.
        region_x: X offset for region capture (0 = full page).
        region_y: Y offset for region capture (0 = full page).
        region_width: Width of region to capture (0 = full page).
        region_height: Height of region to capture (0 = full page).
    """
    from pathlib import Path
    import time
    from boss.preview.session import CaptureRegion, capture_screenshot, get_active_session

    if not url:
        session = get_active_session(project_path or None)
        if session and session.url:
            url = session.url
        else:
            return "No URL provided and no active preview session found."

    captures_dir = settings.app_data_dir / "preview_captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    output_path = captures_dir / f"capture_{timestamp}.png"

    capture_kwargs: dict = {"detail_mode": detail_mode}
    if region_width > 0 and region_height > 0:
        capture_kwargs["region"] = CaptureRegion(
            x=region_x, y=region_y, width=region_width, height=region_height,
        )

    result = capture_screenshot(url, output_path, **capture_kwargs)

    parts: list[str] = []
    if result.screenshot_path:
        parts.append(f"Screenshot saved: {result.screenshot_path}")
    if result.page_title:
        parts.append(f"Page title: {result.page_title}")
    if result.console_errors:
        parts.append(f"Console errors ({len(result.console_errors)}):")
        for err in result.console_errors[:10]:
            parts.append(f"  - {err}")
    if result.network_errors:
        parts.append(f"Network errors ({len(result.network_errors)}):")
        for err in result.network_errors[:10]:
            parts.append(f"  - {err}")
    if result.dom_summary:
        parts.append(f"DOM text (first 500 chars):\n{result.dom_summary[:500]}")
    if not parts:
        parts.append("Capture completed but no data was collected.")

    # Update session with latest capture
    if project_path:
        session = get_active_session(project_path)
        if session:
            session.last_capture = result

    return "\n".join(parts)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Preview Status",
    describe_call=lambda params: "Check preview status",
    scope_key=lambda _params: scope_value("preview", "status"),
    scope_label=lambda _params: "Preview status check",
)
def preview_status_tool(project_path: str = "") -> str:
    """Check the status of preview sessions and available capabilities.

    Args:
        project_path: Optionally filter to a specific project.
    """
    from boss.preview.server import preview_status

    status = preview_status(project_path or None)
    caps = status["capabilities"]
    sessions = status["sessions"]

    parts: list[str] = []

    # Capabilities
    cap_items = []
    if caps.get("has_browser"):
        cap_items.append(f"browser ({caps.get('browser_path', 'detected')})")
    if caps.get("has_playwright"):
        cap_items.append("playwright (screenshots)")
    if caps.get("has_node"):
        cap_items.append("node.js")
    if caps.get("has_swift_build"):
        cap_items.append("swift")
    parts.append(f"Capabilities: {', '.join(cap_items) if cap_items else 'none detected'}")

    # Sessions
    if sessions:
        parts.append(f"\nActive sessions ({status['active_count']} running):")
        for s in sessions:
            line = f"  [{s['status']}] {s['project_path']}"
            if s.get("url"):
                line += f" → {s['url']}"
            parts.append(line)
    else:
        parts.append("No active preview sessions.")

    return "\n".join(parts)
