import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.plan import Plan


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Message cannot be blank")
        return v.strip()


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    instruction: str
    status: str
    intent: str | None
    plan: Plan | None
    error: str | None
    llm_model: str | None
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    llm_latency_ms: float
    current_step: int | None
    result: str | None
    pending_approval: dict | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class TaskEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    type: str
    step_id: int | None
    message: str
    data: dict | None
    created_at: datetime


class ExecutionAccepted(BaseModel):
    task_id: uuid.UUID
    status: str


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    task: TaskRead


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    task_id: uuid.UUID | None
    created_at: datetime


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
