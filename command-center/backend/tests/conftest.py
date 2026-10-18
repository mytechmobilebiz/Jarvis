from __future__ import annotations

import os

# Configure before any app module reads settings.
os.environ.setdefault("TEST_DATABASE_URL", "postgresql+asyncpg://cc:cc@127.0.0.1:55432/cc_test")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["ENVIRONMENT"] = "test"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "correct-horse-battery-staple"
os.environ["CC_SERVICE_TOKEN"] = "test-service-token-0123456789"
os.environ["N8N_BASE_URL"] = "http://n8n.test:5678"
os.environ["N8N_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["MONITOR_RETRY_DELAY_SECONDS"] = "0"
os.environ["LLM_PROVIDER"] = "rule_based"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("https_proxy", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app.bootstrap import ensure_admin  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.integrations.n8n import N8nClient  # noqa: E402
from app.llm import NullProvider  # noqa: E402
from app.security import CSRF_HEADER, CSRF_VALUE  # noqa: E402
from app.worker.runner import Worker  # noqa: E402

from .fakes import FakeN8n, FakeWeb  # noqa: E402

ADMIN = (os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"])
SERVICE_TOKEN = os.environ["CC_SERVICE_TOKEN"]


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
async def migrated(settings):
    engine = db.get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await db.run_migrations()
    yield engine
    await db.dispose_engine()


@pytest.fixture
async def clean_db(migrated, settings):
    async with migrated.begin() as conn:
        tables = [r[0] for r in await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")
        )]
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    await ensure_admin(settings)
    from app.agents.wordpress_monitor import agent as wp_agent

    wp_agent._LATEST_CACHE.clear()
    yield migrated


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def n8n() -> FakeN8n:
    return FakeN8n()


@pytest.fixture
def make_worker(settings, web, n8n):
    def build(**kwargs):
        kwargs.setdefault("llm", NullProvider())
        kwargs.setdefault("n8n", N8nClient(settings, transport=n8n.transport))
        kwargs.setdefault("http_transport", web.transport)
        return Worker(kwargs.pop("settings", settings), worker_id="test-worker", **kwargs)

    return build


@pytest.fixture
async def app_client(clean_db):
    from app.main import create_app

    app = create_app(run_bootstrap=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cc.test") as client:
        yield client


@pytest.fixture
async def admin(app_client):
    """The test client, signed in as the admin, sending the CSRF header."""
    r = await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200, r.text
    app_client.headers[CSRF_HEADER] = CSRF_VALUE
    return app_client


@pytest.fixture
def service_headers():
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}
