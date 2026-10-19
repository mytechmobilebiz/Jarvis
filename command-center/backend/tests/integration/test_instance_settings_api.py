"""SESSION_COOKIE_NAME and CC_INSTANCE_LABEL through the API: the test copy (cc_session_brain_test, "cc-brain-test")
and the defaults of a live installation (cc_session, no label)."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.security import CSRF_HEADER, CSRF_VALUE

from ..conftest import ADMIN

TEST_COOKIE = "cc_session_brain_test"


@pytest.fixture
async def test_copy_client(clean_db, settings):
    """An API configured like the JARVIS Brain test copy (docker-compose.brain-test.yml)."""
    from app.main import create_app

    app = create_app(run_bootstrap=False)
    copy = settings.model_copy(update={"session_cookie_name": TEST_COOKIE, "instance_label": "cc-brain-test"})
    app.dependency_overrides[get_settings] = lambda: copy
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cc.test") as client:
        yield client


async def test_a_live_installation_keeps_cc_session_and_has_no_label(app_client):
    r = await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200
    assert r.cookies.get("cc_session") and r.cookies.get(TEST_COOKIE) is None
    status = (await app_client.get("/api/system/status")).json()
    assert status["instance_label"] == ""


def only(client: httpx.AsyncClient, name: str, value: str) -> None:
    """The client's cookie jar holds exactly this one cookie, as a browser sending it to cc.test would."""
    client.cookies.clear()
    client.cookies.set(name, value, domain="cc.test")


async def test_the_test_copy_signs_in_with_its_own_cookie_and_shows_its_label(test_copy_client):
    client = test_copy_client
    r = await client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200
    token = r.cookies.get(TEST_COOKIE)
    assert token and r.cookies.get("cc_session") is None
    assert r.headers["set-cookie"].startswith(TEST_COOKIE + "=")
    status = (await client.get("/api/system/status")).json()
    assert status["instance_label"] == "cc-brain-test"

    # The live installation's cookie name means nothing here, even with a valid token: no cross-sign-in.
    only(client, "cc_session", token)
    assert (await client.get("/api/auth/me")).status_code == 401
    only(client, TEST_COOKIE, token)
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_password_change_and_logout_use_the_configured_cookie(test_copy_client):
    client = test_copy_client
    first = await client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    other = first.cookies.get(TEST_COOKIE)
    client.cookies.clear()
    second = await client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    current = second.cookies.get(TEST_COOKIE)
    assert other and current and other != current
    only(client, TEST_COOKIE, current)
    client.headers[CSRF_HEADER] = CSRF_VALUE
    new_password = "-".join(["renewed", "pass", "phrase", "42"])
    r = await client.post("/api/auth/password", json={"current_password": ADMIN[1], "new_password": new_password})
    assert r.status_code == 200
    # This session survives (its cookie was read under the configured name); the other one is signed out.
    assert (await client.get("/api/auth/me")).status_code == 200
    only(client, TEST_COOKIE, other)
    assert (await client.get("/api/auth/me")).status_code == 401

    only(client, TEST_COOKIE, current)
    r = await client.post("/api/auth/logout")
    assert r.status_code == 200
    cleared = r.headers["set-cookie"]
    assert cleared.startswith(TEST_COOKIE + "=") and "Max-Age=0" in cleared
    only(client, TEST_COOKIE, current)
    assert (await client.get("/api/auth/me")).status_code == 401
