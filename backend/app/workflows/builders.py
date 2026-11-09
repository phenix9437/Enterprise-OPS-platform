"""Code-built steps: deterministic tool calls and text, no LLM.

Why code instead of the LLM executor for these steps?
  * reliability  - the same inputs always produce the same calls (important for scheduled runs)
  * cost         - no tokens spent on "call this tool with these arguments"
  * safety       - anything security-relevant (e.g. WHO gets an email) is taken from authoritative
                   data (the business database), never from model-generated text.
Each builder returns calls that still go through validation, policies, approval and the gateway.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from app.workflows.models import PurchaseRecommendations, ReminderDecisions


@dataclass
class BuildContext:
    params: dict
    outputs: dict[int, Any]   # step id -> that step's output (list of call results, or {"text"}, or parsed model)
    step: dict


@dataclass
class BuildResult:
    calls: list[dict] = field(default_factory=list)   # [{"tool", "arguments", "meta"?}]
    warnings: list[str] = field(default_factory=list)
    text: str | None = None                           # for render steps


class BuilderError(Exception):
    """The inputs a builder needs are missing or malformed (a bug or a broken upstream step)."""


def _oks(outputs: dict, step_id: int) -> list[dict]:
    return [e for e in (outputs.get(step_id) or []) if e.get("ok")]


def _first_result(outputs: dict, step_id: int) -> dict:
    oks = _oks(outputs, step_id)
    if not oks:
        raise BuilderError(f"Step {step_id} produced no usable result")
    return oks[0]["result"]


def _dep(ctx: BuildContext, index: int) -> int:
    deps = ctx.step.get("depends_on", [])
    if index >= len(deps):
        raise BuilderError(f"step {ctx.step['id']} is missing dependency #{index + 1}")
    return deps[index]


def money(x: float, currency: str = "") -> str:
    return f"{currency} {x:,.2f}".strip()


def clean_line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


# ============================================================ overdue invoice reminders ====
def overdue_list(ctx: BuildContext) -> BuildResult:
    return BuildResult([{"tool": "business_db.list_overdue_invoices",
                         "arguments": {"min_days_overdue": ctx.params["min_days_overdue"], "limit": ctx.params["max_invoices"]}}])


def playbook_search(ctx: BuildContext) -> BuildResult:
    if not ctx.params.get("use_playbook", True):
        return BuildResult()
    return BuildResult([{"tool": "drive.search_files", "arguments": {"query": "Collections Playbook", "max_results": 3}}])


def playbook_read(ctx: BuildContext) -> BuildResult:
    oks = _oks(ctx.outputs, _dep(ctx, 0))
    files = oks[0]["result"].get("files", []) if oks else []
    if not files:
        return BuildResult(warnings=["No collections playbook found in Drive; default tone rules were used"])
    return BuildResult([{"tool": "drive.read_file", "arguments": {"file_id": files[0]["file_id"], "max_chars": 6000}}])


def overdue_search_threads(ctx: BuildContext) -> BuildResult:
    invoices = _first_result(ctx.outputs, _dep(ctx, 0))["invoices"]
    seen, calls = set(), []
    for inv in invoices:
        email = inv["customer_email"].lower()
        if email in seen:
            continue
        seen.add(email)
        calls.append({"tool": "gmail.search_emails", "arguments": {"query": email, "max_results": 5},
                      "meta": {"customer_email": email}})
    return BuildResult(calls)


def overdue_read_threads(ctx: BuildContext) -> BuildResult:
    calls, warnings, total = [], [], 0
    for entry in _oks(ctx.outputs, _dep(ctx, 0)):
        email = entry["meta"]["customer_email"]
        thread_ids = []
        for m in entry["result"].get("emails", []):
            if m["thread_id"] not in thread_ids:
                thread_ids.append(m["thread_id"])
        for tid in thread_ids[:2]:                      # at most 2 conversations per customer
            if total >= 40:
                warnings.append("Reached the 40-thread limit; some customers' history was not read")
                return BuildResult(calls, warnings)
            calls.append({"tool": "gmail.get_thread", "arguments": {"thread_id": tid}, "meta": {"customer_email": email}})
            total += 1
    return BuildResult(calls, warnings)


def overdue_digest(ctx: BuildContext) -> BuildResult:
    """Compact, per-invoice briefing for the LLM: facts from the database + recent email history."""
    inv_res = _first_result(ctx.outputs, ctx.step["depends_on"][0])
    playbook = ""
    pb = _oks(ctx.outputs, ctx.step["depends_on"][1])
    if pb:
        playbook = pb[0]["result"].get("content", "")
    history: dict[str, list[str]] = {}
    for entry in _oks(ctx.outputs, ctx.step["depends_on"][2]):
        email = entry["meta"]["customer_email"]
        for m in entry["result"].get("messages", []):
            history.setdefault(email, []).append(f"  - {m['date'][:10]} FROM {m['from']}: {clean_line(m['body'], 500)}")

    lines = ["COLLECTIONS PLAYBOOK / POLICY:", playbook.strip() or "(none found)", "",
             f"OVERDUE INVOICES as of {inv_res['as_of']} ({inv_res['count']}):"]
    for inv in inv_res["invoices"]:
        last = inv["last_reminder_at"][:10] if inv.get("last_reminder_at") else "never"
        lines.append(f"[invoice_id={inv['invoice_id']}] {inv['number']} | {inv['customer_name']} | "
                     f"outstanding {money(inv['outstanding'], inv['currency'])} of {money(inv['amount'], inv['currency'])} | "
                     f"due {inv['due_date']} ({inv['days_overdue']} days overdue) | status {inv['status']} | "
                     f"reminders sent: {inv['reminders_sent']} (last: {last})")
        msgs = history.get(inv["customer_email"].lower(), [])
        lines.append("  Email history with this customer (oldest first):" if msgs else "  No email history found.")
        lines += msgs[-4:]
    return BuildResult(text="\n".join(lines))


def reminders_send(ctx: BuildContext) -> BuildResult:
    inv_res = _first_result(ctx.outputs, ctx.step["depends_on"][0])
    decisions = ReminderDecisions.model_validate(ctx.outputs[ctx.step["depends_on"][1]])
    by_id = {i["invoice_id"]: i for i in inv_res["invoices"]}
    as_of = date.fromisoformat(inv_res["as_of"])
    max_n, min_gap = ctx.params["max_reminders_per_invoice"], ctx.params["min_days_between_reminders"]

    calls, warnings, handled = [], [], set()
    for d in decisions.decisions:
        inv = by_id.get(d.invoice_id)
        if inv is None:
            warnings.append(f"Ignored a decision for unknown invoice {d.invoice_id}")
            continue
        if d.invoice_id in handled:
            warnings.append(f"Ignored a duplicate decision for {inv['number']}")
            continue
        handled.add(d.invoice_id)
        if d.action != "send":
            continue
        subject, body = clean_line(d.subject or "", 200), (d.body or "").strip()[:10000]
        if not subject or not body:
            warnings.append(f"{inv['number']}: skipped because the drafted email was empty")
            continue
        # guardrails enforced in CODE, whatever the model decided
        if inv["reminders_sent"] >= max_n:
            warnings.append(f"{inv['number']}: already {inv['reminders_sent']} reminders sent; escalate to a person")
            continue
        if inv.get("last_reminder_at") and (as_of - datetime.fromisoformat(inv["last_reminder_at"]).date()).days < min_gap:
            warnings.append(f"{inv['number']}: a reminder was sent less than {min_gap} days ago; skipped")
            continue
        calls.append({"tool": "gmail.send_email",
                      "arguments": {"to": inv["customer_email"], "subject": subject, "body": body},   # recipient from the DB
                      "meta": {"invoice_id": inv["invoice_id"], "number": inv["number"]}})
    return BuildResult(calls, warnings)


def reminders_log(ctx: BuildContext) -> BuildResult:
    calls = []
    for entry in _oks(ctx.outputs, _dep(ctx, 2)):
        meta = entry.get("meta") or {}
        if "invoice_id" not in meta:
            continue
        a = entry["arguments"]
        calls.append({"tool": "business_db.log_invoice_reminder", "arguments": {
            "invoice_id": meta["invoice_id"], "channel": "email",
            "note": clean_line(f"Payment reminder emailed to {a['to']}: {a['subject']}", 500)}})
    return BuildResult(calls)


# ============================================================ purchase request review ====
def pr_list(ctx: BuildContext) -> BuildResult:
    return BuildResult([{"tool": "business_db.list_purchase_requests",
                         "arguments": {"status": "pending", "limit": ctx.params["max_requests"]}}])


def pr_quotations(ctx: BuildContext) -> BuildResult:
    prs = _first_result(ctx.outputs, _dep(ctx, 0))["purchase_requests"]
    return BuildResult([{"tool": "business_db.list_quotations", "arguments": {"purchase_request_id": p["id"]}} for p in prs])


def pr_digest(ctx: BuildContext) -> BuildResult:
    prs = _first_result(ctx.outputs, ctx.step["depends_on"][0])["purchase_requests"]
    quotes = {e["result"]["purchase_request"]["id"]: e["result"] for e in _oks(ctx.outputs, ctx.step["depends_on"][1])}
    lines = [f"PENDING PURCHASE REQUESTS ({len(prs)}):"]
    for p in prs:
        lines.append(f"[request_id={p['id']}] {p['item']} x{p['quantity']} | requested by {p['requester']} on {p['created_on']} | "
                     f"estimated budget {money(p['estimated_cost'], p['currency'])}")
        qs = quotes.get(p["id"], {}).get("quotations", [])
        if not qs:
            lines.append("  No quotations received.")
        for q in qs:
            flags = [] if not q["expired"] else ["EXPIRED"]
            if q["amount"] > p["estimated_cost"]:
                flags.append("OVER BUDGET")
            lines.append(f"  quotation_id={q['quotation_id']} {q['vendor']}: {money(q['amount'], q['currency'])} "
                         f"valid until {q['valid_until']} {' '.join(flags)} {('- ' + q['notes']) if q['notes'] else ''}".rstrip())
    return BuildResult(text="\n".join(lines))


def pr_apply(ctx: BuildContext) -> BuildResult:
    prs = {p["id"]: p for p in _first_result(ctx.outputs, ctx.step["depends_on"][0])["purchase_requests"]}
    quotes = {e["result"]["purchase_request"]["id"]: e["result"]["quotations"] for e in _oks(ctx.outputs, ctx.step["depends_on"][1])}
    recs = PurchaseRecommendations.model_validate(ctx.outputs[ctx.step["depends_on"][2]])
    calls, warnings, handled = [], [], set()
    for r in recs.recommendations:
        if r.request_id not in prs or r.request_id in handled:
            warnings.append(f"Ignored a recommendation for unknown or repeated request {r.request_id}")
            continue
        handled.add(r.request_id)
        if r.action == "needs_info":
            continue
        if r.action == "approve":
            chosen = next((q for q in quotes.get(r.request_id, []) if q["quotation_id"] == r.quotation_id), None)
            if chosen is None or chosen["expired"]:
                warnings.append(f"Request {r.request_id}: not approved (no valid, unexpired quotation was chosen)")
                continue
        calls.append({"tool": "business_db.update_purchase_request_status",
                      "arguments": {"request_id": r.request_id, "new_status": "approved" if r.action == "approve" else "rejected"},
                      "meta": {"request_id": r.request_id, "rationale": clean_line(r.rationale, 300)}})
    return BuildResult(calls, warnings)


# ============================================================ report summary ====
def report_search(ctx: BuildContext) -> BuildResult:
    return BuildResult([{"tool": "drive.search_files",
                         "arguments": {"query": ctx.params["query"], "max_results": ctx.params["max_files"]}}])


def report_read(ctx: BuildContext) -> BuildResult:
    files = _first_result(ctx.outputs, _dep(ctx, 0)).get("files", [])[: ctx.params["max_files"]]
    if not files:
        raise BuilderError(f"No Drive files matched '{ctx.params['query']}'")
    return BuildResult([{"tool": "drive.read_file", "arguments": {"file_id": f["file_id"], "max_chars": 12000}} for f in files])


# ============================================================ operations digest ====
def digest_overdue(ctx: BuildContext) -> BuildResult:
    return BuildResult([{"tool": "business_db.list_overdue_invoices",
                         "arguments": {"min_days_overdue": ctx.params["min_days_overdue"], "limit": 200}}])


def digest_prs(ctx: BuildContext) -> BuildResult:
    return BuildResult([{"tool": "business_db.list_purchase_requests", "arguments": {"status": "pending", "limit": 100}}])


def digest_render(ctx: BuildContext) -> BuildResult:
    inv = _first_result(ctx.outputs, ctx.step["depends_on"][0])
    prs = _first_result(ctx.outputs, ctx.step["depends_on"][1])["purchase_requests"]
    invoices = inv["invoices"]
    lines = [f"OPERATIONS DIGEST - {inv['as_of']}", "", f"Overdue invoices (>= {ctx.params['min_days_overdue']} day(s)): {len(invoices)}"]
    totals: dict[str, float] = {}
    for i in invoices:
        totals[i["currency"]] = totals.get(i["currency"], 0) + i["outstanding"]
    for cur, amount in sorted(totals.items()):
        lines.append(f"  Total outstanding: {money(amount, cur)}")
    buckets = {"1-30 days": 0, "31-60 days": 0, "61+ days": 0}
    for i in invoices:
        d = i["days_overdue"]
        buckets["1-30 days" if d <= 30 else "31-60 days" if d <= 60 else "61+ days"] += 1
    lines += [f"  {name}: {n}" for name, n in buckets.items()]
    top = sorted(invoices, key=lambda i: i["outstanding"], reverse=True)[:5]
    if top:
        lines.append("  Largest outstanding:")
        lines += [f"    {i['number']} {i['customer_name']}: {money(i['outstanding'], i['currency'])} ({i['days_overdue']} days)" for i in top]
    lines += ["", f"Pending purchase requests: {len(prs)}"]
    if prs:
        est: dict[str, float] = {}
        for p in prs:
            est[p["currency"]] = est.get(p["currency"], 0) + p["estimated_cost"]
        lines += [f"  Estimated spend awaiting approval: {money(a, c)}" for c, a in sorted(est.items())]
        oldest = min(prs, key=lambda p: p["created_on"])
        lines.append(f"  Oldest: request {oldest['id']} ({oldest['item']}) since {oldest['created_on']}")
    return BuildResult(text="\n".join(lines))


def digest_deliver(ctx: BuildContext) -> BuildResult:
    report = ctx.outputs[ctx.step["depends_on"][0]]["text"]
    as_of = report.splitlines()[0].split(" - ")[-1]
    tool = "gmail.create_draft" if ctx.params["delivery"] == "draft" else "gmail.send_email"
    return BuildResult([{"tool": tool, "arguments": {"to": ctx.params["recipient"],
                                                      "subject": f"Operations digest {as_of}", "body": report}}])


BUILDERS: dict[str, Callable[[BuildContext], BuildResult]] = {
    "overdue.list": overdue_list, "playbook.search": playbook_search, "playbook.read": playbook_read,
    "overdue.search_threads": overdue_search_threads, "overdue.read_threads": overdue_read_threads,
    "overdue.digest": overdue_digest, "reminders.send": reminders_send, "reminders.log": reminders_log,
    "pr.list": pr_list, "pr.quotations": pr_quotations, "pr.digest": pr_digest, "pr.apply": pr_apply,
    "report.search": report_search, "report.read": report_read,
    "digest.overdue": digest_overdue, "digest.prs": digest_prs, "digest.render": digest_render,
    "digest.deliver": digest_deliver,
}
