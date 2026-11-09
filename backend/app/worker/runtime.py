"""Everything a worker process needs to run tasks, kept alive for the life of the process.

A Celery task body is synchronous, but our runner is async and holds resources that belong to ONE event loop
(DB engine, MCP subprocesses, checkpointer connection). So each worker process runs a private event loop in a
background thread and Celery tasks submit coroutines to it. One long-lived coroutine owns every resource, so
they are opened and closed in the same asyncio task (a requirement of the MCP/anyio transports).
"""
import asyncio
import logging
import threading
from concurrent.futures import Future
from contextlib import AsyncExitStack

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.checkpointer import open_checkpointer
from app.agents.runner import TaskRunner
from app.core.config import Settings, get_settings
from app.llm.client import UnavailableLLM, get_llm_client
from app.tools.bootstrap import build_gateway, start_builtin_servers
from app.worker.celery_app import celery_app, make_enqueue

log = logging.getLogger("app.worker")


class WorkerRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="worker-loop", daemon=True)
        self._stop: asyncio.Event | None = None
        self._main: Future | None = None
        self.sf = self.runner = self.llm = self.gateway = None

    def start(self, timeout: float = 120) -> None:
        self._thread.start()
        ready: Future = Future()
        self._main = asyncio.run_coroutine_threadsafe(self._lifecycle(ready), self.loop)
        ready.result(timeout)     # raises if setup failed

    async def _lifecycle(self, ready: Future) -> None:
        stack = AsyncExitStack()
        engine = create_async_engine(self.settings.database_url, pool_pre_ping=True)
        try:
            self._stop = asyncio.Event()
            self.sf = async_sessionmaker(engine, expire_on_commit=False)
            self.gateway = build_gateway(self.settings)
            await start_builtin_servers(self.gateway, self.settings)
            checkpointer = await stack.enter_async_context(open_checkpointer(self.settings))
            self.llm = get_llm_client() if self.settings.openai_api_key else UnavailableLLM()
            self.runner = TaskRunner(self.sf, self.gateway, checkpointer, self.settings)
            self.runner.dispatch = make_enqueue(celery_app)      # follow-up work goes back on the queue
            ready.set_result(True)
            await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            await self.runner.shutdown() if self.runner else None
            if self.gateway:
                await self.gateway.close()
            await stack.aclose()
            await engine.dispose()

    def call(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        if self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
            if self._main:
                self._main.result(30)
        self.loop.call_soon_threadsafe(self.loop.stop)


_runtime: WorkerRuntime | None = None
_lock = threading.Lock()


def get_runtime() -> WorkerRuntime:
    """Created lazily, inside the process that runs tasks (so it works with prefork, threads and solo)."""
    global _runtime
    with _lock:
        if _runtime is None:
            rt = WorkerRuntime(get_settings())
            rt.start()
            _runtime = rt
    return _runtime
