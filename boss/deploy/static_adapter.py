"""Static-site preview deploy adapter.

Supports projects with a ``build`` script in package.json or a static
``dist/`` / ``out/`` / ``build/`` directory.  The adapter runs the
build locally and then deploys the output directory to the configured
static hosting platform via its CLI.

Currently supports:
- Vercel (``npx vercel --yes``) — requires VERCEL_TOKEN
- Netlify (``npx netlify deploy --dir=<dir>``) — requires NETLIFY_AUTH_TOKEN
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from boss.deploy.adapters import DeployAdapter, register_adapter
from boss.deploy.engine import register_deploy_process, unregister_deploy_process
from boss.deploy.state import Deployment, DeploymentStatus, save_deployment
from boss.runner.engine import current_runner

logger = logging.getLogger(__name__)

# Candidate output directories for static builds.
_STATIC_OUTPUT_DIRS = ("dist", "out", "build", "public", ".next")


def _run_via_runner(
    command: list[str],
    *,
    cwd: str,
    timeout: int = 120,
    deployment_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a command through the Boss Runner if one is active.

    When a runner is available the command goes through
    ``RunnerEngine.start_managed_process()`` which enforces the full
    policy surface — command checks, write-target checks, cwd
    containment, and env scrubbing — rather than reimplementing those
    checks locally.

    Falls back to raw subprocess when no runner is available (e.g. in
    tests or standalone scripts).

    When *deployment_id* is provided the live subprocess is registered
    so that ``cancel_deployment()`` can terminate it immediately rather
    than waiting for the next phase boundary.
    """
    runner = current_runner()
    if runner is not None:
        proc, result = runner.start_managed_process(command, cwd=cwd)
        if proc is None:
            # Policy denied — convert to CompletedProcess.
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr=result.denied_reason or f"Command {result.verdict} by policy",
            )
    else:
        # No runner context — direct execution.
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    if deployment_id:
        register_deploy_process(deployment_id, proc)
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout)
        # start_managed_process omits text=True so output may be bytes.
        stdout = raw_out.decode("utf-8", errors="replace") if isinstance(raw_out, bytes) else raw_out
        stderr = raw_err.decode("utf-8", errors="replace") if isinstance(raw_err, bytes) else raw_err
        return subprocess.CompletedProcess(
            args=command,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        if deployment_id:
            unregister_deploy_process(deployment_id)


class StaticPreviewAdapter(DeployAdapter):
    """Deploy static/frontend projects as preview deploys.

    Requires one of:
    - VERCEL_TOKEN env var  → uses ``npx vercel``
    - NETLIFY_AUTH_TOKEN env var → uses ``npx netlify deploy``
    """

    name = "static_preview"

    def is_configured(self) -> bool:
        return bool(os.getenv("VERCEL_TOKEN") or os.getenv("NETLIFY_AUTH_TOKEN"))

    def detect_project(self, project_path: str | Path) -> bool:
        root = Path(project_path)
        # Has a package.json with a build script?
        pkg_json = root / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                if "build" in pkg.get("scripts", {}):
                    return True
            except (json.JSONDecodeError, OSError):
                pass
        # Has an existing output directory?
        for d in _STATIC_OUTPUT_DIRS:
            if (root / d).is_dir():
                return True
        return False

    def build(self, deployment: Deployment) -> Deployment:
        deployment.status = DeploymentStatus.BUILDING.value
        save_deployment(deployment)

        root = Path(deployment.project_path)
        pkg_json = root / "package.json"

        if not pkg_json.exists():
            deployment.build_log = "No package.json — skipping build step."
            save_deployment(deployment)
            return deployment

        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            deployment.build_log = "Could not read package.json."
            save_deployment(deployment)
            return deployment

        if "build" not in pkg.get("scripts", {}):
            deployment.build_log = "No build script in package.json — skipping."
            save_deployment(deployment)
            return deployment

        # Determine package manager.
        if (root / "yarn.lock").exists():
            install_cmd, build_cmd = ["yarn", "install"], ["yarn", "build"]
        elif (root / "pnpm-lock.yaml").exists():
            install_cmd, build_cmd = ["pnpm", "install"], ["pnpm", "run", "build"]
        else:
            install_cmd, build_cmd = ["npm", "ci"], ["npm", "run", "build"]

        log_parts: list[str] = []

        # Install dependencies.
        try:
            result = _run_via_runner(
                install_cmd,
                cwd=str(root),
                timeout=120,
                deployment_id=deployment.deployment_id,
            )
            log_parts.append(f"$ {' '.join(install_cmd)}\n{result.stdout[-2000:]}")
            if result.returncode != 0:
                deployment.status = DeploymentStatus.FAILED.value
                deployment.error = f"Install failed (exit {result.returncode})"
                deployment.build_log = "\n".join(log_parts) + f"\nSTDERR:\n{result.stderr[-2000:]}"
                save_deployment(deployment)
                return deployment
        except (OSError, subprocess.TimeoutExpired) as exc:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = f"Install error: {exc}"
            deployment.build_log = "\n".join(log_parts)
            save_deployment(deployment)
            return deployment

        # Run build.
        try:
            result = _run_via_runner(
                build_cmd,
                cwd=str(root),
                timeout=180,
                deployment_id=deployment.deployment_id,
            )
            log_parts.append(f"$ {' '.join(build_cmd)}\n{result.stdout[-2000:]}")
            if result.returncode != 0:
                deployment.status = DeploymentStatus.FAILED.value
                deployment.error = f"Build failed (exit {result.returncode})"
                deployment.build_log = "\n".join(log_parts) + f"\nSTDERR:\n{result.stderr[-2000:]}"
                save_deployment(deployment)
                return deployment
        except (OSError, subprocess.TimeoutExpired) as exc:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = f"Build error: {exc}"
            deployment.build_log = "\n".join(log_parts)
            save_deployment(deployment)
            return deployment

        deployment.build_log = "\n".join(log_parts)
        save_deployment(deployment)
        return deployment

    def deploy(self, deployment: Deployment) -> Deployment:
        deployment.status = DeploymentStatus.DEPLOYING.value
        save_deployment(deployment)

        root = Path(deployment.project_path)

        # Find output directory.
        output_dir: Path | None = None
        for d in _STATIC_OUTPUT_DIRS:
            candidate = root / d
            if candidate.is_dir():
                output_dir = candidate
                break

        if output_dir is None:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = "No build output directory found."
            save_deployment(deployment)
            return deployment

        # Pick platform.
        if os.getenv("VERCEL_TOKEN"):
            return self._deploy_vercel(deployment, root, output_dir)
        elif os.getenv("NETLIFY_AUTH_TOKEN"):
            return self._deploy_netlify(deployment, root, output_dir)
        else:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = "No deployment credentials configured."
            save_deployment(deployment)
            return deployment

    def _deploy_vercel(self, deployment: Deployment, root: Path, output_dir: Path) -> Deployment:
        try:
            result = _run_via_runner(
                [
                    "npx", "vercel", "deploy",
                    str(output_dir),
                    "--yes",
                    "--token", os.environ["VERCEL_TOKEN"],
                ],
                cwd=str(root),
                timeout=120,
                deployment_id=deployment.deployment_id,
            )
            deployment.deploy_log = result.stdout[-4000:]
            if result.returncode == 0:
                # Vercel prints the preview URL as the last line of stdout.
                url = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else None
                if url and url.startswith("http"):
                    deployment.preview_url = url
                deployment.status = DeploymentStatus.LIVE.value
                deployment.finished_at = time.time()
            else:
                deployment.status = DeploymentStatus.FAILED.value
                deployment.error = f"Vercel deploy failed (exit {result.returncode})"
                deployment.deploy_log += f"\nSTDERR:\n{result.stderr[-2000:]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = f"Vercel deploy error: {exc}"

        save_deployment(deployment)
        return deployment

    def _deploy_netlify(self, deployment: Deployment, root: Path, output_dir: Path) -> Deployment:
        token = os.environ["NETLIFY_AUTH_TOKEN"]
        try:
            result = _run_via_runner(
                [
                    "npx", "netlify", "deploy",
                    "--dir", str(output_dir),
                    "--auth", token,
                    "--json",
                ],
                cwd=str(root),
                timeout=120,
                deployment_id=deployment.deployment_id,
            )
            deployment.deploy_log = result.stdout[-4000:]
            if result.returncode == 0:
                # Netlify --json outputs a JSON with deploy_url.
                try:
                    deploy_info = json.loads(result.stdout)
                    deployment.preview_url = deploy_info.get("deploy_url") or deploy_info.get("url")
                except json.JSONDecodeError:
                    pass
                deployment.status = DeploymentStatus.LIVE.value
                deployment.finished_at = time.time()
            else:
                deployment.status = DeploymentStatus.FAILED.value
                deployment.error = f"Netlify deploy failed (exit {result.returncode})"
                deployment.deploy_log += f"\nSTDERR:\n{result.stderr[-2000:]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error = f"Netlify deploy error: {exc}"

        save_deployment(deployment)
        return deployment


# Auto-register when the module is imported.
register_adapter(StaticPreviewAdapter())
