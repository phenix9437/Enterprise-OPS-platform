"""The single door through which the platform calls tools.

Every call passes, in order:
  1. registry lookup           -> unknown tool? 404
  2. permission policy         -> role/tool allowed? kill-switch off?
  3. argument validation       -> JSON-schema check BEFORE anything is sent to the server
  4. policies + approval       -> blocked by an admin policy? needs a human? is there a valid approval
                                  record covering EXACTLY this call?
  5. idempotency check         -> same key already done? return the stored result, don't run again
  6. execution with timeout    -> retries ONLY for read-only/idempotent tools
  7. durable record + audit    -> ToolExecution row and an audit log entry, success or failure
"""
import asyncio
import hashlib
import json
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import jsonschema
from mcp.client import Client
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import Approval
from app.models.tooling import ExecutionStatus, ToolExecution
from app.models.user import utcnow
from app.schemas.plan import ActionType, RiskLevel, ToolCategory
from app.services.approval_policy import CallInfo, Requirement, evaluate, load_enabled_policies
from app.services.audit import record_audit
from app.tools.policy import ToolPolicy
from app.tools.registry import ToolRegistry, spec_from_mcp_tool
from app.tools.types import (
    ApprovalRequired, DuplicateInFlight, GatewayError, IdempotencyConflict, InvalidToolArguments,
    PolicyBlocked, ToolContext, ToolOutcomeUnknown, ToolPermissionDenied, ToolReportedError, ToolResult,
    ToolSpec, ToolUnavailable,
)

log = logging.getLogger("app.tools")


class _Connection:
    """One MCP server. `persistent` keeps a single long-lived client (used for stdio subprocesses);
    otherwise a client is opened per call (used for in-process servers, e.g. tests)."""

    def __init__(self, target, persistent: bool):
        self.target = target
        self.persistent = persistent
        self._client: Client | None = None

    async def open(self, stack: AsyncExitStack) -> None:
        if self.persistent:
            self._client = await stack.enter_async_context(Client(self.target))

    @asynccontextmanager
    async def _use(self):
        if self._client is not None:
            yield self._client
        else:
            async with Client(self.target) as client:
                yield client

    async def list_tools(self):
        async with self._use() as c:
            return (await c.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict):
        async with self._use() as c:
            return await c.call_tool(name, arguments)


def parse_tool_result(result) -> tuple[Any, str | None]:
    """-> (content, error_message). Prefers structured output, falls back to JSON text."""
    texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
    if result.is_error:
        return None, " ".join(texts).strip() or "Tool reported an error"
    if result.structured_content is not None:
        return result.structured_content, None
    if len(texts) == 1:
        try:
            return json.loads(texts[0]), None
        except json.JSONDecodeError:
            return texts[0], None
    return "\n".join(texts), None


def _hash_args(arguments: dict) -> str:
    canon = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


class ToolGateway:
    def __init__(self, policy: ToolPolicy | None = None, timeout: float = 30.0,
                 max_retries: int = 2, backoff: float = 0.3):
        self.policy = policy or ToolPolicy()
        self.registry = ToolRegistry()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._connections: dict[str, _Connection] = {}
        self._stack = AsyncExitStack()

    # ------------------------------------------------------------ setup ----
    async def add_server(self, name: str, category: ToolCategory, target, persistent: bool = False) -> int:
        """Connect to an MCP server, DISCOVER its tools and register them. Returns the tool count."""
        conn = _Connection(target, persistent)
        await conn.open(self._stack)
        tools = await conn.list_tools()
        self._connections[name] = conn
        for tool in tools:
            self.registry.add(spec_from_mcp_tool(name, category, tool))
        return len(tools)

    async def close(self) -> None:
        await self._stack.aclose()

    def specs_for(self, role: str) -> list[ToolSpec]:
        return [s for s in self.registry.all() if self.policy.allowed(role, s)]

    def catalog_for(self, role: str) -> str | None:
        specs = self.specs_for(role)
        return self.registry.render_catalog(specs) if specs else None

    # ---------------------------------------------------------- helpers ----
    async def _audit(self, db: AsyncSession, ctx: ToolContext, spec: ToolSpec | None, tool_name: str,
                     action: str, status: str, **details) -> None:
        record_audit(db, action=action, actor_id=ctx.user_id, resource_type="tool", resource_id=tool_name,
                     status=status, ip_address=ctx.ip_address,
                     details={"task_id": str(ctx.task_id) if ctx.task_id else None, **details})
        await db.commit()

    @staticmethod
    def _resolve_key(ctx: ToolContext, spec: ToolSpec, args_hash: str) -> str | None:
        if ctx.idempotency_key:
            return ctx.idempotency_key
        if spec.action == ActionType.WRITE:
            # Writes ALWAYS get a key: identical write, same task -> executed once.
            return f"auto:{ctx.task_id or ctx.user_id}:{spec.name}:{args_hash[:32]}"
        return None  # reads are not cached unless the caller asks for it

    async def approval_requirement(self, db: AsyncSession, spec: ToolSpec, arguments: dict) -> Requirement:
        policies = await load_enabled_policies(db)
        return evaluate([CallInfo(0, spec.name, arguments, spec.risk.value, spec.action.value)], policies)

    @staticmethod
    def _canon(arguments: dict) -> str:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)

    async def _valid_approval(self, db: AsyncSession, ctx: ToolContext, spec: ToolSpec, arguments: dict) -> Approval:
        """The graph already pauses for approval, but the gateway does not trust that: it only runs a call
        that an APPROVED record covers exactly (same tool, same arguments, same task)."""
        problem = None
        approval = await db.get(Approval, ctx.approval_id) if ctx.approval_id else None
        if approval is None:
            problem = "no approval was provided"
        elif approval.status != "approved":
            problem = f"the approval is '{approval.status}'"
        elif ctx.task_id and approval.task_id and approval.task_id != ctx.task_id:
            problem = "the approval belongs to a different task"
        elif not any(c["tool"] == spec.name and self._canon(c["arguments"]) == self._canon(arguments)
                     for c in (approval.final_calls or [])):
            problem = "this exact call was not what the reviewer approved"
        if problem:
            await self._audit(db, ctx, spec, spec.name, "tool.approval_required", "denied", problem=problem)
            raise ApprovalRequired(f"'{spec.name}' needs human approval: {problem}")
        return approval

    # ---------------------------------------------------------- execute ----
    async def execute(self, db: AsyncSession, ctx: ToolContext, tool_name: str, arguments: dict) -> ToolResult:
        spec = self.registry.get(tool_name)

        # 2. permissions
        try:
            self.policy.check(ctx.role, spec)
        except ToolPermissionDenied as exc:
            await self._audit(db, ctx, spec, tool_name, "tool.denied", "denied", reason=str(exc))
            raise

        # 3. arguments
        try:
            jsonschema.validate(instance=arguments, schema=spec.input_schema)
        except jsonschema.ValidationError as exc:
            await self._audit(db, ctx, spec, tool_name, "tool.invalid_arguments", "failure", reason=exc.message)
            raise InvalidToolArguments(exc.message) from exc

        # 4. admin policies + human approval
        req = await self.approval_requirement(db, spec, arguments)
        if req.blocked:
            b = req.blocked[0]
            await self._audit(db, ctx, spec, tool_name, "tool.blocked_by_policy", "denied", policy=b["policy"])
            raise PolicyBlocked(f"Blocked by policy '{b['policy']}': {b['message']}")
        approval = await self._valid_approval(db, ctx, spec, arguments) if req.required else None

        # 5. idempotency
        args_hash = _hash_args(arguments)
        key = self._resolve_key(ctx, spec, args_hash)
        row: ToolExecution | None = None
        if key:
            row = await db.scalar(select(ToolExecution).where(ToolExecution.idempotency_key == key))
            if row is not None:
                if row.tool_name != spec.name or row.arguments_hash != args_hash:
                    raise IdempotencyConflict("This idempotency key was already used for a different call")
                if row.status == ExecutionStatus.SUCCEEDED.value:
                    await self._audit(db, ctx, spec, tool_name, "tool.call", "success",
                                      execution_id=str(row.id), cached=True)
                    return ToolResult(row.id, tool_name, row.result, cached=True, duration_ms=row.duration_ms)
                if row.status == ExecutionStatus.RUNNING.value:
                    raise DuplicateInFlight("The same call is already in progress")
                if row.status == ExecutionStatus.UNKNOWN.value:
                    raise ToolOutcomeUnknown(
                        "An earlier attempt may or may not have taken effect. A human must check "
                        "the target system before this is repeated.")
                # FAILED -> we know it did not take effect, so it is safe to try again below
        if row is None:
            row = ToolExecution(
                idempotency_key=key, tool_name=spec.name, arguments=arguments, arguments_hash=args_hash,
                user_id=ctx.user_id, task_id=ctx.task_id,
                approved_by=approval.decided_by if approval else None, approval_id=approval.id if approval else None,
                status=ExecutionStatus.RUNNING.value)
            db.add(row)
            try:
                await db.commit()   # persisted BEFORE the call, so a crash leaves a visible 'running' row
            except IntegrityError:
                await db.rollback()
                raise DuplicateInFlight("The same call is already in progress") from None
        else:
            row.status, row.error = ExecutionStatus.RUNNING.value, None
            await db.commit()

        # 6. run
        conn = self._connections[spec.server]
        allowed_attempts = 1 + (self.max_retries if spec.safe_to_retry else 0)
        started = time.perf_counter()
        last_exc: Exception | None = None
        for attempt in range(allowed_attempts):
            row.attempts += 1
            try:
                raw = await asyncio.wait_for(conn.call_tool(spec.remote_name, arguments), self.timeout)
            except Exception as exc:  # timeouts, broken pipes, protocol errors
                last_exc = exc
                log.warning("tool %s attempt %d failed: %r", tool_name, attempt + 1, exc)
                if attempt + 1 < allowed_attempts:
                    await asyncio.sleep(self.backoff * (2 ** attempt))
                continue
            content, error = parse_tool_result(raw)
            row.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            row.completed_at = utcnow()
            if error is not None:   # the tool ran and refused: deterministic, never retried
                row.status, row.error = ExecutionStatus.FAILED.value, error
                await db.commit()
                await self._audit(db, ctx, spec, tool_name, "tool.call", "failure",
                                  execution_id=str(row.id), error=error)
                raise ToolReportedError(error)
            row.status, row.result = ExecutionStatus.SUCCEEDED.value, content
            await db.commit()
            await self._audit(db, ctx, spec, tool_name, "tool.call", "success", execution_id=str(row.id),
                              attempts=row.attempts, duration_ms=row.duration_ms, args_hash=args_hash)
            return ToolResult(row.id, tool_name, content, cached=False, duration_ms=row.duration_ms)

        # every attempt failed at the transport level
        row.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        row.completed_at = utcnow()
        row.error = f"{type(last_exc).__name__}: {last_exc}"
        if spec.action == ActionType.WRITE:
            # We don't know whether the write landed. Never auto-retry; surface it to a human.
            row.status = ExecutionStatus.UNKNOWN.value
            await db.commit()
            await self._audit(db, ctx, spec, tool_name, "tool.call", "failure",
                              execution_id=str(row.id), outcome="unknown", error=row.error)
            raise ToolOutcomeUnknown(
                f"'{tool_name}' did not return a result ({row.error}); it may or may not have taken effect.")
        row.status = ExecutionStatus.FAILED.value
        await db.commit()
        await self._audit(db, ctx, spec, tool_name, "tool.call", "failure",
                          execution_id=str(row.id), attempts=row.attempts, error=row.error)
        raise ToolUnavailable(f"'{tool_name}' is unavailable after {row.attempts} attempts ({row.error})")


__all__ = ["ToolGateway", "GatewayError", "parse_tool_result"]
