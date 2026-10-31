"""Server-side checks on the LLM's plan. The model proposes; this code decides what is acceptable.

Two jobs:
  1. Structural validation  - reject plans that can't be executed (bad ids, forward dependencies...).
  2. Safety floor           - never trust the model's own risk/approval flags for dangerous actions.
"""
from app.schemas.plan import ActionType, Intent, Plan, RiskLevel, ToolCategory


class PlanValidationError(Exception):
    pass


_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def _at_least(current: RiskLevel, minimum: RiskLevel) -> RiskLevel:
    return current if _RISK_ORDER[current] >= _RISK_ORDER[minimum] else minimum


def validate_and_normalize(plan: Plan, max_steps: int) -> Plan:
    """Returns a (possibly adjusted) copy of the plan, or raises PlanValidationError."""
    if plan.requires_clarification:
        if not (plan.clarification_question and plan.clarification_question.strip()):
            raise PlanValidationError("requires_clarification is true but no clarification_question was given")
        if plan.steps:
            raise PlanValidationError("A plan that asks for clarification must have no steps")
        return plan

    if plan.intent == Intent.UNSUPPORTED:
        if plan.steps:
            raise PlanValidationError("An unsupported request must have no steps")
        return plan

    if not plan.steps:
        raise PlanValidationError("The plan has no steps")
    if len(plan.steps) > max_steps:
        raise PlanValidationError(f"The plan has {len(plan.steps)} steps; the maximum is {max_steps}")

    seen: set[int] = set()
    for step in plan.steps:
        if step.id in seen:
            raise PlanValidationError(f"Duplicate step id {step.id}")
        for dep in step.depends_on:
            # deps must point at EARLIER, EXISTING steps -> guarantees no cycles
            if dep not in seen:
                raise PlanValidationError(
                    f"Step {step.id} depends on step {dep}, which does not appear earlier in the plan"
                )
        seen.add(step.id)

    # Safety floor
    normalized = plan.model_copy(deep=True)
    for step in normalized.steps:
        if step.tool == ToolCategory.REASONING and step.action != ActionType.NONE:
            step.action = ActionType.NONE  # reasoning never touches external systems
        if step.action == ActionType.WRITE:
            step.risk = _at_least(step.risk, RiskLevel.MEDIUM)
            if step.tool == ToolCategory.GMAIL:  # sending email is external and irreversible
                step.risk = RiskLevel.HIGH
        if step.risk == RiskLevel.HIGH:
            step.requires_approval = True
    return normalized
