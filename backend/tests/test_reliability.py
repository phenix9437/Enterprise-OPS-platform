"""Lease, outbox and reaper: what keeps tasks correct when workers die, messages are lost or delivered twice."""
import asyncio
import uuid
from datetime import timedelta

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, update

from app.agents.runner import TaskRunner
from app.models.approval import Approval
from app.models.chat import Task
from app.models.user import utcnow
from app.services.approvals import as_utc
from app.services.task_start import claim_planned_task
from tests.fakes import ScriptedLLM
from tests.test_execution import events, resume, types, users


async def planned_digest(client, h, **params):
    r = await client.post("/api/v1/workflows/operations_digest/run", json={"params": params, "execute": False}, headers=h["op"])
    assert r.status_code == 202 and r.json()["status"] == "planned"
    return uuid.UUID(r.json()["task_id"])


async def claim(app, tid):
    async with app.state.session_factory() as db:
        assert await claim_planned_task(db, tid, actor_id=None, via="test")


async def row(app, tid) -> Task:
    async with app.state.session_factory() as db:
        return await db.get(Task, tid)


async def patch_task(app, tid, **values):
    async with app.state.session_factory() as db:
        await db.execute(update(Task).where(Task.id == tid).values(**values))
        await db.commit()


def hang_calls(gateway, server="business_db"):
    conn = gateway._connections[server]
    real, entered, release = conn.call_tool, asyncio.Event(), asyncio.Event()
    state = {"calls": 0}

    async def hanging(name, args):
        state["calls"] += 1
        entered.set()
        await release.wait()
        return await real(name, args)

    conn.call_tool = hanging
    return entered, release, state, lambda: setattr(conn, "call_tool", real)


def fresh_runner(app, gateway, runner, **settings):
    return TaskRunner(app.state.session_factory, gateway, runner.checkpointer, runner.settings.model_copy(update=settings))


# ------------------------------------------------------------------ lease ----
async def test_a_live_lease_blocks_a_second_worker_but_an_expired_one_can_be_taken_over(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)
    await patch_task(app, tid, lock_owner="worker-A", lock_expires_at=utcnow() + timedelta(minutes=5))

    await runner.run(tid, ScriptedLLM())
    assert (await row(app, tid)).status == "running" and await events(client, h["op"], str(tid)) == []   # B did nothing

    await patch_task(app, tid, lock_expires_at=utcnow() - timedelta(seconds=1))                          # A died; lease lapsed
    await runner.run(tid, ScriptedLLM())
    t = await row(app, tid)
    assert t.status == "completed" and t.lock_owner is None and t.pending_dispatch is None


async def test_duplicate_deliveries_never_double_run(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)
    await runner.run(tid, ScriptedLLM())
    n = len(await events(client, h["op"], str(tid)))
    await runner.run(tid, ScriptedLLM())                                   # the broker delivered the message again
    assert len(await events(client, h["op"], str(tid))) == n and (await row(app, tid)).status == "completed"


async def test_two_workers_racing_for_one_task_run_it_once(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)
    entered, release, state, restore = hang_calls(gateway)
    other = fresh_runner(app, gateway, runner)
    a = asyncio.create_task(runner.run(tid, ScriptedLLM()))
    await asyncio.wait_for(entered.wait(), 5)                              # A is mid-run, holding the lease
    await other.run(tid, ScriptedLLM())                                    # B arrives: must walk away
    release.set()
    await asyncio.wait_for(a, 10)
    restore()
    assert state["calls"] == 2                                             # the digest's 2 data calls, made ONCE (not 4)
    assert (await row(app, tid)).status == "completed"


async def test_heartbeat_keeps_the_lease_alive_for_a_long_run(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)
    short = fresh_runner(app, gateway, runner, worker_lease_seconds=0.6)    # heartbeat every 0.2s
    entered, release, _, restore = hang_calls(gateway)
    run = asyncio.create_task(short.run(tid, ScriptedLLM()))
    await asyncio.wait_for(entered.wait(), 5)
    first = as_utc((await row(app, tid)).lock_expires_at)
    await asyncio.sleep(1.0)                                               # longer than the lease itself
    t = await row(app, tid)
    assert as_utc(t.lock_expires_at) > first + timedelta(seconds=0.4) and as_utc(t.lock_expires_at) > utcnow()
    release.set()
    await asyncio.wait_for(run, 10)
    restore()
    assert (await row(app, tid)).status == "completed"


async def test_a_worker_that_loses_its_lease_stops_instead_of_running_alongside_the_new_owner(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)
    short = fresh_runner(app, gateway, runner, worker_lease_seconds=0.6)
    entered, release, _, restore = hang_calls(gateway)
    run = asyncio.create_task(short.run(tid, ScriptedLLM()))
    await asyncio.wait_for(entered.wait(), 5)
    await patch_task(app, tid, lock_owner="thief", lock_expires_at=utcnow() + timedelta(minutes=5))
    await asyncio.sleep(0.6)                                               # the next heartbeat notices
    assert run.done()
    release.set()
    restore()
    t = await row(app, tid)
    assert t.status == "running" and t.lock_owner == "thief"                # the old worker did not finish or clobber it


async def test_graceful_shutdown_releases_the_lease_immediately(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)
    entered, release, _, restore = hang_calls(gateway)
    runner.start(tid, ScriptedLLM())
    await asyncio.wait_for(entered.wait(), 5)
    assert (await row(app, tid)).lock_owner == runner.owner
    await runner.shutdown()
    restore()
    t = await row(app, tid)
    assert t.lock_owner is None and t.status == "running"                   # free for another worker to continue right away


# ------------------------------------------------------------------ outbox & reaper ----
async def test_reaper_redispatches_a_task_whose_queue_message_was_lost(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h)
    await claim(app, tid)                                                   # status=running + outbox written... message "lost"
    assert (await row(app, tid)).pending_dispatch == {"resume": None}
    assert await runner.reap_stale_tasks(ScriptedLLM()) == []               # too fresh: give the real message time to arrive

    await patch_task(app, tid, updated_at=utcnow() - timedelta(minutes=10))
    assert await runner.reap_stale_tasks(ScriptedLLM()) == [tid]
    await runner.wait_idle()
    assert (await row(app, tid)).status == "completed"
    assert await runner.reap_stale_tasks(ScriptedLLM()) == []               # finished tasks are left alone


async def test_an_approval_decision_survives_a_lost_message(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_digest(client, h, delivery="email", recipient="boss@mycompany.example")
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 202
    await runner.wait_idle()
    assert (await row(app, tid)).status == "awaiting_approval"

    real_start, real_dispatch = runner.start, runner.dispatch
    runner.start = lambda *a, **k: None                                     # the queue swallows the "resume" message
    runner.dispatch = lambda *a, **k: None
    assert (await resume(client, h["mgr"], str(tid))).status_code == 200    # the human's decision is recorded...
    runner.start, runner.dispatch = real_start, real_dispatch
    await runner.wait_idle()

    t = await row(app, tid)
    assert t.status == "running" and t.pending_dispatch["resume"]["approved"] is True       # ...and parked in the outbox
    assert (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json() == []   # nothing sent yet

    await patch_task(app, tid, updated_at=utcnow() - timedelta(minutes=10))
    assert await runner.reap_stale_tasks(ScriptedLLM()) == [tid]
    await runner.wait_idle()
    t = await row(app, tid)
    assert t.status == "completed" and t.pending_dispatch is None
    sends = (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()
    assert len(sends) == 1 and sends[0]["status"] == "succeeded"             # the approved email went out exactly once
    async with app.state.session_factory() as db:
        assert len((await db.scalars(select(Approval).where(Approval.task_id == tid))).all()) == 1   # no duplicate approval request
    assert types(await events(client, h["op"], str(tid))).count("approval_requested") == 1
