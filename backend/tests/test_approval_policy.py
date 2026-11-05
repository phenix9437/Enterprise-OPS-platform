"""Policy engine unit tests: pure logic, no database."""
import pytest

from app.models.approval import ApprovalPolicy
from app.services.approval_policy import CallInfo, condition_matches, evaluate


def P(**kw):
    base = dict(name="p", enabled=True, tool_pattern="*", min_risk="low", arg_conditions=[], action="require_approval",
                approver_role="manager", allow_self_approval=False, expires_in_minutes=None, message=None, description=None)
    return ApprovalPolicy(**{**base, **kw})


def C(tool="gmail.send_email", args=None, risk="high", action="write", i=0):
    return CallInfo(i, tool, args or {}, risk, action)


@pytest.mark.parametrize("op,value,field_value,expected", [
    ("eq", 5, 5, True), ("eq", 5, 6, False), ("ne", 5, 6, True),
    ("gt", 100, 150, True), ("gt", 100, 100, False), ("gte", 100, 100, True), ("lt", 10, 5, True), ("lte", 10, 11, False),
    ("in", ["a", "b"], "a", True), ("not_in", ["a"], "b", True),
    ("contains", "refund", "Big REFUND now", True), ("not_contains", "x", "abc", True),
    ("startswith", "inv", "INV-1", True), ("endswith", ".example", "a@b.example", True),
    ("email_domain_in", ["acme.com"], "bob@ACME.com", True),
    ("email_domain_not_in", ["acme.com"], "bob@evil.com", True),
    ("email_domain_not_in", ["acme.com"], "bob@acme.com", False),
    ("email_domain_not_in", ["acme.com"], "no-at-sign", True),
])
def test_condition_operators(op, value, field_value, expected):
    assert condition_matches({"field": "f", "op": op, "value": value}, {"f": field_value}) is expected


def test_absent_field_means_not_applicable_but_type_errors_fail_closed():
    assert condition_matches({"field": "amount", "op": "gt", "value": 5}, {}) is False
    assert condition_matches({"field": "amount", "op": "gt", "value": 5}, {"amount": "lots"}) is True   # can't compare -> strict
    assert condition_matches({"field": "a.b", "op": "eq", "value": 1}, {"a": {"b": 1}}) is True          # nested path


def test_baseline_high_risk_requires_approval_with_no_policies_at_all():
    req = evaluate([C()], [])
    assert req.required and req.approver_role == "manager" and req.allow_self_approval is False
    assert evaluate([C(tool="business_db.get_invoice", risk="low", action="read")], []).required is False
    assert evaluate([C(risk="low", action="read")], [], step_requires_approval=True).required is True


def test_policy_can_add_approval_for_a_low_risk_call():
    req = evaluate([C("business_db.get_invoice", risk="low", action="read")],
                   [P(name="audit-db", tool_pattern="business_db.*", message="DB reads are audited")])
    assert req.required and req.policies == ["audit-db"] and "DB reads are audited" in req.reasons[0]


def test_min_risk_and_pattern_filter_matches():
    pol = P(tool_pattern="gmail.*", min_risk="high")
    assert evaluate([C("gmail.create_draft", risk="medium", action="write")], [pol]).policies == []
    assert evaluate([C("business_db.x", risk="high")], [pol]).policies == []
    assert evaluate([C("gmail.send_email", risk="high")], [pol]).policies == ["p"]


def test_strictest_policy_wins_when_several_match():
    req = evaluate([C()], [
        P(name="a", approver_role="manager", allow_self_approval=True, expires_in_minutes=600),
        P(name="b", approver_role="admin", allow_self_approval=False, expires_in_minutes=30),
        P(name="c", approver_role="manager", allow_self_approval=True, expires_in_minutes=120),
    ], default_allow_self=True)
    assert req.approver_role == "admin" and req.allow_self_approval is False and req.expires_in_minutes == 30
    assert req.policies == ["a", "b", "c"]


def test_block_policy_reports_which_call_and_why():
    pol = P(name="no-external", action="block", tool_pattern="gmail.send_email", message="Internal recipients only",
            arg_conditions=[{"field": "to", "op": "email_domain_not_in", "value": ["acme.com"]}])
    req = evaluate([C(args={"to": "x@acme.com"}, i=0), C(args={"to": "x@evil.com"}, i=1)], [pol])
    assert [b["index"] for b in req.blocked] == [1] and req.blocked[0]["message"] == "Internal recipients only"
