import pytest

from app.agents.schemas import ExecutorDecision, FinalSummary, ReasoningOutput, StepVerdict
from app.llm.client import get_llm_client
from app.tools.types import ToolReportedError
from app.workflows.builders import BUILDERS, BuildContext, BuilderError, reminders_send
from app.workflows.models import (
    PurchaseRecommendation, PurchaseRecommendations, ReminderDecision, ReminderDecisions,
)
from app.workflows.templates import TEMPLATES, build_workflow_plan
from tests.fakes import ScriptedLLM, final, reasoning
from tests.test_approvals import make_policy
from tests.test_execution import events, pending_approval_id, resume, types, users


async def run_wf(client, runner, headers, name, params=None, expect=202, **kw):
    r = await client.post(f"/api/v1/workflows/{name}/run", json={"params": params or {}, **kw}, headers=headers)
    assert r.status_code == expect, r.text
    if expect != 202:
        return r
    await runner.wait_idle()
    return r.json()["task_id"]


async def task_of(client, headers, tid):
    return (await client.get(f"/api/v1/tasks/{tid}", headers=headers)).json()


def D(invoice_id, action, reason="r", subject=None, body=None):
    return ReminderDecision(invoice_id=invoice_id, action=action, reason=reason, subject=subject, body=body)


def reminder_llm(*decisions):
    return ScriptedLLM().add(ReminderDecisions, ReminderDecisions(decisions=list(decisions))).add(FinalSummary, final("Reminders handled."))


DECISIONS = (
    D(1, "skip", "customer disputes the quantity"), D(3, "skip", "customer promised payment next Friday"),
    D(2, "send", "partial payment", "Reminder: balance on INV-1002", "Dear Bright Foods, thank you for the INR 20,000 part payment. INR 28,000 on INV-1002 is 45 days overdue.\nAccounts Team"),
    D(7, "send", "no contact", "Payment reminder: INV-1007", "Dear Falcon Auto Parts, INV-1007 (INR 210,000) is 32 days overdue.\nAccounts Team"),
    D(999, "send", "bogus", "x", "y"),                       # an invoice that was never listed
)


async def reminders(client, h, invoice_id):
    r = await client.post("/api/v1/tools/invoke", json={"tool": "business_db.get_invoice", "arguments": {"invoice_id": invoice_id}}, headers=h["op"])
    return r.json()["result"]["reminders"]


# ------------------------------------------------------------------ catalog & validation ----
async def test_catalog_and_parameter_validation(app, client, gateway, runner):
    h = await users(client)
    cat = (await client.get("/api/v1/workflows", headers=h["op"])).json()
    assert {w["name"] for w in cat} == set(TEMPLATES) and len(cat) == 4
    digest = next(w for w in cat if w["name"] == "operations_digest")
    assert "delivery" in digest["params_schema"]["properties"]
    assert (await client.get("/api/v1/workflows", headers=h["vic"])).status_code == 403
    assert (await run_wf(client, runner, h["vic"], "operations_digest", expect=403)).status_code == 403
    assert (await run_wf(client, runner, h["op"], "nope", expect=404)).status_code == 404
    bad = await run_wf(client, runner, h["op"], "operations_digest", {"delivery": "email"}, expect=422)     # no recipient
    assert "recipient" in bad.text
    assert (await run_wf(client, runner, h["op"], "overdue_invoice_reminders", {"max_invoices": 500}, expect=422)).status_code == 422
    assert (await run_wf(client, runner, h["op"], "report_summary", {}, expect=422)).status_code == 422       # query required


def test_every_template_builds_a_valid_plan_with_registered_builders():
    samples = {"overdue_invoice_reminders": {}, "purchase_request_review": {}, "report_summary": {"query": "report"},
               "operations_digest": {"narrative": True, "delivery": "email", "recipient": "a@b.example"}}
    for name, params in samples.items():
        plan, _ = build_workflow_plan(name, params)
        ids = [s["id"] for s in plan["steps"]]
        assert ids == sorted(set(ids))
        for st in plan["steps"]:
            assert all(d < st["id"] for d in st["depends_on"])
            if st.get("mode") in ("builder", "render"):
                assert st["builder"] in BUILDERS
            if st["action"] == "write" and st["tool"] == "gmail":
                assert st["risk"] == "high" and st["requires_approval"] is True        # safety floor applied to templates too


# ------------------------------------------------------------------ operations digest: no LLM at all ----
async def test_operations_digest_runs_without_any_llm(app, client, gateway, runner):
    h = await users(client)                                    # note: no LLM configured or scripted at all
    tid = await run_wf(client, runner, h["op"], "operations_digest")
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and t["workflow"] == "operations_digest" and t["llm_calls"] == 0
    r = t["result"]
    assert "OPERATIONS DIGEST - 2026-11-02" in r and "Overdue invoices (>= 1 day(s)): 5" in r
    assert "Total outstanding: INR 470,500.00" in r                                     # 125000+28000+92500+15000+210000
    assert "1-30 days: 2" in r and "31-60 days: 2" in r and "61+ days: 1" in r
    assert "INV-1007 Falcon Auto Parts: INR 210,000.00 (32 days)" in r
    assert "Pending purchase requests: 2" in r and "Estimated spend awaiting approval: INR 210,000.00" in r
    assert "Oldest: request 1 (Corrugated shipping boxes) since 2026-10-27" in r
    evs = types(await events(client, h["op"], tid))
    assert evs[0] == "task_started" and evs[-1] == "task_completed"


async def test_digest_narrative_and_draft_delivery(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ReasoningOutput, reasoning("Focus on INV-1001, 75 days overdue."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "operations_digest",
                       {"narrative": True, "delivery": "draft", "recipient": "boss@mycompany.example"})
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and "OPERATIONS DIGEST" in t["result"] and "Focus on INV-1001" in t["result"]
    assert t["llm_calls"] == 1                                                         # only the commentary used the LLM
    ex = (await client.get("/api/v1/tool-executions?tool=gmail.create_draft", headers=h["admin"])).json()
    assert len(ex) == 1 and ex[0]["arguments"]["to"] == "boss@mycompany.example" and ex[0]["approval_id"] is None


async def test_digest_email_delivery_needs_approval(app, client, gateway, runner):
    h = await users(client)
    tid = await run_wf(client, runner, h["op"], "operations_digest", {"delivery": "email", "recipient": "boss@mycompany.example"})
    assert (await task_of(client, h["op"], tid))["status"] == "awaiting_approval"
    assert (await resume(client, h["mgr"], tid)).status_code == 200
    await runner.wait_idle()
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed"
    assert (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()[0]["approval_id"]


# ------------------------------------------------------------------ report summary ----
async def test_report_summary_result_is_the_llm_summary_itself(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ReasoningOutput, reasoning("South grew 12% while East fell 2%."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "report_summary", {"query": "report", "focus": "regional growth"})
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and t["result"] == "South grew 12% while East fell 2%."
    assert llm.count(FinalSummary) == 0 and llm.count(ExecutorDecision) == 0
    prompt = llm.prompts(ReasoningOutput)[0]
    assert "4200000" in prompt and "<untrusted_data" in prompt and "regional growth" in prompt


async def test_report_summary_with_no_matching_file_fails_clearly(app, client, gateway, runner):
    h = await users(client)
    tid = await run_wf(client, runner, h["op"], "report_summary", {"query": "zzzz-nothing"})
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "failed" and "No Drive files matched" in t["error"]


# ------------------------------------------------------------------ overdue invoice reminders (the big one) ----
async def test_overdue_reminders_end_to_end(app, client, gateway, runner):
    h = await users(client)
    llm = reminder_llm(*DECISIONS)
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "overdue_invoice_reminders")

    t = await task_of(client, h["op"], tid)
    assert t["status"] == "awaiting_approval" and t["current_step"] == 8
    # the LLM was used ONLY for the judgement step: no executor, no verifier
    assert llm.count(ReminderDecisions) == 1 and llm.count(ExecutorDecision) == 0 and llm.count(StepVerdict) == 0
    briefing = llm.prompts(ReminderDecisions)[0]
    assert "polite, specific, brief" in briefing                              # playbook read from Drive
    assert "billed for 500 units" in briefing and "next Friday" in briefing    # email history read from Gmail
    assert "INV-1004" not in briefing                                          # only 5 days overdue: below the threshold
    assert "<untrusted_data" in briefing

    approval = (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["mgr"])).json()[0]
    calls = approval["proposed_calls"]
    # recipients come from the business database, never from the model; invoice 999 was ignored
    assert [(c["tool"], c["arguments"]["to"], c["meta"]["number"]) for c in calls] == [
        ("gmail.send_email", "finance@brightfoods.example", "INV-1002"),
        ("gmail.send_email", "accounts@falconauto.example", "INV-1007")]

    edited = {**calls[0]["arguments"], "body": calls[0]["arguments"]["body"] + "\nP.S. A payment link is attached."}
    r = await resume(client, h["mgr"], tid, comment="added a P.S.", edits=[{"index": 0, "arguments": edited}])
    assert r.status_code == 200 and r.json()["modified"] is True
    await runner.wait_idle()

    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed"
    assert t["result"].startswith("Reminders handled.") and "Ignored a decision for unknown invoice 999" in t["result"]
    sent = (await client.post("/api/v1/tools/invoke", json={"tool": "gmail.search_emails", "arguments": {"query": "P.S. payment link"}}, headers=h["op"])).json()
    assert sent["result"]["count"] == 1
    # recorded against exactly the invoices that were chased (using the EDITED subject/recipient actually sent)
    assert [len(await reminders(client, h, i)) for i in (1, 2, 3, 7)] == [0, 1, 0, 1]
    assert "finance@brightfoods.example" in (await reminders(client, h, 2))[0]["note"]
    assert llm.count(StepVerdict) == 0


async def test_code_guardrails_override_the_models_decision(app, client, gateway, runner):
    h = await users(client)
    for i in range(3):                                          # INV-1002 already chased 3 times
        r = await client.post("/api/v1/tools/invoke", headers=h["op"], json={
            "tool": "business_db.log_invoice_reminder", "idempotency_key": f"pre-{i}",
            "arguments": {"invoice_id": 2, "channel": "email", "note": f"earlier {i}"}})
        assert r.status_code == 200
    llm = reminder_llm(D(2, "send", "again", "Reminder", "Please pay."), D(7, "send", "x", "Reminder 7", "Please pay 7."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "overdue_invoice_reminders", {"min_days_between_reminders": 0})
    approval = (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["mgr"])).json()[0]
    assert [c["meta"]["number"] for c in approval["proposed_calls"]] == ["INV-1007"]          # INV-1002 not emailed
    await resume(client, h["mgr"], tid)
    await runner.wait_idle()
    t = await task_of(client, h["op"], tid)
    assert "INV-1002: already 3 reminders sent; escalate to a person" in t["result"]


async def test_recent_reminder_gap_is_enforced_in_code(app, client, gateway, runner):
    h = await users(client)
    await client.post("/api/v1/tools/invoke", headers=h["op"], json={
        "tool": "business_db.log_invoice_reminder", "arguments": {"invoice_id": 7, "channel": "email", "note": "just now"}})
    llm = reminder_llm(D(7, "send", "x", "Reminder 7", "Please pay 7."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "overdue_invoice_reminders")        # default: 7 days between reminders
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed"                                              # nothing to approve: no step 8 calls at all
    assert "INV-1007: a reminder was sent less than 7 days ago; skipped" in t["result"]
    assert (await client.get("/api/v1/approvals", headers=h["mgr"])).json() == []


async def test_blocking_policy_skips_one_customer_without_failing_the_run(app, client, gateway, runner):
    h = await users(client)
    await make_policy(client, h, name="no-falcon", action="block", tool_pattern="gmail.send_email", message="Falcon is on hold",
                      arg_conditions=[{"field": "to", "op": "email_domain_in", "value": ["falconauto.example"]}])
    llm = reminder_llm(D(2, "send", "x", "Reminder 2", "Please pay 2."), D(7, "send", "x", "Reminder 7", "Please pay 7."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "overdue_invoice_reminders")
    approval = (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["mgr"])).json()[0]
    assert [c["meta"]["number"] for c in approval["proposed_calls"]] == ["INV-1002"]
    await resume(client, h["mgr"], tid)
    await runner.wait_idle()
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and "blocked by policy 'no-falcon' (Falcon is on hold)" in t["result"]


async def test_partial_send_failure_keeps_successes_and_does_not_ask_twice(app, client, gateway, runner):
    h = await users(client)
    llm = reminder_llm(D(2, "send", "x", "Reminder 2", "Please pay 2."), D(7, "send", "x", "Reminder 7", "Please pay 7."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    orig = gateway.execute

    async def flaky(db, ctx, tool, arguments):
        if tool == "gmail.send_email" and arguments["to"] == "accounts@falconauto.example":
            raise ToolReportedError("mailbox is full")
        return await orig(db, ctx, tool, arguments)

    gateway.execute = flaky
    tid = await run_wf(client, runner, h["op"], "overdue_invoice_reminders")
    await resume(client, h["mgr"], tid)
    await runner.wait_idle()

    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and "partly succeeded" in t["result"] and "mailbox is full" in t["result"]
    evs = types(await events(client, h["op"], tid))
    assert "step_retry" in evs and "approval_reused" in evs and evs.count("approval_requested") == 1   # the retry did NOT re-ask
    assert [len(await reminders(client, h, i)) for i in (2, 7)] == [1, 0]                             # only the sent one is recorded
    sends = (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()
    assert [s["status"] for s in sends].count("succeeded") == 1                                       # Bright Foods got exactly ONE email


async def test_no_overdue_invoices_means_nothing_to_do(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ReminderDecisions, ReminderDecisions(decisions=[])).add(FinalSummary, final("Nothing was overdue."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "overdue_invoice_reminders", {"min_days_overdue": 365})
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and t["result"].startswith("Nothing was overdue.")


# ------------------------------------------------------------------ purchase request review ----
async def test_purchase_review_applies_only_valid_approved_recommendations(app, client, gateway, runner):
    h = await users(client)
    recs = PurchaseRecommendations(recommendations=[
        PurchaseRecommendation(request_id=1, action="approve", quotation_id=3, rationale="Cheapest valid quotation (Apex)"),
        PurchaseRecommendation(request_id=2, action="approve", quotation_id=3, rationale="wrong quotation id"),   # belongs to request 1
    ])
    llm = ScriptedLLM().add(PurchaseRecommendations, recs).add(FinalSummary, final("Reviewed 2 requests."))
    app.dependency_overrides[get_llm_client] = lambda: llm
    tid = await run_wf(client, runner, h["op"], "purchase_request_review")

    briefing = llm.prompts(PurchaseRecommendations)[0]
    assert "OVER BUDGET" in briefing and "quotation_id=3 Apex Office Supplies" in briefing
    assert (await task_of(client, h["op"], tid))["status"] == "awaiting_approval"
    approval = (await client.get(f"/api/v1/approvals?task_id={tid}", headers=h["mgr"])).json()[0]
    assert "plan marked this step" in approval["reason"]
    assert [c["arguments"] for c in approval["proposed_calls"]] == [{"request_id": 1, "new_status": "approved"}]

    assert (await resume(client, h["mgr"], tid)).status_code == 200
    await runner.wait_idle()
    t = await task_of(client, h["op"], tid)
    assert t["status"] == "completed" and "Request 2: not approved" in t["result"]
    pending = (await client.post("/api/v1/tools/invoke", headers=h["op"], json={
        "tool": "business_db.list_purchase_requests", "arguments": {"status": "pending"}})).json()["result"]["purchase_requests"]
    assert [p["id"] for p in pending] == [2]                                      # request 1 is now approved


# ------------------------------------------------------------------ builders (pure functions) ----
def make_ctx(params, outputs, step=None):
    return BuildContext(params=params, outputs=outputs, step=step or {"id": 8, "depends_on": [1, 7]})


def inv(**kw):
    base = dict(invoice_id=1, number="INV-1", customer_email="a@x.example", reminders_sent=0, last_reminder_at=None)
    return {**base, **kw}


def sample_outputs(invoices, decisions):
    return {1: [{"ok": True, "result": {"as_of": "2026-11-02", "invoices": invoices}}], 7: ReminderDecisions(decisions=decisions).model_dump()}


PARAMS = {"max_reminders_per_invoice": 3, "min_days_between_reminders": 7}


def test_send_builder_takes_recipient_from_data_and_skips_bad_decisions():
    out = reminders_send(make_ctx(PARAMS, sample_outputs(
        [inv(invoice_id=1), inv(invoice_id=2, number="INV-2", customer_email="b@x.example")],
        [D(1, "send", "r", "Hello\nBcc: evil@x.example", "Body"), D(1, "send", "dup", "s", "b"),
         D(2, "send", "r", "", "body"), D(5, "skip", "r")])))
    assert [c["arguments"]["to"] for c in out.calls] == ["a@x.example"]
    assert out.calls[0]["arguments"]["subject"] == "Hello Bcc: evil@x.example"       # newline stripped: no header injection
    assert any("duplicate" in w for w in out.warnings) and any("empty" in w for w in out.warnings)
    assert any("unknown invoice 5" in w for w in out.warnings)


def test_builders_fail_loudly_when_inputs_are_missing():
    with pytest.raises(BuilderError):
        reminders_send(make_ctx(PARAMS, {1: [], 7: ReminderDecisions(decisions=[]).model_dump()}))
    with pytest.raises(BuilderError):
        BUILDERS["report.read"](BuildContext({"query": "q", "max_files": 2}, {1: [{"ok": True, "result": {"files": []}}]},
                                             {"id": 2, "depends_on": [1]}))
