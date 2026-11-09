"""Starts, resumes and recovers task runs, and mirrors graph progress into the Task row.

Reliability model (works the same in-process and under Celery):
  * OUTBOX  - whoever makes a task runnable (execute / approval decision / expiry / scheduler) writes
              `pending_dispatch` in the SAME transaction that flips status to 'running', then pokes a
              worker. If the poke is lost (Redis down, worker crash) the reaper re-sends it.
  * LEASE   - a worker must hold a short lease on the task (renewed by heartbeat). Two workers can never
              run the same task, and a dead worker's lease simply expires.
  * IDEMPOTENT RUN - what to do (fresh / resume / continue) is decided from the checkpoint, not from the
              message, so duplicate or late deliveries are harmless.
"""
import asyncio
import logging
import os
import socket
import uuid
from datetime import timedelta

from langgraph.types import Command
from sqlalchemy import or_, select, update

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
_RELEASE = dict(lock_owner=None, lock_expires_at=None, pending_dispatch=None)


class TaskRunner:
    def __init__(self, session_factory, gateway: ToolGateway, checkpointer, settings: Settings):
        self.sf = session_factory
        self.gateway = gateway
        self.checkpointer = checkpointer
        self.settings = settings
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._bg: set[asyncio.Task] = set()
        # how work is handed to a worker. In-process: run it here. Celery: put it on the queue (see app/worker).
        self.dispatch = lambda task_id, llm: self.start(task_id, llm)

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

    # -------------------------------------------------------------- lease ----
    async def _claim(self, task_id: uuid.UUID) -> dict | None:
        """Take the lease. Returns what the run needs, or None if the task isn't runnable / is held by someone else."""
        now = utcnow()
        async with self.sf() as db:
            res = await db.execute(update(Task).where(
                Task.id == task_id, Task.status == TaskStatus.RUNNING.value,
                or_(Task.lock_owner.is_(None), Task.lock_expires_at <= now),
            ).values(lock_owner=self.owner, lock_expires_at=now + timedelta(seconds=self.settings.worker_lease_seconds)))
            if res.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            task = await db.get(Task, task_id)
            owner = await db.get(User, task.user_id)
            return {"plan": task.plan, "instruction": task.instruction, "pending": task.pending_dispatch or {},
                    "user_id": str(owner.id), "user_role": owner.role}

    async def _heartbeat(self, task_id: uuid.UUID, victim: asyncio.Task) -> None:
        lease = self.settings.worker_lease_seconds
        while True:
            await asyncio.sleep(max(lease / 3, 0.05))
            async with self.sf() as db:
                res = await db.execute(update(Task).where(Task.id == task_id, Task.lock_owner == self.owner)
                                       .values(lock_expires_at=utcnow() + timedelta(seconds=lease)))
                await db.commit()
            if res.rowcount != 1:   # someone else took over: stop immediately so two runners never overlap
                log.warning("lost the lease on task %s; abandoning this run", task_id)
                victim.cancel()
                return

    async def _release(self, task_id: uuid.UUID) -> None:
        async with self.sf() as db:
            await db.execute(update(Task).where(Task.id == task_id, Task.lock_owner == self.owner)
                             .values(lock_owner=None, lock_expires_at=None))
            await db.commit()

    # ---------------------------------------------------------------- run ----
    async def run(self, task_id: uuid.UUID, llm: LLMClient, resume: dict | None = None, recover: bool = False) -> None:
        claimed = await self._claim(task_id)
        if claimed is None:
            return
        heartbeat = asyncio.create_task(self._heartbeat(task_id, asyncio.current_task()))
        try:
            await self._run_claimed(task_id, llm, claimed, resume, recover)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._release(task_id)   # graceful stop/cancel frees the task for another worker immediately

    async def _run_claimed(self, task_id, llm, claimed: dict, resume: dict | None, recover: bool) -> None:
        deps = GraphDeps(self.sf, self.gateway, llm, self.settings)
        cx = Ctx(deps)
        stub = {"task_id": str(task_id)}
        plan = claimed["plan"]
        graph = build_graph(deps, self.checkpointer)
        n_steps = len(plan["steps"])
        config = {"configurable": {"thread_id": str(task_id)},
                  "recursion_limit": 25 + n_steps * 6 * self.settings.step_max_attempts}
        try:
            snap = await graph.aget_state(config)
            at_gate = bool(snap.next) and any(t.interrupts for t in snap.tasks)
            pending_resume = resume if resume is not None else claimed["pending"].get("resume")
            if pending_resume is not None and at_gate:
                label = "Expired" if pending_resume.get("expired") else ("Approved" if pending_resume["approved"] else "Rejected")
                await cx.emit(stub, "approval_decision", f"{label} by {pending_resume['approver_id']}",
                              data={"approved": pending_resume["approved"], "comment": pending_resume.get("comment"),
                                    "modified": bool(pending_resume.get("calls"))})
                graph_input = Command(resume=pending_resume)
            elif snap.values:   # crash recovery, duplicate delivery, or a resume that was already consumed
                await cx.emit(stub, "task_recovered", "Resuming from the last checkpoint")
                graph_input = None
            elif recover:
                await self._finish(task_id, TaskStatus.FAILED, error="Interrupted by a restart and no checkpoint was found")
                return
            else:
                await cx.emit(stub, "task_started", f"Executing {n_steps} step(s)")
                graph_input = {
                    "task_id": str(task_id), "user_id": claimed["user_id"], "user_role": claimed["user_role"],
                    "instruction": claimed["instruction"], "plan": plan, "step_index": 0, "attempt": 1,
                    "step_results": {}, "outputs": {}, "warnings": [], "outcome": None, "error": None,
                }
            await graph.ainvoke(graph_input, config)
            snap = await graph.aget_state(config)
        except asyncio.CancelledError:
            raise  # shutdown or lost lease: leave the task 'running' for the reaper / next worker
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
                approval = await db.scalar(select(Approval).where(
                    Approval.task_id == task_id, Approval.step_id == payload["step"]["id"],
                    Approval.attempt == payload["attempt"], Approval.status == "pending"))
                created = approval is None
                if created:   # (not created again if a recovered run lands on the same gate)
                    approval = Approval(
                        task_id=task_id, step_id=payload["step"]["id"], attempt=payload["attempt"],
                        reason=payload["reason"], reasons=req["reasons"], policies=req["policies"],
                        required_role=req["approver_role"], allow_self_approval=req["allow_self_approval"],
                        requested_by=task.user_id, expires_at=utcnow() + timedelta(minutes=req["expires_in_minutes"]),
                        proposed_calls=payload["calls"])
                    db.add(approval)
                    await db.flush()
                payload = {**payload, "approval_id": str(approval.id)}
                # status change AND lease release in one commit: the moment a reviewer can act, the lease is free
                task.status, task.pending_approval = TaskStatus.AWAITING_APPROVAL.value, payload
                task.lock_owner = task.lock_expires_at = task.pending_dispatch = None
                if created:
                    record_audit(db, action="approval.requested", actor_id=task.user_id, resource_type="approval",
                                 resource_id=str(approval.id), details={"task_id": str(task_id), "step": payload["step"]["id"],
                                                                         "tools": [c["tool"] for c in payload["calls"]]})
                await db.commit()
            if created:
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
                current_step=None, finished_at=utcnow(), **_RELEASE))
            await db.commit()

    # ----------------------------------------------------------- recovery ----
    def _unleased(self, now):
        return or_(Task.lock_owner.is_(None), Task.lock_expires_at <= now)

    async def recover_interrupted(self, llm: LLMClient) -> int:
        """At startup: continue every 'running' task that no live worker holds a lease on."""
        async with self.sf() as db:
            ids = (await db.scalars(select(Task.id).where(Task.status == TaskStatus.RUNNING.value,
                                                          self._unleased(utcnow())))).all()
        for tid in ids:
            await self._settle_inflight_calls(tid)
            self.dispatch(tid, llm)
        return len(ids)

    async def reap_stale_tasks(self, llm: LLMClient) -> list[uuid.UUID]:
        """Periodic safety net: a task that is 'running' but has had no live lease for a while was lost
        (dead worker, lost queue message, Redis outage). Settle its cut-off calls and dispatch it again."""
        now = utcnow()
        cutoff = now - timedelta(seconds=self.settings.worker_stale_grace_seconds)
        async with self.sf() as db:
            ids = list((await db.scalars(select(Task.id).where(
                Task.status == TaskStatus.RUNNING.value, self._unleased(now), Task.updated_at <= cutoff))).all())
        for tid in ids:
            await self._settle_inflight_calls(tid)
            self.dispatch(tid, llm)
        return ids

    async def _settle_inflight_calls(self, task_id: uuid.UUID) -> None:
        """Nothing is in flight once a worker is gone. A read that was cut off just failed; a WRITE that was
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
                r.error = "Interrupted: the worker running this call is gone"
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
                             .values(status=TaskStatus.RUNNING.value, pending_approval=None, pending_dispatch={"resume": {
                                 "approved": False, "expired": True, "approver_id": "system", "comment": "Approval request expired"}}))
            record_audit(db, action="approval.expired", resource_type="approval", resource_id=str(approval_id),
                         status="failure", details={"task_id": str(approval.task_id)})
            await db.commit()
        self.dispatch(approval.task_id, llm)
        return True

    async def expire_overdue_approvals(self, llm: LLMClient) -> int:
        async with self.sf() as db:
            ids = (await db.scalars(select(Approval.id).where(
                Approval.status == "pending", Approval.expires_at <= utcnow()))).all()
        done = 0
        for aid in ids:
            done += await self.expire_approval(aid, llm)
        return done
