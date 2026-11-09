import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    workflow: str = Field(min_length=1, max_length=60)
    params: dict[str, Any] = Field(default_factory=dict)
    cron: str = Field(min_length=9, max_length=100, description="5 fields, e.g. '0 9 * * 1-5' = 09:00 on weekdays")
    timezone: str = Field(default="UTC", max_length=60)
    enabled: bool = True
    auto_execute: bool = Field(default=True, description="False: only create the planned task; a person starts it")
    overlap_policy: Literal["skip", "allow"] = "skip"


class ScheduleUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    params: dict[str, Any] | None = None
    cron: str | None = Field(default=None, min_length=9, max_length=100)
    timezone: str | None = Field(default=None, max_length=60)
    enabled: bool | None = None
    auto_execute: bool | None = None
    overlap_policy: Literal["skip", "allow"] | None = None


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    workflow: str
    params: dict
    cron: str
    timezone: str
    enabled: bool
    auto_execute: bool
    overlap_policy: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_task_id: uuid.UUID | None
    created_at: datetime


class WorkflowRunRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    execute: bool = True


class WorkflowRunResponse(BaseModel):
    task_id: uuid.UUID
    workflow: str
    status: str
    steps: int
