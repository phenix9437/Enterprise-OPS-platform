"""Decides, for a set of proposed tool calls, whether a human must approve, who may, and whether
anything is forbidden outright. Pure functions + one DB loader: easy to test, no side effects."""
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.approval import ApprovalPolicy

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
ROLE_RANK = {"manager": 1, "admin": 2}


@dataclass
class CallInfo:
    index: int
    tool: str
    arguments: dict
    risk: str
    action: str


@dataclass
class Requirement:
    required: bool = False
    blocked: list[dict] = field(default_factory=list)     # [{index, tool, policy, message}]
    approver_role: str = "manager"
    allow_self_approval: bool = False
    expires_in_minutes: int = 1440
    policies: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


async def load_enabled_policies(db: AsyncSession) -> list[ApprovalPolicy]:
    return list((await db.scalars(select(ApprovalPolicy).where(ApprovalPolicy.enabled.is_(True))
                                  .order_by(ApprovalPolicy.name))).all())


# ---------------------------------------------------------------- conditions ----
def _lookup(arguments: dict, path: str) -> tuple[bool, Any]:
    cur: Any = arguments
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _domain(value: Any) -> str:
    s = str(value)
    return s.rsplit("@", 1)[1].lower() if "@" in s else ""


def condition_matches(cond: dict, arguments: dict) -> bool:
    """A field that is absent means 'this rule does not apply'. A value we cannot compare
    (wrong type) fails CLOSED: the condition counts as matched, so the stricter outcome applies."""
    found, val = _lookup(arguments, cond["field"])
    if not found:
        return False
    op, target = cond["op"], cond.get("value")
    try:
        if op == "eq":
            return val == target
        if op == "ne":
            return val != target
        if op in ("gt", "gte", "lt", "lte"):
            a, b = float(val), float(target)
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if op == "in":
            return val in target
        if op == "not_in":
            return val not in target
        text, needle = str(val).lower(), str(target).lower()
        if op == "contains":
            return needle in text
        if op == "not_contains":
            return needle not in text
        if op == "startswith":
            return text.startswith(needle)
        if op == "endswith":
            return text.endswith(needle)
        if op == "email_domain_in":
            return _domain(val) in {str(t).lower() for t in target}
        if op == "email_domain_not_in":
            return _domain(val) not in {str(t).lower() for t in target}
    except (TypeError, ValueError):
        return True
    return True  # unknown operator: fail closed


def policy_matches(policy: ApprovalPolicy, call: CallInfo) -> bool:
    if not fnmatchcase(call.tool, policy.tool_pattern):
        return False
    if RISK_ORDER.get(call.risk, 2) < RISK_ORDER.get(policy.min_risk, 0):
        return False
    return all(condition_matches(c, call.arguments) for c in (policy.arg_conditions or []))


# ---------------------------------------------------------------- evaluation ----
def evaluate(calls: list[CallInfo], policies: list[ApprovalPolicy], *, step_requires_approval: bool = False,
             default_expiry: int | None = None, default_allow_self: bool | None = None) -> Requirement:
    s = get_settings()
    req = Requirement(
        allow_self_approval=s.approval_allow_self_approval if default_allow_self is None else default_allow_self,
        expires_in_minutes=default_expiry or s.approval_default_expiry_minutes)

    def need(reason: str):
        req.required = True
        if reason not in req.reasons:
            req.reasons.append(reason)

    # baseline (cannot be disabled)
    if step_requires_approval:
        need("the plan marked this step as needing approval")
    for c in calls:
        if c.risk == "high":
            need(f"{c.tool} is a high-risk tool")

    # admin policies (can only add strictness)
    for p in policies:
        for c in calls:
            if not policy_matches(p, c):
                continue
            message = p.message or p.description or f"policy '{p.name}'"
            if p.action == "block":
                req.blocked.append({"index": c.index, "tool": c.tool, "policy": p.name, "message": message})
                continue
            need(f"{message} (policy '{p.name}')")
            if p.name not in req.policies:
                req.policies.append(p.name)
            if ROLE_RANK.get(p.approver_role, 2) > ROLE_RANK[req.approver_role]:
                req.approver_role = p.approver_role
            req.allow_self_approval = req.allow_self_approval and p.allow_self_approval
            if p.expires_in_minutes is not None:
                req.expires_in_minutes = min(req.expires_in_minutes, p.expires_in_minutes)
    return req
