"""Deployment adapter interface and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from boss.deploy.state import Deployment, DeploymentStatus, save_deployment

logger = logging.getLogger(__name__)


class DeployAdapter(ABC):
    """Base class for deployment adapters.

    Subclasses implement the actual deploy/teardown for a specific
    hosting platform.  All adapters are opt-in — they must be
    explicitly configured and the user must approve each deploy.
    """

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True when this adapter has valid configuration/credentials."""
        ...

    @abstractmethod
    def detect_project(self, project_path: str | Path) -> bool:
        """Return True if this adapter can handle the given project."""
        ...

    @abstractmethod
    def build(self, deployment: Deployment) -> Deployment:
        """Run the build step. Updates deployment in-place and persists."""
        ...

    @abstractmethod
    def deploy(self, deployment: Deployment) -> Deployment:
        """Push the build output to the hosting platform.

        Must set ``deployment.preview_url`` on success.
        """
        ...

    def teardown(self, deployment: Deployment) -> Deployment:
        """Tear down a live deployment.  Optional — not all adapters support it."""
        deployment.status = DeploymentStatus.TORN_DOWN.value
        save_deployment(deployment)
        return deployment

    def status_payload(self) -> dict[str, Any]:
        """Return adapter health/config info for the diagnostics surface."""
        return {
            "adapter": self.name,
            "configured": self.is_configured(),
        }


# ── Adapter registry ────────────────────────────────────────────────

_ADAPTERS: dict[str, DeployAdapter] = {}


def register_adapter(adapter: DeployAdapter) -> None:
    _ADAPTERS[adapter.name] = adapter


def get_adapter(name: str) -> DeployAdapter | None:
    return _ADAPTERS.get(name)


def available_adapters() -> list[DeployAdapter]:
    """Return all registered adapters that are currently configured."""
    return [a for a in _ADAPTERS.values() if a.is_configured()]


def all_adapters() -> list[DeployAdapter]:
    """Return every registered adapter regardless of configuration."""
    return list(_ADAPTERS.values())


def best_adapter_for(project_path: str | Path) -> DeployAdapter | None:
    """Return the first configured adapter that can handle this project."""
    for adapter in available_adapters():
        if adapter.detect_project(project_path):
            return adapter
    return None
