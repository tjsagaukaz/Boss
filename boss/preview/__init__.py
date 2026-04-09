"""Boss Preview: local-first preview and multimodal verification for frontend work.

Manages preview lifecycle (start, discover URL, capture screenshots/errors),
provides verification signals for UI tasks, and integrates with vision-capable
providers for multimodal validation.

This is an optional capability layer — it degrades gracefully when browser
tooling or dev servers are unavailable.
"""

from boss.preview.session import (
    CaptureRegion,
    CaptureResult,
    DetailMode,
    PreviewCapabilities,
    PreviewSession,
    PreviewStatus,
    VerificationMethod,
    detect_preview_capabilities,
    get_active_session,
)

__all__ = [
    "CaptureRegion",
    "CaptureResult",
    "DetailMode",
    "PreviewCapabilities",
    "PreviewSession",
    "PreviewStatus",
    "VerificationMethod",
    "detect_preview_capabilities",
    "get_active_session",
]
