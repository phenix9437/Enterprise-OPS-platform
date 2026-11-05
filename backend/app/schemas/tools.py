import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.tools.types import ToolSpec
from app.schemas.plan import RiskLevel


class ToolRead(BaseModel):
    name: str
    server: str
    category: str
    description: str
    action: str
    risk: str
    requires_approval: bool
    input_schema: dict

    @classmethod
    def from_spec(cls, s: ToolSpec) -> "ToolRead":
        return cls(name=s.name, server=s.server, category=s.category.value, description=s.description,
                   action=s.action.value, risk=s.risk.value, requires_approval=s.risk == RiskLevel.HIGH,
                   input_schema=s.input_schema)


class ToolInvokeRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=150)
    approve: bool = False          # only managers/admins may set this
    task_id: uuid.UUID | None = None


class ToolInvokeResponse(BaseModel):
    execution_id: uuid.UUID
    tool: str
    cached: bool
    duration_ms: float
    result: Any


class ToolExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    idempotency_key: str | None
    status: str
    arguments: dict
    result: Any
    error: str | None
    user_id: uuid.UUID
    task_id: uuid.UUID | None
    approved_by: uuid.UUID | None
    approval_id: uuid.UUID | None
    attempts: int
    duration_ms: float
    created_at: datetime
    completed_at: datetime | None
