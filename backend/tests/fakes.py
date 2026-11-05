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


# ------------------------------------------------------------ Phase 4 helpers ----
import json  # noqa: E402
from collections import defaultdict, deque  # noqa: E402

from app.agents.schemas import ExecutorDecision, FinalSummary, ProposedCall, ReasoningOutput, StepVerdict  # noqa: E402


class ScriptedLLM:
    """Routes by requested output type, so each agent (executor/verifier/...) has its own script."""

    def __init__(self):
        self.queues = defaultdict(deque)
        self.calls: list[tuple[str, list[dict]]] = []

    def add(self, model, *items):
        self.queues[model].extend(items)
        return self

    def count(self, model) -> int:
        return sum(1 for name, _ in self.calls if name == model.__name__)

    def prompts(self, model) -> list[str]:
        return [m[-1]["content"] for name, m in self.calls if name == model.__name__]

    async def parse(self, messages, response_model):
        self.calls.append((response_model.__name__, messages))
        queue = self.queues[response_model]
        if not queue:
            raise AssertionError(f"Unscripted LLM call for {response_model.__name__}")
        item = queue.popleft()
        if isinstance(item, Exception):
            raise item
        return LLMResult(parsed=item, usage=LLMUsage("fake-model", 10, 5, 1.0))


def tc(tool, **args):
    return ProposedCall(tool=tool, arguments_json=json.dumps(args))


def decide(*calls, cannot=None):
    return ExecutorDecision(reasoning="because", cannot_proceed=cannot is not None,
                            cannot_proceed_reason=cannot, calls=list(calls))


def verdict(ok=True, reason="all good", advice=None):
    return StepVerdict(satisfied=ok, reason=reason, retry_advice=advice)


def reasoning(text):
    return ReasoningOutput(content=text)


def final(text="All done."):
    return FinalSummary(summary=text)


# plans used by the execution tests
READ_ONLY_PLAN = plan(
    step(1, "Find overdue invoices"),
    step(2, "Summarise", tool=ToolCategory.REASONING, action=ActionType.NONE, depends_on=[1]),
    intent=Intent.DATA_LOOKUP, objective="Summarise overdue invoices")

REMIND_PLAN = plan(
    step(1, "Find overdue invoices"),
    step(2, "Check email history", tool=ToolCategory.GMAIL, depends_on=[1]),
    step(3, "Draft reminders", tool=ToolCategory.REASONING, action=ActionType.NONE, depends_on=[1, 2]),
    step(4, "Send reminders", tool=ToolCategory.GMAIL, action=ActionType.WRITE, depends_on=[3],
         risk=RiskLevel.HIGH, approval=True),
    objective="Send payment reminders")

LOG_PLAN = plan(
    step(1, "Log a reminder", action=ActionType.WRITE, risk=RiskLevel.MEDIUM),
    objective="Log a reminder")
