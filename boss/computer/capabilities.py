"""Capability detection for the computer-use subsystem.

Reports whether the local environment has the tools needed to run
computer-use sessions: Playwright, a browser runtime, screenshot
support, and a compatible model configuration.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ComputerCapabilities:
    """Snapshot of what the local environment can support."""

    playwright_installed: bool = False
    playwright_browsers_installed: bool = False
    browser_executable: str | None = None
    screenshot_supported: bool = False
    computer_use_model_ready: bool = False
    computer_use_model: str | None = None
    details: dict[str, str] = field(default_factory=dict)

    @property
    def can_run_session(self) -> bool:
        return (
            self.playwright_installed
            and self.playwright_browsers_installed
            and self.screenshot_supported
            and self.computer_use_model_ready
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["can_run_session"] = self.can_run_session
        return d


def detect_capabilities() -> ComputerCapabilities:
    """Probe the local environment and return a capabilities snapshot."""
    caps = ComputerCapabilities()

    # -- Playwright --
    try:
        import playwright  # noqa: F401
        caps.playwright_installed = True
    except ImportError:
        caps.details["playwright"] = "not installed (pip install playwright)"

    if caps.playwright_installed:
        caps.playwright_browsers_installed = _check_playwright_browsers()
        if not caps.playwright_browsers_installed:
            caps.details["playwright_browsers"] = (
                "no browsers installed (python -m playwright install chromium)"
            )

    # -- Browser executable fallback (system Chromium / Chrome) --
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            caps.browser_executable = path
            break

    # -- Screenshot: Playwright provides this if installed --
    caps.screenshot_supported = caps.playwright_installed and caps.playwright_browsers_installed

    # -- Model readiness --
    try:
        from boss.config import settings
        # GPT-5.4 supports computer-use; check for an API key
        caps.computer_use_model = settings.code_model
        caps.computer_use_model_ready = bool(settings.cloud_api_key)
        if not caps.computer_use_model_ready:
            caps.details["model"] = "no OPENAI_API_KEY configured"
    except Exception as exc:
        caps.details["model"] = f"config error: {exc}"

    return caps


def _check_playwright_browsers() -> bool:
    """Return True if at least one Playwright browser is installed."""
    try:
        from playwright._impl._driver import compute_driver_executable  # type: ignore
        import subprocess

        driver = compute_driver_executable()
        result = subprocess.run(
            [str(driver), "install", "--dry-run"],
            capture_output=True, text=True, timeout=10,
        )
        # If dry-run exits 0 and mentions "already installed", we're good
        if result.returncode == 0:
            return True
        # Fallback: check if the chromium dir exists
        return _check_browser_dir_exists()
    except Exception:
        return _check_browser_dir_exists()


def _check_browser_dir_exists() -> bool:
    """Heuristic: check if Playwright's browser cache has content."""
    from pathlib import Path
    import sys

    if sys.platform == "darwin":
        browser_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        browser_dir = Path.home() / ".cache" / "ms-playwright"

    if browser_dir.is_dir():
        # At least one browser directory present
        return any(d.is_dir() for d in browser_dir.iterdir())
    return False
