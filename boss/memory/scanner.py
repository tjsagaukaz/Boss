"""Incremental filesystem and project scanner — builds project intelligence."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from boss.config import settings
from boss.memory.knowledge import KnowledgeStore, Project, get_knowledge_store


_PROJECT_MARKERS: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "Package.swift": "swift",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "Gemfile": "ruby",
    "pom.xml": "java",
    "build.gradle": "java",
    "CMakeLists.txt": "cpp",
    "Makefile": "make",
    "docker-compose.yml": "docker",
    "Dockerfile": "docker",
}

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pnpm-store",
    ".yarn",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".build",
    "build",
    "dist",
    "target",
    ".tox",
    ".eggs",
    "vendor",
    "Pods",
    "Carthage",
    "DerivedData",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".cache",
}
_SKIP_DIR_SUFFIXES = {".egg-info", ".xcworkspace", ".xcodeproj"}
_ALLOWED_HIDDEN_DIRS = {".github", ".vscode", ".devcontainer"}
_BINARY_EXTENSIONS = {
    ".a",
    ".bin",
    ".class",
    ".dylib",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".o",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".sqlite",
    ".tar",
    ".ttf",
    ".webp",
    ".zip",
}
_ENTRY_POINT_NAMES = {
    "start-server.sh",
    "main.py",
    "app.py",
    "api.py",
    "server.py",
    "manage.py",
    "Package.swift",
    "main.swift",
    "index.js",
    "index.ts",
    "index.tsx",
    "Dockerfile",
    "docker-compose.yml",
}
_NOTABLE_KEYWORDS = (
    "agent",
    "api",
    "app",
    "client",
    "config",
    "execution",
    "handler",
    "main",
    "manager",
    "memory",
    "model",
    "router",
    "scanner",
    "server",
    "service",
    "tool",
    "view",
    "viewmodel",
)


@dataclass
class ProjectScanResult:
    path: str
    name: str
    project_type: str
    files_seen: int
    files_indexed: int
    files_removed: int
    summary_refreshed: bool
    updated: bool
    truncated: bool
    stack: list[str]
    entry_points: list[str]


def _detect_project_type(project_path: Path) -> str:
    for marker, ptype in _PROJECT_MARKERS.items():
        if (project_path / marker).exists():
            return ptype
    return "unknown"


def _is_project_root(path: Path) -> bool:
    return (path / ".git").exists() or any((path / marker).exists() for marker in _PROJECT_MARKERS)


def _should_skip_dir(name: str) -> bool:
    if name in _SKIP_DIRS:
        return True
    if any(name.endswith(suffix) for suffix in _SKIP_DIR_SUFFIXES):
        return True
    if name.startswith(".") and name not in _ALLOWED_HIDDEN_DIRS:
        return True
    return False


def _should_skip_file(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    try:
        stat = path.stat()
    except OSError:
        return True
    if stat.st_size <= 0 or stat.st_size > settings.project_scan_max_file_bytes:
        return True
    return not _looks_like_text_file(path)


def _looks_like_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return False
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    control_chars = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return control_chars <= max(12, len(sample) // 8)


def _discover_projects(directory: Path, max_depth: int) -> list[Path]:
    discovered: list[Path] = []
    seen: set[str] = set()

    def _walk(current: Path, depth: int) -> None:
        try:
            resolved = str(current.resolve())
        except OSError:
            resolved = str(current)
        if resolved in seen:
            return
        seen.add(resolved)

        if depth > max_depth:
            return
        if _is_project_root(current):
            discovered.append(current)
            return

        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return

        for entry in entries:
            if not entry.is_dir() or entry.is_symlink() or _should_skip_dir(entry.name):
                continue
            _walk(entry, depth + 1)

    if directory.exists() and directory.is_dir():
        _walk(directory, 0)
    return discovered


def _get_git_info(project_path: Path) -> tuple[str | None, str | None]:
    if not (project_path / ".git").exists():
        return None, None

    remote = None
    branch = None
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            remote = result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            branch = result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return remote, branch


def _walk_project_files(project_path: Path) -> tuple[list[Path], int, bool]:
    candidates: list[Path] = []
    seen = 0
    truncated = False

    for root, dirnames, filenames in os.walk(project_path, topdown=True, followlinks=False):
        dirnames[:] = [
            name
            for name in sorted(dirnames, key=str.lower)
            if not _should_skip_dir(name)
        ]

        for filename in sorted(filenames, key=str.lower):
            path = Path(root) / filename
            if path.is_symlink() or not path.is_file() or _should_skip_file(path):
                continue
            candidates.append(path)
            seen += 1
            if seen >= settings.project_scan_max_files_per_project:
                truncated = True
                return candidates, seen, truncated

    return candidates, seen, truncated


def _relative_paths(project_path: Path, files: list[Path]) -> list[Path]:
    relative: list[Path] = []
    for path in files:
        try:
            relative.append(path.relative_to(project_path))
        except ValueError:
            relative.append(path)
    return relative


def _read_text(path: Path, limit: int = 32_000) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    if not raw or b"\x00" in raw:
        return ""
    return raw.decode("utf-8", errors="ignore")


def _package_metadata(project_path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {}

    pyproject = project_path / "pyproject.toml"
    if pyproject.exists():
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            payload = {}
        project_info = payload.get("project") if isinstance(payload, dict) else None
        if isinstance(project_info, dict):
            name = project_info.get("name")
            description = project_info.get("description")
            if isinstance(name, str) and name.strip():
                metadata["package_name"] = name.strip()
            if isinstance(description, str) and description.strip():
                metadata["description"] = description.strip()

    package_json = project_path / "package.json"
    if package_json.exists():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            name = payload.get("name")
            description = payload.get("description")
            if isinstance(name, str) and name.strip():
                metadata["package_name"] = name.strip()
            if isinstance(description, str) and description.strip():
                metadata["description"] = description.strip()

    return metadata


def _collect_samples(project_path: Path, relative_paths: list[Path], entry_points: list[str]) -> list[str]:
    preferred: list[Path] = []
    lookup = {path.as_posix(): path for path in relative_paths}
    for rel in entry_points:
        candidate = lookup.get(rel)
        if candidate is not None:
            preferred.append(candidate)
    for rel in relative_paths:
        if rel.name in {"pyproject.toml", "package.json", "Package.swift"}:
            preferred.append(rel)
    samples: list[str] = []
    seen: set[str] = set()
    for rel in preferred:
        rel_key = rel.as_posix()
        if rel_key in seen:
            continue
        seen.add(rel_key)
        text = _read_text(project_path / rel, limit=24_000)
        if text:
            samples.append(text.lower())
        if len(samples) >= 12:
            break
    return samples


def _infer_stack(project_path: Path, project_type: str, relative_paths: list[Path], entry_points: list[str]) -> list[str]:
    stack: list[str] = []
    suffixes = {path.suffix.lower() for path in relative_paths}
    path_strings = {path.as_posix() for path in relative_paths}
    samples = _collect_samples(project_path, relative_paths, entry_points)

    def add(label: str) -> None:
        if label not in stack:
            stack.append(label)

    if project_type == "python" or ".py" in suffixes:
        add("Python")
    if project_type == "swift" or ".swift" in suffixes or any(path.endswith("Package.swift") for path in path_strings):
        add("Swift")
    if project_type == "node" or suffixes & {".js", ".jsx", ".ts", ".tsx"}:
        add("Node.js")
    if ".ts" in suffixes or ".tsx" in suffixes:
        add("TypeScript")
    if ".go" in suffixes or project_type == "go":
        add("Go")
    if ".rs" in suffixes or project_type == "rust":
        add("Rust")
    if any("from fastapi import" in sample or "fastapi(" in sample for sample in samples):
        add("FastAPI")
    if any("import swiftui" in sample for sample in samples):
        add("SwiftUI")
    if any("react" in sample for sample in samples):
        add("React")

    return stack


def _likely_entry_points(relative_paths: list[Path], project_type: str) -> list[str]:
    scored: list[tuple[int, str]] = []
    for path in relative_paths:
        rel = path.as_posix()
        name = path.name
        stem = path.stem.lower()
        score = 0
        if name in _ENTRY_POINT_NAMES:
            score += 10
        if rel.startswith("src/") and name in {"index.ts", "index.tsx", "main.ts", "main.tsx"}:
            score += 8
        if name.endswith("App.swift"):
            score += 8
        if stem in {"main", "app", "api", "server"}:
            score += 6
        if len(path.parts) == 1:
            score += 2
        if project_type == "python" and path.suffix == ".py":
            score += 1
        if score > 0:
            scored.append((score, rel))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return _dedupe_strings(rel for _, rel in scored)[:8]


def _top_python_packages(project_path: Path) -> list[str]:
    packages: list[str] = []
    try:
        entries = sorted(project_path.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return packages

    for entry in entries:
        if entry.is_dir() and (entry / "__init__.py").exists():
            packages.append(entry.name)
    return packages


def _package_scripts(package_json: Path) -> list[str]:
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [key for key, value in scripts.items() if isinstance(key, str) and isinstance(value, str)]


def _useful_commands(project_path: Path, relative_paths: list[Path], project_type: str) -> list[str]:
    rel_set = {path.as_posix() for path in relative_paths}
    commands: list[str] = []

    def add(command: str) -> None:
        if command not in commands:
            commands.append(command)

    if "start-server.sh" in rel_set:
        add("./start-server.sh")

    python_packages = _top_python_packages(project_path)
    for package in python_packages[:3]:
        if f"{package}/main.py" in rel_set:
            add(f"python -m {package}.main")
        if f"{package}/api.py" in rel_set:
            add(f"python -m uvicorn {package}.api:app --host 127.0.0.1 --port 8321")

    if "Package.swift" in rel_set:
        add("swift build")
    for rel in sorted(rel_set):
        if rel.endswith("/Package.swift"):
            add(f"cd {Path(rel).parent.as_posix()} && swift build")

    if "Cargo.toml" in rel_set:
        add("cargo build")
        add("cargo test")
    if "go.mod" in rel_set:
        add("go test ./...")
    if "Makefile" in rel_set:
        add("make")
    if "package.json" in rel_set:
        for script in _package_scripts(project_path / "package.json")[:4]:
            add(f"npm run {script}")
    for rel in sorted(rel_set):
        if rel.endswith("/package.json"):
            package_dir = Path(rel).parent.as_posix()
            for script in _package_scripts(project_path / rel)[:3]:
                add(f"cd {package_dir} && npm run {script}")

    if project_type == "python" and not commands:
        add("python -m <package>.main")

    return commands[:8]


def _file_type_summary(relative_paths: list[Path]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for path in relative_paths:
        key = path.suffix.lstrip(".").lower() if path.suffix else path.name.lower()
        if not key:
            continue
        counts[key] += 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    return dict(ordered)


def _file_map(relative_paths: list[Path]) -> list[str]:
    directory_counts: Counter[str] = Counter()
    top_files: list[str] = []

    for path in relative_paths:
        if len(path.parts) == 1:
            if len(top_files) < 8:
                top_files.append(path.as_posix())
            continue
        directory = path.parts[0]
        directory_counts[directory] += 1
        if len(path.parts) > 2:
            nested = "/".join(path.parts[:2])
            directory_counts[nested] += 1

    items = [f"{name}/ ({count} indexed files)" for name, count in directory_counts.most_common(8)]
    if top_files:
        items.append("top-level files: " + ", ".join(sorted(top_files)))
    return items[:10]


def _notable_modules(relative_paths: list[Path]) -> list[str]:
    scored: list[tuple[int, str]] = []
    for path in relative_paths:
        rel = path.as_posix()
        name = path.name.lower()
        score = 0
        if name in {"pyproject.toml", "package.json", "Package.swift", "Dockerfile"}:
            score += 8
        for keyword in _NOTABLE_KEYWORDS:
            if keyword in name or keyword in rel.lower():
                score += 3
        if path.suffix.lower() in {".py", ".swift", ".ts", ".tsx", ".js", ".jsx"}:
            score += 1
        if len(path.parts) <= 2:
            score += 1
        if score > 0:
            scored.append((score, rel))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return _dedupe_strings(rel for _, rel in scored)[:12]


def _build_project_metadata(
    project_path: Path,
    *,
    project_type: str,
    git_remote: str | None,
    git_branch: str | None,
    indexed_files: list[Path],
    truncated: bool,
) -> dict[str, Any]:
    relative_paths = _relative_paths(project_path, indexed_files)
    entry_points = _likely_entry_points(relative_paths, project_type)
    metadata = _package_metadata(project_path)
    metadata.update(
        {
            "file_types": _file_type_summary(relative_paths),
            "stack": _infer_stack(project_path, project_type, relative_paths, entry_points),
            "entry_points": entry_points,
            "useful_commands": _useful_commands(project_path, relative_paths, project_type),
            "file_map": _file_map(relative_paths[: settings.project_scan_summary_file_limit]),
            "notable_modules": _notable_modules(relative_paths[: settings.project_scan_summary_file_limit]),
            "indexed_files_count": len(relative_paths),
            "scan_truncated": truncated,
        }
    )
    if git_remote:
        metadata["git_remote"] = git_remote
    if git_branch:
        metadata["git_branch"] = git_branch
    return metadata


def _project_summary_signature(
    *,
    project_type: str,
    git_remote: str | None,
    git_branch: str | None,
    metadata: dict[str, Any],
) -> str:
    summary = {
        "project_type": project_type,
        "git_remote": git_remote,
        "git_branch": git_branch,
        "package_name": metadata.get("package_name"),
        "description": metadata.get("description"),
        "file_types": metadata.get("file_types"),
        "stack": metadata.get("stack"),
        "entry_points": metadata.get("entry_points"),
        "useful_commands": metadata.get("useful_commands"),
        "file_map": metadata.get("file_map"),
        "notable_modules": metadata.get("notable_modules"),
        "indexed_files_count": metadata.get("indexed_files_count"),
        "scan_truncated": metadata.get("scan_truncated"),
    }
    return json.dumps(summary, sort_keys=True)


def _file_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _scan_project(project_path: Path, store: KnowledgeStore) -> ProjectScanResult:
    project_type = _detect_project_type(project_path)
    git_remote, git_branch = _get_git_info(project_path)
    indexed_files, files_seen, truncated = _walk_project_files(project_path)
    metadata = _build_project_metadata(
        project_path,
        project_type=project_type,
        git_remote=git_remote,
        git_branch=git_branch,
        indexed_files=indexed_files,
        truncated=truncated,
    )

    existing_project = store.get_project(str(project_path))
    previous_signature = None
    if existing_project is not None:
        previous_signature = _project_summary_signature(
            project_type=existing_project.project_type,
            git_remote=existing_project.git_remote,
            git_branch=existing_project.git_branch,
            metadata=existing_project.metadata,
        )

    project = store.upsert_project(
        path=str(project_path),
        name=project_path.name,
        project_type=project_type,
        git_remote=git_remote,
        git_branch=git_branch,
        metadata=metadata,
    )

    existing_index = store.get_project_file_index(project.id)
    files_indexed = 0
    keep_paths: list[str] = []
    for path in indexed_files:
        file_path = str(path)
        keep_paths.append(file_path)
        try:
            stat = path.stat()
            modified_at = _file_timestamp(path)
        except OSError:
            continue
        existing = existing_index.get(file_path)
        if existing and existing.get("size") == stat.st_size and existing.get("modified_at") == modified_at:
            continue
        store.index_file(file_path, project_id=project.id)
        files_indexed += 1

    files_removed = store.prune_project_files(project.id, keep_paths, commit=False)
    store.commit_file_index()

    current_signature = _project_summary_signature(
        project_type=project.project_type,
        git_remote=project.git_remote,
        git_branch=project.git_branch,
        metadata=project.metadata,
    )
    summary_refreshed = previous_signature != current_signature
    updated = existing_project is None or files_indexed > 0 or files_removed > 0 or summary_refreshed

    return ProjectScanResult(
        path=project.path,
        name=project.name,
        project_type=project.project_type,
        files_seen=files_seen,
        files_indexed=files_indexed,
        files_removed=files_removed,
        summary_refreshed=summary_refreshed,
        updated=updated,
        truncated=truncated,
        stack=_string_list(project.metadata.get("stack")),
        entry_points=_string_list(project.metadata.get("entry_points")),
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _dedupe_strings(values: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def scan_directory(
    directory: Path,
    store: KnowledgeStore,
    max_depth: int | None = None,
    known_projects: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan a directory for projects and return per-project scan results."""
    known_projects = known_projects if known_projects is not None else set()
    results: list[dict[str, Any]] = []
    depth = settings.project_scan_discovery_depth if max_depth is None else max_depth

    for project_path in _discover_projects(directory, depth):
        try:
            resolved = str(project_path.resolve())
        except OSError:
            resolved = str(project_path)
        if resolved in known_projects:
            continue
        known_projects.add(resolved)
        outcome = _scan_project(project_path, store)
        results.append(
            {
                "path": outcome.path,
                "name": outcome.name,
                "type": outcome.project_type,
                "files_seen": outcome.files_seen,
                "files_indexed": outcome.files_indexed,
                "files_removed": outcome.files_removed,
                "summary_refreshed": outcome.summary_refreshed,
                "updated": outcome.updated,
                "truncated": outcome.truncated,
                "stack": outcome.stack,
                "entry_points": outcome.entry_points,
            }
        )

    return results


def full_scan(store: KnowledgeStore | None = None) -> dict[str, Any]:
    """Run a full incremental scan across all configured project roots."""
    if store is None:
        store = get_knowledge_store()

    started_at = datetime.now(timezone.utc)
    scanned_dirs: list[str] = []
    discovered_projects: set[str] = set()
    projects: list[dict[str, Any]] = []

    for scan_dir in settings.project_scan_roots:
        if not scan_dir.exists() or not scan_dir.is_dir():
            continue
        scanned_dirs.append(str(scan_dir))
        projects.extend(scan_directory(scan_dir, store, known_projects=discovered_projects))

    projects_found = len(projects)
    projects_updated = sum(1 for project in projects if project["updated"])
    files_indexed = sum(int(project["files_indexed"]) for project in projects)
    files_removed = sum(int(project["files_removed"]) for project in projects)
    summaries_refreshed = sum(1 for project in projects if project["summary_refreshed"])
    finished_at = datetime.now(timezone.utc)

    return {
        "directories_scanned": scanned_dirs,
        "projects_found": projects_found,
        "projects_updated": projects_updated,
        "files_indexed": files_indexed,
        "files_removed": files_removed,
        "summaries_refreshed": summaries_refreshed,
        "last_scan_time": finished_at.isoformat(),
        "scan_duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "projects": projects,
    }
