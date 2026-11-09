import json
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Task, TaskStatus
from app.models.schedule import Schedule
from app.models.user import User
from app.workflows.templates import TEMPLATES, build_workflow_plan


async def create_workflow_task(db: AsyncSession, user: User, name: str, params: dict, *,
                               schedule: Schedule | None = None, scheduled_for: datetime | None = None) -> Task:
    """Build the plan from the template and store it as a PLANNED task owned by `user`.
    Raises KeyError (unknown workflow) or pydantic.ValidationError (bad params)."""
    plan, validated = build_workflow_plan(name, params)
    template = TEMPLATES[name]
    task = Task(
        user_id=user.id, conversation_id=None, workflow=name,
        instruction=f"Workflow: {template.title} {json.dumps(validated, default=str)}",
        status=TaskStatus.PLANNED.value, intent=plan["intent"], plan=plan,
        schedule_id=schedule.id if schedule else None, scheduled_for=scheduled_for)
    db.add(task)
    await db.flush()
    return task
