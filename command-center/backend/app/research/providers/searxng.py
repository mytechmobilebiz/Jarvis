"""The SearXNG provider (plan sections 3.2, 3.8 and 3.11): a narrow client of the private container, nothing more.

- One fixed address, http://searxng:8080, reachable only on the worker's "research" network. It is not a setting:
  queries can never be sent to a public SearXNG.
- Three requests only: POST /search, GET /healthz, GET /config. No redirect is followed, no proxy or other setting is
  read from the environment (trust_env=False), and no answer is read beyond 2 MB.
- POST, so the query stays out of every URL (httpx logs URLs). The form holds exactly q, format, engines, language,
  time_range and pageno: never "categories" or "category_*" (SearXNG would add that category's engines, a cooling one
  included), never "safesearch" (locked in settings.yml), "timeout_limit" or any other preference.
- X-Real-IP: 127.0.0.1 only silences SearXNG's bot-detection warning (its limiter is off).
- 10 seconds overall per search; one retry, after a second, on a connection error or a 500. Nothing else is retried.

Answers (the gateway's status):
  200 with results              ok, or partial when an engine asked did not answer
  200, no results               empty, or provider_down when every engine asked was unresponsive or suspended
  any 3xx                       bad_request (searxng_redirect)
  403                           provider_misconfigured (JSON switched off)
  400                           bad_request
  500                           retried once, then failed (searxng_error)
  no connection / no answer     unreachable (connect_error / timeout)
  over 2 MB / not the JSON shape  failed (oversized / malformed_json)
Only results, infoboxes and unresponsive_engines are read; number_of_results is absent at the pinned version.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from ...config import SEARXNG_URL
from .. import health
from .base import NOT_SUPPORTED, ProviderFetchResult, ProviderHealth, ProviderSearchResult, SearchRequest

BASE_URL = SEARXNG_URL
MAX_BYTES = 2 * 1024 * 1024
SEARCH_SECONDS = 10.0
PROBE_SECONDS = 5.0
RETRY_DELAY = 1.0
HEADERS = {"X-Real-IP": "127.0.0.1", "Accept": "application/json"}
_ENGINE = re.compile(r"[a-z0-9 _.\-]{1,40}")


class Oversized(Exception):
    pass


class Malformed(Exception):
    pass


def _unresponsive(value: object) -> list[dict[str, Any]]:
    """SearXNG's [[engine, message], ...] as codes: {engine, reason, suspended}. Raises Malformed."""
    if not isinstance(value, list):
        raise Malformed("unresponsive_engines")
    out = []
    for item in value:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(x, str) for x in item)):
            raise Malformed("unresponsive_engines")
        engine = item[0].strip().lower()
        if not _ENGINE.fullmatch(engine):
            raise Malformed("unresponsive_engines")
        reason, suspended = health.classify(item[1])
        out.append({"engine": engine, "reason": reason, "suspended": suspended})
    return out


def parse_answer(body: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the JSON answer, its unresponsive engines as codes). Raises Malformed for anything but SearXNG's shape."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Malformed("json") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise Malformed("results")
    infoboxes = data.get("infoboxes", [])
    if not isinstance(infoboxes, list):
        raise Malformed("infoboxes")
    return {"results": data["results"], "infoboxes": infoboxes}, _unresponsive(data.get("unresponsive_engines", []))


def status_of(data: dict[str, Any], unresponsive: list[dict[str, Any]], asked: tuple[str, ...]) -> tuple[str, str]:
    failed = {u["engine"] for u in unresponsive}
    if data["results"] or data["infoboxes"]:
        return ("partial", "engines_unresponsive") if failed & set(asked) else ("ok", "")
    if asked and set(asked) <= failed:
        return "provider_down", "all_engines_failing"
    return "empty", ""


class SearXNGProvider:
    name = "searxng"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, search_seconds: float = SEARCH_SECONDS,
                 retry_delay: float = RETRY_DELAY) -> None:
        self._client = httpx.AsyncClient(base_url=BASE_URL, transport=transport, follow_redirects=False,
                                         trust_env=False, timeout=httpx.Timeout(search_seconds), headers=HEADERS)
        self.search_seconds = search_seconds
        self.retry_delay = retry_delay

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _read(self, method: str, path: str, **kwargs: Any) -> tuple[int, bytes]:
        """(status, body) with the body capped; a redirect's body is not read."""
        async with self._client.stream(method, path, **kwargs) as response:
            if response.is_redirect or 300 <= response.status_code < 400:
                return response.status_code, b""
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise Oversized()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise Oversized()
            return response.status_code, bytes(body)

    async def search(self, request: SearchRequest) -> ProviderSearchResult:
        form = {"q": request.query, "format": "json", "engines": ",".join(request.engines),
                "language": request.language, "time_range": request.time_range or "", "pageno": str(request.page)}
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.search_seconds
        result = ProviderSearchResult(status="failed")
        while True:
            result.attempts += 1
            retry = False
            try:
                async with asyncio.timeout_at(deadline):
                    status, body = await self._read("POST", "/search", data=form)
                result.sent, result.http_status = True, status
                if 300 <= status < 400:
                    result.status, result.reason = "bad_request", "searxng_redirect"
                elif status == 403:
                    result.status, result.reason = "provider_misconfigured", "searxng_forbidden"
                elif status == 400:
                    result.status, result.reason = "bad_request", "searxng_bad_request"
                elif status == 500:
                    result.status, result.reason = "failed", "searxng_error"
                    retry = True
                elif status != 200:
                    result.status, result.reason = "failed", "searxng_status"
                else:
                    data, unresponsive = parse_answer(body)
                    result.data, result.unresponsive = data, unresponsive
                    result.status, result.reason = status_of(data, unresponsive, request.engines)
            except TimeoutError:
                result.sent = True  # the request may have reached SearXNG and the engines
                result.status, result.reason = "unreachable", "timeout"
            except Oversized:
                result.sent = True  # the headers arrived: SearXNG and the engines ran the search
                result.status, result.reason = "failed", "oversized"
            except Malformed:
                result.status, result.reason = "failed", "malformed_json"
            except (httpx.ConnectError, httpx.ConnectTimeout):
                result.status, result.reason = "unreachable", "connect_error"
                retry = True
            except httpx.TimeoutException:
                result.sent = True
                result.status, result.reason = "unreachable", "timeout"
            except httpx.HTTPError:
                result.sent = True
                result.status, result.reason = "unreachable", "connection_lost"
            if not retry or result.attempts >= 2 or deadline - loop.time() <= self.retry_delay:
                break
            await asyncio.sleep(self.retry_delay)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    async def fetch(self, url: str) -> ProviderFetchResult:
        return NOT_SUPPORTED

    async def health(self, *, with_config: bool = False) -> ProviderHealth:
        """GET /healthz, and GET /config when asked (its version and engines)."""
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                status, _ = await self._read("GET", "/healthz")
                if status != 200:
                    return ProviderHealth(ok=False, reason=f"healthz_{status}")
                if not with_config:
                    return ProviderHealth(ok=True)
                status, body = await self._read("GET", "/config")
        except TimeoutError:
            return ProviderHealth(ok=False, reason="timeout")
        except Oversized:
            return ProviderHealth(ok=False, reason="oversized")
        except httpx.HTTPError:
            return ProviderHealth(ok=False, reason="unreachable")
        try:
            config = json.loads(body.decode("utf-8")) if status == 200 else None
        except (UnicodeDecodeError, ValueError):
            config = None
        if not isinstance(config, dict):
            return ProviderHealth(ok=True, reason="config_unreadable")
        return ProviderHealth(ok=True, config=config)
