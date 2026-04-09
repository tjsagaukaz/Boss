"""Worker role definitions and capabilities."""

from __future__ import annotations

from enum import StrEnum


class WorkerRole(StrEnum):
    """Distinct worker roles with different isolation requirements."""

    EXPLORER = "explorer"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


# Which runner permission profile each role maps to.
ROLE_PERMISSION_PROFILES: dict[WorkerRole, str] = {
    WorkerRole.EXPLORER: "read_only",
    WorkerRole.IMPLEMENTER: "workspace_write",
    WorkerRole.REVIEWER: "read_only",
}

# Whether the role needs an isolated workspace copy.
ROLE_NEEDS_ISOLATION: dict[WorkerRole, bool] = {
    WorkerRole.EXPLORER: False,
    WorkerRole.IMPLEMENTER: True,
    WorkerRole.REVIEWER: False,
}

# Default mode string for the agents SDK runner.
ROLE_AGENT_MODE: dict[WorkerRole, str] = {
    WorkerRole.EXPLORER: "ask",
    WorkerRole.IMPLEMENTER: "agent",
    WorkerRole.REVIEWER: "review",
}
