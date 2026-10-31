# Enterprise Ops Platform

Build status: **Phase 1 (foundation) and Phase 2 (LLM core) complete.**

| # | Phase | Status |
|---|---|---|
| 1 | Foundation: FastAPI, PostgreSQL, JWT auth, RBAC, audit log | done |
| 2 | LLM core: structured plans, validation, chat + task API | done |
| 3 | MCP tool layer (Postgres, Gmail, Drive) | next |
| 4 | LangGraph orchestration | |
| 5 | Human-in-the-loop approvals | |
| 6 | Business workflows + Celery/Redis | |
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
