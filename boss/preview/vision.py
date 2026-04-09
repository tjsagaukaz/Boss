"""Preview vision: provider-aware multimodal input from preview captures.

Converts CaptureResult screenshots into model input when the active provider
supports vision (image input).  Degrades to textual summary when vision is
unavailable or the screenshot is missing.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from boss.preview.session import (
    CaptureResult,
    DetailMode,
    VerificationMethod,
)

logger = logging.getLogger(__name__)

# Models known to support image/vision input.
# This is heuristic — new models should be added as they ship.
_VISION_CAPABLE_PREFIXES = (
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "claude-3",
    "claude-4",
    "gemini",
)

# Models known NOT to support vision (override for prefixes above)
_VISION_EXCLUDED = (
    "gpt-4o-mini-audio",
)


def model_supports_vision(model_name: str | None) -> bool:
    """Check whether a model name is likely to support image input."""
    if not model_name:
        return False
    name = model_name.strip().lower()

    for excluded in _VISION_EXCLUDED:
        if name.startswith(excluded):
            return False

    for prefix in _VISION_CAPABLE_PREFIXES:
        if name.startswith(prefix):
            return True

    return False


def is_vision_available() -> bool:
    """Check whether the currently configured provider model supports vision."""
    from boss.config import settings
    return model_supports_vision(settings.general_model)


def capture_to_model_input(
    capture: CaptureResult,
    *,
    detail: str = DetailMode.AUTO.value,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Convert a CaptureResult into model-consumable input.

    Returns a dict with:
        - "method": "visual" | "textual" | "skipped"
        - "content": list of content parts (text + optional image)
        - "summary": human-readable summary of what was included
    """
    # Determine if we should use vision
    use_vision = False
    if model_name is not None:
        use_vision = model_supports_vision(model_name)
    else:
        use_vision = is_vision_available()

    has_screenshot = (
        capture.screenshot_path
        and Path(capture.screenshot_path).exists()
    )

    if use_vision and has_screenshot:
        return _build_visual_input(capture, detail=detail)
    elif capture.screenshot_path or capture.dom_summary or capture.page_title:
        return _build_textual_input(capture)
    else:
        return {
            "method": VerificationMethod.SKIPPED.value,
            "content": [],
            "summary": "No preview data available for verification.",
        }


def _build_visual_input(
    capture: CaptureResult,
    *,
    detail: str = DetailMode.AUTO.value,
) -> dict[str, Any]:
    """Build multimodal input with screenshot image."""
    content_parts: list[dict[str, Any]] = []

    # Text context first
    text_lines = ["Preview capture verification:"]
    if capture.page_title:
        text_lines.append(f"Page title: {capture.page_title}")
    if capture.console_errors:
        text_lines.append(f"Console errors: {len(capture.console_errors)}")
        for err in capture.console_errors[:5]:
            text_lines.append(f"  - {err}")
    if capture.network_errors:
        text_lines.append(f"Network errors: {len(capture.network_errors)}")
        for err in capture.network_errors[:5]:
            text_lines.append(f"  - {err}")
    if capture.region:
        text_lines.append(
            f"Focus region: ({capture.region.get('x', 0)}, {capture.region.get('y', 0)}) "
            f"{capture.region.get('width', 0)}x{capture.region.get('height', 0)}"
        )

    content_parts.append({
        "type": "input_text",
        "text": "\n".join(text_lines),
    })

    # Image part
    screenshot_path = Path(capture.screenshot_path)
    try:
        image_data = screenshot_path.read_bytes()
        b64 = base64.b64encode(image_data).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        # Map detail mode to SDK values
        sdk_detail = detail if detail in ("low", "high", "auto", "original") else "auto"

        content_parts.append({
            "type": "input_image",
            "image_url": data_uri,
            "detail": sdk_detail,
        })
    except (OSError, IOError) as exc:
        logger.warning("Failed to read screenshot %s: %s", screenshot_path, exc)
        content_parts.append({
            "type": "input_text",
            "text": f"[Screenshot file could not be read: {exc}]",
        })
        return {
            "method": VerificationMethod.TEXTUAL.value,
            "content": content_parts,
            "summary": f"Screenshot read failed, textual fallback. Title: {capture.page_title or 'unknown'}",
        }

    summary_parts = [f"Visual verification with {detail} detail"]
    if capture.page_title:
        summary_parts.append(f"title='{capture.page_title}'")
    if capture.has_errors:
        error_count = len(capture.console_errors) + len(capture.network_errors)
        summary_parts.append(f"{error_count} error(s)")

    return {
        "method": VerificationMethod.VISUAL.value,
        "content": content_parts,
        "summary": ", ".join(summary_parts),
    }


def _build_textual_input(capture: CaptureResult) -> dict[str, Any]:
    """Build text-only input when vision is unavailable."""
    text = capture.textual_summary()

    return {
        "method": VerificationMethod.TEXTUAL.value,
        "content": [{"type": "input_text", "text": f"Preview verification (text only):\n{text}"}],
        "summary": f"Textual verification. Title: {capture.page_title or 'unknown'}",
    }
