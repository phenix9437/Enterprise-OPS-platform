import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.agents.runner import TaskRunner
from app.api.deps import DB, CurrentUser, client_ip, lenient_llm, require_roles
from app.llm.client import LLMClient
from app.models.chat import Task, TaskEvent, TaskStatus
from app.models.user import Role, User
from app.schemas.chat import ExecutionAccepted, TaskEventRead
from app.services.task_start import claim_planned_task

router = APIRouter(tags=["execution"])
CanRun = Depends(require_roles(Role.ADMIN, Role.MANAGER, Role.OPERATOR))
PRIVILEGED = {Role.ADMIN.value, Role.MANAGER.value}


def get_runner(request: Request) -> TaskRunner:
    runner = getattr(request.app.state, "task_runner", None)
    if runner is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Task execution is not available (tool layer is off)")
    return runner


async def _task_for(db, task_id: uuid.UUID, user: User, owner_only: bool) -> Task:
    task = await db.get(Task, task_id)
    allowed = task is not None and (task.user_id == user.id or (not owner_only and user.role in PRIVILEGED)
                                    or user.role in PRIVILEGED)
    if not allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return task


@router.post("/tasks/{task_id}/execute", response_model=ExecutionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def execute_task(task_id: uuid.UUID, request: Request, db: DB, user: User = CanRun,
                       runner: TaskRunner = Depends(get_runner), llm: LLMClient = Depends(lenient_llm)):
    """Start running a planned task in the background. Poll GET /tasks/{id} and /tasks/{id}/events."""
    task = await _task_for(db, task_id, user, owner_only=True)
    if task.status != TaskStatus.PLANNED.value or not (task.plan or {}).get("steps"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Only a planned task with steps can be executed (status: {task.status})")

    if not await claim_planned_task(db, task_id, actor_id=user.id, ip=client_ip(request)):
        raise HTTPException(status.HTTP_409_CONFLICT, "This task is already being executed")
    runner.start(task_id, llm)
    return ExecutionAccepted(task_id=task_id, status=TaskStatus.RUNNING.value)


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventRead])
async def task_events(task_id: uuid.UUID, user: CurrentUser, db: DB):
    await _task_for(db, task_id, user, owner_only=True)
    return (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq))).all()
