from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from boss.config import settings


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_REVIEW_KEYWORDS = (
    "review",
    "code review",
    "audit",
    "security review",
    "architecture review",
)
_SUPPORTED_WORK_MODES = {"ask", "plan", "agent", "review"}
_MODE_ALIASES = {
    "default": "agent",
    "general": "agent",
}


def default_workspace_root() -> Path:
    return _REPO_ROOT


@dataclass(frozen=True)
class BossRule:
    name: str
    path: Path
    title: str
    body: str
    modes: tuple[str, ...]
    targets: tuple[str, ...]
    tags: tuple[str, ...]
    always: bool = False


@dataclass(frozen=True)
class BossProjectConfig:
    raw: dict[str, Any]
    default_mode: str
    permissions: dict[str, Any]
    indexing: dict[str, Any]
    memory: dict[str, Any]
    jobs: dict[str, Any]
    review: dict[str, Any]

    def review_mode_name(self) -> str:
        configured = self.review.get("mode_name")
        if isinstance(configured, str) and configured.strip():
            return _normalize_mode(configured)
        return "review"

    def review_keywords(self) -> tuple[str, ...]:
        configured = _string_tuple(self.review.get("auto_mode_keywords"))
        return configured or _DEFAULT_REVIEW_KEYWORDS


@dataclass(frozen=True)
class IgnoreMatcher:
    root: Path
    source: Path
    patterns: tuple[str, ...]

    def matches(self, path: str | Path, *, is_dir: bool = False) -> bool:
        relative = _to_relative_posix(path, self.root)
        if relative is None:
            return False
        if not relative:
            return False

        for pattern in self.patterns:
            if _pattern_matches(pattern, relative, is_dir=is_dir):
                return True
        return False


@dataclass(frozen=True)
class BossControlState:
    root: Path
    boss_md_path: Path
    boss_md: str
    config_path: Path
    config: BossProjectConfig
    rules_dir: Path
    rules: tuple[BossRule, ...]
    review_path: Path
    review: str
    environment_path: Path
    environment: dict[str, Any]
    access_ignore_path: Path
    access_ignore: IgnoreMatcher
    index_ignore_path: Path
    index_ignore: IgnoreMatcher

    def has_project_instructions(self) -> bool:
        return bool(self.boss_md.strip())

    def is_configured(self) -> bool:
        return any(
            (
                self.boss_md_path.exists(),
                self.config_path.exists(),
                self.rules_dir.exists() and bool(self.rules),
                self.review_path.exists(),
                self.environment_path.exists(),
                self.access_ignore_path.exists(),
                self.index_ignore_path.exists(),
            )
        )


def load_boss_control(workspace_root: Path | str | None = None, *, refresh: bool = False) -> BossControlState:
    root = _normalize_root(workspace_root)
    if refresh:
        _load_boss_control_cached.cache_clear()
    return _load_boss_control_cached(str(root))


def resolve_request_mode(
    user_message: str,
    *,
    explicit_mode: str | None = None,
    workspace_root: Path | str | None = None,
) -> str:
    control = load_boss_control(workspace_root)
    if explicit_mode and explicit_mode.strip():
        return _normalize_mode(explicit_mode)

    review_config = control.config.review
    auto_activate = _bool_value(review_config.get("auto_activate"), default=True)
    if auto_activate:
        normalized_message = user_message.lower()
        for keyword in control.config.review_keywords():
            if keyword.lower() in normalized_message:
                return control.config.review_mode_name()
    return control.config.default_mode


def build_agent_instructions(
    base_instructions: str,
    *,
    agent_name: str,
    mode: str | None = None,
    workspace_root: Path | str | None = None,
) -> str:
    control = load_boss_control(workspace_root)
    resolved_mode = _normalize_mode(mode) if mode else control.config.default_mode

    sections = [base_instructions.strip()]

    if control.boss_md.strip():
        sections.append("Project instructions (BOSS.md):\n" + control.boss_md.strip())

    environment_summary = _render_environment_summary(control.environment)
    if environment_summary:
        sections.append("Local environment and validation:\n" + environment_summary)

    rules = applicable_rules(agent_name=agent_name, mode=resolved_mode, workspace_root=control.root)
    if rules:
        sections.append(
            "Boss project rules:\n"
            + "\n\n".join(f"{rule.title}:\n{rule.body.strip()}" for rule in rules if rule.body.strip())
        )

    if resolved_mode == control.config.review_mode_name() and control.review.strip():
        sections.append("Review behavior:\n" + control.review.strip())

    return "\n\n".join(section for section in sections if section.strip())


def applicable_rules(
    *,
    agent_name: str,
    mode: str,
    workspace_root: Path | str | None = None,
) -> list[BossRule]:
    control = load_boss_control(workspace_root)
    targets = _agent_targets(agent_name)
    resolved_mode = _normalize_mode(mode)

    selected: list[BossRule] = []
    for rule in control.rules:
        if not rule.body.strip():
            continue
        if not rule.always and rule.modes and "all" not in rule.modes and resolved_mode not in rule.modes:
            continue
        if rule.targets and "all" not in rule.targets and not any(target in rule.targets for target in targets):
            continue
        selected.append(rule)
    return selected


def is_memory_injection_enabled(workspace_root: Path | str | None = None) -> bool:
    if os.getenv("BOSS_AUTO_MEMORY_ENABLED") is not None:
        return settings.auto_memory_enabled
    control = load_boss_control(workspace_root)
    configured = control.config.memory.get("auto_injection")
    return _bool_value(configured, default=settings.auto_memory_enabled)


def is_memory_distillation_enabled(workspace_root: Path | str | None = None) -> bool:
    if os.getenv("BOSS_AUTO_MEMORY_ENABLED") is not None:
        return settings.auto_memory_enabled
    control = load_boss_control(workspace_root)
    configured = control.config.memory.get("auto_distillation")
    return _bool_value(configured, default=settings.auto_memory_enabled)


def memory_auto_approve_enabled(workspace_root: Path | str | None = None) -> bool:
    control = load_boss_control(workspace_root)
    configured = control.config.memory.get("auto_approve")
    return _bool_value(configured, default=False)


def memory_auto_approve_min_confidence(workspace_root: Path | str | None = None) -> float:
    control = load_boss_control(workspace_root)
    configured = control.config.memory.get("auto_approve_min_confidence")
    return _float_value(configured, default=0.98)


def jobs_branch_behavior(workspace_root: Path | str | None = None) -> str:
    control = load_boss_control(workspace_root)
    configured = _string_value(control.config.jobs.get("branch_behavior"), fallback="suggest").lower()
    if configured not in {"off", "suggest", "create"}:
        return "suggest"
    return configured


def jobs_takeover_cancels_background(workspace_root: Path | str | None = None) -> bool:
    control = load_boss_control(workspace_root)
    configured = control.config.jobs.get("takeover_cancels_background")
    return _bool_value(configured, default=True)


def indexing_respects_ignore(workspace_root: Path | str | None = None) -> bool:
    control = load_boss_control(workspace_root)
    return _bool_value(control.config.indexing.get("respect_bossindexignore"), default=True)


def agent_access_respects_ignore(workspace_root: Path | str | None = None) -> bool:
    control = load_boss_control(workspace_root)
    return _bool_value(control.config.permissions.get("respect_bossignore"), default=True)


def should_index_path(project_root: Path | str, path: Path, *, is_dir: bool) -> bool:
    control = load_boss_control(project_root)
    if not indexing_respects_ignore(control.root):
        return True
    return not control.index_ignore.matches(path, is_dir=is_dir)


def is_path_allowed_for_agent(path: Path | str) -> bool:
    candidate = Path(path).expanduser()
    control_root = find_control_root(candidate)
    if control_root is None:
        return True
    control = load_boss_control(control_root)
    if not agent_access_respects_ignore(control.root):
        return True
    return not control.access_ignore.matches(candidate, is_dir=candidate.is_dir())


def find_control_root(path: Path | str) -> Path | None:
    candidate = Path(path).expanduser()
    current = candidate if candidate.is_dir() else candidate.parent

    for root in [current, *current.parents]:
        if _has_control_marker(root):
            return root
    return None


def boss_control_status_payload(workspace_root: Path | str | None = None) -> dict[str, Any]:
    control = load_boss_control(workspace_root)
    return {
        "configured": control.is_configured(),
        "root_path": str(control.root),
        "available_modes": sorted(_SUPPORTED_WORK_MODES),
        "default_mode": control.config.default_mode,
        "review_mode_name": control.config.review_mode_name(),
        "review_keywords": list(control.config.review_keywords()),
        "permissions": {
            "default_policy": str(control.config.permissions.get("default_policy", "prompt")),
            "respect_bossignore": agent_access_respects_ignore(control.root),
        },
        "indexing": {
            "respect_bossindexignore": indexing_respects_ignore(control.root),
            "ignore_patterns": len(control.index_ignore.patterns),
            "include_boss_control_files": _bool_value(
                control.config.indexing.get("include_boss_control_files"),
                default=False,
            ),
        },
        "memory": {
            "auto_injection_enabled": is_memory_injection_enabled(control.root),
            "auto_distillation_enabled": is_memory_distillation_enabled(control.root),
            "auto_approve_enabled": memory_auto_approve_enabled(control.root),
            "auto_approve_min_confidence": memory_auto_approve_min_confidence(control.root),
        },
        "jobs": {
            "branch_behavior": jobs_branch_behavior(control.root),
            "takeover_cancels_background": jobs_takeover_cancels_background(control.root),
        },
        "files": {
            "BOSS.md": {"path": str(control.boss_md_path), "exists": control.boss_md_path.exists()},
            "config": {"path": str(control.config_path), "exists": control.config_path.exists()},
            "review": {"path": str(control.review_path), "exists": control.review_path.exists()},
            "environment": {"path": str(control.environment_path), "exists": control.environment_path.exists()},
            "bossignore": {"path": str(control.access_ignore_path), "exists": control.access_ignore_path.exists()},
            "bossindexignore": {"path": str(control.index_ignore_path), "exists": control.index_ignore_path.exists()},
        },
        "rules": [
            {
                "name": rule.name,
                "title": rule.title,
                "path": str(rule.path),
                "modes": list(rule.modes),
                "targets": list(rule.targets),
                "always": rule.always,
                "tags": list(rule.tags),
            }
            for rule in control.rules
        ],
        "environment": control.environment,
    }


@lru_cache(maxsize=16)
def _load_boss_control_cached(root_path: str) -> BossControlState:
    root = Path(root_path)
    boss_md_path = root / "BOSS.md"
    config_path = root / ".boss" / "config.toml"
    rules_dir = root / ".boss" / "rules"
    review_path = root / ".boss" / "review.md"
    environment_path = root / ".boss" / "environment.json"
    access_ignore_path = root / ".bossignore"
    index_ignore_path = root / ".bossindexignore"

    raw_config = _read_toml(config_path)
    config = BossProjectConfig(
        raw=raw_config,
        default_mode=_normalize_mode(_nested_string(raw_config, "mode", "default", fallback="agent")),
        permissions=_mapping(raw_config.get("permissions")),
        indexing=_mapping(raw_config.get("indexing")),
        memory=_mapping(raw_config.get("memory")),
        jobs=_mapping(raw_config.get("jobs")),
        review=_mapping(raw_config.get("review")),
    )

    return BossControlState(
        root=root,
        boss_md_path=boss_md_path,
        boss_md=_read_text(boss_md_path),
        config_path=config_path,
        config=config,
        rules_dir=rules_dir,
        rules=_load_rules(rules_dir),
        review_path=review_path,
        review=_read_text(review_path),
        environment_path=environment_path,
        environment=_read_json(environment_path),
        access_ignore_path=access_ignore_path,
        access_ignore=IgnoreMatcher(root=root, source=access_ignore_path, patterns=_read_ignore_patterns(access_ignore_path)),
        index_ignore_path=index_ignore_path,
        index_ignore=IgnoreMatcher(root=root, source=index_ignore_path, patterns=_read_ignore_patterns(index_ignore_path)),
    )


def _normalize_root(workspace_root: Path | str | None) -> Path:
    if workspace_root is None:
        return default_workspace_root()
    root = Path(workspace_root).expanduser()
    return root if root.is_dir() else root.parent


def _load_rules(rules_dir: Path) -> tuple[BossRule, ...]:
    if not rules_dir.exists() or not rules_dir.is_dir():
        return ()

    rules: list[BossRule] = []
    for path in sorted(rules_dir.glob("*.md")):
        text = _read_text(path)
        metadata, body = _parse_frontmatter(text)
        title = _string_value(metadata.get("title"), fallback=path.stem)
        rules.append(
            BossRule(
                name=path.stem,
                path=path,
                title=title,
                body=body.strip(),
                modes=tuple(_normalize_mode(item) for item in (_string_tuple(metadata.get("modes")) or ("agent",))),
                targets=_string_tuple(metadata.get("targets")) or ("all",),
                tags=_string_tuple(metadata.get("tags")),
                always=_bool_value(metadata.get("always"), default=False),
            )
        )
    return tuple(rules)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("+++\n"):
        return {}, text

    match = re.match(r"\A\+\+\+\n(.*?)\n\+\+\+\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return {}, text

    metadata_text, body = match.groups()
    try:
        metadata = tomllib.loads(metadata_text)
    except tomllib.TOMLDecodeError:
        return {}, text
    return _mapping(metadata), body


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _mapping(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _mapping(payload)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_ignore_patterns(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()

    patterns: list[str] = []
    for line in _read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return tuple(patterns)


def _render_environment_summary(environment: dict[str, Any]) -> str:
    if not environment:
        return ""

    lines: list[str] = []
    name = _string_value(environment.get("name"))
    platform = _string_value(environment.get("platform"))
    if name:
        lines.append(f"- Environment: {name}")
    if platform:
        lines.append(f"- Platform: {platform}")

    for constraint in _string_tuple(environment.get("constraints"))[:6]:
        lines.append(f"- Constraint: {constraint}")

    validation = _mapping(environment.get("validation"))
    backend_commands = _string_tuple(validation.get("backend"))
    client_commands = _string_tuple(validation.get("client"))
    if backend_commands:
        lines.append(f"- Backend validation: {backend_commands[0]}")
    if client_commands:
        lines.append(f"- Client validation: {client_commands[0]}")
    return "\n".join(lines)


def _has_control_marker(path: Path) -> bool:
    return any(
        (
            (path / "BOSS.md").exists(),
            (path / ".boss" / "config.toml").exists(),
            (path / ".boss" / "review.md").exists(),
            (path / ".boss" / "environment.json").exists(),
            (path / ".bossignore").exists(),
            (path / ".bossindexignore").exists(),
            (path / ".boss" / "rules").exists(),
        )
    )


def _pattern_matches(pattern: str, relative_path: str, *, is_dir: bool) -> bool:
    anchored = pattern.startswith("/")
    directory_only = pattern.endswith("/")
    candidate = pattern.lstrip("/").rstrip("/")
    if not candidate:
        return False

    relative = relative_path.rstrip("/")
    rel_path = PurePosixPath(relative)

    if directory_only:
        if anchored:
            return relative == candidate or relative.startswith(candidate + "/")
        parts = rel_path.parts
        if candidate in parts:
            return True
        return relative == candidate or relative.startswith(candidate + "/") or f"/{candidate}/" in f"/{relative}/"

    if anchored:
        return rel_path.match(candidate) or relative == candidate

    if "/" in candidate:
        return rel_path.match(candidate) or rel_path.match(f"**/{candidate}")

    return rel_path.match(candidate)


def _to_relative_posix(path: str | Path, root: Path) -> str | None:
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        base = root.resolve(strict=False)
        relative = resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _agent_targets(agent_name: str) -> tuple[str, ...]:
    normalized = agent_name.strip().lower()
    if normalized == "boss":
        # Primary agent matches "boss", "general" (backward compat),
        # and code-related targets.
        return ("boss", "general", "code", "backend-python", "macos-client")
    if normalized == "code":
        return ("code", "backend-python", "macos-client")
    if normalized == "general":
        return ("general", "boss")
    if normalized == "mac":
        return ("mac",)
    return (normalized,)


def _normalize_mode(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    normalized = normalized.strip("-") or "agent"
    normalized = _MODE_ALIASES.get(normalized, normalized)
    if normalized not in _SUPPORTED_WORK_MODES:
        return "agent"
    return normalized


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested_string(payload: dict[str, Any], section: str, key: str, *, fallback: str) -> str:
    section_value = payload.get(section)
    if not isinstance(section_value, dict):
        return fallback
    return _string_value(section_value.get(key), fallback=fallback)


def _string_value(value: Any, *, fallback: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _float_value(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default