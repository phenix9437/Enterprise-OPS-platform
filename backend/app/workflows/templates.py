"""The four built-in business workflows. A template turns validated parameters into a complete plan
(the same JSON shape the planner produces, plus a few extra keys the executor understands):

    mode          "builder" = tool calls built by code | "render" = text built by code | (absent) = LLM executor / reasoning
    builder       name in builders.BUILDERS
    params        parameters handed to the builder
    output_model  reasoning step returns a structured model (models.OUTPUT_MODELS)
    allow_partial finish the step even if some calls failed (successes are kept)
    context_chars how much of earlier outputs this step's LLM may see
"""
import re
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.schemas.plan import Plan
from app.services.plan_validation import validate_and_normalize

_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


# ------------------------------------------------------------------ parameters ----
class OverdueRemindersParams(BaseModel):
    min_days_overdue: int = Field(default=7, ge=1, le=365, description="Only chase invoices at least this overdue")
    max_invoices: int = Field(default=20, ge=1, le=50)
    max_reminders_per_invoice: int = Field(default=3, ge=1, le=10, description="After this many, escalate to a person instead")
    min_days_between_reminders: int = Field(default=7, ge=0, le=90)
    use_playbook: bool = Field(default=True, description="Read the 'Collections Playbook' from Drive for tone and rules")


class PurchaseReviewParams(BaseModel):
    max_requests: int = Field(default=10, ge=1, le=25)


class ReportSummaryParams(BaseModel):
    query: str = Field(min_length=2, max_length=200, description="What to look for in Drive")
    max_files: int = Field(default=3, ge=1, le=5)
    focus: str | None = Field(default=None, max_length=300, description="What the summary should concentrate on")


class DigestParams(BaseModel):
    min_days_overdue: int = Field(default=1, ge=0, le=365)
    narrative: bool = Field(default=False, description="Add a short LLM-written commentary (the numbers are always computed by code)")
    delivery: Literal["none", "draft", "email"] = Field(default="none", description="'draft' saves a Gmail draft; 'email' sends (needs approval)")
    recipient: str | None = Field(default=None, max_length=254)

    @model_validator(mode="after")
    def _recipient_rules(self):
        if self.delivery != "none" and not (self.recipient and _EMAIL_RE.match(self.recipient)):
            raise ValueError("a valid recipient email is required when delivery is 'draft' or 'email'")
        return self


# ------------------------------------------------------------------ plan assembly ----
def S(id, title, description, tool, action="read", deps=(), risk="low", approval=False, **extra) -> dict:
    return {"id": id, "title": title, "description": description, "tool": tool, "action": action,
            "depends_on": list(deps), "risk": risk, "requires_approval": approval, **extra}


_BASE_KEYS = {"id", "title", "description", "tool", "action", "depends_on", "risk", "requires_approval"}


def make_plan(objective: str, intent: str, steps: list[dict], meta: dict) -> dict:
    """Validate the structure like any plan, then put the extra executor keys back.

    Unlike planner-made plans, template risk levels are NOT bumped by the coarse plan-level floor (it would force
    every Gmail write, even a harmless draft, through approval). Templates are vetted code, and the real enforcement
    is unchanged: the gateway rates each actual tool (draft = medium, send = high) and applies policies and approval."""
    extras = {s["id"]: {k: v for k, v in s.items() if k not in _BASE_KEYS} for s in steps}
    base = Plan.model_validate({
        "objective": objective, "intent": intent, "requires_clarification": False, "clarification_question": None,
        "steps": [{k: v for k, v in s.items() if k in _BASE_KEYS} for s in steps]})
    validate_and_normalize(base, get_settings().planner_max_steps)   # raises if the structure is invalid
    out = base.model_dump(mode="json")
    for st in out["steps"]:
        st.update(extras[st["id"]])
    out["meta"] = meta
    return out


# ------------------------------------------------------------------ templates ----
def build_overdue_reminders(p: OverdueRemindersParams) -> dict:
    cp = p.model_dump()
    rules = (
        "Decide, for EVERY invoice in the digest, whether to send a payment reminder now.\n"
        "Rules: (1) skip if the customer's emails show a dispute or unresolved query about this invoice; "
        "(2) skip if the customer has promised a payment date that has not passed yet (do not chase; it will be "
        "followed up later); (3) skip if a partial-payment arrangement is in progress and the customer said when "
        "the balance will arrive; otherwise send. Follow the collections playbook in the digest for tone and "
        "escalation rules (polite, specific, brief).\n"
        "For 'send': write a personalised subject and body that quote the invoice number, the outstanding amount "
        "with currency, the original due date and how many days overdue it is, and acknowledge relevant history "
        "(for example a part payment already received). Do NOT include the recipient address; only write the subject "
        "and body. Sign off as 'Accounts Team'. For 'skip': give a short reason and set subject and body to null.\n"
        "Return exactly one decision per invoice, using the exact invoice_id values shown.")
    steps = [
        S(1, "Find overdue invoices", "List unpaid invoices past their due date.", "database", mode="builder", builder="overdue.list", params=cp),
        S(2, "Find the collections playbook", "Search Drive for the collections playbook.", "drive", mode="builder", builder="playbook.search", params=cp),
        S(3, "Read the playbook", "Read the playbook for tone and escalation rules.", "drive", deps=[2], mode="builder", builder="playbook.read", params=cp),
        S(4, "Search email history", "Search the mailbox for each customer's recent messages.", "gmail", deps=[1], mode="builder", builder="overdue.search_threads", params=cp),
        S(5, "Read the conversations", "Read the most recent conversations with each customer.", "gmail", deps=[4], mode="builder", builder="overdue.read_threads", params=cp),
        S(6, "Prepare the briefing", "Combine invoices, playbook and email history into one briefing.", "reasoning", "none", deps=[1, 3, 5], mode="render", builder="overdue.digest", params=cp),
        S(7, "Decide and draft reminders", rules, "reasoning", "none", deps=[6], output_model="reminder_decisions", context_chars=40000),
        S(8, "Send the reminders", "Email each approved reminder to the customer's address on file.", "gmail", "write", deps=[1, 7],
          risk="high", approval=True, mode="builder", builder="reminders.send", params=cp, allow_partial=True),
        S(9, "Record the reminders", "Log every reminder that was actually sent against its invoice.", "database", "write", deps=[1, 7, 8],
          risk="medium", mode="builder", builder="reminders.log", params=cp),
    ]
    return make_plan("Send personalised payment reminders for overdue invoices", "invoice_followup", steps,
                     {"workflow": "overdue_invoice_reminders", "params": cp, "result_steps": []})


def build_purchase_review(p: PurchaseReviewParams) -> dict:
    cp = p.model_dump()
    rules = (
        "Review every pending purchase request in the digest and recommend an action. 'approve' only if at least one "
        "unexpired quotation exists that fits the estimated budget (or exceeds it by a small, justified margin); then "
        "set quotation_id to the best-value quotation (usually the cheapest valid one; mention any delivery or discount "
        "notes in the rationale). 'reject' if every quotation is expired or far over budget. 'needs_info' if there are "
        "no quotations or the request is unclear. Return exactly one recommendation per request, using the exact "
        "request_id and quotation_id values shown.")
    steps = [
        S(1, "List pending purchase requests", "List purchase requests awaiting a decision.", "database", mode="builder", builder="pr.list", params=cp),
        S(2, "Collect vendor quotations", "Fetch the quotations received for each request.", "database", deps=[1], mode="builder", builder="pr.quotations", params=cp),
        S(3, "Prepare the briefing", "Combine requests and quotations into one briefing.", "reasoning", "none", deps=[1, 2], mode="render", builder="pr.digest", params=cp),
        S(4, "Recommend decisions", rules, "reasoning", "none", deps=[3], output_model="purchase_recommendations", context_chars=40000),
        S(5, "Apply the approved decisions", "Update each request's status to match the recommendation, after human sign-off.", "database", "write",
          deps=[1, 2, 4], risk="medium", approval=True, mode="builder", builder="pr.apply", params=cp),
    ]
    return make_plan("Review pending purchase requests against vendor quotations", "purchase_request", steps,
                     {"workflow": "purchase_request_review", "params": cp, "result_steps": []})


def build_report_summary(p: ReportSummaryParams) -> dict:
    cp = p.model_dump()
    focus = f" Concentrate on: {p.focus}." if p.focus else ""
    steps = [
        S(1, "Find the reports", f"Search Drive for '{p.query}'.", "drive", mode="builder", builder="report.search", params=cp),
        S(2, "Read the reports", "Read the matching documents.", "drive", deps=[1], mode="builder", builder="report.read", params=cp),
        S(3, "Summarise", "Summarise the key figures, trends and anything that needs attention, for a busy manager. "
                          "Use only the documents; quote figures exactly; keep it under 250 words." + focus,
          "reasoning", "none", deps=[2], context_chars=40000),
    ]
    return make_plan(f"Summarise business reports matching '{p.query}'", "report_generation", steps,
                     {"workflow": "report_summary", "params": cp, "result_steps": [3]})


def build_operations_digest(p: DigestParams) -> dict:
    cp = p.model_dump()
    steps = [
        S(1, "Collect overdue invoices", "List overdue invoices.", "database", mode="builder", builder="digest.overdue", params=cp),
        S(2, "Collect pending purchase requests", "List pending purchase requests.", "database", mode="builder", builder="digest.prs", params=cp),
        S(3, "Build the digest", "Compute totals and ageing buckets (done by code, no LLM).", "reasoning", "none", deps=[1, 2],
          mode="render", builder="digest.render", params=cp),
    ]
    result_steps, nxt = [3], 4
    if p.narrative:
        steps.append(S(nxt, "Write the commentary", "In under 120 words, point out the 2-3 things a manager should act on today. "
                                                   "Use only the figures in the digest.", "reasoning", "none", deps=[3]))
        result_steps.append(nxt)
        nxt += 1
    if p.delivery != "none":
        email = p.delivery == "email"
        steps.append(S(nxt, "Email the digest" if email else "Save the digest as a draft",
                       f"{'Send' if email else 'Draft'} the digest to {p.recipient}.", "gmail", "write", deps=[3],
                       risk="high" if email else "medium", approval=email, mode="builder", builder="digest.deliver", params=cp))
    return make_plan("Produce the operations digest", "report_generation", steps,
                     {"workflow": "operations_digest", "params": cp, "result_steps": result_steps})


@dataclass(frozen=True)
class WorkflowTemplate:
    name: str
    title: str
    description: str
    params_model: type[BaseModel]
    build: Callable[[BaseModel], dict]
    approval_note: str


TEMPLATES: dict[str, WorkflowTemplate] = {t.name: t for t in [
    WorkflowTemplate(
        "overdue_invoice_reminders", "Overdue invoice reminders",
        "Finds overdue invoices, reads each customer's email history and the collections playbook, decides who to chase, "
        "drafts personalised reminders, sends them after human approval, and logs them against the invoices.",
        OverdueRemindersParams, build_overdue_reminders, "Emails are sent only after a manager approves the exact messages."),
    WorkflowTemplate(
        "purchase_request_review", "Purchase request review",
        "Reviews pending purchase requests against vendor quotations and recommends approve / reject / needs-info; "
        "status changes are applied only after human sign-off.",
        PurchaseReviewParams, build_purchase_review, "Status changes need approval."),
    WorkflowTemplate(
        "report_summary", "Report summary",
        "Finds reports in Google Drive and produces a short management summary.",
        ReportSummaryParams, build_report_summary, "Read-only: never needs approval."),
    WorkflowTemplate(
        "operations_digest", "Operations digest",
        "Daily/weekly snapshot of overdue invoices and pending purchase requests. Numbers are computed by code; an LLM "
        "commentary and email/draft delivery are optional. Runs with no LLM by default.",
        DigestParams, build_operations_digest, "Read-only unless delivery='email' (then the send needs approval)."),
]}


def build_workflow_plan(name: str, params: dict) -> tuple[dict, dict]:
    """-> (plan, validated_params). Raises KeyError for an unknown workflow, pydantic.ValidationError for bad params."""
    t = TEMPLATES[name]
    validated = t.params_model.model_validate(params or {})
    return t.build(validated), validated.model_dump()
