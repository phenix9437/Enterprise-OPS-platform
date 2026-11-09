"""Celery wiring. The first group is fast and always runs. The second group starts a REAL Redis, a REAL Celery
worker process and uses REAL PostgreSQL: run it with `python -m scripts.test_on_postgres`."""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from app.agents.runner import TaskRunner
from app.core.config import get_settings
from app.models.chat import Task
from app.models.schedule import Schedule
from app.models.user import utcnow
from app.worker.celery_app import celery_app, make_celery_app, make_enqueue
from app.worker.queue_runner import QueueRunner
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import ScriptedLLM
from tests.test_execution import events, resume, types, users

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
BACKEND = Path(__file__).resolve().parents[1]


class FakeCelery:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_task(self, name, args=None, **kw):
        if self.fail:
            raise ConnectionError("redis is down")
        self.sent.append((name, args))


# ------------------------------------------------------------------ fast unit tests ----
def test_celery_is_configured_for_at_least_once_delivery_and_registers_the_tasks():
    import app.worker.tasks  # noqa: F401  (a worker imports this at start-up; registers the tasks)

    c = celery_app.conf
    assert c.task_acks_late and c.task_reject_on_worker_lost and c.worker_prefetch_multiplier == 1
    assert c.task_ignore_result and c.task_serializer == "json"
    assert c.beat_schedule["ops-tick"]["task"] == "ops.tick"
    assert "ops.run_task" in celery_app.tasks and "ops.tick" in celery_app.tasks


async def test_in_celery_mode_the_api_only_enqueues_and_a_worker_does_the_run(app, client, gateway, runner):
    h = await users(client)
    fake = FakeCelery()
    queue_runner = QueueRunner(app.state.session_factory, gateway, get_settings(), celery=fake)
    app.state.task_runner = queue_runner

    r = await client.post("/api/v1/workflows/operations_digest/run", json={}, headers=h["op"])
    assert r.status_code == 202 and r.json()["status"] == "running"
    tid = r.json()["task_id"]
    assert fake.sent == [("ops.run_task", [tid])]                                       # exactly one message, just the id
    t = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert t["status"] == "running" and t["current_step"] is None                       # the API did not run anything itself

    await runner.run(uuid.UUID(tid), ScriptedLLM())                                     # "the worker" picks it up
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"


async def test_decisions_and_expiry_also_go_through_the_queue(app, client, gateway, runner):
    h = await users(client)
    fake = FakeCelery()
    app.state.task_runner = QueueRunner(app.state.session_factory, gateway, get_settings(), celery=fake)
    r = await client.post("/api/v1/workflows/operations_digest/run", json={"params": {"delivery": "email", "recipient": "b@mycompany.example"}}, headers=h["op"])
    tid = r.json()["task_id"]
    await runner.run(uuid.UUID(tid), ScriptedLLM())                                     # worker runs it up to the approval gate
    fake.sent.clear()
    assert (await resume(client, h["mgr"], tid)).status_code == 200
    assert fake.sent == [("ops.run_task", [tid])]                                       # the resume is just another poke


async def test_a_dead_broker_never_loses_the_task_or_breaks_the_api(app, client, gateway, runner):
    h = await users(client)
    app.state.task_runner = QueueRunner(app.state.session_factory, gateway, get_settings(), celery=FakeCelery(fail=True))
    r = await client.post("/api/v1/workflows/operations_digest/run", json={}, headers=h["op"])
    assert r.status_code == 202                                                          # the user is not shown a 500
    tid = r.json()["task_id"]
    async with app.state.session_factory() as db:
        t = await db.get(Task, uuid.UUID(tid))
    assert t.status == "running" and t.pending_dispatch == {"resume": None}              # it is parked in the outbox
    async with app.state.session_factory() as db:                                        # ...and the reaper recovers it later
        await db.execute(update(Task).where(Task.id == t.id).values(updated_at=utcnow() - timedelta(minutes=10)))
        await db.commit()
    assert await runner.reap_stale_tasks(ScriptedLLM()) == [t.id]
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"


def test_enqueue_helper_swallows_broker_errors():
    make_enqueue(FakeCelery(fail=True))(uuid.uuid4())      # must not raise


# ------------------------------------------------------------------ real Redis + real worker + real PostgreSQL ----
needs_stack = pytest.mark.skipif(not (TEST_DATABASE_URL and TEST_REDIS_URL), reason="needs real PostgreSQL + Redis (scripts/test_on_postgres.py)")


@pytest.fixture(scope="module")
def worker():
    if not (TEST_DATABASE_URL and TEST_REDIS_URL):
        pytest.skip("needs real PostgreSQL + Redis")
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "BUSINESS_DATABASE_URL": TEST_DATABASE_URL,
           "REDIS_URL": TEST_REDIS_URL, "CELERY_ENABLED": "true", "OPENAI_API_KEY": "", "SEED_DEMO_DATA": "true",
           "GMAIL_BACKEND": "mock", "DRIVE_BACKEND": "mock", "SCHEDULE_TICK_SECONDS": "3600", "ENVIRONMENT": "test"}
    proc = subprocess.Popen([sys.executable, "-m", "celery", "-A", "app.worker.celery_app", "worker",
                             f"--pool={os.environ.get('TEST_CELERY_POOL', 'solo')}", "-c", "1", "-l", "INFO", "-Q", "ops"], cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
    proc.log_path = log.name
    yield proc
    proc.terminate()
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        proc.kill()


def worker_log(worker) -> str:
    return Path(worker.log_path).read_text()[-12000:]


async def wait_for(client, headers, tid, statuses, worker, timeout=150):
    end = time.time() + timeout
    while time.time() < end:
        t = (await client.get(f"/api/v1/tasks/{tid}", headers=headers)).json()
        if t["status"] in statuses:
            return t
        assert worker.poll() is None, f"the worker process died:\n{worker_log(worker)}"
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out waiting for {statuses}; last status {t['status']}.\nworker log:\n{worker_log(worker)}")


def api_side(app, gateway):
    """The API process: it only ENQUEUES (to the real Redis); a different OS process runs the work."""
    test_celery = make_celery_app(get_settings().model_copy(update={"redis_url": TEST_REDIS_URL}), name="test-api")
    app.state.task_runner = QueueRunner(app.state.session_factory, gateway, get_settings(), celery=test_celery)
    return test_celery


@needs_stack
async def test_real_worker_runs_a_workflow_from_a_real_redis_queue(app, client, gateway, worker):
    h = await users(client)
    api_side(app, gateway)
    r = await client.post("/api/v1/workflows/operations_digest/run", json={}, headers=h["op"])
    assert r.status_code == 202
    t = await wait_for(client, h["op"], r.json()["task_id"], {"completed", "failed"}, worker)
    assert t["status"] == "completed", (t["error"], t["result"], worker_log(worker))
    assert "OPERATIONS DIGEST" in t["result"] and "Overdue invoices" in t["result"] and t["llm_calls"] == 0
    evs = types(await events(client, h["op"], t["id"]))
    assert evs[0] == "task_started" and evs[-1] == "task_completed"


@needs_stack
async def test_an_approval_pauses_in_the_worker_and_resumes_after_the_api_decides(app, client, gateway, worker):
    h = await users(client)
    api_side(app, gateway)
    r = await client.post("/api/v1/workflows/operations_digest/run", headers=h["op"],
                          json={"params": {"delivery": "email", "recipient": "boss@mycompany.example"}})
    tid = r.json()["task_id"]
    paused = await wait_for(client, h["op"], tid, {"awaiting_approval", "failed"}, worker)
    assert paused["status"] == "awaiting_approval" and paused["pending_approval"]["approval_id"]

    assert (await resume(client, h["mgr"], tid)).status_code == 200                       # decision made in the API process
    done = await wait_for(client, h["op"], tid, {"completed", "failed", "needs_attention"}, worker)   # resumed in the worker
    assert done["status"] == "completed", done
    sends = (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()
    assert len(sends) == 1 and sends[0]["status"] == "succeeded" and sends[0]["approval_id"]


@needs_stack
async def test_the_tick_task_fires_a_due_schedule_end_to_end(app, client, gateway, worker):
    h = await users(client)
    celery = api_side(app, gateway)
    r = await client.post("/api/v1/schedules", headers=h["op"], json={
        "name": "real-schedule", "workflow": "operations_digest", "cron": "0 9 * * 1-5", "timezone": "Asia/Kolkata"})
    sid = r.json()["id"]
    async with app.state.session_factory() as db:
        await db.execute(update(Schedule).where(Schedule.id == uuid.UUID(sid)).values(next_run_at=utcnow() - timedelta(minutes=1)))
        await db.commit()

    celery.send_task("ops.tick", queue="ops")                                              # what Celery beat would send
    end = time.time() + 150
    run = None
    while time.time() < end and not run:
        runs = (await client.get(f"/api/v1/schedules/{sid}/runs", headers=h["op"])).json()
        run = runs[0] if runs else None
        await asyncio.sleep(0.5)
    assert run, f"the schedule never fired.\n{worker_log(worker)}"
    t = await wait_for(client, h["op"], run["id"], {"completed", "failed"}, worker)
    assert t["status"] == "completed" and t["schedule_id"] == sid and "OPERATIONS DIGEST" in t["result"]
