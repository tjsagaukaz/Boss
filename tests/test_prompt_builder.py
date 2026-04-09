"""Prompt-builder unit tests, diagnostics, and regression coverage.

Tests the layered prompt composition system:
- Mode-specific layering
- Missing-file fallbacks
- Findings-first review instructions
- Frontend guidance activation
- Suppression of planning chatter
- Separation of durable instructions from transient context
- Regression guards for mode-specific behaviour
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boss.prompting.builder import PromptBuilder, PromptResult
from boss.prompting.core_instructions import (
    CORE_OPERATING,
    FRONTEND_GUIDANCE,
    REVIEW_DISCIPLINE,
)
from boss.prompting.layers import PromptLayer, PromptLayerKind
from boss.prompting.modes import MODE_INSTRUCTIONS, role_instructions


def _temp_workspace(
    *,
    boss_md: str = "",
    config_toml: str = "",
    review_md: str = "",
    environment_json: str = "",
    rules: dict[str, str] | None = None,
) -> tuple[tempfile.TemporaryDirectory, Path]:
    """Create a temporary Boss workspace with optional control files."""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)

    if boss_md:
        (root / "BOSS.md").write_text(boss_md, encoding="utf-8")
    boss_dir = root / ".boss"
    boss_dir.mkdir()
    rules_dir = boss_dir / "rules"
    rules_dir.mkdir()

    if config_toml:
        (boss_dir / "config.toml").write_text(config_toml, encoding="utf-8")
    if review_md:
        (boss_dir / "review.md").write_text(review_md, encoding="utf-8")
    if environment_json:
        (boss_dir / "environment.json").write_text(environment_json, encoding="utf-8")
    if rules:
        for name, content in rules.items():
            (rules_dir / name).write_text(content, encoding="utf-8")

    return td, root


class TestPromptLayerStructure(unittest.TestCase):
    """PromptLayer and PromptLayerKind basics."""

    def test_layer_to_dict_includes_all_fields(self):
        layer = PromptLayer(
            kind=PromptLayerKind.CORE,
            source="test_source",
            content="hello world",
            active=True,
        )
        d = layer.to_dict()
        self.assertEqual(d["kind"], "core")
        self.assertEqual(d["source"], "test_source")
        self.assertTrue(d["active"])
        self.assertEqual(d["content_length"], 11)

    def test_inactive_layer_included_in_diagnostics(self):
        layer = PromptLayer(
            kind=PromptLayerKind.REVIEW,
            source="review.md",
            content="review text",
            active=False,
        )
        self.assertFalse(layer.active)
        d = layer.to_dict()
        self.assertFalse(d["active"])
        self.assertEqual(d["content_length"], 11)


class TestPromptBuilderLayering(unittest.TestCase):
    """Mode-specific layering and the correct layer order."""

    def test_agent_mode_produces_core_mode_role_layers(self):
        result = PromptBuilder(mode="agent", agent_name="general").build()
        kinds = [l.kind for l in result.active_layers()]
        self.assertIn(PromptLayerKind.CORE, kinds)
        self.assertIn(PromptLayerKind.MODE, kinds)
        self.assertIn(PromptLayerKind.ROLE, kinds)

    def test_core_is_always_first_active_layer(self):
        for mode in ("agent", "ask", "plan", "review"):
            result = PromptBuilder(mode=mode, agent_name="general").build()
            first = result.active_layers()[0]
            self.assertEqual(first.kind, PromptLayerKind.CORE, f"mode={mode}")

    def test_mode_layer_contains_mode_name(self):
        for mode in ("agent", "ask", "plan", "review"):
            result = PromptBuilder(mode=mode, agent_name="general").build()
            mode_layers = [l for l in result.active_layers() if l.kind == PromptLayerKind.MODE]
            self.assertEqual(len(mode_layers), 1)
            self.assertIn(f"mode:{mode}", mode_layers[0].source)

    def test_review_mode_activates_review_layers(self):
        td, root = _temp_workspace(review_md="Findings first always.")
        try:
            result = (
                PromptBuilder(mode="review", agent_name="general")
                .with_workspace(root)
                .build()
            )
            review_layers = [l for l in result.layers if l.kind == PromptLayerKind.REVIEW]
            self.assertTrue(len(review_layers) >= 1)
            active_review = [l for l in review_layers if l.active]
            self.assertTrue(len(active_review) >= 1, "Review layers should be active in review mode")
            self.assertIn("findings", result.text.lower())
        finally:
            td.cleanup()

    def test_non_review_mode_deactivates_review_layers(self):
        td, root = _temp_workspace(review_md="Findings first always.")
        try:
            for mode in ("agent", "ask", "plan"):
                result = (
                    PromptBuilder(mode=mode, agent_name="general")
                    .with_workspace(root)
                    .build()
                )
                review_layers = [l for l in result.layers if l.kind == PromptLayerKind.REVIEW]
                for layer in review_layers:
                    self.assertFalse(layer.active, f"Review layer should be inactive in {mode}")
                # review content should not appear in the assembled text
                self.assertNotIn("Findings first always.", result.text)
        finally:
            td.cleanup()

    def test_review_discipline_from_core_instructions_active_in_review(self):
        result = PromptBuilder(mode="review", agent_name="code").build()
        self.assertIn("severity", result.text.lower())
        self.assertIn("findings", result.text.lower())

    def test_diagnostics_structure(self):
        result = PromptBuilder(mode="agent", agent_name="general").build()
        diag = result.diagnostics()
        self.assertIn("total_layers", diag)
        self.assertIn("active_layers", diag)
        self.assertIn("total_chars", diag)
        self.assertIn("layers", diag)
        self.assertIsInstance(diag["layers"], list)
        self.assertGreater(diag["total_layers"], 0)
        self.assertGreater(diag["active_layers"], 0)


class TestMissingFileFallbacks(unittest.TestCase):
    """Builder handles missing control files gracefully."""

    def test_empty_workspace_still_produces_valid_instructions(self):
        td, root = _temp_workspace()
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            self.assertIn(CORE_OPERATING, result.text)
            self.assertGreater(len(result.text), 100)
        finally:
            td.cleanup()

    def test_missing_boss_md_omits_project_layer(self):
        td, root = _temp_workspace()  # no BOSS.md
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            project_layers = [l for l in result.layers if l.kind == PromptLayerKind.PROJECT]
            self.assertEqual(len(project_layers), 0)
        finally:
            td.cleanup()

    def test_boss_md_present_adds_project_layer(self):
        td, root = _temp_workspace(boss_md="This is Boss project instructions.")
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            project_layers = [l for l in result.active_layers() if l.kind == PromptLayerKind.PROJECT]
            self.assertEqual(len(project_layers), 1)
            self.assertIn("This is Boss project instructions.", result.text)
        finally:
            td.cleanup()

    def test_missing_environment_json(self):
        td, root = _temp_workspace()
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            env_layers = [l for l in result.layers if l.kind == PromptLayerKind.ENVIRONMENT]
            self.assertEqual(len(env_layers), 0)
        finally:
            td.cleanup()

    def test_no_workspace_still_builds(self):
        # No workspace at all — builder should not crash
        result = PromptBuilder(mode="agent", agent_name="general").build()
        self.assertIn(CORE_OPERATING, result.text)


class TestFrontendGuidance(unittest.TestCase):
    """Frontend guidance layer activates only when relevant."""

    def test_frontend_active_with_swiftui_hint(self):
        result = (
            PromptBuilder(mode="agent", agent_name="general")
            .with_task_hint("Fix the SwiftUI ChatView layout")
            .build()
        )
        frontend_layers = [l for l in result.layers if l.kind == PromptLayerKind.FRONTEND]
        self.assertTrue(any(l.active for l in frontend_layers))
        self.assertIn(FRONTEND_GUIDANCE, result.text)

    def test_frontend_active_with_bossapp_hint(self):
        result = (
            PromptBuilder(mode="agent", agent_name="general")
            .with_task_hint("Update the BossApp diagnostics view")
            .build()
        )
        frontend_layers = [l for l in result.layers if l.kind == PromptLayerKind.FRONTEND]
        self.assertTrue(any(l.active for l in frontend_layers))

    def test_frontend_inactive_for_backend_hint(self):
        result = (
            PromptBuilder(mode="agent", agent_name="general")
            .with_task_hint("Refactor the Python API endpoint")
            .build()
        )
        frontend_layers = [l for l in result.layers if l.kind == PromptLayerKind.FRONTEND]
        for l in frontend_layers:
            self.assertFalse(l.active)
        self.assertNotIn(FRONTEND_GUIDANCE, result.text)

    def test_frontend_inactive_with_no_hint(self):
        result = (
            PromptBuilder(mode="agent", agent_name="general")
            .build()
        )
        frontend_layers = [l for l in result.layers if l.kind == PromptLayerKind.FRONTEND]
        for l in frontend_layers:
            self.assertFalse(l.active)

    def test_mac_agent_always_activates_frontend(self):
        result = (
            PromptBuilder(mode="agent", agent_name="mac")
            .build()
        )
        frontend_layers = [l for l in result.layers if l.kind == PromptLayerKind.FRONTEND]
        self.assertTrue(any(l.active for l in frontend_layers))


class TestOutputDiscipline(unittest.TestCase):
    """Core instructions suppress planning chatter and narration."""

    def test_no_narration_instruction(self):
        self.assertIn("Do not narrate your own process", CORE_OPERATING)

    def test_no_planning_chatter(self):
        self.assertIn("Do not emit planning chatter", CORE_OPERATING)

    def test_no_progress_updates(self):
        self.assertIn("do not emit progress updates", CORE_OPERATING)

    def test_concise_directive(self):
        self.assertIn("Be concise", CORE_OPERATING)

    def test_plan_mode_does_not_suppress_planning(self):
        """Plan mode explicitly asks for structured output, which is not 'chatter'."""
        plan_text = MODE_INSTRUCTIONS["plan"]
        self.assertIn("structured plan", plan_text)
        self.assertIn("Goal, Execution Plan, Risks, Validation", plan_text)


class TestTerminationDiscipline(unittest.TestCase):
    """Termination and permission awareness prevent runaway loops."""

    def test_termination_boundary_present(self):
        self.assertIn("Stop after two or three distinct approaches", CORE_OPERATING)

    def test_blocked_report_instruction(self):
        self.assertIn("report the blocker", CORE_OPERATING)

    def test_partial_success_preference(self):
        self.assertIn("partial but correct result", CORE_OPERATING)

    def test_permission_awareness_present(self):
        self.assertIn("Permission awareness", CORE_OPERATING)

    def test_denied_no_retry(self):
        self.assertIn("do not retry the same action", CORE_OPERATING)

    def test_pivot_to_safer_alternative(self):
        self.assertIn("pivot to a safer alternative", CORE_OPERATING)


class TestCodeChangeDiscipline(unittest.TestCase):
    """Code change minimality constraints."""

    def test_smallest_possible_change(self):
        self.assertIn("smallest possible change", CORE_OPERATING)

    def test_no_unrelated_refactoring(self):
        self.assertIn("Do not refactor unrelated code", CORE_OPERATING)

    def test_regression_risk_awareness(self):
        self.assertIn("regression risk", CORE_OPERATING)


class TestResultFormatting(unittest.TestCase):
    """Result formatting contract."""

    def test_structured_output_preference(self):
        self.assertIn("structured results over narrative", CORE_OPERATING)

    def test_usable_format(self):
        self.assertIn("directly usable by the user", CORE_OPERATING)


class TestFrontendGuidanceContent(unittest.TestCase):
    """Frontend guidance strength."""

    def test_design_language_constraint(self):
        self.assertIn("design language", FRONTEND_GUIDANCE)

    def test_typography_consistency(self):
        self.assertIn("typography, spacing, and alignment", FRONTEND_GUIDANCE)

    def test_functional_purpose_constraint(self):
        self.assertIn("clear functional purpose", FRONTEND_GUIDANCE)


class TestDurableVsTransientSeparation(unittest.TestCase):
    """Durable prompt instructions must not contain transient context."""

    def test_prompt_text_has_no_memory_injection(self):
        td, root = _temp_workspace(boss_md="Project instructions here.")
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            # Memory injection text patterns should never appear in durable instructions
            self.assertNotIn("BOSS_CONTEXT:", result.text)
            self.assertNotIn("recalled memory", result.text.lower())
            self.assertNotIn("session history", result.text.lower())
        finally:
            td.cleanup()

    def test_prompt_builder_does_not_call_memory_system(self):
        """PromptBuilder should never import or call memory injection."""
        import inspect
        from boss.prompting import builder as builder_module

        source = inspect.getsource(builder_module)
        self.assertNotIn("build_memory_injection", source)
        self.assertNotIn("from boss.memory", source)
        self.assertNotIn("from boss.context", source)

    def test_prompt_builder_does_not_include_session_state(self):
        """PromptBuilder has no session/conversation concept."""
        import inspect
        from boss.prompting import builder as builder_module

        source = inspect.getsource(builder_module)
        self.assertNotIn("session_id", source)
        self.assertNotIn("SessionState", source)
        self.assertNotIn("conversation", source)


class TestRuleFiltering(unittest.TestCase):
    """Rules are correctly filtered by mode and agent target."""

    def test_review_only_rule_excluded_from_agent_mode(self):
        td, root = _temp_workspace(
            rules={
                "30-review.md": (
                    "+++\ntitle = \"Review Only\"\ntargets = [\"general\"]\n"
                    "modes = [\"review\"]\n+++\n\nReview-only content."
                ),
            },
        )
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            self.assertNotIn("Review-only content.", result.text)
        finally:
            td.cleanup()

    def test_review_only_rule_included_in_review_mode(self):
        td, root = _temp_workspace(
            rules={
                "30-review.md": (
                    "+++\ntitle = \"Review Only\"\ntargets = [\"general\"]\n"
                    "modes = [\"review\"]\n+++\n\nReview-only content."
                ),
            },
        )
        try:
            result = (
                PromptBuilder(mode="review", agent_name="general")
                .with_workspace(root)
                .build()
            )
            self.assertIn("Review-only content.", result.text)
        finally:
            td.cleanup()

    def test_always_rule_included_in_all_modes(self):
        td, root = _temp_workspace(
            rules={
                "00-core.md": (
                    "+++\ntitle = \"Core Always\"\ntargets = [\"all\"]\n"
                    "always = true\n+++\n\nAlways-on core rule."
                ),
            },
        )
        try:
            for mode in ("agent", "ask", "plan", "review"):
                result = (
                    PromptBuilder(mode=mode, agent_name="general")
                    .with_workspace(root)
                    .build()
                )
                self.assertIn("Always-on core rule.", result.text, f"Failed for mode={mode}")
        finally:
            td.cleanup()

    def test_target_filtering_excludes_non_matching_agent(self):
        td, root = _temp_workspace(
            rules={
                "20-mac.md": (
                    "+++\ntitle = \"Mac Only\"\ntargets = [\"mac\"]\n"
                    "modes = [\"agent\"]\n+++\n\nMac-specific guidance."
                ),
            },
        )
        try:
            # general agent should not see mac-only rules
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            self.assertNotIn("Mac-specific guidance.", result.text)
        finally:
            td.cleanup()


class TestEnvironmentLayer(unittest.TestCase):
    """Environment JSON is rendered into a prompt layer."""

    def test_environment_layer_renders(self):
        td, root = _temp_workspace(
            environment_json=json.dumps({
                "name": "dev",
                "platform": "macOS",
                "constraints": ["local only"],
                "validation": {"backend": ["python3 -m compileall boss"]},
            }),
        )
        try:
            result = (
                PromptBuilder(mode="agent", agent_name="general")
                .with_workspace(root)
                .build()
            )
            env_layers = [l for l in result.active_layers() if l.kind == PromptLayerKind.ENVIRONMENT]
            self.assertEqual(len(env_layers), 1)
            self.assertIn("macOS", result.text)
            self.assertIn("local only", result.text)
            self.assertIn("python3 -m compileall boss", result.text)
        finally:
            td.cleanup()


# ── Regression tests ────────────────────────────────────────────────

class TestPromptRegressions(unittest.TestCase):
    """Regression guards that catch prompt-composition breakages."""

    def test_review_mode_findings_first_behaviour(self):
        """Review mode must always include findings-first instructions."""
        result = PromptBuilder(mode="review", agent_name="code").build()
        text_lower = result.text.lower()
        self.assertIn("findings", text_lower)
        self.assertIn("severity", text_lower)
        # Must not include autonomous edit instructions
        self.assertNotIn("full access", result.text)
        self.assertIn("Do not fix code", result.text)

    def test_ask_mode_no_edit_instructions(self):
        """ask mode must not inherit autonomous edit tool permissions."""
        result = PromptBuilder(mode="ask", agent_name="general").build()
        self.assertIn("read-only", result.text)
        self.assertNotIn("full access", result.text)
        self.assertIn("Do not perform edits", result.text)

    def test_plan_mode_no_edit_instructions(self):
        """plan mode must not inherit autonomous edit tool permissions."""
        result = PromptBuilder(mode="plan", agent_name="general").build()
        self.assertIn("read-only", result.text)
        self.assertNotIn("full access", result.text)
        self.assertIn("Do not execute", result.text)

    def test_agent_mode_has_full_access(self):
        """agent mode should have full governed tool access."""
        result = PromptBuilder(mode="agent", agent_name="general").build()
        self.assertIn("full", result.text.lower())
        self.assertIn("Mode: agent", result.text)

    def test_boss_reviewer_role_in_review_mode(self):
        """boss agent in review mode should identify as reviewer, not programmer."""
        result = PromptBuilder(mode="review", agent_name="boss").build()
        self.assertIn("code review", result.text.lower())
        self.assertNotIn("expert programmer", result.text.lower())

    def test_boss_role_in_agent_mode(self):
        """boss agent in agent mode should have full governed tool access."""
        result = PromptBuilder(mode="agent", agent_name="boss").build()
        self.assertIn("governed tools", result.text.lower())

    def test_memory_injection_never_in_durable_prompt(self):
        """Memory injection must stay in transient context, not durable instructions."""
        for mode in ("agent", "ask", "plan", "review"):
            result = PromptBuilder(mode=mode, agent_name="general").build()
            self.assertNotIn("BOSS_CONTEXT:", result.text)

    def test_review_layers_count(self):
        """Review mode should have at least the core review discipline layer active."""
        result = PromptBuilder(mode="review", agent_name="general").build()
        active_review = [l for l in result.active_layers() if l.kind == PromptLayerKind.REVIEW]
        self.assertGreaterEqual(len(active_review), 1)

    def test_all_layer_kinds_have_source(self):
        """Every layer must have a non-empty source for diagnostics."""
        td, root = _temp_workspace(
            boss_md="Test",
            review_md="Test review",
            environment_json='{"platform": "test"}',
        )
        try:
            result = (
                PromptBuilder(mode="review", agent_name="general")
                .with_workspace(root)
                .with_task_hint("Fix the SwiftUI view")
                .build()
            )
            for layer in result.layers:
                self.assertTrue(layer.source, f"Layer {layer.kind} has empty source")
        finally:
            td.cleanup()


class TestPromptDiagnosticsOutput(unittest.TestCase):
    """Diagnostics output is safe, complete, and developer-oriented."""

    def test_diagnostics_does_not_leak_full_content(self):
        """diagnostics() should have content_length, not full content text."""
        result = PromptBuilder(mode="agent", agent_name="general").build()
        diag = result.diagnostics()
        for layer_info in diag["layers"]:
            self.assertNotIn("content", layer_info)
            self.assertIn("content_length", layer_info)

    def test_diagnostics_layer_count_matches(self):
        result = PromptBuilder(mode="agent", agent_name="general").build()
        diag = result.diagnostics()
        self.assertEqual(diag["total_layers"], len(result.layers))
        self.assertEqual(diag["active_layers"], len(result.active_layers()))

    def test_diagnostics_char_count_matches_text(self):
        result = PromptBuilder(mode="agent", agent_name="general").build()
        diag = result.diagnostics()
        self.assertEqual(diag["total_chars"], len(result.text))

    def test_prompt_preview_truncation(self):
        """The API endpoint truncates the preview to 2000 chars.
        Here we test that the diagnostics structure supports it."""
        result = PromptBuilder(mode="agent", agent_name="general").build()
        preview = result.text[:2000]
        # Preview should be a prefix of the full text
        self.assertTrue(result.text.startswith(preview))
        # Full text is likely longer than 2000
        self.assertGreater(len(result.text), 100)


class TestSafeSummary(unittest.TestCase):
    """safe_summary() output for the diagnostics UI."""

    def test_safe_summary_fields(self):
        result = PromptBuilder(mode="agent", agent_name="general").build()
        summary = result.safe_summary()
        self.assertIn("active_layer_count", summary)
        self.assertIn("total_layer_count", summary)
        self.assertIn("active_kinds", summary)
        self.assertIn("instruction_sources", summary)
        self.assertIn("review_guidance_active", summary)
        self.assertIn("frontend_guidance_active", summary)
        self.assertIn("total_chars", summary)

    def test_safe_summary_review_flag(self):
        result = PromptBuilder(mode="review", agent_name="general").build()
        summary = result.safe_summary()
        self.assertTrue(summary["review_guidance_active"])

        result2 = PromptBuilder(mode="agent", agent_name="general").build()
        summary2 = result2.safe_summary()
        self.assertFalse(summary2["review_guidance_active"])

    def test_safe_summary_frontend_flag(self):
        result = (
            PromptBuilder(mode="agent", agent_name="general")
            .with_task_hint("Fix the SwiftUI ChatView")
            .build()
        )
        self.assertTrue(result.safe_summary()["frontend_guidance_active"])

        result2 = PromptBuilder(mode="agent", agent_name="general").build()
        self.assertFalse(result2.safe_summary()["frontend_guidance_active"])

    def test_safe_summary_no_raw_content(self):
        """safe_summary must not include raw prompt text."""
        result = PromptBuilder(mode="agent", agent_name="general").build()
        summary = result.safe_summary()
        # Should not have any key that leaks instruction text
        for key, value in summary.items():
            if isinstance(value, str):
                self.assertNotIn("You are Boss", value)

    def test_safe_summary_instruction_sources_are_paths(self):
        td, root = _temp_workspace(
            boss_md="Test instructions",
            review_md="Review guide",
            rules={
                "00-core.md": (
                    "+++\ntitle = \"Core\"\ntargets = [\"all\"]\n"
                    "always = true\n+++\n\nCore content."
                ),
            },
        )
        try:
            result = (
                PromptBuilder(mode="review", agent_name="general")
                .with_workspace(root)
                .build()
            )
            summary = result.safe_summary()
            # instruction_sources should contain file paths
            sources = summary["instruction_sources"]
            self.assertTrue(len(sources) > 0)
            for s in sources:
                # All sources should contain a path separator or .md
                self.assertTrue("/" in s or s.endswith(".md"), f"Unexpected source: {s}")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
