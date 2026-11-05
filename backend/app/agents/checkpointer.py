"""Where LangGraph saves its state after every node.

PostgreSQL  -> durable: tasks survive restarts and can wait days for an approval.
Anything else (SQLite dev/test) -> in memory: fine for development, NOT durable.
"""
import logging
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from app.core.config import Settings

log = logging.getLogger("app.agents")


@asynccontextmanager
async def open_checkpointer(settings: Settings):
    url = settings.checkpoint_database_url or settings.database_url
    if url.startswith("postgresql"):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        conninfo = url.replace("+asyncpg", "").replace("+psycopg", "")  # psycopg wants a plain URL
        async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
            await saver.setup()  # creates LangGraph's checkpoint tables if missing
            yield saver
    else:
        log.warning("Using an in-memory checkpointer: running tasks will NOT survive a restart")
        yield InMemorySaver()
