"""Optional deployment subsystem for Boss."""

from boss.deploy.state import (
    Deployment,
    DeploymentStatus,
    DeploymentTarget,
)

# Import adapters so they auto-register.
import boss.deploy.static_adapter as _static_adapter  # noqa: F401

__all__ = [
    "Deployment",
    "DeploymentStatus",
    "DeploymentTarget",
]
