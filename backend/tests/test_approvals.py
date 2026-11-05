import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.agents.schemas import ExecutorDecision, FinalSummary, StepVerdict
from app.core.config import get_settings
from app.models.approval import Approval
from app.models.tooling import ToolExecution  # noqa: F401
from app.tools.types import ApprovalRequired, ToolContext
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import ScriptedLLM, decide, final, plan, step, tc, verdict
from tests.test_execution import events, pending_approval_id, planned_task, resume, run, types, users

SEND_PLAN = plan(step(1, "Send reminders", tool="gmail", action="write", risk="high", approval=True), objective="Send")
MAIL_A = {"to": "ap@cedarlogistics.example", "subject": "Reminder INV-1003", "body": "Hello Cedar, INV-1003 is overdue."}
MAIL_B = {"to": "finance@brightfoods.example", "subject": "Reminder INV-1002", "body": "Hello Bright, balance is due."}


def send_llm(*calls, verdicts=(True,)):
    return (ScriptedLLM().add(ExecutorDecision, decide(*calls))
            .add(StepVerdict, *[verdict(v, "ok" if v else "not ok") for v in verdicts]).add(FinalSummary, final("done")))


async def paused(app, client, runner, h, llm, owner="op", the_plan=SEND_PLAN):
    tid = await planned_task(app, client, h[owner], the_plan, llm)
    task = await run(client, runner, h[owner], tid)
    assert task["status"] == "awaiting_approval", task
    return tid


async def mailbox(client, h, query):
    r = await client.post("/api/v1/tools/invoke", json={"tool": "gmail.search_emails", "arguments": {"query": query}}, headers=h["op"])
    return r.json()["result"]["emails"]


async def audit_actions(client, h):
    return {(l["action"], l["status"]) for l in (await client.get("/api/v1/audit-logs?limit=500", headers=h["admin"])).json()}


async def make_policy(client, h, **body):
    r = await client.post("/api/v1/approval-policies", json=body, headers=h["admin"])
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------ records & visibility ----
async def test_approval_record_visibility_and_full_lifecycle(app, client, gateway, runner):
    h = await users(client)
    tid = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)))

    mgr_view = (await client.get("/api/v1/approvals?status=pending", headers=h["mgr"])).json()
    assert len(mgr_view) == 1
    a = mgr_view[0]
    assert a["status"] == "pending" and a["instruction"] == "do it" and a["can_decide"] is True
    assert a["required_role"] == "manager" and a["step_id"] == 1 and a["task_id"] == tid
    assert "gmail.send_email is a high-risk tool" in a["reason"]
    assert a["proposed_calls"][0]["arguments"]["to"] == MAIL_A["to"]
    exp = datetime.fromisoformat(a["expires_at"]).replace(tzinfo=timezone.utc) if "+" not in a["expires_at"] else datetime.fromisoformat(a["expires_at"])
    assert timedelta(minutes=1430) < exp - datetime.now(timezone.utc) < timedelta(minutes=1441)

    own = (await client.get("/api/v1/approvals", headers=h["op"])).json()
    assert [x["id"] for x in own] == [a["id"]] and own[0]["can_decide"] is False
    assert (await client.get("/api/v1/approvals", headers=h["op2"])).json() == []           # not their task
    assert (await client.get("/api/v1/approvals", headers=h["vic"])).json() == []
    assert (await client.get(f"/api/v1/approvals/{a['id']}", headers=h["op2"])).status_code == 404
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["pending_approval"]["approval_id"] == a["id"]

    r = await client.post(f"/api/v1/approvals/{a['id']}/decision", json={"decision": "approve", "comment": "looks right"}, headers=h["mgr"])
    assert r.status_code == 200
    done = r.json()
    assert done["status"] == "approved" and done["decision_comment"] == "looks right" and done["modified"] is False
    assert done["final_calls"][0]["arguments"] == MAIL_A and done["decided_by"] and done["can_decide"] is False
    await runner.wait_idle()

    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"
    ex = (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()
    assert len(ex) == 1 and ex[0]["approval_id"] == a["id"] and ex[0]["approved_by"] == done["decided_by"]
    acts = await audit_actions(client, h)
    assert ("approval.requested", "success") in acts and ("approval.approved", "success") in acts
    assert (await client.post(f"/api/v1/approvals/{a['id']}/decision", json={"decision": "approve"}, headers=h["admin"])).status_code == 409


async def test_plan_flagged_step_pauses_even_when_the_tool_is_low_risk(app, client, gateway, runner):
    h = await users(client)
    plan_ = plan(step(1, "Look up", approval=True))
    llm = ScriptedLLM().add(ExecutorDecision, decide(tc("business_db.get_customer", customer_id=1))).add(FinalSummary, final())
    tid = await paused(app, client, runner, h, llm, the_plan=plan_)
    a = (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["mgr"])).json()[0]
    assert "plan marked this step" in a["reason"]


# ------------------------------------------------------------ separation of duties ----
async def test_you_cannot_approve_your_own_request_unless_configured(app, client, gateway, runner, monkeypatch):
    h = await users(client)
    tid = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)), owner="mgr")
    aid = await pending_approval_id(client, h["mgr"], tid)
    listed = (await client.get(f"/api/v1/approvals/{aid}", headers=h["mgr"])).json()
    assert listed["can_decide"] is False and "separation of duties" in listed["cannot_decide_reason"]

    r = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=h["mgr"])
    assert r.status_code == 403 and "separation of duties" in r.text
    assert ("approval.denied", "denied") in await audit_actions(client, h)
    assert (await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=h["admin"])).status_code == 200
    await runner.wait_idle()

    # solo-developer mode
    monkeypatch.setattr(get_settings(), "approval_allow_self_approval", True)
    llm = send_llm(tc("gmail.send_email", **MAIL_B))
    tid2 = await paused(app, client, runner, h, llm, owner="mgr")
    assert (await resume(client, h["mgr"], tid2)).status_code == 200


# ------------------------------------------------------------ modify ----
async def test_reviewer_can_edit_a_call_and_the_edited_version_is_what_runs(app, client, gateway, runner):
    h = await users(client)
    llm = send_llm(tc("gmail.send_email", **MAIL_A))
    tid = await paused(app, client, runner, h, llm)
    edited = {**MAIL_A, "body": "EDITED BY MANAGER: please pay INV-1003 this week."}
    r = await resume(client, h["mgr"], tid, comment="softer wording", edits=[{"index": 0, "arguments": edited}])
    assert r.status_code == 200
    a = r.json()
    assert a["modified"] is True and a["status"] == "approved"
    assert a["proposed_calls"][0]["arguments"]["body"] == MAIL_A["body"]            # the original is preserved
    assert a["final_calls"][0]["arguments"]["body"].startswith("EDITED BY MANAGER")
    assert a["modifications"][0]["from"]["body"] == MAIL_A["body"]
    await runner.wait_idle()

    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"
    sent = await mailbox(client, h, "EDITED BY MANAGER")
    assert len(sent) == 1 and await mailbox(client, h, "overdue") == []             # the original wording never went out
    assert "reviewer modified the proposed calls" in llm.prompts(StepVerdict)[0] and "changed body" in llm.prompts(StepVerdict)[0]
    assert ("approval.modified", "success") in await audit_actions(client, h)


async def test_reviewer_can_drop_one_call_and_approve_the_rest(app, client, gateway, runner):
    h = await users(client)
    llm = send_llm(tc("gmail.send_email", **MAIL_A), tc("gmail.send_email", **MAIL_B))
    tid = await paused(app, client, runner, h, llm)
    r = await resume(client, h["mgr"], tid, remove=[1])
    assert r.status_code == 200 and len(r.json()["final_calls"]) == 1 and len(r.json()["proposed_calls"]) == 2
    await runner.wait_idle()
    assert len(await mailbox(client, h, "Reminder INV")) == 1
    assert "call 2 (gmail.send_email) was removed" in llm.prompts(StepVerdict)[0]


async def test_invalid_edits_are_refused_and_the_request_stays_pending(app, client, gateway, runner):
    h = await users(client)
    tid = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)))
    aid = await pending_approval_id(client, h["mgr"], tid)

    async def attempt(**body):
        return await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve", **body}, headers=h["mgr"])

    assert (await attempt(edits=[{"index": 0, "arguments": {"subject": "only subject"}}])).status_code == 422   # schema
    assert (await attempt(edits=[{"index": 4, "arguments": MAIL_A}])).status_code == 422                          # bad index
    assert (await attempt(remove=[3])).status_code == 422
    assert (await attempt(edits=[{"index": 0, "arguments": MAIL_A}], remove=[0])).status_code == 422
    r = await attempt(remove=[0])
    assert r.status_code == 422 and "reject the request instead" in r.text
    # an edit that violates a policy created AFTER the request was raised
    await make_policy(client, h, name="internal-only", action="block", tool_pattern="gmail.send_email", message="Internal recipients only",
                      arg_conditions=[{"field": "to", "op": "email_domain_in", "value": ["evil.example"]}])
    r = await attempt(edits=[{"index": 0, "arguments": {**MAIL_A, "to": "x@evil.example"}}])
    assert r.status_code == 422 and "blocked by policy 'internal-only'" in r.text
    assert (await client.get(f"/api/v1/approvals/{aid}", headers=h["mgr"])).json()["status"] == "pending"
    assert (await attempt()).status_code == 200                                                                   # untouched request still approvable


# ------------------------------------------------------------ reject ----
async def test_rejection_needs_a_reason_and_is_recorded(app, client, gateway, runner):
    h = await users(client)
    tid = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)))
    aid = await pending_approval_id(client, h["mgr"], tid)
    no_reason = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "reject"}, headers=h["mgr"])
    assert no_reason.status_code == 422
    r = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "reject", "comment": "customer is in dispute"}, headers=h["mgr"])
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["final_calls"] is None
    await runner.wait_idle()
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "rejected" and "customer is in dispute" in task["error"]
    assert await mailbox(client, h, "Reminder") == []
    assert ("approval.rejected", "success") in await audit_actions(client, h)
    assert (await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=h["mgr"])).status_code == 409


# ------------------------------------------------------------ policies ----
async def test_policy_management_is_admin_only_validated_and_audited(app, client, gateway, runner):
    h = await users(client)
    body = {"name": "dual-control", "tool_pattern": "business_db.update_*", "min_risk": "medium", "approver_role": "admin"}
    assert (await client.post("/api/v1/approval-policies", json=body, headers=h["mgr"])).status_code == 403
    assert (await client.post("/api/v1/approval-policies", json=body, headers=h["op"])).status_code == 403
    p = await make_policy(client, h, **body)
    assert (await client.post("/api/v1/approval-policies", json=body, headers=h["admin"])).status_code == 409   # duplicate name
    assert [x["name"] for x in (await client.get("/api/v1/approval-policies", headers=h["mgr"])).json()] == ["dual-control"]
    assert (await client.get("/api/v1/approval-policies", headers=h["op"])).status_code == 403

    for bad in ({"name": "x", "arg_conditions": [{"field": "a", "op": "in", "value": "not-a-list"}]},
                {"name": "x", "arg_conditions": [{"field": "a", "op": "gt", "value": "text"}]},
                {"name": "x", "arg_conditions": [{"field": "a b", "op": "eq", "value": 1}]},
                {"name": "x", "tool_pattern": "gmail/send;rm"}, {"name": "x", "action": "allow"},
                {"name": "x", "expires_in_minutes": 0}, {"name": "x", "approver_role": "operator"}):
        assert (await client.post("/api/v1/approval-policies", json=bad, headers=h["admin"])).status_code == 422, bad

    r = await client.patch(f"/api/v1/approval-policies/{p['id']}", json={"enabled": False, "expires_in_minutes": 15}, headers=h["admin"])
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["expires_in_minutes"] == 15
    assert (await client.delete(f"/api/v1/approval-policies/{p['id']}", headers=h["admin"])).status_code == 204
    assert (await client.delete(f"/api/v1/approval-policies/{p['id']}", headers=h["admin"])).status_code == 404
    acts = await audit_actions(client, h)
    assert {("policy.create", "success"), ("policy.update", "success"), ("policy.delete", "success")} <= acts


async def test_policy_adds_approval_for_a_medium_risk_tool_with_stricter_approver_and_expiry(app, client, gateway, runner):
    h = await users(client)
    await make_policy(client, h, name="audit-reminders", tool_pattern="business_db.log_*", min_risk="medium", approver_role="admin",
                      expires_in_minutes=30, message="Reminder logging needs admin sign-off")
    llm = (ScriptedLLM().add(ExecutorDecision, decide(tc("business_db.log_invoice_reminder", invoice_id=3, channel="email", note="n")))
           .add(StepVerdict, verdict(True)).add(FinalSummary, final()))
    plan_ = plan(step(1, "Log", action="write", risk="medium"))
    tid = await paused(app, client, runner, h, llm, the_plan=plan_)

    a = (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["admin"])).json()[0]
    assert a["required_role"] == "admin" and a["policies"] == ["audit-reminders"] and "admin sign-off" in a["reason"]
    exp = datetime.fromisoformat(a["expires_at"])
    exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=28) < exp - datetime.now(timezone.utc) < timedelta(minutes=31)

    r = await resume(client, h["mgr"], tid)
    assert r.status_code == 403 and "requires the 'admin' role" in r.text
    assert (await resume(client, h["admin"], tid)).status_code == 200
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"


async def test_blocking_policy_is_fed_back_to_the_agent_and_enforced_at_the_gateway(app, client, gateway, runner):
    h = await users(client)
    await make_policy(client, h, name="internal-only-email", action="block", tool_pattern="gmail.send_email",
                      message="External recipients are not allowed",
                      arg_conditions=[{"field": "to", "op": "email_domain_not_in", "value": ["cedarlogistics.example"]}])
    llm = (ScriptedLLM().add(ExecutorDecision,
                             decide(tc("gmail.send_email", to="someone@gmail.example", subject="s", body="b")),   # blocked
                             decide(tc("gmail.send_email", **MAIL_A)))                                              # allowed
           .add(StepVerdict, verdict(True)).add(FinalSummary, final()))
    tid = await paused(app, client, runner, h, llm)
    assert "internal-only-email" in llm.prompts(ExecutorDecision)[1] and "External recipients" in llm.prompts(ExecutorDecision)[1]
    assert "calls_blocked" in types(await events(client, h["op"], tid))
    assert (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["mgr"])).json()[0]["proposed_calls"][0]["arguments"]["to"] == MAIL_A["to"]

    # not just the graph: the gateway refuses it too (manual invoke, even with an admin's approval)
    r = await client.post("/api/v1/tools/invoke", headers=h["admin"], json={
        "tool": "gmail.send_email", "approve": True, "arguments": {"to": "x@gmail.example", "subject": "s", "body": "b"}})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "blocked_by_policy"
    assert ("tool.blocked_by_policy", "denied") in await audit_actions(client, h)


async def test_policy_dry_run_endpoint(app, client, gateway, runner):
    h = await users(client)
    await make_policy(client, h, name="big", tool_pattern="business_db.log_*", min_risk="medium",
                      arg_conditions=[{"field": "invoice_id", "op": "gt", "value": 2}], approver_role="admin")
    ev = lambda args: client.post("/api/v1/approval-policies/evaluate", headers=h["mgr"], json={"tool": "business_db.log_invoice_reminder", "arguments": args})
    hit = (await ev({"invoice_id": 3, "channel": "email", "note": "x"})).json()
    miss = (await ev({"invoice_id": 1, "channel": "email", "note": "x"})).json()
    assert hit["required"] is True and hit["approver_role"] == "admin" and hit["policies"] == ["big"]
    assert miss["required"] is False and miss["risk"] == "medium"
    assert (await client.post("/api/v1/approval-policies/evaluate", headers=h["op"], json={"tool": "x"})).status_code == 403
    assert (await client.post("/api/v1/approval-policies/evaluate", headers=h["mgr"], json={"tool": "nope.nope"})).status_code == 404


# ------------------------------------------------------------ expiry ----
async def expire_now(app, aid):
    async with app.state.session_factory() as db:
        await db.execute(update(Approval).where(Approval.id == uuid.UUID(aid)).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await db.commit()


async def test_late_decision_on_an_expired_request_is_refused_and_the_task_expires(app, client, gateway, runner):
    h = await users(client)
    tid = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)))
    aid = await pending_approval_id(client, h["mgr"], tid)
    await expire_now(app, aid)
    r = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=h["mgr"])
    assert r.status_code == 409 and "expired" in r.text
    await runner.wait_idle()
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "expired" and "timed out waiting for approval" in task["error"]
    assert (await client.get(f"/api/v1/approvals/{aid}", headers=h["mgr"])).json()["status"] == "expired"
    assert await mailbox(client, h, "Reminder") == []
    assert ("approval.expired", "failure") in await audit_actions(client, h)


async def test_sweeper_expires_overdue_requests_and_leaves_fresh_ones(app, client, gateway, runner):
    h = await users(client)
    old = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)))
    fresh = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_B)), owner="op2")
    await expire_now(app, await pending_approval_id(client, h["mgr"], old))
    assert await runner.expire_overdue_approvals(ScriptedLLM()) == 1
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/tasks/{old}", headers=h["op"])).json()["status"] == "expired"
    assert (await client.get(f"/api/v1/tasks/{fresh}", headers=h["op2"])).json()["status"] == "awaiting_approval"
    assert await runner.expire_overdue_approvals(ScriptedLLM()) == 0                       # idempotent


# ------------------------------------------------------------ defence in depth ----
async def test_gateway_only_runs_the_exact_call_an_approval_covers(app, client, gateway, runner):
    h = await users(client)
    uid = uuid.UUID((await client.get("/api/v1/users/me", headers=h["mgr"])).json()["id"])
    tid = uuid.UUID(await planned_task(app, client, h["op"], SEND_PLAN, ScriptedLLM()))
    call = {"tool": "gmail.send_email", "arguments": MAIL_A, "risk": "high", "action": "write"}

    async with app.state.session_factory() as db:
        ap = Approval(status="pending", requested_by=uid, task_id=tid, proposed_calls=[call], final_calls=[call])
        db.add(ap)
        await db.commit()
        ctx = ToolContext(user_id=uid, role="manager", task_id=tid, approval_id=ap.id)

        with pytest.raises(ApprovalRequired, match="'pending'"):
            await gateway.execute(db, ctx, "gmail.send_email", MAIL_A)                       # not approved yet
        ap.status = "approved"
        await db.commit()
        with pytest.raises(ApprovalRequired, match="exact call"):
            await gateway.execute(db, ctx, "gmail.send_email", {**MAIL_A, "body": "something else"})   # different text
        with pytest.raises(ApprovalRequired, match="different task"):
            await gateway.execute(db, ToolContext(user_id=uid, role="manager", task_id=uuid.uuid4(), approval_id=ap.id),
                                  "gmail.send_email", MAIL_A)
        with pytest.raises(ApprovalRequired, match="no approval"):
            await gateway.execute(db, ToolContext(user_id=uid, role="manager", task_id=tid), "gmail.send_email", MAIL_A)
        ok = await gateway.execute(db, ctx, "gmail.send_email", MAIL_A)
        assert ok.content["status"] == "sent"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrency (PostgreSQL)")
async def test_two_simultaneous_decisions_only_one_wins(app, client, gateway, runner):
    h = await users(client)
    tid = await paused(app, client, runner, h, send_llm(tc("gmail.send_email", **MAIL_A)))
    results = await asyncio.gather(resume(client, h["mgr"], tid), resume(client, h["admin"], tid))
    assert sorted(r.status_code for r in results) == [200, 409]
    await runner.wait_idle()
    assert len(await mailbox(client, h, "Reminder")) == 1
