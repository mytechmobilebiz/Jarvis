"""Talking to the n8n instance that runs beside the Command Center.

Outbound: events (site down, approval needed, ...) are POSTed to an n8n
webhook, where workflows route them. Inbound: n8n workflows call the API back
with the service token (see app/api/integrations.py).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..events import event_payload
from ..models import Event

WEBHOOK_SECRET_HEADER = "X-CC-Webhook-Secret"


class N8nClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.base_url = settings.n8n_base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        # trust_env=False: n8n is on the private Docker network, never via a proxy.
        return httpx.AsyncClient(timeout=10.0, transport=self._transport, trust_env=False)

    @property
    def events_webhook_url(self) -> str:
        return f"{self.base_url}/webhook/{self.settings.n8n_events_webhook_path}"

    async def health(self) -> dict[str, Any]:
        async with self._client() as client:
            live = await client.get(f"{self.base_url}/healthz")
            ready = await client.get(f"{self.base_url}/healthz/readiness")
        return {
            "reachable": live.status_code == 200,
            "ready": ready.status_code == 200,
            "status_code": live.status_code,
        }

    async def send_event(self, event: Event) -> int:
        headers = {}
        if self.settings.n8n_webhook_secret:
            headers[WEBHOOK_SECRET_HEADER] = self.settings.n8n_webhook_secret.get_secret_value()
        async with self._client() as client:
            response = await client.post(self.events_webhook_url, json=event_payload(event), headers=headers)
        response.raise_for_status()
        return response.status_code

    async def list_workflows(self) -> list[dict[str, Any]] | None:
        """Workflows via n8n's public API; None when no N8N_API_KEY is configured."""
        if not self.settings.n8n_api_key:
            return None
        async with self._client() as client:
            response = await client.get(
                f"{self.base_url}/api/v1/workflows",
                headers={"X-N8N-API-KEY": self.settings.n8n_api_key.get_secret_value()},
            )
        response.raise_for_status()
        return [
            {"id": w.get("id"), "name": w.get("name"), "active": w.get("active")}
            for w in response.json().get("data", [])
        ]
