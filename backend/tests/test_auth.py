from tests.conftest import login_headers, register


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_first_user_is_admin_next_is_viewer(client):
    a = await register(client, "admin@example.com")
    b = await register(client, "bob@example.com")
    assert a.status_code == 201 and a.json()["role"] == "admin"
    assert b.json()["role"] == "viewer"
    assert "hashed_password" not in a.json()


async def test_duplicate_email_rejected_case_insensitive(client):
    await register(client, "dup@example.com")
    r = await register(client, "DUP@example.com")
    assert r.status_code == 409


async def test_weak_or_oversized_password_rejected(client):
    assert (await register(client, "a@example.com", password="short")).status_code == 422
    assert (await register(client, "b@example.com", password="x" * 100)).status_code == 422


async def test_login_success_and_me(client):
    await register(client, "admin@example.com")
    h = await login_headers(client, "admin@example.com")
    r = await client.get("/api/v1/users/me", headers=h)
    assert r.status_code == 200 and r.json()["email"] == "admin@example.com"


async def test_login_failure(client):
    await register(client, "admin@example.com")
    bad_pw = await client.post("/api/v1/auth/login", data={"username": "admin@example.com", "password": "wrong-pass"})
    no_user = await client.post("/api/v1/auth/login", data={"username": "ghost@example.com", "password": "whatever1"})
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json() == no_user.json()  # no user enumeration


async def test_protected_route_needs_token(client):
    assert (await client.get("/api/v1/users/me")).status_code == 401
    r = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


async def test_refresh_flow_and_token_type_confusion(client):
    await register(client, "admin@example.com")
    r = await client.post("/api/v1/auth/login", data={"username": "admin@example.com", "password": "Passw0rd!x"})
    tokens = r.json()

    new = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert new.status_code == 200

    # access token must not work as refresh, and refresh must not work as access
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]})).status_code == 401
    h = {"Authorization": f"Bearer {tokens['refresh_token']}"}
    assert (await client.get("/api/v1/users/me", headers=h)).status_code == 401


async def test_rbac_viewer_cannot_list_users_and_denial_is_audited(client):
    await register(client, "admin@example.com")
    await register(client, "bob@example.com")
    bob = await login_headers(client, "bob@example.com")
    assert (await client.get("/api/v1/users", headers=bob)).status_code == 403

    admin = await login_headers(client, "admin@example.com")
    logs = (await client.get("/api/v1/audit-logs", headers=admin)).json()
    assert any(l["action"] == "rbac.denied" and l["status"] == "denied" for l in logs)


async def test_admin_changes_role_and_is_audited(client):
    await register(client, "admin@example.com")
    bob = (await register(client, "bob@example.com")).json()
    admin = await login_headers(client, "admin@example.com")

    r = await client.patch(f"/api/v1/users/{bob['id']}/role", json={"role": "operator"}, headers=admin)
    assert r.status_code == 200 and r.json()["role"] == "operator"

    logs = (await client.get("/api/v1/audit-logs", headers=admin)).json()
    change = next(l for l in logs if l["action"] == "user.role_change")
    assert change["details"] == {"from": "viewer", "to": "operator"}


async def test_admin_cannot_change_own_role(client):
    me = (await register(client, "admin@example.com")).json()
    admin = await login_headers(client, "admin@example.com")
    r = await client.patch(f"/api/v1/users/{me['id']}/role", json={"role": "viewer"}, headers=admin)
    assert r.status_code == 400


async def test_failed_login_is_audited(client):
    await register(client, "admin@example.com")
    await client.post("/api/v1/auth/login", data={"username": "admin@example.com", "password": "nope-nope"})
    admin = await login_headers(client, "admin@example.com")
    logs = (await client.get("/api/v1/audit-logs", headers=admin)).json()
    assert any(l["action"] == "auth.login" and l["status"] == "failure" for l in logs)
