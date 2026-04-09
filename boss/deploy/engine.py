"""Deploy engine: orchestrates build, deploy, and teardown workflows."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from boss.deploy.adapters import best_adapter_for, get_adapter, available_adapters, all_adapters
from boss.deploy.state import (
    Deployment,
    DeploymentStatus,
    DeploymentTarget,
    list_deployments,
    load_deployment,
    new_deployment_id,
    save_deployment,
)

logger = logging.getLogger(__name__)

# ── Cancellation registry ───────────────────────────────────────────
# Tracks deployment IDs that have been cancelled while in-flight.
# The pipeline checks this between phases and aborts early.

_cancel_lock = threading.Lock()
_cancelled_ids: set[str] = set()

# ── Live process registry (for real cancellation) ───────────────────
# Maps deployment_id → subprocess.Popen so cancel_deployment() can
# terminate the in-flight build/deploy command, not just wait for the
# next phase boundary.

_process_lock = threading.Lock()
_live_processes: dict[str, subprocess.Popen[str]] = {}


def _mark_cancelled(deployment_id: str) -> None:
    with _cancel_lock:
        _cancelled_ids.add(deployment_id)


def _clear_cancelled(deployment_id: str) -> None:
    with _cancel_lock:
        _cancelled_ids.discard(deployment_id)


def is_cancelled(deployment_id: str) -> bool:
    with _cancel_lock:
        return deployment_id in _cancelled_ids


def register_deploy_process(deployment_id: str, proc: subprocess.Popen[str]) -> None:
    """Register a live subprocess for a deployment so it can be terminated on cancel."""
    with _process_lock:
        _live_processes[deployment_id] = proc


def unregister_deploy_process(deployment_id: str) -> None:
    """Remove a process from the live registry (call after process completes)."""
    with _process_lock:
        _live_processes.pop(deployment_id, None)


def _terminate_deploy_process(deployment_id: str) -> bool:
    """Terminate the live process for a deployment.  Returns True if a process was killed.

    Deploy processes are started via ``RunnerEngine.start_managed_process()``
    which creates a dedicated process group (``preexec_fn=os.setsid``).
    We kill the entire group so that child processes spawned by npm/npx
    are also cleaned up.
    """
    with _process_lock:
        proc = _live_processes.pop(deployment_id, None)
    if proc is None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)
        logger.info("Terminated deploy process group for %s (pid=%s, pgid=%s)", deployment_id, proc.pid, pgid)
        return True
    except (OSError, ProcessLookupError) as exc:
        logger.warning("Failed to terminate deploy process for %s: %s", deployment_id, exc)
        return False


def deploy_status() -> dict[str, Any]:
    """Return an overview of deploy subsystem state for the diagnostics surface."""
    from boss.config import settings as _settings

    adapters = all_adapters()
    configured = [a for a in adapters if a.is_configured()]
    recent = list_deployments(limit=20)
    live = [d for d in recent if d.status == DeploymentStatus.LIVE.value]

    return {
        "enabled": _settings.deploy_enabled and len(configured) > 0,
        "adapters": [a.status_payload() for a in adapters],
        "configured_count": len(configured),
        "recent_deployments": len(recent),
        "live_count": len(live),
    }


def create_deployment(
    *,
    project_path: str,
    session_id: str,
    adapter_name: str | None = None,
    target: str = DeploymentTarget.PREVIEW.value,
) -> Deployment:
    """Create a new deployment record.

    If *adapter_name* is None the engine picks the best configured
    adapter for the project.  Raises ValueError when no adapter is
    available or the project is not supported.
    """
    if adapter_name:
        adapter = get_adapter(adapter_name)
        if adapter is None:
            raise ValueError(f"Unknown deploy adapter: {adapter_name}")
        if not adapter.is_configured():
            raise ValueError(f"Adapter '{adapter_name}' is not configured — set the required credentials")
    else:
        adapter = best_adapter_for(project_path)
        if adapter is None:
            configured = available_adapters()
            if not configured:
                raise ValueError(
                    "No deployment adapters are configured. "
                    "Set VERCEL_TOKEN or NETLIFY_AUTH_TOKEN to enable deploys."
                )
            raise ValueError(
                f"No adapter supports this project ({project_path}). "
                f"Configured adapters: {', '.join(a.name for a in configured)}"
            )

    deploy = Deployment(
        deployment_id=new_deployment_id(),
        project_path=project_path,
        session_id=session_id,
        adapter=adapter.name,
        target=target,
    )
    save_deployment(deploy)
    return deploy


def run_deployment(deployment_id: str) -> Deployment:
    """Execute the full build → deploy pipeline for a deployment.

    The deployment must already exist (created via ``create_deployment``).
    This is synchronous — callers should run it in a background task.
    Checks the cancellation registry between phases and aborts early
    if ``cancel_deployment`` was called while the pipeline is running.

    Establishes a Runner context with ``full_access`` profile so all
    subprocess calls go through Boss Runner governance.  Deploy tokens
    are passed as CLI arguments rather than env vars, so env scrubbing
    does not affect authentication.
    """
    from boss.runner.engine import get_runner

    # Establish a governed runner context for the deploy pipeline.
    # full_access is appropriate because the approve gate already fired
    # at the API layer (requires approved=true).
    get_runner(mode="deploy")

    deploy = load_deployment(deployment_id)
    if deploy is None:
        raise ValueError(f"Deployment {deployment_id} not found")
    if deploy.is_terminal:
        raise ValueError(f"Deployment {deployment_id} is already terminal ({deploy.status})")

    adapter = get_adapter(deploy.adapter)
    if adapter is None:
        deploy.status = DeploymentStatus.FAILED.value
        deploy.error = f"Adapter '{deploy.adapter}' is no longer available"
        save_deployment(deploy)
        return deploy

    if not adapter.is_configured():
        deploy.status = DeploymentStatus.FAILED.value
        deploy.error = f"Adapter '{deploy.adapter}' lost its configuration"
        save_deployment(deploy)
        return deploy

    # Build phase.
    try:
        deploy = adapter.build(deploy)
    except Exception as exc:
        deploy.status = DeploymentStatus.FAILED.value
        deploy.error = f"Build exception: {exc}"
        save_deployment(deploy)
        return deploy

    if deploy.status == DeploymentStatus.FAILED.value:
        return deploy

    # Check for cancellation between build and deploy.
    if is_cancelled(deployment_id):
        _clear_cancelled(deployment_id)
        deploy.status = DeploymentStatus.CANCELLED.value
        deploy.finished_at = time.time()
        deploy.error = "Cancelled after build phase"
        save_deployment(deploy)
        logger.info("Deployment %s cancelled between build and deploy phases", deployment_id)
        return deploy

    # Deploy phase.
    try:
        deploy = adapter.deploy(deploy)
    except Exception as exc:
        deploy.status = DeploymentStatus.FAILED.value
        deploy.error = f"Deploy exception: {exc}"
        save_deployment(deploy)
        return deploy

    # If cancelled during deploy, honour the flag over the adapter result.
    if is_cancelled(deployment_id):
        _clear_cancelled(deployment_id)
        if deploy.status == DeploymentStatus.LIVE.value:
            # The deploy completed but was cancelled — mark as cancelled
            # so the user knows they need to tear it down manually.
            deploy.status = DeploymentStatus.CANCELLED.value
            deploy.finished_at = time.time()
            deploy.error = "Cancelled during deploy phase (deploy may have completed — check hosting platform)"
            save_deployment(deploy)
            logger.warning(
                "Deployment %s cancelled during deploy phase but adapter reported live", deployment_id
            )

    return deploy


def teardown_deployment(deployment_id: str) -> Deployment:
    """Tear down a live deployment."""
    deploy = load_deployment(deployment_id)
    if deploy is None:
        raise ValueError(f"Deployment {deployment_id} not found")

    adapter = get_adapter(deploy.adapter)
    if adapter is None:
        deploy.status = DeploymentStatus.TORN_DOWN.value
        deploy.finished_at = time.time()
        save_deployment(deploy)
        return deploy

    return adapter.teardown(deploy)


def cancel_deployment(deployment_id: str) -> Deployment:
    """Cancel a non-terminal deployment.

    If a build/deploy subprocess is currently running, it is terminated
    immediately.  If the pipeline is between phases, the cancellation
    flag causes it to abort at the next check.  If the deployment is
    still pending (not yet running), the status is flipped immediately.
    """
    deploy = load_deployment(deployment_id)
    if deploy is None:
        raise ValueError(f"Deployment {deployment_id} not found")
    if deploy.is_terminal:
        raise ValueError(f"Deployment {deployment_id} is already terminal ({deploy.status})")

    # Signal the in-flight pipeline to stop between phases.
    _mark_cancelled(deployment_id)

    # Kill any live subprocess immediately (real cancellation).
    killed = _terminate_deploy_process(deployment_id)

    deploy.status = DeploymentStatus.CANCELLED.value
    deploy.finished_at = time.time()
    if killed:
        deploy.error = "Cancelled — in-flight process terminated"
    save_deployment(deploy)
    logger.info("Deployment %s cancelled (process_killed=%s)", deployment_id, killed)
    return deploy
