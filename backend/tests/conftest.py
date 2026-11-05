import os
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ["BCRYPT_ROUNDS"] = "4"  # must be set before the app is imported
os.environ["OPENAI_API_KEY"] = ""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import create_app


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")  # set -> run the whole suite on real PostgreSQL


@pytest_asyncio.fixture
async def app():
    """App wired to a throwaway DB, fresh for every test (in-memory SQLite, or PostgreSQL if
    TEST_DATABASE_URL is set; see scripts/test_on_postgres.py)."""
    if TEST_DATABASE_URL:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    app = create_app(init_db=False, init_tools=False)
    app.state.session_factory = maker

    async def override_get_db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app
    await engine.dispose()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def register(client, email, password="Passw0rd!x", name="Test User"):
    return await client.post("/api/v1/auth/register", json={"email": email, "full_name": name, "password": password})


async def login_headers(client, email, password="Passw0rd!x"):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------- tool layer ----
from datetime import date  # noqa: E402

FIXED_TODAY = date(2026, 11, 2)


@pytest_asyncio.fixture
async def gateway(app):
    """Real MCP servers (in-process, real MCP protocol) with deterministic demo data, wired into the app."""
    from app.mcp_servers import business_db, drive_server, gmail_server
    from app.mcp_servers.backends import MockDriveBackend, MockGmailBackend
    from app.mcp_servers.business_models import BusinessBase, seed_demo_data
    from app.schemas.plan import ToolCategory
    from app.tools.gateway import ToolGateway
    from app.tools.policy import ToolPolicy

    biz_engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with biz_engine.begin() as conn:
        await conn.run_sync(BusinessBase.metadata.create_all)
    biz_maker = async_sessionmaker(biz_engine, expire_on_commit=False)
    async with biz_maker() as db:
        await seed_demo_data(db, FIXED_TODAY)

    gw = ToolGateway(ToolPolicy(), timeout=5, max_retries=2, backoff=0)
    await gw.add_server("business_db", ToolCategory.DATABASE,
                        business_db.create_server(biz_maker, today_fn=lambda: FIXED_TODAY))
    await gw.add_server("gmail", ToolCategory.GMAIL, gmail_server.create_server(MockGmailBackend(FIXED_TODAY)))
    await gw.add_server("drive", ToolCategory.DRIVE, drive_server.create_server(MockDriveBackend(FIXED_TODAY)))
    app.state.tool_gateway = gw
    yield gw
    await biz_engine.dispose()


@pytest_asyncio.fixture
async def runner(app, gateway):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agents.runner import TaskRunner
    from app.core.config import get_settings

    r = TaskRunner(app.state.session_factory, gateway, InMemorySaver(), get_settings())
    app.state.task_runner = r
    yield r
    await r.shutdown()
