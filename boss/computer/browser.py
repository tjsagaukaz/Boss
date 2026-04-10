"""Browser harness for computer-use sessions.

Wraps Playwright to provide an isolated browser context with screenshot
capture and structured action execution.  All page content is treated as
untrusted input — no eval, no script injection, no trust of on-screen
instructions.

The harness is synchronous (uses ``playwright.sync_api``) because the
computer-use loop is already turn-based and Boss owns the scheduling.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HarnessError(Exception):
    """Raised when a harness operation fails."""


class HarnessNotReady(HarnessError):
    """The browser has not been launched or has been closed."""


class PlaywrightMissing(HarnessError):
    """Playwright is not installed."""


# ---------------------------------------------------------------------------
# Action dispatch result
# ---------------------------------------------------------------------------

@dataclass
class HarnessActionResult:
    """Result of a single action executed by the harness."""

    action_type: str
    success: bool = True
    error: str | None = None
    duration_ms: float = 0.0
    screenshot_path: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Browser harness
# ---------------------------------------------------------------------------

class BrowserHarness:
    """Manages an isolated Playwright browser context for computer-use.

    Lifecycle: ``launch()`` → actions / screenshots → ``close()``.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        screenshot_dir: Path | None = None,
    ) -> None:
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._screenshot_dir = screenshot_dir

        # Playwright objects — set during launch()
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._page is not None

    @property
    def current_url(self) -> str | None:
        """Return the browser's current URL, or None if no page is open."""
        return self._page.url if self._page is not None else None

    def launch(self) -> None:
        """Start the browser.  Raises PlaywrightMissing or HarnessError."""
        if self._page is not None:
            return  # already launched

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise PlaywrightMissing(
                "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
            )

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
            self._context = self._browser.new_context(
                viewport=self._viewport,
                locale="en-US",
                # Block geolocation/notifications to keep the session isolated
                permissions=[],
            )
            self._page = self._context.new_page()
            logger.info("Browser harness launched (headless=%s)", self._headless)
        except Exception as exc:
            self._cleanup()
            raise HarnessError(f"Failed to launch browser: {exc}") from exc

    def close(self) -> None:
        """Shut down the browser context and cleanup."""
        self._cleanup()
        logger.info("Browser harness closed")

    def _cleanup(self) -> None:
        for obj_name in ("_context", "_browser"):
            obj = getattr(self, obj_name, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    def _require_page(self) -> Any:
        if self._page is None:
            raise HarnessNotReady("Browser not launched — call launch() first")
        return self._page

    # ── Navigation ───────────────────────────────────────────────────

    def navigate(self, url: str, *, timeout_ms: int = 30_000) -> HarnessActionResult:
        page = self._require_page()
        t0 = time.monotonic()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            return HarnessActionResult(
                action_type="navigate",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            return HarnessActionResult(
                action_type="navigate",
                success=False,
                error=str(exc),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    # ── Screenshots ──────────────────────────────────────────────────

    def screenshot(self, dest: Path | None = None) -> Path:
        """Capture a full-page screenshot.  Returns the file path."""
        page = self._require_page()
        if dest is None:
            if self._screenshot_dir is None:
                raise HarnessError("No screenshot destination provided")
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            dest = self._screenshot_dir / f"screenshot_{int(time.time() * 1000)}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(dest), full_page=False)
        return dest

    # ── Actions ──────────────────────────────────────────────────────

    def execute_action(self, action: dict[str, Any]) -> HarnessActionResult:
        """Execute a single structured action.  Returns the result."""
        action_type = action.get("type", "")
        handler = _ACTION_HANDLERS.get(action_type)
        if handler is None:
            return HarnessActionResult(
                action_type=action_type,
                success=False,
                error=f"Unknown action type: {action_type}",
            )
        t0 = time.monotonic()
        try:
            result = handler(self, action)
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result
        except HarnessNotReady:
            raise
        except Exception as exc:
            return HarnessActionResult(
                action_type=action_type,
                success=False,
                error=str(exc),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    def execute_batch(self, actions: list[dict[str, Any]]) -> list[HarnessActionResult]:
        """Execute a batch of actions sequentially."""
        results: list[HarnessActionResult] = []
        for action in actions:
            result = self.execute_action(action)
            results.append(result)
            if not result.success:
                break  # stop batch on first failure
        return results

    # ── Individual action implementations ────────────────────────────

    def _do_click(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        x, y = _require_coords(action)
        button = action.get("button", "left")
        page.mouse.click(x, y, button=button)
        return HarnessActionResult(action_type="click")

    def _do_double_click(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        x, y = _require_coords(action)
        page.mouse.dblclick(x, y)
        return HarnessActionResult(action_type="double_click")

    def _do_scroll(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        x = action.get("x", 0)
        y = action.get("y", 0)
        dx = action.get("scroll_x", 0)
        dy = action.get("scroll_y", 0)
        page.mouse.move(x, y)
        page.mouse.wheel(dx, dy)
        return HarnessActionResult(action_type="scroll")

    def _do_type(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        text = action.get("text")
        if not text:
            return HarnessActionResult(action_type="type", success=False, error="No text provided")
        page.keyboard.type(text)
        return HarnessActionResult(action_type="type")

    def _do_keypress(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        key = action.get("key")
        if not key:
            return HarnessActionResult(action_type="keypress", success=False, error="No key provided")
        page.keyboard.press(key)
        return HarnessActionResult(action_type="keypress")

    def _do_move(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        x, y = _require_coords(action)
        page.mouse.move(x, y)
        return HarnessActionResult(action_type="move")

    def _do_drag(self, action: dict[str, Any]) -> HarnessActionResult:
        page = self._require_page()
        x, y = _require_coords(action)
        end_x = action.get("drag_end_x")
        end_y = action.get("drag_end_y")
        if end_x is None or end_y is None:
            return HarnessActionResult(
                action_type="drag",
                success=False,
                error="drag requires drag_end_x and drag_end_y",
            )
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(end_x, end_y)
        page.mouse.up()
        return HarnessActionResult(action_type="drag")

    def _do_wait(self, action: dict[str, Any]) -> HarnessActionResult:
        duration_ms = action.get("duration_ms", 1000)
        # Cap at 10 seconds to prevent stall
        duration_ms = min(duration_ms, 10_000)
        time.sleep(duration_ms / 1000)
        return HarnessActionResult(action_type="wait")

    def _do_screenshot(self, _action: dict[str, Any]) -> HarnessActionResult:
        path = self.screenshot()
        return HarnessActionResult(
            action_type="screenshot",
            screenshot_path=str(path),
        )

    def _do_navigate(self, action: dict[str, Any]) -> HarnessActionResult:
        url = action.get("url")
        if not url:
            return HarnessActionResult(action_type="navigate", success=False, error="No URL provided")
        return self.navigate(url)


# ---------------------------------------------------------------------------
# Action handler dispatch table
# ---------------------------------------------------------------------------

def _require_coords(action: dict[str, Any]) -> tuple[int, int]:
    x = action.get("x")
    y = action.get("y")
    if x is None or y is None:
        raise HarnessError(f"Action '{action.get('type')}' requires x and y coordinates")
    return int(x), int(y)


_ACTION_HANDLERS: dict[str, Any] = {
    "click": BrowserHarness._do_click,
    "double_click": BrowserHarness._do_double_click,
    "scroll": BrowserHarness._do_scroll,
    "type": BrowserHarness._do_type,
    "keypress": BrowserHarness._do_keypress,
    "move": BrowserHarness._do_move,
    "drag": BrowserHarness._do_drag,
    "wait": BrowserHarness._do_wait,
    "screenshot": BrowserHarness._do_screenshot,
    "navigate": BrowserHarness._do_navigate,
}
