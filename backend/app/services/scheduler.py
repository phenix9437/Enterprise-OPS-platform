"""Cron scheduling. The `schedules` table is the source of truth; a periodic tick (in-process loop or
Celery beat) calls fire_due_schedules(). Guarantees:
  * a schedule slot produces AT MOST ONE task (unique (schedule_id, scheduled_for)), even with several tickers
  * missed slots (downtime) are coalesced into ONE run, not replayed
  * overlap_policy='skip' never starts a run while the previous one is still active (e.g. awaiting approval)
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.models.chat import Task, TaskStatus
from app.models.schedule import Schedule
from app.models.user import Role, User, utcnow
from app.services.approvals import as_utc
from app.services.audit import record_audit
from app.services.task_start import claim_planned_task
from app.services.workflow_runs import create_workflow_task

log = logging.getLogger("app.scheduler")
CAN_RUN = {Role.ADMIN.value, Role.MANAGER.value, Role.OPERATOR.value}
ACTIVE = [TaskStatus.PLANNED.value, TaskStatus.RUNNING.value, TaskStatus.AWAITING_APPROVAL.value]


def validate_schedule_spec(cron: str, tz: str, min_interval_minutes: int) -> None:
    """Raises ValueError with a human-readable reason."""
    if len(cron.split()) != 5 or not croniter.is_valid(cron):
        raise ValueError("cron must be a valid 5-field expression, e.g. '0 9 * * 1-5'")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone '{tz}' (use an IANA name such as 'Asia/Kolkata' or 'UTC')") from None
    it = croniter(cron, datetime(2026, 1, 1, tzinfo=ZoneInfo(tz)))
    fires = [it.get_next(datetime) for _ in range(12)]
    gap = min((b - a) for a, b in zip(fires, fires[1:]))
    if gap < timedelta(minutes=min_interval_minutes):
        raise ValueError(f"this would run every {int(gap.total_seconds() // 60)} minute(s); the minimum interval is {min_interval_minutes}")


def next_fire(cron: str, tz: str, after: datetime) -> datetime:
    local = as_utc(after).astimezone(ZoneInfo(tz))
    return croniter(cron, local).get_next(datetime).astimezone(timezone.utc)


async def fire_due_schedules(sf, dispatch: Callable, llm, now: datetime | None = None, limit: int = 50) -> list[dict]:
    now = now or utcnow()
    async with sf() as db:
        ids = (await db.scalars(select(Schedule.id).where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                                .order_by(Schedule.next_run_at).limit(limit))).all()
    fired = []
    for sid in ids:
        outcome = await _fire_one(sf, dispatch, llm, sid, now)
        if outcome:
            fired.append(outcome)
    return fired


async def _fire_one(sf, dispatch, llm, schedule_id: uuid.UUID, now: datetime) -> dict | None:
    async with sf() as db:
        sched = await db.get(Schedule, schedule_id)
        if sched is None or not sched.enabled or sched.next_run_at is None or as_utc(sched.next_run_at) > now:
            return None
        current_next = sched.next_run_at   # raw DB value; read NOW because a rollback expires the object
        slot = as_utc(sched.next_run_at)
        nxt = next_fire(sched.cron, sched.timezone, max(now, slot))   # coalesce: skip every slot we missed
        owner = await db.get(User, sched.owner_id)

        def disable(reason: str):
            sched.enabled = False
            record_audit(db, action="schedule.disabled", resource_type="schedule", resource_id=str(sched.id),
                         status="failure", details={"reason": reason})
            log.warning("schedule %s disabled: %s", sched.name, reason)

        if owner is None or not owner.is_active or owner.role not in CAN_RUN:
            disable("its owner can no longer run workflows")
            await db.commit()
            return None

        if sched.overlap_policy == "skip":
            active = await db.scalar(select(func.count()).select_from(Task).where(
                Task.schedule_id == sched.id, Task.status.in_(ACTIVE)))
            if active:
                sched.next_run_at = nxt
                record_audit(db, action="schedule.skipped", actor_id=owner.id, resource_type="schedule",
                             resource_id=str(sched.id), details={"reason": "previous run still active", "slot": slot.isoformat()})
                await db.commit()
                return {"schedule": sched.name, "skipped": "previous run still active"}
        try:
            task = await create_workflow_task(db, owner, sched.workflow, sched.params, schedule=sched, scheduled_for=slot)
        except (KeyError, ValidationError) as exc:
            disable(f"its workflow or parameters are no longer valid ({type(exc).__name__})")
            await db.commit()
            return None
        except IntegrityError:   # another ticker already created this slot's task
            await db.rollback()
            await db.execute(update(Schedule).where(Schedule.id == schedule_id, Schedule.next_run_at == current_next)
                             .values(next_run_at=nxt))
            await db.commit()
            return None
        sched.next_run_at, sched.last_run_at, sched.last_task_id = nxt, now, task.id
        record_audit(db, action="schedule.fired", actor_id=owner.id, resource_type="schedule", resource_id=str(sched.id),
                     details={"task_id": str(task.id), "slot": slot.isoformat()})
        await db.commit()
        task_id, auto, name = task.id, sched.auto_execute, sched.name

    if auto:
        async with sf() as db:
            if await claim_planned_task(db, task_id, actor_id=None, via=f"schedule:{name}"):
                dispatch(task_id, llm)
    return {"schedule": name, "task_id": str(task_id), "executed": auto}
