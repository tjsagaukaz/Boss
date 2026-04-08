#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
BASE_URL = os.getenv("BOSS_BASE_URL", "http://127.0.0.1:8321")
LOCK_FILE = Path(os.getenv("BOSS_API_LOCK_FILE", Path.home() / ".boss" / "api-server.lock"))
REQUIRED_STATUS_FIELDS = {
    "process_id",
    "started_at",
    "ready_at",
    "interpreter_path",
    "workspace_path",
    "app_version",
    "build_marker",
    "runtime_trust",
    "memory",
    "pending_run_count",
    "stale_pending_run_count",
}
EXTRA_RUNTIME_PACKAGES = ["fastapi", "uvicorn"]


@dataclass
class Check:
    label: str
    ok: bool
    detail: str


def main() -> int:
    checks: list[Check] = []

    checks.append(check_venv_exists())
    checks.extend(check_venv_packages())

    status = fetch_status()
    if isinstance(status, Exception):
        checks.append(Check("system status", False, str(status)))
    else:
        checks.append(check_status_fields(status))
        checks.append(check_lock_agreement(status))
        checks.append(check_runtime_identity(status))

    print(f"Boss Dev Doctor\nroot: {ROOT}\nbase_url: {BASE_URL}\n")
    for check in checks:
        prefix = "PASS" if check.ok else "FAIL"
        print(f"[{prefix}] {check.label}: {check.detail}")

    failed = [check for check in checks if not check.ok]
    if failed:
        print(f"\nBoss is not healthy: {len(failed)} check(s) failed.")
        return 1

    print("\nBoss looks healthy.")
    return 0


def check_venv_exists() -> Check:
    if VENV_PYTHON.exists():
        return Check("venv", True, f"Using {VENV_PYTHON}")
    return Check("venv", False, f"Missing expected interpreter at {VENV_PYTHON}")


def check_venv_packages() -> list[Check]:
    if not VENV_PYTHON.exists():
        return [Check("venv packages", False, "Skipped because the venv is missing")]

    required_packages = required_runtime_packages()
    probe = (
        "import importlib.metadata as md, json\n"
        f"packages = {json.dumps(required_packages)}\n"
        "result = {}\n"
        "for name in packages:\n"
        "    try:\n"
        "        result[name] = md.version(name)\n"
        "    except md.PackageNotFoundError:\n"
        "        result[name] = None\n"
        "print(json.dumps(result))\n"
    )
    completed = subprocess.run(
        [str(VENV_PYTHON), "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return [Check("venv packages", False, completed.stderr.strip() or "Package probe failed")]

    payload = json.loads(completed.stdout)
    missing = sorted(name for name, version in payload.items() if version is None)
    if missing:
        return [Check("venv packages", False, f"Missing packages in .venv: {', '.join(missing)}")]

    versions = ", ".join(f"{name}={payload[name]}" for name in sorted(payload))
    return [Check("venv packages", True, versions)]


def required_runtime_packages() -> list[str]:
    pyproject = ROOT / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = payload.get("project", {}).get("dependencies", [])

    packages: list[str] = []
    for dependency in dependencies:
        name = dependency.split(";", 1)[0].strip()
        for separator in [">=", "<=", "==", "~=", "!="]:
            if separator in name:
                name = name.split(separator, 1)[0].strip()
        name = name.strip()
        if name and name not in packages:
            packages.append(name)

    for name in EXTRA_RUNTIME_PACKAGES:
        if name not in packages:
            packages.append(name)
    return packages


def fetch_status() -> dict | Exception:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/system/status", timeout=2.5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        return exc


def check_status_fields(status: dict) -> Check:
    missing = sorted(REQUIRED_STATUS_FIELDS.difference(status.keys()))
    if missing:
        return Check("system status fields", False, f"Missing fields: {', '.join(missing)}")
    return Check("system status fields", True, "Required runtime-trust fields are present")


def check_lock_agreement(status: dict) -> Check:
    runtime_trust = status.get("runtime_trust") or {}
    warnings = runtime_trust.get("warnings") or []
    lock_exists = runtime_trust.get("lock_exists")
    lock_pid = runtime_trust.get("lock_pid")
    process_id = status.get("process_id")

    if not lock_exists:
        return Check("lock agreement", False, f"Lock file missing at {LOCK_FILE}")
    if lock_pid != process_id:
        return Check("lock agreement", False, f"Lock pid {lock_pid} does not match live pid {process_id}")
    if warnings:
        return Check("lock agreement", False, f"Runtime trust warnings: {', '.join(warnings)}")
    return Check("lock agreement", True, f"Lock file agrees with live pid {process_id}")


def check_runtime_identity(status: dict) -> Check:
    workspace_path = Path(status.get("workspace_path", "")) if status.get("workspace_path") else None
    interpreter_path = Path(status.get("interpreter_path", "")) if status.get("interpreter_path") else None

    issues: list[str] = []
    if workspace_path is None or workspace_path.resolve() != ROOT.resolve():
        issues.append(f"workspace_path={workspace_path}")
    if interpreter_path is None or interpreter_path.resolve() != VENV_PYTHON.resolve():
        issues.append(f"interpreter_path={interpreter_path}")

    if issues:
        return Check("runtime identity", False, "; ".join(issues))
    return Check("runtime identity", True, f"workspace={workspace_path} interpreter={interpreter_path}")


if __name__ == "__main__":
    raise SystemExit(main())