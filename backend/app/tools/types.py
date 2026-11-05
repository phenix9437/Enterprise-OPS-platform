import uuid
from dataclasses import dataclass
from typing import Any

from app.schemas.plan import ActionType, RiskLevel, ToolCategory


@dataclass(frozen=True)
class ToolSpec:
    """Everything the platform knows about one MCP tool."""

    name: str            # registry name, e.g. "gmail.send_email"
    server: str          # which MCP server owns it, e.g. "gmail"
    remote_name: str     # name on that server, e.g. "send_email"
    category: ToolCategory
    description: str
    input_schema: dict
    action: ActionType   # READ or WRITE
    risk: RiskLevel
    safe_to_retry: bool  # read-only or declared idempotent


@dataclass
class ToolContext:
    """Who is calling, for which task, and with what authority."""

    user_id: uuid.UUID
    role: str
    task_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    approval_id: uuid.UUID | None = None   # the Approval record that authorises this call (if one is needed)
    ip_address: str | None = None


@dataclass
class ToolResult:
    execution_id: uuid.UUID
    tool: str
    content: Any
    cached: bool
    duration_ms: float


class GatewayError(Exception):
    status_code = 500
    code = "tool_error"


class ToolNotFound(GatewayError):
    status_code, code = 404, "tool_not_found"


class ToolPermissionDenied(GatewayError):
    status_code, code = 403, "permission_denied"


class InvalidToolArguments(GatewayError):
    status_code, code = 422, "invalid_arguments"


class ApprovalRequired(GatewayError):
    status_code, code = 409, "approval_required"


class PolicyBlocked(GatewayError):
    status_code, code = 403, "blocked_by_policy"


class IdempotencyConflict(GatewayError):
    status_code, code = 409, "idempotency_conflict"


class DuplicateInFlight(GatewayError):
    status_code, code = 409, "duplicate_in_flight"


class ToolReportedError(GatewayError):
    """The tool ran and said no (bad id, validation, business rule). Retrying won't help."""

    status_code, code = 422, "tool_error"


class ToolUnavailable(GatewayError):
    """Transport/timeout failure on a call that is safe to repeat."""

    status_code, code = 502, "tool_unavailable"


class ToolOutcomeUnknown(GatewayError):
    """A WRITE timed out or the connection dropped: it may or may not have happened."""

    status_code, code = 502, "outcome_unknown"
