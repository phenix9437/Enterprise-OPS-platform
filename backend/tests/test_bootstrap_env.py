import pytest

from app.tools.bootstrap import stdio_params


def test_business_db_receives_only_its_allowlisted_environment(monkeypatch):
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("JWT_SECRET", "sensitive-jwt")
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive-openai")
    monkeypatch.setenv("DATABASE_URL", "ambient-database-url")

    params = stdio_params(
        "app.mcp_servers.business_db",
        {
            "DATABASE_URL": "business-fallback-url",
            "BUSINESS_DATABASE_URL": "dedicated-business-url",
            "SEED_DEMO_DATA": False,
            "JWT_SECRET": "injected-jwt",
            "OPENAI_API_KEY": "injected-openai",
            "GMAIL_BACKEND": "google",
        },
    )

    assert params.env["PATH"] == "safe-path"
    assert params.env["DATABASE_URL"] == "business-fallback-url"
    assert params.env["BUSINESS_DATABASE_URL"] == "dedicated-business-url"
    assert params.env["SEED_DEMO_DATA"] == "False"
    assert "JWT_SECRET" not in params.env
    assert "OPENAI_API_KEY" not in params.env
    assert "GMAIL_BACKEND" not in params.env


def test_gmail_receives_only_its_allowlisted_environment(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "sensitive-jwt")
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive-openai")
    monkeypatch.setenv("DATABASE_URL", "sensitive-database")

    params = stdio_params(
        "app.mcp_servers.gmail_server",
        {
            "GMAIL_BACKEND": "mock",
            "GOOGLE_CREDENTIALS_FILE": "/tmp/google.json",
            "GMAIL_ALLOWED_RECIPIENT_DOMAINS": "example.com",
            "DATABASE_URL": "injected-database",
            "JWT_SECRET": "injected-jwt",
            "OPENAI_API_KEY": "injected-openai",
        },
    )

    assert params.env["GMAIL_BACKEND"] == "mock"
    assert params.env["GOOGLE_CREDENTIALS_FILE"] == "/tmp/google.json"
    assert params.env["GMAIL_ALLOWED_RECIPIENT_DOMAINS"] == "example.com"
    assert "DATABASE_URL" not in params.env
    assert "JWT_SECRET" not in params.env
    assert "OPENAI_API_KEY" not in params.env


def test_drive_receives_only_its_allowlisted_environment(monkeypatch):
    params = stdio_params(
        "app.mcp_servers.drive_server",
        {
            "DRIVE_BACKEND": "mock",
            "GOOGLE_CREDENTIALS_FILE": "/tmp/google.json",
            "DATABASE_URL": "injected-database",
            "GMAIL_BACKEND": "google",
            "JWT_SECRET": "injected-jwt",
            "OPENAI_API_KEY": "injected-openai",
        },
    )

    assert params.env["DRIVE_BACKEND"] == "mock"
    assert params.env["GOOGLE_CREDENTIALS_FILE"] == "/tmp/google.json"
    assert "DATABASE_URL" not in params.env
    assert "GMAIL_BACKEND" not in params.env
    assert "JWT_SECRET" not in params.env
    assert "OPENAI_API_KEY" not in params.env


def test_unknown_mcp_module_is_rejected():
    with pytest.raises(ValueError, match="No environment allowlist"):
        stdio_params("app.mcp_servers.unknown")
