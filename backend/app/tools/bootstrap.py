"""Starts the built-in MCP servers as stdio subprocesses and registers their tools."""
import logging
import os
import sys
from pathlib import Path

from mcp import StdioServerParameters

from app.core.config import Settings
from app.schemas.plan import ToolCategory
from app.tools.gateway import ToolGateway
from app.tools.policy import ToolPolicy

log = logging.getLogger("app.tools")
BACKEND_DIR = Path(__file__).resolve().parents[2]

BUILTIN_SERVERS = [
    ("business_db", ToolCategory.DATABASE, "app.mcp_servers.business_db"),
    ("gmail", ToolCategory.GMAIL, "app.mcp_servers.gmail_server"),
    ("drive", ToolCategory.DRIVE, "app.mcp_servers.drive_server"),
]


def stdio_params(module: str, extra_env: dict | None = None) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", module],
        env={**os.environ, **(extra_env or {})}, cwd=str(BACKEND_DIR),
    )


def build_gateway(settings: Settings) -> ToolGateway:
    disabled = {t.strip() for t in settings.tools_disabled.split(",") if t.strip()}
    return ToolGateway(
        policy=ToolPolicy(disabled), timeout=settings.tool_call_timeout_seconds,
        max_retries=settings.tool_max_retries, backoff=settings.tool_retry_backoff_seconds,
    )


async def start_builtin_servers(gateway: ToolGateway) -> None:
    """One broken server must not take the whole API down: log it and carry on without it."""
    for name, category, module in BUILTIN_SERVERS:
        try:
            count = await gateway.add_server(name, category, stdio_params(module), persistent=True)
            log.info("MCP server '%s' ready with %d tools", name, count)
        except Exception:
            log.exception("MCP server '%s' failed to start; its tools are unavailable", name)
