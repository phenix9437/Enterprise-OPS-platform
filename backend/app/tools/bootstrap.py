"""Starts built-in MCP servers with narrowly scoped subprocess environments."""
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

SAFE_OS_ENV_KEYS = {
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
}

SERVER_ENV_KEYS = {
    "app.mcp_servers.business_db": {
        "DATABASE_URL", "BUSINESS_DATABASE_URL", "SEED_DEMO_DATA",
    },
    "app.mcp_servers.gmail_server": {
        "GMAIL_BACKEND", "GOOGLE_CREDENTIALS_FILE",
        "GMAIL_ALLOWED_RECIPIENT_DOMAINS",
    },
    "app.mcp_servers.drive_server": {
        "DRIVE_BACKEND", "GOOGLE_CREDENTIALS_FILE",
    },
}


def stdio_params(module: str, extra_env: dict | None = None) -> StdioServerParameters:
    allowed_keys = SERVER_ENV_KEYS.get(module)
    if allowed_keys is None:
        raise ValueError(f"No environment allowlist defined for MCP server: {module}")

    child_env = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_OS_ENV_KEYS
    }

    if extra_env:
        child_env.update({
            key: str(value)
            for key, value in extra_env.items()
            if key in allowed_keys and value is not None
        })

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env=child_env,
        cwd=str(BACKEND_DIR),
    )


def build_gateway(settings: Settings) -> ToolGateway:
    disabled = {t.strip() for t in settings.tools_disabled.split(",") if t.strip()}
    return ToolGateway(
        policy=ToolPolicy(disabled),
        timeout=settings.tool_call_timeout_seconds,
        max_retries=settings.tool_max_retries,
        backoff=settings.tool_retry_backoff_seconds,
    )


async def start_builtin_servers(gateway: ToolGateway, settings: Settings) -> None:
    """Start each MCP server with only the configuration it requires."""
    server_envs = {
        "business_db": {
            "DATABASE_URL": settings.database_url,
            "BUSINESS_DATABASE_URL": settings.business_database_url,
            "SEED_DEMO_DATA": settings.seed_demo_data,
        },
        "gmail": {
            "GMAIL_BACKEND": settings.gmail_backend,
            "GOOGLE_CREDENTIALS_FILE": settings.google_credentials_file,
            "GMAIL_ALLOWED_RECIPIENT_DOMAINS": settings.gmail_allowed_recipient_domains,
        },
        "drive": {
            "DRIVE_BACKEND": settings.drive_backend,
            "GOOGLE_CREDENTIALS_FILE": settings.google_credentials_file,
        },
    }

    for name, category, module in BUILTIN_SERVERS:
        try:
            count = await gateway.add_server(
                name,
                category,
                stdio_params(module, server_envs[name]),
                persistent=True,
            )
            log.info("MCP server '%s' ready with %d tools", name, count)
        except Exception:
            log.exception(
                "MCP server '%s' failed to start; its tools are unavailable",
                name,
            )
