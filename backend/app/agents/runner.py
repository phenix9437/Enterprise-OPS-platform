"""Starts, resumes and recovers task runs, and mirrors graph progress into the Task row."""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from langgraph.types import Command
from sqlalchemy import select, update

from app.agents.graph import Ctx, GraphDeps, build_graph
from app.core.config import Settings
from app.llm.client import LLMClient
from app.models.approval import Approval
from app.models.chat import Task, TaskStatus
from app.models.tooling import ExecutionStatus, ToolExecution
from app.models.user import User, utcnow
from app.schemas.plan import ActionType
from app.services.audit import record_audit
from app.tools.gateway import ToolGateway
from app.tools.types import ToolNotFound

log = logging.getLogger("app.agents")

OUTCOME_TO_STATUS = {
    "completed": TaskStatus.COMPLETED, "failed": TaskStatus.FAILED,
    "rejected": TaskStatus.REJECTED, "expired": TaskStatus.EXPIRED, "needs_attention": TaskStatus.NEEDS_ATTENTION,
}


class TaskRunner:
    def __init__(self, session_factory, gateway: ToolGateway, checkpointer, settings: Settings):
        self.sf = session_factory
        self.gateway = gateway
        self.checkpointer = checkpointer
        self.settings = settings
        self._bg: set[asyncio.Task] = set()

    # ---------------------------------------------------------- lifecycle ----
    def start(self, task_id: uuid.UUID, llm: LLMClient, resume: dict | None = None, recover: bool = False) -> asyncio.Task:
        t = asyncio.create_task(self.run(task_id, llm, resume=resume, recover=recover), name=f"task-{task_id}")
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    async def wait_idle(self) -> None:
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    async def shutdown(self) -> None:
        for t in list(self._bg):
            t.cancel()
        await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ---------------------------------------------------------------- run ----
    async def run(self, task_id: uuid.UUID, llm: LLMClient, resume: dict | None = None, recover: bool = False) -> None:
        deps = GraphDeps(self.sf, self.gateway, llm, self.settings)
        cx = Ctx(deps)
        stub = {"task_id": str(task_id)}
        async with self.sf() as db:
            task = await db.get(Task, task_id)
            owner = await db.get(User, task.user_id)
            plan, instruction = task.plan, task.instruction

        graph = build_graph(deps, self.checkpointer)
        n_steps = len(plan["steps"])
        config = {"configurable": {"thread_id": str(task_id)},
                  "recursion_limit": 25 + n_steps * 6 * self.settings.step_max_attempts}
        try:
            if resume is not None:
                label = "Expired" if resume.get("expired") else ("Approved" if resume["approved"] else "Rejected")
                await cx.emit(stub, "approval_decision", f"{label} by {resume['approver_id']}",
                              data={"approved": resume["approved"], "comment": resume.get("comment"),
                                    "modified": bool(resume.get("calls"))})
                graph_input = Command(resume=resume)
            elif recover:
                snap = await graph.aget_state(config)
                if not snap.values:
                    await self._finish(task_id, TaskStatus.FAILED, error="Interrupted by a restart and no checkpoint was found")
                    return
                await cx.emit(stub, "task_recovered", "Resuming from the last checkpoint")
                graph_input = None
            else:
                await cx.emit(stub, "task_started", f"Executing {n_steps} step(s)")
                graph_input = {
                    "task_id": str(task_id), "user_id": str(owner.id), "user_role": owner.role,
                    "instruction": instruction, "plan": plan, "step_index": 0, "attempt": 1,
                    "step_results": {}, "outcome": None, "error": None,
                }
            await graph.ainvoke(graph_input, config)
            snap = await graph.aget_state(config)
        except asyncio.CancelledError:
            raise  # e.g. shutdown: leave status 'running'; recover_interrupted() picks it up next start
        except Exception as exc:
            log.exception("task %s crashed", task_id)
            await self._finish(task_id, TaskStatus.FAILED, error=f"Internal error: {type(exc).__name__}: {exc}")
            await cx.emit(stub, "task_failed", f"Internal error: {type(exc).__name__}")
            return
        await self._apply(task_id, snap, cx)

    async def _apply(self, task_id: uuid.UUID, snap, cx: Ctx) -> None:
        stub = {"task_id": str(task_id)}
        if snap.next:  # paused at the approval gate -> open an Approval request
            payload = next(i.value for t in snap.tasks for i in t.interrupts)
            req = payload["approval"]
            async with self.sf() as db:
                task = await db.get(Task, task_id)
                approval = Approval(
                    task_id=task_id, step_id=payload["step"]["id"], attempt=payload["attempt"],
                    reason=payload["reason"], reasons=req["reasons"], policies=req["policies"],
                    required_role=req["approver_role"], allow_self_approval=req["allow_self_approval"],
                    requested_by=task.user_id, expires_at=utcnow() + timedelta(minutes=req["expires_in_minutes"]),
                    proposed_calls=payload["calls"])
                db.add(approval)
                await db.flush()
                payload = {**payload, "approval_id": str(approval.id)}
                task.status, task.pending_approval = TaskStatus.AWAITING_APPROVAL.value, payload
                record_audit(db, action="approval.requested", actor_id=task.user_id, resource_type="approval",
                             resource_id=str(approval.id), details={"task_id": str(task_id), "step": payload["step"]["id"],
                                                                     "tools": [c["tool"] for c in payload["calls"]]})
                await db.commit()
            await cx.emit(stub, "approval_requested", f"Waiting for approval: {payload['reason']}",
                          payload["step"]["id"], {"approval_id": payload["approval_id"], "calls": [c["tool"] for c in payload["calls"]]})
            return
        v = snap.values
        outcome = v.get("outcome") or "completed"
        await self._finish(task_id, OUTCOME_TO_STATUS[outcome], error=v.get("error"), result=v.get("final_summary"))
        await cx.emit(stub, f"task_{outcome}", (v.get("final_summary") or outcome)[:300])

    async def _finish(self, task_id: uuid.UUID, status: TaskStatus, error: str | None = None, result: str | None = None):
        async with self.sf() as db:
            await db.execute(update(Task).where(Task.id == task_id).values(
                status=status.value, error=error, result=result, pending_approval=None,
                current_step=None, finished_at=utcnow()))
            await db.commit()

    # ----------------------------------------------------------- recovery ----
    async def recover_interrupted(self, llm: LLMClient) -> int:
        """After a restart: continue every task that was 'running' from its last checkpoint."""
        async with self.sf() as db:
            ids = (await db.scalars(select(Task.id).where(Task.status == TaskStatus.RUNNING.value))).all()
        for tid in ids:
            await self._settle_inflight_calls(tid)
            self.start(tid, llm, recover=True)
        return len(ids)

    async def _settle_inflight_calls(self, task_id: uuid.UUID) -> None:
        """Nothing is in flight after a restart. A read that was cut off just failed; a WRITE that was
        cut off might have happened, so it becomes 'unknown' and will never be repeated automatically."""
        async with self.sf() as db:
            rows = (await db.scalars(select(ToolExecution).where(
                ToolExecution.task_id == task_id, ToolExecution.status == ExecutionStatus.RUNNING.value))).all()
            for r in rows:
                try:
                    is_write = self.gateway.registry.get(r.tool_name).action == ActionType.WRITE
                except ToolNotFound:
                    is_write = True
                r.status = (ExecutionStatus.UNKNOWN if is_write else ExecutionStatus.FAILED).value
                r.error = "Interrupted by a restart"
                r.completed_at = utcnow()
            await db.commit()

    # ----------------------------------------------------------- expiry ----
    async def expire_approval(self, approval_id: uuid.UUID, llm: LLMClient) -> bool:
        """Close an unanswered request and end its task as 'expired'. Returns False if someone beat us to it."""
        async with self.sf() as db:
            res = await db.execute(update(Approval).where(Approval.id == approval_id, Approval.status == "pending").values(
                status="expired", decided_at=utcnow(), decision_comment="Approval request expired"))
            if res.rowcount != 1:
                return False
            approval = await db.get(Approval, approval_id)
            await db.execute(update(Task).where(Task.id == approval.task_id, Task.status == TaskStatus.AWAITING_APPROVAL.value)
                             .values(status=TaskStatus.RUNNING.value, pending_approval=None))
            record_audit(db, action="approval.expired", resource_type="approval", resource_id=str(approval_id),
                         status="failure", details={"task_id": str(approval.task_id)})
            await db.commit()
        self.start(approval.task_id, llm, resume={"approved": False, "expired": True, "approver_id": "system",
                                                  "comment": "Approval request expired"})
        return True

    async def expire_overdue_approvals(self, llm: LLMClient) -> int:
        async with self.sf() as db:
            ids = (await db.scalars(select(Approval.id).where(
                Approval.status == "pending", Approval.expires_at <= utcnow()))).all()
        done = 0
        for aid in ids:
            done += await self.expire_approval(aid, llm)
        return done
