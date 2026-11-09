"""Spawns the REAL servers as subprocesses over stdio, exactly like production does."""
from app.schemas.plan import ToolCategory
from app.tools.bootstrap import stdio_params
from app.tools.gateway import ToolGateway, parse_tool_result


async def test_builtin_servers_work_over_real_stdio(tmp_path):
    env = {
        "JWT_SECRET": "test-only-secret-" + ("x" * 48),
        "BUSINESS_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/biz.db",
        "SEED_DEMO_DATA": "true",
        "GMAIL_BACKEND": "mock",
        "DRIVE_BACKEND": "mock",
    }

    gw = ToolGateway()
    try:
        n1 = await gw.add_server(
            "business_db",
            ToolCategory.DATABASE,
            stdio_params("app.mcp_servers.business_db", env),
            persistent=True,
        )
        n2 = await gw.add_server(
            "gmail",
            ToolCategory.GMAIL,
            stdio_params("app.mcp_servers.gmail_server", env),
            persistent=True,
        )
        n3 = await gw.add_server(
            "drive",
            ToolCategory.DRIVE,
            stdio_params("app.mcp_servers.drive_server", env),
            persistent=True,
        )

        assert (n1, n2, n3) == (8, 4, 2)
        assert gw.registry.get("gmail.send_email").risk.value == "high"

        raw = await gw._connections["business_db"].call_tool(
            "list_overdue_invoices", {"min_days_overdue": 1}
        )
        content, err = parse_tool_result(raw)
        assert err is None and content["count"] == 5
        assert content["invoices"][0]["number"] == "INV-1001"

        raw = await gw._connections["drive"].call_tool("read_file", {"file_id": "f-2"})
        content, err = parse_tool_result(raw)
        assert "polite" in content["content"]
    finally:
        await gw.close()
