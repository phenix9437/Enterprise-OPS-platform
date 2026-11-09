import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Task, TaskStatus
from app.models.user import utcnow
from app.services.audit import record_audit


async def claim_planned_task(db: AsyncSession, task_id: uuid.UUID, *, actor_id: uuid.UUID | None, ip: str | None = None,
                             via: str = "api") -> bool:
    """planned -> running, atomically, with the OUTBOX entry written in the same commit.
    Returns False if another request already claimed it. The caller then pokes the runner."""
    res = await db.execute(update(Task).where(Task.id == task_id, Task.status == TaskStatus.PLANNED.value).values(
        status=TaskStatus.RUNNING.value, started_at=utcnow(), finished_at=None, error=None, result=None,
        pending_dispatch={"resume": None}))
    if res.rowcount != 1:
        await db.rollback()
        return False
    record_audit(db, action="task.execute", actor_id=actor_id, resource_type="task", resource_id=str(task_id),
                 ip_address=ip, details={"via": via})
    await db.commit()
    return True
