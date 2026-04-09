"""File-target conflict detection and merge-safety checks."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Sequence

from boss.workers.state import WorkerRecord

_CASE_INSENSITIVE_PATHS = sys.platform == "darwin" or os.name == "nt"


@dataclass(frozen=True)
class ConflictReport:
    """Result of checking workers for overlapping file targets."""

    has_conflicts: bool
    conflicts: list[FileConflict] = field(default_factory=list)

    def summary(self) -> str:
        if not self.has_conflicts:
            return "No file-target conflicts detected."
        lines = ["File-target conflicts detected:"]
        for c in self.conflicts:
            lines.append(f"  {c.path}: workers {', '.join(c.worker_ids)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class FileConflict:
    """A single file path claimed by multiple workers."""

    path: str
    worker_ids: list[str] = field(default_factory=list)


def detect_conflicts(workers: Sequence[WorkerRecord]) -> ConflictReport:
    """Check whether any workers share file targets.

    Only implementer workers that actually write files are checked.
    Explorers and reviewers are read-only and excluded.
    """
    # Collect file → list of worker_ids.
    file_owners: dict[str, list[str]] = {}
    for w in workers:
        if w.role != "implementer":
            continue
        for target in w.file_targets:
            normalized = _normalize_path(target)
            file_owners.setdefault(normalized, []).append(w.worker_id)

    conflicts: list[FileConflict] = []
    for path, owners in file_owners.items():
        if len(owners) > 1:
            conflicts.append(FileConflict(path=path, worker_ids=list(owners)))

    return ConflictReport(
        has_conflicts=len(conflicts) > 0,
        conflicts=conflicts,
    )


def detect_directory_overlap(workers: Sequence[WorkerRecord]) -> ConflictReport:
    """Detect workers targeting files within the same directory subtree.

    This catches cases like worker A editing ``src/foo/bar.py`` while
    worker B edits ``src/foo/baz.py`` — technically distinct files but
    same directory scope that could produce merge conflicts.
    """
    # Collect parent dirs claimed by each worker.
    dir_workers: dict[str, list[str]] = {}
    for w in workers:
        if w.role != "implementer":
            continue
        dirs_for_worker: set[str] = set()
        for target in w.file_targets:
            parent = str(PurePosixPath(_normalize_path(target)).parent)
            dirs_for_worker.add(parent)
        for d in dirs_for_worker:
            dir_workers.setdefault(d, []).append(w.worker_id)

    conflicts: list[FileConflict] = []
    for path, owners in dir_workers.items():
        if len(owners) > 1:
            conflicts.append(FileConflict(path=path + "/", worker_ids=list(owners)))

    return ConflictReport(
        has_conflicts=len(conflicts) > 0,
        conflicts=conflicts,
    )


def _normalize_path(raw: str) -> str:
    """Normalize a file path for comparison (lower-case, forward slashes)."""
    normalized = raw.strip().replace("\\", "/").rstrip("/")
    if _CASE_INSENSITIVE_PATHS:
        return normalized.casefold()
    return normalized
