"""TestFlight / App Store Connect upload orchestration.

Handles the actual upload of a signed IPA to Apple's services through
governed subprocess execution.  Supports two upload strategies:

1. **fastlane pilot** — preferred when fastlane is available and an
   API key JSON file is configured.  Provides richer output and
   built-in processing-status polling.

2. **xcrun altool** — Apple's official CLI, always available with Xcode.
   Uses ``--apiKey`` / ``--apiIssuer`` for App Store Connect API auth.

Both strategies require an App Store Connect API key.  Boss never
stores or transmits credentials — it only passes file paths and
key IDs to the CLI tools, which handle the actual authentication.

Upload is always approval-gated: it goes through the governed runner
with EXTERNAL execution type semantics, separate from local build steps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from boss.ios_delivery.state import (
    IOSDeliveryRun,
    UploadMethod,
    UploadStatus,
    UploadTarget,
    append_event,
    save_run,
)

logger = logging.getLogger(__name__)


# ── Upload strategy selection ──────────────────────────────────────


class UploadStrategy(StrEnum):
    FASTLANE_PILOT = "fastlane_pilot"
    XCRUN_ALTOOL = "xcrun_altool"


@dataclass
class UploadPlan:
    """Resolved upload strategy and credentials."""

    strategy: UploadStrategy
    command: list[str]
    method: UploadMethod
    # For logging — never log actual secrets
    description: str
    api_key_id: str | None = None


@dataclass
class UploadResult:
    """Outcome of an upload attempt."""

    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    governed: bool
    # Extracted from output if available
    upload_id: str | None = None
    processing_url: str | None = None
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout_length": len(self.stdout),
            "stderr_length": len(self.stderr),
            "duration_ms": self.duration_ms,
            "governed": self.governed,
            "upload_id": self.upload_id,
            "error_detail": self.error_detail,
        }


# ── Credential validation ─────────────────────────────────────────


def validate_upload_credentials(
    run: IOSDeliveryRun,
) -> tuple[bool, str]:
    """Check that upload credentials are available and sufficient.

    Returns ``(ok, detail)`` — if not ok, detail explains what's missing.
    Does NOT read private key contents.
    """
    from boss.ios_delivery.signing import (
        CredentialStatus,
        check_signing_readiness,
        load_signing_config,
        ConfigFileCorrupt,
    )

    try:
        config = load_signing_config()
    except ConfigFileCorrupt as exc:
        return False, f"Signing config is malformed: {exc.reason}"

    if config is None:
        return False, (
            "No signing config found at ~/.boss/ios-signing.json. "
            "See docs/ios-signing-setup.md for setup instructions."
        )

    readiness = check_signing_readiness(config)
    if not readiness.can_upload:
        api_check = next(
            (c for c in readiness.checks if c.name == "api_key"), None,
        )
        detail = api_check.detail if api_check else "API key not configured"
        return False, f"Upload credentials not ready: {detail}"

    if config.api_key is None:
        return False, "API key configuration missing"

    return True, "Credentials available"


# ── Strategy resolution ────────────────────────────────────────────


def resolve_upload_plan(run: IOSDeliveryRun) -> UploadPlan | None:
    """Determine which upload CLI tool to use and build the command.

    Returns ``None`` if no viable upload path is available.

    Preference order:
    1. fastlane pilot (if fastlane available + api_key_path configured)
    2. xcrun altool (if xcrun available + api_key configured)
    """
    from boss.ios_delivery.signing import ConfigFileCorrupt, load_signing_config
    from boss.ios_delivery.toolchain import (
        build_altool_upload_command,
        build_pilot_upload_command,
        get_toolchain,
    )

    try:
        config = load_signing_config()
    except ConfigFileCorrupt:
        return None

    if config is None or config.api_key is None:
        return None

    if not run.ipa_path:
        return None

    toolchain = get_toolchain()

    # Strategy 1: fastlane pilot with API key JSON
    if toolchain.has_fastlane and config.fastlane and config.fastlane.api_key_path:
        api_key_path = config.fastlane.api_key_path
        if Path(api_key_path).is_file():
            cmd = build_pilot_upload_command(
                ipa_path=run.ipa_path,
                api_key_path=api_key_path,
            )
            return UploadPlan(
                strategy=UploadStrategy.FASTLANE_PILOT,
                command=cmd,
                method=UploadMethod.FASTLANE_PILOT,
                description=f"fastlane pilot upload with API key {config.api_key.key_id}",
                api_key_id=config.api_key.key_id,
            )

    # Strategy 2: xcrun altool with API key
    if toolchain.xcrun.available:
        # Derive the directory containing the .p8 so altool can find it
        # without requiring the user to stage it in Apple's default dirs.
        key_dir: str | None = None
        if config.api_key.key_path:
            _kp = Path(config.api_key.key_path)
            if _kp.is_file():
                key_dir = str(_kp.parent)
        cmd = build_altool_upload_command(
            ipa_path=run.ipa_path,
            api_key=config.api_key.key_id,
            api_issuer=config.api_key.issuer_id,
            api_key_path=key_dir,
        )
        return UploadPlan(
            strategy=UploadStrategy.XCRUN_ALTOOL,
            command=cmd,
            method=UploadMethod.XCRUN_ALTOOL,
            description=f"xcrun altool upload with API key {config.api_key.key_id}",
            api_key_id=config.api_key.key_id,
        )

    return None


# ── Upload execution ───────────────────────────────────────────────


def execute_upload(
    run: IOSDeliveryRun,
    plan: UploadPlan,
    *,
    timeout: int = 1800,
) -> UploadResult:
    """Execute the upload command through the governed runner.

    Timeout defaults to 30 minutes — large IPAs over slow connections
    can take a while.  The subprocess is registered for cancellation.
    """
    from boss.ios_delivery.runner import run_build_command

    run.upload_status = UploadStatus.UPLOADING.value
    run.upload_method = plan.method.value
    run.upload_started_at = time.time()
    save_run(run)

    append_event(
        run.run_id,
        event_type="upload_start",
        message=f"Starting upload via {plan.strategy.value}",
        payload={
            "strategy": plan.strategy.value,
            "command_preview": " ".join(plan.command[:5]) + "...",
            "api_key_id": plan.api_key_id,
        },
    )

    result = run_build_command(
        plan.command,
        cwd=run.project_path,
        timeout=timeout,
        run_id=run.run_id,
    )

    upload_result = UploadResult(
        success=result.success,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        governed=result.governed,
    )

    # Try to extract upload ID or error detail from output
    if result.success:
        upload_result.upload_id = _extract_upload_id(result.output, plan.strategy)
    else:
        upload_result.error_detail = _extract_error_detail(
            result.output, plan.strategy,
        )
        if result.denied_reason:
            upload_result.error_detail = result.denied_reason

    return upload_result


# ── Processing status check ────────────────────────────────────────


@dataclass
class ProcessingStatus:
    """Current processing state of an uploaded build."""

    status: str  # UploadStatus value
    detail: str
    build_number: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "detail": self.detail}
        if self.build_number:
            d["build_number"] = self.build_number
        if self.version:
            d["version"] = self.version
        return d


def check_processing_status(run: IOSDeliveryRun) -> ProcessingStatus:
    """Check the processing state of an uploaded build.

    Uses fastlane pilot if available, otherwise reports based on
    known state only (altool doesn't have a processing query).

    When the check reveals a status transition (e.g. processing → ready),
    the run record is updated and persisted so the change is durable.
    """
    if run.upload_status == UploadStatus.NOT_STARTED.value:
        return ProcessingStatus(
            status=UploadStatus.NOT_STARTED.value,
            detail="Upload has not started",
        )

    if run.upload_status == UploadStatus.FAILED.value:
        return ProcessingStatus(
            status=UploadStatus.FAILED.value,
            detail=run.error or "Upload failed",
        )

    if run.upload_status == UploadStatus.READY.value:
        return ProcessingStatus(
            status=UploadStatus.READY.value,
            detail="Build is ready for testing on TestFlight",
            build_number=run.metadata.get("build_number"),
            version=run.metadata.get("app_version"),
        )

    if run.upload_status in (
        UploadStatus.UPLOADING.value,
        UploadStatus.PROCESSING.value,
    ):
        # If we used fastlane pilot and it completed, the upload itself
        # waited for processing.  Otherwise we can try to query.
        if run.upload_method == UploadMethod.FASTLANE_PILOT.value:
            result = _check_via_pilot(run)
        else:
            result = ProcessingStatus(
                status=UploadStatus.PROCESSING.value,
                detail=(
                    "Build uploaded via altool. Check App Store Connect "
                    "for processing status — altool does not provide a "
                    "processing query."
                ),
            )

        # Persist any status transition discovered by the check
        _persist_status_transition(run, result)
        return result

    return ProcessingStatus(
        status=run.upload_status,
        detail=f"Upload status: {run.upload_status}",
    )


def _persist_status_transition(
    run: IOSDeliveryRun, result: ProcessingStatus
) -> None:
    """Update and save the run if the processing check found a new status."""
    from boss.ios_delivery.state import DeliveryPhase, save_run

    if result.status == run.upload_status:
        return  # no change

    old_status = run.upload_status
    run.upload_status = result.status

    # When processing completes, mark the delivery run as truly finished
    if result.status == UploadStatus.READY.value:
        run.phase = DeliveryPhase.COMPLETED.value
        run.upload_finished_at = time.time()

    append_event(
        run.run_id,
        event_type="upload_status_transition",
        message=f"Upload status changed from {old_status} to {result.status}",
        payload={"from": old_status, "to": result.status},
    )
    save_run(run)


def _check_via_pilot(run: IOSDeliveryRun) -> ProcessingStatus:
    """Query build processing state using fastlane pilot."""
    from boss.ios_delivery.signing import ConfigFileCorrupt, load_signing_config
    from boss.ios_delivery.toolchain import build_pilot_builds_command

    try:
        config = load_signing_config()
    except ConfigFileCorrupt:
        config = None

    if (
        config is None
        or config.fastlane is None
        or not config.fastlane.api_key_path
    ):
        return ProcessingStatus(
            status=UploadStatus.PROCESSING.value,
            detail="Cannot check processing: fastlane API key path not configured",
        )

    cmd = build_pilot_builds_command(
        api_key_path=config.fastlane.api_key_path,
        app_identifier=run.bundle_identifier,
    )

    from boss.ios_delivery.runner import run_build_command

    result = run_build_command(cmd, cwd=run.project_path, timeout=60, run_id=None)

    if not result.success:
        return ProcessingStatus(
            status=UploadStatus.PROCESSING.value,
            detail="Could not query processing status",
        )

    # Parse pilot output for build status
    return _parse_pilot_builds_output(result.output, run)


# ── Output parsing helpers ─────────────────────────────────────────


def _extract_upload_id(output: str, strategy: UploadStrategy) -> str | None:
    """Try to extract an upload/build ID from tool output."""
    if strategy == UploadStrategy.XCRUN_ALTOOL:
        # altool outputs: No errors uploading '<path>'.
        # or: RequestUUID = <uuid>
        m = re.search(r"RequestUUID\s*=\s*(\S+)", output)
        if m:
            return m.group(1)

    if strategy == UploadStrategy.FASTLANE_PILOT:
        # pilot logs various info; look for build number
        m = re.search(r"Successfully uploaded.*build[:\s]+(\S+)", output, re.IGNORECASE)
        if m:
            return m.group(1)

    return None


def _extract_error_detail(output: str, strategy: UploadStrategy) -> str | None:
    """Extract a meaningful error message from failed upload output."""
    if not output:
        return None

    # Common Apple errors
    error_patterns = [
        r"error:\s*(.+?)(?:\n|$)",
        r"Error:\s*(.+?)(?:\n|$)",
        r"ERROR ITMS-\d+:\s*\"(.+?)\"",
        r"Unable to authenticate",
        r"The provided entity includes.*that is not valid",
        r"App Store Connect Operation Error",
    ]
    for pattern in error_patterns:
        m = re.search(pattern, output)
        if m:
            return m.group(1) if m.lastindex else m.group(0)

    # Truncated last few lines as fallback
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    if lines:
        return lines[-1][:200]

    return None


def _parse_pilot_builds_output(
    output: str,
    run: IOSDeliveryRun,
) -> ProcessingStatus:
    """Parse ``fastlane pilot builds`` output for processing state."""
    # pilot outputs a table like:
    #   +---------+---------+----------+-----------+
    #   | # | Version | Build | Testing |
    #   ...
    # Look for "processing" or "active" in the most recent build
    lower = output.lower()
    if "processing" in lower:
        return ProcessingStatus(
            status=UploadStatus.PROCESSING.value,
            detail="Build is still processing on App Store Connect",
        )
    if "active" in lower or "ready" in lower:
        return ProcessingStatus(
            status=UploadStatus.READY.value,
            detail="Build processing complete — ready for TestFlight testing",
        )

    return ProcessingStatus(
        status=UploadStatus.PROCESSING.value,
        detail="Build status unclear from pilot output — check App Store Connect",
    )
