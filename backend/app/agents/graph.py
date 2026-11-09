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
from app.workflows.builders import BUILDERS, BuildContext, BuilderError
from app.workflows.models import OUTPUT_MODELS
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
    outputs: dict[str, Any]      # full structured output per step id (tool results / parsed model / rendered text)
    warnings: list[str]
    approved_calls: list[str]    # canonical form of the calls a human already approved for this step
    approved_approval_id: str | None


@dataclass
class GraphDeps:
    session_factory: Any
    gateway: ToolGateway
    llm: LLMClient
    settings: Settings


# ------------------------------------------------------------------ helpers ----
SUMMARY_CLIP = 60000


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"...[truncated {len(text) - limit} chars]"


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def canon(call: dict) -> str:
    return json.dumps({"t": call["tool"], "a": call["arguments"]}, sort_keys=True, default=str)


def current_step(state: ExecState) -> dict:
    return state["plan"]["steps"][state["step_index"]]


def outputs_by_id(state: ExecState) -> dict[int, Any]:
    return {int(k): v for k, v in (state.get("outputs") or {}).items()}


def outline(state: ExecState) -> str:
    done = state.get("step_results", {})
    lines = []
    for st in state["plan"]["steps"]:
        status = done.get(str(st["id"]), {}).get("status", "pending")
        lines.append(f"{st['id']}. [{status}] {st['title']}")
    return "\n".join(lines)


def dependency_block(state: ExecState, step: dict, limit: int) -> str:
    limit = step.get("context_chars") or limit
    parts = []
    for dep in step["depends_on"]:
        res = state.get("step_results", {}).get(str(dep))
        if not res:
            continue
        body = f"Summary: {res.get('summary') or ''}"
        out = (state.get("outputs") or {}).get(str(dep))
        if isinstance(out, list):  # results of tool calls
            body += "\nData: " + dumps([{"tool": e["tool"], "arguments": e["arguments"],
                                         "result": e.get("result", e.get("error"))} for e in out])
        parts.append(f'<untrusted_data step="{dep}" title="{res["title"]}">\n{clip(body, limit)}\n</untrusted_data>')
    return "\n".join(parts) or "(none)"


def tools_block(specs: list[ToolSpec]) -> str:
    return "\n".join(
        f"- {sp.name} [{sp.action.value}, {sp.risk.value} risk]: {sp.description.splitlines()[0] if sp.description else ''}\n"
        f"  schema: {dumps(sp.input_schema)}" for sp in specs)


def base_context(state: ExecState, step: dict, limit: int) -> str:
    return (
        f"User request: {state['instruction']}\n"
        f"Plan objective: {state['plan']['objective']}\n"
        f"Plan outline:\n{outline(state)}\n\n"
        f"CURRENT STEP {step['id']}: {step['title']}\n{step['description']}\n"
        f"Capability category for this step: {step['tool']}\n\n"
        f"Inputs from earlier steps:\n{dependency_block(state, step, limit)}\n"
    )


def describe_calls(results: list[dict]) -> str:
    ok = [r for r in results if r["ok"]]
    lines = [f"{len(ok)} of {len(results)} call(s) succeeded"]
    lines += [f"{r['tool']} {clip(dumps(r['arguments']), 100)}" for r in ok[:10]]
    return "; ".join(lines[:1]) + ((": " + " | ".join(lines[1:])) if len(lines) > 1 else "")


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


def with_warnings(summary: str, state: ExecState) -> str:
    warnings = state.get("warnings") or []
    return summary + ("\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings) if warnings else "")


def deterministic_summary(state: ExecState) -> str:
    outcome = state.get("outcome") or "completed"
    lines = [f"Task {outcome}."]
    if state.get("error"):
        lines.append(state["error"])
    for st in state["plan"]["steps"]:
        r = state.get("step_results", {}).get(str(st["id"]))
        status = r["status"] if r else "not run"
        lines.append(f"- Step {st['id']} {st['title']}: {status}")
    return with_warnings("\n".join(lines), state)


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
                "approved_calls": [], "approved_approval_id": None, "unknown_outcome": False}

    def after_begin(state: ExecState) -> str:
        if state["step_index"] >= len(state["plan"]["steps"]):
            return "finalize"
        return kind_of(current_step(state))

    def kind_of(step: dict) -> str:
        if step.get("mode") == "render":
            return "compute"
        return "reason" if step["tool"] == "reasoning" else "plan_calls"

    # ---- render steps: text built by code (no LLM) ----
    async def compute(state: ExecState) -> dict:
        step = current_step(state)
        try:
            built = BUILDERS[step["builder"]](BuildContext(step.get("params", {}), outputs_by_id(state), step))
        except (BuilderError, KeyError, ValueError) as exc:
            return {"step_ok": False, "fatal": True, "feedback": f"Could not build this step: {exc}"}
        text = built.text or ""
        await cx.emit(state, "step_completed", f"Step {step['id']} done", step["id"])
        return {"step_ok": True,
                "step_results": {**state.get("step_results", {}), str(step["id"]): {
                    "title": step["title"], "status": "done", "summary": clip(text, SUMMARY_CLIP), "data": None}},
                "outputs": {**(state.get("outputs") or {}), str(step["id"]): {"text": text}},
                "warnings": [*(state.get("warnings") or []), *built.warnings]}

    # ---- reasoning steps (LLM judgement) ----
    async def reason(state: ExecState) -> dict:
        step = current_step(state)
        model = OUTPUT_MODELS.get(step.get("output_model"), ReasoningOutput)
        system = prompts.STRUCTURED_REASONING_SYSTEM if model is not ReasoningOutput else prompts.REASONING_SYSTEM
        user = base_context(state, step, s.context_chars_per_item)
        if state.get("feedback"):
            user += f"\nFeedback on the previous attempt (fix this): {state['feedback']}\n"
        try:
            out = await cx.ask(state, [{"role": "system", "content": system}, {"role": "user", "content": user}], model)
        except LLMError as exc:
            return {"step_ok": False, "feedback": f"LLM error: {exc}"}
        if model is ReasoningOutput:
            summary, output = out.content, {"text": out.content}
        else:
            summary, output = out.to_summary(), out.model_dump()
        await cx.emit(state, "step_completed", f"Step {step['id']} done", step["id"])
        return {"step_ok": True,
                "step_results": {**state.get("step_results", {}), str(step["id"]): {
                    "title": step["title"], "status": "done", "summary": clip(summary, SUMMARY_CLIP), "data": None}},
                "outputs": {**(state.get("outputs") or {}), str(step["id"]): output}}

    def after_reason(state: ExecState) -> str:
        return "advance" if state.get("step_ok") else "retry_or_fail"

    # ---- shared tail of both planning paths ----
    async def propose(state: ExecState, step: dict, calls: list[dict], req, note: str, warnings: list[str]) -> dict:
        update_ = {"proposed_calls": calls, "approval": req.to_dict(), "approval_id": None, "human_note": None,
                   "step_ok": None, "fatal": False, "warnings": [*(state.get("warnings") or []), *warnings]}
        memo = state.get("approved_calls") or []
        if req.required and memo and all(canon(c) in memo for c in calls):
            # a retry of calls a human ALREADY approved for this step: no need to ask again
            update_["approval"] = {**req.to_dict(), "required": False}
            update_["approval_id"] = state.get("approved_approval_id")
            await cx.emit(state, "approval_reused", "Re-using the earlier approval for identical calls", step["id"])
        await cx.emit(state, "calls_proposed", f"{len(calls)} call(s) proposed", step["id"],
                      {"calls": [c["tool"] for c in calls], "reasoning": note})
        return update_

    # ---- code-built tool steps ----
    async def plan_builder(state: ExecState, step: dict, specs: list[ToolSpec]) -> dict:
        try:
            built = BUILDERS[step["builder"]](BuildContext(step.get("params", {}), outputs_by_id(state), step))
        except (BuilderError, KeyError, ValueError) as exc:
            return {"step_ok": False, "fatal": True, "proposed_calls": [], "feedback": f"Could not build this step: {exc}"}
        by_name = {sp.name: sp for sp in specs}
        warnings = list(built.warnings)
        if len(built.calls) > s.workflow_max_calls_per_step:
            warnings.append(f"Only the first {s.workflow_max_calls_per_step} of {len(built.calls)} calls were made")
        calls = []
        for i, c in enumerate(built.calls[: s.workflow_max_calls_per_step], 1):
            spec = by_name.get(c["tool"])
            if spec is None:
                return {"step_ok": False, "fatal": True, "proposed_calls": [],
                        "feedback": f"Tool '{c['tool']}' is not available for this step"}
            try:
                jsonschema.validate(c["arguments"], spec.input_schema)
            except jsonschema.ValidationError as exc:
                return {"step_ok": False, "fatal": True, "proposed_calls": [],
                        "feedback": f"Built call {i} ({c['tool']}) is invalid: {exc.message}"}
            calls.append({"tool": c["tool"], "arguments": c["arguments"], "action": spec.action.value,
                          "risk": spec.risk.value, "meta": c.get("meta")})

        async with deps.session_factory() as db:
            policies = await load_enabled_policies(db)
        infos = lambda cs: [CallInfo(i, c["tool"], c["arguments"], c["risk"], c["action"]) for i, c in enumerate(cs)]  # noqa: E731
        probe = evaluate(infos(calls), policies)
        if probe.blocked:   # a blocked item is skipped (with a warning); it does not fail the whole run
            drop = {b["index"] for b in probe.blocked}
            for b in probe.blocked:
                warnings.append(f"Skipped {b['tool']} call {b['index'] + 1}: blocked by policy '{b['policy']}' ({b['message']})")
            calls = [c for i, c in enumerate(calls) if i not in drop]

        if not calls:   # nothing to do (e.g. no overdue invoices): the step is complete
            await cx.emit(state, "step_completed", f"Step {step['id']}: nothing to do", step["id"])
            summary = "Nothing to do" + (f" ({'; '.join(warnings)})" if warnings else "")
            return {"step_ok": True, "proposed_calls": [], "warnings": [*(state.get("warnings") or []), *warnings],
                    "step_results": {**state.get("step_results", {}), str(step["id"]): {
                        "title": step["title"], "status": "done", "summary": summary, "data": None}},
                    "outputs": {**(state.get("outputs") or {}), str(step["id"]): []}}
        req = evaluate(infos(calls), policies, step_requires_approval=step["requires_approval"])
        return await propose(state, step, calls, req, "built by code", warnings)

    # ---- executor agent: decide the tool calls (no side effects) ----
    async def plan_calls(state: ExecState) -> dict:
        step = current_step(state)
        specs = [sp for sp in gw.specs_for(state["user_role"]) if sp.category.value == step["tool"]]
        if not specs:
            return {"step_ok": False, "fatal": True, "proposed_calls": [],
                    "feedback": f"No '{step['tool']}' tools are available to this user"}
        if step.get("mode") == "builder":
            return await plan_builder(state, step, specs)
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
        return await propose(state, step, calls, req, d.reasoning, [])

    def after_plan(state: ExecState) -> str:
        if state.get("step_ok"):
            return "advance"         # a code-built step that had nothing to do
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
            "calls": [{"tool": c["tool"], "arguments": c["arguments"], "risk": c["risk"], "meta": c.get("meta")}
                      for c in state["proposed_calls"]],
            "reason": "; ".join(req["reasons"]) or "approval required",
            "approval": req,
        })
        if decision.get("approved"):
            final = decision.get("calls") or state["proposed_calls"]
            update_ = {"approval_id": decision["approval_id"], "approved_approval_id": decision["approval_id"],
                       "approved_calls": [canon(c) for c in final]}
            if decision.get("calls"):   # the reviewer edited the calls: run THEIR version
                update_["proposed_calls"] = decision["calls"]
                update_["human_note"] = decision.get("note")
            return update_
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
            entry = {"tool": call["tool"], "arguments": call["arguments"], "meta": call.get("meta")}
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

    # ---- verifier ----
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

        outputs = {**(state.get("outputs") or {}), str(step["id"]): results}
        # Code-built steps and pure reads are verified deterministically; LLM-chosen writes get an LLM verifier.
        if step.get("mode") == "builder" or all(c["action"] == "read" for c in state["proposed_calls"]):
            summary = describe_calls(results)
        else:
            data = clip(dumps([{"tool": r["tool"], "arguments": r["arguments"], "result": r["result"]} for r in results]),
                        s.context_chars_per_item)
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
        await cx.emit(state, "step_completed", f"Step {step['id']} verified", step["id"])
        return {"step_ok": True, "outputs": outputs,
                "step_results": {**state.get("step_results", {}), str(step["id"]): {
                    "title": step["title"], "status": "done", "summary": clip(summary, SUMMARY_CLIP), "data": None}}}

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
        results = state.get("call_results") or []
        if step.get("allow_partial") and any(r["ok"] for r in results):
            # keep what DID happen (e.g. 4 of 5 emails sent) so later steps can record it; flag the rest
            bad = [r for r in results if not r["ok"]]
            note = f"Step {step['id']} ({step['title']}) partly succeeded: " + "; ".join(
                f"{r['tool']} {clip(dumps(r['arguments']), 60)} failed: {clip(str(r.get('error')), 200)}" for r in bad)
            await cx.emit(state, "step_partial", note, step["id"])
            return {"step_ok": True, "warnings": [*(state.get("warnings") or []), note],
                    "outputs": {**(state.get("outputs") or {}), str(step["id"]): results},
                    "step_results": {**state.get("step_results", {}), str(step["id"]): {
                        "title": step["title"], "status": "partial", "summary": describe_calls(results), "data": None}}}
        await cx.emit(state, "step_failed", f"Step {step['id']} failed: {reason}", step["id"])
        failed = {**state.get("step_results", {}), str(step["id"]): {
            "title": step["title"], "status": "failed", "summary": reason, "data": None}}
        return {"outcome": "failed", "error": f"Step {step['id']} ({step['title']}) failed: {reason}",
                "step_results": failed}

    def after_retry(state: ExecState) -> str:
        if state.get("outcome"):
            return "finalize"
        if state.get("step_ok"):
            return "advance"
        return kind_of(current_step(state))

    async def advance(state: ExecState) -> dict:
        return {"step_index": state["step_index"] + 1}

    # ---- finish ----
    async def finalize(state: ExecState) -> dict:
        outcome = state.get("outcome") or "completed"
        summary = deterministic_summary({**state, "outcome": outcome})
        if outcome == "completed":
            meta = state["plan"].get("meta") or {}
            result_steps = [str(i) for i in meta.get("result_steps") or [] if str(i) in state.get("step_results", {})]
            if result_steps:   # the workflow's own output IS the result: no extra LLM call
                summary = with_warnings("\n\n".join(state["step_results"][i]["summary"] for i in result_steps), state)
            else:
                lines = "\n".join(f"Step {sid} - {r['title']}: {r['summary']}" for sid, r in state["step_results"].items())
                try:
                    out: FinalSummary = await cx.ask(state, [
                        {"role": "system", "content": prompts.SUMMARY_SYSTEM},
                        {"role": "user", "content": f"User request: {state['instruction']}\n\nStep results:\n{clip(lines, 12000)}"}],
                        FinalSummary)
                    summary = with_warnings(out.summary, state)
                except LLMError:
                    pass  # keep the deterministic summary
        return {"outcome": outcome, "final_summary": summary}

    g = StateGraph(ExecState)
    for name, fn in [("begin_step", begin_step), ("compute", compute), ("reason", reason), ("plan_calls", plan_calls),
                     ("approval_gate", approval_gate), ("run_calls", run_calls), ("verify", verify),
                     ("retry_or_fail", retry_or_fail), ("advance", advance), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "begin_step")
    g.add_conditional_edges("begin_step", after_begin, ["finalize", "compute", "reason", "plan_calls"])
    g.add_conditional_edges("compute", after_reason, ["advance", "retry_or_fail"])
    g.add_conditional_edges("reason", after_reason, ["advance", "retry_or_fail"])
    g.add_conditional_edges("plan_calls", after_plan, ["approval_gate", "advance", "retry_or_fail"])
    g.add_conditional_edges("approval_gate", after_gate, ["finalize", "run_calls"])
    g.add_edge("run_calls", "verify")
    g.add_conditional_edges("verify", after_verify, ["finalize", "advance", "retry_or_fail"])
    g.add_conditional_edges("retry_or_fail", after_retry, ["finalize", "advance", "compute", "reason", "plan_calls"])
    g.add_edge("advance", "begin_step")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
