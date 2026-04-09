"""Preview session lifecycle and capability detection."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PreviewStatus(str, Enum):
    """Current state of a preview session."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


class DetailMode(str, Enum):
    """Image detail level for multimodal capture."""

    AUTO = "auto"
    LOW = "low"
    HIGH = "high"
    ORIGINAL = "original"


class VerificationMethod(str, Enum):
    """How a preview was verified."""

    VISUAL = "visual"       # Screenshot sent to vision model
    TEXTUAL = "textual"     # DOM/errors only (no vision)
    SKIPPED = "skipped"     # Preview unavailable


@dataclass
class CaptureRegion:
    """Bounding box for region-focused capture or follow-up extraction."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CaptureRegion":
        return cls(
            x=data.get("x", 0),
            y=data.get("y", 0),
            width=data.get("width", 0),
            height=data.get("height", 0),
            label=data.get("label", ""),
        )

    @property
    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass
class PreviewCapabilities:
    """What preview tooling is available on this machine."""

    has_browser: bool = False
    browser_path: str | None = None
    has_playwright: bool = False
    has_node: bool = False
    has_swift_build: bool = False
    policy_enforced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def can_screenshot(self) -> bool:
        return self.has_playwright

    @property
    def can_preview(self) -> bool:
        return self.has_browser or self.has_node


@dataclass
class CaptureResult:
    """Result of a preview capture (screenshot, DOM, errors)."""

    screenshot_path: str | None = None
    dom_summary: str | None = None
    console_errors: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    page_title: str | None = None
    timestamp: float = field(default_factory=time.time)
    detail_mode: str = DetailMode.AUTO.value
    verification_method: str = VerificationMethod.SKIPPED.value
    region: dict | None = None
    policy_enforced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_errors(self) -> bool:
        return bool(self.console_errors) or bool(self.network_errors)

    def textual_summary(self) -> str:
        """Return a text-only summary suitable for non-vision models."""
        parts: list[str] = []
        if self.page_title:
            parts.append(f"Page title: {self.page_title}")
        if self.console_errors:
            parts.append(f"Console errors ({len(self.console_errors)}):")
            for err in self.console_errors[:10]:
                parts.append(f"  - {err}")
        if self.network_errors:
            parts.append(f"Network errors ({len(self.network_errors)}):")
            for err in self.network_errors[:10]:
                parts.append(f"  - {err}")
        if self.dom_summary:
            parts.append(f"DOM text:\n{self.dom_summary[:800]}")
        if not parts:
            parts.append("No capture data collected.")
        return "\n".join(parts)


@dataclass
class PreviewSession:
    """Tracks a running preview process."""

    session_id: str
    project_path: str
    url: str | None = None
    status: PreviewStatus = PreviewStatus.IDLE
    start_command: str | None = None
    pid: int | None = None
    started_at: float | None = None
    last_capture: CaptureResult | None = None
    error_message: str | None = None
    verification_method: str = VerificationMethod.SKIPPED.value
    policy_enforced: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "project_path": self.project_path,
            "url": self.url,
            "status": self.status.value,
            "start_command": self.start_command,
            "pid": self.pid,
            "started_at": self.started_at,
            "error_message": self.error_message,
            "verification_method": self.verification_method,
            "policy_enforced": self.policy_enforced,
        }
        if self.last_capture:
            d["last_capture"] = self.last_capture.to_dict()
        return d

    @property
    def is_running(self) -> bool:
        if self.status != PreviewStatus.RUNNING or self.pid is None:
            return False
        try:
            import os

            os.kill(self.pid, 0)
            return True
        except (OSError, ProcessLookupError):
            self.status = PreviewStatus.STOPPED
            return False


# ── Module-level session registry ───────────────────────────────────

_active_sessions: dict[str, PreviewSession] = {}


def get_active_session(project_path: str | None = None) -> PreviewSession | None:
    """Return the active preview session, optionally filtered by project."""
    if project_path:
        return _active_sessions.get(project_path)
    # Return most recent running session
    for session in reversed(list(_active_sessions.values())):
        if session.is_running:
            return session
    return next(iter(_active_sessions.values()), None) if _active_sessions else None


def register_session(session: PreviewSession) -> None:
    """Register a preview session in the active registry."""
    _active_sessions[session.project_path] = session


def remove_session(project_path: str) -> None:
    """Remove a session from the registry."""
    _active_sessions.pop(project_path, None)


def all_sessions() -> list[PreviewSession]:
    """Return all tracked preview sessions."""
    return list(_active_sessions.values())


# ── Capability detection ────────────────────────────────────────────

def detect_preview_capabilities() -> PreviewCapabilities:
    """Detect available preview tooling on this machine."""
    caps = PreviewCapabilities()

    # Browser detection (macOS)
    for browser in ("/Applications/Google Chrome.app", "/Applications/Safari.app", "/Applications/Firefox.app"):
        if Path(browser).exists():
            caps.has_browser = True
            caps.browser_path = browser
            break

    # Playwright
    if shutil.which("playwright") or _check_python_module("playwright"):
        caps.has_playwright = True

    # Node.js (needed for most dev servers)
    if shutil.which("node"):
        caps.has_node = True

    # Swift build (for SwiftUI previews / macOS apps)
    if shutil.which("swift"):
        caps.has_swift_build = True

    # Check if runner policy is available
    try:
        from boss.runner.engine import current_runner
        caps.policy_enforced = current_runner() is not None
    except ImportError:
        pass

    return caps


# ── Preview project detection ──────────────────────────────────────

def detect_preview_command(project_path: str) -> str | None:
    """Try to detect the right preview/dev-server command for a project."""
    root = Path(project_path)

    # package.json scripts
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            for cmd_name in ("dev", "start", "serve", "preview"):
                if cmd_name in scripts:
                    return f"npm run {cmd_name}"
        except (json.JSONDecodeError, OSError):
            pass

    # Python projects
    if (root / "manage.py").exists():
        return "python manage.py runserver"
    if (root / "app.py").exists():
        return "python app.py"

    # Swift / macOS
    if (root / "Package.swift").exists():
        return "swift build && swift run"

    return None


# ── Screenshot capture ──────────────────────────────────────────────

def capture_screenshot(
    url: str,
    output_path: str | Path,
    *,
    timeout_ms: int = 10_000,
    detail_mode: str = DetailMode.AUTO.value,
    region: CaptureRegion | None = None,
) -> CaptureResult:
    """Capture a screenshot and page diagnostics using Playwright if available.

    Falls back to a minimal result if Playwright is not installed.
    Runs through the Boss runner when a runner context is active.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = CaptureResult(
        detail_mode=detail_mode,
        region=region.to_dict() if region else None,
    )

    if not _check_python_module("playwright"):
        result.console_errors.append("Playwright not available — screenshot skipped")
        result.verification_method = VerificationMethod.SKIPPED.value
        return result

    try:
        script = _playwright_capture_script(url, str(output_path), timeout_ms)
        exec_result = _run_capture_command(
            ["python3", "-c", script],
            timeout=max(30, timeout_ms // 1000 + 10),
        )

        result.policy_enforced = exec_result.get("policy_enforced", False)

        stdout = exec_result.get("stdout", "")
        stderr = exec_result.get("stderr", "")
        returncode = exec_result.get("returncode", -1)

        if returncode == 0 and output_path.exists():
            result.screenshot_path = str(output_path)
            result.verification_method = VerificationMethod.TEXTUAL.value
            # Parse structured output from the script
            try:
                data = json.loads(stdout.strip())
                result.page_title = data.get("title")
                result.console_errors = data.get("console_errors", [])
                result.network_errors = data.get("network_errors", [])
                result.dom_summary = data.get("dom_summary")
            except (json.JSONDecodeError, ValueError):
                pass
        else:
            err_text = stderr.strip()[:500] if stderr else "Unknown error"
            result.console_errors.append(f"Screenshot capture failed: {err_text}")

    except subprocess.TimeoutExpired:
        result.console_errors.append(f"Screenshot capture timed out after {timeout_ms}ms")
    except FileNotFoundError:
        result.console_errors.append("python3 not found for Playwright capture")

    return result


def _run_capture_command(
    command: list[str],
    *,
    timeout: int = 30,
) -> dict:
    """Run a command through the runner if available, else direct subprocess."""
    try:
        from boss.runner.engine import current_runner
        runner = current_runner()
    except ImportError:
        runner = None

    if runner is not None:
        exec_result = runner.run_command(command, timeout=timeout)
        return {
            "returncode": exec_result.exit_code,
            "stdout": exec_result.stdout,
            "stderr": exec_result.stderr,
            "policy_enforced": True,
        }

    # Fallback: direct execution when no runner context
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "policy_enforced": False,
        }
    except subprocess.TimeoutExpired:
        raise
    except FileNotFoundError:
        raise


# ── Internal helpers ────────────────────────────────────────────────

def _check_python_module(module_name: str) -> bool:
    """Check if a Python module is importable without importing it."""
    try:
        result = subprocess.run(
            ["python3", "-c", f"import {module_name}"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _playwright_capture_script(url: str, output_path: str, timeout_ms: int) -> str:
    """Generate a self-contained Playwright capture script.

    The script captures a screenshot, page title, console errors,
    network errors, and a DOM text summary, then prints JSON to stdout.
    """
    # Sanitise inputs to prevent injection
    safe_url = url.replace("'", "\\'")
    safe_path = output_path.replace("'", "\\'")

    return f"""\
import json, sys
from playwright.sync_api import sync_playwright

console_errors = []
network_errors = []

def on_console(msg):
    if msg.type in ('error', 'warning'):
        console_errors.append(msg.text[:200])

def on_response(response):
    if response.status >= 400:
        network_errors.append(f"{{response.status}} {{response.url[:150]}}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on('console', on_console)
    page.on('response', on_response)
    try:
        page.goto('{safe_url}', timeout={timeout_ms}, wait_until='networkidle')
    except Exception as e:
        console_errors.append(f"Navigation error: {{str(e)[:200]}}")
    page.screenshot(path='{safe_path}', full_page=False)
    title = page.title()
    dom_text = page.evaluate('() => document.body ? document.body.innerText.slice(0, 1000) : ""')
    browser.close()
    print(json.dumps({{
        "title": title,
        "console_errors": console_errors[:20],
        "network_errors": network_errors[:20],
        "dom_summary": dom_text[:1000] if dom_text else None,
    }}))
"""
