from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from boss.config import settings


_LOCK_HELD = False
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


def workspace_root() -> Path:
    return _WORKSPACE_ROOT


def app_version() -> str:
    try:
        return metadata.version("boss-assistant")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def build_marker() -> str:
    return os.getenv("BOSS_BUILD_MARKER", f"boss-assistant@{app_version()}")


def read_api_lock_file() -> dict[str, Any] | None:
    return _read_lock_file(settings.api_lock_file)


def ensure_api_server_lock() -> dict[str, Any]:
    """Prevent duplicate Boss API startup with a local lock file and port probe."""
    global _LOCK_HELD

    current_pid = os.getpid()
    existing = _read_lock_file(settings.api_lock_file)
    if existing is not None:
        existing_pid = _coerce_int(existing.get("pid"))
        if existing_pid == current_pid:
            _LOCK_HELD = True
            return existing

        if existing_pid is not None and _pid_is_alive(existing_pid):
            port_in_use = _local_port_is_in_use(settings.api_port)
            existing_workspace = existing.get("workspace_path") or _process_snapshot(existing_pid).get("cwd")
            existing_interpreter = existing.get("interpreter_path") or _process_snapshot(existing_pid).get("executable")
            identity_parts = []
            if existing_workspace:
                identity_parts.append(f"workspace {existing_workspace}")
            if existing_interpreter:
                identity_parts.append(f"interpreter {existing_interpreter}")
            identity_suffix = f" ({', '.join(identity_parts)})" if identity_parts else ""

            if not port_in_use:
                raise RuntimeError(
                    f"Boss API lock file {settings.api_lock_file} points to pid {existing_pid}{identity_suffix}, "
                    f"but port {settings.api_port} is not listening on 127.0.0.1. "
                    "This may be a stale or mismatched server process. Inspect the process and lock file before removing anything."
                )

            raise RuntimeError(
                f"Boss API already appears to be running on port {settings.api_port} "
                f"(pid {existing_pid}){identity_suffix}. Reuse the existing backend instead of starting another instance."
            )

        _safe_unlink(settings.api_lock_file)

    if _local_port_is_in_use(settings.api_port):
        listener_pids = _listeners_for_local_port(settings.api_port)
        pid_suffix = f" Listening pid(s): {', '.join(str(pid) for pid in listener_pids)}." if listener_pids else ""
        raise RuntimeError(
            f"Port {settings.api_port} is already in use on 127.0.0.1. "
            f"Lock file missing or stale at {settings.api_lock_file}.{pid_suffix} "
            "Boss will not start a duplicate backend over an existing listener."
        )

    payload = {
        "pid": current_pid,
        "port": settings.api_port,
        "started_at": time.time(),
        "status": "starting",
        "workspace_path": str(workspace_root()),
        "current_working_directory": str(Path.cwd()),
        "interpreter_path": sys.executable,
        "command": sys.argv,
        "app_version": app_version(),
        "build_marker": build_marker(),
    }
    _write_lock_file(settings.api_lock_file, payload)
    _LOCK_HELD = True
    return payload


def mark_api_server_ready() -> None:
    if not _LOCK_HELD:
        return

    payload = _read_lock_file(settings.api_lock_file) or {}
    payload["pid"] = os.getpid()
    payload["port"] = settings.api_port
    payload["status"] = "running"
    payload["ready_at"] = time.time()
    payload.setdefault("workspace_path", str(workspace_root()))
    payload.setdefault("current_working_directory", str(Path.cwd()))
    payload.setdefault("interpreter_path", sys.executable)
    payload.setdefault("command", sys.argv)
    payload.setdefault("app_version", app_version())
    payload.setdefault("build_marker", build_marker())
    _write_lock_file(settings.api_lock_file, payload)


def release_api_server_lock() -> None:
    global _LOCK_HELD

    if not _LOCK_HELD:
        return

    payload = _read_lock_file(settings.api_lock_file)
    if payload is None:
        _LOCK_HELD = False
        return

    if _coerce_int(payload.get("pid")) == os.getpid():
        _safe_unlink(settings.api_lock_file)
    _LOCK_HELD = False


def dependency_availability() -> dict[str, dict[str, Any]]:
    return {
        "python": {
            "available": True,
            "version": sys.version.split()[0],
            "executable": sys.executable,
        },
        "openai": _package_info("openai"),
        "openai_agents": _package_info("openai-agents"),
        "uvicorn": _package_info("uvicorn"),
        "git": _binary_info("git"),
        "swift": _binary_info("swift"),
    }


def runtime_trust_report() -> dict[str, Any]:
    lock = read_api_lock_file()
    lock_pid = _coerce_int(lock.get("pid")) if lock else None
    pid_alive = _pid_is_alive(lock_pid) if lock_pid is not None else False
    port_in_use = _local_port_is_in_use(settings.api_port)
    listener_pids = _listeners_for_local_port(settings.api_port)
    process = _process_snapshot(lock_pid) if pid_alive and lock_pid is not None else {}
    expected_interpreter = workspace_root() / ".venv" / "bin" / "python"

    warnings: list[str] = []
    if lock is None and listener_pids:
        warnings.append("listener_present_without_lock_file")
    if lock is not None and lock_pid is None:
        warnings.append("lock_missing_pid")
    if lock_pid is not None and not pid_alive:
        warnings.append("lock_pid_not_running")
    if lock is not None and not port_in_use:
        warnings.append("lock_present_but_port_closed")
    if lock_pid is not None and listener_pids and lock_pid not in listener_pids:
        warnings.append("lock_pid_mismatch_with_listener")

    reported_workspace = str(lock.get("workspace_path")) if lock and lock.get("workspace_path") else process.get("cwd")
    reported_interpreter = str(lock.get("interpreter_path")) if lock and lock.get("interpreter_path") else process.get("executable")

    if reported_workspace:
        try:
            if Path(reported_workspace).resolve() != workspace_root().resolve():
                warnings.append("workspace_path_mismatch")
        except OSError:
            warnings.append("workspace_path_unreadable")

    if reported_interpreter and expected_interpreter.exists():
        try:
            if Path(reported_interpreter).resolve() != expected_interpreter.resolve():
                warnings.append("interpreter_path_mismatch")
        except OSError:
            warnings.append("interpreter_path_unreadable")

    return {
        "lock_exists": lock is not None,
        "lock_path": str(settings.api_lock_file),
        "lock_status": lock.get("status") if lock else "missing",
        "lock_pid": lock_pid,
        "lock_pid_alive": pid_alive,
        "port": settings.api_port,
        "port_in_use": port_in_use,
        "listener_pids": listener_pids,
        "process_command": process.get("command"),
        "process_cwd": process.get("cwd"),
        "process_executable": process.get("executable"),
        "reported_workspace_path": reported_workspace,
        "reported_interpreter_path": reported_interpreter,
        "warnings": warnings,
    }


def runtime_status_payload() -> dict[str, Any]:
    lock = read_api_lock_file() or {}
    return {
        "process_id": os.getpid(),
        "started_at": _coerce_float(lock.get("started_at")),
        "ready_at": _coerce_float(lock.get("ready_at")),
        "interpreter_path": sys.executable,
        "workspace_path": str(workspace_root()),
        "current_working_directory": str(Path.cwd()),
        "app_version": lock.get("app_version") or app_version(),
        "build_marker": lock.get("build_marker") or build_marker(),
        "runtime_trust": runtime_trust_report(),
    }


def _package_info(name: str) -> dict[str, Any]:
    try:
        version = metadata.version(name)
    except metadata.PackageNotFoundError:
        return {"available": False}
    return {"available": True, "version": version}


def _binary_info(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"available": path is not None, "path": path}


def _listeners_for_local_port(port: int) -> list[int]:
    lsof = shutil.which("lsof")
    if lsof is None:
        return []

    try:
        result = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        if not line.startswith("p"):
            continue
        pid = _coerce_int(line[1:])
        if pid is not None and pid not in pids:
            pids.append(pid)
    return pids


def _process_snapshot(pid: int | None) -> dict[str, Any]:
    if pid is None or pid <= 0:
        return {}

    snapshot: dict[str, Any] = {}
    ps = shutil.which("ps")
    if ps is not None:
        try:
            result = subprocess.run(
                [ps, "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            command = result.stdout.strip()
            if command:
                snapshot["command"] = command
                snapshot["executable"] = command.split(" ", 1)[0]

    lsof = shutil.which("lsof")
    if lsof is not None:
        try:
            result = subprocess.run(
                [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("n"):
                    snapshot["cwd"] = line[1:].strip()
                    break

    return snapshot


def _read_lock_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_lock_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _local_port_is_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


atexit.register(release_api_server_lock)