"""Server-level tests: talk to each MCP server over the real MCP protocol, no gateway involved."""
from mcp.client import Client

from app.mcp_servers import business_db, drive_server, gmail_server
from app.mcp_servers.backends import MockDriveBackend, MockGmailBackend
from tests.conftest import FIXED_TODAY


async def call(server, tool, args):
    async with Client(server) as c:
        r = await c.call_tool(tool, args)
    return r


def text(r):
    return r.content[0].text


async def test_gmail_search_operators_and_thread():
    srv = gmail_server.create_server(MockGmailBackend(FIXED_TODAY))
    async with Client(srv) as c:
        r = await c.call_tool("search_emails", {"query": "from:cedarlogistics INV-1003"})
        assert not r.is_error and '"count": 1' in text(r)
        r = await c.call_tool("get_thread", {"thread_id": "t-101"})
        assert "420" in text(r) and "corrected invoice" in text(r)
        r = await c.call_tool("get_thread", {"thread_id": "nope"})
        assert r.is_error and "not found" in text(r)


async def test_gmail_rejects_bad_recipient_and_header_injection():
    srv = gmail_server.create_server(MockGmailBackend(FIXED_TODAY))
    bad = await call(srv, "send_email", {"to": "not-an-email", "subject": "x", "body": "y"})
    assert bad.is_error and "valid" in text(bad)
    inj = await call(srv, "send_email", {"to": "a@b.example", "subject": "hi\nBcc: evil@x.example", "body": "y"})
    assert inj.is_error and "Line breaks" in text(inj)


async def test_gmail_recipient_domain_allowlist():
    backend = MockGmailBackend(FIXED_TODAY)
    srv = gmail_server.create_server(backend, allowed_domains={"cedarlogistics.example"})
    blocked = await call(srv, "send_email", {"to": "x@other.example", "subject": "s", "body": "b"})
    assert blocked.is_error and "allowed list" in text(blocked)
    ok = await call(srv, "send_email", {"to": "ap@cedarlogistics.example", "subject": "s", "body": "b"})
    assert not ok.is_error
    assert (await backend.search("subject:s", 5))  # actually sent


async def test_drive_search_read_and_truncate():
    srv = drive_server.create_server(MockDriveBackend(FIXED_TODAY))
    async with Client(srv) as c:
        r = await c.call_tool("search_files", {"query": "payment terms"})
        assert '"f-1"' in text(r)
        r = await c.call_tool("read_file", {"file_id": "f-1", "max_chars": 100})
        assert '"truncated": true' in text(r)
        r = await c.call_tool("read_file", {"file_id": "zzz"})
        assert r.is_error


async def test_business_tools_validate_inputs_and_business_rules(gateway):
    # reach the in-process business server through the gateway's connection (no DB/auth involved)
    conn = gateway._connections["business_db"]
    r = await conn.call_tool("update_purchase_request_status", {"request_id": 3, "new_status": "approved"})
    assert r.is_error and "Cannot change status" in text(r)          # already approved -> invalid transition
    r = await conn.call_tool("update_purchase_request_status", {"request_id": 1, "new_status": "approved"})
    assert not r.is_error
    r = await conn.call_tool("log_invoice_reminder", {"invoice_id": 6, "channel": "email", "note": "x"})
    assert r.is_error and "already paid" in text(r)                    # INV-1006 is paid
    r = await conn.call_tool("search_customers", {"query": "50%"})      # LIKE wildcard must be escaped
    assert not r.is_error and '"count": 0' in text(r)
