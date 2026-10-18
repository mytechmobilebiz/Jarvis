"""Customer settings and secrets, editions and licenses, users, workflow-engine choice."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select, text

from app import customer_settings, licensing
from app.config import Settings, get_settings
from app.db import session_scope
from app.licensing import sign_license
from app.llm import NullProvider, runtime_provider
from app.llm.anthropic_provider import AnthropicProvider
from app.models import Event
from app.secrets import SecretsUnavailable, put_secret
from app.workflow import NoEngine

from ..conftest import ADMIN

KEY = "sk-ant-api03-test-0000000000000000000000000000abcd"


@pytest.fixture
def signer(app_client, settings):
    """Installs a license public key into the app's settings for the test."""
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    licensed = settings.model_copy(update={"license_public_key": public})
    app = app_client._transport.app
    app.dependency_overrides[get_settings] = lambda: licensed

    def make(edition: str, **extra) -> dict:
        payload = {"license_id": f"LIC-{edition}", "customer": "Test Customer", "edition": edition, "issued_at": "2026-10-18",
                   "expires_at": (datetime.now(UTC) + timedelta(days=30)).date().isoformat(), **extra}
        return sign_license(payload, private)

    yield make
    app.dependency_overrides.clear()
    licensing.clear_cache()


async def test_secret_is_write_only_and_encrypted(admin):
    assert (await admin.put("/api/settings/secrets/anthropic_api_key", json={"value": "not-a-key-123"})).status_code == 400
    r = await admin.put("/api/settings/secrets/anthropic_api_key", json={"value": KEY})
    assert r.status_code == 200 and r.json()["hint"] == "…abcd"
    page = (await admin.get("/api/settings")).json()
    assert page["secrets"]["anthropic_api_key"]["set"] is True
    assert KEY not in str(page)
    async with session_scope() as s:
        stored = (await s.execute(text("SELECT ciphertext FROM secret_values"))).scalar_one()
    assert KEY not in stored and stored.startswith("gAAAA")  # a Fernet token
    entries = (await admin.get("/api/audit")).json()["entries"]
    assert any(e["action"] == "secret.set" for e in entries) and KEY not in str(entries)
    assert (await admin.delete("/api/settings/secrets/anthropic_api_key")).json()["removed"] is True
    assert (await admin.put("/api/settings/secrets/unknown", json={"value": "x" * 10})).status_code == 404


async def test_secrets_need_the_encryption_key(clean_db):
    async with session_scope() as s:
        with pytest.raises(SecretsUnavailable):
            await put_secret(s, Settings(CC_SECRETS_KEY=""), "anthropic_api_key", KEY, "admin")


async def test_customer_settings_validation(admin):
    r = await admin.patch("/api/settings", json={"business_name": "Infinite Marketing", "timezone": "America/Chicago", "ai_daily_request_cap": 50})
    assert r.status_code == 200
    page = (await admin.get("/api/settings")).json()
    assert page["settings"]["timezone"]["value"] == "America/Chicago"
    assert page["settings"]["ai_daily_request_cap"]["value"] == 50
    assert "database_url" not in str(page["settings"])  # deployment settings never appear
    for bad in ({"timezone": "Mars/Base"}, {"ai_provider": "gpt"}, {"ai_daily_request_cap": 0}, {"database_url": "x"}):
        assert (await admin.patch("/api/settings", json=bad)).status_code == 400, bad


async def test_customer_key_switches_the_ai_provider(admin, settings):
    async with session_scope() as s:
        assert isinstance(await runtime_provider(s, settings), NullProvider)
    await admin.put("/api/settings/secrets/anthropic_api_key", json={"value": KEY})
    await admin.patch("/api/settings", json={"ai_provider": "auto", "ai_model": "claude-sonnet-5-5", "ai_daily_request_cap": 25})
    async with session_scope() as s:
        provider = await runtime_provider(s, settings)
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-sonnet-5-5" and provider.settings.llm_daily_request_cap == 25
    status = (await admin.get("/api/system/status")).json()["llm"]
    assert status["configured"] and status["model"] == "claude-sonnet-5-5"
    await admin.patch("/api/settings", json={"ai_provider": "rule_based"})
    async with session_scope() as s:
        assert isinstance(await runtime_provider(s, settings), NullProvider)


async def test_settings_are_admin_only(app_client):
    from app.models import User
    from app.security import hash_password

    async with session_scope() as s:
        s.add(User(email="op@test.local", password_hash=hash_password("operator-password"), role="operator"))
    await app_client.post("/api/auth/login", json={"email": "op@test.local", "password": "operator-password"})
    app_client.headers["x-requested-with"] = "command-center"
    assert (await app_client.get("/api/settings")).status_code == 403
    assert (await app_client.put("/api/settings/secrets/anthropic_api_key", json={"value": KEY})).status_code == 403
    assert (await app_client.get("/api/users")).status_code == 403


async def test_default_edition_is_developer(admin):
    edition = (await admin.get("/api/system/edition")).json()
    assert edition["edition"] == "developer" and edition["source"] == "configuration"
    assert edition["limits"] == {"max_sites": None, "max_users": None}


async def test_personal_license_limits_sites_users_and_agents(admin, signer, gate_agent):
    r = await admin.post("/api/system/license", json={"license": signer("personal")})
    assert r.status_code == 200, r.text
    edition = (await admin.get("/api/system/edition")).json()
    assert edition["edition"] == "personal" and edition["source"] == "license"
    for i in range(5):
        assert (await admin.post("/api/sites", json={"name": f"S{i}", "url": f"s{i}.example.test"})).status_code == 201
    r = await admin.post("/api/sites", json={"name": "S6", "url": "s6.example.test"})
    assert r.status_code == 403 and "up to 5" in r.json()["detail"]
    r = await admin.post("/api/users", json={"email": "two@test.local", "role": "viewer", "password": "viewer-password-1"})
    assert r.status_code == 403
    r = await admin.post("/api/agents/gate_test/tasks", json={"capability": "publish", "params": {"title": "x"}})
    assert r.status_code == 400 and "not included in the Personal edition" in r.json()["detail"]
    agents = {a["name"]: a for a in (await admin.get("/api/agents")).json()["agents"]}
    assert agents["wordpress_monitor"]["entitled"] and not agents["gate_test"]["entitled"]


async def test_bad_and_removed_licenses(admin, signer):
    doc = signer("agency")
    doc["payload"]["max_sites"] = 9999
    assert (await admin.post("/api/system/license", json={"license": doc})).status_code == 400
    assert (await admin.post("/api/system/license", json={"license": "{not json"})).status_code == 400
    assert (await admin.post("/api/system/license", json={"license": signer("agency")})).status_code == 200
    assert (await admin.delete("/api/system/license")).status_code == 200
    assert (await admin.get("/api/system/edition")).json()["edition"] == "developer"


async def test_user_management(admin, app_client):
    r = await admin.post("/api/users", json={"email": "Op@Test.local", "role": "operator", "password": "operator-password-1"})
    assert r.status_code == 201 and r.json()["user"]["email"] == "op@test.local"
    assert (await admin.post("/api/users", json={"email": "op@test.local", "role": "viewer", "password": "x" * 12})).status_code == 409
    users = (await admin.get("/api/users")).json()["users"]
    me = next(u for u in users if u["email"] == ADMIN[0])
    # the last admin cannot be demoted or deactivated
    assert (await admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"})).status_code == 400
    assert (await admin.patch(f"/api/users/{me['id']}", json={"is_active": False})).status_code == 400
    op = next(u for u in users if u["email"] == "op@test.local")
    assert (await admin.patch(f"/api/users/{op['id']}", json={"is_active": False})).json()["user"]["is_active"] is False
    r = await app_client.post("/api/auth/login", json={"email": "op@test.local", "password": "operator-password-1"})
    assert r.status_code == 401


async def test_no_workflow_engine_skips_delivery(admin, make_worker, web):
    from ..fakes import static_site

    web.add("down.example.test", static_site("x", status=500))
    site = (await admin.post("/api/sites", json={"name": "Down", "url": "https://down.example.test"})).json()["site"]
    await admin.post(f"/api/sites/{site['id']}/check", json={"deep": False})
    await make_worker(engine=NoEngine()).run_until_idle()
    async with session_scope() as s:
        event = await s.scalar(select(Event).where(Event.kind == "site.down"))
    assert event.forward_status == "skipped" and "no workflow engine" in event.forward_error
