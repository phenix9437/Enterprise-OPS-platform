import logging
import uuid

from app.worker.celery_app import celery_app

log = logging.getLogger("app.worker")


@celery_app.task(name="ops.run_task", acks_late=True, reject_on_worker_lost=True)
def run_task(task_id: str) -> None:
    """Run (or resume, or continue) one task. WHAT to do is decided from the database and the checkpoint, not
    from this message, so a duplicate or late delivery is harmless."""
    from app.worker.runtime import get_runtime

    rt = get_runtime()
    rt.call(rt.runner.run(uuid.UUID(task_id), rt.llm))


@celery_app.task(name="ops.tick")
def tick() -> dict:
    """Beat calls this every SCHEDULE_TICK_SECONDS: expire stale approvals, fire due schedules, re-dispatch lost tasks."""
    from app.services.maintenance import run_maintenance
    from app.worker.runtime import get_runtime

    rt = get_runtime()
    result = rt.call(run_maintenance(rt.sf, rt.runner, rt.llm))
    log.info("tick: %s", result)
    return result
