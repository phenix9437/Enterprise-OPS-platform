import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Op = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "not_contains",
             "startswith", "endswith", "email_domain_in", "email_domain_not_in"]
LIST_OPS = {"in", "not_in", "email_domain_in", "email_domain_not_in"}
NUMBER_OPS = {"gt", "gte", "lt", "lte"}


class ArgCondition(BaseModel):
    field: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.]+$",
                       description="Argument name; dots reach into nested objects")
    op: Op
    value: Any

    @model_validator(mode="after")
    def _value_matches_op(self):
        if self.op in LIST_OPS and not (isinstance(self.value, list) and self.value):
            raise ValueError(f"'{self.op}' needs a non-empty list")
        if self.op in NUMBER_OPS and (isinstance(self.value, bool) or not isinstance(self.value, (int, float))):
            raise ValueError(f"'{self.op}' needs a number")
        return self


class PolicyBase(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    tool_pattern: str = Field(default="*", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.*?\[\]-]+$")
    min_risk: Literal["low", "medium", "high"] = "low"
    arg_conditions: list[ArgCondition] = Field(default_factory=list, max_length=10)
    action: Literal["require_approval", "block"] = "require_approval"
    approver_role: Literal["manager", "admin"] = "manager"
    allow_self_approval: bool = False
    expires_in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    message: str | None = Field(default=None, max_length=300)


class PolicyCreate(PolicyBase):
    name: str = Field(min_length=1, max_length=100)


class PolicyUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    tool_pattern: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.*?\[\]-]+$")
    min_risk: Literal["low", "medium", "high"] | None = None
    arg_conditions: list[ArgCondition] | None = Field(default=None, max_length=10)
    action: Literal["require_approval", "block"] | None = None
    approver_role: Literal["manager", "admin"] | None = None
    allow_self_approval: bool | None = None
    expires_in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    message: str | None = Field(default=None, max_length=300)


class PolicyRead(PolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    arg_conditions: list[dict]
    created_at: datetime
    updated_at: datetime


class PolicyEvaluateRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CallEdit(BaseModel):
    index: int = Field(ge=0)
    arguments: dict[str, Any]


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=500)
    edits: list[CallEdit] = Field(default_factory=list, max_length=20)
    remove: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("comment")
    @classmethod
    def _strip(cls, v):
        return v.strip() if v else v

    @model_validator(mode="after")
    def _rules(self):
        if self.decision == "reject":
            if not self.comment:
                raise ValueError("A reason (comment) is required when rejecting")
            if self.edits or self.remove:
                raise ValueError("edits/remove only apply when approving")
        return self


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID | None
    step_id: int | None
    attempt: int
    status: str
    reason: str
    reasons: list
    policies: list
    required_role: str
    allow_self_approval: bool
    requested_by: uuid.UUID
    requested_at: datetime
    expires_at: datetime | None
    proposed_calls: list
    final_calls: list | None
    modified: bool
    modifications: list | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_comment: str | None
    instruction: str | None = None      # what the user originally asked for (context for the reviewer)
    can_decide: bool = False            # can the CURRENT user decide this right now?
    cannot_decide_reason: str | None = None
