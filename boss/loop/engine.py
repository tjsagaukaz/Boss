"""Loop engine: bounded autonomous edit-run-test-fix lifecycle.

The engine wraps the normal agent streaming flow.  Each iteration runs
the agent with accumulated context (prior attempt results, test output,
diff summary).  Budget checks happen *between* iterations so a running
agent turn is never interrupted.

Permission gates pause the loop — the engine yields the same
``permission_request`` SSE events that the single-pass flow uses, letting
the frontend / job system handle approval and resume.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator

from boss.loop.policy import ExecutionStyle, LoopBudget
from boss.loop.state import (
    AttemptCommand,
    LoopAttempt,
    LoopPhase,
    LoopState,
    StopReason,
    save_loop_state,
)

logger = logging.getLogger(__name__)

_TAIL_LIMIT = 2000  # chars of stdout/stderr to keep per command


def _clip(text: str, limit: int = _TAIL_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _build_iteration_prompt(
    *,
    task: str,
    attempt_number: int,
    micro_plan: list[str],
    prior_attempts: list[LoopAttempt],
    phase: str,
) -> str:
    """Build the agent prompt for this iteration, including loop context."""
    parts: list[str] = []

    parts.append(f"## Task\n{task}\n")

    if micro_plan:
        plan_text = "\n".join(f"{i+1}. {step}" for i, step in enumerate(micro_plan))
        parts.append(f"## Micro-Plan\n{plan_text}\n")

    parts.append(f"## Loop Context\nAttempt {attempt_number}. Phase: {phase}.\n")

    if prior_attempts:
        parts.append("## Prior Attempts")
        for attempt in prior_attempts[-3:]:  # keep last 3 for context window
            status = "PASSED" if attempt.test_passed else "FAILED"
            parts.append(f"\n### Attempt {attempt.attempt_number} [{status}]")
            if attempt.error:
                parts.append(f"Error: {attempt.error}")
            if attempt.test_output_tail:
                parts.append(f"Test output:\n```\n{attempt.test_output_tail}\n```")
            if attempt.diff_summary:
                parts.append(f"Diff:\n```\n{attempt.diff_summary}\n```")
            if attempt.assistant_output:
                tail = _clip(attempt.assistant_output, 1500)
                parts.append(f"Assistant output (tail):\n{tail}")
            # Include preview verification results for visual context
            if attempt.verification_method and attempt.verification_method != "skipped":
                parts.append(f"Preview verification: {attempt.verification_method}")
                if attempt.preview_evidence:
                    ev = attempt.preview_evidence
                    if ev.get("page_title"):
                        parts.append(f"  Page title: {ev['page_title']}")
                    if ev.get("console_errors"):
                        parts.append(f"  Console errors: {ev['console_errors']}")
                    if ev.get("network_errors"):
                        parts.append(f"  Network errors: {ev['network_errors']}")

    if attempt_number == 1:
        parts.append(
            "\n## Instructions\n"
            "1. Understand the task and gather context.\n"
            "2. Propose a brief micro-plan of edits and tests.\n"
            "3. Make the edits.\n"
            "4. Run the test/build command to verify.\n"
            "5. If tests pass, reply with LOOP_RESULT:SUCCESS.\n"
            "6. If tests fail, reply with LOOP_RESULT:RETRY and explain what to fix.\n"
            "7. If the task cannot be completed, reply with LOOP_RESULT:STOP and explain why.\n"
        )
    else:
        parts.append(
            "\n## Instructions (Retry)\n"
            "The previous attempt did not pass. Review the test output above, "
            "fix the issue, re-run the test, and report LOOP_RESULT:SUCCESS, "
            "LOOP_RESULT:RETRY, or LOOP_RESULT:STOP.\n"
        )

    return "\n".join(parts)


def _parse_loop_result(text: str) -> str | None:
    """Extract LOOP_RESULT directive from assistant output."""
    for line in reversed(text.splitlines()):
        stripped = line.strip().upper()
        if "LOOP_RESULT:SUCCESS" in stripped:
            return "success"
        if "LOOP_RESULT:RETRY" in stripped:
            return "retry"
        if "LOOP_RESULT:STOP" in stripped:
            return "stop"
    return None


class LoopEngine:
    """Drives the bounded iterative loop.

    Usage::

        engine = LoopEngine(...)
        async for sse_chunk in engine.run():
            yield sse_chunk  # forward to client
    """

    def __init__(
        self,
        *,
        task: str,
        session_id: str,
        budget: LoopBudget,
        mode: str = "agent",
        workspace_root: str | None = None,
        loop_id: str | None = None,
        job_id: str | None = None,
        resume_state: LoopState | None = None,
    ):
        self._task = task
        self._session_id = session_id
        self._budget = budget
        self._mode = mode
        self._workspace_root = workspace_root
        self._job_id = job_id
        self._pending_preview_content: list[dict] | None = None

        if resume_state:
            self._state = resume_state
            self._state.pending_run_id = None  # clear stale pending
        else:
            lid = loop_id or uuid.uuid4().hex
            self._state = LoopState(
                loop_id=lid,
                session_id=session_id,
                task_description=task,
                budget=budget.to_dict(),
                execution_style=ExecutionStyle.ITERATIVE.value,
                started_at=time.time(),
                job_id=job_id,
                workspace_root=workspace_root,
            )

    @property
    def state(self) -> LoopState:
        return self._state

    async def run(self) -> AsyncIterator[str]:
        """Run the iterative loop, yielding SSE events."""
        # Emit initial loop status
        yield _sse_event({
            "type": "loop_status",
            "loop_id": self._state.loop_id,
            "status": "started",
            "budget": self._budget.to_dict(),
            "task": self._task,
        })
        save_loop_state(self._state)

        while True:
            # --- Budget checks between iterations ---
            stop = self._check_budget()
            if stop:
                self._state.stop_reason = stop.value
                self._state.finished_at = time.time()
                save_loop_state(self._state)
                yield _sse_event({
                    "type": "loop_status",
                    "loop_id": self._state.loop_id,
                    "status": "stopped",
                    "stop_reason": stop.value,
                    "attempt": self._state.current_attempt,
                })
                return

            # Start new attempt
            self._state.current_attempt += 1
            attempt = LoopAttempt(
                attempt_number=self._state.current_attempt,
                started_at=time.time(),
            )
            self._state.attempts.append(attempt)
            self._state.phase = LoopPhase.PLAN.value if self._state.current_attempt == 1 else LoopPhase.EDIT.value

            yield _sse_event({
                "type": "loop_attempt",
                "loop_id": self._state.loop_id,
                "attempt_number": self._state.current_attempt,
                "phase": self._state.phase,
                "budget_remaining": self._budget_remaining(),
            })

            save_loop_state(self._state)

            # Build iteration prompt
            prompt = _build_iteration_prompt(
                task=self._task,
                attempt_number=self._state.current_attempt,
                micro_plan=self._state.micro_plan,
                prior_attempts=self._state.attempts[:-1],
                phase=self._state.phase,
            )

            # Run agent iteration — stream through and collect results
            assistant_text = ""
            permission_blocked = False
            commands_this_attempt: list[AttemptCommand] = []
            test_output = ""

            try:
                async for chunk in self._run_agent_iteration(
                    prompt, preview_content=self._pending_preview_content,
                ):
                    payload = _try_parse_sse(chunk)

                    if payload is not None:
                        event_type = payload.get("type", "")

                        # Track tool calls as commands
                        if event_type == "tool_call":
                            cmd_name = payload.get("name", "")
                            if cmd_name:
                                cmd_rec = AttemptCommand(
                                    command=f"{cmd_name}({payload.get('arguments', '')[:200]})",
                                    exit_code=None,
                                    stdout_tail="",
                                    stderr_tail="",
                                    verdict=payload.get("execution_type", "unknown"),
                                    timestamp=time.time(),
                                )
                                commands_this_attempt.append(cmd_rec)
                                self._state.total_commands += 1

                        elif event_type == "tool_result":
                            output = payload.get("output", "")
                            if commands_this_attempt:
                                commands_this_attempt[-1].stdout_tail = _clip(output)
                                # Heuristic: detect test output
                                if any(kw in output.lower() for kw in ("passed", "failed", "error", "ok", "fail")):
                                    test_output = _clip(output, 3000)

                        elif event_type == "text":
                            assistant_text += payload.get("content", "")

                        elif event_type == "permission_request":
                            permission_blocked = True
                            self._state.pending_run_id = payload.get("run_id")
                            self._state.phase = LoopPhase.EDIT.value
                            save_loop_state(self._state)
                            yield chunk
                            # Stop the loop — will resume when permission resolves
                            attempt.finished_at = time.time()
                            attempt.commands = commands_this_attempt
                            attempt.assistant_output = _clip(assistant_text, 4000)
                            attempt.stop_reason = StopReason.APPROVAL_BLOCKED.value
                            self._state.stop_reason = StopReason.APPROVAL_BLOCKED.value
                            save_loop_state(self._state)
                            yield _sse_event({
                                "type": "loop_status",
                                "loop_id": self._state.loop_id,
                                "status": "paused",
                                "stop_reason": StopReason.APPROVAL_BLOCKED.value,
                                "attempt": self._state.current_attempt,
                            })
                            return

                        elif event_type == "done":
                            # Suppress per-iteration done events — the loop
                            # wrapper emits the final done when all iterations
                            # are complete.  Forwarding these would cause the
                            # client to stop listening after the first pass.
                            continue

                    # Forward everything except suppressed events to client
                    yield chunk

            except Exception as exc:
                logger.exception("Loop iteration %d failed", self._state.current_attempt)
                attempt.error = str(exc)[:1000]
                attempt.finished_at = time.time()
                attempt.commands = commands_this_attempt
                attempt.assistant_output = _clip(assistant_text, 4000)
                self._state.stop_reason = StopReason.ERROR.value
                self._state.finished_at = time.time()
                save_loop_state(self._state)
                yield _sse_event({
                    "type": "loop_status",
                    "loop_id": self._state.loop_id,
                    "status": "stopped",
                    "stop_reason": StopReason.ERROR.value,
                    "error": attempt.error,
                    "attempt": self._state.current_attempt,
                })
                return

            if permission_blocked:
                return

            # Clear preview content — consumed by this iteration
            self._pending_preview_content = None

            # Finalize attempt
            attempt.finished_at = time.time()
            attempt.commands = commands_this_attempt
            attempt.assistant_output = _clip(assistant_text, 4000)
            attempt.test_output_tail = test_output

            # Parse loop result from assistant output
            result = _parse_loop_result(assistant_text)

            if result == "success":
                attempt.test_passed = True

                # Preview verification for UI/frontend tasks
                preview_result = _try_preview_verification(
                    task=self._task,
                    workspace_root=self._workspace_root,
                )
                attempt.verification_method = preview_result["method"]
                attempt.preview_evidence = preview_result.get("evidence")

                if preview_result.get("has_blocking_errors"):
                    # Preview found errors — don't declare success yet, retry
                    # Store the multimodal content so the next iteration can see
                    # the screenshot alongside the error descriptions.
                    self._pending_preview_content = preview_result.get("model_content")
                    attempt.test_passed = False
                    self._state.total_test_failures += 1
                    self._state.phase = LoopPhase.INSPECT.value

                    yield _sse_event({
                        "type": "loop_status",
                        "loop_id": self._state.loop_id,
                        "status": "preview_retry",
                        "verification_method": preview_result["method"],
                        "preview_errors": preview_result.get("error_summary", ""),
                        "attempt": self._state.current_attempt,
                    })

                    save_loop_state(self._state)
                    continue

                self._state.stop_reason = StopReason.SUCCESS.value
                self._state.finished_at = time.time()
                self._state.phase = LoopPhase.DONE.value
                save_loop_state(self._state)
                yield _sse_event({
                    "type": "loop_status",
                    "loop_id": self._state.loop_id,
                    "status": "completed",
                    "stop_reason": StopReason.SUCCESS.value,
                    "verification_method": preview_result["method"],
                    "attempt": self._state.current_attempt,
                })
                return

            if result == "stop":
                self._state.stop_reason = StopReason.ERROR.value
                self._state.finished_at = time.time()
                self._state.phase = LoopPhase.DONE.value
                save_loop_state(self._state)
                yield _sse_event({
                    "type": "loop_status",
                    "loop_id": self._state.loop_id,
                    "status": "stopped",
                    "stop_reason": "agent_stopped",
                    "attempt": self._state.current_attempt,
                })
                return

            # Treat as retry (explicit or implicit)
            attempt.test_passed = False
            self._state.total_test_failures += 1
            self._state.phase = LoopPhase.INSPECT.value

            # Extract micro-plan from first attempt assistant output
            if self._state.current_attempt == 1 and not self._state.micro_plan:
                self._state.micro_plan = _extract_micro_plan(assistant_text)

            save_loop_state(self._state)
            # Continue loop

    def _check_budget(self) -> StopReason | None:
        """Check all budget limits.  Return a stop reason or None."""
        if self._state.current_attempt >= self._budget.max_attempts:
            return StopReason.MAX_ATTEMPTS

        if self._state.total_commands >= self._budget.max_commands:
            return StopReason.MAX_COMMANDS

        if self._state.elapsed_seconds >= self._budget.max_wall_seconds:
            return StopReason.MAX_WALL_TIME

        if (
            self._budget.max_test_failures is not None
            and self._state.total_test_failures >= self._budget.max_test_failures
        ):
            return StopReason.MAX_FAILURES

        return None

    def _budget_remaining(self) -> dict:
        return {
            "attempts": max(0, self._budget.max_attempts - self._state.current_attempt),
            "commands": max(0, self._budget.max_commands - self._state.total_commands),
            "wall_seconds": max(0.0, self._budget.max_wall_seconds - self._state.elapsed_seconds),
        }

    async def _run_agent_iteration(
        self, prompt: str, *, preview_content: list[dict] | None = None,
    ) -> AsyncIterator[str]:
        """Run one agent pass using the existing streaming infrastructure."""
        # Import here to avoid circular imports
        from boss.api import _stream_chat_run
        from boss.context.manager import SessionContextManager

        ctx = SessionContextManager()
        prepared = ctx.prepare_input(self._session_id, prompt)

        # Inject multimodal preview content into model input when available.
        # This appends image/text content parts from the preview verification
        # so the model can see the screenshot alongside the text prompt.
        if preview_content:
            prepared.model_input.append({
                "role": "user",
                "content": preview_content,
            })

        async for chunk in _stream_chat_run(
            run_input=prepared.model_input,
            session_id=self._session_id,
            emit_session=False,
            mode=self._mode,
            workspace_root=self._workspace_root,
            loop_id=self._state.loop_id,
        ):
            yield chunk


def _try_parse_sse(chunk: str) -> dict | None:
    """Try to parse an SSE data line into a dict."""
    if not chunk.startswith("data: "):
        return None
    try:
        return json.loads(chunk[6:].strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_micro_plan(text: str) -> list[str]:
    """Extract numbered steps from assistant output as a micro-plan."""
    import re

    steps: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)[.)]\s+(.+)", line)
        if m:
            steps.append(m.group(2).strip())
    return steps[:10]  # cap at 10 steps


# ── Frontend task detection ─────────────────────────────────────────

_FRONTEND_TASK_SIGNALS = re.compile(
    r"\b(swiftui|swift\s+build|uikit|frontend|bossapp|chatview|contentview|"
    r"nswindow|appkit|view\s+model|preview|ui\s+task|css|html|react|vue|"
    r"angular|component|layout|render|display|visual)\b",
    re.IGNORECASE,
)


def _is_frontend_task(task: str) -> bool:
    """Heuristic: does this task description suggest frontend/UI work?"""
    return bool(_FRONTEND_TASK_SIGNALS.search(task))


def _try_preview_verification(
    *,
    task: str,
    workspace_root: str | None,
) -> dict:
    """Attempt preview verification if this is a UI task and tooling is available.

    Returns a dict with:
        - method: "visual" | "textual" | "skipped"
        - evidence: dict with capture data (if available)
        - has_blocking_errors: bool
        - error_summary: str (if errors found)
        - model_content: list of content parts for model input (if available)
    """
    if not _is_frontend_task(task):
        return {
            "method": "skipped",
            "reason": "Not a frontend/UI task",
            "has_blocking_errors": False,
        }

    try:
        from boss.preview.session import (
            VerificationMethod,
            detect_preview_capabilities,
            get_active_session,
        )

        caps = detect_preview_capabilities()
        if not caps.can_preview:
            return {
                "method": VerificationMethod.SKIPPED.value,
                "reason": "No preview tooling available",
                "has_blocking_errors": False,
            }

        # Check for an active preview session
        session = get_active_session(workspace_root)
        if session is None or not session.is_running or not session.url:
            return {
                "method": VerificationMethod.SKIPPED.value,
                "reason": "No active preview session with URL",
                "has_blocking_errors": False,
            }

        # Attempt capture
        if not caps.can_screenshot:
            # No Playwright — can only report session status
            return {
                "method": VerificationMethod.TEXTUAL.value,
                "evidence": {
                    "session_url": session.url,
                    "session_status": session.status.value,
                },
                "has_blocking_errors": False,
            }

        import time
        from boss.config import settings
        from boss.preview.session import capture_screenshot

        captures_dir = settings.app_data_dir / "preview_captures"
        captures_dir.mkdir(parents=True, exist_ok=True)
        output_path = captures_dir / f"loop_verify_{int(time.time())}.png"

        capture = capture_screenshot(session.url, output_path)

        # Determine verification method and build model content
        model_content: list[dict] = []
        try:
            from boss.preview.vision import capture_to_model_input
            model_input = capture_to_model_input(capture)
            method = model_input["method"]
            model_content = model_input.get("content", [])
        except ImportError:
            method = VerificationMethod.TEXTUAL.value

        # Write verification method back to capture and session metadata
        capture.verification_method = method
        session.last_capture = capture
        session.verification_method = method

        # Check for blocking errors
        has_blocking = bool(capture.console_errors) or bool(capture.network_errors)
        error_parts: list[str] = []
        if capture.console_errors:
            error_parts.append(f"{len(capture.console_errors)} console error(s)")
        if capture.network_errors:
            error_parts.append(f"{len(capture.network_errors)} network error(s)")

        return {
            "method": method,
            "model_content": model_content,
            "evidence": {
                "screenshot_path": capture.screenshot_path,
                "page_title": capture.page_title,
                "console_errors": capture.console_errors[:5],
                "network_errors": capture.network_errors[:5],
                "dom_summary_length": len(capture.dom_summary or ""),
            },
            "has_blocking_errors": has_blocking,
            "error_summary": "; ".join(error_parts) if error_parts else "",
        }

    except Exception as exc:
        logger.warning("Preview verification failed: %s", exc)
        return {
            "method": "skipped",
            "reason": f"Preview verification error: {str(exc)[:200]}",
            "has_blocking_errors": False,
        }
