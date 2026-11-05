# Enterprise Ops Platform

Build status: **Phases 1-5 complete (foundation, LLM core, MCP tool layer, LangGraph orchestration, human approvals).**

| # | Phase | Status |
|---|---|---|
| 1 | Foundation: FastAPI, PostgreSQL, JWT auth, RBAC, audit log | done |
| 2 | LLM core: structured plans, validation, chat + task API | done |
| 3 | MCP tool layer: Postgres, Gmail, Drive servers + gateway | done |
| 4 | LangGraph orchestration: executor/verifier agents, retries, checkpoints | done |
| 5 | Human-in-the-loop approvals: records, policies, edit/reject, expiry | done |
| 6 | Business workflows + Celery/Redis | next |
| 7 | Frontend + analytics | |
| 8 | Hardening + deploy | |

## Run with Docker
    cp .env.example .env      # set JWT_SECRET and OPENAI_API_KEY
    docker compose up --build
    # API docs: http://localhost:8000/docs

## Run tests (no Postgres or OpenAI needed)
    cd backend
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt
    pytest

## Try it
1. POST /api/v1/auth/register  -> first user becomes admin
2. Authorize in /docs (username = your email)
3. Register a second user, then PATCH /api/v1/users/{id}/role to make them "operator"
   (viewers cannot use chat)
4. POST /api/v1/chat {"message": "Send payment reminders to customers overdue by 30+ days"}
5. GET /api/v1/tasks, GET /api/v1/audit-logs (admin)

## Try only the planner against real OpenAI (no DB/server)
    cd backend && export OPENAI_API_KEY=sk-...
    python -m scripts.try_planner "Summarise this month's overdue invoices"

## Phase 3: tools
The API starts three MCP servers (stdio subprocesses) at boot: `business_db`, `gmail`, `drive`.
Gmail and Drive use **mock backends by default** (demo data, no credentials needed).
The business DB seeds demo customers/invoices on first start (`SEED_DEMO_DATA=false` to disable).

    GET  /api/v1/tools                       # discovered tools + risk level for your role
    POST /api/v1/tools/invoke                # {"tool": "business_db.list_overdue_invoices", "arguments": {"min_days_overdue": 30}}
    GET  /api/v1/tool-executions             # admin/manager: every call, status, attempts, result

High-risk tools (e.g. `gmail.send_email`) return 409 `approval_required` unless a manager/admin
passes `"approve": true`. Repeating an identical write returns the stored result instead of running again.

Real Google accounts: `pip install -r requirements-google.txt`, set `GMAIL_BACKEND=google`,
`DRIVE_BACKEND=google` and `GOOGLE_CREDENTIALS_FILE=/path/to/authorized_user.json`
(untested against live Google; see notes in `app/mcp_servers/backends.py`).

## Phase 4: running a plan
    POST /api/v1/chat                      -> plans the request (Phase 2)
    POST /api/v1/tasks/{id}/execute        -> runs the plan in the background (202)
    GET  /api/v1/tasks/{id}                -> status, current_step, result, pending_approval, token usage
    GET  /api/v1/tasks/{id}/events         -> timeline: step_started, tool_call, step_retry, approval_requested...

Task statuses: planned -> running -> (awaiting_approval <-> running) -> completed | failed | rejected | expired | needs_attention.
`needs_attention` means a write's outcome is unknown (e.g. an email send timed out): a human must check.

The graph saves a checkpoint after every node. With PostgreSQL (the default in docker-compose) a task can wait
days for approval and survives restarts; with SQLite the checkpoints are in memory only.

**Upgrading from an earlier phase:** Phase 4 adds columns and tables. `create_all` does not alter existing tables,
so reset the dev database once (`docker compose down -v`). Alembic migrations arrive in Phase 8.

## Testing
    cd backend && pytest                          # fast, SQLite
    pip install pgserver && python -m scripts.test_on_postgres   # same suite on a real PostgreSQL

## Phase 5: approvals
When a step needs a human, the task pauses (`awaiting_approval`) and an **approval request** is created.

    GET   /api/v1/approvals?status=pending            reviewers see all; others see requests for their own tasks
    GET   /api/v1/approvals/{id}                      exact calls (recipient, subject, body...), reason, expiry, can_decide
    POST  /api/v1/approvals/{id}/decision             {"decision": "approve"|"reject", "comment": "...",
                                                       "edits": [{"index": 0, "arguments": {...}}], "remove": [1]}
    GET/POST/PATCH/DELETE /api/v1/approval-policies   admin-managed rules (managers can read)
    POST  /api/v1/approval-policies/evaluate          dry run: what would happen for this tool call?

Rules of the road
* **Baseline, always on:** high-risk tools need approval. Policies can only add strictness (require approval for more
  calls, require `admin`, shorten expiry, forbid self-approval, or **block** calls outright); they cannot turn the baseline off.
* **Separation of duties:** you cannot approve your own request (`APPROVAL_ALLOW_SELF_APPROVAL=true` for solo dev only).
* **Edit before approving:** reviewers can change arguments or drop calls (not add calls or change tools). Edits are
  re-validated and re-checked against current policies; the original proposal is kept next to what was approved.
* **Exact-call enforcement:** the tool gateway only runs a high-risk call if an *approved* record covers that exact
  tool + arguments for that task, regardless of what the graph says.
* **Expiry:** unanswered requests expire (default 24h, `APPROVAL_DEFAULT_EXPIRY_MINUTES`); the task ends as `expired`.
* Every request, decision, edit, denial and policy change is in `approvals` and the audit log.

Example policies
    {"name": "internal-only-email", "action": "block", "tool_pattern": "gmail.send_email",
     "arg_conditions": [{"field": "to", "op": "email_domain_not_in", "value": ["mycompany.com"]}],
     "message": "Only internal recipients are allowed"}
    {"name": "audit-db-writes", "tool_pattern": "business_db.*", "min_risk": "medium",
     "approver_role": "admin", "expires_in_minutes": 120}
