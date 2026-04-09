"""Deployment state management and persistence."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from boss.config import settings

logger = logging.getLogger(__name__)

# Bump when the serialized format changes.
DEPLOYMENT_VERSION = 1


class DeploymentStatus(StrEnum):
    PENDING = "pending"
    BUILDING = "building"
    DEPLOYING = "deploying"
    LIVE = "live"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TORN_DOWN = "torn_down"


class DeploymentTarget(StrEnum):
    """Supported deployment adapter types."""
    STATIC = "static"        # Static file hosting (e.g. Vercel, Netlify static)
    PREVIEW = "preview"      # Preview/staging deploy


@dataclass
class Deployment:
    """A single deployment record."""

    deployment_id: str
    project_path: str
    session_id: str
    adapter: str                # which adapter performed the deploy
    target: str = DeploymentTarget.PREVIEW.value
    status: str = DeploymentStatus.PENDING.value
    preview_url: str | None = None
    build_log: str = ""
    deploy_log: str = ""
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = DEPLOYMENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["version"] = DEPLOYMENT_VERSION
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Deployment:
        return Deployment(**{k: v for k, v in data.items() if k in Deployment.__dataclass_fields__})

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            DeploymentStatus.LIVE.value,
            DeploymentStatus.FAILED.value,
            DeploymentStatus.CANCELLED.value,
            DeploymentStatus.TORN_DOWN.value,
        )


# ── Persistence ─────────────────────────────────────────────────────


def _deploys_dir() -> Path:
    d = settings.deploy_history_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deploy_path(deployment_id: str) -> Path:
    return _deploys_dir() / f"{deployment_id}.json"


def save_deployment(deploy: Deployment) -> Path:
    deploy.updated_at = time.time()
    path = _deploy_path(deploy.deployment_id)
    temp = path.with_suffix(".json.tmp")
    try:
        temp.write_text(json.dumps(deploy.to_dict(), indent=2, default=str), encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        logger.error("Failed to save deployment %s: %s", deploy.deployment_id, exc)
        temp.unlink(missing_ok=True)
        raise
    return path


def load_deployment(deployment_id: str) -> Deployment | None:
    path = _deploy_path(deployment_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Deployment.from_dict(data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def list_deployments(*, limit: int = 50) -> list[Deployment]:
    deploys: list[Deployment] = []
    for p in sorted(_deploys_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        if len(deploys) >= limit:
            break
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            deploys.append(Deployment.from_dict(data))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return deploys


def new_deployment_id() -> str:
    return uuid.uuid4().hex
