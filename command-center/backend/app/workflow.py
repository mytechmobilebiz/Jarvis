"""The workflow-engine boundary.

The Command Center never depends on n8n directly: the worker hands events to
whatever WorkflowEngine is configured (WORKFLOW_ENGINE), and engines call back
through the engine-neutral endpoints under /api/integrations/workflow/ with
the service token. n8n is one implementation; the generic signed webhook lets
any engine that can receive an HTTPS POST and verify an HMAC take its place
(a commercially licensed n8n, Windmill, Node-RED, Make, a customer's own
system), and "none" runs the platform with no engine at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Protocol

import httpx

from .config import Settings
from .events import event_payload
from .integrations.n8n import N8nClient
from .models import Event

SIGNATURE_HEADER = "X-CC-Signature"
TIMESTAMP_HEADER = "X-CC-Timestamp"


class WorkflowEngine(Protocol):
    name: str
    title: str
    delivers: bool  # False: events are recorded but not handed anywhere

    async def deliver(self, event: Event) -> None: ...
    async def health(self) -> dict[str, Any]: ...
    async def list_workflows(self) -> list[dict[str, Any]] | None: ...


class N8nEngine:
    name = "n8n"
    title = "n8n workflow engine"
    delivers = True

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client = N8nClient(settings, transport=transport)

    async def deliver(self, event: Event) -> None:
        await self.client.send_event(event)

    async def health(self) -> dict[str, Any]:
        return await self.client.health()

    async def list_workflows(self) -> list[dict[str, Any]] | None:
        return await self.client.list_workflows()


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookEngine:
    """POSTs each event as JSON, signed: X-CC-Signature = sha256=HMAC(secret, "<timestamp>.<body>")."""

    name = "webhook"
    title = "Generic signed webhook"
    delivers = True

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.workflow_webhook_url:
            raise ValueError("WORKFLOW_ENGINE=webhook needs WORKFLOW_WEBHOOK_URL")
        if not settings.workflow_webhook_secret:
            raise ValueError("WORKFLOW_ENGINE=webhook needs WORKFLOW_WEBHOOK_SECRET")
        self.url = settings.workflow_webhook_url
        self.secret = settings.workflow_webhook_secret.get_secret_value()
        self.transport = transport

    async def deliver(self, event: Event) -> None:
        body = json.dumps(event_payload(event), separators=(",", ":"), default=str).encode("utf-8")
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sign(self.secret, ts, body)}
        async with httpx.AsyncClient(timeout=10.0, transport=self.transport) as client:
            response = await client.post(self.url, content=body, headers=headers)
        response.raise_for_status()

    async def health(self) -> dict[str, Any]:
        return {"reachable": None, "ready": None, "note": "webhook engines are checked by delivering events"}

    async def list_workflows(self) -> list[dict[str, Any]] | None:
        return None


class NoEngine:
    name = "none"
    title = "No workflow engine"
    delivers = False

    async def deliver(self, event: Event) -> None:
        raise RuntimeError("no workflow engine is configured")

    async def health(self) -> dict[str, Any]:
        return {"reachable": None, "ready": None, "note": "no workflow engine configured"}

    async def list_workflows(self) -> list[dict[str, Any]] | None:
        return None


def build_engine(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> WorkflowEngine:
    if settings.workflow_engine == "n8n":
        return N8nEngine(settings, transport=transport)
    if settings.workflow_engine == "webhook":
        return WebhookEngine(settings, transport=transport)
    return NoEngine()
