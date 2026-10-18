"""Test doubles: a fake website served through httpx.MockTransport, a fake LLM, a fake n8n."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from app.llm.base import LLMError, LLMProvider

WP_HOME = """<!doctype html><html><head>
<meta name="generator" content="WordPress 6.4.1" />
<link rel="https://api.w.org/" href="{origin}/wp-json/" />
<link rel="stylesheet" href="{origin}/wp-content/themes/astra/style.css?ver=6.4.1" />
<script src="{origin}/wp-content/plugins/contact-form-7/includes/js/index.js"></script>
</head><body><h1>Acme Plumbing</h1><p>Welcome.</p></body></html>"""

WP_REST_INDEX = {
    "name": "Acme Plumbing",
    "description": "Just another WordPress site",
    "namespaces": ["oembed/1.0", "wp/v2", "wp-site-health/v1", "yoast/v1", "contact-form-7/v1", "wc/v3"],
}
WP_FEED = """<?xml version="1.0"?><rss><channel><generator>https://wordpress.org/?v=6.4.1</generator></channel></rss>"""
WP_VERSION_API = {"offers": [{"response": "upgrade", "current": "6.8.3", "version": "6.8.3"}]}


class FakeWeb:
    """A tiny fake internet. Register handlers per host; every request is recorded."""

    def __init__(self) -> None:
        self.sites: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.add("api.wordpress.org", lambda r: httpx.Response(200, json=WP_VERSION_API))

    def add(self, host: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.sites[host] = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        site = self.sites.get(request.url.host)
        if site is None:
            raise httpx.ConnectError(f"[Errno -2] Name or service not known: {request.url.host}", request=request)
        return site(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self, host: str) -> list[str]:
        return [r.url.path for r in self.requests if r.url.host == host]

    def methods(self) -> set[str]:
        return {r.method for r in self.requests}


def wordpress_site(
    origin: str,
    *,
    version: str = "6.4.1",
    exposed: bool = True,
    home_status: int = 200,
    home_html: str | None = None,
    headers: dict[str, str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    html = (home_html if home_html is not None else WP_HOME).replace("{origin}", origin).replace("6.4.1", version)

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        base_headers = {"content-type": "text/html; charset=UTF-8", **(headers or {})}
        if path in ("", "/"):
            return httpx.Response(home_status, text=html, headers=base_headers)
        if path == "/wp-json/":
            return httpx.Response(200, json=WP_REST_INDEX)
        if path == "/feed/":
            return httpx.Response(200, text=WP_FEED.replace("6.4.1", version), headers={"content-type": "application/rss+xml"})
        if not exposed:
            return httpx.Response(404, text="<html>Not found</html>", headers={"content-type": "text/html"})
        if path == "/xmlrpc.php":
            return httpx.Response(405, text="XML-RPC server accepts POST requests only.")
        if path == "/wp-json/wp/v2/users":
            return httpx.Response(200, json=[{"id": 1, "slug": "admin"}, {"id": 2, "slug": "editor"}])
        if path == "/readme.html":
            return httpx.Response(200, text="<html><h1>WordPress</h1> Version 6.4</html>")
        if path == "/wp-content/debug.log":
            return httpx.Response(
                206,
                text="[12-Oct-2026 10:01:02 UTC] PHP Warning:  Undefined variable $x in /var/www/html/wp-content/plugins/x.php",
                headers={"content-type": "text/plain"},
            )
        if path == "/wp-content/uploads/":
            return httpx.Response(200, text="<html><title>Index of /wp-content/uploads</title>Index of /wp-content/uploads</html>")
        return httpx.Response(404, text="<html>Not found</html>", headers={"content-type": "text/html"})

    return handle


def static_site(body: str = "<html><body>Hello</body></html>", status: int = 200, headers: dict | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("", "/"):
            return httpx.Response(status, text=body, headers={"content-type": "text/html", **(headers or {})})
        return httpx.Response(404, text="not found")

    return handle


class FakeLLM(LLMProvider):
    """Returns canned responses; records every call."""

    available = True
    name = "fake:test-model"

    def __init__(self, json_responses: list[Any] | None = None, text_response: str = "Fake summary.", fail: bool = False):
        self.json_responses = list(json_responses or [])
        self.text_response = text_response
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def json(self, *, purpose, system, prompt, schema, max_tokens=16000):
        self.calls.append({"purpose": purpose, "system": system, "prompt": prompt, "schema": schema})
        if self.fail:
            raise LLMError("simulated outage")
        return self.json_responses.pop(0)

    async def text(self, *, purpose, system, prompt, max_tokens=16000):
        self.calls.append({"purpose": purpose, "system": system, "prompt": prompt})
        if self.fail:
            raise LLMError("simulated outage")
        return self.text_response


class FakeN8n:
    """Stands in for n8n's events webhook and health endpoints."""

    def __init__(self, fail: bool = False) -> None:
        self.received: list[dict[str, Any]] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.startswith("/webhook/"):
            if self.fail:
                return httpx.Response(500, json={"message": "workflow error"})
            self.received.append({"headers": dict(request.headers), "body": json.loads(request.content)})
            return httpx.Response(200, json={"message": "Workflow was started"})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
