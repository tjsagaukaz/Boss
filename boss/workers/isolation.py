"""Workspace isolation for parallel workers."""

from __future__ import annotations

import difflib
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from boss.runner.workspace import (
    TaskWorkspace,
    WorkspaceState,
    WorkspaceStrategy,
    create_task_workspace,
    cleanup_task_workspace,
    load_task_workspace,
    update_task_workspace,
)
from boss.workers.roles import WorkerRole, ROLE_NEEDS_ISOLATION
from boss.workers.state import WorkerRecord

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceChanges:
    """Diff collected from an implementer's isolated workspace."""

    worker_id: str
    diff_text: str
    files_changed: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    strategy: str = "git_diff"  # "git_diff" or "file_diff"
    workspace_path: str | None = None


def provision_workspace(worker: WorkerRecord, source_path: str | Path) -> TaskWorkspace | None:
    """Create an isolated workspace for a worker that requires one.

    Returns None for read-only roles that share the source workspace.
    """
    role = WorkerRole(worker.role)
    if not ROLE_NEEDS_ISOLATION[role]:
        return None

    ws = create_task_workspace(
        source_path=source_path,
        task_slug=f"w-{worker.worker_id}",
        branch_name=f"boss/worker-{worker.worker_id}",
    )
    worker.workspace_id = ws.workspace_id
    worker.workspace_path = ws.workspace_path
    update_task_workspace(ws.workspace_id, state=WorkspaceState.ACTIVE.value)
    return ws


# ── Diff collection and apply ──────────────────────────────────────


def collect_workspace_changes(worker: WorkerRecord, source_path: str | Path) -> WorkspaceChanges | None:
    """Collect changes made in a worker's isolated workspace as a unified diff.

    Must be called BEFORE ``release_workspace`` so the workspace still exists.
    Returns None when no workspace is present or no changes were made.
    """
    if not worker.workspace_id or not worker.workspace_path:
        return None

    ws = load_task_workspace(worker.workspace_id)
    if ws is None:
        return None

    workspace = Path(ws.workspace_path)
    if not workspace.exists():
        return None

    if ws.strategy == WorkspaceStrategy.GIT_WORKTREE.value:
        return _collect_git_changes(worker, ws)
    else:
        return _collect_file_changes(worker, ws)


def apply_workspace_changes(changes: WorkspaceChanges, target_path: str | Path) -> bool:
    """Apply collected workspace changes to the target project.

    Uses ``git apply`` for git-based diffs and direct file sync for temp
    workspaces. Returns True on success.
    """
    if not changes.diff_text.strip():
        return True  # nothing to apply

    target = Path(target_path)
    if changes.strategy == "file_diff":
        return _apply_file_changes(changes, target)

    try:
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=changes.diff_text,
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "git apply failed for worker %s: %s",
                changes.worker_id,
                result.stderr[:500],
            )
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("apply_workspace_changes error for %s: %s", changes.worker_id, exc)
        return False


def _collect_git_changes(worker: WorkerRecord, ws: TaskWorkspace) -> WorkspaceChanges | None:
    """Collect changes in a git worktree relative to its base branch."""
    workspace = Path(ws.workspace_path)
    base_branch = (ws.metadata or {}).get("base_branch", "HEAD")

    # Capture both staged and unstaged changes relative to the base.
    try:
        result = subprocess.run(
            ["git", "diff", base_branch],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        diff_text = result.stdout or ""

        # Also include uncommitted staged/unstaged changes
        unstaged = subprocess.run(
            ["git", "diff"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if unstaged.stdout and unstaged.stdout not in diff_text:
            diff_text += unstaged.stdout

    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Git diff failed for worker %s: %s", worker.worker_id, exc)
        return None

    if not diff_text.strip():
        return None

    files = _extract_changed_files(diff_text)
    return WorkspaceChanges(
        worker_id=worker.worker_id,
        diff_text=diff_text,
        files_changed=files,
        strategy="git_diff",
    )


def _collect_file_changes(worker: WorkerRecord, ws: TaskWorkspace) -> WorkspaceChanges | None:
    """Collect changes in a temp-directory workspace via relative file diff."""
    source = Path(ws.source_path)
    workspace = Path(ws.workspace_path)
    if not source.exists():
        return None

    files_changed, files_deleted = _collect_file_paths(source, workspace)
    if not files_changed and not files_deleted:
        return None

    diff_text = _build_file_diff(source, workspace, files_changed, files_deleted)
    return WorkspaceChanges(
        worker_id=worker.worker_id,
        diff_text=diff_text,
        files_changed=files_changed,
        files_deleted=files_deleted,
        strategy="file_diff",
        workspace_path=str(workspace),
    )


def _extract_changed_files(diff_text: str) -> list[str]:
    """Extract file paths from a unified diff."""
    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            files.append(line[4:])
    return files


def _collect_file_paths(source: Path, workspace: Path) -> tuple[list[str], list[str]]:
    """Return changed/added files and deleted files relative to the workspace roots."""
    source_files = _relative_files(source)
    workspace_files = _relative_files(workspace)

    deleted = sorted(source_files - workspace_files)
    changed: list[str] = []
    for rel in sorted(workspace_files):
        src_file = source / rel
        ws_file = workspace / rel
        if rel not in source_files or _files_differ(src_file, ws_file):
            changed.append(rel)
    return changed, deleted


def _relative_files(root: Path) -> set[str]:
    """List file paths under root relative to root using POSIX separators."""
    files: set[str] = set()
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files


def _files_differ(source_file: Path, workspace_file: Path) -> bool:
    """Return True when files differ by content."""
    try:
        return source_file.read_bytes() != workspace_file.read_bytes()
    except OSError:
        return True


def _read_diff_lines(path: Path) -> list[str]:
    """Read a file for unified diff generation, tolerating decode failures."""
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return []


def _build_file_diff(
    source: Path,
    workspace: Path,
    files_changed: list[str],
    files_deleted: list[str],
) -> str:
    """Build a git-style relative diff for temp workspace changes."""
    parts: list[str] = []

    for rel in files_changed:
        src_path = source / rel
        ws_path = workspace / rel
        src_lines = _read_diff_lines(src_path)
        ws_lines = _read_diff_lines(ws_path)
        parts.extend(
            difflib.unified_diff(
                src_lines,
                ws_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
        )

    for rel in files_deleted:
        src_path = source / rel
        src_lines = _read_diff_lines(src_path)
        parts.extend(
            difflib.unified_diff(
                src_lines,
                [],
                fromfile=f"a/{rel}",
                tofile="/dev/null",
                lineterm="",
            )
        )

    return "\n".join(parts) + ("\n" if parts else "")


def _apply_file_changes(changes: WorkspaceChanges, target: Path) -> bool:
    """Apply temp-workspace changes by copying/removing relative files."""
    if not changes.workspace_path:
        logger.warning("Missing workspace path for temp changes from worker %s", changes.worker_id)
        return False

    workspace = Path(changes.workspace_path)
    if not workspace.exists():
        logger.warning("Workspace %s missing for worker %s", workspace, changes.worker_id)
        return False

    try:
        for rel in changes.files_deleted:
            dest = target / rel
            if dest.is_file() or dest.is_symlink():
                dest.unlink(missing_ok=True)

        for rel in changes.files_changed:
            src = workspace / rel
            dest = target / rel
            if not src.exists():
                logger.warning("Changed file %s missing in workspace for worker %s", rel, changes.worker_id)
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        return True
    except OSError as exc:
        logger.warning("file-sync apply failed for worker %s: %s", changes.worker_id, exc)
        return False


def release_workspace(worker: WorkerRecord) -> None:
    """Release (and optionally clean up) a worker's isolated workspace."""
    if worker.workspace_id:
        cleanup_task_workspace(worker.workspace_id)
