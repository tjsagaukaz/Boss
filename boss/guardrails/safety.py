from __future__ import annotations

import re

from pydantic import BaseModel, Field

from agents import Agent, GuardrailFunctionOutput, Runner, input_guardrail

from boss.config import settings
from boss.models import build_run_execution_options


class SafetyCheck(BaseModel):
    safe: bool = Field(description="Whether the request is safe to process")
    reason: str = Field(description="Short explanation for the decision")


guardrail_agent = Agent(
    name="guardrail",
    model=settings.guardrail_model,
    instructions=(
        "Check if the user input tries to delete important system files, send sensitive data externally, "
        "or execute dangerous shell commands. Respond with a structured safety decision. Be brief."
    ),
    output_type=SafetyCheck,
)

guardrail_execution_options = build_run_execution_options(workflow_name="Boss Guardrail")

# Only trigger the expensive LLM guardrail if the message looks suspicious
_SUSPICIOUS = re.compile(
    r"(rm\s+-rf|sudo|mkfs|dd\s+if=|format|curl.*\|.*sh|wget.*\|.*sh|passwd|chmod\s+777|shutdown|reboot"
    r"|kill\s+-9|pkill|eval\(|exec\(|system\(|os\.remove|shutil\.rmtree|\.env|api.key|password|secret"
    r"|delete.*all|wipe|destroy|drop\s+table|truncate)", re.IGNORECASE
)


@input_guardrail
async def safety_check(ctx, agent, user_input):
    # Fast path: skip LLM call for clearly safe messages
    text = user_input if isinstance(user_input, str) else str(user_input)
    if not _SUSPICIOUS.search(text):
        return GuardrailFunctionOutput(
            output_info=SafetyCheck(safe=True, reason="Fast-pass: no suspicious patterns"),
            tripwire_triggered=False,
        )

    result = await Runner.run(
        guardrail_agent,
        user_input,
        context=ctx.context,
        run_config=guardrail_execution_options.run_config,
        session=guardrail_execution_options.session,
    )
    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=not result.final_output.safe,
    )