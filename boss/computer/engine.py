"""Core computer-use loop engine.

Orchestrates the turn-based cycle:
  1. Capture screenshot
  2. Send screenshot + history to model (computer-use enabled)
  3. Parse structured actions from response
  4. Check approval / pause state
  5. Execute action batch through the browser harness
  6. Update session state
  7. Repeat or terminate

The engine is stateless — all mutable state lives in ``ComputerSession``
and is persisted after every turn.  Cancellation and pause are checked at
each turn boundary via a thread-safe registry (same pattern as iOS delivery).

Page content and on-screen instructions are treated as untrusted — the loop
never evals arbitrary page data or follows instructions found in screenshots.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path
from typing import Any

from boss.computer.browser import (
    BrowserHarness,
    HarnessActionResult,
    HarnessError,
    HarnessNotReady,
    PlaywrightMissing,
)
from boss.computer.state import (
    ActionResult,
    BrowserStatus,
    ComputerAction,
    ComputerSession,
    SessionStatus,
    append_event,
    save_session,
    screenshot_path_for,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cancellation registry (thread-safe, same pattern as ios_delivery.engine)
# ---------------------------------------------------------------------------

_cancel_lock = threading.Lock()
_cancelled_ids: set[str] = set()
_paused_ids: set[str] = set()


def cancel_session(session_id: str) -> None:
    with _cancel_lock:
        _cancelled_ids.add(session_id)


def pause_session(session_id: str) -> None:
    with _cancel_lock:
        _paused_ids.add(session_id)


def resume_session(session_id: str) -> None:
    with _cancel_lock:
        _paused_ids.discard(session_id)


def is_cancelled(session_id: str) -> bool:
    with _cancel_lock:
        return session_id in _cancelled_ids


def is_paused(session_id: str) -> bool:
    with _cancel_lock:
        return session_id in _paused_ids


def _check_cancelled(session: ComputerSession) -> bool:
    if is_cancelled(session.session_id):
        session.status = SessionStatus.CANCELLED
        session.touch()
        save_session(session)
        append_event(session.session_id, "cancelled")
        return True
    return False


def _check_paused(session: ComputerSession) -> bool:
    if is_paused(session.session_id):
        session.status = SessionStatus.PAUSED
        session.pause_requested = True
        session.touch()
        save_session(session)
        append_event(session.session_id, "paused")
        return True
    return False


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------

def create_session(
    *,
    target_url: str,
    project_path: str | None = None,
    model: str | None = None,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    metadata: dict[str, Any] | None = None,
) -> ComputerSession:
    """Create and persist a new computer-use session."""
    from boss.config import settings

    session = ComputerSession(
        target_url=target_url,
        target_domain=_extract_domain(target_url),
        project_path=project_path,
        active_model=model or settings.code_model,
        metadata=metadata or {},
    )
    session.metadata["headless"] = headless
    session.metadata["viewport_width"] = viewport_width
    session.metadata["viewport_height"] = viewport_height

    save_session(session)
    append_event(session.session_id, "session_created", {
        "target_url": target_url,
        "model": session.active_model,
    })
    logger.info("Created computer-use session %s → %s", session.session_id[:12], target_url)
    return session


# ---------------------------------------------------------------------------
# Single-turn execution
# ---------------------------------------------------------------------------

def execute_turn(
    session: ComputerSession,
    harness: BrowserHarness,
) -> ComputerSession:
    """Run one turn of the computer-use loop.

    1. Capture screenshot
    2. Send to model
    3. Parse actions
    4. Execute action batch
    5. Update session state

    Returns the updated session.  Caller is responsible for the
    outer loop (calling ``execute_turn`` repeatedly).
    """
    if session.is_terminal:
        return session

    if _check_cancelled(session):
        return session
    if _check_paused(session):
        return session

    session.status = SessionStatus.RUNNING
    session.turn_index += 1
    turn = session.turn_index

    # 1. Screenshot
    try:
        ss_path = screenshot_path_for(session.session_id, turn)
        actual_path = harness.screenshot(ss_path)
        session.latest_screenshot_path = str(actual_path)
        session.latest_screenshot_ts = time.time()
        append_event(session.session_id, "screenshot", {"turn": turn, "path": str(actual_path)})
    except Exception as exc:
        session.status = SessionStatus.FAILED
        session.error = f"Screenshot failed: {exc}"
        save_session(session)
        append_event(session.session_id, "error", {"turn": turn, "error": session.error})
        return session

    # 2. Send to model
    try:
        model_response = _call_model(session)
    except Exception as exc:
        session.status = SessionStatus.FAILED
        session.error = f"Model call failed: {exc}"
        save_session(session)
        append_event(session.session_id, "error", {"turn": turn, "error": session.error})
        return session

    # 3. Parse response
    actions, final_answer, response_id = _parse_model_response(model_response)
    session.last_model_response_id = response_id

    if final_answer is not None:
        session.status = SessionStatus.COMPLETED
        session.final_answer = final_answer
        session.last_action_batch = []
        session.last_action_results = []
        save_session(session)
        append_event(session.session_id, "completed", {"turn": turn, "answer": final_answer})
        return session

    if not actions:
        session.status = SessionStatus.FAILED
        session.error = "Model returned no actions and no final answer"
        save_session(session)
        append_event(session.session_id, "error", {"turn": turn, "error": session.error})
        return session

    session.last_action_batch = [a.to_dict() for a in actions]

    # 4. Execute actions
    results = _execute_actions(harness, actions, session.session_id, turn)
    session.last_action_results = [r.to_dict() for r in results]

    # Check for action failures
    failures = [r for r in results if not r.success]
    if failures:
        last_failure = failures[-1]
        append_event(session.session_id, "action_failed", {
            "turn": turn,
            "error": last_failure.error,
        })

    # 5. Persist
    save_session(session)
    append_event(session.session_id, "turn_completed", {
        "turn": turn,
        "actions": len(actions),
        "failures": len(failures),
    })

    return session


# ---------------------------------------------------------------------------
# Run the full loop
# ---------------------------------------------------------------------------

def run_session(session: ComputerSession, *, max_turns: int = 50) -> ComputerSession:
    """Execute the computer-use loop until completion, pause, or error.

    This is the top-level entry point that manages the browser lifetime
    and calls ``execute_turn`` in a loop.
    """
    if session.is_terminal:
        return session

    headless = session.metadata.get("headless", True)
    vw = session.metadata.get("viewport_width", 1280)
    vh = session.metadata.get("viewport_height", 800)

    from boss.computer.state import _screenshots_dir

    harness = BrowserHarness(
        headless=headless,
        viewport_width=vw,
        viewport_height=vh,
        screenshot_dir=_screenshots_dir(),
    )

    session.status = SessionStatus.LAUNCHING
    session.browser_status = BrowserStatus.LAUNCHING
    save_session(session)
    append_event(session.session_id, "browser_launching")

    try:
        harness.launch()
    except PlaywrightMissing as exc:
        session.status = SessionStatus.FAILED
        session.browser_status = BrowserStatus.ERROR
        session.error = str(exc)
        save_session(session)
        append_event(session.session_id, "error", {"error": str(exc)})
        return session
    except HarnessError as exc:
        session.status = SessionStatus.FAILED
        session.browser_status = BrowserStatus.ERROR
        session.error = str(exc)
        save_session(session)
        append_event(session.session_id, "error", {"error": str(exc)})
        return session

    session.browser_status = BrowserStatus.READY
    save_session(session)
    append_event(session.session_id, "browser_ready")

    # Navigate to target
    if session.target_url:
        session.browser_status = BrowserStatus.NAVIGATING
        save_session(session)
        nav_result = harness.navigate(session.target_url)
        if not nav_result.success:
            session.status = SessionStatus.FAILED
            session.browser_status = BrowserStatus.ERROR
            session.error = f"Navigation failed: {nav_result.error}"
            save_session(session)
            harness.close()
            append_event(session.session_id, "error", {"error": session.error})
            return session
        session.browser_status = BrowserStatus.ACTIVE
        save_session(session)
        append_event(session.session_id, "navigated", {"url": session.target_url})

    # Turn loop
    try:
        for _ in range(max_turns):
            if session.is_terminal or session.is_paused:
                break
            session = execute_turn(session, harness)
    finally:
        harness.close()
        session.browser_status = BrowserStatus.CLOSED
        save_session(session)
        append_event(session.session_id, "browser_closed")

    # Clean up cancellation tracking
    with _cancel_lock:
        _cancelled_ids.discard(session.session_id)
        _paused_ids.discard(session.session_id)

    return session


# ---------------------------------------------------------------------------
# Model interaction (scaffolding)
# ---------------------------------------------------------------------------

def _call_model(session: ComputerSession) -> dict[str, Any]:
    """Send the current screenshot + context to the model.

    This is scaffolding — the actual OpenAI computer-use API call will be
    wired when we integrate the Responses API with computer-use tool type.
    For now, this returns a structured dict that the parser expects.
    """
    screenshot_path = session.latest_screenshot_path
    if not screenshot_path or not Path(screenshot_path).is_file():
        raise RuntimeError("No screenshot available for model call")

    # Read screenshot as base64
    screenshot_b64 = base64.b64encode(Path(screenshot_path).read_bytes()).decode("ascii")

    # Build the request payload shape (matches what we'll send to OpenAI)
    _request_payload = {
        "model": session.active_model,
        "turn_index": session.turn_index,
        "screenshot_base64": screenshot_b64[:50] + "...",  # truncated for logging
        "previous_response_id": session.last_model_response_id,
    }

    logger.info(
        "Model call for session %s turn %d (model=%s)",
        session.session_id[:12],
        session.turn_index,
        session.active_model,
    )

    # TODO: Wire actual Responses API call with computer-use tool type
    # For now, raise to signal the scaffolding boundary
    raise NotImplementedError(
        "Model call not yet wired — awaiting Responses API integration "
        "with computer-use tool type. Session state is valid up to this point."
    )


def _parse_model_response(
    response: dict[str, Any],
) -> tuple[list[ComputerAction], str | None, str | None]:
    """Parse the model response into actions and/or a final answer.

    Expected response shape (from OpenAI Responses API with computer-use)::

        {
            "id": "resp_...",
            "output": [
                {
                    "type": "computer_call",
                    "action": {
                        "type": "click",
                        "x": 100,
                        "y": 200
                    }
                },
                ...
            ]
        }

    Or a text output for the final answer::

        {
            "id": "resp_...",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Done. ..."}]
                }
            ]
        }
    """
    response_id = response.get("id")
    output_items = response.get("output", [])

    actions: list[ComputerAction] = []
    final_answer: str | None = None

    for item in output_items:
        item_type = item.get("type", "")

        if item_type == "computer_call":
            action_data = item.get("action", {})
            if action_data:
                actions.append(ComputerAction.from_dict(action_data))

        elif item_type == "message":
            # Extract text content
            content_parts = item.get("content", [])
            for part in content_parts:
                if part.get("type") == "output_text":
                    text = part.get("text", "")
                    if text:
                        final_answer = text
                        break

    return actions, final_answer, response_id


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _execute_actions(
    harness: BrowserHarness,
    actions: list[ComputerAction],
    session_id: str,
    turn: int,
) -> list[ActionResult]:
    """Execute a batch of actions through the harness."""
    results: list[ActionResult] = []
    harness_results = harness.execute_batch([a.to_dict() for a in actions])

    for i, hr in enumerate(harness_results):
        ar = ActionResult(
            action_type=hr.action_type,
            success=hr.success,
            error=hr.error,
            screenshot_path=hr.screenshot_path,
        )
        results.append(ar)
        append_event(session_id, "action_executed", {
            "turn": turn,
            "index": i,
            "type": hr.action_type,
            "success": hr.success,
            "error": hr.error,
            "duration_ms": hr.duration_ms,
        })

    return results


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def computer_use_status() -> dict[str, Any]:
    """Return diagnostic information about the computer-use subsystem."""
    from boss.computer.capabilities import detect_capabilities
    from boss.computer.state import list_sessions

    caps = detect_capabilities()
    sessions = list_sessions()

    active = [s for s in sessions if not s.is_terminal]
    completed = [s for s in sessions if s.status == SessionStatus.COMPLETED]
    failed = [s for s in sessions if s.status == SessionStatus.FAILED]

    return {
        "capabilities": caps.to_dict(),
        "sessions": {
            "total": len(sessions),
            "active": len(active),
            "completed": len(completed),
            "failed": len(failed),
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str | None:
    """Extract the domain from a URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or None
    except Exception:
        return None
