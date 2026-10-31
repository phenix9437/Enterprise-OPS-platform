from app.llm.client import LLMError, get_llm_client
from app.schemas.plan import ActionType, RiskLevel, ToolCategory
from tests.conftest import login_headers, register
from tests.fakes import GOOD_PLAN, FakeLLM, clarification, plan, step


async def make_users(client):
    """admin + operator(op) + viewer(vic) + second operator(op2). Returns auth headers."""
    await register(client, "admin@example.com")
    ids = {}
    for name in ("op", "vic", "op2"):
        ids[name] = (await register(client, f"{name}@example.com")).json()["id"]
    admin = await login_headers(client, "admin@example.com")
    for name in ("op", "op2"):
        r = await client.patch(f"/api/v1/users/{ids[name]}/role", json={"role": "operator"}, headers=admin)
        assert r.status_code == 200
    return {n: await login_headers(client, f"{n}@example.com") for n in ("op", "vic", "op2")} | {"admin": admin}


def use_llm(app, fake):
    app.dependency_overrides[get_llm_client] = lambda: fake
    return fake


async def test_chat_creates_conversation_task_and_stores_plan(app, client):
    llm = use_llm(app, FakeLLM(GOOD_PLAN))
    h = await make_users(client)

    r = await client.post("/api/v1/chat", json={"message": "Remind customers with overdue invoices"}, headers=h["op"])
    assert r.status_code == 200, r.text
    body = r.json()
    task = body["task"]
    assert task["status"] == "planned" and task["intent"] == "invoice_followup"
    assert [s["id"] for s in task["plan"]["steps"]] == [1, 2, 3]
    assert task["llm_model"] == "fake-model" and task["prompt_tokens"] == 100 and task["llm_calls"] == 1
    assert "Find overdue invoices" in body["reply"] and "[needs your approval]" in body["reply"]
    assert len(llm.calls) == 1 and llm.calls[0][0]["role"] == "system"

    msgs = (await client.get(f"/api/v1/conversations/{body['conversation_id']}/messages", headers=h["op"])).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]


async def test_viewer_cannot_chat(app, client):
    use_llm(app, FakeLLM(GOOD_PLAN))
    h = await make_users(client)
    r = await client.post("/api/v1/chat", json={"message": "hello"}, headers=h["vic"])
    assert r.status_code == 403


async def test_chat_requires_auth_and_valid_body(app, client):
    use_llm(app, FakeLLM(GOOD_PLAN))
    h = await make_users(client)
    assert (await client.post("/api/v1/chat", json={"message": "x"})).status_code == 401
    assert (await client.post("/api/v1/chat", json={"message": "   "}, headers=h["op"])).status_code == 422
    assert (await client.post("/api/v1/chat", json={"message": "x" * 5000}, headers=h["op"])).status_code == 422


async def test_clarification_flow_and_history_is_sent_on_followup(app, client):
    llm = use_llm(app, FakeLLM(clarification(), GOOD_PLAN))
    h = await make_users(client)

    first = (await client.post("/api/v1/chat", json={"message": "Follow up with customers"}, headers=h["op"])).json()
    assert first["task"]["status"] == "needs_clarification"
    assert first["reply"] == "Which customers should I include?"

    second = await client.post(
        "/api/v1/chat",
        json={"message": "Only those overdue by 30+ days", "conversation_id": first["conversation_id"]},
        headers=h["op"],
    )
    assert second.json()["task"]["status"] == "planned"
    # the 2nd LLM call must contain the earlier exchange
    contents = [m["content"] for m in llm.calls[1]]
    assert "Follow up with customers" in contents and "Which customers should I include?" in contents


async def test_invalid_plan_is_repaired_on_second_attempt(app, client):
    bad = plan(step(1, depends_on=[2]), step(2))
    llm = use_llm(app, FakeLLM(bad, GOOD_PLAN))
    h = await make_users(client)

    r = await client.post("/api/v1/chat", json={"message": "do the thing"}, headers=h["op"])
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["status"] == "planned" and task["llm_calls"] == 2 and task["prompt_tokens"] == 200
    assert "does not appear earlier" in llm.calls[1][-1]["content"]  # error was fed back to the model


async def test_plan_invalid_twice_marks_task_failed(app, client):
    bad = plan(step(1, depends_on=[2]), step(2))
    use_llm(app, FakeLLM(bad, bad))
    h = await make_users(client)

    r = await client.post("/api/v1/chat", json={"message": "do the thing"}, headers=h["op"])
    assert r.status_code == 502
    tasks = (await client.get("/api/v1/tasks", headers=h["op"])).json()
    assert len(tasks) == 1 and tasks[0]["status"] == "failed" and tasks[0]["llm_calls"] == 2
    assert "valid plan" in tasks[0]["error"]


async def test_llm_outage_marks_task_failed_and_is_audited(app, client):
    use_llm(app, FakeLLM(LLMError("OpenAI request failed: APITimeoutError")))
    h = await make_users(client)

    r = await client.post("/api/v1/chat", json={"message": "do the thing"}, headers=h["op"])
    assert r.status_code == 502
    task = (await client.get("/api/v1/tasks", headers=h["op"])).json()[0]
    assert task["status"] == "failed"

    logs = (await client.get("/api/v1/audit-logs", headers=h["admin"])).json()
    assert any(l["action"] == "task.plan" and l["status"] == "failure" for l in logs)
    assert any(l["action"] == "task.create" for l in logs)


async def test_server_enforces_approval_on_email_send_even_if_llm_says_no(app, client):
    sneaky = plan(step(1, "Send emails", tool=ToolCategory.GMAIL, action=ActionType.WRITE,
                       risk=RiskLevel.LOW, approval=False))
    use_llm(app, FakeLLM(sneaky))
    h = await make_users(client)

    s = (await client.post("/api/v1/chat", json={"message": "email everyone"}, headers=h["op"])).json()["task"]["plan"]["steps"][0]
    assert s["risk"] == "high" and s["requires_approval"] is True


async def test_task_visibility_rules(app, client):
    use_llm(app, FakeLLM(GOOD_PLAN))
    h = await make_users(client)
    task_id = (await client.post("/api/v1/chat", json={"message": "go"}, headers=h["op"])).json()["task"]["id"]

    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=h["op"])).status_code == 200
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=h["admin"])).status_code == 200
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=h["op2"])).status_code == 404
    assert (await client.get("/api/v1/tasks", headers=h["op2"])).json() == []


async def test_cannot_use_someone_elses_conversation(app, client):
    use_llm(app, FakeLLM(GOOD_PLAN, GOOD_PLAN))
    h = await make_users(client)
    conv = (await client.post("/api/v1/chat", json={"message": "go"}, headers=h["op"])).json()["conversation_id"]

    r = await client.post("/api/v1/chat", json={"message": "hi", "conversation_id": conv}, headers=h["op2"])
    assert r.status_code == 404
    assert (await client.get(f"/api/v1/conversations/{conv}/messages", headers=h["op2"])).status_code == 404


async def test_unsupported_request_returns_polite_reply_without_steps(app, client):
    unsupported = plan(intent="unsupported", objective="Booking flights is not supported.")
    use_llm(app, FakeLLM(unsupported))
    h = await make_users(client)
    body = (await client.post("/api/v1/chat", json={"message": "book me a flight"}, headers=h["op"])).json()
    assert body["task"]["status"] == "planned" and body["task"]["plan"]["steps"] == []
    assert "can't do that" in body["reply"]


async def test_missing_api_key_gives_503_not_500(client):
    # no dependency override -> real get_llm_client with empty OPENAI_API_KEY
    h = await make_users(client)
    r = await client.post("/api/v1/chat", json={"message": "hello"}, headers=h["op"])
    assert r.status_code == 503
