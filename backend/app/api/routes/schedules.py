import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.agents.runner import TaskRunner
from app.api.deps import DB, client_ip, lenient_llm, require_roles
from app.api.routes.execution import PRIVILEGED, get_runner
from app.core.config import get_settings
from app.llm.client import LLMClient
from app.models.chat import Task
from app.models.schedule import Schedule
from app.models.user import Role, User, utcnow
from app.schemas.chat import TaskRead
from app.schemas.schedules import ScheduleCreate, ScheduleRead, ScheduleUpdate, WorkflowRunResponse
from app.services.audit import record_audit
from app.services.scheduler import next_fire, validate_schedule_spec
from app.services.task_start import claim_planned_task
from app.services.workflow_runs import create_workflow_task
from app.workflows.templates import TEMPLATES

router = APIRouter(tags=["schedules"])
CanRun = Depends(require_roles(Role.ADMIN, Role.MANAGER, Role.OPERATOR))


def _check_params(workflow: str, params: dict) -> None:
    if workflow not in TEMPLATES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown workflow '{workflow}'")
    try:
        TEMPLATES[workflow].params_model.model_validate(params)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc


def _check_spec(cron: str, tz: str) -> None:
    try:
        validate_schedule_spec(cron, tz, get_settings().schedule_min_interval_minutes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _own(db, schedule_id: uuid.UUID, user: User, *, write: bool) -> Schedule:
    s = await db.get(Schedule, schedule_id)
    can_see = s is not None and (s.owner_id == user.id or user.role in PRIVILEGED)
    if not can_see or (write and s.owner_id != user.id and user.role != Role.ADMIN.value):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return s


@router.post("/schedules", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED)
async def create_schedule(body: ScheduleCreate, request: Request, db: DB, user: User = CanRun):
    _check_params(body.workflow, body.params)
    _check_spec(body.cron, body.timezone)
    s = Schedule(**body.model_dump(), owner_id=user.id, next_run_at=next_fire(body.cron, body.timezone, utcnow()))
    db.add(s)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"A schedule named '{body.name}' already exists")
    record_audit(db, action="schedule.create", actor_id=user.id, resource_type="schedule", resource_id=str(s.id),
                 ip_address=client_ip(request), details=body.model_dump())
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/schedules", response_model=list[ScheduleRead])
async def list_schedules(db: DB, user: User = CanRun):
    stmt = select(Schedule).order_by(Schedule.name)
    if user.role not in PRIVILEGED:
        stmt = stmt.where(Schedule.owner_id == user.id)
    return (await db.scalars(stmt)).all()


@router.get("/schedules/{schedule_id}", response_model=ScheduleRead)
async def get_schedule(schedule_id: uuid.UUID, db: DB, user: User = CanRun):
    return await _own(db, schedule_id, user, write=False)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleRead)
async def update_schedule(schedule_id: uuid.UUID, body: ScheduleUpdate, request: Request, db: DB, user: User = CanRun):
    s = await _own(db, schedule_id, user, write=True)
    changes = body.model_dump(exclude_unset=True)
    cron, tz = changes.get("cron", s.cron), changes.get("timezone", s.timezone)
    if "params" in changes:
        _check_params(s.workflow, changes["params"])
    if {"cron", "timezone"} & changes.keys():
        _check_spec(cron, tz)
    before = {k: getattr(s, k) for k in changes}
    was_enabled = s.enabled
    for k, v in changes.items():
        setattr(s, k, v)
    if {"cron", "timezone"} & changes.keys() or (s.enabled and not was_enabled):
        s.next_run_at = next_fire(cron, tz, utcnow())      # never fire for the past
    record_audit(db, action="schedule.update", actor_id=user.id, resource_type="schedule", resource_id=str(s.id),
                 ip_address=client_ip(request), details={"from": before, "to": changes})
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: uuid.UUID, request: Request, db: DB, user: User = CanRun):
    s = await _own(db, schedule_id, user, write=True)
    await db.execute(update(Task).where(Task.schedule_id == s.id).values(schedule_id=None))   # keep the history
    record_audit(db, action="schedule.delete", actor_id=user.id, resource_type="schedule", resource_id=str(s.id),
                 ip_address=client_ip(request), details={"name": s.name})
    await db.delete(s)
    await db.commit()


@router.post("/schedules/{schedule_id}/run-now", response_model=WorkflowRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_now(schedule_id: uuid.UUID, request: Request, db: DB, user: User = CanRun,
                  runner: TaskRunner = Depends(get_runner), llm: LLMClient = Depends(lenient_llm)):
    """Run the schedule's workflow immediately (as the schedule's owner), outside its normal timetable."""
    s = await _own(db, schedule_id, user, write=True)
    owner = await db.get(User, s.owner_id)
    task = await create_workflow_task(db, owner, s.workflow, s.params, schedule=s)
    s.last_run_at, s.last_task_id = utcnow(), task.id
    record_audit(db, action="schedule.run_now", actor_id=user.id, resource_type="schedule", resource_id=str(s.id),
                 ip_address=client_ip(request), details={"task_id": str(task.id)})
    await db.commit()
    if await claim_planned_task(db, task.id, actor_id=user.id, ip=client_ip(request), via=f"run-now:{s.name}"):
        runner.start(task.id, llm)
    return WorkflowRunResponse(task_id=task.id, workflow=s.workflow, status="running", steps=len(task.plan["steps"]))


@router.get("/schedules/{schedule_id}/runs", response_model=list[TaskRead])
async def schedule_runs(schedule_id: uuid.UUID, db: DB, user: User = CanRun, limit: int = 50):
    s = await _own(db, schedule_id, user, write=False)
    return (await db.scalars(select(Task).where(Task.schedule_id == s.id).order_by(Task.created_at.desc())
                             .limit(max(1, min(limit, 200))))).all()
