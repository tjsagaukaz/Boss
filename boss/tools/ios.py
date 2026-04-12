"""iOS / Xcode project intelligence and delivery tools.

Read-only tools let agents inspect Apple project structure.
Delivery tools let agents create and start iOS delivery pipeline runs
through the governed tool system.
"""

from __future__ import annotations

import json

from boss.execution import ExecutionType, display_value, governed_function_tool, scope_value


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Inspect Xcode Project",
    describe_call=lambda params: f'Inspect Xcode project at {params.get("project_path", ".")}',
    scope_key=lambda _params: scope_value("ios", "inspect"),
    scope_label=lambda _params: "Xcode project inspection",
)
def inspect_xcode_project(project_path: str) -> str:
    """Inspect an Xcode / iOS project and return its structure.

    Discovers targets, build configurations, signing hints, bundle IDs,
    schemes, Info.plist files, and entitlements.

    Args:
        project_path: Path to the project directory containing .xcodeproj.
    """
    from boss.intelligence.xcode import inspect_xcode_project as _inspect

    info = _inspect(project_path)
    return info.summary()


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="List Xcode Schemes",
    describe_call=lambda params: f'List schemes for {params.get("project_path", ".")}',
    scope_key=lambda _params: scope_value("ios", "schemes"),
    scope_label=lambda _params: "Xcode scheme listing",
)
def list_xcode_schemes(project_path: str) -> str:
    """List build schemes discovered in an Xcode project.

    Args:
        project_path: Path to the project directory.
    """
    from boss.intelligence.xcode import inspect_xcode_project as _inspect

    info = _inspect(project_path)
    if not info.schemes:
        return f"No shared schemes found in {project_path}. Schemes may be user-local or auto-generated."

    lines = [f"Schemes ({len(info.schemes)}):"]
    for s in info.schemes:
        parts = [f"  - {s.name}"]
        if s.build_targets:
            parts.append(f"    Build: {', '.join(s.build_targets)}")
        if s.test_targets:
            parts.append(f"    Test: {', '.join(s.test_targets)}")
        if s.launch_target:
            parts.append(f"    Launch: {s.launch_target}")
        lines.extend(parts)
    return "\n".join(lines)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Summarize iOS Project",
    describe_call=lambda params: f'Summarize iOS project at {params.get("project_path", ".")}',
    scope_key=lambda _params: scope_value("ios", "summary"),
    scope_label=lambda _params: "iOS project summary",
)
def summarize_ios_project(project_path: str) -> str:
    """Get a delivery-focused summary of an iOS project: app target, bundle ID,
    signing, test targets, and TestFlight readiness hints.

    Args:
        project_path: Path to the project directory.
    """
    from pathlib import Path as _Path

    from boss.intelligence.xcode import (
        extract_plist_summary,
        inspect_xcode_project as _inspect,
        read_entitlements,
        read_info_plist,
        summarize_entitlements,
    )

    info = _inspect(project_path)
    lines: list[str] = []

    app = info.likely_app_target
    if app:
        lines.append(f"App target: {app.name}")
        if app.bundle_identifier:
            lines.append(f"Bundle ID: {app.bundle_identifier}")
        if app.signing_style:
            lines.append(f"Signing style: {app.signing_style}")
        if app.team_id:
            lines.append(f"Team ID: {app.team_id}")
        if app.build_configurations:
            lines.append(f"Build configurations: {', '.join(app.build_configurations)}")
    else:
        lines.append("No app target found.")

    test_targets = info.test_targets
    if test_targets:
        lines.append(f"\nTest targets: {', '.join(t.name for t in test_targets)}")

    # Info.plist summary
    if app and app.info_plist_file:
        plist_path = _Path(project_path) / app.info_plist_file
        if plist_path.exists():
            plist = read_info_plist(plist_path)
            summary = extract_plist_summary(plist)
            if summary:
                lines.append(f"\nInfo.plist ({app.info_plist_file}):")
                for k, v in summary.items():
                    lines.append(f"  {k}: {v}")

    # Entitlements
    if app and app.entitlements_file:
        ent_path = _Path(project_path) / app.entitlements_file
        if ent_path.exists():
            ent = read_entitlements(ent_path)
            caps = summarize_entitlements(ent)
            if caps:
                lines.append(f"\nCapabilities: {', '.join(caps)}")

    # TestFlight readiness hints
    lines.append("\nTestFlight readiness:")
    issues: list[str] = []
    if not app:
        issues.append("No app target identified")
    elif not app.bundle_identifier:
        issues.append("No bundle identifier set")
    if not app or not app.signing_style:
        issues.append("Signing style not detected (check build settings)")
    if not app or not app.team_id:
        issues.append("No development team configured")
    if not info.schemes:
        issues.append("No shared schemes (xcodebuild may need -scheme)")

    if issues:
        for issue in issues:
            lines.append(f"  ⚠ {issue}")
    else:
        lines.append("  ✓ Basic configuration looks complete")

    if info.errors:
        lines.append(f"\nWarnings: {'; '.join(info.errors)}")

    return "\n".join(lines)


# ── Delivery pipeline tools ─────────────────────────────────────────


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Start iOS Delivery",
    describe_call=lambda params: (
        f'Start iOS delivery pipeline for {params.get("project_path", "project")}'
        + (f' scheme={params.get("scheme")}' if params.get("scheme") else "")
        + (f' → {params.get("upload_target")}' if params.get("upload_target", "none") != "none" else "")
    ),
    scope_key=lambda params: scope_value("ios-delivery", "run"),
    scope_label=lambda params: display_value(
        params.get("project_path"), fallback="iOS delivery run"
    ),
)
def start_ios_delivery(
    project_path: str,
    scheme: str = "",
    configuration: str = "Release",
    export_method: str = "app-store",
    upload_target: str = "none",
) -> str:
    """Create and start an iOS delivery pipeline run.

    The pipeline runs: inspect → archive → export → optional upload.
    Archive and export execute xcodebuild through governed subprocess
    execution. Upload uses fastlane pilot or xcrun altool with App
    Store Connect API key authentication.

    The run executes in the background. Use ios_delivery_status to
    check progress.

    Args:
        project_path: Path to the directory containing the Xcode project.
        scheme: Build scheme. Auto-detected if empty.
        configuration: Build configuration (Release or Debug).
        export_method: Export method (app-store, ad-hoc, development, enterprise).
        upload_target: Upload destination (none, testflight, app-store-connect).
    """
    import contextvars
    import threading

    from boss.ios_delivery.engine import create_run, run_full_pipeline
    from boss.runner.engine import _current_runner_var, get_runner

    # Apply the same validation the API endpoint uses — reject blank paths.
    project_path = project_path.strip()
    if not project_path:
        return "Cannot create delivery run: project_path is required"

    try:
        run = create_run(
            project_path=project_path,
            scheme=scheme or None,
            configuration=configuration,
            export_method=export_method,
            upload_target=upload_target,
        )
    except ValueError as exc:
        return f"Cannot create delivery run: {exc}"

    # Establish runner governance for the background thread, then restore
    # the caller's runner so this tool doesn't leave the context elevated.
    prev_runner = _current_runner_var.get(None)
    get_runner(mode="deploy", workspace_root=project_path)
    ctx = contextvars.copy_context()
    _current_runner_var.set(prev_runner)

    def _run_pipeline() -> None:
        try:
            run_full_pipeline(run)
        except Exception:
            import logging
            import traceback
            logging.getLogger("boss.tools.ios").exception(
                "Pipeline execution failed for run %s", run.run_id
            )
            from boss.ios_delivery.state import DeliveryPhase, save_run
            run.phase = DeliveryPhase.FAILED.value
            run.error = f"Pipeline crashed: {traceback.format_exc(limit=3)}"
            run.updated_at = __import__('time').time()
            save_run(run)

    threading.Thread(
        target=ctx.run, args=(_run_pipeline,),
        daemon=True, name=f"ios-delivery-{run.run_id}",
    ).start()

    parts = [f"iOS delivery run started: {run.run_id}"]
    parts.append(f"Project: {project_path}")
    if run.scheme:
        parts.append(f"Scheme: {run.scheme}")
    parts.append(f"Configuration: {configuration}")
    parts.append(f"Export method: {export_method}")
    if upload_target != "none":
        parts.append(f"Upload target: {upload_target}")
    parts.append("")
    parts.append("The pipeline is running in the background (inspect → archive → export"
                  + (" → upload" if upload_target != "none" else "") + ").")
    parts.append("Use ios_delivery_status to check progress.")
    return "\n".join(parts)


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="iOS Delivery Status",
    describe_call=lambda _params: "Check iOS delivery pipeline status",
    scope_key=lambda _params: scope_value("ios-delivery", "status"),
    scope_label=lambda _params: "iOS delivery status",
)
def ios_delivery_status(run_id: str = "") -> str:
    """Check the status of iOS delivery pipeline runs.

    Without a run_id, returns an overview of all active and recent runs.
    With a run_id, returns detailed status for that specific run.
    If the run is in an upload-processing state, this also triggers the
    same status-transition check the UI uses, so processing→ready
    transitions are observed.

    Args:
        run_id: Optional run ID for detailed status. Empty for overview.
    """
    if run_id:
        from boss.ios_delivery.state import load_run

        run = load_run(run_id)
        if run is None:
            return f"No delivery run found with ID: {run_id}"

        # Drive the same upload status-transition check the UI uses.
        # Without this, the agent would only see stale persisted state
        # and never observe processing → ready.
        # Only check when already "processing" — polling during "uploading"
        # would prematurely mutate altool runs to processing.
        if run.upload_status == "processing":
            from boss.ios_delivery.upload import check_processing_status

            processing_result = check_processing_status(run)
            # Reload in case check_processing_status persisted a transition
            reloaded = load_run(run_id)
            if reloaded is not None:
                run = reloaded

        lines = [f"Run {run.run_id}"]
        lines.append(f"Phase: {run.phase}")
        lines.append(f"Project: {run.project_path}")
        if run.scheme:
            lines.append(f"Scheme: {run.scheme}")
        lines.append(f"Configuration: {run.configuration}")
        if run.bundle_identifier:
            lines.append(f"Bundle ID: {run.bundle_identifier}")
        if run.archive_path:
            lines.append(f"Archive: {run.archive_path}")
        if run.ipa_path:
            lines.append(f"IPA: {run.ipa_path}")
        if run.upload_target != "none":
            lines.append(f"Upload target: {run.upload_target}")
            lines.append(f"Upload status: {run.upload_status}")
            if run.upload_method != "none":
                lines.append(f"Upload method: {run.upload_method}")
        if run.error:
            lines.append(f"Error: {run.error}")
        return "\n".join(lines)

    from boss.ios_delivery.engine import delivery_status

    status = delivery_status()
    active = status["active_runs"]
    recent = status["recent_completed"]
    signing = status["signing"]

    lines = ["iOS Delivery Status"]
    lines.append(f"Total runs: {status['total_runs']}")
    lines.append(f"Signing: can_sign={signing.get('can_sign', False)}, "
                 f"can_upload={signing.get('can_upload', False)}")

    if active:
        lines.append(f"\nActive runs ({len(active)}):")
        for r in active:
            label = r.get("scheme") or r.get("project_path", "?").rsplit("/", 1)[-1]
            lines.append(f"  [{r['phase']}] {label} — {r['run_id'][:12]}")
    else:
        lines.append("\nNo active runs.")

    if recent:
        lines.append(f"\nRecent completed ({len(recent)}):")
        for r in recent[:5]:
            label = r.get("scheme") or r.get("project_path", "?").rsplit("/", 1)[-1]
            lines.append(f"  [{r['phase']}] {label} — {r['run_id'][:12]}")

    return "\n".join(lines)


@governed_function_tool(
    execution_type=ExecutionType.RUN,
    title="Resume iOS Delivery",
    describe_call=lambda params: f'Resume failed iOS delivery run {params.get("run_id", "?")}',
    scope_key=lambda _params: scope_value("ios-delivery", "resume"),
    scope_label=lambda _params: "Resume failed iOS delivery run",
)
def resume_ios_delivery(run_id: str) -> str:
    """Resume a previously failed iOS delivery pipeline run.

    Restarts execution from the phase that failed.  Each run can be
    resumed up to 2 times to prevent infinite retry loops.

    Args:
        run_id: The ID of the failed delivery run to resume.
    """
    import contextvars
    import threading

    from boss.ios_delivery.engine import resume_pipeline
    from boss.ios_delivery.state import load_run
    from boss.runner.engine import _current_runner_var, get_runner

    run = load_run(run_id)
    if run is None:
        return f"No delivery run found with ID: {run_id}"

    if run.phase != "failed":
        return f"Run {run_id} is in phase '{run.phase}', not 'failed'. Only failed runs can be resumed."

    if run.retry_count >= 2:
        return f"Run {run_id} has already been retried {run.retry_count} times. Create a new delivery run instead."

    prev_runner = _current_runner_var.get(None)
    get_runner(mode="deploy", workspace_root=run.project_path)
    ctx = contextvars.copy_context()
    _current_runner_var.set(prev_runner)

    def _run_resume() -> None:
        try:
            resume_pipeline(run)
        except Exception:
            import logging
            import traceback
            logging.getLogger("boss.tools.ios").exception(
                "Resume execution failed for run %s", run.run_id
            )
            from boss.ios_delivery.state import DeliveryPhase, save_run
            run.phase = DeliveryPhase.FAILED.value
            run.error = f"Resume crashed: {traceback.format_exc(limit=3)}"
            run.updated_at = __import__('time').time()
            save_run(run)

    threading.Thread(
        target=ctx.run, args=(_run_resume,),
        daemon=True, name=f"ios-resume-{run.run_id}",
    ).start()

    failed_phase = run.metadata.get("failed_at_phase", "unknown")
    return (
        f"Resuming iOS delivery run {run_id} from {failed_phase} "
        f"(attempt {run.retry_count + 1}/3).\n"
        f"Use ios_delivery_status to check progress."
    )
