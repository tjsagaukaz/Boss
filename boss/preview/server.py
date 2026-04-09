"""Preview server management — start, stop, and monitor dev servers."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from boss.preview.session import (
    PreviewSession,
    PreviewStatus,
    VerificationMethod,
    detect_preview_command,
    get_active_session,
    register_session,
    remove_session,
)


def start_preview(
    project_path: str,
    *,
    command: str | None = None,
    port: int | None = None,
    session_id: str | None = None,
) -> PreviewSession:
    """Start a dev server for the given project.

    When a Boss runner context is active the process is launched and
    managed entirely through the runner's ``start_managed_process`` so
    that command, path, network, and environment policies are enforced
    for the lifetime of the process — not just at launch time.
    """
    existing = get_active_session(project_path)
    if existing and existing.is_running:
        return existing

    resolved_command = command or detect_preview_command(project_path)
    if not resolved_command:
        session = PreviewSession(
            session_id=session_id or _generate_id(),
            project_path=project_path,
            status=PreviewStatus.FAILED,
            error_message="No preview command detected. Provide one explicitly.",
        )
        register_session(session)
        return session

    session = PreviewSession(
        session_id=session_id or _generate_id(),
        project_path=project_path,
        start_command=resolved_command,
        status=PreviewStatus.STARTING,
    )

    runner = _get_runner()

    if runner is not None:
        session.policy_enforced = True
        cmd_parts = resolved_command.split()

        # Network check
        net_verdict = runner.policy.check_network("localhost")
        if net_verdict.value == "denied":
            session.status = PreviewStatus.FAILED
            session.error_message = "Network access denied by runner policy"
            register_session(session)
            return session

        # Launch through the runner — all policy checks happen inside.
        proc, result = runner.start_managed_process(
            cmd_parts,
            cwd=project_path,
            shell=True,
        )

        if proc is None:
            session.status = PreviewStatus.FAILED
            session.error_message = (
                result.denied_reason
                or f"Preview command blocked by runner policy ({result.policy_profile})"
            )
            register_session(session)
            return session

        session.pid = proc.pid
        session.started_at = time.time()
        session.status = PreviewStatus.RUNNING

        url = _discover_url(proc, port=port, timeout=5.0)
        if url:
            session.url = url
        elif port:
            session.url = f"http://localhost:{port}"
        else:
            session.url = "http://localhost:3000"
    else:
        # No runner context — keep the existing raw Popen path.
        try:
            proc = subprocess.Popen(
                resolved_command,
                shell=True,
                cwd=project_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            session.pid = proc.pid
            session.started_at = time.time()
            session.status = PreviewStatus.RUNNING

            url = _discover_url(proc, port=port, timeout=5.0)
            if url:
                session.url = url
            elif port:
                session.url = f"http://localhost:{port}"
            else:
                session.url = "http://localhost:3000"
        except OSError as exc:
            session.status = PreviewStatus.FAILED
            session.error_message = str(exc)[:300]

    register_session(session)
    return session


def stop_preview(project_path: str) -> bool:
    """Stop a running preview session.

    When a Boss runner context is active the process is terminated
    through the runner's ``terminate_managed_process`` so that the
    kill operation is governed and recorded.
    """
    session = get_active_session(project_path)
    if not session:
        return False

    if session.pid:
        runner = _get_runner()
        if runner is not None:
            runner.terminate_managed_process(session.pid)
        else:
            try:
                os.killpg(os.getpgid(session.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    session.status = PreviewStatus.STOPPED
    remove_session(project_path)
    return True


def preview_status(project_path: str | None = None) -> dict:
    """Return status of preview sessions."""
    from boss.preview.session import all_sessions, detect_preview_capabilities

    caps = detect_preview_capabilities()
    sessions = all_sessions()

    if project_path:
        sessions = [s for s in sessions if s.project_path == project_path]

    # Check vision availability
    vision_available = False
    try:
        from boss.preview.vision import is_vision_available
        vision_available = is_vision_available()
    except ImportError:
        pass

    return {
        "capabilities": caps.to_dict(),
        "sessions": [s.to_dict() for s in sessions],
        "active_count": sum(1 for s in sessions if s.is_running),
        "vision_available": vision_available,
    }


def _discover_url(proc: subprocess.Popen, *, port: int | None, timeout: float) -> str | None:
    """Try to read the URL from the process's early stdout/stderr output."""
    import re
    import select

    url_pattern = re.compile(r"https?://[\w.:]+(?:/\S*)?")
    deadline = time.time() + timeout

    streams = [s for s in (proc.stdout, proc.stderr) if s is not None]
    if not streams:
        return None

    collected = ""
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        readable, _, _ = select.select(streams, [], [], min(remaining, 0.5))
        for stream in readable:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else b""
            if chunk:
                collected += chunk.decode("utf-8", errors="replace")

        match = url_pattern.search(collected)
        if match:
            return match.group(0)

        if proc.poll() is not None:
            break

    return None


def _generate_id() -> str:
    import uuid

    return f"preview-{uuid.uuid4().hex[:8]}"


def _get_runner():
    """Return the current runner context, or None if unavailable."""
    try:
        from boss.runner.engine import current_runner
        return current_runner()
    except ImportError:
        return None
