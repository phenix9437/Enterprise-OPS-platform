import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update

from app.models.chat import Task
from app.models.schedule import Schedule
from app.models.user import utcnow
from app.services.maintenance import run_maintenance
from app.services.scheduler import fire_due_schedules, next_fire, validate_schedule_spec
from tests.fakes import ScriptedLLM
from tests.test_execution import events, resume, types, users


def utc(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def mk(client, headers, **kw):
    body = {"name": "daily-digest", "workflow": "operations_digest", "params": {}, "cron": "0 9 * * 1-5",
            "timezone": "Asia/Kolkata", **kw}
    return await client.post("/api/v1/schedules", json=body, headers=headers)


async def make_due(app, sid, minutes=-5):
    sid = uuid.UUID(str(sid))
    async with app.state.session_factory() as db:
        await db.execute(update(Schedule).where(Schedule.id == sid).values(next_run_at=utcnow() + timedelta(minutes=minutes)))
        await db.commit()


async def task_count(app, sid):
    sid = uuid.UUID(str(sid))
    async with app.state.session_factory() as db:
        return await db.scalar(select(func.count()).select_from(Task).where(Task.schedule_id == sid))


async def fire(app, runner, **kw):
    out = await fire_due_schedules(app.state.session_factory, runner.dispatch, ScriptedLLM(), **kw)
    await runner.wait_idle()
    return out


# ------------------------------------------------------------------ cron maths ----
def test_next_fire_respects_timezone_and_daylight_saving():
    ist = next_fire("0 9 * * *", "Asia/Kolkata", datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc))
    assert ist == datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)                   # 09:00 IST = 03:30 UTC
    # New York clocks spring forward on 2026-03-08: the same 09:00 local is 14:00Z before and 13:00Z after
    before = next_fire("0 9 * * *", "America/New_York", datetime(2026, 3, 6, 20, 0, tzinfo=timezone.utc))
    after = next_fire("0 9 * * *", "America/New_York", datetime(2026, 3, 7, 20, 0, tzinfo=timezone.utc))
    assert (before.hour, after.hour) == (14, 13) and after.day == 8


@pytest.mark.parametrize("cron,tz,ok", [
    ("0 9 * * 1-5", "UTC", True), ("*/5 * * * *", "UTC", True), ("*/1 * * * *", "UTC", False),
    ("* * * * *", "UTC", False), ("0,2 9 * * *", "UTC", False),            # two runs 2 minutes apart
    ("0 0 9 * * *", "UTC", False), ("not a cron", "UTC", False), ("0 9 * * *", "Mars/Olympus", False),
])
def test_schedule_validation(cron, tz, ok):
    if ok:
        validate_schedule_spec(cron, tz, 5)
    else:
        with pytest.raises(ValueError):
            validate_schedule_spec(cron, tz, 5)


# ------------------------------------------------------------------ CRUD & permissions ----
async def test_create_validate_and_permissions(app, client, gateway, runner):
    h = await users(client)
    r = await mk(client, h["op"])
    assert r.status_code == 201, r.text
    s = r.json()
    nxt = utc(s["next_run_at"])
    local = nxt.astimezone(ZoneInfo("Asia/Kolkata"))
    assert nxt > datetime.now(timezone.utc) and (local.hour, local.minute) == (9, 0) and local.weekday() < 5
    assert s["owner_id"] and s["enabled"] and s["overlap_policy"] == "skip"

    assert (await mk(client, h["op"])).status_code == 409                                  # duplicate name
    assert (await mk(client, h["vic"], name="v")).status_code == 403
    assert (await mk(client, h["op"], name="a", cron="* * * * *")).status_code == 422
    assert (await mk(client, h["op"], name="b", timezone="Nowhere/Land")).status_code == 422
    assert (await mk(client, h["op"], name="c", workflow="nope")).status_code == 404
    assert (await mk(client, h["op"], name="d", params={"delivery": "email"})).status_code == 422   # params are checked up front

    assert [x["name"] for x in (await client.get("/api/v1/schedules", headers=h["op"])).json()] == ["daily-digest"]
    assert (await client.get("/api/v1/schedules", headers=h["op2"])).json() == []
    assert len((await client.get("/api/v1/schedules", headers=h["mgr"])).json()) == 1              # reviewers see all
    sid = s["id"]
    assert (await client.get(f"/api/v1/schedules/{sid}", headers=h["op2"])).status_code == 404
    assert (await client.patch(f"/api/v1/schedules/{sid}", json={"enabled": False}, headers=h["op2"])).status_code == 404
    assert (await client.patch(f"/api/v1/schedules/{sid}", json={"enabled": False}, headers=h["mgr"])).status_code == 404   # not their own
    assert (await client.patch(f"/api/v1/schedules/{sid}", json={"description": "x"}, headers=h["admin"])).status_code == 200


async def test_update_recomputes_next_run_and_enabling_never_fires_for_the_past(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"])).json()["id"]
    r = await client.patch(f"/api/v1/schedules/{sid}", json={"cron": "30 18 * * *", "timezone": "UTC"}, headers=h["op"])
    assert r.status_code == 200
    n = utc(r.json()["next_run_at"])
    assert (n.hour, n.minute) == (18, 30) and n > datetime.now(timezone.utc)
    await client.patch(f"/api/v1/schedules/{sid}", json={"enabled": False}, headers=h["op"])
    await make_due(app, sid, minutes=-60 * 24)                                    # stale next_run while disabled
    assert await fire(app, runner) == []                                           # disabled: never fires
    r = await client.patch(f"/api/v1/schedules/{sid}", json={"enabled": True}, headers=h["op"])
    assert utc(r.json()["next_run_at"]) > datetime.now(timezone.utc)               # re-enabled: next FUTURE slot, no catch-up run
    assert (await client.patch(f"/api/v1/schedules/{sid}", json={"cron": "bad"}, headers=h["op"])).status_code == 422
    assert (await client.patch(f"/api/v1/schedules/{sid}", json={"params": {"min_days_overdue": -1}}, headers=h["op"])).status_code == 422


# ------------------------------------------------------------------ firing ----
async def test_due_schedule_creates_and_runs_a_task_then_moves_on(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"])).json()["id"]
    await make_due(app, sid)
    out = await fire(app, runner)
    assert len(out) == 1 and out[0]["executed"] is True

    s = (await client.get(f"/api/v1/schedules/{sid}", headers=h["op"])).json()
    assert utc(s["next_run_at"]) > datetime.now(timezone.utc) and s["last_task_id"] == out[0]["task_id"]
    runs = (await client.get(f"/api/v1/schedules/{sid}/runs", headers=h["op"])).json()
    assert len(runs) == 1 and runs[0]["status"] == "completed" and runs[0]["workflow"] == "operations_digest"
    assert runs[0]["schedule_id"] == sid and "OPERATIONS DIGEST" in runs[0]["result"] and runs[0]["llm_calls"] == 0
    acts = {(l["action"], l["status"]) for l in (await client.get("/api/v1/audit-logs?limit=300", headers=h["admin"])).json()}
    assert ("schedule.fired", "success") in acts and ("schedule.create", "success") in acts
    assert await fire(app, runner) == []                                           # nothing due any more


async def test_a_slot_can_never_produce_two_tasks(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"])).json()["id"]
    await make_due(app, sid)
    async with app.state.session_factory() as db:
        slot = (await db.get(Schedule, uuid.UUID(sid))).next_run_at
    assert len(await fire(app, runner)) == 1
    async with app.state.session_factory() as db:        # a second ticker still sees the OLD slot (stale read)
        await db.execute(update(Schedule).where(Schedule.id == uuid.UUID(sid)).values(next_run_at=slot))
        await db.commit()
    assert await fire(app, runner) == []
    assert await task_count(app, uuid.UUID(sid)) == 1
    s = (await client.get(f"/api/v1/schedules/{sid}", headers=h["op"])).json()
    assert utc(s["next_run_at"]) > datetime.now(timezone.utc)                      # and it still moved on


async def test_missed_runs_after_downtime_are_coalesced_into_one(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"], cron="0 * * * *", timezone="UTC")).json()["id"]
    await make_due(app, sid, minutes=-60 * 72)                                     # the system was down for 3 days
    assert len(await fire(app, runner)) == 1                                       # ONE catch-up run, not 72
    assert await task_count(app, uuid.UUID(sid)) == 1
    assert utc((await client.get(f"/api/v1/schedules/{sid}", headers=h["op"])).json()["next_run_at"]) > datetime.now(timezone.utc)


async def test_overlap_policy_skip_vs_allow(app, client, gateway, runner):
    h = await users(client)
    params = {"delivery": "email", "recipient": "boss@mycompany.example"}      # this run waits for an approval
    sid = (await mk(client, h["op"], params=params)).json()["id"]
    uid = uuid.UUID(sid)
    assert (await client.post(f"/api/v1/schedules/{sid}/run-now", headers=h["op"])).status_code == 202
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/schedules/{sid}/runs", headers=h["op"])).json()[0]["status"] == "awaiting_approval"

    await make_due(app, sid)
    out = await fire(app, runner)
    assert out == [{"schedule": "daily-digest", "skipped": "previous run still active"}]
    assert await task_count(app, uid) == 1
    assert ("schedule.skipped", "success") in {(l["action"], l["status"]) for l in (await client.get("/api/v1/audit-logs?limit=300", headers=h["admin"])).json()}

    await client.patch(f"/api/v1/schedules/{sid}", json={"overlap_policy": "allow"}, headers=h["op"])
    await make_due(app, sid)
    assert len(await fire(app, runner)) == 1 and await task_count(app, uid) == 2


async def test_auto_execute_false_only_prepares_the_task(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"], auto_execute=False)).json()["id"]
    await make_due(app, sid)
    out = await fire(app, runner)
    assert out[0]["executed"] is False
    run = (await client.get(f"/api/v1/schedules/{sid}/runs", headers=h["op"])).json()[0]
    assert run["status"] == "planned"
    assert (await client.post(f"/api/v1/tasks/{run['id']}/execute", headers=h["op"])).status_code == 202    # a person starts it
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/tasks/{run['id']}", headers=h["op"])).json()["status"] == "completed"


async def test_schedule_is_disabled_when_its_owner_loses_the_right_to_run_workflows(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"])).json()["id"]
    me = (await client.get("/api/v1/users/me", headers=h["op"])).json()
    assert (await client.patch(f"/api/v1/users/{me['id']}/role", json={"role": "viewer"}, headers=h["admin"])).status_code == 200
    await make_due(app, sid)
    assert await fire(app, runner) == []
    assert (await client.get(f"/api/v1/schedules/{sid}", headers=h["admin"])).json()["enabled"] is False
    assert await task_count(app, uuid.UUID(sid)) == 0
    assert ("schedule.disabled", "failure") in {(l["action"], l["status"]) for l in (await client.get("/api/v1/audit-logs?limit=300", headers=h["admin"])).json()}


async def test_scheduled_run_pauses_for_approval_like_any_other(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"], params={"delivery": "email", "recipient": "boss@mycompany.example"})).json()["id"]
    await make_due(app, sid)
    await fire(app, runner)
    run = (await client.get(f"/api/v1/schedules/{sid}/runs", headers=h["op"])).json()[0]
    assert run["status"] == "awaiting_approval"
    assert (await resume(client, h["mgr"], run["id"])).status_code == 200
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/tasks/{run['id']}", headers=h["op"])).json()["status"] == "completed"


async def test_run_now_and_delete_keep_history(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"])).json()["id"]
    assert (await client.post(f"/api/v1/schedules/{sid}/run-now", headers=h["op2"])).status_code == 404
    r = await client.post(f"/api/v1/schedules/{sid}/run-now", headers=h["op"])
    assert r.status_code == 202
    await runner.wait_idle()
    tid = r.json()["task_id"]
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"
    assert (await client.delete(f"/api/v1/schedules/{sid}", headers=h["op2"])).status_code == 404
    assert (await client.delete(f"/api/v1/schedules/{sid}", headers=h["op"])).status_code == 204
    t = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert t["status"] == "completed" and t["schedule_id"] is None                  # the run history survives


async def test_maintenance_tick_runs_every_duty_even_if_one_fails(app, client, gateway, runner):
    h = await users(client)
    sid = (await mk(client, h["op"])).json()["id"]
    await make_due(app, sid)

    async def boom(llm):
        raise RuntimeError("expiry sweep broke")

    runner.expire_overdue_approvals = boom
    out = await run_maintenance(app.state.session_factory, runner, ScriptedLLM())
    await runner.wait_idle()
    assert out == {"expired_approvals": -1, "schedules_fired": 1, "reaped_tasks": 0}
