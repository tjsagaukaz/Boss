"""Governed deploy tools — EXTERNAL execution type, always require approval."""

from __future__ import annotations

from boss.config import settings
from boss.execution import (
    ExecutionType,
    display_value,
    governed_function_tool,
    scope_value,
)


@governed_function_tool(
    execution_type=ExecutionType.EXTERNAL,
    title="Preview Deploy",
    describe_call=lambda params: f'Deploy preview for {params.get("project_path", "project")}',
    scope_key=lambda params: scope_value("deploy", "preview"),
    scope_label=lambda params: display_value(
        params.get("project_path"), fallback="Create preview deployment"
    ),
)
def deploy_preview(project_path: str, adapter: str = "") -> str:
    """Create a preview deployment for a project.

    Requires explicit configuration (VERCEL_TOKEN or NETLIFY_AUTH_TOKEN)
    and will always prompt for user approval before deploying.

    Args:
        project_path: Root directory of the project to deploy.
        adapter: Explicit adapter name. Auto-detected if empty.
    """
    if not settings.deploy_enabled:
        return (
            "Deployment is not enabled. "
            "Set BOSS_DEPLOY_ENABLED=true and configure credentials "
            "(VERCEL_TOKEN or NETLIFY_AUTH_TOKEN) to enable."
        )

    from boss.deploy.engine import create_deployment, run_deployment

    try:
        deploy = create_deployment(
            project_path=project_path,
            session_id="tool",
            adapter_name=adapter or None,
        )
    except ValueError as exc:
        return f"Cannot deploy: {exc}"

    deploy = run_deployment(deploy.deployment_id)

    if deploy.status == "live":
        parts = [f"Preview deployed: {deploy.preview_url}"]
        parts.append(f"Adapter: {deploy.adapter}")
        parts.append(f"Deployment ID: {deploy.deployment_id}")
        return "\n".join(parts)
    elif deploy.status == "failed":
        return f"Deployment failed: {deploy.error or 'unknown error'}"
    else:
        return f"Deployment ended in state: {deploy.status}"


@governed_function_tool(
    execution_type=ExecutionType.EXTERNAL,
    title="Teardown Deploy",
    describe_call=lambda params: f'Teardown deployment {params.get("deployment_id", "")}',
    scope_key=lambda params: scope_value("deploy", "teardown"),
    scope_label=lambda params: f"Tear down deployment {params.get('deployment_id', '')[:12]}",
)
def teardown_deploy(deployment_id: str) -> str:
    """Tear down a previously created deployment.

    Args:
        deployment_id: The deployment ID returned by deploy_preview.
    """
    if not settings.deploy_enabled:
        return "Deployment is not enabled."

    from boss.deploy.engine import teardown_deployment

    try:
        deploy = teardown_deployment(deployment_id)
        return f"Deployment {deployment_id[:12]} torn down."
    except ValueError as exc:
        return f"Cannot teardown: {exc}"


@governed_function_tool(
    execution_type=ExecutionType.READ,
    title="Deploy Status",
    describe_call=lambda _params: "Check deploy status",
    scope_key=lambda _params: scope_value("deploy", "status"),
    scope_label=lambda _params: "Deployment status check",
)
def deploy_status_tool() -> str:
    """Check the status of the deployment subsystem and recent deploys."""
    from boss.deploy.engine import deploy_status
    from boss.deploy.state import list_deployments

    status = deploy_status()

    if not settings.deploy_enabled:
        return "Deployment subsystem is disabled. Set BOSS_DEPLOY_ENABLED=true to enable."

    parts = [f"Deploy enabled: {status['enabled']}"]
    parts.append(f"Configured adapters: {status['configured_count']}")
    parts.append(f"Live deploys: {status['live_count']}")

    recent = list_deployments(limit=5)
    if recent:
        parts.append("\nRecent deploys:")
        for d in recent:
            url_part = f" → {d.preview_url}" if d.preview_url else ""
            parts.append(f"  [{d.status}] {d.adapter} {d.project_path}{url_part}")

    return "\n".join(parts)
