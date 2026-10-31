import pytest

from app.schemas.plan import ActionType, RiskLevel, ToolCategory
from app.services.plan_validation import PlanValidationError, validate_and_normalize
from tests.fakes import clarification, plan, step


def test_valid_plan_passes():
    p = plan(step(1), step(2, depends_on=[1]))
    assert len(validate_and_normalize(p, 12).steps) == 2


def test_forward_dependency_rejected():
    with pytest.raises(PlanValidationError, match="does not appear earlier"):
        validate_and_normalize(plan(step(1, depends_on=[2]), step(2)), 12)


def test_self_dependency_and_missing_dependency_rejected():
    with pytest.raises(PlanValidationError):
        validate_and_normalize(plan(step(1, depends_on=[1])), 12)
    with pytest.raises(PlanValidationError):
        validate_and_normalize(plan(step(1, depends_on=[99])), 12)


def test_duplicate_ids_rejected():
    with pytest.raises(PlanValidationError, match="Duplicate"):
        validate_and_normalize(plan(step(1), step(1)), 12)


def test_empty_and_oversized_plans_rejected():
    with pytest.raises(PlanValidationError, match="no steps"):
        validate_and_normalize(plan(), 12)
    with pytest.raises(PlanValidationError, match="maximum"):
        validate_and_normalize(plan(*[step(i) for i in range(1, 5)]), 3)


def test_clarification_rules():
    assert validate_and_normalize(clarification(), 12).requires_clarification
    bad = clarification()
    bad.clarification_question = "  "
    with pytest.raises(PlanValidationError):
        validate_and_normalize(bad, 12)


def test_safety_floor_forces_email_send_to_high_and_approval():
    p = plan(step(1, tool=ToolCategory.GMAIL, action=ActionType.WRITE, risk=RiskLevel.LOW, approval=False))
    out = validate_and_normalize(p, 12).steps[0]
    assert out.risk == RiskLevel.HIGH and out.requires_approval is True


def test_safety_floor_write_is_at_least_medium_and_original_untouched():
    p = plan(step(1, tool=ToolCategory.CRM, action=ActionType.WRITE, risk=RiskLevel.LOW))
    out = validate_and_normalize(p, 12)
    assert out.steps[0].risk == RiskLevel.MEDIUM
    assert p.steps[0].risk == RiskLevel.LOW  # input not mutated


def test_reasoning_steps_never_count_as_external_actions():
    p = plan(step(1, tool=ToolCategory.REASONING, action=ActionType.WRITE))
    assert validate_and_normalize(p, 12).steps[0].action == ActionType.NONE
