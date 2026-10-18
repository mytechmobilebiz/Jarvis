from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.models import Event
from app.workflow import N8nEngine, NoEngine, WebhookEngine, build_engine


def event() -> Event:
    return Event(id=7, kind="site.down", severity="critical", source="monitoring", title="Acme is DOWN",
                 detail={"site_id": 1}, at=datetime(2026, 10, 18, tzinfo=UTC), task_id=3)


async def test_webhook_engine_signs_each_event():
    seen = []
    engine = WebhookEngine(
        Settings(workflow_engine="webhook", workflow_webhook_url="https://hooks.example.test/cc", workflow_webhook_secret="s3cret-value"),
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(204)),
    )
    await engine.deliver(event())
    request = seen[0]
    body = request.content
    expected = hmac.new(b"s3cret-value", request.headers["x-cc-timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert request.headers["x-cc-signature"] == f"sha256={expected}"
    assert json.loads(body)["event_id"] == 7 and json.loads(body)["kind"] == "site.down"


async def test_webhook_engine_raises_on_failure():
    engine = WebhookEngine(
        Settings(workflow_engine="webhook", workflow_webhook_url="https://hooks.example.test/cc", workflow_webhook_secret="x" * 12),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await engine.deliver(event())


def test_webhook_engine_needs_url_and_secret():
    with pytest.raises(ValueError):
        WebhookEngine(Settings(workflow_engine="webhook"))


def test_engine_selection():
    assert isinstance(build_engine(Settings(workflow_engine="n8n")), N8nEngine)
    assert isinstance(build_engine(Settings(workflow_engine="none")), NoEngine)
    assert not build_engine(Settings(workflow_engine="none")).delivers
