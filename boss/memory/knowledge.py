"""SQLite-backed knowledge store for durable memory, project context, and search."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from boss.config import settings


_TEXT_CHUNK_SIZE = 900
_MAX_FILE_CHUNKS = 4
_MAX_TEXT_FILE_BYTES = settings.project_scan_max_file_bytes

# Current schema version.  Bump this and add a migration function
# to _SCHEMA_MIGRATIONS whenever the schema changes.
SCHEMA_VERSION = 1

# Migration functions keyed by TARGET version.  Each receives the
# sqlite3 connection and must apply the DDL for that version step.
# The caller handles the version bump and commit.
_SCHEMA_MIGRATIONS: dict[int, Any] = {
    # Example for v2:
    # 2: _migrate_v1_to_v2,
}
_TEXT_FILE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".md",
    ".m",
    ".mm",
    ".php",
    ".prompt.md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass
class Fact:
    id: int
    category: str
    key: str
    value: str
    source: str
    created_at: str
    updated_at: str


@dataclass
class Project:
    id: int
    path: str
    name: str
    project_type: str
    git_remote: str | None
    git_branch: str | None
    last_scanned: str
    metadata: dict


@dataclass
class DurableMemory:
    id: int
    memory_kind: str
    category: str
    key: str
    value: str
    tags: list[str]
    confidence: float
    salience: float
    source: str
    project_path: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None
    pinned: bool
    pinned_at: str | None
    legacy_fact_id: int | None


@dataclass
class MemoryCandidateRecord:
    id: int
    session_id: str | None
    memory_kind: str
    category: str
    key: str
    value: str
    evidence: str
    tags: list[str]
    confidence: float
    salience: float
    source: str
    project_path: str | None
    status: str
    review_note: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None
    reviewed_at: str | None
    expires_at: str | None
    existing_memory_id: int | None
    promoted_memory_id: int | None


@dataclass
class ConversationEpisode:
    id: int
    session_id: str
    memory_kind: str
    title: str
    summary: str
    category: str
    tags: list[str]
    confidence: float
    salience: float
    source: str
    project_path: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None


@dataclass
class ProjectNote:
    id: int
    project_path: str
    memory_kind: str
    note_key: str
    title: str
    body: str
    category: str
    tags: list[str]
    confidence: float
    salience: float
    source: str
    created_at: str
    updated_at: str
    last_used_at: str | None


@dataclass
class FileChunk:
    id: int
    file_path: str
    project_path: str | None
    file_name: str
    extension: str | None
    memory_kind: str
    category: str
    chunk_index: int
    line_start: int
    line_end: int
    byte_size: int
    modified_at: str | None
    content: str
    content_hash: str | None
    tags: list[str]
    confidence: float
    salience: float
    source: str
    token_estimate: int
    created_at: str
    updated_at: str
    last_used_at: str | None


@dataclass
class ExtractedFileChunk:
    content: str
    line_start: int
    line_end: int


@dataclass
class MemorySearchResult:
    id: int
    source_table: str
    memory_kind: str
    category: str
    key: str
    text: str
    tags: list[str]
    confidence: float
    salience: float
    source: str
    project_path: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None
    score: float
    legacy_fact_id: int | None = None
    review_state: str = "approved"
    pinned: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category, key)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    project_type TEXT NOT NULL DEFAULT 'unknown',
    git_remote TEXT,
    git_branch TEXT,
    last_scanned TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS file_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    extension TEXT,
    size INTEGER,
    modified_at TEXT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS durable_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_kind TEXT NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.75,
    salience REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'user',
    project_path TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    pinned_at TEXT,
    legacy_fact_id INTEGER UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    memory_kind TEXT NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.75,
    salience REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'auto_distillation',
    project_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    reviewed_at TEXT,
    expires_at TEXT,
    existing_memory_id INTEGER REFERENCES durable_memories(id) ON DELETE SET NULL,
    promoted_memory_id INTEGER REFERENCES durable_memories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS conversation_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    memory_kind TEXT NOT NULL DEFAULT 'session_summary',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'session_summary',
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.65,
    salience REAL NOT NULL DEFAULT 0.55,
    source TEXT NOT NULL DEFAULT 'session_manager',
    project_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'project_note',
    note_key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'project_note',
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.7,
    salience REAL NOT NULL DEFAULT 0.6,
    source TEXT NOT NULL DEFAULT 'agent',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    UNIQUE(project_path, note_key)
);

CREATE TABLE IF NOT EXISTS file_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    project_path TEXT,
    file_name TEXT NOT NULL DEFAULT '',
    extension TEXT,
    memory_kind TEXT NOT NULL DEFAULT 'file_chunk',
    category TEXT NOT NULL DEFAULT 'file_chunk',
    chunk_index INTEGER NOT NULL,
    line_start INTEGER NOT NULL DEFAULT 1,
    line_end INTEGER NOT NULL DEFAULT 1,
    byte_size INTEGER NOT NULL DEFAULT 0,
    modified_at TEXT,
    content TEXT NOT NULL,
    content_hash TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.4,
    salience REAL NOT NULL DEFAULT 0.35,
    source TEXT NOT NULL DEFAULT 'scanner',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    UNIQUE(file_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
CREATE INDEX IF NOT EXISTS idx_projects_type ON projects(project_type);
CREATE INDEX IF NOT EXISTS idx_files_project ON file_index(project_id);
CREATE INDEX IF NOT EXISTS idx_files_ext ON file_index(extension);
CREATE INDEX IF NOT EXISTS idx_durable_kind ON durable_memories(memory_kind);
CREATE INDEX IF NOT EXISTS idx_durable_project_path ON durable_memories(project_path);
CREATE INDEX IF NOT EXISTS idx_durable_last_used ON durable_memories(last_used_at);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_status ON memory_candidates(status);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_session ON memory_candidates(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_project_path ON memory_candidates(project_path);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_kind ON memory_candidates(memory_kind);
CREATE INDEX IF NOT EXISTS idx_episode_kind ON conversation_episodes(memory_kind);
CREATE INDEX IF NOT EXISTS idx_episode_project_path ON conversation_episodes(project_path);
CREATE INDEX IF NOT EXISTS idx_project_notes_kind ON project_notes(memory_kind);
CREATE INDEX IF NOT EXISTS idx_project_notes_path ON project_notes(project_path);
CREATE INDEX IF NOT EXISTS idx_file_chunks_path ON file_chunks(file_path);
CREATE INDEX IF NOT EXISTS idx_file_chunks_project_path ON file_chunks(project_path);
"""


class KnowledgeStore:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = settings.knowledge_db_file
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=5.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._ensure_schema_version()
        self._ensure_schema_extensions()
        self._migrate_legacy_data()

    def close(self):
        self._conn.close()

    # --- Facts compatibility ---

    def store_fact(
        self,
        category: str,
        key: str,
        value: str,
        source: str = "agent",
        *,
        tags: Iterable[str] | None = None,
        confidence: float | None = None,
        salience: float | None = None,
        project_path: str | None = None,
    ) -> Fact:
        now = _now_iso()
        self._conn.execute(
            """INSERT INTO facts (category, key, value, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(category, key) DO UPDATE SET
                   value = excluded.value,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            (category, key, value, source, now, now),
        )
        row = self._conn.execute(
            "SELECT * FROM facts WHERE category = ? AND key = ?",
            (category, key),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to store fact")

        fact = self._row_to_fact(row)
        resolved_project_path = project_path or self._resolve_project_path(category, key, value)
        default_tags = list(tags or []) + [category, _memory_kind_for_category(category, resolved_project_path)]
        self.upsert_durable_memory(
            memory_kind=_memory_kind_for_category(category, resolved_project_path),
            category=category,
            key=key,
            value=value,
            tags=default_tags,
            confidence=confidence if confidence is not None else _default_confidence_for_kind(category),
            salience=salience if salience is not None else _default_salience_for_kind(category),
            source=source,
            project_path=resolved_project_path,
            legacy_fact_id=fact.id,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
            commit=False,
        )
        self._conn.commit()
        return fact

    def get_facts(self, category: str | None = None, limit: int = 50) -> list[Fact]:
        if category:
            rows = self._conn.execute(
                "SELECT * FROM facts WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM facts ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def search_facts(self, query: str, limit: int = 20) -> list[Fact]:
        matches = self.search_memories(
            query,
            limit=limit,
            kinds={
                "durable_memory",
                "user_profile",
                "preference",
                "ongoing_goal",
                "workflow",
                "project_note",
                "project_constraint",
                "session_summary",
            },
        )
        return [self._fact_from_search_result(match) for match in matches]

    def delete_fact(self, fact_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self._conn.execute("DELETE FROM durable_memories WHERE legacy_fact_id = ?", (fact_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_durable_memory(self, memory_id: int) -> bool:
        row = self._conn.execute(
            "SELECT legacy_fact_id FROM durable_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return False

        self._conn.execute("DELETE FROM durable_memories WHERE id = ?", (memory_id,))
        legacy_fact_id = row["legacy_fact_id"]
        if legacy_fact_id is not None:
            self._conn.execute("DELETE FROM facts WHERE id = ?", (legacy_fact_id,))
        self._conn.commit()
        return True

    def get_durable_memory(self, memory_id: int) -> DurableMemory | None:
        row = self._conn.execute(
            "SELECT * FROM durable_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        return self._row_to_durable_memory(row) if row is not None else None

    # --- Durable memories ---

    def upsert_durable_memory(
        self,
        *,
        memory_kind: str,
        category: str,
        key: str,
        value: str,
        tags: Iterable[str] | None = None,
        confidence: float = 0.75,
        salience: float = 0.5,
        source: str = "agent",
        project_path: str | None = None,
        legacy_fact_id: int | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        last_used_at: str | None = None,
        commit: bool = True,
    ) -> DurableMemory:
        created_at = created_at or _now_iso()
        updated_at = updated_at or created_at
        tags_json = _json_dumps(_normalize_tags(tags))
        existing = None
        if legacy_fact_id is not None:
            existing = self._conn.execute(
                "SELECT id, created_at, last_used_at FROM durable_memories WHERE legacy_fact_id = ?",
                (legacy_fact_id,),
            ).fetchone()
        else:
            existing = self._conn.execute(
                """SELECT id, created_at, last_used_at FROM durable_memories
                   WHERE legacy_fact_id IS NULL AND memory_kind = ? AND category = ? AND key = ?
                     AND COALESCE(project_path, '') = COALESCE(?, '')""",
                (memory_kind, category, key, project_path),
            ).fetchone()

        if existing is not None:
            created_at = str(existing["created_at"])
            if last_used_at is None:
                last_used_at = existing["last_used_at"]

        if legacy_fact_id is not None:
            self._conn.execute(
                """INSERT INTO durable_memories (
                       memory_kind, category, key, value, tags, confidence, salience,
                       source, project_path, legacy_fact_id, created_at, updated_at, last_used_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(legacy_fact_id) DO UPDATE SET
                       memory_kind = excluded.memory_kind,
                       category = excluded.category,
                       key = excluded.key,
                       value = excluded.value,
                       tags = excluded.tags,
                       confidence = excluded.confidence,
                       salience = excluded.salience,
                       source = excluded.source,
                       project_path = excluded.project_path,
                       updated_at = excluded.updated_at,
                       last_used_at = COALESCE(excluded.last_used_at, durable_memories.last_used_at)""",
                (
                    memory_kind,
                    category,
                    key,
                    value,
                    tags_json,
                    _clamp(confidence),
                    _clamp(salience),
                    source,
                    project_path,
                    legacy_fact_id,
                    created_at,
                    updated_at,
                    last_used_at,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM durable_memories WHERE legacy_fact_id = ?",
                (legacy_fact_id,),
            ).fetchone()
        else:
            if existing is None:
                self._conn.execute(
                    """INSERT INTO durable_memories (
                           memory_kind, category, key, value, tags, confidence, salience,
                           source, project_path, created_at, updated_at, last_used_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        memory_kind,
                        category,
                        key,
                        value,
                        tags_json,
                        _clamp(confidence),
                        _clamp(salience),
                        source,
                        project_path,
                        created_at,
                        updated_at,
                        last_used_at,
                    ),
                )
            else:
                self._conn.execute(
                    """UPDATE durable_memories SET
                           value = ?,
                           tags = ?,
                           confidence = ?,
                           salience = ?,
                           source = ?,
                           updated_at = ?,
                           last_used_at = COALESCE(?, last_used_at)
                       WHERE id = ?""",
                    (
                        value,
                        tags_json,
                        _clamp(confidence),
                        _clamp(salience),
                        source,
                        updated_at,
                        last_used_at,
                        existing["id"],
                    ),
                )
            row = self._conn.execute(
                """SELECT * FROM durable_memories
                   WHERE legacy_fact_id IS NULL AND memory_kind = ? AND category = ? AND key = ?
                     AND COALESCE(project_path, '') = COALESCE(?, '')
                   ORDER BY id DESC LIMIT 1""",
                (memory_kind, category, key, project_path),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to upsert durable memory")
        if commit:
            self._conn.commit()
        return self._row_to_durable_memory(row)

    def list_durable_memories(
        self,
        *,
        memory_kind: str | None = None,
        category: str | None = None,
        project_path: str | None = None,
        pinned: bool | None = None,
        limit: int = 100,
    ) -> list[DurableMemory]:
        sql = "SELECT * FROM durable_memories WHERE 1 = 1"
        params: list[Any] = []

        if memory_kind:
            sql += " AND memory_kind = ?"
            params.append(memory_kind)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if project_path is not None:
            sql += " AND COALESCE(project_path, '') = COALESCE(?, '')"
            params.append(project_path)
        if pinned is not None:
            sql += " AND pinned = ?"
            params.append(1 if pinned else 0)

        sql += " ORDER BY pinned DESC, COALESCE(last_used_at, updated_at) DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_durable_memory(row) for row in rows]

    def update_durable_memory(
        self,
        memory_id: int,
        *,
        key: str | None = None,
        value: str | None = None,
        tags: Iterable[str] | None = None,
        confidence: float | None = None,
        salience: float | None = None,
        source: str | None = None,
        project_path: str | None = None,
        last_used_at: str | None = None,
        pinned: bool | None = None,
        commit: bool = True,
    ) -> DurableMemory | None:
        row = self._conn.execute(
            "SELECT * FROM durable_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None

        now = _now_iso()
        resolved_key = key.strip() if isinstance(key, str) and key.strip() else str(row["key"])
        resolved_value = value.strip() if isinstance(value, str) and value.strip() else str(row["value"])
        resolved_tags = _json_dumps(_normalize_tags(tags if tags is not None else _json_loads_list(row["tags"])))
        resolved_confidence = _clamp(confidence if confidence is not None else float(row["confidence"]))
        resolved_salience = _clamp(salience if salience is not None else float(row["salience"]))
        resolved_source = source or str(row["source"])
        resolved_project_path = row["project_path"] if project_path is None else project_path
        resolved_last_used_at = row["last_used_at"] if last_used_at is None else last_used_at
        was_pinned = bool(row["pinned"])
        resolved_pinned = was_pinned if pinned is None else pinned
        resolved_pinned_at = row["pinned_at"]
        if pinned is not None:
            resolved_pinned_at = now if pinned else None

        self._conn.execute(
            """UPDATE durable_memories SET
                   key = ?,
                   value = ?,
                   tags = ?,
                   confidence = ?,
                   salience = ?,
                   source = ?,
                   project_path = ?,
                   updated_at = ?,
                   last_used_at = ?,
                   pinned = ?,
                   pinned_at = ?
               WHERE id = ?""",
            (
                resolved_key,
                resolved_value,
                resolved_tags,
                resolved_confidence,
                resolved_salience,
                resolved_source,
                resolved_project_path,
                now,
                resolved_last_used_at,
                1 if resolved_pinned else 0,
                resolved_pinned_at,
                memory_id,
            ),
        )

        legacy_fact_id = row["legacy_fact_id"]
        if legacy_fact_id is not None:
            try:
                self._conn.execute(
                    """UPDATE facts SET
                           category = ?,
                           key = ?,
                           value = ?,
                           source = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (
                        str(row["category"]),
                        resolved_key,
                        resolved_value,
                        resolved_source,
                        now,
                        legacy_fact_id,
                    ),
                )
            except sqlite3.IntegrityError:
                self._conn.execute(
                    "UPDATE facts SET value = ?, source = ?, updated_at = ? WHERE id = ?",
                    (resolved_value, resolved_source, now, legacy_fact_id),
                )

        updated = self.get_durable_memory(memory_id)
        if updated is None:
            raise RuntimeError("Failed to update durable memory")
        if commit:
            self._conn.commit()
        return updated

    def set_durable_memory_pinned(
        self,
        memory_id: int,
        *,
        pinned: bool,
        commit: bool = True,
    ) -> DurableMemory | None:
        return self.update_durable_memory(memory_id, pinned=pinned, commit=commit)

    # --- Pending memory candidates ---

    def queue_memory_candidate(
        self,
        *,
        session_id: str | None,
        memory_kind: str,
        category: str,
        key: str,
        value: str,
        evidence: str = "",
        tags: Iterable[str] | None = None,
        confidence: float = 0.75,
        salience: float = 0.5,
        source: str = "auto_distillation",
        project_path: str | None = None,
        commit: bool = True,
    ) -> MemoryCandidateRecord:
        now = _now_iso()
        normalized_tags = _normalize_tags(tags)
        proposed_key = key
        proposed_value = value
        proposed_evidence = evidence
        proposed_confidence = _clamp(confidence)
        proposed_salience = _clamp(salience)

        matching_memory = self._match_durable_memory(
            self.list_durable_memories(memory_kind=memory_kind, project_path=project_path, limit=50),
            category=category,
            key=key,
            value=value,
        )
        existing_memory_id = matching_memory.id if matching_memory is not None else None
        if matching_memory is not None:
            proposed_key = matching_memory.key
            proposed_value = _merge_value(matching_memory.value, proposed_value)
            proposed_tags = _merge_tags(matching_memory.tags, normalized_tags)
            proposed_confidence = min(1.0, max(matching_memory.confidence, proposed_confidence) + 0.05)
            proposed_salience = min(1.0, max(matching_memory.salience, proposed_salience) + 0.03)
        else:
            proposed_tags = normalized_tags

        pending_candidates = self.list_memory_candidates(
            status="pending",
            session_id=session_id,
            memory_kind=memory_kind,
            project_path=project_path,
            limit=50,
        )
        matching_candidate = self._match_memory_candidate(
            pending_candidates,
            category=category,
            key=proposed_key,
            value=proposed_value,
        )

        if matching_candidate is not None:
            proposed_key = matching_candidate.key
            proposed_value = _merge_value(matching_candidate.value, proposed_value)
            proposed_evidence = _merge_value(matching_candidate.evidence, proposed_evidence)
            proposed_tags = _merge_tags(matching_candidate.tags, proposed_tags)
            proposed_confidence = min(1.0, max(matching_candidate.confidence, proposed_confidence) + 0.03)
            proposed_salience = min(1.0, max(matching_candidate.salience, proposed_salience) + 0.02)
            existing_memory_id = matching_candidate.existing_memory_id or existing_memory_id
            self._conn.execute(
                """UPDATE memory_candidates SET
                       key = ?,
                       value = ?,
                       evidence = ?,
                       tags = ?,
                       confidence = ?,
                       salience = ?,
                       source = ?,
                       project_path = ?,
                       updated_at = ?,
                       existing_memory_id = ?
                   WHERE id = ?""",
                (
                    proposed_key,
                    proposed_value,
                    proposed_evidence,
                    _json_dumps(proposed_tags),
                    proposed_confidence,
                    proposed_salience,
                    source,
                    project_path,
                    now,
                    existing_memory_id,
                    matching_candidate.id,
                ),
            )
            candidate_id = matching_candidate.id
        else:
            self._conn.execute(
                """INSERT INTO memory_candidates (
                       session_id, memory_kind, category, key, value, evidence, tags,
                       confidence, salience, source, project_path, status,
                       created_at, updated_at, existing_memory_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    session_id,
                    memory_kind,
                    category,
                    proposed_key,
                    proposed_value,
                    proposed_evidence,
                    _json_dumps(proposed_tags),
                    proposed_confidence,
                    proposed_salience,
                    source,
                    project_path,
                    now,
                    now,
                    existing_memory_id,
                ),
            )
            candidate_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        row = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to queue memory candidate")
        if commit:
            self._conn.commit()
        return self._row_to_memory_candidate(row)

    def get_memory_candidate(self, candidate_id: int) -> MemoryCandidateRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        return self._row_to_memory_candidate(row) if row is not None else None

    def list_memory_candidates(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        memory_kind: str | None = None,
        project_path: str | None = None,
        limit: int = 50,
    ) -> list[MemoryCandidateRecord]:
        sql = "SELECT * FROM memory_candidates WHERE 1 = 1"
        params: list[Any] = []

        if status:
            sql += " AND status = ?"
            params.append(status)
        if session_id is not None:
            sql += " AND COALESCE(session_id, '') = COALESCE(?, '')"
            params.append(session_id)
        if memory_kind:
            sql += " AND memory_kind = ?"
            params.append(memory_kind)
        if project_path is not None:
            sql += " AND COALESCE(project_path, '') = COALESCE(?, '')"
            params.append(project_path)

        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_memory_candidate(row) for row in rows]

    def update_memory_candidate(
        self,
        candidate_id: int,
        *,
        key: str | None = None,
        value: str | None = None,
        evidence: str | None = None,
        tags: Iterable[str] | None = None,
        commit: bool = True,
    ) -> MemoryCandidateRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        if str(row["status"]) != "pending":
            return None

        now = _now_iso()
        resolved_key = key.strip() if isinstance(key, str) and key.strip() else str(row["key"])
        resolved_value = value.strip() if isinstance(value, str) and value.strip() else str(row["value"])
        resolved_evidence = evidence.strip() if isinstance(evidence, str) and evidence.strip() else str(row["evidence"] or "")
        resolved_tags = _json_dumps(_normalize_tags(tags if tags is not None else _json_loads_list(row["tags"])))

        self._conn.execute(
            """UPDATE memory_candidates SET
                   key = ?,
                   value = ?,
                   evidence = ?,
                   tags = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                resolved_key,
                resolved_value,
                resolved_evidence,
                resolved_tags,
                now,
                candidate_id,
            ),
        )

        updated = self.get_memory_candidate(candidate_id)
        if updated is None:
            raise RuntimeError("Failed to update memory candidate")
        if commit:
            self._conn.commit()
        return updated

    def approve_memory_candidate(
        self,
        candidate_id: int,
        *,
        key: str | None = None,
        value: str | None = None,
        evidence: str | None = None,
        tags: Iterable[str] | None = None,
        pin: bool = False,
        review_note: str | None = None,
    ) -> DurableMemory:
        candidate = self.get_memory_candidate(candidate_id)
        if candidate is None or candidate.status != "pending":
            raise ValueError("Memory candidate is not pending")

        if any(item is not None for item in (key, value, evidence, tags)):
            updated_candidate = self.update_memory_candidate(
                candidate_id,
                key=key,
                value=value,
                evidence=evidence,
                tags=tags,
                commit=False,
            )
            if updated_candidate is not None:
                candidate = updated_candidate

        target_memory = self.get_durable_memory(candidate.existing_memory_id) if candidate.existing_memory_id else None
        if target_memory is not None:
            durable = self.update_durable_memory(
                target_memory.id,
                key=candidate.key,
                value=candidate.value,
                tags=candidate.tags,
                confidence=max(target_memory.confidence, candidate.confidence),
                salience=max(target_memory.salience, candidate.salience),
                source="memory_review",
                project_path=target_memory.project_path or candidate.project_path,
                commit=False,
            )
            if durable is None:
                raise RuntimeError("Failed to update durable memory from candidate")
        else:
            durable = self.upsert_durable_memory(
                memory_kind=candidate.memory_kind,
                category=candidate.category,
                key=candidate.key,
                value=candidate.value,
                tags=candidate.tags,
                confidence=candidate.confidence,
                salience=candidate.salience,
                source="memory_review",
                project_path=candidate.project_path,
                commit=False,
            )

        if pin:
            pinned_memory = self.set_durable_memory_pinned(durable.id, pinned=True, commit=False)
            if pinned_memory is not None:
                durable = pinned_memory

        now = _now_iso()
        self._conn.execute(
            """UPDATE memory_candidates SET
                   status = 'approved',
                   review_note = ?,
                   reviewed_at = ?,
                   updated_at = ?,
                   last_used_at = ?,
                   promoted_memory_id = ?
               WHERE id = ?""",
            (review_note, now, now, now, durable.id, candidate_id),
        )
        self._conn.commit()
        return durable

    def reject_memory_candidate(
        self,
        candidate_id: int,
        *,
        review_note: str | None = None,
        commit: bool = True,
    ) -> MemoryCandidateRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "pending":
            return None
        now = _now_iso()
        self._conn.execute(
            """UPDATE memory_candidates SET
                   status = 'rejected',
                   review_note = ?,
                   reviewed_at = ?,
                   updated_at = ?
               WHERE id = ?""",
            (review_note, now, now, candidate_id),
        )
        candidate = self.get_memory_candidate(candidate_id)
        if commit:
            self._conn.commit()
        return candidate

    def expire_memory_candidate(
        self,
        candidate_id: int,
        *,
        review_note: str | None = None,
        commit: bool = True,
    ) -> MemoryCandidateRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "pending":
            return None
        now = _now_iso()
        self._conn.execute(
            """UPDATE memory_candidates SET
                   status = 'expired',
                   review_note = ?,
                   reviewed_at = ?,
                   expires_at = ?,
                   updated_at = ?
               WHERE id = ?""",
            (review_note, now, now, now, candidate_id),
        )
        candidate = self.get_memory_candidate(candidate_id)
        if commit:
            self._conn.commit()
        return candidate

    def delete_memory_candidate(self, candidate_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM memory_candidates WHERE id = ?", (candidate_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Projects ---

    def upsert_project(
        self,
        path: str,
        name: str,
        project_type: str = "unknown",
        git_remote: str | None = None,
        git_branch: str | None = None,
        metadata: dict | None = None,
    ) -> Project:
        now = _now_iso()
        meta_json = json.dumps(metadata or {}, sort_keys=True)
        self._conn.execute(
            """INSERT INTO projects (path, name, project_type, git_remote, git_branch, last_scanned, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   name = excluded.name,
                   project_type = excluded.project_type,
                   git_remote = excluded.git_remote,
                   git_branch = excluded.git_branch,
                   last_scanned = excluded.last_scanned,
                   metadata = excluded.metadata""",
            (path, name, project_type, git_remote, git_branch, now, meta_json),
        )
        row = self._conn.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone()
        if row is None:
            raise RuntimeError("Failed to upsert project")
        project = self._row_to_project(row)
        self._sync_project_overview_note(project, commit=False)
        self._conn.commit()
        return project

    def list_projects(self, project_type: str | None = None) -> list[Project]:
        if project_type:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE project_type = ? ORDER BY name",
                (project_type,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(self, path: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone()
        return self._row_to_project(row) if row else None

    def upsert_project_note(
        self,
        *,
        project_path: str,
        memory_kind: str = "project_note",
        note_key: str,
        title: str,
        body: str,
        category: str = "project_note",
        tags: Iterable[str] | None = None,
        confidence: float = 0.7,
        salience: float = 0.6,
        source: str = "agent",
        created_at: str | None = None,
        updated_at: str | None = None,
        last_used_at: str | None = None,
        commit: bool = True,
    ) -> ProjectNote:
        existing = self._conn.execute(
            "SELECT created_at, last_used_at FROM project_notes WHERE project_path = ? AND note_key = ?",
            (project_path, note_key),
        ).fetchone()
        created_at = created_at or (str(existing["created_at"]) if existing else _now_iso())
        updated_at = updated_at or _now_iso()
        if last_used_at is None and existing is not None:
            last_used_at = existing["last_used_at"]

        self._conn.execute(
            """INSERT INTO project_notes (
                   project_path, memory_kind, note_key, title, body, category, tags,
                   confidence, salience, source, created_at, updated_at, last_used_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_path, note_key) DO UPDATE SET
                   memory_kind = excluded.memory_kind,
                   title = excluded.title,
                   body = excluded.body,
                   category = excluded.category,
                   tags = excluded.tags,
                   confidence = excluded.confidence,
                   salience = excluded.salience,
                   source = excluded.source,
                   updated_at = excluded.updated_at,
                   last_used_at = COALESCE(excluded.last_used_at, project_notes.last_used_at)""",
            (
                project_path,
                memory_kind,
                note_key,
                title,
                body,
                category,
                _json_dumps(_normalize_tags(tags)),
                _clamp(confidence),
                _clamp(salience),
                source,
                created_at,
                updated_at,
                last_used_at,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM project_notes WHERE project_path = ? AND note_key = ?",
            (project_path, note_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to upsert project note")
        if commit:
            self._conn.commit()
        return self._row_to_project_note(row)

    def list_project_notes(self, project_path: str, limit: int = 20) -> list[ProjectNote]:
        rows = self._conn.execute(
            """SELECT * FROM project_notes
               WHERE project_path = ?
               ORDER BY COALESCE(last_used_at, updated_at) DESC
               LIMIT ?""",
            (project_path, limit),
        ).fetchall()
        return [self._row_to_project_note(row) for row in rows]

    def list_project_summary_notes(self, limit: int = 50) -> list[ProjectNote]:
        rows = self._conn.execute(
            """SELECT * FROM project_notes
               WHERE category = 'project_profile' OR note_key = 'overview'
               ORDER BY updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._row_to_project_note(row) for row in rows]

    def delete_project_note(self, note_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM project_notes WHERE id = ?", (note_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Conversation episodes ---

    def store_conversation_episode(
        self,
        *,
        session_id: str,
        summary: str,
        title: str = "",
        category: str = "session_summary",
        tags: Iterable[str] | None = None,
        confidence: float = 0.65,
        salience: float = 0.55,
        source: str = "session_manager",
        project_path: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        last_used_at: str | None = None,
        commit: bool = True,
    ) -> ConversationEpisode:
        existing = self._conn.execute(
            "SELECT created_at, last_used_at FROM conversation_episodes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        created_at = created_at or (str(existing["created_at"]) if existing else _now_iso())
        updated_at = updated_at or _now_iso()
        if last_used_at is None and existing is not None:
            last_used_at = existing["last_used_at"]

        self._conn.execute(
            """INSERT INTO conversation_episodes (
                   session_id, memory_kind, title, summary, category, tags,
                   confidence, salience, source, project_path, created_at, updated_at, last_used_at
               ) VALUES (?, 'session_summary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   title = excluded.title,
                   summary = excluded.summary,
                   category = excluded.category,
                   tags = excluded.tags,
                   confidence = excluded.confidence,
                   salience = excluded.salience,
                   source = excluded.source,
                   project_path = excluded.project_path,
                   updated_at = excluded.updated_at,
                   last_used_at = COALESCE(excluded.last_used_at, conversation_episodes.last_used_at)""",
            (
                session_id,
                title,
                summary,
                category,
                _json_dumps(_normalize_tags(tags or ["session_summary", category])),
                _clamp(confidence),
                _clamp(salience),
                source,
                project_path,
                created_at,
                updated_at,
                last_used_at,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM conversation_episodes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to store conversation episode")
        if commit:
            self._conn.commit()
        return self._row_to_conversation_episode(row)

    def delete_conversation_episode(self, session_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM conversation_episodes WHERE session_id = ?",
            (session_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_conversation_episodes(
        self,
        *,
        project_path: str | None = None,
        limit: int = 20,
    ) -> list[ConversationEpisode]:
        sql = "SELECT * FROM conversation_episodes WHERE 1 = 1"
        params: list[Any] = []
        if project_path is not None:
            sql += " AND COALESCE(project_path, '') = COALESCE(?, '')"
            params.append(project_path)
        sql += " ORDER BY COALESCE(last_used_at, updated_at) DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_conversation_episode(row) for row in rows]

    def delete_conversation_episode_by_id(self, episode_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM conversation_episodes WHERE id = ?",
            (episode_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- File index and chunks ---

    def index_file(self, path: str, project_id: int | None = None) -> None:
        p = Path(path)
        if not p.exists():
            return
        try:
            stat = p.stat()
        except OSError:
            return
        self._conn.execute(
            """INSERT INTO file_index (path, name, extension, size, modified_at, project_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   size = excluded.size,
                   modified_at = excluded.modified_at,
                   project_id = excluded.project_id""",
            (
                str(p),
                p.name,
                p.suffix.lstrip(".") or None,
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                project_id,
            ),
        )
        self._upsert_file_chunks(p, project_id=project_id)

    def commit_file_index(self):
        self._conn.commit()

    def get_project_file_index(self, project_id: int) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT path, size, modified_at FROM file_index WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return {
            str(row["path"]): {
                "size": int(row["size"] or 0),
                "modified_at": row["modified_at"],
            }
            for row in rows
        }

    def prune_project_files(
        self,
        project_id: int,
        keep_paths: Iterable[str],
        *,
        commit: bool = True,
    ) -> int:
        keep = {str(path) for path in keep_paths}
        rows = self._conn.execute(
            "SELECT path FROM file_index WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        stale_paths = [str(row["path"]) for row in rows if str(row["path"]) not in keep]
        if not stale_paths:
            return 0

        placeholders = ", ".join("?" for _ in stale_paths)
        self._conn.execute(
            f"DELETE FROM file_index WHERE path IN ({placeholders})",
            stale_paths,
        )
        self._conn.execute(
            f"DELETE FROM file_chunks WHERE file_path IN ({placeholders})",
            stale_paths,
        )
        if commit:
            self._conn.commit()
        return len(stale_paths)

    def search_files(self, query: str, limit: int = 20) -> list[dict]:
        pattern = f"%{query}%"
        rows = self._conn.execute(
            """SELECT * FROM file_index
               WHERE name LIKE ? OR path LIKE ?
               ORDER BY modified_at DESC LIMIT ?""",
            (pattern, pattern, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Rich search ---

    def search_memories(
        self,
        query: str,
        limit: int = 20,
        *,
        project_path: str | None = None,
        session_id: str | None = None,
        kinds: Iterable[str] | None = None,
        touch_results: bool = True,
    ) -> list[MemorySearchResult]:
        query = query.strip()
        if not query:
            return []

        kind_filter = {kind.strip().lower() for kind in (kinds or []) if kind.strip()}
        tokens = _tokenize(query)
        results: list[MemorySearchResult] = []

        results.extend(
            self._search_durable_memory_candidates(
                query=query,
                tokens=tokens,
                project_path=project_path,
                kind_filter=kind_filter,
                candidate_limit=max(limit * 6, 40),
            )
        )
        if session_id:
            results.extend(
                self._search_memory_candidate_candidates(
                    query=query,
                    tokens=tokens,
                    session_id=session_id,
                    project_path=project_path,
                    kind_filter=kind_filter,
                    candidate_limit=max(limit * 4, 20),
                )
            )
        results.extend(
            self._search_project_note_candidates(
                query=query,
                tokens=tokens,
                project_path=project_path,
                kind_filter=kind_filter,
                candidate_limit=max(limit * 4, 20),
            )
        )
        results.extend(
            self._search_episode_candidates(
                query=query,
                tokens=tokens,
                project_path=project_path,
                kind_filter=kind_filter,
                candidate_limit=max(limit * 4, 20),
            )
        )
        results.extend(
            self._search_file_chunk_candidates(
                query=query,
                tokens=tokens,
                project_path=project_path,
                kind_filter=kind_filter,
                candidate_limit=max(limit * 3, 15),
            )
        )

        deduped: dict[tuple[str, int], MemorySearchResult] = {}
        for result in results:
            key = (result.source_table, result.id)
            existing = deduped.get(key)
            if existing is None or result.score > existing.score:
                deduped[key] = result

        ranked = sorted(
            deduped.values(),
            key=lambda item: (item.score, item.updated_at),
            reverse=True,
        )[:limit]
        if touch_results:
            self._touch_search_results(ranked)
        return ranked

    def search_file_chunks(
        self,
        query: str,
        limit: int = 10,
        *,
        project_path: str | None = None,
        touch_results: bool = True,
    ) -> list[FileChunk]:
        results = self.search_memories(
            query,
            limit=limit,
            project_path=project_path,
            kinds={"file_chunk"},
            touch_results=touch_results,
        )
        chunk_ids = [result.id for result in results if result.source_table == "file_chunks"]
        if not chunk_ids:
            return []
        placeholders = ", ".join("?" for _ in chunk_ids)
        rows = self._conn.execute(
            f"SELECT * FROM file_chunks WHERE id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
        by_id = {row["id"]: self._row_to_file_chunk(row) for row in rows}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    # --- Stats ---

    def stats(self) -> dict:
        facts_count = self._count_table("facts")
        projects_count = self._count_table("projects")
        files_count = self._count_table("file_index")
        durable_count = self._count_table("durable_memories")
        candidate_count = self._count_table("memory_candidates")
        episodes_count = self._count_table("conversation_episodes")
        notes_count = self._count_table("project_notes")
        file_chunks_count = self._count_table("file_chunks")
        latest_scan = self._conn.execute("SELECT MAX(last_scanned) FROM projects").fetchone()[0]
        pinned_count = int(
            self._conn.execute("SELECT COUNT(*) FROM durable_memories WHERE pinned = 1").fetchone()[0]
        )
        candidate_status_counts = {
            row["status"]: row["cnt"]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM memory_candidates GROUP BY status"
            ).fetchall()
        }

        fact_categories = {
            row["category"]: row["cnt"]
            for row in self._conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM facts GROUP BY category"
            ).fetchall()
        }

        memory_types: dict[str, int] = {}
        for table in ("durable_memories", "memory_candidates", "conversation_episodes", "project_notes", "file_chunks"):
            for row in self._conn.execute(
                f"SELECT memory_kind, COUNT(*) AS cnt FROM {table} GROUP BY memory_kind"
            ).fetchall():
                memory_types[row["memory_kind"]] = memory_types.get(row["memory_kind"], 0) + row["cnt"]

        memory_categories: dict[str, int] = {}
        for table in ("durable_memories", "memory_candidates", "conversation_episodes", "project_notes"):
            for row in self._conn.execute(
                f"SELECT category, COUNT(*) AS cnt FROM {table} GROUP BY category"
            ).fetchall():
                memory_categories[row["category"]] = memory_categories.get(row["category"], 0) + row["cnt"]

        return {
            "facts": facts_count,
            "projects": projects_count,
            "files_indexed": files_count,
            "last_project_scan_at": latest_scan,
            "fact_categories": fact_categories,
            "durable_memories": durable_count,
            "memory_candidates": candidate_count,
            "pending_memory_candidates": candidate_status_counts.get("pending", 0),
            "approved_memory_candidates": candidate_status_counts.get("approved", 0),
            "rejected_memory_candidates": candidate_status_counts.get("rejected", 0),
            "expired_memory_candidates": candidate_status_counts.get("expired", 0),
            "pinned_durable_memories": pinned_count,
            "conversation_episodes": episodes_count,
            "project_notes": notes_count,
            "file_chunks": file_chunks_count,
            "memory_types": memory_types,
            "memory_categories": memory_categories,
        }

    # --- Backfill and migration ---

    def _ensure_schema_version(self) -> None:
        """Track and upgrade the schema version.

        On a fresh DB the schema_version table is empty — seed it at
        SCHEMA_VERSION.  On an existing DB, run any pending migrations
        sequentially to bring it up to SCHEMA_VERSION.
        """
        row = self._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_version (id, version) VALUES (1, ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()
            return

        current = int(row["version"])
        if current >= SCHEMA_VERSION:
            return

        # Run migrations sequentially from current+1 → SCHEMA_VERSION.
        for target in range(current + 1, SCHEMA_VERSION + 1):
            migrator = _SCHEMA_MIGRATIONS.get(target)
            if migrator is None:
                raise RuntimeError(
                    f"No migration defined for schema version {target}"
                )
            migrator(self._conn)
            self._conn.execute(
                "UPDATE schema_version SET version = ? WHERE id = 1",
                (target,),
            )
            self._conn.commit()

    def _ensure_schema_extensions(self) -> None:
        self._ensure_column("durable_memories", "pinned", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("durable_memories", "pinned_at", "TEXT")
        self._ensure_column("file_chunks", "file_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("file_chunks", "extension", "TEXT")
        self._ensure_column("file_chunks", "line_start", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("file_chunks", "line_end", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("file_chunks", "byte_size", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("file_chunks", "modified_at", "TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_durable_pinned ON durable_memories(pinned)")
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing_columns = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in existing_columns:
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_legacy_data(self) -> None:
        self._backfill_facts_into_durable_memories()
        self._backfill_projects_into_project_notes()
        self._backfill_session_summaries()
        self._conn.commit()

    def _backfill_facts_into_durable_memories(self) -> None:
        rows = self._conn.execute("SELECT * FROM facts").fetchall()
        for row in rows:
            fact = self._row_to_fact(row)
            project_path = self._resolve_project_path(fact.category, fact.key, fact.value)
            self.upsert_durable_memory(
                memory_kind=_memory_kind_for_category(fact.category, project_path),
                category=fact.category,
                key=fact.key,
                value=fact.value,
                tags=[fact.category, _memory_kind_for_category(fact.category, project_path)],
                confidence=_default_confidence_for_kind(fact.category),
                salience=_default_salience_for_kind(fact.category),
                source=fact.source,
                project_path=project_path,
                legacy_fact_id=fact.id,
                created_at=fact.created_at,
                updated_at=fact.updated_at,
                last_used_at=fact.updated_at,
                commit=False,
            )

    def _backfill_projects_into_project_notes(self) -> None:
        rows = self._conn.execute("SELECT * FROM projects").fetchall()
        for row in rows:
            self._sync_project_overview_note(self._row_to_project(row), commit=False)

    def _backfill_session_summaries(self) -> None:
        history_dir = settings.history_dir
        if not history_dir.exists():
            return

        for path in history_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            if not isinstance(payload, dict):
                continue

            summary = str(payload.get("summary", "")).strip()
            if not summary:
                continue

            session_id = str(payload.get("session_id") or path.stem)
            updated_at = str(payload.get("updated_at") or _file_timestamp(path))
            title = self._episode_title_from_payload(payload)
            self.store_conversation_episode(
                session_id=session_id,
                summary=summary,
                title=title,
                source="session_manager",
                created_at=updated_at,
                updated_at=updated_at,
                last_used_at=updated_at,
                commit=False,
            )

    # --- Search helpers ---

    def _search_durable_memory_candidates(
        self,
        *,
        query: str,
        tokens: list[str],
        project_path: str | None,
        kind_filter: set[str],
        candidate_limit: int,
    ) -> list[MemorySearchResult]:
        sql = """
            SELECT * FROM durable_memories
            WHERE 1 = 1
        """
        params: list[Any] = []

        if project_path:
            sql += " AND (project_path = ? OR project_path IS NULL)"
            params.append(project_path)

        if kind_filter:
            placeholders = ", ".join("?" for _ in kind_filter)
            sql += f" AND memory_kind IN ({placeholders})"
            params.extend(sorted(kind_filter))

        sql, params = self._append_token_filter(
            sql,
            params,
            tokens,
            ["category", "key", "value", "tags", "COALESCE(project_path, '')", "memory_kind"],
        )
        sql += " ORDER BY COALESCE(last_used_at, updated_at) DESC LIMIT ?"
        params.append(candidate_limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [
            self._build_search_result(
                source_table="durable_memories",
                row=row,
                key=str(row["key"]),
                text=str(row["value"]),
                query=query,
                tokens=tokens,
                legacy_fact_id=row["legacy_fact_id"],
            )
            for row in rows
        ]

    def _search_memory_candidate_candidates(
        self,
        *,
        query: str,
        tokens: list[str],
        session_id: str,
        project_path: str | None,
        kind_filter: set[str],
        candidate_limit: int,
    ) -> list[MemorySearchResult]:
        sql = """
            SELECT * FROM memory_candidates
            WHERE status = 'pending' AND COALESCE(session_id, '') = COALESCE(?, '')
        """
        params: list[Any] = [session_id]

        if project_path:
            sql += " AND (project_path = ? OR project_path IS NULL)"
            params.append(project_path)

        if kind_filter:
            placeholders = ", ".join("?" for _ in kind_filter)
            sql += f" AND memory_kind IN ({placeholders})"
            params.extend(sorted(kind_filter))

        sql, params = self._append_token_filter(
            sql,
            params,
            tokens,
            ["category", "key", "value", "evidence", "tags", "COALESCE(project_path, '')", "memory_kind"],
        )
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(candidate_limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [
            self._build_search_result(
                source_table="memory_candidates",
                row=row,
                key=str(row["key"]),
                text=str(row["value"]),
                query=query,
                tokens=tokens,
                review_state=str(row["status"]),
            )
            for row in rows
        ]

    def _search_project_note_candidates(
        self,
        *,
        query: str,
        tokens: list[str],
        project_path: str | None,
        kind_filter: set[str],
        candidate_limit: int,
    ) -> list[MemorySearchResult]:
        if kind_filter and "project_note" not in kind_filter:
            if not ({"project_note", "project_constraint", "project_profile"} & kind_filter):
                return []

        sql = """
            SELECT * FROM project_notes
            WHERE 1 = 1
        """
        params: list[Any] = []
        if project_path:
            sql += " AND project_path = ?"
            params.append(project_path)
        if kind_filter:
            placeholders = ", ".join("?" for _ in kind_filter)
            sql += f" AND memory_kind IN ({placeholders})"
            params.extend(sorted(kind_filter))
        sql, params = self._append_token_filter(
            sql,
            params,
            tokens,
            ["title", "body", "category", "tags", "project_path", "note_key"],
        )
        sql += " ORDER BY COALESCE(last_used_at, updated_at) DESC LIMIT ?"
        params.append(candidate_limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            self._build_search_result(
                source_table="project_notes",
                row=row,
                key=str(row["title"] or row["note_key"]),
                text=str(row["body"]),
                query=query,
                tokens=tokens,
            )
            for row in rows
        ]

    def _search_episode_candidates(
        self,
        *,
        query: str,
        tokens: list[str],
        project_path: str | None,
        kind_filter: set[str],
        candidate_limit: int,
    ) -> list[MemorySearchResult]:
        if kind_filter and "session_summary" not in kind_filter:
            return []

        sql = """
            SELECT * FROM conversation_episodes
            WHERE 1 = 1
        """
        params: list[Any] = []
        if project_path:
            sql += " AND (project_path = ? OR project_path IS NULL)"
            params.append(project_path)
        sql, params = self._append_token_filter(
            sql,
            params,
            tokens,
            ["title", "summary", "category", "tags", "COALESCE(project_path, '')", "session_id"],
        )
        sql += " ORDER BY COALESCE(last_used_at, updated_at) DESC LIMIT ?"
        params.append(candidate_limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            self._build_search_result(
                source_table="conversation_episodes",
                row=row,
                key=str(row["title"] or row["session_id"]),
                text=str(row["summary"]),
                query=query,
                tokens=tokens,
            )
            for row in rows
        ]

    def _search_file_chunk_candidates(
        self,
        *,
        query: str,
        tokens: list[str],
        project_path: str | None,
        kind_filter: set[str],
        candidate_limit: int,
    ) -> list[MemorySearchResult]:
        if not tokens:
            return []
        if kind_filter and "file_chunk" not in kind_filter:
            return []

        sql = """
            SELECT * FROM file_chunks
            WHERE 1 = 1
        """
        params: list[Any] = []
        if project_path:
            sql += " AND project_path = ?"
            params.append(project_path)
        sql, params = self._append_token_filter(
            sql,
            params,
            tokens,
            ["file_path", "content", "tags", "category", "COALESCE(project_path, '')"],
        )
        sql += " ORDER BY COALESCE(last_used_at, updated_at) DESC LIMIT ?"
        params.append(candidate_limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            self._build_search_result(
                source_table="file_chunks",
                row=row,
                key=f"{row['file_path']}:{row['line_start']}-{row['line_end']}",
                text=str(row["content"]),
                query=query,
                tokens=tokens,
            )
            for row in rows
        ]

    def _append_token_filter(
        self,
        sql: str,
        params: list[Any],
        tokens: list[str],
        columns: list[str],
    ) -> tuple[str, list[Any]]:
        if not tokens:
            return sql, params

        token_clauses: list[str] = []
        for token in tokens:
            pattern = f"%{token}%"
            per_token = []
            for column in columns:
                per_token.append(f"LOWER({column}) LIKE ?")
                params.append(pattern)
            token_clauses.append("(" + " OR ".join(per_token) + ")")

        sql += " AND (" + " OR ".join(token_clauses) + ")"
        return sql, params

    def _build_search_result(
        self,
        *,
        source_table: str,
        row: sqlite3.Row,
        key: str,
        text: str,
        query: str,
        tokens: list[str],
        legacy_fact_id: int | None = None,
        review_state: str = "approved",
        pinned: bool | None = None,
    ) -> MemorySearchResult:
        result = MemorySearchResult(
            id=int(row["id"]),
            source_table=source_table,
            memory_kind=str(row["memory_kind"]),
            category=str(row["category"]),
            key=key,
            text=text,
            tags=_json_loads_list(row["tags"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            source=str(row["source"]),
            project_path=row["project_path"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_used_at=row["last_used_at"],
            legacy_fact_id=legacy_fact_id,
            review_state=review_state,
            pinned=bool(row["pinned"]) if pinned is None and "pinned" in row.keys() else bool(pinned),
            score=0.0,
        )
        result.score = self._score_search_result(result, query, tokens)
        return result

    def _score_search_result(
        self,
        result: MemorySearchResult,
        query: str,
        tokens: list[str],
    ) -> float:
        query_lower = query.lower()
        key_lower = result.key.lower()
        text_lower = result.text.lower()
        category_lower = result.category.lower()
        kind_lower = result.memory_kind.lower()
        project_path_lower = (result.project_path or "").lower()
        tags_text = " ".join(result.tags).lower()

        score = (result.salience * 3.0) + (result.confidence * 1.75)
        score += _recency_score(result.last_used_at or result.updated_at) * 2.5

        if query_lower in key_lower:
            score += 8.0
        if query_lower in text_lower:
            score += 5.0
        if query_lower in category_lower or query_lower in kind_lower:
            score += 3.0
        if project_path_lower and query_lower in project_path_lower:
            score += 2.5

        for token in tokens:
            if token in key_lower:
                score += 3.0
            if token in text_lower:
                score += 1.6
            if token in tags_text:
                score += 2.2
            if token in category_lower or token in kind_lower:
                score += 1.8
            if project_path_lower and token in project_path_lower:
                score += 1.2

        if result.memory_kind == "preference":
            score += 0.4
        elif result.memory_kind == "ongoing_goal":
            score += 0.45
        elif result.memory_kind == "workflow":
            score += 0.35
        elif result.memory_kind == "project_constraint":
            score += 0.45
        elif result.memory_kind == "session_summary":
            score += 0.3

        if result.source_table == "memory_candidates":
            score += 0.6
        if result.pinned:
            score += 0.5

        return score

    def _match_durable_memory(
        self,
        memories: Iterable[DurableMemory],
        *,
        category: str,
        key: str,
        value: str,
    ) -> DurableMemory | None:
        best: DurableMemory | None = None
        best_score = 0.0
        for memory in memories:
            score = 0.0
            if memory.key == key:
                score += 1.0
            score += _similarity(memory.value, value)
            if memory.category == category:
                score += 0.15
            if score > best_score:
                best_score = score
                best = memory
        return best if best_score >= 0.9 else None

    def _match_memory_candidate(
        self,
        candidates: Iterable[MemoryCandidateRecord],
        *,
        category: str,
        key: str,
        value: str,
    ) -> MemoryCandidateRecord | None:
        best: MemoryCandidateRecord | None = None
        best_score = 0.0
        for candidate in candidates:
            score = 0.0
            if candidate.key == key:
                score += 1.0
            score += _similarity(candidate.value, value)
            if candidate.category == category:
                score += 0.15
            if score > best_score:
                best_score = score
                best = candidate
        return best if best_score >= 0.9 else None

    def _touch_search_results(self, results: list[MemorySearchResult]) -> None:
        if not results:
            return
        now = _now_iso()
        tables: dict[str, list[int]] = {}
        for result in results:
            tables.setdefault(result.source_table, []).append(result.id)

        for table, ids in tables.items():
            placeholders = ", ".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE {table} SET last_used_at = ? WHERE id IN ({placeholders})",
                [now, *ids],
            )
        self._conn.commit()

    # --- Row helpers ---

    def _fact_from_search_result(self, result: MemorySearchResult) -> Fact:
        synthetic_id_offsets = {
            "durable_memories": 1_000_000,
            "project_notes": 2_000_000,
            "conversation_episodes": 3_000_000,
            "file_chunks": 4_000_000,
        }
        fact_id = result.legacy_fact_id or (synthetic_id_offsets.get(result.source_table, 9_000_000) + result.id)
        return Fact(
            id=fact_id,
            category=result.category,
            key=result.key,
            value=result.text,
            source=result.source,
            created_at=result.created_at,
            updated_at=result.updated_at,
        )

    def _sync_project_overview_note(self, project: Project, *, commit: bool) -> None:
        body = self._project_overview_text(project)
        tags = [project.project_type, "project_overview", project.name]
        existing = self._conn.execute(
            "SELECT * FROM project_notes WHERE project_path = ? AND note_key = ?",
            (project.path, "overview"),
        ).fetchone()
        normalized_tags = _normalize_tags(tags)
        if existing is not None:
            if (
                str(existing["title"]) == f"{project.name} overview"
                and str(existing["body"]) == body
                and str(existing["memory_kind"]) == "project_note"
                and str(existing["category"]) == "project_profile"
                and _json_loads_list(existing["tags"]) == normalized_tags
            ):
                return
        self.upsert_project_note(
            project_path=project.path,
            memory_kind="project_note",
            note_key="overview",
            title=f"{project.name} overview",
            body=body,
            category="project_profile",
            tags=normalized_tags,
            confidence=0.9,
            salience=0.75,
            source="scanner",
            created_at=project.last_scanned,
            updated_at=project.last_scanned,
            last_used_at=project.last_scanned,
            commit=commit,
        )

    def _project_overview_text(self, project: Project) -> str:
        parts = [f"Project {project.name}", f"Type: {project.project_type}", f"Path: {project.path}"]
        if project.git_branch:
            parts.append(f"Branch: {project.git_branch}")
        if project.git_remote:
            parts.append(f"Remote: {project.git_remote}")
        package_name = project.metadata.get("package_name") if project.metadata else None
        if package_name:
            parts.append(f"Package: {package_name}")
        description = project.metadata.get("description") if project.metadata else None
        if description:
            parts.append(f"Description: {description}")
        file_types = project.metadata.get("file_types") if project.metadata else None
        if isinstance(file_types, dict) and file_types:
            top_types = ", ".join(f"{ext}:{count}" for ext, count in list(file_types.items())[:6])
            parts.append(f"File types: {top_types}")
        stack = project.metadata.get("stack") if project.metadata else None
        if isinstance(stack, list) and stack:
            parts.append(f"Stack: {', '.join(str(item) for item in stack[:8])}")
        entry_points = project.metadata.get("entry_points") if project.metadata else None
        if isinstance(entry_points, list) and entry_points:
            parts.append("Likely entry points:")
            parts.extend(f"- {item}" for item in entry_points[:8])
        useful_commands = project.metadata.get("useful_commands") if project.metadata else None
        if isinstance(useful_commands, list) and useful_commands:
            parts.append("Useful commands:")
            parts.extend(f"- {item}" for item in useful_commands[:8])
        file_map = project.metadata.get("file_map") if project.metadata else None
        if isinstance(file_map, list) and file_map:
            parts.append("File map:")
            parts.extend(f"- {item}" for item in file_map[:10])
        notable_modules = project.metadata.get("notable_modules") if project.metadata else None
        if isinstance(notable_modules, list) and notable_modules:
            parts.append("Notable modules:")
            parts.extend(f"- {item}" for item in notable_modules[:12])
        indexed_files_count = project.metadata.get("indexed_files_count") if project.metadata else None
        if isinstance(indexed_files_count, int) and indexed_files_count > 0:
            parts.append(f"Indexed files: {indexed_files_count}")
        return "\n".join(parts)

    def _episode_title_from_payload(self, payload: dict[str, Any]) -> str:
        recent_items = payload.get("recent_items")
        if isinstance(recent_items, list):
            for item in recent_items:
                if isinstance(item, dict) and item.get("role") == "user":
                    text = _extract_message_text(item)
                    if text:
                        return _clip(text, 80)
        session_id = payload.get("session_id")
        return f"Session {session_id}" if session_id else "Session summary"

    def _resolve_project_path(self, category: str, key: str, value: str) -> str | None:
        if category.strip().lower() not in {"project", "project_note", "project_notes", "project_profile", "workflow"}:
            return None

        candidates = [key, value]
        for candidate in candidates:
            if not candidate:
                continue
            exact = self._conn.execute("SELECT path FROM projects WHERE path = ?", (candidate,)).fetchone()
            if exact is not None:
                return str(exact["path"])
            by_name = self._conn.execute("SELECT path FROM projects WHERE name = ?", (candidate,)).fetchone()
            if by_name is not None:
                return str(by_name["path"])
        return None

    def _upsert_file_chunks(self, path: Path, *, project_id: int | None) -> None:
        chunks = _extract_file_chunks(path)
        file_path = str(path)
        project_path = self._project_path_for_project_id(project_id)
        try:
            stat = path.stat()
        except OSError:
            self._conn.execute("DELETE FROM file_chunks WHERE file_path = ?", (file_path,))
            return
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

        if not chunks:
            self._conn.execute("DELETE FROM file_chunks WHERE file_path = ?", (file_path,))
            return

        now = _now_iso()
        for index, chunk in enumerate(chunks):
            content_hash = hashlib.sha1(chunk.content.encode("utf-8")).hexdigest()[:16]
            tags = _normalize_tags([path.suffix.lstrip("."), path.name])
            self._conn.execute(
                """INSERT INTO file_chunks (
                       file_path, project_path, file_name, extension, memory_kind, category, chunk_index,
                       line_start, line_end, byte_size, modified_at, content,
                       content_hash, tags, confidence, salience, source, token_estimate,
                       created_at, updated_at, last_used_at
                   ) VALUES (?, ?, ?, ?, 'file_chunk', 'file_chunk', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scanner', ?, ?, ?, ?)
                   ON CONFLICT(file_path, chunk_index) DO UPDATE SET
                       project_path = excluded.project_path,
                       file_name = excluded.file_name,
                       extension = excluded.extension,
                       line_start = excluded.line_start,
                       line_end = excluded.line_end,
                       byte_size = excluded.byte_size,
                       modified_at = excluded.modified_at,
                       content = excluded.content,
                       content_hash = excluded.content_hash,
                       tags = excluded.tags,
                       confidence = excluded.confidence,
                       salience = excluded.salience,
                       token_estimate = excluded.token_estimate,
                       updated_at = excluded.updated_at""",
                (
                    file_path,
                    project_path,
                    path.name,
                    path.suffix.lstrip(".") or None,
                    index,
                    chunk.line_start,
                    chunk.line_end,
                    stat.st_size,
                    modified_at,
                    chunk.content,
                    content_hash,
                    _json_dumps(tags),
                    0.4,
                    0.35,
                    max(1, len(chunk.content.split())),
                    now,
                    now,
                    now,
                ),
            )

        self._conn.execute(
            "DELETE FROM file_chunks WHERE file_path = ? AND chunk_index >= ?",
            (file_path, len(chunks)),
        )

    def _project_path_for_project_id(self, project_id: int | None) -> str | None:
        if project_id is None:
            return None
        row = self._conn.execute("SELECT path FROM projects WHERE id = ?", (project_id,)).fetchone()
        return str(row["path"]) if row is not None else None

    def _count_table(self, table: str) -> int:
        return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            path=row["path"],
            name=row["name"],
            project_type=row["project_type"],
            git_remote=row["git_remote"],
            git_branch=row["git_branch"],
            last_scanned=row["last_scanned"],
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_durable_memory(row: sqlite3.Row) -> DurableMemory:
        return DurableMemory(
            id=row["id"],
            memory_kind=row["memory_kind"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            tags=_json_loads_list(row["tags"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            source=row["source"],
            project_path=row["project_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            pinned=bool(row["pinned"]),
            pinned_at=row["pinned_at"],
            legacy_fact_id=row["legacy_fact_id"],
        )

    @staticmethod
    def _row_to_memory_candidate(row: sqlite3.Row | None) -> MemoryCandidateRecord | None:
        if row is None:
            return None
        return MemoryCandidateRecord(
            id=row["id"],
            session_id=row["session_id"],
            memory_kind=row["memory_kind"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            evidence=row["evidence"],
            tags=_json_loads_list(row["tags"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            source=row["source"],
            project_path=row["project_path"],
            status=row["status"],
            review_note=row["review_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            reviewed_at=row["reviewed_at"],
            expires_at=row["expires_at"],
            existing_memory_id=row["existing_memory_id"],
            promoted_memory_id=row["promoted_memory_id"],
        )

    @staticmethod
    def _row_to_conversation_episode(row: sqlite3.Row) -> ConversationEpisode:
        return ConversationEpisode(
            id=row["id"],
            session_id=row["session_id"],
            memory_kind=row["memory_kind"],
            title=row["title"],
            summary=row["summary"],
            category=row["category"],
            tags=_json_loads_list(row["tags"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            source=row["source"],
            project_path=row["project_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
        )

    @staticmethod
    def _row_to_project_note(row: sqlite3.Row) -> ProjectNote:
        return ProjectNote(
            id=row["id"],
            project_path=row["project_path"],
            memory_kind=row["memory_kind"],
            note_key=row["note_key"],
            title=row["title"],
            body=row["body"],
            category=row["category"],
            tags=_json_loads_list(row["tags"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
        )

    @staticmethod
    def _row_to_file_chunk(row: sqlite3.Row) -> FileChunk:
        return FileChunk(
            id=row["id"],
            file_path=row["file_path"],
            project_path=row["project_path"],
            file_name=row["file_name"],
            extension=row["extension"],
            memory_kind=row["memory_kind"],
            category=row["category"],
            chunk_index=row["chunk_index"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            byte_size=row["byte_size"],
            modified_at=row["modified_at"],
            content=row["content"],
            content_hash=row["content_hash"],
            tags=_json_loads_list(row["tags"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            source=row["source"],
            token_estimate=row["token_estimate"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _extract_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _extract_file_chunks(path: Path) -> list[ExtractedFileChunk]:
    try:
        stat = path.stat()
        if stat.st_size > _MAX_TEXT_FILE_BYTES:
            return []
        raw = path.read_bytes()
    except OSError:
        return []

    if not raw or _looks_binary_bytes(raw[:4096]):
        return []

    text = raw.decode("utf-8", errors="ignore")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if _looks_binary(text):
        return []

    chunks: list[ExtractedFileChunk] = []
    current_lines: list[str] = []
    current_length = 0
    line_start = 1
    line_number = 1

    for line in text.split("\n"):
        line_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + line_length > _TEXT_CHUNK_SIZE:
            content = "\n".join(current_lines).strip()
            if content:
                chunks.append(
                    ExtractedFileChunk(
                        content=content,
                        line_start=line_start,
                        line_end=max(line_start, line_number - 1),
                    )
                )
            if len(chunks) >= _MAX_FILE_CHUNKS:
                return chunks
            current_lines = [line]
            current_length = len(line)
            line_start = line_number
        else:
            current_lines.append(line)
            current_length += line_length
        line_number += 1

    if current_lines and len(chunks) < _MAX_FILE_CHUNKS:
        content = "\n".join(current_lines).strip()
        if content:
            chunks.append(
                ExtractedFileChunk(
                    content=content,
                    line_start=line_start,
                    line_end=max(line_start, line_number - 1),
                )
            )

    return chunks


def _looks_binary_bytes(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    control_chars = sum(1 for byte in data if byte < 9 or (13 < byte < 32))
    return control_chars > max(12, len(data) // 8)


def _looks_binary(text: str) -> bool:
    if not text:
        return False
    control_chars = sum(1 for ch in text[:1000] if ord(ch) < 9 or (13 < ord(ch) < 32))
    return control_chars > 10


def _clip(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."


def _tokenize(query: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9._/-]{3,}", query.lower())
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered[:10]


def _normalize_tags(tags: Iterable[str] | None) -> list[str]:
    if not tags:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        text = str(tag).strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _recency_score(value: str | None) -> float:
    parsed = _parse_iso(value)
    if parsed is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - parsed).total_seconds() / 86400.0)
    return 1.0 / (1.0 + (age_days / 30.0))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _memory_kind_for_category(category: str, project_path: str | None = None) -> str:
    normalized = category.strip().lower()
    if project_path or normalized in {"project", "project_note", "project_notes", "project_profile"}:
        return "project_note"
    if normalized in {"user", "profile", "identity", "personal", "user_profile"}:
        return "user_profile"
    if normalized in {"preference", "preferences", "likes", "dislikes"}:
        return "preference"
    if normalized in {"ongoing_goal", "goal", "goals"}:
        return "ongoing_goal"
    if normalized in {"workflow", "workflows", "routine", "process", "automation", "habit"}:
        return "workflow"
    if normalized in {"project_constraint", "constraint", "constraints"}:
        return "project_constraint"
    if normalized in {"session", "session_summary", "summary", "episode", "conversation"}:
        return "session_summary"
    return "durable_memory"


def _default_confidence_for_kind(category: str) -> float:
    kind = _memory_kind_for_category(category)
    if kind == "user_profile":
        return 0.9
    if kind == "preference":
        return 0.85
    if kind == "ongoing_goal":
        return 0.78
    if kind == "workflow":
        return 0.8
    if kind in {"project_note", "project_constraint"}:
        return 0.8
    if kind == "session_summary":
        return 0.65
    return 0.75


def _default_salience_for_kind(category: str) -> float:
    kind = _memory_kind_for_category(category)
    if kind in {"user_profile", "preference"}:
        return 0.85
    if kind in {"ongoing_goal", "workflow", "project_note", "project_constraint"}:
        return 0.7
    if kind == "session_summary":
        return 0.55
    return 0.6


# Singleton instance
_store: KnowledgeStore | None = None


def get_knowledge_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store
