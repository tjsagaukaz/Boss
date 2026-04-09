"""iOS toolchain detection and command construction.

Detects availability and versions of:
  - xcodebuild  (Xcode CLI tools)
  - xcrun        (developer tool dispatcher)
  - fastlane     (optional automation)
  - security     (keychain / signing queries)

Provides structured command builders for archive, export, and upload
steps so the engine doesn't have to manually assemble argument lists.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Toolchain availability ──────────────────────────────────────────


@dataclass(frozen=True)
class ToolInfo:
    """Availability record for a single CLI tool."""

    name: str
    available: bool
    path: str | None = None
    version: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "available": self.available}
        if self.path:
            d["path"] = self.path
        if self.version:
            d["version"] = self.version
        if self.error:
            d["error"] = self.error
        return d


@dataclass(frozen=True)
class IOSToolchain:
    """Snapshot of the local iOS build toolchain."""

    xcodebuild: ToolInfo
    xcrun: ToolInfo
    fastlane: ToolInfo
    security: ToolInfo
    xcode_path: str | None = None
    xcode_version: str | None = None

    @property
    def can_build(self) -> bool:
        """True if the minimum tooling (xcodebuild) is available."""
        return self.xcodebuild.available

    @property
    def has_fastlane(self) -> bool:
        return self.fastlane.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "xcodebuild": self.xcodebuild.to_dict(),
            "xcrun": self.xcrun.to_dict(),
            "fastlane": self.fastlane.to_dict(),
            "security": self.security.to_dict(),
            "xcode_path": self.xcode_path,
            "xcode_version": self.xcode_version,
            "can_build": self.can_build,
            "has_fastlane": self.has_fastlane,
        }

    def summary(self) -> str:
        parts = []
        if self.xcode_version:
            parts.append(f"Xcode {self.xcode_version}")
        if self.xcode_path:
            parts.append(f"at {self.xcode_path}")
        if not self.can_build:
            parts.append("(xcodebuild NOT available)")
        if self.has_fastlane:
            parts.append(f"fastlane={self.fastlane.version or 'yes'}")
        return " | ".join(parts) if parts else "No iOS toolchain detected"


def _probe_tool(name: str, version_args: list[str] | None = None) -> ToolInfo:
    """Check whether *name* is on PATH and optionally query its version."""
    path = shutil.which(name)
    if not path:
        return ToolInfo(name=name, available=False, error="not found on PATH")
    version: str | None = None
    if version_args:
        try:
            result = subprocess.run(
                [path] + version_args,
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = (result.stdout + result.stderr).strip()
            # Extract the first version-like string
            m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)", out)
            if m:
                version = m.group(1)
            elif out:
                # Use first line as version info
                version = out.splitlines()[0][:120]
        except (subprocess.TimeoutExpired, OSError):
            pass
    return ToolInfo(name=name, available=True, path=path, version=version)


def _probe_xcode_path() -> tuple[str | None, str | None]:
    """Return (xcode_developer_dir, xcode_version) via xcode-select."""
    xcode_path: str | None = None
    xcode_version: str | None = None
    try:
        result = subprocess.run(
            ["xcode-select", "-p"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            xcode_path = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Try to get Xcode version from xcodebuild
    xcodebuild = shutil.which("xcodebuild")
    if xcodebuild:
        try:
            result = subprocess.run(
                [xcodebuild, "-version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = result.stdout.strip()
            # Xcode 15.2\nBuild version 15C500b
            m = re.search(r"Xcode\s+(\d+\.\d+(?:\.\d+)?)", out)
            if m:
                xcode_version = m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return xcode_path, xcode_version


def detect_toolchain() -> IOSToolchain:
    """Probe the local system for iOS build tooling.

    This is intentionally synchronous and cached per-process since
    toolchain availability doesn't change during a single session.
    """
    xcodebuild = _probe_tool("xcodebuild", ["-version"])
    xcrun = _probe_tool("xcrun", ["--version"])
    fastlane = _probe_tool("fastlane", ["--version"])
    security = _probe_tool("security", ["--help"])
    xcode_path, xcode_version = _probe_xcode_path()
    return IOSToolchain(
        xcodebuild=xcodebuild,
        xcrun=xcrun,
        fastlane=fastlane,
        security=security,
        xcode_path=xcode_path,
        xcode_version=xcode_version,
    )


# Module-level cache
_cached_toolchain: IOSToolchain | None = None


def get_toolchain(*, refresh: bool = False) -> IOSToolchain:
    """Return the cached toolchain snapshot, probing once per process."""
    global _cached_toolchain
    if _cached_toolchain is None or refresh:
        _cached_toolchain = detect_toolchain()
    return _cached_toolchain


# ── Command construction ───────────────────────────────────────────


def build_archive_command(
    *,
    workspace: str | None = None,
    project: str | None = None,
    scheme: str,
    configuration: str = "Release",
    archive_path: str,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Construct the ``xcodebuild archive`` command."""
    cmd = ["xcodebuild", "archive"]
    if workspace:
        cmd += ["-workspace", workspace]
    elif project:
        cmd += ["-project", project]
    cmd += ["-scheme", scheme]
    cmd += ["-configuration", configuration]
    cmd += ["-archivePath", archive_path]
    # Disable code-signing during archive if automatic signing handles it
    # at export time — this is standard for CI.
    cmd += ["CODE_SIGN_ALLOW_PROVISIONING_UPDATES=YES"]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def build_export_command(
    *,
    archive_path: str,
    export_path: str,
    export_options_plist: str,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Construct the ``xcodebuild -exportArchive`` command."""
    cmd = [
        "xcodebuild",
        "-exportArchive",
        "-archivePath", archive_path,
        "-exportPath", export_path,
        "-exportOptionsPlist", export_options_plist,
    ]
    cmd += ["-allowProvisioningUpdates"]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def build_fastlane_archive_command(
    *,
    workspace: str | None = None,
    project: str | None = None,
    scheme: str,
    configuration: str = "Release",
    output_directory: str,
    export_method: str = "app-store",
    extra_args: list[str] | None = None,
) -> list[str]:
    """Construct a ``fastlane gym`` (build_ios_app) command."""
    cmd = ["fastlane", "gym"]
    if workspace:
        cmd += ["--workspace", workspace]
    elif project:
        cmd += ["--project", project]
    cmd += ["--scheme", scheme]
    cmd += ["--configuration", configuration]
    cmd += ["--output_directory", output_directory]
    cmd += ["--export_method", export_method]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


# ── Upload command builders ────────────────────────────────────────


def build_pilot_upload_command(
    *,
    ipa_path: str,
    api_key_path: str,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Construct a ``fastlane pilot upload`` command with API key auth.

    Uses ``--api_key_path`` for App Store Connect API authentication,
    avoiding Apple ID / app-specific password flows.
    """
    cmd = [
        "fastlane", "pilot", "upload",
        "--ipa", ipa_path,
        "--api_key_path", api_key_path,
        "--skip_waiting_for_build_processing", "false",
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def build_altool_upload_command(
    *,
    ipa_path: str,
    api_key: str,
    api_issuer: str,
    api_key_path: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Construct an ``xcrun altool --upload-app`` command with API key auth.

    Apple's official upload tool, available with every Xcode install.
    Uses ``--apiKey`` and ``--apiIssuer`` for App Store Connect API auth.

    When *api_key_path* is provided it is passed as ``--apiKeyPath`` so
    altool can locate the ``.p8`` file directly instead of searching
    its default directories (``./private_keys/``, ``~/private_keys/``,
    ``~/.private_keys/``, ``~/.appstoreconnect/private_keys/``).
    The value should be the **directory** containing the key file.
    """
    cmd = [
        "xcrun", "altool",
        "--upload-app",
        "--file", ipa_path,
        "--type", "ios",
        "--apiKey", api_key,
        "--apiIssuer", api_issuer,
    ]
    if api_key_path:
        cmd.extend(["--apiKeyPath", api_key_path])
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def build_pilot_builds_command(
    *,
    api_key_path: str,
    app_identifier: str | None = None,
) -> list[str]:
    """Construct a ``fastlane pilot builds`` command to check processing state."""
    cmd = [
        "fastlane", "pilot", "builds",
        "--api_key_path", api_key_path,
    ]
    if app_identifier:
        cmd += ["--app_identifier", app_identifier]
    return cmd


# ── Build log diagnostics ──────────────────────────────────────────


@dataclass
class BuildDiagnostic:
    """A single structured diagnostic extracted from xcodebuild output."""

    severity: str  # "error", "warning", "note"
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    category: str | None = None  # "signing", "compilation", "linking", "provisioning"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"severity": self.severity, "message": self.message}
        if self.file:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        if self.column is not None:
            d["column"] = self.column
        if self.category:
            d["category"] = self.category
        return d


# Patterns for extracting structured diagnostics from xcodebuild output.
# Format: /path/to/file.swift:42:10: error: message
_XCODE_DIAG_RE = re.compile(
    r"^(?P<file>[^\s:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<sev>error|warning|note):\s*(?P<msg>.+)$",
    re.MULTILINE,
)

# Signing/provisioning error patterns
_SIGNING_PATTERNS = [
    (re.compile(r"Code Sign error:?\s*(.+)", re.IGNORECASE), "signing"),
    (re.compile(r"No signing certificate (.+)", re.IGNORECASE), "signing"),
    (re.compile(r"Provisioning profile (.+)", re.IGNORECASE), "provisioning"),
    (re.compile(r"No profiles for '(.+)' were found", re.IGNORECASE), "provisioning"),
    (re.compile(r"(?:Signing|codesign) (?:requires|failed|error)(.+)", re.IGNORECASE), "signing"),
    (re.compile(r"errSecInternalComponent", re.IGNORECASE), "signing"),
    (re.compile(r"CSSMERR_TP_NOT_TRUSTED", re.IGNORECASE), "signing"),
]

# Linker error patterns
_LINKER_PATTERNS = [
    (re.compile(r"Undefined symbols? for architecture", re.IGNORECASE), "linking"),
    (re.compile(r"ld: (.+)", re.IGNORECASE), "linking"),
    (re.compile(r"clang: error: linker command failed", re.IGNORECASE), "linking"),
]


def _classify_message(message: str) -> str | None:
    """Classify a diagnostic message into a category."""
    for pattern, category in _SIGNING_PATTERNS:
        if pattern.search(message):
            return category
    for pattern, category in _LINKER_PATTERNS:
        if pattern.search(message):
            return category
    return None


def parse_build_log(log: str) -> list[BuildDiagnostic]:
    """Extract structured diagnostics from xcodebuild output.

    Returns errors and warnings sorted by severity (errors first).
    """
    diagnostics: list[BuildDiagnostic] = []
    seen: set[str] = set()

    # 1. Standard compiler/build diagnostics with file:line:col
    for m in _XCODE_DIAG_RE.finditer(log):
        key = f"{m.group('file')}:{m.group('line')}:{m.group('msg')}"
        if key in seen:
            continue
        seen.add(key)
        msg = m.group("msg").strip()
        line_no = int(m.group("line"))
        col = int(m.group("col")) if m.group("col") else None
        diagnostics.append(BuildDiagnostic(
            severity=m.group("sev"),
            message=msg,
            file=m.group("file"),
            line=line_no,
            column=col,
            category=_classify_message(msg) or "compilation",
        ))

    # 2. Signing / provisioning errors (often not in file:line format)
    for pattern, category in _SIGNING_PATTERNS + _LINKER_PATTERNS:
        for m in pattern.finditer(log):
            text = m.group(0).strip()
            if text in seen:
                continue
            seen.add(text)
            diagnostics.append(BuildDiagnostic(
                severity="error",
                message=text,
                category=category,
            ))

    # Sort: errors first, then warnings, then notes
    severity_order = {"error": 0, "warning": 1, "note": 2}
    diagnostics.sort(key=lambda d: severity_order.get(d.severity, 3))
    return diagnostics


def summarize_build_failure(log: str) -> dict[str, Any]:
    """Return a structured summary of a build failure.

    Designed to be attached to the run metadata so the UI can display
    actionable information instead of raw logs.
    """
    diagnostics = parse_build_log(log)
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]

    categories: dict[str, int] = {}
    for d in errors:
        cat = d.category or "other"
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "categories": categories,
        "errors": [d.to_dict() for d in errors[:20]],
        "warnings": [d.to_dict() for d in warnings[:10]],
        "is_signing_failure": "signing" in categories or "provisioning" in categories,
        "is_compilation_failure": "compilation" in categories,
        "is_linking_failure": "linking" in categories,
    }
