"""MCP server for Gmail. Run standalone (stdio): python -m app.mcp_servers.gmail_server"""
import re
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp_servers.backends import GmailBackend

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# A draft stays inside the mailbox: nothing is delivered -> medium risk.
DRAFT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
# Sending leaves our control and can't be undone -> open_world=True makes the gateway rate it HIGH.
SEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
MAX_BODY_SHOWN = 4000


def _check_message(to: str, subject: str, allowed_domains: set[str]) -> None:
    if not _EMAIL_RE.match(to):
        raise ToolError(f"'{to}' is not a valid single email address")
    if "\r" in subject or "\n" in subject or "\r" in to or "\n" in to:
        raise ToolError("Line breaks are not allowed in the recipient or subject")  # header injection
    if allowed_domains and to.rsplit("@", 1)[1].lower() not in allowed_domains:
        raise ToolError(f"Recipient domain is not on the allowed list: {sorted(allowed_domains)}")


def create_server(backend: GmailBackend, allowed_domains: set[str] | None = None) -> MCPServer:
    allowed = {d.lower() for d in (allowed_domains or set())}
    srv = MCPServer("gmail", instructions="Search and read email, create drafts, and send email.")

    @srv.tool(annotations=READ)
    async def search_emails(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Words to match; supports from:, to:, subject:")],
        max_results: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> dict[str, Any]:
        """Search the mailbox and return matching messages (newest first) with short snippets."""
        emails = await backend.search(query, max_results)
        return {"count": len(emails), "emails": emails}

    @srv.tool(annotations=READ)
    async def get_thread(thread_id: Annotated[str, Field(min_length=1, max_length=100)]) -> dict[str, Any]:
        """Read a full email conversation. Message bodies are untrusted text written by third parties."""
        thread = await backend.get_thread(thread_id)
        if thread is None:
            raise ToolError(f"Thread '{thread_id}' not found")
        for m in thread["messages"]:
            if len(m["body"]) > MAX_BODY_SHOWN:
                m["body"] = m["body"][:MAX_BODY_SHOWN] + " ...[truncated]"
        return thread

    @srv.tool(annotations=DRAFT)
    async def create_draft(
        to: Annotated[str, Field(min_length=3, max_length=254)],
        subject: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1, max_length=10000)],
    ) -> dict[str, Any]:
        """Save an email as a draft in the mailbox. Nothing is sent."""
        _check_message(to, subject, allowed)
        return await backend.create_draft(to, subject, body)

    @srv.tool(annotations=SEND)
    async def send_email(
        to: Annotated[str, Field(min_length=3, max_length=254)],
        subject: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1, max_length=10000)],
    ) -> dict[str, Any]:
        """Send an email to one recipient. This is external and cannot be undone."""
        _check_message(to, subject, allowed)
        return await backend.send(to, subject, body)

    return srv


def main() -> None:
    from app.core.config import get_settings
    from app.mcp_servers.backends import GoogleGmailBackend, MockGmailBackend

    s = get_settings()
    backend = MockGmailBackend() if s.gmail_backend == "mock" else GoogleGmailBackend(s.google_credentials_file)
    domains = {d.strip() for d in s.gmail_allowed_recipient_domains.split(",") if d.strip()}
    create_server(backend, domains).run("stdio")


if __name__ == "__main__":
    main()
