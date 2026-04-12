"""iOS delivery run state models and persistence.

Each delivery run is a self-contained record that tracks a single
archive → export → upload pipeline.  Serialized as JSON under
``~/.boss/ios-deliveries/<run_id>.json`` with an append-only JSONL
event log alongside it.
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

from boss.config import settings

logger = logging.getLogger(__name__)

# Bump when the serialized shape changes.  ``from_dict`` silently drops
# unknown keys so new *optional* fields are backward-compatible without a
# bump; bump only when a required field is renamed or the semantics of an
# existing field change.
IOS_DELIVERY_VERSION = 1


# ── Enums ───────────────────────────────────────────────────────────


class DeliveryPhase(StrEnum):
    """High-level phase of an iOS delivery run."""

    PENDING = "pending"
    INSPECTING = "inspecting"
    ARCHIVING = "archiving"
    EXPORTING = "exporting"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExportMethod(StrEnum):
    """Xcode export methods (maps to ``-exportOptionsPlist`` ``method``)."""

    APP_STORE = "app-store"
    AD_HOC = "ad-hoc"
    DEVELOPMENT = "development"
    ENTERPRISE = "enterprise"
    APP_STORE_CONNECT = "app-store-connect"


class SigningMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class UploadTarget(StrEnum):
    TESTFLIGHT = "testflight"
    APP_STORE_CONNECT = "app-store-connect"
    NONE = "none"


class UploadStatus(StrEnum):
    """Granular upload lifecycle status."""

    NOT_STARTED = "not_started"
    CREDENTIAL_CHECK = "credential_check"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class UploadMethod(StrEnum):
    """Which CLI tool performs the upload."""

    FASTLANE_PILOT = "fastlane_pilot"
    XCRUN_ALTOOL = "xcrun_altool"
    NONE = "none"


_TERMINAL_PHASES = frozenset({
    DeliveryPhase.COMPLETED,
    DeliveryPhase.FAILED,
    DeliveryPhase.CANCELLED,
})


# ── Core state model ───────────────────────────────────────────────


@dataclass
class IOSDeliveryRun:
    """Complete state of a single iOS delivery pipeline run."""

    # ── Identity
    run_id: str
    project_path: str

    # ── Project references  (resolved during inspect phase)
    xcodeproj_path: str | None = None
    xcworkspace_path: str | None = None
    scheme: str | None = None
    configuration: str = "Release"

    # ── Export / signing
    export_method: str = ExportMethod.APP_STORE.value
    bundle_identifier: str | None = None
    signing_mode: str = SigningMode.UNKNOWN.value
    team_id: str | None = None

    # ── Artifact paths  (populated as phases complete)
    archive_path: str | None = None
    ipa_path: str | None = None
    dsym_path: str | None = None

    # ── Upload
    upload_target: str = UploadTarget.NONE.value
    upload_status: str = UploadStatus.NOT_STARTED.value
    upload_method: str = UploadMethod.NONE.value
    upload_id: str | None = None  # Apple's build/upload ID if returned
    upload_started_at: float | None = None
    upload_finished_at: float | None = None

    # ── Pipeline state
    phase: str = DeliveryPhase.PENDING.value
    error: str | None = None
    retry_count: int = 0
    build_log: str = ""
    export_log: str = ""
    upload_log: str = ""

    # ── Timestamps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    # ── Extensibility
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = IOS_DELIVERY_VERSION

    # ── Serialization ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["version"] = IOS_DELIVERY_VERSION
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> IOSDeliveryRun:
        known = {k for k in IOSDeliveryRun.__dataclass_fields__}
        return IOSDeliveryRun(**{k: v for k, v in data.items() if k in known})

    # ── Convenience ────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    def summary(self) -> str:
        parts = [
            f"run={self.run_id[:12]}",
            f"phase={self.phase}",
        ]
        if self.scheme:
            parts.append(f"scheme={self.scheme}")
        if self.bundle_identifier:
            parts.append(f"bundle={self.bundle_identifier}")
        if self.export_method:
            parts.append(f"export={self.export_method}")
        if self.archive_path:
            parts.append(f"archive={self.archive_path}")
        if self.ipa_path:
            parts.append(f"ipa={self.ipa_path}")
        if self.error:
            parts.append(f"error={self.error[:80]}")
        return " | ".join(parts)


# ── Persistence helpers ─────────────────────────────────────────────


def _runs_dir() -> Path:
    d = settings.app_data_dir / "ios-deliveries"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.json"


def _event_log_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.events.jsonl"


def save_run(run: IOSDeliveryRun) -> Path:
    """Atomically persist a delivery run to disk.

    Uses a unique temp file per call so concurrent saves (e.g. an active
    phase and a cancel request) never stomp the same temp path.
    """
    run.updated_at = time.time()
    path = _run_path(run.run_id)
    fd = None
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{run.run_id}_",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        data = json.dumps(run.to_dict(), indent=2, default=str).encode("utf-8")
        os.write(fd, data)
        os.close(fd)
        fd = None
        tmp_path.replace(path)
    except OSError as exc:
        logger.error("Failed to save iOS delivery run %s: %s", run.run_id, exc)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return path


def load_run(run_id: str) -> IOSDeliveryRun | None:
    """Load a delivery run from disk, or *None* if missing/corrupt."""
    path = _run_path(run_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return IOSDeliveryRun.from_dict(data)
    except (json.JSONDecodeError, TypeError, KeyError):
        logger.warning("Corrupt iOS delivery run file: %s", path)
        return None


def list_runs(*, limit: int = 50) -> list[IOSDeliveryRun]:
    """Return the most recent delivery runs, newest first."""
    runs: list[IOSDeliveryRun] = []
    for p in sorted(
        _runs_dir().glob("*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    ):
        if p.name.endswith(".json.tmp"):
            continue
        if len(runs) >= limit:
            break
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            runs.append(IOSDeliveryRun.from_dict(data))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return runs


def delete_run(run_id: str) -> bool:
    """Remove a run file and its event log.  Returns True if anything was deleted."""
    deleted = False
    for path in (_run_path(run_id), _event_log_path(run_id)):
        if path.exists():
            path.unlink(missing_ok=True)
            deleted = True
    return deleted


def new_run_id() -> str:
    return uuid.uuid4().hex


# ── Event log ───────────────────────────────────────────────────────


# Maximum event log lines per run.  When exceeded, the oldest events
# are trimmed to keep the file bounded.
_MAX_EVENT_LOG_LINES = 2000


def append_event(
    run_id: str,
    *,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append a structured event to the run's JSONL log."""
    path = _event_log_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.time(),
        "type": event_type,
        "message": message,
        "payload": payload or {},
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")

    # Rotate if the log has grown too large.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_EVENT_LOG_LINES:
            keep = lines[-(_MAX_EVENT_LOG_LINES // 2):]
            path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_events(run_id: str) -> list[dict[str, Any]]:
    """Read all events for a given run."""
    path = _event_log_path(run_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
