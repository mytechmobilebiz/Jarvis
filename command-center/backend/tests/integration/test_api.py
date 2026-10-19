"""API, auth and permission tests against the PostgreSQL test database."""

from __future__ import annotations

from sqlalchemy import select, text

from app.db import session_scope
from app.models import AuditLog, User
from app.security import hash_password

from ..conftest import ADMIN


async def test_health_is_public(app_client):
    r = await app_client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_everything_else_needs_auth(app_client):
    for path in ("/api/system/status", "/api/agents", "/api/sites", "/api/tasks", "/api/approvals", "/api/events"):
        assert (await app_client.get(path)).status_code == 401, path


async def test_login_logout_and_me(app_client):
    r = await app_client.post("/api/auth/login", json={"email": ADMIN[0].upper(), "password": ADMIN[1]})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert (await app_client.get("/api/auth/me")).json()["user"]["email"] == ADMIN[0]
    assert (await app_client.post("/api/auth/logout")).status_code == 200
    app_client.cookies.clear()
    assert (await app_client.get("/api/auth/me")).status_code == 401


async def test_session_tokens_are_stored_hashed(app_client):
    r = await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    token = r.cookies.get("cc_session")
    async with session_scope() as s:
        stored = (await s.execute(text("SELECT token_hash FROM sessions"))).scalars().all()
    assert token not in stored and len(stored[0]) == 64


async def test_wrong_password_and_lockout(app_client):
    for _ in range(5):
        r = await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": "wrong"})
        assert r.status_code == 401
    r = await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 429
    async with session_scope() as s:
        failures = (await s.scalars(select(AuditLog).where(AuditLog.action == "auth.login_failed"))).all()
    assert len(failures) == 5


async def test_mutations_need_csrf_header(admin):
    del admin.headers["x-requested-with"]
    r = await admin.post("/api/sites", json={"name": "A", "url": "a.example.test"})
    assert r.status_code == 403


async def test_viewer_is_read_only(app_client):
    async with session_scope() as s:
        s.add(User(email="viewer@test.local", password_hash=hash_password("viewer-password-123"), role="viewer"))
    await app_client.post("/api/auth/login", json={"email": "viewer@test.local", "password": "viewer-password-123"})
    app_client.headers["x-requested-with"] = "command-center"
    assert (await app_client.get("/api/sites")).status_code == 200
    assert (await app_client.post("/api/sites", json={"name": "A", "url": "a.example.test"})).status_code == 403
    assert (await app_client.post("/api/supervisor/requests", json={"message": "check all websites"})).status_code == 403
    assert (await app_client.get("/api/audit")).status_code == 403


async def test_service_token_scope(app_client, service_headers):
    r = await app_client.post("/api/supervisor/requests", json={"message": "Check all websites"}, headers=service_headers)
    assert r.status_code == 202
    assert r.json()["request"]["source"] == "n8n"
    # cannot read the dashboard data, cannot approve, cannot change sites
    assert (await app_client.get("/api/sites", headers=service_headers)).status_code == 403
    assert (await app_client.post("/api/approvals/1/decision", json={"approve": True}, headers=service_headers)).status_code == 403
    assert (await app_client.post("/api/sites", json={"name": "x", "url": "x.test"}, headers=service_headers)).status_code == 403
    bad = {"Authorization": "Bearer wrong"}
    assert (await app_client.post("/api/supervisor/requests", json={"message": "x"}, headers=bad)).status_code == 401


async def test_sites_crud(admin):
    r = await admin.post("/api/sites", json={"name": "Acme", "url": "Acme.Example.test/", "client": "Acme Co"})
    assert r.status_code == 201
    site = r.json()["site"]
    assert site["url"] == "https://acme.example.test"
    assert site["last_status"] == "unknown"
    assert (await admin.post("/api/sites", json={"name": "Dup", "url": "https://acme.example.test"})).status_code == 409
    r = await admin.patch(f"/api/sites/{site['id']}", json={"enabled": False, "notes": "on hold"})
    assert r.json()["site"]["enabled"] is False
    assert len((await admin.get("/api/sites")).json()["sites"]) == 1
    assert (await admin.delete(f"/api/sites/{site['id']}")).status_code == 200
    assert (await admin.get(f"/api/sites/{site['id']}")).status_code == 404
    entries = (await admin.get("/api/audit")).json()["entries"]
    assert {"site.created", "site.updated", "site.deleted"} <= {e["action"] for e in entries}


async def test_invalid_site_url(admin):
    assert (await admin.post("/api/sites", json={"name": "Bad", "url": "https://"})).status_code == 422


async def test_agents_listing_and_toggle(admin):
    agents = (await admin.get("/api/agents")).json()["agents"]
    names = {a["name"] for a in agents}
    assert {"supervisor", "monitoring"} <= names
    wp = next(a for a in agents if a["name"] == "monitoring")
    assert all(c["risk"] == "read" and not c["requires_approval"] for c in wp["capabilities"])
    assert (await admin.patch("/api/agents/monitoring", json={"enabled": False})).json()["enabled"] is False
    r = await admin.post("/api/agents/monitoring/tasks", json={"capability": "check_url", "params": {"url": "a.test"}})
    assert r.status_code == 400 and "switched off" in r.json()["detail"]
    assert (await admin.patch("/api/agents/supervisor", json={"enabled": False})).status_code == 400


async def test_run_capability_validates_params(admin):
    r = await admin.post("/api/agents/monitoring/tasks", json={"capability": "check_site", "params": {"site_id": "abc"}})
    assert r.status_code == 400
    r = await admin.post("/api/agents/monitoring/tasks", json={"capability": "check_url", "params": {"url": "a.test", "evil": 1}})
    assert r.status_code == 400
    r = await admin.post("/api/agents/monitoring/tasks", json={"capability": "nope"})
    assert r.status_code == 400
    r = await admin.post("/api/agents/supervisor/tasks", json={"capability": "plan_request", "params": {"request_id": 1}})
    assert r.status_code == 400


async def test_schedules_seeded_and_editable(admin):
    from app.bootstrap import ensure_schedules

    await ensure_schedules()
    schedules = (await admin.get("/api/schedules")).json()["schedules"]
    names = {s["name"] for s in schedules}
    assert {"wordpress-uptime-sweep", "wordpress-security-scan", "supervisor-health-review"} <= names
    sid = schedules[0]["id"]
    assert (await admin.patch(f"/api/schedules/{sid}", json={"cron": "not a cron"})).status_code == 400
    r = await admin.patch(f"/api/schedules/{sid}", json={"cron": "*/5 * * * *", "enabled": False})
    assert r.json()["schedule"]["cron"] == "*/5 * * * *" and r.json()["schedule"]["enabled"] is False


async def test_system_status(admin):
    status = (await admin.get("/api/system/status")).json()
    assert status["database"]["ok"]
    assert status["llm"]["provider"] == "rule-based"
    assert status["counts"]["approvals_pending"] == 0


async def test_integrations_listing_marks_mocks(admin):
    items = (await admin.get("/api/integrations")).json()["integrations"]
    by_name = {i["name"]: i for i in items}
    assert by_name["n8n"]["mode"] == "live"
    assert by_name["anthropic"]["mode"] == "disabled"


async def test_password_change(admin):
    r = await admin.post("/api/auth/password", json={"current_password": "bad", "new_password": "a-much-longer-password"})
    assert r.status_code == 400
    r = await admin.post("/api/auth/password", json={"current_password": ADMIN[1], "new_password": "short"})
    assert r.status_code == 422
    r = await admin.post("/api/auth/password", json={"current_password": ADMIN[1], "new_password": "a-much-longer-password"})
    assert r.status_code == 200
