"""Runs only when TEST_DATABASE_URL points at PostgreSQL (python -m scripts.test_on_postgres)."""
import pytest

from app.agents.checkpointer import open_checkpointer
from app.agents.runner import TaskRunner
from app.agents.schemas import ExecutorDecision, FinalSummary, StepVerdict
from app.core.config import get_settings
from app.llm.client import get_llm_client
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import FakeLLM, ScriptedLLM, decide, final, plan, step, tc, verdict
from tests.test_execution import planned_task, resume, users

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs a real PostgreSQL (TEST_DATABASE_URL)")


async def test_approval_wait_survives_a_full_restart(app, client, gateway):
    settings = get_settings().model_copy(update={"checkpoint_database_url": TEST_DATABASE_URL})
    h = await users(client)
    mail = {"to": "ap@cedarlogistics.example", "subject": "Reminder INV-1003", "body": "Friendly reminder."}
    llm = (ScriptedLLM()
           .add(ExecutorDecision, decide(tc("gmail.send_email", **mail)))
           .add(StepVerdict, verdict(True, "sent"))
           .add(FinalSummary, final("Sent after restart.")))
    plan_ = plan(step(1, "Send reminder", tool="gmail", action="write", risk="high", approval=True))
    tid = await planned_task(app, client, h["op"], plan_, llm)

    # ---- "process 1": run until the approval gate, then shut everything down ----
    async with open_checkpointer(settings) as saver:
        app.state.task_runner = TaskRunner(app.state.session_factory, gateway, saver, settings)
        assert (await client.post(f"/api/v1/tasks/{tid}/execute", headers=h["op"])).status_code == 202
        await app.state.task_runner.wait_idle()
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert task["status"] == "awaiting_approval"

    # ---- "process 2": brand-new connection and runner; the checkpoint must still be in PostgreSQL ----
    async with open_checkpointer(settings) as saver2:
        app.state.task_runner = TaskRunner(app.state.session_factory, gateway, saver2, settings)
        r = await resume(client, h["mgr"], tid)
        assert r.status_code == 200
        await app.state.task_runner.wait_idle()
    done = (await client.get(f"/api/v1/tasks/{tid}", headers=h["op"])).json()
    assert done["status"] == "completed" and done["result"] == "Sent after restart."
    found = (await client.post("/api/v1/tools/invoke", json={"tool": "gmail.search_emails", "arguments": {"query": "Reminder INV-1003"}}, headers=h["op"])).json()
    assert found["result"]["count"] == 1
