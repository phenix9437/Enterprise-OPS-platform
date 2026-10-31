import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.api.routes import auth, chat, health, users
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.base import Base
from app.db.session import engine

log = logging.getLogger("app.request")


def create_app(init_db: bool = True) -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if init_db:
            # Dev convenience. Replaced by Alembic migrations in the hardening phase.
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        yield

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

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
    return app


app = create_app()
