"""Computer-use session state and persistence.

A ``ComputerSession`` tracks every aspect of a single computer-use loop:
identity, target, browser lifecycle, model interaction, approval state,
screenshots, and terminal conditions.  Sessions are persisted as JSON files
under ``settings.app_data_dir / "computer" / "sessions"``.  An append-only
JSONL event log sits alongside for structured audit.

Persistence helpers follow the same atomic-write pattern used by iOS delivery:
``tempfile.mkstemp`` → write → ``os.replace``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version — bump when the persisted schema changes in a non-additive way
# ---------------------------------------------------------------------------

COMPUTER_SESSION_VERSION = 1

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionStatus(StrEnum):
    """High-level lifecycle status of a computer-use session."""

    CREATED = "created"
    LAUNCHING = "launching"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HarnessType(StrEnum):
    """Execution environment for the computer-use session."""

    BROWSER = "browser"


class BrowserStatus(StrEnum):
    """Browser lifecycle within a session."""

    NOT_STARTED = "not_started"
    LAUNCHING = "launching"
    READY = "ready"
    NAVIGATING = "navigating"
    ACTIVE = "active"
    CLOSED = "closed"
    ERROR = "error"


_TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset({
    SessionStatus.COMPLETED,
    SessionStatus.FAILED,
    SessionStatus.CANCELLED,
})


# ---------------------------------------------------------------------------
# Action model — structured actions returned by the model
# ---------------------------------------------------------------------------


class ActionType(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    SCROLL = "scroll"
    TYPE = "type"
    KEYPRESS = "keypress"
    MOVE = "move"
    DRAG = "drag"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    NAVIGATE = "navigate"


@dataclass
class ComputerAction:
    """A single computer-use action from the model."""

    type: str  # ActionType value
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    url: str | None = None
    scroll_x: int = 0
    scroll_y: int = 0
    drag_end_x: int | None = None
    drag_end_y: int | None = None
    duration_ms: int | None = None
    button: str = "left"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComputerAction:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ActionResult:
    """Result of executing a single action."""

    action_type: str
    success: bool = True
    error: str | None = None
    screenshot_path: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionResult:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------


@dataclass
class ComputerSession:
    """Full state of a computer-use session."""

    # Identity
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    project_path: str | None = None
    target_url: str | None = None
    target_domain: str | None = None
    task: str | None = None

    # Live browser location (updated after navigations)
    current_url: str | None = None
    current_domain: str | None = None

    # Harness
    harness_type: str = HarnessType.BROWSER
    browser_status: str = BrowserStatus.NOT_STARTED

    # Session lifecycle
    status: str = SessionStatus.CREATED
    approval_pending: bool = False
    pending_approval_id: str | None = None
    pause_requested: bool = False

    # Model
    active_model: str = ""

    # Screenshots
    latest_screenshot_path: str | None = None
    latest_screenshot_ts: float | None = None

    # Loop state
    turn_index: int = 0
    last_action_batch: list[dict[str, Any]] = field(default_factory=list)
    last_action_results: list[dict[str, Any]] = field(default_factory=list)
    last_model_response_id: str | None = None
    last_call_id: str | None = None

    # Timestamps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Terminal state
    error: str | None = None
    final_answer: str | None = None

    # Extensibility
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = COMPUTER_SESSION_VERSION

    # ── Helpers ──────────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return SessionStatus(self.status) in _TERMINAL_STATUSES

    @property
    def is_running(self) -> bool:
        return self.status == SessionStatus.RUNNING

    @property
    def is_paused(self) -> bool:
        return self.status == SessionStatus.PAUSED

    def touch(self) -> None:
        self.updated_at = time.time()

    def summary(self) -> str:
        return (
            f"[{self.session_id[:12]}] {self.status} "
            f"turn={self.turn_index} harness={self.harness_type} "
            f"url={self.target_url or '(none)'}"
        )

    # ── Serialization ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["version"] = COMPUTER_SESSION_VERSION
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ComputerSession:
        known = {f.name for f in ComputerSession.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return ComputerSession(**filtered)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _sessions_dir() -> Path:
    from boss.config import settings
    d = settings.app_data_dir / "computer" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _events_dir() -> Path:
    from boss.config import settings
    d = settings.app_data_dir / "computer" / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _screenshots_dir() -> Path:
    from boss.config import settings
    d = settings.app_data_dir / "computer" / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_session_id() -> str:
    return uuid.uuid4().hex


def save_session(session: ComputerSession) -> Path:
    """Persist session state atomically."""
    session.touch()
    dest = _sessions_dir() / f"{session.session_id}.json"
    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(session.to_dict(), f, indent=2)
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return dest


def load_session(session_id: str) -> ComputerSession | None:
    """Load a session by ID.  Returns None if not found."""
    path = _sessions_dir() / f"{session_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return ComputerSession.from_dict(data)
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Failed to load session %s: %s", session_id, exc)
        return None


def list_sessions() -> list[ComputerSession]:
    """Return all persisted sessions, newest first."""
    sessions: list[ComputerSession] = []
    for p in sorted(_sessions_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
            sessions.append(ComputerSession.from_dict(data))
        except Exception:
            continue
    return sessions


def delete_session(session_id: str) -> bool:
    """Remove a persisted session.  Returns True if deleted."""
    path = _sessions_dir() / f"{session_id}.json"
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def append_event(session_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
    """Append a structured event to the session's JSONL log."""
    path = _events_dir() / f"{session_id}.jsonl"
    entry = {
        "ts": time.time(),
        "event": event_type,
        "session_id": session_id,
    }
    if data:
        entry["data"] = data
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("Failed to append event for %s: %s", session_id, exc)


def read_events(session_id: str) -> list[dict[str, Any]]:
    """Read all events for a session."""
    path = _events_dir() / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def screenshot_path_for(session_id: str, turn_index: int) -> Path:
    """Return the canonical screenshot path for a given turn."""
    return _screenshots_dir() / f"{session_id}_turn{turn_index:04d}.png"
