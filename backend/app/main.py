import asyncio
import logging
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.api.routes import approvals, auth, chat, execution, health, tools, users
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.base import Base
from app.db.session import SessionLocal, engine

log = logging.getLogger("app.request")


def create_app(init_db: bool = True, init_tools: bool = True) -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stack = AsyncExitStack()
        gateway = runner = sweeper = None
        try:
            if init_db:
                # Dev convenience. Replaced by Alembic migrations in the hardening phase.
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            if init_tools and settings.tools_enabled:
                from app.agents.checkpointer import open_checkpointer
                from app.agents.runner import TaskRunner
                from app.tools.bootstrap import build_gateway, start_builtin_servers

                gateway = build_gateway(settings)
                await start_builtin_servers(gateway)
                app.state.tool_gateway = gateway
                checkpointer = await stack.enter_async_context(open_checkpointer(settings))
                runner = TaskRunner(app.state.session_factory, gateway, checkpointer, settings)
                app.state.task_runner = runner
                from app.llm.client import UnavailableLLM, get_llm_client

                llm = get_llm_client() if settings.openai_api_key else UnavailableLLM()
                if settings.openai_api_key:  # continue tasks that were running when we last stopped
                    n = await runner.recover_interrupted(llm)
                    if n:
                        log.info("recovering %d interrupted task(s)", n)

                async def sweep_expired_approvals():
                    while True:
                        await asyncio.sleep(settings.approval_sweep_seconds)
                        try:
                            await runner.expire_overdue_approvals(llm)
                        except Exception:
                            log.exception("approval expiry sweep failed")

                sweeper = asyncio.create_task(sweep_expired_approvals(), name="approval-sweeper")
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
            if runner is not None:
                await runner.shutdown()
            if gateway is not None:
                await gateway.close()
            await stack.aclose()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.session_factory = SessionLocal

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        start = time.perf_counter()
        response = await call_next(request)
        ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["x-request-id"] = request_id
        log.info(
            "request",
            extra={"extra_fields": {
                "request_id": request_id, "method": request.method,
                "path": request.url.path, "status": response.status_code, "duration_ms": ms,
            }},
        )
        return response

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(tools.router, prefix="/api/v1")
    app.include_router(execution.router, prefix="/api/v1")
    app.include_router(approvals.router, prefix="/api/v1")
    return app


app = create_app()
