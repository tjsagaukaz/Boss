from __future__ import annotations

import subprocess
from pathlib import Path

from boss.execution import (
    ExecutionType,
    applescript_scope_key,
    applescript_scope_label,
    display_value,
    governed_function_tool,
    hashed_scope,
    scope_value,
)


def _run_command(command: list[str], *, input_text: str | None = None, timeout: int = 10) -> str:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"Command failed: {' '.join(command)}")
    return output


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Open App",
    describe_call=lambda params: f'Open {params.get("app_name", "the app")}',
    scope_key=lambda params: scope_value("app", params.get("app_name", "unknown")),
    scope_label=lambda params: display_value(params.get("app_name"), fallback="Unknown app"),
)
def open_app(app_name: str) -> str:
    subprocess.run(["open", "-a", app_name], check=True)
    return f"Opened {app_name}"


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Run AppleScript",
    describe_call=lambda _params: "Run AppleScript",
    scope_key=lambda params: applescript_scope_key(str(params.get("script", ""))),
    scope_label=lambda params: applescript_scope_label(str(params.get("script", ""))),
)
def run_applescript(script: str) -> str:
    return _run_command(["osascript", "-e", script])


@governed_function_tool(
    execution_type=ExecutionType.SEARCH,
    title="Search Files",
    describe_call=lambda params: f'Search files for "{params.get("query", "")}"',
    scope_key=lambda params: scope_value("directory", params.get("directory", "~")),
)
def search_files(query: str, directory: str = "~") -> str:
    expanded_directory = str(Path(directory).expanduser())
    return _run_command(
        ["mdfind", "-onlyin", expanded_directory, f"kMDItemDisplayName == '*{query}*'"]
    ) or "No files found"


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Read Clipboard",
    describe_call=lambda _params: "Read the clipboard",
    scope_label=lambda _params: "Clipboard read",
)
def get_clipboard() -> str:
    return _run_command(["pbpaste"], timeout=5)


@governed_function_tool(
    execution_type=ExecutionType.EDIT,
    title="Set Clipboard",
    describe_call=lambda _params: "Update the clipboard",
    scope_key=lambda _params: scope_value("clipboard", "write"),
    scope_label=lambda _params: "Clipboard write",
)
def set_clipboard(text: str) -> str:
    _run_command(["pbcopy"], input_text=text, timeout=5)
    return "Clipboard updated"


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Send Notification",
    describe_call=lambda params: f'Send notification "{params.get("title", "")}"',
    scope_key=lambda params: hashed_scope(
        "notification", f'{params.get("title", "")}|{params.get("message", "")}'
    ),
    scope_label=lambda params: display_value(params.get("title"), fallback="Notification"),
)
def send_notification(title: str, message: str) -> str:
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    _run_command(
        ["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
        timeout=5,
    )
    return "Notification sent"


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Take Screenshot",
    describe_call=lambda params: f'Save screenshot to {params.get("filepath", "/tmp/boss-screenshot.png")}',
    scope_key=lambda params: scope_value("screenshot", params.get("filepath", "/tmp/boss-screenshot.png")),
    scope_label=lambda params: display_value(
        params.get("filepath"), fallback="/tmp/boss-screenshot.png"
    ),
)
def screenshot(filepath: str = "/tmp/boss-screenshot.png") -> str:
    subprocess.run(["screencapture", "-x", filepath], check=True)
    return f"Screenshot saved to {filepath}"