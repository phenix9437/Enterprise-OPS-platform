from app.llm.client import LLMError, LLMResult, LLMUsage
from app.schemas.plan import (
    ActionType, Intent, Plan, PlanStep, RiskLevel, ToolCategory,
)


class FakeLLM:
    """Plays back queued responses (Plan objects or exceptions) and records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def parse(self, messages, response_model):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(parsed=item, usage=LLMUsage("fake-model", 100, 50, 12.5))


def step(id, title="Do thing", tool=ToolCategory.DATABASE, action=ActionType.READ,
         depends_on=(), risk=RiskLevel.LOW, approval=False):
    return PlanStep(id=id, title=title, description=f"{title} in detail", tool=tool, action=action,
                    depends_on=list(depends_on), risk=risk, requires_approval=approval)


def plan(*steps, intent=Intent.INVOICE_FOLLOWUP, objective="Remind overdue customers"):
    return Plan(objective=objective, intent=intent, requires_clarification=False,
                clarification_question=None, steps=list(steps))


def clarification(question="Which customers should I include?"):
    return Plan(objective="Unclear request", intent=Intent.GENERAL, requires_clarification=True,
                clarification_question=question, steps=[])


GOOD_PLAN = plan(
    step(1, "Find overdue invoices"),
    step(2, "Draft reminders", tool=ToolCategory.REASONING, action=ActionType.NONE, depends_on=[1]),
    step(3, "Send reminders", tool=ToolCategory.GMAIL, action=ActionType.WRITE, depends_on=[2],
         risk=RiskLevel.HIGH, approval=True),
)

__all__ = ["FakeLLM", "LLMError", "GOOD_PLAN", "plan", "step", "clarification"]
