import asyncio
import uuid

from app.agents.runner import TaskRunner
from app.agents.schemas import ExecutorDecision, FinalSummary, ReasoningOutput, StepVerdict
from app.llm.client import LLMError, get_llm_client
from tests.conftest import login_headers, register
from tests.fakes import (
    LOG_PLAN, READ_ONLY_PLAN, REMIND_PLAN, FakeLLM, ScriptedLLM, decide, final, plan, reasoning, step, tc, verdict,
)


async def users(client):
    await register(client, "admin@example.com")
    ids = {n: (await register(client, f"{n}@example.com")).json()["id"] for n in ("mgr", "op", "op2", "vic")}
    admin = await login_headers(client, "admin@example.com")
    for n, role in (("mgr", "manager"), ("op", "operator"), ("op2", "operator")):
        assert (await client.patch(f"/api/v1/users/{ids[n]}/role", json={"role": role}, headers=admin)).status_code == 200
    return {n: await login_headers(client, f"{n}@example.com") for n in ("mgr", "op", "op2", "vic")} | {"admin": admin}


async def planned_task(app, client, headers, the_plan, llm):
    """Create a planned task via /chat, then switch the LLM override to the scripted executor."""
    app.dependency_overrides[get_llm_client] = lambda: FakeLLM(the_plan)
    r = await client.post("/api/v1/chat", json={"message": "do it"}, headers=headers)
    assert r.status_code == 200, r.text
    app.dependency_overrides[get_llm_client] = lambda: llm
    return r.json()["task"]["id"]


async def pending_approval_id(client, headers, tid):
    r = await client.get(f"/api/v1/approvals?status=pending&task_id={tid}", headers=headers)
    items = r.json()
    assert len(items) == 1, items
    return items[0]["id"]


async def resume(client, deciding_headers, tid, approve=True, comment=None, lookup_headers=None, **extra):
    """Approve/reject the task's pending approval through the real approvals API."""
    aid = await pending_approval_id(client, lookup_headers or deciding_headers, tid)
    body = {"decision": "approve" if approve else "reject", "comment": comment or (None if approve else "no"), **extra}
    return await client.post(f"/api/v1/approvals/{aid}/decision", json=body, headers=deciding_headers)


async def run(client, runner, headers, task_id):
    r = await client.post(f"/api/v1/tasks/{task_id}/execute", headers=headers)
    assert r.status_code == 202, r.text
    await runner.wait_idle()
    return (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()


async def events(client, headers, task_id):
    return (await client.get(f"/api/v1/tasks/{task_id}/events", headers=headers)).json()


def types(evs):
    return [e["type"] for e in evs]


# --------------------------------------------------------------- happy path ----
async def test_read_only_plan_runs_to_completion(app, client, gateway, runner):
    h = await users(client)
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(tc("business_db.list_overdue_invoices", min_days_overdue=30)))
           .add(ReasoningOutput, reasoning("3 invoices are 30+ days overdue."))
           .add(FinalSummary, final("Found 3 invoices 30+ days overdue.")))
    task = await run(client, runner, h["op"], await planned_task(app, client, h["op"], READ_ONLY_PLAN, llm))

    assert task["status"] == "completed" and task["result"] == "Found 3 invoices 30+ days overdue."
    assert task["current_step"] is None and task["finished_at"] and task["started_at"]
    assert llm.count(StepVerdict) == 0                      # read-only steps are verified deterministically
    assert task["llm_calls"] == 1 + 3                       # 1 planning call + executor + reasoning + summary
    evs = await events(client, h["op"], task["id"])
    assert types(evs)[0] == "task_started" and types(evs)[-1] == "task_completed"
    assert types(evs).count("step_completed") == 2 and "tool_call" in types(evs)
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    # the reasoning step really received the data fetched by step 1, wrapped as untrusted
    assert "INV-1001" in llm.prompts(ReasoningOutput)[0] and "<untrusted_data" in llm.prompts(ReasoningOutput)[0]


async def test_full_reminder_workflow_pauses_for_approval_then_sends_once(app, client, gateway, runner):
    h = await users(client)
    mail1 = {"to": "accounts@falconauto.example", "subject": "Payment reminder INV-1007", "body": "Hello, INV-1007 is overdue."}
    mail2 = {"to": "finance@brightfoods.example", "subject": "Payment reminder INV-1002", "body": "Balance INR 28,000 is due."}
    llm = (ScriptedLLM()
           .add(ExecutorDecision,
                decide(tc("business_db.list_overdue_invoices", min_days_overdue=30)),
                decide(tc("gmail.search_emails", query="INV-1002"), tc("gmail.search_emails", query="INV-1007")),
                decide(tc("gmail.send_email", **mail1), tc("gmail.send_email", **mail2)))
           .add(ReasoningOutput, reasoning("Draft 1...\n---\nDraft 2..."))
           .add(StepVerdict, verdict(True, "both emails sent"))
           .add(FinalSummary, final("Sent 2 reminders.")))
    tid = await planned_task(app, client, h["op"], REMIND_PLAN, llm)

    paused = await run(client, runner, h["op"], tid)
    assert paused["status"] == "awaiting_approval" and paused["current_step"] == 4
    pending = paused["pending_approval"]
    assert [c["tool"] for c in pending["calls"]] == ["gmail.send_email", "gmail.send_email"]
    assert pending["calls"][0]["arguments"]["to"] == "accounts@falconauto.example"      # human sees the exact email
    assert "gmail.send_email" in pending["reason"]
    sent_now = (await client.post("/api/v1/tools/invoke", json={"tool": "gmail.search_emails", "arguments": {"query": "Payment reminder"}}, headers=h["op"])).json()
    assert sent_now["result"]["count"] == 0                                              # nothing sent yet

    assert (await resume(client, h["op"], tid, lookup_headers=h["mgr"])).status_code == 403
    assert (await resume(client, h["mgr"], tid, comment="ok")).status_code == 200
    await runner.wait_idle()

    done = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert done["status"] == "completed" and done["result"] == "Sent 2 reminders." and done["pending_approval"] is None
    found = (await client.post("/api/v1/tools/invoke", json={"tool": "gmail.search_emails", "arguments": {"query": "Payment reminder"}}, headers=h["op"])).json()
    assert found["result"]["count"] == 2
    ex = (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()
    assert len(ex) == 2 and all(e["approved_by"] for e in ex) and all(e["task_id"] == tid for e in ex)
    assert {"approval_requested", "approval_decision"} <= set(types(await events(client, h["op"], tid)))
    # second step really got 2 fan-out calls
    assert sum(1 for e in await events(client, h["op"], tid) if e["type"] == "tool_call" and "search_emails" in e["message"]) == 2


async def test_rejection_stops_the_task_and_sends_nothing(app, client, gateway, runner):
    h = await users(client)
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(tc("business_db.list_overdue_invoices")), decide(tc("gmail.search_emails", query="x")),
                decide(tc("gmail.send_email", to="a@b.example", subject="s", body="b")))
           .add(ReasoningOutput, reasoning("drafts")))
    tid = await planned_task(app, client, h["op"], REMIND_PLAN, llm)
    await run(client, runner, h["op"], tid)
    assert (await resume(client, h["mgr"], tid, approve=False, comment="wrong customer")).status_code == 200
    await runner.wait_idle()

    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "rejected" and "wrong customer" in task["error"] and "Step 4" in task["result"]
    assert (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json() == []
    assert llm.count(FinalSummary) == 0                       # no LLM spend on a rejected task


async def test_retry_after_approval_requires_a_fresh_approval_for_changed_calls(app, client, gateway, runner):
    h = await users(client)
    plan_ = plan(step(1, "Send reminder", tool="gmail", action="write", risk="high", approval=True))
    a = {"to": "a@cedarlogistics.example", "subject": "s", "body": "to cedar"}
    b = {"to": "b@brightfoods.example", "subject": "s", "body": "to bright"}
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(tc("gmail.send_email", **a)), decide(tc("gmail.send_email", **b)))
           .add(StepVerdict, verdict(False, "wrong recipient", "send to Bright Foods"), verdict(True, "ok"))
           .add(FinalSummary, final()))
    tid = await planned_task(app, client, h["op"], plan_, llm)

    assert (await run(client, runner, h["op"], tid))["status"] == "awaiting_approval"
    assert (await resume(client, h["mgr"], tid)).status_code == 200
    await runner.wait_idle()
    again = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert again["status"] == "awaiting_approval"                              # NOT auto-approved for the new calls
    assert again["pending_approval"]["calls"][0]["arguments"]["to"] == "b@brightfoods.example"
    assert (await resume(client, h["mgr"], tid)).status_code == 200
    await runner.wait_idle()
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "completed"


# --------------------------------------------------------------- self-correction ----
async def test_executor_self_corrects_from_feedback(app, client, gateway, runner):
    h = await users(client)
    plan_ = plan(step(1, "Look up invoice"))
    llm = (ScriptedLLM()
           .add(ExecutorDecision,
                decide(tc("business_db.get_invoice", invoice_id=999)),           # tool error: not found
                decide(tc("business_db.nonexistent_tool")),                       # unknown tool
                decide(tc("gmail.search_emails", query="x")),                     # wrong category
                decide(tc("business_db.get_invoice", invoice_id=-5)))             # schema violation
           .add(FinalSummary, final()))
    tid = await planned_task(app, client, h["op"], plan_, llm)
    # default is 2 attempts: first fails (not found), second proposes an unknown tool -> task fails with that reason
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "failed" and "not an available tool" in task["error"]
    prompts = llm.prompts(ExecutorDecision)
    assert "Invoice 999 not found" in prompts[1]                                   # failure fed back to the model


async def test_wrong_category_and_bad_arguments_are_caught_before_any_call(app, client, gateway, runner):
    h = await users(client)
    plan_ = plan(step(1, "Look up invoice"))
    llm = (ScriptedLLM()
           .add(ExecutorDecision,
                decide(tc("gmail.send_email", to="a@b.example", subject="s", body="b")),   # wrong category AND dangerous
                decide(tc("business_db.get_invoice", invoice_id=-5)))                        # schema violation
           .add(FinalSummary, final()))
    tid = await planned_task(app, client, h["op"], plan_, llm)
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "failed" and "invalid arguments" in task["error"]
    assert (await client.get("/api/v1/tool-executions", headers=h["admin"])).json() == []   # nothing reached the gateway
    assert "not an available tool" in llm.prompts(ExecutorDecision)[1]


async def test_retry_succeeds_second_time(app, client, gateway, runner):
    h = await users(client)
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(tc("business_db.get_invoice", invoice_id=999)), decide(tc("business_db.get_invoice", invoice_id=1)))
           .add(FinalSummary, final("Looked up INV-1001.")))
    tid = await planned_task(app, client, h["op"], plan(step(1, "Look up invoice")), llm)
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "completed"
    assert "step_retry" in types(await events(client, h["op"], tid))


async def test_cannot_proceed_fails_immediately_without_retry(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ExecutorDecision, decide(cannot="No customer was specified"))
    tid = await planned_task(app, client, h["op"], plan(step(1, "Look up customer")), llm)
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "failed" and "No customer was specified" in task["error"]
    assert llm.count(ExecutorDecision) == 1 and llm.count(FinalSummary) == 0


async def test_failure_in_step_two_stops_later_steps(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ExecutorDecision, decide(tc("business_db.list_overdue_invoices")),
                            decide(cannot="no thread"))
    plan_ = plan(step(1, "A"), step(2, "B", tool="gmail", depends_on=[1]), step(3, "C", depends_on=[2]))
    tid = await planned_task(app, client, h["op"], plan_, llm)
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "failed" and "Step 2" in task["error"]
    assert "- Step 1 A: done" in task["result"] and "- Step 3 C: not run" in task["result"]
    assert llm.count(ExecutorDecision) == 2


async def test_llm_outage_during_execution_fails_cleanly(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ExecutorDecision, LLMError("down"), LLMError("still down"))
    tid = await planned_task(app, client, h["op"], plan(step(1, "A")), llm)
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "failed" and "LLM error" in task["error"]


# --------------------------------------------------------------- verifier & duplicates ----
async def test_verifier_rejection_retries_but_the_write_is_not_repeated(app, client, gateway, runner):
    h = await users(client)
    call = tc("business_db.log_invoice_reminder", invoice_id=3, channel="email", note="reminder sent")
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(call), decide(call))
           .add(StepVerdict, verdict(False, "result unclear", "try again"), verdict(True, "reminder logged"))
           .add(FinalSummary, final()))
    tid = await planned_task(app, client, h["op"], LOG_PLAN, llm)
    task = await run(client, runner, h["op"], tid)
    assert task["status"] == "completed"
    inv = (await client.post("/api/v1/tools/invoke", json={"tool": "business_db.get_invoice", "arguments": {"invoice_id": 3}}, headers=h["op"])).json()
    assert len(inv["result"]["reminders"]) == 1                                    # attempt 2 hit the idempotency cache
    ex = (await client.get("/api/v1/tool-executions?tool=business_db.log_invoice_reminder", headers=h["admin"])).json()
    assert len(ex) == 1


async def test_unknown_write_outcome_needs_attention_and_stops(app, client, gateway, runner):
    h = await users(client)
    conn = gateway._connections["gmail"]
    state = {"calls": 0}

    async def timeout(name, args):
        state["calls"] += 1
        raise asyncio.TimeoutError()

    conn.call_tool = timeout
    plan_ = plan(step(1, "Send", tool="gmail", action="write", risk="high", approval=True), step(2, "After", depends_on=[1]))
    llm = ScriptedLLM().add(ExecutorDecision, decide(tc("gmail.send_email", to="a@b.example", subject="s", body="b")))
    tid = await planned_task(app, client, h["op"], plan_, llm)
    await run(client, runner, h["op"], tid)
    assert (await resume(client, h["mgr"], tid)).status_code == 200
    await runner.wait_idle()
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "needs_attention" and "A human must check" in task["error"]
    assert state["calls"] == 1 and llm.count(ExecutorDecision) == 1               # never retried, step 2 never started


# --------------------------------------------------------------- guards ----
async def test_execute_guards(app, client, gateway, runner):
    h = await users(client)
    llm = ScriptedLLM().add(ExecutorDecision, decide(tc("business_db.list_overdue_invoices"))).add(ReasoningOutput, reasoning("x")).add(FinalSummary, final())
    tid = await planned_task(app, client, h["op"], READ_ONLY_PLAN, llm)

    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["vic"])).status_code == 403
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op2"])).status_code == 404    # not the owner
    assert (await client.post(f"/api/v1/tasks/{tid}/execute")).status_code == 401
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 202
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 409    # already running/finished
    await runner.wait_idle()
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 409    # completed
    assert (await client.post(f"/api/v1/approvals/{uuid.uuid4()}/decision", json={"decision": "approve"}, headers=h["mgr"])).status_code == 404
    assert (await client.get(f"/api/v1/tasks/{tid}/events", headers=h["op2"])).status_code == 404


async def test_cannot_execute_a_task_without_steps(app, client, gateway, runner):
    h = await users(client)
    tid = await planned_task(app, client, h["op"], plan(intent="unsupported", objective="Not possible"), ScriptedLLM())
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 409


async def test_execute_without_tool_layer_gives_503(app, client):
    h = await users(client)
    app.dependency_overrides[get_llm_client] = lambda: ScriptedLLM()
    # (no runner/gateway fixture here -> no task_runner on app.state)
    r = await client.post("/api/v1/tasks/00000000-0000-0000-0000-000000000000/execute", headers=h["op"])
    assert r.status_code == 503


# --------------------------------------------------------------- crash recovery ----
async def test_crashed_run_resumes_from_checkpoint_without_redoing_work(app, client, gateway, runner):
    h = await users(client)
    plan_ = plan(step(1, "Overdue"), step(2, "Customer", depends_on=[1]))
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(tc("business_db.list_overdue_invoices")), decide(tc("business_db.get_customer", customer_id=1)))
           .add(FinalSummary, final("recovered fine")))
    tid = await planned_task(app, client, h["op"], plan_, llm)

    conn = gateway._connections["business_db"]
    real_call, entered, hang = conn.call_tool, asyncio.Event(), asyncio.Event()

    async def hanging(name, args):
        if name == "get_customer":
            entered.set()
            await hang.wait()                 # the process "dies" while this call is in flight
        return await real_call(name, args)

    conn.call_tool = hanging
    assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 202
    await asyncio.wait_for(entered.wait(), 5)
    await runner.shutdown()                    # simulate the crash: cancel the run mid-call
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()["status"] == "running"

    conn.call_tool = real_call                 # a fresh process: the tool server is healthy again
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401
    new_runner = TaskRunner(app.state.session_factory, gateway, runner.checkpointer, runner.settings)
    assert await new_runner.recover_interrupted(llm) == 1
    await new_runner.wait_idle()

    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "completed" and task["result"] == "recovered fine"
    assert llm.count(ExecutorDecision) == 2                                         # step 1 was NOT re-planned
    assert "task_recovered" in types(await events(client, h["op"], tid))
    stuck = (await client.get("/api/v1/tool-executions?status_filter=running", headers=h["admin"])).json()
    assert stuck == []                                                               # the cut-off read was settled


async def test_recovery_marks_a_cut_off_write_unknown(app, client, gateway, runner):
    h = await users(client)
    plan_ = plan(step(1, "Log", action="write", risk="medium"))
    call = tc("business_db.log_invoice_reminder", invoice_id=3, channel="email", note="n")
    llm = ScriptedLLM().add(ExecutorDecision, decide(call)).add(StepVerdict, verdict(True, "ok")).add(FinalSummary, final())
    tid = await planned_task(app, client, h["op"], plan_, llm)

    conn = gateway._connections["business_db"]
    real_call, entered, hang = conn.call_tool, asyncio.Event(), asyncio.Event()

    async def hanging(name, args):
        entered.set()
        await hang.wait()

    conn.call_tool = hanging
    await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])
    await asyncio.wait_for(entered.wait(), 5)
    await runner.shutdown()
    conn.call_tool = real_call

    new_runner = TaskRunner(app.state.session_factory, gateway, runner.checkpointer, runner.settings)
    await new_runner.recover_interrupted(llm)
    await new_runner.wait_idle()
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "needs_attention"                                       # did NOT blindly repeat the write
    inv = (await client.post("/api/v1/tools/invoke", json={"tool": "business_db.get_invoice", "arguments": {"invoice_id": 3}}, headers=h["op"])).json()
    assert inv["result"]["reminders"] == []
