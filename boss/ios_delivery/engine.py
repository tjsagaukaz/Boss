"""iOS delivery engine — orchestrates inspect → archive → export → upload.

Each public function accepts an ``IOSDeliveryRun``, mutates it through the
relevant phase, persists the result, and returns it.  The caller (tool layer
or API endpoint) decides which phases to execute and in what order.

Archive and export phases execute real ``xcodebuild`` commands through the
Boss Runner governance layer.  Upload uses ``fastlane pilot`` (preferred) or
``xcrun altool`` with App Store Connect API key authentication.
"""

from __future__ import annotations

import logging
import plistlib
import signal
import threading
import time
from pathlib import Path
from typing import Any

from boss.ios_delivery.state import (
    DeliveryPhase,
    ExportMethod,
    IOSDeliveryRun,
    SigningMode,
    UploadMethod,
    UploadStatus,
    UploadTarget,
    append_event,
    new_run_id,
    save_run,
)

logger = logging.getLogger(__name__)


# ── Cancellation registry ──────────────────────────────────────────

_cancel_lock = threading.Lock()
_cancelled_ids: set[str] = set()


def cancel_run(run: IOSDeliveryRun) -> IOSDeliveryRun:
    """Mark a run as cancelled.  Safe to call from any thread."""
    with _cancel_lock:
        _cancelled_ids.add(run.run_id)
    if not run.is_terminal:
        run.phase = DeliveryPhase.CANCELLED.value
        run.error = "Cancelled by user"
        # Terminate any live build subprocess
        from boss.ios_delivery.runner import terminate_build_process
        terminate_build_process(run.run_id)
        append_event(run.run_id, event_type="cancelled", message="Run cancelled")
        save_run(run)
    return run


def is_cancelled(run_id: str) -> bool:
    with _cancel_lock:
        return run_id in _cancelled_ids


def _check_cancelled(run: IOSDeliveryRun) -> bool:
    """Return True and mark the run if cancellation was requested."""
    if is_cancelled(run.run_id):
        run.phase = DeliveryPhase.CANCELLED.value
        run.error = "Cancelled by user"
        save_run(run)
        return True
    return False


# ── Phase: create ───────────────────────────────────────────────────


def create_run(
    project_path: str | Path,
    *,
    scheme: str | None = None,
    configuration: str = "Release",
    export_method: str = ExportMethod.APP_STORE.value,
    upload_target: str = UploadTarget.NONE.value,
) -> IOSDeliveryRun:
    """Create a new delivery run in PENDING state."""
    run = IOSDeliveryRun(
        run_id=new_run_id(),
        project_path=str(project_path),
        scheme=scheme,
        configuration=configuration,
        export_method=export_method,
        upload_target=upload_target,
    )
    save_run(run)
    append_event(
        run.run_id,
        event_type="created",
        message=f"Delivery run created for {project_path}",
        payload={"scheme": scheme, "configuration": configuration},
    )
    return run


# ── Phase: inspect ──────────────────────────────────────────────────


def inspect_project(run: IOSDeliveryRun) -> IOSDeliveryRun:
    """Populate project metadata from Xcode intelligence.

    Resolves the xcodeproj/xcworkspace, default scheme, bundle id,
    and signing mode so subsequent phases know what to build.
    """
    if _check_cancelled(run):
        return run

    run.phase = DeliveryPhase.INSPECTING.value
    save_run(run)
    append_event(run.run_id, event_type="phase", message="Inspecting project")

    try:
        from boss.intelligence.xcode import inspect_xcode_project

        info = inspect_xcode_project(run.project_path)

        run.xcodeproj_path = info.xcodeproj_path
        run.xcworkspace_path = info.xcworkspace_path

        # Auto-select scheme if not specified
        if not run.scheme and info.schemes:
            # Prefer the scheme whose name matches the app target
            app = info.likely_app_target
            if app:
                matching = [s for s in info.schemes if s.name == app.name]
                run.scheme = matching[0].name if matching else info.schemes[0].name
            else:
                run.scheme = info.schemes[0].name

        # Fill in target metadata from the likely app target
        app = info.likely_app_target
        if app:
            if not run.bundle_identifier:
                run.bundle_identifier = app.bundle_identifier
            if app.signing_style:
                run.signing_mode = (
                    SigningMode.AUTOMATIC.value
                    if app.signing_style.lower() == "automatic"
                    else SigningMode.MANUAL.value
                )
            if app.team_id:
                run.team_id = app.team_id

        run.metadata["inspect"] = {
            "targets": len(info.targets),
            "schemes": [s.name for s in info.schemes],
            "build_configurations": info.build_configurations,
            "errors": info.errors,
        }

        if info.errors:
            run.metadata["inspect_warnings"] = info.errors

        # If inspection found nothing usable, fail explicitly so the
        # pipeline doesn't continue into archive with no scheme.
        if not run.xcodeproj_path and not run.xcworkspace_path:
            run.phase = DeliveryPhase.FAILED.value
            run.error = "No Xcode project or workspace found"
            append_event(
                run.run_id,
                event_type="inspect_not_applicable",
                message=run.error,
            )
            save_run(run)
            return run

        append_event(
            run.run_id,
            event_type="inspect_done",
            message=f"Found scheme={run.scheme} bundle={run.bundle_identifier}",
            payload=run.metadata.get("inspect", {}),
        )

    except Exception as exc:
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Inspect failed: {exc}"
        append_event(
            run.run_id,
            event_type="error",
            message=str(exc),
        )
        save_run(run)
        return run

    # Supplement team_id from signing config if the project didn't provide one
    if not run.team_id:
        from boss.ios_delivery.signing import ConfigFileCorrupt, load_signing_config
        try:
            signing_cfg = load_signing_config()
        except ConfigFileCorrupt:
            signing_cfg = None
        if signing_cfg and signing_cfg.team_id:
            run.team_id = signing_cfg.team_id
            append_event(
                run.run_id,
                event_type="signing_config",
                message=f"team_id {signing_cfg.team_id} loaded from ios-signing.json",
            )

    save_run(run)
    return run


# ── Phase: archive ──────────────────────────────────────────────────


def archive_build(run: IOSDeliveryRun) -> IOSDeliveryRun:
    """Run ``xcodebuild archive`` through the governed runner.

    Uses the toolchain module to detect available tools and construct
    the command, then executes through the Boss Runner.  Build failures
    are parsed into structured diagnostics.
    """
    if _check_cancelled(run):
        return run

    run.phase = DeliveryPhase.ARCHIVING.value
    save_run(run)
    append_event(run.run_id, event_type="phase", message="Archiving build")

    # --- Pre-flight checks ---
    if not run.scheme:
        run.phase = DeliveryPhase.FAILED.value
        run.error = "Cannot archive: no scheme resolved"
        save_run(run)
        return run

    from boss.ios_delivery.toolchain import (
        build_archive_command,
        build_fastlane_archive_command,
        get_toolchain,
        parse_build_log,
        summarize_build_failure,
    )

    toolchain = get_toolchain()
    run.metadata["toolchain"] = toolchain.to_dict()

    if not toolchain.can_build:
        run.phase = DeliveryPhase.FAILED.value
        run.error = "xcodebuild is not available — install Xcode Command Line Tools"
        append_event(
            run.run_id,
            event_type="toolchain_missing",
            message=run.error,
            payload=toolchain.to_dict(),
        )
        save_run(run)
        return run

    # Determine archive output path
    archive_dir = Path(run.project_path) / "build" / "archives"
    archive_path = archive_dir / f"{run.scheme}.xcarchive"
    run.archive_path = str(archive_path)

    # Build the command — prefer xcodebuild, fall back to fastlane
    use_fastlane = not toolchain.xcodebuild.available and toolchain.has_fastlane
    if use_fastlane:
        cmd = build_fastlane_archive_command(
            workspace=run.xcworkspace_path,
            project=run.xcodeproj_path,
            scheme=run.scheme,
            configuration=run.configuration,
            output_directory=str(archive_dir),
            export_method=run.export_method,
        )
    else:
        cmd = build_archive_command(
            workspace=run.xcworkspace_path,
            project=run.xcodeproj_path,
            scheme=run.scheme,
            configuration=run.configuration,
            archive_path=str(archive_path),
        )

    run.metadata["archive_command"] = cmd

    append_event(
        run.run_id,
        event_type="archive_command",
        message=f"Executing: {' '.join(cmd[:6])}...",
        payload={"command": cmd, "archive_path": run.archive_path},
    )

    # Execute through governed runner
    from boss.ios_delivery.runner import run_build_command

    result = run_build_command(cmd, cwd=run.project_path, run_id=run.run_id)

    run.build_log = result.output
    run.metadata["archive_result"] = result.to_dict()

    # Re-check cancellation after the subprocess finishes — if the
    # process was killed by cancel_run() the exit code will be negative
    # (e.g. -15 SIGTERM) but the run should stay cancelled, not failed.
    if _check_cancelled(run):
        return run

    if result.exit_code is None and result.denied_reason:
        # Policy denied the command
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Build command denied: {result.denied_reason}"
        append_event(
            run.run_id,
            event_type="archive_denied",
            message=run.error,
            payload=result.to_dict(),
        )
        save_run(run)
        return run

    if not result.success:
        run.phase = DeliveryPhase.FAILED.value
        failure_summary = summarize_build_failure(result.output)
        run.metadata["build_failure"] = failure_summary
        if failure_summary["is_signing_failure"]:
            run.error = "Archive failed: code signing error"
        elif failure_summary["is_compilation_failure"]:
            run.error = f"Archive failed: {failure_summary['error_count']} compilation error(s)"
        elif failure_summary["is_linking_failure"]:
            run.error = "Archive failed: linker error"
        else:
            run.error = f"Archive failed (exit code {result.exit_code})"
        append_event(
            run.run_id,
            event_type="archive_failed",
            message=run.error,
            payload=failure_summary,
        )
        save_run(run)
        return run

    # Check if archive actually exists
    if not Path(run.archive_path).exists():
        logger.warning(
            "xcodebuild reported success but archive not found at %s",
            run.archive_path,
        )
        # Don't fail — xcodebuild may have placed it at a slightly
        # different path; the export phase will catch missing archives.

    # Look for dSYMs inside the archive
    dsym_dir = Path(run.archive_path) / "dSYMs"
    if dsym_dir.exists():
        dsyms = list(dsym_dir.glob("*.dSYM"))
        if dsyms:
            run.dsym_path = str(dsyms[0])

    append_event(
        run.run_id,
        event_type="archive_done",
        message=f"Archive completed in {result.duration_ms:.0f}ms",
        payload={
            "archive_path": run.archive_path,
            "dsym_path": run.dsym_path,
            "duration_ms": result.duration_ms,
        },
    )

    save_run(run)
    return run


# ── Phase: export ───────────────────────────────────────────────────


def export_archive(run: IOSDeliveryRun) -> IOSDeliveryRun:
    """Run ``xcodebuild -exportArchive`` to produce the signed IPA.

    Writes the ExportOptions.plist, then executes the export command
    through the governed runner.  On success, scans the output directory
    for .ipa and .dSYM files.
    """
    if _check_cancelled(run):
        return run

    run.phase = DeliveryPhase.EXPORTING.value
    save_run(run)
    append_event(run.run_id, event_type="phase", message="Exporting archive")

    if not run.archive_path:
        run.phase = DeliveryPhase.FAILED.value
        run.error = "Cannot export: no archive path"
        save_run(run)
        return run

    if not Path(run.archive_path).exists():
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Cannot export: archive not found at {run.archive_path}"
        save_run(run)
        return run

    export_dir = Path(run.project_path) / "build" / "export"

    # Write ExportOptions.plist
    plist_path = Path(run.project_path) / "build" / "ExportOptions.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    opts = export_options_dict(run)
    with open(plist_path, "wb") as fp:
        plistlib.dump(opts, fp)
    run.metadata["export_options"] = opts

    from boss.ios_delivery.toolchain import (
        build_export_command,
        parse_build_log,
        summarize_build_failure,
    )

    cmd = build_export_command(
        archive_path=run.archive_path,
        export_path=str(export_dir),
        export_options_plist=str(plist_path),
    )
    run.metadata["export_command"] = cmd

    append_event(
        run.run_id,
        event_type="export_command",
        message=f"Executing: {' '.join(cmd[:6])}...",
        payload={"command": cmd, "export_path": str(export_dir)},
    )

    from boss.ios_delivery.runner import run_build_command

    result = run_build_command(cmd, cwd=run.project_path, run_id=run.run_id)
    run.metadata["export_result"] = result.to_dict()

    # Re-check cancellation after subprocess finishes
    if _check_cancelled(run):
        return run

    if result.exit_code is None and result.denied_reason:
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Export command denied: {result.denied_reason}"
        append_event(
            run.run_id,
            event_type="export_denied",
            message=run.error,
            payload=result.to_dict(),
        )
        save_run(run)
        return run

    if not result.success:
        run.phase = DeliveryPhase.FAILED.value
        failure_summary = summarize_build_failure(result.output)
        run.metadata["export_failure"] = failure_summary
        if failure_summary["is_signing_failure"]:
            run.error = "Export failed: code signing error"
        else:
            run.error = f"Export failed (exit code {result.exit_code})"
        append_event(
            run.run_id,
            event_type="export_failed",
            message=run.error,
            payload=failure_summary,
        )
        save_run(run)
        return run

    # Scan export directory for IPA and dSYM
    if export_dir.exists():
        ipas = list(export_dir.glob("*.ipa"))
        if ipas:
            run.ipa_path = str(ipas[0])
        dsyms = list(export_dir.glob("*.dSYM"))
        if dsyms and not run.dsym_path:
            run.dsym_path = str(dsyms[0])

    append_event(
        run.run_id,
        event_type="export_done",
        message=f"Export completed in {result.duration_ms:.0f}ms",
        payload={
            "ipa_path": run.ipa_path,
            "dsym_path": run.dsym_path,
            "duration_ms": result.duration_ms,
        },
    )

    save_run(run)
    return run


def export_options_dict(run: IOSDeliveryRun) -> dict[str, Any]:
    """Build the exportOptionsPlist content for a run."""
    opts: dict[str, Any] = {"method": run.export_method}
    if run.team_id:
        opts["teamID"] = run.team_id
    if run.signing_mode == SigningMode.AUTOMATIC.value:
        opts["signingStyle"] = "automatic"
    elif run.signing_mode == SigningMode.MANUAL.value:
        opts["signingStyle"] = "manual"
    if run.upload_target == UploadTarget.TESTFLIGHT.value:
        opts["uploadSymbols"] = True
    return opts


# ── Phase: upload ───────────────────────────────────────────────────


def upload_artifact(run: IOSDeliveryRun) -> IOSDeliveryRun:
    """Upload the exported IPA to TestFlight / App Store Connect.

    Uses ``fastlane pilot upload`` when available with API key JSON,
    otherwise falls back to ``xcrun altool --upload-app``.  Both paths
    use App Store Connect API key authentication — never Apple ID
    passwords.

    The upload is executed through the governed runner, so it is
    subject to the same policy checks as build commands.
    """
    if _check_cancelled(run):
        return run

    if run.upload_target == UploadTarget.NONE.value:
        # Nothing to upload — skip straight to completed
        run.phase = DeliveryPhase.COMPLETED.value
        save_run(run)
        return run

    run.phase = DeliveryPhase.UPLOADING.value
    save_run(run)
    append_event(run.run_id, event_type="phase", message="Uploading artifact")

    if not run.ipa_path:
        run.phase = DeliveryPhase.FAILED.value
        run.error = "Cannot upload: no IPA path"
        run.upload_status = UploadStatus.FAILED.value
        save_run(run)
        return run

    if not Path(run.ipa_path).exists():
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Cannot upload: IPA not found at {run.ipa_path}"
        run.upload_status = UploadStatus.FAILED.value
        save_run(run)
        return run

    # ── Credential check ──
    from boss.ios_delivery.upload import (
        execute_upload,
        resolve_upload_plan,
        validate_upload_credentials,
    )

    run.upload_status = UploadStatus.CREDENTIAL_CHECK.value
    save_run(run)

    creds_ok, creds_detail = validate_upload_credentials(run)
    if not creds_ok:
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Upload credential check failed: {creds_detail}"
        run.upload_status = UploadStatus.FAILED.value
        append_event(
            run.run_id,
            event_type="upload_credential_failed",
            message=creds_detail,
        )
        save_run(run)
        return run

    # ── Resolve strategy ──
    plan = resolve_upload_plan(run)
    if plan is None:
        run.phase = DeliveryPhase.FAILED.value
        run.error = (
            "No viable upload path: need fastlane with API key JSON "
            "or xcrun altool with API key configured"
        )
        run.upload_status = UploadStatus.FAILED.value
        save_run(run)
        return run

    run.metadata["upload_strategy"] = plan.strategy.value
    run.metadata["upload_command_preview"] = " ".join(plan.command[:5]) + "..."
    run.upload_method = plan.method.value

    # Re-check cancellation before the long upload
    if _check_cancelled(run):
        return run

    # ── Execute upload ──
    upload_result = execute_upload(run, plan)
    run.metadata["upload_result"] = upload_result.to_dict()

    # Re-check cancellation after subprocess finishes
    if _check_cancelled(run):
        return run

    if upload_result.exit_code is None and upload_result.error_detail:
        # Policy denied
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Upload command denied: {upload_result.error_detail}"
        run.upload_status = UploadStatus.FAILED.value
        append_event(
            run.run_id,
            event_type="upload_denied",
            message=run.error,
        )
        save_run(run)
        return run

    if not upload_result.success:
        run.phase = DeliveryPhase.FAILED.value
        run.error = f"Upload failed: {upload_result.error_detail or 'unknown error'}"
        run.upload_status = UploadStatus.FAILED.value
        run.upload_log = (upload_result.stdout + "\n" + upload_result.stderr).strip()
        append_event(
            run.run_id,
            event_type="upload_failed",
            message=run.error,
            payload=upload_result.to_dict(),
        )
        save_run(run)
        return run

    # ── Upload succeeded ──
    run.upload_id = upload_result.upload_id
    run.upload_finished_at = time.time()
    run.upload_log = (upload_result.stdout + "\n" + upload_result.stderr).strip()

    # If fastlane pilot waited for processing, it may have confirmed ready
    if (
        plan.strategy.value == "fastlane_pilot"
        and "successfully" in upload_result.stdout.lower()
    ):
        run.upload_status = UploadStatus.READY.value
        run.phase = DeliveryPhase.COMPLETED.value
    else:
        # Uploaded but processing not yet confirmed — keep phase as
        # UPLOADING so the run stays in active_runs until processing
        # completes.  Only move to COMPLETED when we confirm readiness.
        run.upload_status = UploadStatus.PROCESSING.value
        run.phase = DeliveryPhase.UPLOADING.value

    append_event(
        run.run_id,
        event_type="upload_done",
        message=f"Upload completed in {upload_result.duration_ms:.0f}ms",
        payload={
            "strategy": plan.strategy.value,
            "upload_id": run.upload_id,
            "upload_status": run.upload_status,
            "duration_ms": upload_result.duration_ms,
        },
    )

    save_run(run)
    return run


# ── Full pipeline ───────────────────────────────────────────────────


def run_full_pipeline(run: IOSDeliveryRun) -> IOSDeliveryRun:
    """Execute all phases sequentially, stopping on failure or cancellation.

    This is the main orchestration entry point.  Each phase checks for
    cancellation at its boundary.
    """
    phases = [inspect_project, archive_build, export_archive, upload_artifact]
    for phase_fn in phases:
        run = phase_fn(run)
        if run.is_terminal:
            break

    if run.phase not in (
        DeliveryPhase.COMPLETED.value,
        DeliveryPhase.FAILED.value,
        DeliveryPhase.CANCELLED.value,
    ):
        # Pipeline completed all phases without failure — mark done
        run.phase = DeliveryPhase.COMPLETED.value

    import time as _time
    run.finished_at = _time.time()
    save_run(run)
    append_event(
        run.run_id,
        event_type="finished",
        message=f"Pipeline finished: {run.phase}",
        payload={"error": run.error},
    )
    return run


# ── Diagnostics ─────────────────────────────────────────────────────


def delivery_status() -> dict[str, Any]:
    """Return a summary suitable for the diagnostics / status endpoint."""
    from boss.ios_delivery.signing import check_signing_readiness
    from boss.ios_delivery.state import list_runs

    runs = list_runs(limit=20)
    active = [r for r in runs if not r.is_terminal]
    recent_completed = [r for r in runs if r.is_terminal][:10]

    readiness = check_signing_readiness()

    return {
        "active_runs": [r.to_dict() for r in active],
        "recent_completed": [r.to_dict() for r in recent_completed],
        "total_runs": len(runs),
        "signing": readiness.to_dict(),
    }
