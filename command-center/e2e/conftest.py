"""End-to-end tests against a running stack (docker compose ... testing overlay).

    docker compose -f docker-compose.yml -f docker-compose.testing.yml up -d --build
    pytest e2e

Settings come from command-center/.env; CC_BASE_URL overrides the dashboard URL.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.testing.yml")]
TEST_SITE_URL = "http://wp-test"
TEST_SITE_NAME = "Local test WordPress"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    return read_env()


@pytest.fixture(scope="session")
def base_url() -> str:
    port = read_env().get("DASHBOARD_PORT", "8090")
    return os.environ.get("CC_BASE_URL", f"http://127.0.0.1:{port}")


@pytest.fixture(scope="session")
def api(base_url, env):
    client = httpx.Client(base_url=base_url, timeout=30, trust_env=False)
    r = client.post("/api/auth/login", json={"email": env["ADMIN_EMAIL"], "password": env["ADMIN_PASSWORD"]})
    assert r.status_code == 200, r.text
    client.headers["X-Requested-With"] = "command-center"
    yield client
    client.close()


@pytest.fixture(scope="session")
def service_headers(env) -> dict[str, str]:
    return {"Authorization": f"Bearer {env['CC_SERVICE_TOKEN']}"}


def wait_for(predicate, timeout: float = 90, interval: float = 1.5, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}; last value: {last!r}")


def compose(*args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run([*COMPOSE, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="session")
def test_site(api) -> dict:
    sites = api.get("/api/sites").json()["sites"]
    site = next((s for s in sites if s["url"] == TEST_SITE_URL), None)
    if site is None:
        r = api.post("/api/sites", json={"name": TEST_SITE_NAME, "url": TEST_SITE_URL, "client": "Test only"})
        assert r.status_code == 201, r.text
        site = r.json()["site"]
    return site


def wait_task(api, task_id: int, timeout: float = 120) -> dict:
    return wait_for(
        lambda: (t := api.get(f"/api/tasks/{task_id}").json()["task"])["status"] in ("succeeded", "failed") and t,
        timeout,
        what=f"task {task_id}",
    )


def wait_request(api, request_id: int, timeout: float = 120) -> dict:
    return wait_for(
        lambda: (d := api.get(f"/api/supervisor/requests/{request_id}").json())["request"]["status"] in ("completed", "failed") and d,
        timeout,
        what=f"supervisor request {request_id}",
    )
