from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_scan_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / "Documents",
        home / "Desktop",
        home / "Developer",
        home / "Projects",
        home / "Code",
        home / "repos",
        home / "workspace",
        home / "boss",
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_path_tuple(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    value = os.getenv(name)
    if value is None:
        return default

    paths = [Path(part).expanduser() for part in value.split(os.pathsep) if part.strip()]
    return tuple(paths) or default


@dataclass(frozen=True)
class Settings:
    app_data_dir: Path = Path(os.getenv("BOSS_DATA_DIR", Path.home() / ".boss"))
    api_port: int = max(1, _env_int("BOSS_API_PORT", 8321))
    tracing_enabled: bool = _env_bool("BOSS_TRACING_ENABLED", True)
    auto_memory_enabled: bool = _env_bool("BOSS_AUTO_MEMORY_ENABLED", True)
    project_scan_roots: tuple[Path, ...] = _env_path_tuple("BOSS_SCAN_ROOTS", _default_scan_roots())
    project_scan_discovery_depth: int = max(1, _env_int("BOSS_SCAN_DISCOVERY_DEPTH", 4))
    project_scan_max_files_per_project: int = max(100, _env_int("BOSS_SCAN_MAX_FILES_PER_PROJECT", 2500))
    project_scan_max_file_bytes: int = max(4_096, _env_int("BOSS_SCAN_MAX_FILE_BYTES", 200_000))
    project_scan_summary_file_limit: int = max(20, _env_int("BOSS_SCAN_SUMMARY_FILE_LIMIT", 160))
    auto_memory_injection_limit: int = max(1, _env_int("BOSS_AUTO_MEMORY_INJECTION_LIMIT", 8))
    auto_memory_distillation_limit: int = max(
        1,
        _env_int("BOSS_AUTO_MEMORY_DISTILLATION_LIMIT", 6),
    )
    session_max_recent_turns: int = max(1, _env_int("BOSS_SESSION_MAX_RECENT_TURNS", 6))
    session_max_serialized_size: int = max(
        4096,
        _env_int("BOSS_SESSION_MAX_SERIALIZED_SIZE", 65536),
    )
    session_summary_threshold: int = max(1, _env_int("BOSS_SESSION_SUMMARY_THRESHOLD", 8))
    provider_mode: str = os.getenv("BOSS_PROVIDER_MODE", "auto").strip().lower()
    provider_session_mode: str = os.getenv("BOSS_PROVIDER_SESSION_MODE", "disabled").strip().lower()
    provider_session_limit: int = max(1, _env_int("BOSS_PROVIDER_SESSION_LIMIT", 200))
    provider_compaction_threshold: int = max(1, _env_int("BOSS_PROVIDER_COMPACTION_THRESHOLD", 10))
    cloud_api_key: str | None = os.getenv("OPENAI_API_KEY")
    general_model: str = os.getenv("BOSS_GENERAL_MODEL", "gpt-5.4-mini")
    mac_model: str = os.getenv("BOSS_MAC_MODEL", "gpt-5.4-mini")
    research_model: str = os.getenv("BOSS_RESEARCH_MODEL", "gpt-5.4")
    reasoning_model: str = os.getenv("BOSS_REASONING_MODEL", "gpt-5.4")
    code_model: str = os.getenv("BOSS_CODE_MODEL", "gpt-5.4")
    guardrail_model: str = os.getenv("BOSS_GUARDRAIL_MODEL", "gpt-5.4-mini")
    history_dir: Path = Path(os.getenv("BOSS_HISTORY_DIR", app_data_dir / "conversations"))
    knowledge_db_file: Path = Path(os.getenv("BOSS_KNOWLEDGE_DB_FILE", app_data_dir / "knowledge.db"))
    provider_session_db_file: Path = Path(
        os.getenv("BOSS_PROVIDER_SESSION_DB_FILE", app_data_dir / "provider_sessions.sqlite")
    )
    provider_compaction_model: str = os.getenv(
        "BOSS_PROVIDER_COMPACTION_MODEL",
        general_model,
    )
    permissions_file: Path = Path(os.getenv("BOSS_PERMISSIONS_FILE", app_data_dir / "permissions.json"))
    review_history_dir: Path = Path(os.getenv("BOSS_REVIEW_HISTORY_DIR", app_data_dir / "reviews"))
    jobs_dir: Path = Path(os.getenv("BOSS_JOBS_DIR", app_data_dir / "jobs"))
    job_logs_dir: Path = Path(os.getenv("BOSS_JOB_LOGS_DIR", app_data_dir / "job-logs"))
    pending_runs_dir: Path = Path(os.getenv("BOSS_PENDING_RUNS_DIR", app_data_dir / "pending_runs"))
    pending_run_expiration_seconds: int = max(
        300,
        _env_int("BOSS_PENDING_RUN_EXPIRATION_SECONDS", 43200),
    )
    expired_pending_run_retention_seconds: int = max(
        3600,
        _env_int("BOSS_EXPIRED_PENDING_RUN_RETENTION_SECONDS", 604800),
    )
    api_lock_file: Path = Path(os.getenv("BOSS_API_LOCK_FILE", app_data_dir / "api-server.lock"))
    permission_log_file: Path = Path(
        os.getenv("BOSS_PERMISSION_LOG_FILE", app_data_dir / "permission_events.jsonl")
    )
    event_log_file: Path = Path(os.getenv("BOSS_EVENT_LOG_FILE", app_data_dir / "events.jsonl"))
    max_concurrent_workers: int = max(1, _env_int("BOSS_MAX_CONCURRENT_WORKERS", 3))
    deploy_enabled: bool = _env_bool("BOSS_DEPLOY_ENABLED", False)
    deploy_history_dir: Path = Path(os.getenv("BOSS_DEPLOY_HISTORY_DIR", app_data_dir / "deploys"))


settings = Settings()