"""Structured outputs for the executor, verifier, reasoning and summary prompts.
(No defaults, for the same strict-mode reason as schemas/plan.py.)"""
from pydantic import BaseModel, Field


class ProposedCall(BaseModel):
    tool: str = Field(description="Registry name of the tool, e.g. 'gmail.search_emails'")
    arguments_json: str = Field(description="The tool arguments as a JSON object encoded in a string")


class ExecutorDecision(BaseModel):
    reasoning: str = Field(description="One or two sentences: why these calls accomplish the step")
    cannot_proceed: bool = Field(description="True only if the step cannot be done with the information available")
    cannot_proceed_reason: str | None
    calls: list[ProposedCall]


class StepVerdict(BaseModel):
    satisfied: bool = Field(description="True only if the step's goal was met for everything it covered")
    reason: str = Field(description="Short justification based on the tool results")
    retry_advice: str | None = Field(description="If not satisfied: concretely what to do differently, else null")


class ReasoningOutput(BaseModel):
    content: str


class FinalSummary(BaseModel):
    summary: str
