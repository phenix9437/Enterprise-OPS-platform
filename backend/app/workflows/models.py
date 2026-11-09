"""Structured outputs for the workflow steps where an LLM makes a JUDGEMENT.
Note what is deliberately NOT here: recipient addresses. The model decides what to say; code decides who to send to."""
from typing import Literal

from pydantic import BaseModel, Field


class ReminderDecision(BaseModel):
    invoice_id: int = Field(description="Exactly as shown in the digest")
    action: Literal["send", "skip"]
    reason: str = Field(description="One short sentence explaining the decision")
    subject: str | None = Field(description="Email subject if action is 'send', else null")
    body: str | None = Field(description="Email body if action is 'send', else null")


class ReminderDecisions(BaseModel):
    decisions: list[ReminderDecision]

    def to_summary(self) -> str:
        sends = [d for d in self.decisions if d.action == "send"]
        lines = [f"{len(sends)} reminder(s) to send, {len(self.decisions) - len(sends)} skipped."]
        lines += [f"- invoice {d.invoice_id}: {d.action} ({d.reason})" for d in self.decisions]
        return "\n".join(lines)


class PurchaseRecommendation(BaseModel):
    request_id: int = Field(description="Exactly as shown in the digest")
    action: Literal["approve", "reject", "needs_info"]
    quotation_id: int | None = Field(description="The quotation you recommend (for 'approve'), else null")
    rationale: str


class PurchaseRecommendations(BaseModel):
    recommendations: list[PurchaseRecommendation]

    def to_summary(self) -> str:
        lines = [f"{len(self.recommendations)} purchase request(s) reviewed."]
        lines += [f"- request {r.request_id}: {r.action} ({r.rationale})" for r in self.recommendations]
        return "\n".join(lines)


OUTPUT_MODELS = {"reminder_decisions": ReminderDecisions, "purchase_recommendations": PurchaseRecommendations}
