"""Computer-use tools — governed tools for agent-triggered browser automation."""

from __future__ import annotations

from boss.execution import (
    ExecutionType,
    display_value,
    governed_function_tool,
    scope_value,
)


# ---------------------------------------------------------------------------
# Implementation helpers — testable without the FunctionTool wrapper
# ---------------------------------------------------------------------------

def _start_computer_session_impl(
    url: str,
    task: str = "",
    project_path: str = "",
    headless: bool = True,
) -> str:
    import contextvars
    import logging
    import threading

    from boss.config import settings
    from boss.computer.engine import (
        create_session,
        run_session,
        validate_target_domain,
    )

    if not settings.computer_use_enabled:
        return "Computer-use is disabled in settings."

    allowed, reason = validate_target_domain(url)
    if not allowed:
        return f"Domain not allowed: {reason}"

    session = create_session(
        target_url=url,
        task=task or None,
        project_path=project_path or None,
        headless=headless,
    )

    ctx = contextvars.copy_context()
    logger = logging.getLogger("boss.tools.computer")

    def _run_loop() -> None:
        try:
            run_session(session, max_turns=settings.computer_use_max_turns)
        except Exception:
            logger.exception(
                "Computer-use session failed: %s", session.session_id
            )
            from boss.computer.state import SessionStatus, save_session
            session.status = SessionStatus.FAILED
            session.error = f"Session crashed: {__import__('traceback').format_exc(limit=3)}"
            session.touch()
            save_session(session)

    threading.Thread(
        target=ctx.run,
        args=(_run_loop,),
        daemon=True,
        name=f"computer-{session.session_id[:12]}",
    ).start()

    parts = [
        f"Computer-use session started: {session.session_id}",
        f"Target: {url}",
        f"Status: {session.status}",
    ]
    if task:
        parts.append(f"Task: {task}")
    return "\n".join(parts)


def _computer_session_status_impl(session_id: str = "") -> str:
    from boss.config import settings

    if not settings.computer_use_enabled:
        return "Computer-use is disabled in settings."

    if session_id:
        from boss.computer.state import load_session

        session = load_session(session_id)
        if session is None:
            return f"Session {session_id[:12]} not found."

        parts = [
            f"Session: {session.session_id[:12]}",
            f"Status: {session.status}",
            f"Target: {session.target_url}",
            f"Turn: {session.turn_index}",
        ]
        if session.task:
            parts.append(f"Task: {session.task}")
        if session.approval_pending:
            parts.append(f"Approval pending: {session.pending_approval_id or 'yes'}")
        if session.error:
            parts.append(f"Error: {session.error}")
        if session.final_answer:
            parts.append(f"Result: {session.final_answer}")
        if session.latest_screenshot_path:
            parts.append(f"Latest screenshot: {session.latest_screenshot_path}")
        return "\n".join(parts)

    from boss.computer.engine import computer_use_status

    status = computer_use_status()
    caps = status["capabilities"]
    sessions = status["sessions"]

    parts = [
        f"Sessions: {sessions['total']} total, {sessions['active']} active, "
        f"{sessions['completed']} completed, {sessions['failed']} failed",
    ]
    if caps.get("has_browser"):
        parts.append(f"Browser: {caps.get('browser_path', 'detected')}")
    if caps.get("has_playwright"):
        parts.append("Playwright: available")
    return "\n".join(parts)


def _pause_computer_session_impl(session_id: str) -> str:
    from boss.computer.engine import pause_session
    from boss.computer.state import load_session

    session = load_session(session_id)
    if session is None:
        return f"Session {session_id[:12]} not found."
    if session.is_terminal:
        return f"Session {session_id[:12]} is already {session.status}."

    pause_session(session_id)
    return f"Pause requested for session {session_id[:12]}."


def _resume_computer_session_impl(session_id: str) -> str:
    import contextvars
    import logging
    import threading

    from boss.config import settings
    from boss.computer.engine import resume_session, run_session
    from boss.computer.state import SessionStatus, load_session, save_session

    session = load_session(session_id)
    if session is None:
        return f"Session {session_id[:12]} not found."
    if session.is_terminal:
        return f"Session {session_id[:12]} is already {session.status}."
    if session.status not in (SessionStatus.PAUSED, SessionStatus.CREATED):
        return (
            f"Session {session_id[:12]} is {session.status}, "
            f"expected 'paused' or 'created'."
        )

    # Clear in-memory flag so the loop won't immediately re-pause
    resume_session(session_id)

    # Reset persisted paused state so run_session sees a startable session
    session.status = SessionStatus.CREATED
    session.pause_requested = False
    save_session(session)

    ctx = contextvars.copy_context()
    logger = logging.getLogger("boss.tools.computer")

    def _run_loop() -> None:
        try:
            run_session(session, max_turns=settings.computer_use_max_turns)
        except Exception:
            logger.exception(
                "Computer-use session resume failed: %s", session_id
            )

    threading.Thread(
        target=ctx.run,
        args=(_run_loop,),
        daemon=True,
        name=f"computer-resume-{session_id[:12]}",
    ).start()

    return f"Session {session_id[:12]} resumed — background loop restarted."


def _stop_computer_session_impl(session_id: str) -> str:
    from boss.computer.engine import cancel_session
    from boss.computer.state import SessionStatus, load_session, save_session, append_event

    session = load_session(session_id)
    if session is None:
        return f"Session {session_id[:12]} not found."
    if session.is_terminal:
        return f"Session {session_id[:12]} is already {session.status}."

    # Set in-memory flag + close parked harness (works for running loops)
    cancel_session(session_id)

    # For sessions NOT inside an active loop, persist cancellation now
    if session.status in (
        SessionStatus.PAUSED,
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.CREATED,
    ):
        session.status = SessionStatus.CANCELLED
        session.touch()
        save_session(session)
        append_event(session_id, "cancelled", {"source": "stop_tool"})

    return f"Session {session_id[:12]} cancelled."


def _computer_take_screenshot_impl(session_id: str) -> str:
    from boss.computer.state import load_session

    session = load_session(session_id)
    if session is None:
        return f"Session {session_id[:12]} not found."

    if session.latest_screenshot_path:
        return f"Latest screenshot: {session.latest_screenshot_path}"
    return f"No screenshot available yet for session {session_id[:12]}."


# ---------------------------------------------------------------------------
# Governed tool surface — thin wrappers registered with the agent framework
# ---------------------------------------------------------------------------


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Start Computer Session",
    describe_call=lambda params: f'Start computer-use session for {params.get("url", "URL")}',
    scope_key=lambda params: scope_value("computer", "start"),
    scope_label=lambda params: display_value(
        params.get("url"), fallback="Start computer-use session"
    ),
)
def start_computer_session(
    url: str,
    task: str = "",
    project_path: str = "",
    headless: bool = True,
) -> str:
    """Start a new computer-use browser session targeting a URL.

    The session launches in the background and the agent can poll status.
    Domain must be in the allowed set if an allowlist is configured.

    Args:
        url: Target URL to open in the browser.
        task: Description of what the session should accomplish.
        project_path: Optional project root for context.
        headless: Run the browser headless (True) or visible (False).
    """
    return _start_computer_session_impl(url, task, project_path, headless)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Computer Session Status",
    describe_call=lambda params: "Check computer-use session status",
    scope_key=lambda _params: scope_value("computer", "status"),
    scope_label=lambda _params: "Computer session status check",
)
def computer_session_status(session_id: str = "") -> str:
    """Check the status of a computer-use session or the overall subsystem.

    Args:
        session_id: Session ID to check. If empty, returns overall status.
    """
    return _computer_session_status_impl(session_id)


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Pause Computer Session",
    describe_call=lambda params: f'Pause computer session {params.get("session_id", "")[:12]}',
    scope_key=lambda params: scope_value("computer", "pause"),
    scope_label=lambda params: display_value(
        params.get("session_id", "")[:12], fallback="Pause computer session"
    ),
)
def pause_computer_session(session_id: str) -> str:
    """Pause a running computer-use session.

    Args:
        session_id: ID of the session to pause.
    """
    return _pause_computer_session_impl(session_id)


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Resume Computer Session",
    describe_call=lambda params: f'Resume computer session {params.get("session_id", "")[:12]}',
    scope_key=lambda params: scope_value("computer", "resume"),
    scope_label=lambda params: display_value(
        params.get("session_id", "")[:12], fallback="Resume computer session"
    ),
)
def resume_computer_session(session_id: str) -> str:
    """Resume a paused computer-use session.

    Clears the paused flag and relaunches the turn loop on a background
    thread, just like the initial start.

    Args:
        session_id: ID of the session to resume.
    """
    return _resume_computer_session_impl(session_id)


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Stop Computer Session",
    describe_call=lambda params: f'Stop computer session {params.get("session_id", "")[:12]}',
    scope_key=lambda params: scope_value("computer", "stop"),
    scope_label=lambda params: display_value(
        params.get("session_id", "")[:12], fallback="Stop computer session"
    ),
)
def stop_computer_session(session_id: str) -> str:
    """Stop and cancel a running computer-use session.

    For sessions that are actively inside a turn loop the cancellation
    flag will be picked up on the next iteration.  For sessions that are
    paused, waiting for approval, or still in 'created' state the
    persisted status is set to cancelled immediately so they don't
    linger.

    Args:
        session_id: ID of the session to stop.
    """
    return _stop_computer_session_impl(session_id)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Computer Screenshot",
    describe_call=lambda params: f'Get screenshot for session {params.get("session_id", "")[:12]}',
    scope_key=lambda params: scope_value("computer", "screenshot"),
    scope_label=lambda params: display_value(
        params.get("session_id", "")[:12], fallback="Computer screenshot"
    ),
)
def computer_take_screenshot(session_id: str) -> str:
    """Get the latest screenshot path from a computer-use session.

    Args:
        session_id: ID of the session to get the screenshot for.
    """
    return _computer_take_screenshot_impl(session_id)
