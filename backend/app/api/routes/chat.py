import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, client_ip, require_roles
from app.core.config import get_settings
from app.llm.client import LLMClient, get_llm_client
from app.models.chat import Conversation, Message, Task, TaskStatus
from app.models.user import Role, User
from app.schemas.chat import ChatRequest, ChatResponse, ConversationRead, MessageRead, TaskRead
from app.services.audit import record_audit
from app.services.planner import PlanningFailed, create_plan, render_reply

router = APIRouter(tags=["chat"])

# Viewers are read-only: they cannot ask the agent to do (or plan) anything.
CanIssueTasks = Depends(require_roles(Role.ADMIN, Role.MANAGER, Role.OPERATOR))
SEES_ALL_TASKS = {Role.ADMIN.value, Role.MANAGER.value}


async def _owned_conversation(db, conversation_id: uuid.UUID, user: User) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:  # same 404 either way: don't leak existence
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    db: DB,
    user: User = CanIssueTasks,
    llm: LLMClient = Depends(get_llm_client),
):
    s = get_settings()

    # 1. Conversation (new or existing) + conversation history for context
    if body.conversation_id:
        conv = await _owned_conversation(db, body.conversation_id, user)
        past = (
            await db.scalars(
                select(Message).where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc()).limit(s.chat_history_messages)
            )
        ).all()
        history = [{"role": m.role, "content": m.content} for m in reversed(past)]
    else:
        conv = Conversation(user_id=user.id, title=body.message[:60])
        db.add(conv)
        await db.flush()
        history = []

    # 2. Persist the user's message and a Task row BEFORE calling the LLM,
    #    so the request is never lost even if the model call fails.
    task = Task(user_id=user.id, conversation_id=conv.id, instruction=body.message)
    db.add(task)
    await db.flush()
    db.add(Message(conversation_id=conv.id, role="user", content=body.message, task_id=task.id))
    record_audit(
        db, action="task.create", actor_id=user.id, resource_type="task",
        resource_id=str(task.id), ip_address=client_ip(request),
    )
    await db.commit()

    # 3. Ask the LLM for a plan (with validation + one repair attempt)
    try:
        outcome = await create_plan(llm, body.message, history)
    except PlanningFailed as exc:
        task.status = TaskStatus.FAILED.value
        task.error = str(exc)
        task.llm_calls = exc.calls
        task.prompt_tokens = sum(u.prompt_tokens for u in exc.usages)
        task.completion_tokens = sum(u.completion_tokens for u in exc.usages)
        record_audit(
            db, action="task.plan", actor_id=user.id, resource_type="task",
            resource_id=str(task.id), status="failure", details={"error": str(exc)},
        )
        db.add(Message(
            conversation_id=conv.id, role="assistant", task_id=task.id,
            content="Sorry, I couldn't produce a plan for that request. Please try rephrasing it.",
        ))
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Planning failed: {exc}")

    # 4. Store the result
    plan = outcome.plan
    task.plan = plan.model_dump(mode="json")
    task.intent = plan.intent.value
    task.status = (TaskStatus.NEEDS_CLARIFICATION if plan.requires_clarification else TaskStatus.PLANNED).value
    task.llm_model = outcome.model
    task.llm_calls = outcome.calls
    task.prompt_tokens = outcome.prompt_tokens
    task.completion_tokens = outcome.completion_tokens
    task.llm_latency_ms = outcome.latency_ms

    reply = render_reply(plan)
    db.add(Message(conversation_id=conv.id, role="assistant", content=reply, task_id=task.id))
    record_audit(
        db, action="task.plan", actor_id=user.id, resource_type="task", resource_id=str(task.id),
        details={"intent": task.intent, "steps": len(plan.steps), "status": task.status},
    )
    await db.commit()

    return ChatResponse(conversation_id=conv.id, reply=reply, task=TaskRead.model_validate(task))


@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(user: CurrentUser, db: DB, status_filter: str | None = None, limit: int = 50, offset: int = 0):
    stmt = select(Task).order_by(Task.created_at.desc()).limit(max(1, min(limit, 200))).offset(offset)
    if user.role not in SEES_ALL_TASKS:
        stmt = stmt.where(Task.user_id == user.id)
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    return (await db.scalars(stmt)).all()


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, user: CurrentUser, db: DB):
    task = await db.get(Task, task_id)
    if task is None or (task.user_id != user.id and user.role not in SEES_ALL_TASKS):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return task


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(user: CurrentUser, db: DB):
    stmt = select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.created_at.desc())
    return (await db.scalars(stmt)).all()


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def conversation_messages(conversation_id: uuid.UUID, user: CurrentUser, db: DB):
    conv = await _owned_conversation(db, conversation_id, user)
    stmt = select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
    return (await db.scalars(stmt)).all()
