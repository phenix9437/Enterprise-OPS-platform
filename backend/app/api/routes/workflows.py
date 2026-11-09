import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from app.agents.runner import TaskRunner
from app.api.deps import DB, client_ip, lenient_llm, require_roles
from app.api.routes.execution import get_runner
from app.llm.client import LLMClient
from app.models.user import Role, User
from app.schemas.schedules import WorkflowRunRequest, WorkflowRunResponse
from app.services.audit import record_audit
from app.services.task_start import claim_planned_task
from app.services.workflow_runs import create_workflow_task
from app.workflows.templates import TEMPLATES

router = APIRouter(tags=["workflows"])
CanRun = Depends(require_roles(Role.ADMIN, Role.MANAGER, Role.OPERATOR))


@router.get("/workflows")
async def list_workflows(user: User = CanRun):
    """The built-in workflows and the parameters each accepts (JSON Schema)."""
    return [{"name": t.name, "title": t.title, "description": t.description, "approval": t.approval_note,
             "params_schema": t.params_model.model_json_schema()} for t in TEMPLATES.values()]


@router.post("/workflows/{name}/run", response_model=WorkflowRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_workflow(name: str, body: WorkflowRunRequest, request: Request, db: DB, user: User = CanRun,
                       runner: TaskRunner = Depends(get_runner), llm: LLMClient = Depends(lenient_llm)):
    """Create a task from a workflow template and (by default) start it. Steps that need a human pause for approval."""
    try:
        task = await create_workflow_task(db, user, name, body.params)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown workflow '{name}'") from None
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc
    record_audit(db, action="workflow.run", actor_id=user.id, resource_type="task", resource_id=str(task.id),
                 ip_address=client_ip(request), details={"workflow": name, "execute": body.execute})
    await db.commit()
    state = "planned"
    if body.execute and await claim_planned_task(db, task.id, actor_id=user.id, ip=client_ip(request)):
        runner.start(task.id, llm)
        state = "running"
    return WorkflowRunResponse(task_id=task.id, workflow=name, status=state, steps=len(task.plan["steps"]))
