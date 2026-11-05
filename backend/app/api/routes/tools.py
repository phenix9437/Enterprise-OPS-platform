from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import DB, client_ip, require_roles
import jsonschema

from app.models.approval import Approval
from app.models.chat import Task
from app.models.tooling import ToolExecution
from app.models.user import Role, User, utcnow
from app.schemas.tools import ToolExecutionRead, ToolInvokeRequest, ToolInvokeResponse, ToolRead
from app.services.audit import record_audit
from app.tools.gateway import ToolGateway
from app.services.approval_policy import ROLE_RANK
from app.tools.types import GatewayError, ToolContext

router = APIRouter(tags=["tools"])
CanUseTools = Depends(require_roles(Role.ADMIN, Role.MANAGER, Role.OPERATOR))
Approvers = {Role.ADMIN.value, Role.MANAGER.value}


def get_gateway(request: Request) -> ToolGateway:
    gw = getattr(request.app.state, "tool_gateway", None)
    if gw is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The tool layer is not running")
    return gw


@router.get("/tools", response_model=list[ToolRead])
async def list_tools(user: User = CanUseTools, gw: ToolGateway = Depends(get_gateway)):
    """Tools discovered from the MCP servers that THIS user's role may use."""
    return [ToolRead.from_spec(s) for s in gw.specs_for(user.role)]


@router.post("/tools/invoke", response_model=ToolInvokeResponse)
async def invoke_tool(
    body: ToolInvokeRequest, request: Request, db: DB,
    user: User = CanUseTools, gw: ToolGateway = Depends(get_gateway),
):
    """Manual tool call for testing/debugging. The agent (Phase 4) calls the same gateway."""
    if body.approve and user.role not in Approvers:
        record_audit(db, action="tool.approve_denied", actor_id=user.id, status="denied",
                     resource_type="tool", resource_id=body.tool, ip_address=client_ip(request))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only managers and admins can approve high-risk actions")

    if body.task_id:
        task = await db.get(Task, body.task_id)
        if task is None or (task.user_id != user.id and user.role not in Approvers):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")

    approval_id = None
    if body.approve:
        # A manager/admin pre-approving their OWN manual call. Recorded exactly like any other approval.
        try:
            spec = gw.registry.get(body.tool)
            jsonschema.validate(body.arguments, spec.input_schema)
            req = await gw.approval_requirement(db, spec, body.arguments)
        except (GatewayError, jsonschema.ValidationError):
            req = None          # unknown tool / bad arguments: the gateway reports it properly below
        if req is not None and req.required and not req.blocked:
            if ROLE_RANK.get(user.role, 0) < ROLE_RANK[req.approver_role]:
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"This call needs the '{req.approver_role}' role to approve")
            call = {"tool": spec.name, "arguments": body.arguments, "risk": spec.risk.value, "action": spec.action.value}
            approval = Approval(
                task_id=body.task_id, status="approved", reason="; ".join(req.reasons) or "manual invoke",
                reasons=req.reasons, policies=req.policies, required_role=req.approver_role,
                allow_self_approval=True, requested_by=user.id, proposed_calls=[call], final_calls=[call],
                decided_by=user.id, decided_at=utcnow(), decision_comment="Approved at invocation (manual invoke)")
            db.add(approval)
            record_audit(db, action="approval.approved", actor_id=user.id, resource_type="approval",
                         ip_address=client_ip(request), details={"manual_invoke": True, "tool": spec.name})
            await db.commit()
            approval_id = approval.id

    ctx = ToolContext(
        user_id=user.id, role=user.role, task_id=body.task_id, idempotency_key=body.idempotency_key,
        approval_id=approval_id, ip_address=client_ip(request),
    )
    try:
        res = await gw.execute(db, ctx, body.tool, body.arguments)
    except GatewayError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    return ToolInvokeResponse(execution_id=res.execution_id, tool=res.tool, cached=res.cached,
                              duration_ms=res.duration_ms, result=res.content)


@router.get("/tool-executions", response_model=list[ToolExecutionRead],
            dependencies=[Depends(require_roles(Role.ADMIN, Role.MANAGER))])
async def list_executions(db: DB, status_filter: str | None = None, tool: str | None = None,
                          limit: int = 50, offset: int = 0):
    stmt = select(ToolExecution).order_by(ToolExecution.created_at.desc()).limit(max(1, min(limit, 200))).offset(offset)
    if status_filter:
        stmt = stmt.where(ToolExecution.status == status_filter)
    if tool:
        stmt = stmt.where(ToolExecution.tool_name == tool)
    return (await db.scalars(stmt)).all()
