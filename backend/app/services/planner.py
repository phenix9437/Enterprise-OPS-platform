"""Turns a user instruction into a validated Plan, using the LLM.

Failure recovery loop: if the model's plan fails OUR validation, we tell it exactly what was
wrong and ask again (up to `planner_max_attempts`). Provider-level retries (429/5xx) are handled
inside the OpenAI SDK.
"""
from dataclasses import dataclass

from app.core.config import get_settings
from app.llm.client import LLMClient, LLMError, LLMUsage
from app.llm.prompts import PLANNER_SYSTEM_PROMPT, REPAIR_PROMPT
from app.schemas.plan import Plan
from app.services.plan_validation import PlanValidationError, validate_and_normalize


@dataclass
class PlanningOutcome:
    plan: Plan
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


class PlanningFailed(Exception):
    """Raised when no valid plan could be obtained. Carries usage so cost is still recorded."""

    def __init__(self, message: str, calls: int, usages: list[LLMUsage]):
        super().__init__(message)
        self.calls = calls
        self.usages = usages


async def create_plan(llm: LLMClient, instruction: str, history: list[dict]) -> PlanningOutcome:
    s = get_settings()
    messages: list[dict] = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT.format(max_steps=s.planner_max_steps)},
        *history,
        {"role": "user", "content": instruction},
    ]

    usages: list[LLMUsage] = []
    last_error = "unknown error"
    for _ in range(s.planner_max_attempts):
        try:
            result = await llm.parse(messages, Plan)
        except LLMError as exc:
            raise PlanningFailed(str(exc), len(usages) + 1, usages) from exc
        usages.append(result.usage)

        try:
            plan = validate_and_normalize(result.parsed, s.planner_max_steps)
        except PlanValidationError as exc:
            last_error = str(exc)
            messages += [
                {"role": "assistant", "content": result.parsed.model_dump_json()},
                {"role": "user", "content": REPAIR_PROMPT.format(error=last_error)},
            ]
            continue

        return PlanningOutcome(
            plan=plan,
            model=usages[-1].model,
            calls=len(usages),
            prompt_tokens=sum(u.prompt_tokens for u in usages),
            completion_tokens=sum(u.completion_tokens for u in usages),
            latency_ms=round(sum(u.latency_ms for u in usages), 1),
        )

    raise PlanningFailed(f"Could not produce a valid plan: {last_error}", len(usages), usages)


def render_reply(plan: Plan) -> str:
    """Plain-text assistant message shown in the chat for a plan."""
    if plan.requires_clarification:
        return plan.clarification_question or ""
    if not plan.steps:
        return f"I can't do that with the tools I have. {plan.objective}"
    lines = [f"Here's my plan: {plan.objective}", ""]
    for st in plan.steps:
        flag = "  [needs your approval]" if st.requires_approval else ""
        lines.append(f"{st.id}. {st.title} ({st.tool.value}, {st.risk.value} risk){flag}")
    return "\n".join(lines)
