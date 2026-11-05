"""The LangGraph execution graph. One run = one task = one pass through the plan, step by step.

    begin_step -+-> reason ---------------------------+
                |                                      |
                +-> plan_calls -> approval_gate -> run_calls -> verify
                |        |             |                          |
                |        +--> retry_or_fail <---------------------+
                |                  |                              |
                +<------ advance <-+-------- (step ok) -----------+
                |
                +-> finalize -> END

Design rules that matter:
  * plan_calls (LLM) has no side effects; approval_gate holds the ONLY interrupt() and nothing
    else, because LangGraph re-runs an interrupted node from its start when it resumes.
  * Approval is bound to the exact proposed calls: a retry that proposes different calls must be
    approved again.
  * Writes are de-duplicated by the gateway (auto idempotency key), so retries and crash
    recovery can never send the same email twice.
  * Everything in the graph state is plain JSON so it can be checkpointed to PostgreSQL.
"""
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

import jsonschema
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select, update

from app.agents import prompts
from app.agents.schemas import ExecutorDecision, FinalSummary, ReasoningOutput, StepVerdict
from app.core.config import Settings
from app.llm.client import LLMClient, LLMError, LLMUsage
from app.models.chat import Task, TaskEvent
from app.tools.gateway import ToolGateway
from app.services.approval_policy import CallInfo, evaluate, load_enabled_policies
from app.tools.types import GatewayError, ToolContext, ToolOutcomeUnknown, ToolSpec

log = logging.getLogger("app.agents")


class ExecState(TypedDict, total=False):
    task_id: str
    user_id: str
    user_role: str
    instruction: str
    plan: dict
    step_index: int
    attempt: int
    feedback: str | None
    fatal: bool                  # no point retrying this step
    step_ok: bool | None
    proposed_calls: list[dict]   # [{tool, arguments, action, risk}]
    call_results: list[dict]
    unknown_outcome: bool
    approval: dict | None        # requirement computed from policies when the calls were proposed
    approval_id: str | None      # the Approval record that authorised the proposed calls
    human_note: str | None       # what a reviewer changed before approving (so the verifier knows)
    step_results: dict[str, dict]
    outcome: str | None          # completed | failed | rejected | needs_attention
    error: str | None
    final_summary: str | None


@dataclass
class GraphDeps:
    session_factory: Any
    gateway: ToolGateway
    llm: LLMClient
    settings: Settings


# ------------------------------------------------------------------ helpers ----
def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"...[truncated {len(text) - limit} chars]"


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def current_step(state: ExecState) -> dict:
    return state["plan"]["steps"][state["step_index"]]


def outline(state: ExecState) -> str:
    done = state.get("step_results", {})
    lines = []
    for s in state["plan"]["steps"]:
        status = done.get(str(s["id"]), {}).get("status", "pending")
        lines.append(f"{s['id']}. [{status}] {s['title']}")
    return "\n".join(lines)


def dependency_block(state: ExecState, step: dict, limit: int) -> str:
    parts = []
    for dep in step["depends_on"]:
        res = state.get("step_results", {}).get(str(dep))
        if not res:
            continue
        body = f"Summary: {res.get('summary') or ''}"
        if res.get("data"):
            body += f"\nData: {res['data']}"
        parts.append(f'<untrusted_data step="{dep}" title="{res["title"]}">\n{clip(body, limit)}\n</untrusted_data>')
    return "\n".join(parts) or "(none)"


def tools_block(specs: list[ToolSpec]) -> str:
    return "\n".join(
        f"- {s.name} [{s.action.value}, {s.risk.value} risk]: {s.description.splitlines()[0] if s.description else ''}\n"
        f"  schema: {dumps(s.input_schema)}" for s in specs)


def base_context(state: ExecState, step: dict, limit: int) -> str:
    return (
        f"User request: {state['instruction']}\n"
        f"Plan objective: {state['plan']['objective']}\n"
        f"Plan outline:\n{outline(state)}\n\n"
        f"CURRENT STEP {step['id']}: {step['title']}\n{step['description']}\n"
        f"Capability category for this step: {step['tool']}\n\n"
        f"Inputs from earlier steps:\n{dependency_block(state, step, limit)}\n"
    )


class Ctx:
    """Small helper bundle used by the nodes: DB writes for events/usage."""

    def __init__(self, deps: GraphDeps):
        self.deps = deps

    async def emit(self, state: ExecState, type_: str, message: str, step_id: int | None = None, data: dict | None = None):
        async with self.deps.session_factory() as db:
            seq = (await db.scalar(select(func.coalesce(func.max(TaskEvent.seq), 0))
                                   .where(TaskEvent.task_id == uuid.UUID(state["task_id"])))) + 1
            db.add(TaskEvent(task_id=uuid.UUID(state["task_id"]), seq=seq, type=type_,
                             step_id=step_id, message=message, data=data))
            await db.commit()

    async def usage(self, state: ExecState, u: LLMUsage):
        async with self.deps.session_factory() as db:
            await db.execute(update(Task).where(Task.id == uuid.UUID(state["task_id"])).values(
                llm_model=u.model, llm_calls=Task.llm_calls + 1,
                prompt_tokens=Task.prompt_tokens + u.prompt_tokens,
                completion_tokens=Task.completion_tokens + u.completion_tokens,
                llm_latency_ms=Task.llm_latency_ms + u.latency_ms))
            await db.commit()

    async def ask(self, state: ExecState, messages: list[dict], model):
        """LLM call + usage accounting. Raises LLMError."""
        result = await self.deps.llm.parse(messages, model)
        await self.usage(state, result.usage)
        return result.parsed


def deterministic_summary(state: ExecState) -> str:
    outcome = state.get("outcome") or "completed"
    lines = [f"Task {outcome}."]
    if state.get("error"):
        lines.append(state["error"])
    for s in state["plan"]["steps"]:
        r = state.get("step_results", {}).get(str(s["id"]))
        status = r["status"] if r else "not run"
        lines.append(f"- Step {s['id']} {s['title']}: {status}")
    return "\n".join(lines)


# -------------------------------------------------------------------- graph ----
def build_graph(deps: GraphDeps, checkpointer):
    cx = Ctx(deps)
    s = deps.settings
    gw = deps.gateway

    async def begin_step(state: ExecState) -> dict:
        if state["step_index"] >= len(state["plan"]["steps"]):
            return {}
        step = current_step(state)
        async with deps.session_factory() as db:
            await db.execute(update(Task).where(Task.id == uuid.UUID(state["task_id"])).values(current_step=step["id"]))
            await db.commit()
        await cx.emit(state, "step_started", f"Step {step['id']}: {step['title']}", step["id"])
        return {"attempt": 1, "feedback": None, "fatal": False, "step_ok": None, "proposed_calls": [],
                "call_results": [], "approval": None, "approval_id": None, "human_note": None,
                "unknown_outcome": False}

    def after_begin(state: ExecState) -> str:
        if state["step_index"] >= len(state["plan"]["steps"]):
            return "finalize"
        return "reason" if current_step(state)["tool"] == "reasoning" else "plan_calls"

    # ---- reasoning steps (pure LLM work) ----
    async def reason(state: ExecState) -> dict:
        step = current_step(state)
        user = base_context(state, step, s.context_chars_per_item)
        if state.get("feedback"):
            user += f"\nFeedback on the previous attempt (fix this): {state['feedback']}\n"
        try:
            out = await cx.ask(state, [{"role": "system", "content": prompts.REASONING_SYSTEM},
                                       {"role": "user", "content": user}], ReasoningOutput)
        except LLMError as exc:
            return {"step_ok": False, "feedback": f"LLM error: {exc}"}
        results = {**state.get("step_results", {}), str(step["id"]): {
            "title": step["title"], "status": "done", "summary": clip(out.content, s.context_chars_per_item), "data": None}}
        await cx.emit(state, "step_completed", f"Step {step['id']} done", step["id"])
        return {"step_ok": True, "step_results": results}

    def after_reason(state: ExecState) -> str:
        return "advance" if state.get("step_ok") else "retry_or_fail"

    # ---- executor agent: decide the tool calls (no side effects) ----
    async def plan_calls(state: ExecState) -> dict:
        step = current_step(state)
        specs = [sp for sp in gw.specs_for(state["user_role"]) if sp.category.value == step["tool"]]
        if not specs:
            return {"step_ok": False, "fatal": True, "proposed_calls": [],
                    "feedback": f"No '{step['tool']}' tools are available to this user"}
        user = base_context(state, step, s.context_chars_per_item) + f"\nTools you may call:\n{tools_block(specs)}\n"
        if state.get("feedback"):
            user += f"\nFeedback from the previous attempt (fix this): {state['feedback']}\n"
        try:
            d: ExecutorDecision = await cx.ask(
                state, [{"role": "system", "content": prompts.EXECUTOR_SYSTEM.format(max_calls=s.executor_max_calls_per_step)},
                        {"role": "user", "content": user}], ExecutorDecision)
        except LLMError as exc:
            return {"step_ok": False, "feedback": f"LLM error: {exc}", "proposed_calls": []}

        if d.cannot_proceed:
            return {"step_ok": False, "fatal": True, "proposed_calls": [],
                    "feedback": d.cannot_proceed_reason or "The executor could not proceed"}
        by_name = {sp.name: sp for sp in specs}
        errors, calls, seen = [], [], set()
        if not d.calls:
            errors.append("No tool calls were proposed")
        if len(d.calls) > s.executor_max_calls_per_step:
            errors.append(f"Too many calls ({len(d.calls)}); the limit is {s.executor_max_calls_per_step}")
        for i, c in enumerate(d.calls[: s.executor_max_calls_per_step], 1):
            spec = by_name.get(c.tool)
            if spec is None:
                errors.append(f"Call {i}: '{c.tool}' is not an available tool for this step. Choose from: {sorted(by_name)}")
                continue
            try:
                args = json.loads(c.arguments_json)
                if not isinstance(args, dict):
                    raise ValueError("not an object")
            except ValueError:
                errors.append(f"Call {i} ({c.tool}): arguments_json must be a JSON object")
                continue
            try:
                jsonschema.validate(args, spec.input_schema)
            except jsonschema.ValidationError as exc:
                errors.append(f"Call {i} ({c.tool}): invalid arguments: {exc.message}")
                continue
            key = (c.tool, json.dumps(args, sort_keys=True))
            if key in seen:
                continue  # identical call proposed twice
            seen.add(key)
            calls.append({"tool": c.tool, "arguments": args, "action": spec.action.value, "risk": spec.risk.value})
        if errors:
            return {"step_ok": False, "proposed_calls": [], "feedback": "; ".join(errors)}

        # admin policies: blocked calls go back to the executor as feedback; the rest decides approval
        async with deps.session_factory() as db:
            policies = await load_enabled_policies(db)
        req = evaluate([CallInfo(i, c["tool"], c["arguments"], c["risk"], c["action"]) for i, c in enumerate(calls)],
                       policies, step_requires_approval=step["requires_approval"])
        if req.blocked:
            msg = "; ".join(f"Call {b['index'] + 1} ({b['tool']}) is not allowed by policy '{b['policy']}': {b['message']}"
                            for b in req.blocked)
            await cx.emit(state, "calls_blocked", msg, step["id"])
            return {"step_ok": False, "proposed_calls": [], "feedback": msg}
        await cx.emit(state, "calls_proposed", f"{len(calls)} call(s) proposed", step["id"],
                      {"calls": [c["tool"] for c in calls], "reasoning": d.reasoning})
        return {"proposed_calls": calls, "approval": req.to_dict(), "approval_id": None, "human_note": None,
                "step_ok": None, "fatal": False}

    def after_plan(state: ExecState) -> str:
        return "approval_gate" if state.get("proposed_calls") else "retry_or_fail"

    # ---- approval gate: the only place that pauses the graph ----
    async def approval_gate(state: ExecState) -> dict:
        step = current_step(state)
        req = state.get("approval") or {}
        if not req.get("required"):
            return {}
        decision = interrupt({
            "task_id": state["task_id"],
            "step": {"id": step["id"], "title": step["title"], "description": step["description"]},
            "attempt": state["attempt"],
            "calls": [{"tool": c["tool"], "arguments": c["arguments"], "risk": c["risk"]} for c in state["proposed_calls"]],
            "reason": "; ".join(req["reasons"]) or "approval required",
            "approval": req,
        })
        if decision.get("approved"):
            update = {"approval_id": decision["approval_id"]}
            if decision.get("calls"):   # the reviewer edited the calls: run THEIR version
                update["proposed_calls"] = decision["calls"]
                update["human_note"] = decision.get("note")
            return update
        who = decision.get("approver_id", "a reviewer")
        if decision.get("expired"):
            return {"outcome": "expired",
                    "error": f"Step {step['id']} ({step['title']}) timed out waiting for approval"}
        note = f" ({decision['comment']})" if decision.get("comment") else ""
        return {"outcome": "rejected", "error": f"Step {step['id']} ({step['title']}) was rejected by {who}{note}"}

    def after_gate(state: ExecState) -> str:
        return "finalize" if state.get("outcome") in ("rejected", "expired") else "run_calls"

    # ---- run the calls through the gateway ----
    async def run_calls(state: ExecState) -> dict:
        step = current_step(state)
        ctx = ToolContext(
            user_id=uuid.UUID(state["user_id"]), role=state["user_role"], task_id=uuid.UUID(state["task_id"]),
            approval_id=uuid.UUID(state["approval_id"]) if state.get("approval_id") else None)
        results, unknown = [], False
        for call in state["proposed_calls"]:
            entry = {"tool": call["tool"], "arguments": call["arguments"]}
            async with deps.session_factory() as db:
                try:
                    r = await gw.execute(db, ctx, call["tool"], call["arguments"])
                    entry.update(ok=True, result=r.content, cached=r.cached)
                except ToolOutcomeUnknown as exc:
                    entry.update(ok=False, error=str(exc), code=exc.code)
                    unknown = True
                except GatewayError as exc:
                    entry.update(ok=False, error=str(exc), code=exc.code)
            results.append(entry)
            await cx.emit(state, "tool_call", f"{call['tool']}: {'ok' if entry['ok'] else entry['code']}", step["id"],
                          {"tool": call["tool"], "ok": entry["ok"], "cached": entry.get("cached", False),
                           "error": entry.get("error")})
            if unknown:
                break  # never keep going after a write whose outcome we can't confirm
        return {"call_results": results, "unknown_outcome": unknown}

    # ---- verifier agent ----
    async def verify(state: ExecState) -> dict:
        step = current_step(state)
        results = state["call_results"]
        if state.get("unknown_outcome"):
            bad = next(r for r in results if not r["ok"])
            return {"outcome": "needs_attention",
                    "error": f"Step {step['id']} ({step['title']}): {bad['error']} A human must check the target system."}
        failures = [r for r in results if not r["ok"]]
        if failures:
            fatal = any(r.get("code") == "permission_denied" for r in failures)
            text = "; ".join(f"{r['tool']} failed ({r['code']}): {r['error']}" for r in failures)
            return {"step_ok": False, "fatal": fatal, "feedback": text}

        data = clip(dumps([{"tool": r["tool"], "arguments": r["arguments"], "result": r["result"]} for r in results]),
                    s.context_chars_per_item)
        if all(c["action"] == "read" for c in state["proposed_calls"]):
            summary = f"{len(results)} read call(s) succeeded"
        else:
            note = (f"A human reviewer modified the proposed calls before approving. Treat this as intentional "
                    f"and judge the step against the calls that were actually approved: {state['human_note']}\n\n"
                    if state.get("human_note") else "")
            user = (f"STEP {step['id']}: {step['title']}\n{step['description']}\n\n{note}"
                    f"Calls made and their results:\n<untrusted_data>\n{data}\n</untrusted_data>")
            try:
                v: StepVerdict = await cx.ask(state, [{"role": "system", "content": prompts.VERIFIER_SYSTEM},
                                                      {"role": "user", "content": user}], StepVerdict)
            except LLMError as exc:
                return {"step_ok": False, "feedback": f"Verification unavailable: {exc}"}
            if not v.satisfied:
                return {"step_ok": False, "feedback": f"{v.reason}. {v.retry_advice or ''}".strip()}
            summary = v.reason
        results_map = {**state.get("step_results", {}), str(step["id"]): {
            "title": step["title"], "status": "done", "summary": summary, "data": data}}
        await cx.emit(state, "step_completed", f"Step {step['id']} verified", step["id"])
        return {"step_ok": True, "step_results": results_map}

    def after_verify(state: ExecState) -> str:
        if state.get("outcome"):
            return "finalize"
        return "advance" if state.get("step_ok") else "retry_or_fail"

    # ---- retry / give up ----
    async def retry_or_fail(state: ExecState) -> dict:
        step = current_step(state)
        if not state.get("fatal") and state["attempt"] < s.step_max_attempts:
            await cx.emit(state, "step_retry", f"Retrying step {step['id']}: {state.get('feedback')}", step["id"],
                          {"attempt": state["attempt"] + 1})
            return {"attempt": state["attempt"] + 1, "step_ok": None, "approval": None, "approval_id": None,
                    "human_note": None}
        reason = state.get("feedback") or "unknown error"
        await cx.emit(state, "step_failed", f"Step {step['id']} failed: {reason}", step["id"])
        results = {**state.get("step_results", {}), str(step["id"]): {
            "title": step["title"], "status": "failed", "summary": reason, "data": None}}
        return {"outcome": "failed", "error": f"Step {step['id']} ({step['title']}) failed: {reason}",
                "step_results": results}

    def after_retry(state: ExecState) -> str:
        if state.get("outcome"):
            return "finalize"
        return "reason" if current_step(state)["tool"] == "reasoning" else "plan_calls"

    async def advance(state: ExecState) -> dict:
        return {"step_index": state["step_index"] + 1}

    # ---- finish ----
    async def finalize(state: ExecState) -> dict:
        outcome = state.get("outcome") or "completed"
        summary = deterministic_summary({**state, "outcome": outcome})
        if outcome == "completed":
            lines = "\n".join(f"Step {sid} - {r['title']}: {r['summary']}" for sid, r in state["step_results"].items())
            try:
                out: FinalSummary = await cx.ask(state, [
                    {"role": "system", "content": prompts.SUMMARY_SYSTEM},
                    {"role": "user", "content": f"User request: {state['instruction']}\n\nStep results:\n{clip(lines, 12000)}"}],
                    FinalSummary)
                summary = out.summary
            except LLMError:
                pass  # keep the deterministic summary
        return {"outcome": outcome, "final_summary": summary}

    g = StateGraph(ExecState)
    for name, fn in [("begin_step", begin_step), ("reason", reason), ("plan_calls", plan_calls),
                     ("approval_gate", approval_gate), ("run_calls", run_calls), ("verify", verify),
                     ("retry_or_fail", retry_or_fail), ("advance", advance), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "begin_step")
    g.add_conditional_edges("begin_step", after_begin, ["finalize", "reason", "plan_calls"])
    g.add_conditional_edges("reason", after_reason, ["advance", "retry_or_fail"])
    g.add_conditional_edges("plan_calls", after_plan, ["approval_gate", "retry_or_fail"])
    g.add_conditional_edges("approval_gate", after_gate, ["finalize", "run_calls"])
    g.add_edge("run_calls", "verify")
    g.add_conditional_edges("verify", after_verify, ["finalize", "advance", "retry_or_fail"])
    g.add_conditional_edges("retry_or_fail", after_retry, ["finalize", "reason", "plan_calls"])
    g.add_edge("advance", "begin_step")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
