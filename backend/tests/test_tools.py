import asyncio

from mcp.server.mcpserver import MCPServer

from app.llm.client import get_llm_client
from app.schemas.plan import ActionType, RiskLevel, ToolCategory
from tests.conftest import login_headers, register
from tests.fakes import GOOD_PLAN, FakeLLM


async def users(client):
    """admin, manager (mgr), operator (op), viewer (vic)."""
    await register(client, "admin@example.com")
    ids = {n: (await register(client, f"{n}@example.com")).json()["id"] for n in ("mgr", "op", "vic")}
    admin = await login_headers(client, "admin@example.com")
    for n, role in (("mgr", "manager"), ("op", "operator")):
        r = await client.patch(f"/api/v1/users/{ids[n]}/role", json={"role": role}, headers=admin)
        assert r.status_code == 200
    return {n: await login_headers(client, f"{n}@example.com") for n in ("mgr", "op", "vic")} | {"admin": admin}


async def invoke(client, h, tool, arguments=None, **extra):
    return await client.post("/api/v1/tools/invoke", json={"tool": tool, "arguments": arguments or {}, **extra}, headers=h)


# ---------------------------------------------------------------- discovery ----
async def test_tools_are_discovered_with_risk_derived_from_annotations(app, client, gateway):
    h = await users(client)
    tools = {t["name"]: t for t in (await client.get("/api/v1/tools", headers=h["op"])).json()}
    assert tools["business_db.list_overdue_invoices"]["risk"] == "low"
    assert tools["business_db.list_overdue_invoices"]["action"] == "read"
    assert tools["business_db.log_invoice_reminder"]["risk"] == "medium"
    assert tools["gmail.create_draft"]["risk"] == "medium"
    assert tools["gmail.send_email"]["risk"] == "high" and tools["gmail.send_email"]["requires_approval"] is True
    assert "input_schema" in tools["gmail.send_email"] and len(tools) == 14


async def test_viewer_cannot_list_or_invoke_tools(app, client, gateway):
    h = await users(client)
    assert (await client.get("/api/v1/tools", headers=h["vic"])).status_code == 403
    assert (await invoke(client, h["vic"], "drive.search_files", {"query": "policy"})).status_code == 403


async def test_unannotated_tool_defaults_to_high_risk_write(app, client, gateway):
    sloppy = MCPServer("sloppy")

    @sloppy.tool()
    def do_thing() -> dict:
        """Does something, author never declared annotations."""
        return {"ok": True}

    await gateway.add_server("sloppy", ToolCategory.CRM, sloppy)
    spec = gateway.registry.get("sloppy.do_thing")
    assert spec.action == ActionType.WRITE and spec.risk == RiskLevel.HIGH and spec.safe_to_retry is False


# ---------------------------------------------------------------- reads ----
async def test_read_tool_returns_real_business_data(app, client, gateway):
    h = await users(client)
    r = await invoke(client, h["op"], "business_db.list_overdue_invoices", {"min_days_overdue": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    inv = body["result"]["invoices"]
    assert [i["number"] for i in inv] == ["INV-1001", "INV-1002", "INV-1007"]   # oldest first, 1003/1004 too recent
    assert inv[0]["days_overdue"] == 75 and inv[1]["outstanding"] == 28000.0
    assert body["cached"] is False and body["result"]["as_of"] == "2026-11-02"


async def test_invalid_arguments_rejected_before_reaching_server(app, client, gateway):
    h = await users(client)
    r = await invoke(client, h["op"], "business_db.list_overdue_invoices", {"min_days_overdue": -5})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_arguments"
    r = await invoke(client, h["op"], "business_db.get_invoice", {"invoice_id": "abc"})
    assert r.status_code == 422
    assert (await invoke(client, h["op"], "nope.nothing")).status_code == 404


async def test_tool_reported_error_is_422_and_recorded(app, client, gateway):
    h = await users(client)
    r = await invoke(client, h["op"], "business_db.get_invoice", {"invoice_id": 999})
    assert r.status_code == 422 and "not found" in r.json()["detail"]["message"]
    ex = (await client.get("/api/v1/tool-executions?status_filter=failed", headers=h["admin"])).json()
    assert len(ex) == 1 and ex[0]["tool_name"] == "business_db.get_invoice"


# ---------------------------------------------------------------- idempotency ----
async def test_identical_write_runs_once_and_second_call_is_cached(app, client, gateway):
    h = await users(client)
    args = {"invoice_id": 3, "channel": "email", "note": "first reminder"}
    a = (await invoke(client, h["op"], "business_db.log_invoice_reminder", args)).json()
    b = (await invoke(client, h["op"], "business_db.log_invoice_reminder", args)).json()
    assert a["cached"] is False and b["cached"] is True and a["execution_id"] == b["execution_id"]

    inv = (await invoke(client, h["op"], "business_db.get_invoice", {"invoice_id": 3})).json()["result"]
    assert len(inv["reminders"]) == 1          # the write really happened only once

    # an explicit, different key is a deliberate second action
    c = (await invoke(client, h["op"], "business_db.log_invoice_reminder", args, idempotency_key="second-chase")).json()
    assert c["cached"] is False
    inv = (await invoke(client, h["op"], "business_db.get_invoice", {"invoice_id": 3})).json()["result"]
    assert len(inv["reminders"]) == 2


async def test_same_key_with_different_arguments_is_a_conflict(app, client, gateway):
    h = await users(client)
    base = {"invoice_id": 3, "channel": "email", "note": "a"}
    assert (await invoke(client, h["op"], "business_db.log_invoice_reminder", base, idempotency_key="k1")).status_code == 200
    r = await invoke(client, h["op"], "business_db.log_invoice_reminder", {**base, "note": "b"}, idempotency_key="k1")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_conflict"


# ---------------------------------------------------------------- approval ----
async def test_high_risk_tool_needs_approval_and_only_managers_can_give_it(app, client, gateway):
    h = await users(client)
    mail = {"to": "ap@cedarlogistics.example", "subject": "Payment reminder INV-1003", "body": "Friendly reminder."}

    blocked = await invoke(client, h["op"], "gmail.send_email", mail)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "approval_required"

    self_approve = await invoke(client, h["op"], "gmail.send_email", mail, approve=True)
    assert self_approve.status_code == 403                                   # operators can't approve

    sent = await invoke(client, h["mgr"], "gmail.send_email", mail, approve=True)
    assert sent.status_code == 200 and sent.json()["result"]["status"] == "sent"

    again = await invoke(client, h["mgr"], "gmail.send_email", mail, approve=True)
    assert again.json()["cached"] is True                                     # NOT sent twice
    found = (await invoke(client, h["op"], "gmail.search_emails", {"query": "Payment reminder INV-1003"})).json()
    assert found["result"]["count"] == 1

    ex = (await client.get("/api/v1/tool-executions?tool=gmail.send_email", headers=h["admin"])).json()
    assert len(ex) == 1 and ex[0]["approved_by"] is not None


async def test_policy_kill_switch_blocks_a_tool_for_everyone(app, client, gateway):
    h = await users(client)
    gateway.policy.disabled.add("gmail.send_email")
    mail = {"to": "ap@cedarlogistics.example", "subject": "s", "body": "b"}
    r = await invoke(client, h["admin"], "gmail.send_email", mail, approve=True)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "permission_denied"
    names = [t["name"] for t in (await client.get("/api/v1/tools", headers=h["admin"])).json()]
    assert "gmail.send_email" not in names


# ---------------------------------------------------------------- failure handling ----
def break_connection(gateway, server, fail_times, exc):
    conn = gateway._connections[server]
    orig, state = conn.call_tool, {"calls": 0}

    async def flaky(name, args):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc
        return await orig(name, args)

    conn.call_tool = flaky
    return state


async def test_read_tool_is_retried_after_transient_failures(app, client, gateway):
    h = await users(client)
    state = break_connection(gateway, "business_db", 2, ConnectionError("pipe broke"))
    r = await invoke(client, h["op"], "business_db.get_customer", {"customer_id": 1})
    assert r.status_code == 200 and state["calls"] == 3
    ex = (await client.get("/api/v1/tool-executions", headers=h["admin"])).json()
    assert ex[0]["status"] == "succeeded" and ex[0]["attempts"] == 3


async def test_read_tool_gives_up_after_max_retries(app, client, gateway):
    h = await users(client)
    state = break_connection(gateway, "business_db", 99, ConnectionError("down"))
    r = await invoke(client, h["op"], "business_db.get_customer", {"customer_id": 1})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "tool_unavailable" and state["calls"] == 3


async def test_write_timeout_is_marked_unknown_and_never_auto_retried(app, client, gateway):
    h = await users(client)
    state = break_connection(gateway, "gmail", 99, asyncio.TimeoutError())
    mail = {"to": "ap@cedarlogistics.example", "subject": "Reminder", "body": "b"}

    r = await invoke(client, h["mgr"], "gmail.send_email", mail, approve=True)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "outcome_unknown"
    assert state["calls"] == 1                                                # no retry of a write

    r2 = await invoke(client, h["mgr"], "gmail.send_email", mail, approve=True)
    assert r2.json()["detail"]["code"] == "outcome_unknown" and state["calls"] == 1   # refused without calling
    ex = (await client.get("/api/v1/tool-executions?status_filter=unknown", headers=h["admin"])).json()
    assert len(ex) == 1


async def test_slow_tool_times_out(app, client, gateway):
    h = await users(client)
    gateway.timeout = 0.05
    conn = gateway._connections["drive"]

    async def slow(name, args):
        await asyncio.sleep(1)

    conn.call_tool = slow
    gateway.max_retries = 0
    r = await invoke(client, h["op"], "drive.search_files", {"query": "policy"})
    assert r.status_code == 502


# ---------------------------------------------------------------- audit ----
async def test_every_tool_call_and_denial_is_audited(app, client, gateway):
    h = await users(client)
    await invoke(client, h["op"], "drive.search_files", {"query": "playbook"})
    await invoke(client, h["op"], "gmail.send_email", {"to": "a@b.example", "subject": "s", "body": "b"})  # needs approval
    await invoke(client, h["op"], "business_db.get_invoice", {"invoice_id": "x"})                         # invalid args
    logs = (await client.get("/api/v1/audit-logs?limit=200", headers=h["admin"])).json()
    seen = {(l["action"], l["status"], l["resource_id"]) for l in logs if l["resource_type"] == "tool"}
    assert ("tool.call", "success", "drive.search_files") in seen
    assert ("tool.approval_required", "denied", "gmail.send_email") in seen
    assert ("tool.invalid_arguments", "failure", "business_db.get_invoice") in seen


# ---------------------------------------------------------------- planner integration ----
async def test_planner_prompt_lists_only_tools_that_really_exist(app, client, gateway):
    h = await users(client)
    llm = FakeLLM(GOOD_PLAN)
    app.dependency_overrides[get_llm_client] = lambda: llm
    r = await client.post("/api/v1/chat", json={"message": "chase overdue invoices"}, headers=h["op"])
    assert r.status_code == 200
    prompt = llm.calls[0][0]["content"]
    assert "business_db.list_overdue_invoices [read, low risk]" in prompt
    assert "gmail.send_email [write, high risk]" in prompt
    assert "- crm:" not in prompt          # no CRM server connected -> planner is not told it exists
