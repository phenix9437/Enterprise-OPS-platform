"""The structured plan the LLM must return.

Deliberately no default values: OpenAI strict structured-output mode requires every field
to be present, which also forces the model to make an explicit choice for each one.
"""
from enum import Enum

from pydantic import BaseModel, Field


class Intent(str, Enum):
    INVOICE_FOLLOWUP = "invoice_followup"
    REPORT_GENERATION = "report_generation"
    PURCHASE_REQUEST = "purchase_request"
    DATA_LOOKUP = "data_lookup"
    GENERAL = "general"
    UNSUPPORTED = "unsupported"


class ToolCategory(str, Enum):
    DATABASE = "database"
    GMAIL = "gmail"
    DRIVE = "drive"
    CRM = "crm"
    REASONING = "reasoning"  # pure LLM work (summarise, draft text) - no external system touched


class ActionType(str, Enum):
    READ = "read"
    WRITE = "write"   # changes something outside our system (sends email, updates a record)
    NONE = "none"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlanStep(BaseModel):
    id: int = Field(description="1-based step number, unique within the plan")
    title: str = Field(description="Short imperative title, max ~8 words")
    description: str = Field(description="What exactly this step does and what it produces")
    tool: ToolCategory
    action: ActionType
    depends_on: list[int] = Field(description="Ids of earlier steps whose output this step needs")
    risk: RiskLevel
    requires_approval: bool = Field(description="True if a human must approve before this step runs")


class Plan(BaseModel):
    objective: str = Field(description="One-sentence restatement of what the user wants")
    intent: Intent
    requires_clarification: bool = Field(
        description="True ONLY if critical information is missing and no sensible plan is possible"
    )
    clarification_question: str | None = Field(description="The single question to ask, or null")
    steps: list[PlanStep]
