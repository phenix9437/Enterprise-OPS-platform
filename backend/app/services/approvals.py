import json
from datetime import datetime, timezone

import jsonschema

from app.models.approval import Approval
from app.models.user import User
from app.services.approval_policy import ROLE_RANK, CallInfo, evaluate, load_enabled_policies
from app.tools.gateway import ToolGateway
from app.tools.types import GatewayError


class ApprovalError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite hands back naive datetimes
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_expired(a: Approval, now: datetime | None = None) -> bool:
    exp = as_utc(a.expires_at)
    return exp is not None and exp <= (now or datetime.now(timezone.utc))


def can_decide(user: User, a: Approval) -> tuple[bool, str | None]:
    if a.status != "pending":
        return False, f"already {a.status}"
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(a.required_role, 2):
        return False, f"requires the '{a.required_role}' role"
    if user.id == a.requested_by and not a.allow_self_approval:
        return False, "separation of duties: you cannot approve your own request"
    return True, None


def canonical(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


def describe_changes(modifications: list[dict]) -> str:
    parts = []
    for m in modifications:
        n = m["index"] + 1
        if m.get("removed"):
            parts.append(f"call {n} ({m['tool']}) was removed")
        else:
            changed = sorted(k for k in set(m["from"]) | set(m["to"]) if m["from"].get(k) != m["to"].get(k))
            parts.append(f"call {n} ({m['tool']}): changed {', '.join(changed)}")
    return "; ".join(parts)


async def build_final_calls(db, gateway: ToolGateway, approval: Approval, user: User,
                            edits: list, remove: list[int]) -> tuple[list[dict], list[dict]]:
    """Apply a reviewer's edits. Reviewers may change arguments of the proposed calls or drop calls;
    they may NOT add calls or switch tools. Everything is re-validated and re-checked against policies."""
    original = approval.proposed_calls
    n = len(original)
    removed = set(remove)
    if any(i < 0 or i >= n for i in removed):
        raise ApprovalError(422, f"remove: index out of range (there are {n} calls)")
    by_index: dict[int, dict] = {}
    for e in edits:
        if e.index >= n:
            raise ApprovalError(422, f"edits: index {e.index} out of range (there are {n} calls)")
        if e.index in removed:
            raise ApprovalError(422, f"edits: call {e.index} is also being removed")
        if e.index in by_index:
            raise ApprovalError(422, f"edits: call {e.index} is edited twice")
        by_index[e.index] = e.arguments

    final, mods = [], []
    for i, call in enumerate(original):
        if i in removed:
            mods.append({"index": i, "tool": call["tool"], "removed": True})
            continue
        args = by_index.get(i, call["arguments"])
        try:
            spec = gateway.registry.get(call["tool"])
            jsonschema.validate(args, spec.input_schema)
        except jsonschema.ValidationError as exc:
            raise ApprovalError(422, f"Call {i + 1} ({call['tool']}): invalid arguments: {exc.message}") from exc
        except GatewayError as exc:
            raise ApprovalError(422, str(exc)) from exc
        final.append({"tool": call["tool"], "arguments": args, "risk": spec.risk.value, "action": spec.action.value,
                      "meta": call.get("meta")})
        if i in by_index and args != call["arguments"]:
            mods.append({"index": i, "tool": call["tool"], "from": call["arguments"], "to": args})
    if not final:
        raise ApprovalError(422, "Cannot approve with every call removed; reject the request instead")

    # re-check the final calls against the CURRENT policies
    policies = await load_enabled_policies(db)
    req = evaluate([CallInfo(i, c["tool"], c["arguments"], c["risk"], c["action"]) for i, c in enumerate(final)], policies)
    if req.blocked:
        b = req.blocked[0]
        raise ApprovalError(422, f"The edited call is blocked by policy '{b['policy']}': {b['message']}")
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK[req.approver_role]:
        raise ApprovalError(403, f"After your edits this needs the '{req.approver_role}' role")
    return final, mods
