"""Layered prompt builder.

Composes durable agent instructions from independent layers, keeps
track of which layers were used, and exposes lightweight diagnostics.

Usage::

    result = (
        PromptBuilder(mode="agent", agent_name="general")
        .with_workspace("/Users/tj/boss")
        .with_tool_names({"recall", "remember", "find_symbol"})
        .build()
    )
    instructions = result.text
    diag = result.diagnostics()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from boss.prompting.core_instructions import (
    CORE_OPERATING,
    FRONTEND_GUIDANCE,
    PREVIEW_GUIDANCE,
    REVIEW_DISCIPLINE,
)
from boss.prompting.layers import PromptLayer, PromptLayerKind
from boss.prompting.modes import (
    MODE_INSTRUCTIONS,
    ROLE_INSTRUCTIONS,
    general_tool_hints,
    role_instructions,
    specialist_handoff_hints,
)


# Keywords in the user message or task that signal frontend work.
_FRONTEND_SIGNALS = re.compile(
    r"\b(swiftui|swift\s+build|uikit|xcodeproj|\.swift\b|frontend|"
    r"bossapp|chatview|contentview|nswindow|appkit|view\s+model)\b",
    re.IGNORECASE,
)


@dataclass
class PromptResult:
    """Output of the prompt builder."""

    text: str
    layers: list[PromptLayer]

    def active_layers(self) -> list[PromptLayer]:
        return [l for l in self.layers if l.active]

    def diagnostics(self) -> dict:
        """Lightweight diagnostics dict suitable for logging or SSE."""
        return {
            "total_layers": len(self.layers),
            "active_layers": len(self.active_layers()),
            "total_chars": len(self.text),
            "layers": [l.to_dict() for l in self.layers],
        }

    def safe_summary(self) -> dict:
        """Developer-oriented summary without raw instruction content.

        Suitable for rendering in the app diagnostics surface.  Does not
        expose memory, user data, or full prompt text.
        """
        active = self.active_layers()
        active_kinds = sorted({l.kind.value for l in active})
        source_files = [l.source for l in active if "/" in l.source or l.source.endswith(".md")]
        review_active = any(l.kind == PromptLayerKind.REVIEW and l.active for l in self.layers)
        frontend_active = any(l.kind == PromptLayerKind.FRONTEND and l.active for l in self.layers)
        return {
            "active_layer_count": len(active),
            "total_layer_count": len(self.layers),
            "active_kinds": active_kinds,
            "instruction_sources": source_files,
            "review_guidance_active": review_active,
            "frontend_guidance_active": frontend_active,
            "total_chars": len(self.text),
        }


class PromptBuilder:
    """Assemble durable agent instructions from composable layers.

    Does **not** handle transient context (session history, memory
    injection).  Those remain the responsibility of SessionContextManager.
    """

    def __init__(self, *, mode: str = "agent", agent_name: str = "general"):
        self._mode = mode
        self._agent_name = agent_name
        self._workspace_root: Path | None = None
        self._tool_names: set[str] = set()
        self._task_hint: str | None = None

    # ── Fluent setters ──────────────────────────────────────────────

    def with_workspace(self, root: str | Path | None) -> "PromptBuilder":
        if root is not None:
            self._workspace_root = Path(root)
        return self

    def with_tool_names(self, names: set[str]) -> "PromptBuilder":
        self._tool_names = names
        return self

    def with_task_hint(self, hint: str | None) -> "PromptBuilder":
        """Optional snippet (e.g. user message) used to detect whether
        frontend guidance should be activated."""
        self._task_hint = hint
        return self

    # ── Build ───────────────────────────────────────────────────────

    def build(self) -> PromptResult:
        layers: list[PromptLayer] = []

        # 1. Core operating instructions  (always active)
        layers.append(PromptLayer(
            kind=PromptLayerKind.CORE,
            source="core_instructions.CORE_OPERATING",
            content=CORE_OPERATING,
        ))

        # 2. Mode constraints
        mode_text = MODE_INSTRUCTIONS.get(self._mode, MODE_INSTRUCTIONS["agent"])
        layers.append(PromptLayer(
            kind=PromptLayerKind.MODE,
            source=f"mode:{self._mode}",
            content=mode_text,
        ))

        # 3. Role identity
        role_text = role_instructions(self._agent_name, self._mode)
        if self._agent_name in ("boss", "general"):
            # Append tool hints and handoff hints for the primary agent
            extra_parts = [role_text]
            if self._tool_names:
                extra_parts.append(general_tool_hints(self._tool_names))
            extra_parts.append(specialist_handoff_hints())
            role_text = "\n\n".join(p for p in extra_parts if p)
        if role_text:
            layers.append(PromptLayer(
                kind=PromptLayerKind.ROLE,
                source=f"role:{self._agent_name}",
                content=role_text,
            ))

        # 4–7. Project layers from Boss control files
        layers.extend(self._control_layers())

        # 8. Frontend guidance (active only when task signals UI work)
        frontend_active = self._detect_frontend()
        layers.append(PromptLayer(
            kind=PromptLayerKind.FRONTEND,
            source="core_instructions.FRONTEND_GUIDANCE",
            content=FRONTEND_GUIDANCE,
            active=frontend_active,
        ))

        # 9. Preview verification guidance (active alongside frontend)
        layers.append(PromptLayer(
            kind=PromptLayerKind.FRONTEND,
            source="core_instructions.PREVIEW_GUIDANCE",
            content=PREVIEW_GUIDANCE,
            active=frontend_active,
        ))

        # Assemble final text from active layers
        text = "\n\n".join(l.content for l in layers if l.active and l.content.strip())
        return PromptResult(text=text, layers=layers)

    # ── Internal helpers ────────────────────────────────────────────

    def _control_layers(self) -> list[PromptLayer]:
        """Load project instructions, environment, rules, and review
        guidance from Boss control files."""
        from boss.control import applicable_rules, load_boss_control

        control = load_boss_control(self._workspace_root)
        layers: list[PromptLayer] = []

        # Project instructions (BOSS.md)
        if control.boss_md.strip():
            layers.append(PromptLayer(
                kind=PromptLayerKind.PROJECT,
                source=str(control.boss_md_path),
                content="Project instructions (BOSS.md):\n" + control.boss_md.strip(),
            ))

        # Environment summary
        env_text = _render_environment_summary(control.environment)
        if env_text:
            layers.append(PromptLayer(
                kind=PromptLayerKind.ENVIRONMENT,
                source=str(control.environment_path),
                content="Local environment and validation:\n" + env_text,
            ))

        # Applicable rules
        rules = applicable_rules(
            agent_name=self._agent_name,
            mode=self._mode,
            workspace_root=control.root,
        )
        for rule in rules:
            if rule.body.strip():
                layers.append(PromptLayer(
                    kind=PromptLayerKind.RULE,
                    source=str(rule.path),
                    content=f"{rule.title}:\n{rule.body.strip()}",
                ))

        # Review guidance (only in review mode)
        review_active = self._mode == control.config.review_mode_name()
        if control.review.strip():
            layers.append(PromptLayer(
                kind=PromptLayerKind.REVIEW,
                source=str(control.review_path),
                content="Review behavior:\n" + control.review.strip(),
                active=review_active,
            ))

        # Core review discipline (always present, active only in review)
        layers.append(PromptLayer(
            kind=PromptLayerKind.REVIEW,
            source="core_instructions.REVIEW_DISCIPLINE",
            content=REVIEW_DISCIPLINE,
            active=review_active,
        ))

        return layers

    def _detect_frontend(self) -> bool:
        """Return True if the task looks like frontend/UI work."""
        if self._task_hint and _FRONTEND_SIGNALS.search(self._task_hint):
            return True
        if self._agent_name == "mac":
            return True
        return False


def _render_environment_summary(env: dict) -> str:
    """Render .boss/environment.json into a prompt-friendly summary."""
    if not env:
        return ""
    parts: list[str] = []
    if env.get("name"):
        parts.append(f"Environment: {env['name']}")
    if env.get("platform"):
        parts.append(f"Platform: {env['platform']}")

    constraints = env.get("constraints")
    if isinstance(constraints, list) and constraints:
        parts.append("Constraints:")
        for c in constraints:
            parts.append(f"  - {c}")

    validation = env.get("validation")
    if isinstance(validation, dict):
        for category, commands in validation.items():
            if isinstance(commands, list):
                parts.append(f"Validation ({category}):")
                for cmd in commands:
                    parts.append(f"  $ {cmd}")
    return "\n".join(parts)
