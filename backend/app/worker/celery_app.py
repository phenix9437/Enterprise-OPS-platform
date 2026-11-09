"""Celery configuration. Start with:

    celery -A app.worker.celery_app worker -l info -c 2      # runs tasks
    celery -A app.worker.celery_app beat   -l info           # ticks every SCHEDULE_TICK_SECONDS

Redis is only a MAILBOX here. All real state (task status, outbox, lease, checkpoints, schedules) lives in
PostgreSQL, so losing a queue message or restarting Redis never loses work: the reaper re-sends it.
"""
import logging
import uuid

from celery import Celery

from app.core.config import Settings, get_settings

log = logging.getLogger("app.worker")


def make_celery_app(settings: Settings, name: str = "ops") -> Celery:
    app = Celery(name, broker=settings.redis_url, include=["app.worker.tasks"])
    app.conf.update(
        task_default_queue="ops",
        task_serializer="json", accept_content=["json"], result_serializer="json",
        task_ignore_result=True,                       # results live in the database, not in Redis
        task_acks_late=True,                           # acknowledge AFTER the run, so a crash re-delivers it
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,                  # long tasks: don't hoard messages
        broker_connection_retry_on_startup=True,
        # Celery would replace sys.stderr with a logging proxy that has no file descriptor, and the MCP stdio
        # transport needs a real one to spawn the tool servers. (Found by the real-worker integration test.)
        worker_redirect_stdouts=False,
        broker_transport_options={"visibility_timeout": 3600},
        timezone="UTC", enable_utc=True,
        beat_schedule={"ops-tick": {"task": "ops.tick", "schedule": float(settings.schedule_tick_seconds)}},
    )
    return app


celery_app = make_celery_app(get_settings())


def make_enqueue(app: Celery):
    """Returns dispatch(task_id, llm). Never raises: if the broker is down the task simply stays 'running'
    with its outbox entry, and the reaper re-dispatches it later."""
    def enqueue(task_id: uuid.UUID, llm=None) -> None:
        try:
            app.send_task("ops.run_task", args=[str(task_id)], queue="ops")
        except Exception:
            log.exception("could not enqueue task %s; the reaper will retry", task_id)
    return enqueue
