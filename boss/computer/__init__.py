"""Computer-use subsystem — browser-first computer-use sessions with Boss governance.

V1 targets a browser harness via Playwright.  The engine owns the loop:
screenshot → model → actions → execute → repeat.  All actions flow through
Boss approval; page content is treated as untrusted input.

Gracefully degrades when Playwright or a browser runtime is missing —
capability detection reports honestly what is and is not available.
"""

from boss.computer.state import (
    ActionResult,
    ActionType,
    BrowserStatus,
    ComputerAction,
    ComputerSession,
    HarnessType,
    SessionStatus,
    append_event,
    delete_session,
    list_sessions,
    load_session,
    read_events,
    save_session,
    screenshot_path_for,
)
from boss.computer.capabilities import ComputerCapabilities, detect_capabilities
from boss.computer.browser import (
    BrowserHarness,
    HarnessActionResult,
    HarnessError,
    HarnessNotReady,
    PlaywrightMissing,
)
from boss.computer.engine import (
    cancel_session,
    computer_use_status,
    create_session,
    execute_turn,
    is_cancelled,
    is_paused,
    pause_session,
    resume_session,
    run_session,
)

__all__ = [
    # State / models
    "ActionResult",
    "ActionType",
    "BrowserStatus",
    "ComputerAction",
    "ComputerSession",
    "HarnessType",
    "SessionStatus",
    # Persistence
    "append_event",
    "delete_session",
    "list_sessions",
    "load_session",
    "read_events",
    "save_session",
    "screenshot_path_for",
    # Capabilities
    "ComputerCapabilities",
    "detect_capabilities",
    # Browser harness
    "BrowserHarness",
    "HarnessActionResult",
    "HarnessError",
    "HarnessNotReady",
    "PlaywrightMissing",
    # Engine
    "cancel_session",
    "computer_use_status",
    "create_session",
    "execute_turn",
    "is_cancelled",
    "is_paused",
    "pause_session",
    "resume_session",
    "run_session",
]
