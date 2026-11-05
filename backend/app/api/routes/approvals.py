import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.agents.runner import TaskRunner
from app.api.deps import DB, CurrentUser, client_ip, require_roles
from app.api.routes.execution import PRIVILEGED, get_runner
from app.api.routes.tools import get_gateway
from app.llm.client import LLMClient, get_llm_client
from app.models.approval import Approval, ApprovalPolicy
from app.models.chat import Task, TaskStatus
from app.models.user import Role, User, utcnow
from app.schemas.approvals import (
    ApprovalRead, DecisionRequest, PolicyCreate, PolicyEvaluateRequest, PolicyRead, PolicyUpdate,
)
from app.services.approval_policy import CallInfo, evaluate, load_enabled_policies
from app.services.approvals import ApprovalError, build_final_calls, can_decide, describe_changes, is_expired
from app.services.audit import record_audit
from app.tools.gateway import ToolGateway
from app.tools.types import GatewayError

router = APIRouter(tags=["approvals"])
Reviewers = Depends(require_roles(Role.ADMIN, Role.MANAGER))
AdminOnly = Depends(require_roles(Role.ADMIN))


def _read(a: Approval, user: User, instruction: str | None) -> ApprovalRead:
    ok, why = can_decide(user, a) if user.role in PRIVILEGED else (False, "only managers and admins can decide")
    out = ApprovalRead.model_validate(a)
    out.instruction, out.can_decide, out.cannot_decide_reason = instruction, ok, why
    return out


# ------------------------------------------------------------------ approvals ----
@router.get("/approvals", response_model=list[ApprovalRead])
async def list_approvals(user: CurrentUser, db: DB, status_filter: str | None = Query(default=None, alias="status"),
                         task_id: uuid.UUID | None = None, limit: int = 50, offset: int = 0):
    """Reviewers see every request; everyone else sees only requests for their own tasks."""
    stmt = select(Approval).order_by(Approval.requested_at.desc()).limit(max(1, min(limit, 200))).offset(offset)
    if user.role not in PRIVILEGED:
        stmt = stmt.where(Approval.requested_by == user.id)
    if status_filter:
        stmt = stmt.where(Approval.status == status_filter)
    if task_id:
        stmt = stmt.where(Approval.task_id == task_id)
    rows = (await db.scalars(stmt)).all()
    tasks = {t.id: t.instruction for t in (await db.scalars(select(Task).where(Task.id.in_([a.task_id for a in rows if a.task_id])))).all()} if rows else {}
    return [_read(a, user, tasks.get(a.task_id)) for a in rows]


@router.get("/approvals/{approval_id}", response_model=ApprovalRead)
async def get_approval(approval_id: uuid.UUID, user: CurrentUser, db: DB):
    a = await db.get(Approval, approval_id)
    if a is None or (a.requested_by != user.id and user.role not in PRIVILEGED):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    task = await db.get(Task, a.task_id) if a.task_id else None
    return _read(a, user, task.instruction if task else None)


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalRead)
async def decide(approval_id: uuid.UUID, body: DecisionRequest, request: Request, db: DB,
                 user: User = Reviewers, runner: TaskRunner = Depends(get_runner),
                 gw: ToolGateway = Depends(get_gateway), llm: LLMClient = Depends(get_llm_client)):
    """Approve (optionally editing or removing calls) or reject. Exactly one decision can ever win."""
    a = await db.get(Approval, approval_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    ok, why = can_decide(user, a)
    if not ok:
        if a.status != "pending":
            raise HTTPException(status.HTTP_409_CONFLICT, f"This request is {a.status}")
        record_audit(db, action="approval.denied", actor_id=user.id, resource_type="approval", resource_id=str(a.id),
                     status="denied", ip_address=client_ip(request), details={"why": why})
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, why)
    if is_expired(a):
        await runner.expire_approval(a.id, llm)
        raise HTTPException(status.HTTP_409_CONFLICT, "This request has expired")
    task = await db.get(Task, a.task_id)
    if task is None or task.status != TaskStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "The task is no longer waiting for approval")

    approve = body.decision == "approve"
    final, mods = (None, None)
    if approve:
        try:
            final, mods = await build_final_calls(db, gw, a, user, body.edits, body.remove)
        except ApprovalError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
    modified = bool(mods)

    claim = await db.execute(update(Approval).where(Approval.id == a.id, Approval.status == "pending").values(
        status="approved" if approve else "rejected", decided_by=user.id, decided_at=utcnow(),
        decision_comment=body.comment, final_calls=final if approve else None,
        modified=modified, modifications=mods or None))
    resumed = await db.execute(update(Task).where(Task.id == a.task_id, Task.status == TaskStatus.AWAITING_APPROVAL.value)
                               .values(status=TaskStatus.RUNNING.value, pending_approval=None))
    if claim.rowcount != 1 or resumed.rowcount != 1:      # somebody else decided first
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This request was just decided by someone else")
    action = "approval.rejected" if not approve else ("approval.modified" if modified else "approval.approved")
    record_audit(db, action=action, actor_id=user.id, resource_type="approval", resource_id=str(a.id),
                 ip_address=client_ip(request),
                 details={"task_id": str(a.task_id), "comment": body.comment, "tools": [c["tool"] for c in a.proposed_calls],
                          "modifications": mods or None})
    await db.commit()
    await db.refresh(a)

    runner.start(a.task_id, llm, resume={
        "approved": approve, "approver_id": str(user.id), "comment": body.comment, "approval_id": str(a.id),
        "calls": final if modified else None, "note": describe_changes(mods) if modified else None})
    return _read(a, user, task.instruction)


# ------------------------------------------------------------------- policies ----
@router.get("/approval-policies", response_model=list[PolicyRead], dependencies=[Reviewers])
async def list_policies(db: DB):
    return (await db.scalars(select(ApprovalPolicy).order_by(ApprovalPolicy.name))).all()


@router.post("/approval-policies", response_model=PolicyRead, status_code=status.HTTP_201_CREATED)
async def create_policy(body: PolicyCreate, request: Request, db: DB, admin: User = AdminOnly):
    data = body.model_dump()
    policy = ApprovalPolicy(**data, created_by=admin.id)
    db.add(policy)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"A policy named '{body.name}' already exists")
    record_audit(db, action="policy.create", actor_id=admin.id, resource_type="policy", resource_id=str(policy.id),
                 ip_address=client_ip(request), details=data)
    await db.commit()
    await db.refresh(policy)
    return policy


@router.patch("/approval-policies/{policy_id}", response_model=PolicyRead)
async def update_policy(policy_id: uuid.UUID, body: PolicyUpdate, request: Request, db: DB, admin: User = AdminOnly):
    policy = await db.get(ApprovalPolicy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    changes = body.model_dump(exclude_unset=True)
    before = {k: getattr(policy, k) for k in changes}
    for k, v in changes.items():
        setattr(policy, k, v)
    record_audit(db, action="policy.update", actor_id=admin.id, resource_type="policy", resource_id=str(policy.id),
                 ip_address=client_ip(request), details={"from": before, "to": changes})
    await db.commit()
    await db.refresh(policy)
    return policy


@router.delete("/approval-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(policy_id: uuid.UUID, request: Request, db: DB, admin: User = AdminOnly):
    policy = await db.get(ApprovalPolicy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    record_audit(db, action="policy.delete", actor_id=admin.id, resource_type="policy", resource_id=str(policy.id),
                 ip_address=client_ip(request), details={"name": policy.name})
    await db.delete(policy)
    await db.commit()


@router.post("/approval-policies/evaluate", dependencies=[Reviewers])
async def evaluate_policies(body: PolicyEvaluateRequest, db: DB, gw: ToolGateway = Depends(get_gateway)):
    """Dry run: what would happen if the agent tried this call right now?"""
    try:
        spec = gw.registry.get(body.tool)
    except GatewayError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    req = evaluate([CallInfo(0, spec.name, body.arguments, spec.risk.value, spec.action.value)], await load_enabled_policies(db))
    return {"tool": spec.name, "risk": spec.risk.value, **req.to_dict()}
