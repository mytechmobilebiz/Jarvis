"""The SearXNG provider against a fake SearXNG (httpx.MockTransport): the request it sends, and every answer."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.research.providers import MockProvider, SearchRequest
from app.research.providers.searxng import BASE_URL, MAX_BYTES, SearXNGProvider

from ..research_fakes import forbid_real_http

ALL = ("duckduckgo", "brave", "wikipedia")


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    forbid_real_http(monkeypatch)


def answer(results=None, infoboxes=None, unresponsive=None, **extra) -> dict:
    return {"query": "q", "results": results or [], "answers": [], "corrections": [], "infoboxes": infoboxes or [],
            "suggestions": [], "unresponsive_engines": unresponsive or [], **extra}


ONE = [{"url": "https://example.com/a", "title": "A", "content": "x", "engine": "brave", "engines": ["brave"],
        "score": 1.0, "publishedDate": None}]


class FakeSearXNG:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def provider(self, **kwargs) -> SearXNGProvider:
        kwargs.setdefault("retry_delay", 0.01)
        return SearXNGProvider(transport=httpx.MockTransport(self.handler), **kwargs)

    def form(self, n: int = 0) -> dict[str, list[str]]:
        return parse_qs(self.requests[n].content.decode(), keep_blank_values=True)


def ok(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def request(engines=ALL, **kwargs) -> SearchRequest:
    return SearchRequest(query="WordPress 6.9 release notes", engines=tuple(engines), **kwargs)


async def test_the_request_is_a_post_with_exactly_the_listed_form_fields():
    fake = FakeSearXNG(ok(answer(ONE)))
    result = await fake.provider().search(request(engines=("duckduckgo", "wikipedia"), time_range="month", page=2))
    assert result.status == "ok" and result.sent
    sent = fake.requests[0]
    assert sent.method == "POST" and str(sent.url) == BASE_URL + "/search" and sent.url.query == b""
    form = fake.form()
    assert set(form) == {"q", "format", "engines", "language", "time_range", "pageno"}
    assert form["q"] == ["WordPress 6.9 release notes"] and form["format"] == ["json"]
    assert form["engines"] == ["duckduckgo,wikipedia"]  # brave is cooling down: left out, and no categories
    assert form["language"] == ["en-US"] and form["time_range"] == ["month"] and form["pageno"] == ["2"]
    assert sent.headers["x-real-ip"] == "127.0.0.1"
    assert not any(k.startswith("category") or k in ("safesearch", "timeout_limit") for k in form)


async def test_the_address_is_fixed_and_the_environment_is_not_read(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    fake = FakeSearXNG(ok(answer(ONE)))
    provider = fake.provider()
    assert provider._client.trust_env is False and provider._client.follow_redirects is False
    await provider.search(request())
    assert fake.requests[0].url.host == "searxng" and fake.requests[0].url.port == 8080


@pytest.mark.parametrize("data, status, reason", [
    (answer(ONE), "ok", ""),
    (answer(ONE, unresponsive=[["brave", "timeout"]]), "partial", "engines_unresponsive"),
    (answer(infoboxes=[{"infobox": "W", "id": "https://en.wikipedia.org/wiki/W"}]), "ok", ""),
    (answer(unresponsive=[["brave", "timeout"]]), "empty", ""),
    (answer(), "empty", ""),
    (answer(unresponsive=[["brave", "HTTP connection error"], ["duckduckgo", "Suspended: CAPTCHA"],
                          ["wikipedia", "Suspended: timeout"]]), "provider_down", "all_engines_failing"),
])
async def test_200_answers(data, status, reason):
    result = await FakeSearXNG(ok(data)).provider().search(request())
    assert (result.status, result.reason, result.http_status) == (status, reason, 200)


async def test_unresponsive_engines_become_codes():
    data = answer(unresponsive=[["brave", "Suspended: too many requests"], ["duckduckgo", "CAPTCHA"]])
    result = await FakeSearXNG(ok(data)).provider().search(request(engines=("brave", "duckduckgo")))
    assert result.unresponsive == [{"engine": "brave", "reason": "too_many_requests", "suspended": True},
                                   {"engine": "duckduckgo", "reason": "captcha", "suspended": False}]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_bad_request_and_never_followed(status):
    fake = FakeSearXNG(httpx.Response(status, headers={"location": "https://evil.example/x"}))
    result = await fake.provider().search(request())
    assert (result.status, result.reason, result.attempts) == ("bad_request", "searxng_redirect", 1)
    assert len(fake.requests) == 1  # nothing followed, nothing retried


@pytest.mark.parametrize("status, expected, reason", [
    (403, "provider_misconfigured", "searxng_forbidden"),
    (400, "bad_request", "searxng_bad_request"),
    (404, "failed", "searxng_status"),
    (502, "failed", "searxng_status"),
])
async def test_error_answers_are_not_retried(status, expected, reason):
    fake = FakeSearXNG(httpx.Response(status, json={"error": "x"}))
    result = await fake.provider().search(request())
    assert (result.status, result.reason) == (expected, reason) and len(fake.requests) == 1


async def test_a_500_is_retried_once():
    fake = FakeSearXNG(httpx.Response(500, json={"error": "search error"}), ok(answer(ONE)))
    result = await fake.provider().search(request())
    assert result.status == "ok" and result.attempts == 2 and len(fake.requests) == 2

    fake = FakeSearXNG(httpx.Response(500, json={"error": "search error"}))
    result = await fake.provider().search(request())
    assert (result.status, result.reason, result.attempts) == ("failed", "searxng_error", 2)
    assert len(fake.requests) == 2


async def test_a_connection_error_is_retried_once_then_unreachable():
    fake = FakeSearXNG(httpx.ConnectError("refused"))
    result = await fake.provider().search(request())
    assert (result.status, result.reason, result.attempts, result.sent) == ("unreachable", "connect_error", 2, False)
    fake = FakeSearXNG(httpx.ConnectError("refused"), ok(answer(ONE)))
    assert (await fake.provider().search(request())).status == "ok"


async def test_the_retry_waits_a_second(monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    fake = FakeSearXNG(httpx.ConnectError("refused"), ok(answer(ONE)))
    await SearXNGProvider(transport=httpx.MockTransport(fake.handler)).search(request())
    assert slept == [1.0]


async def test_a_slow_answer_hits_the_overall_deadline():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ok(answer(ONE))

    provider = SearXNGProvider(transport=httpx.MockTransport(slow), search_seconds=0.2, retry_delay=0.01)
    result = await provider.search(request())
    assert (result.status, result.reason, result.attempts) == ("unreachable", "timeout", 1)


async def test_a_slow_trickle_hits_the_overall_deadline():
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b" "

    fake = FakeSearXNG(httpx.Response(200, stream=Trickle()))
    result = await fake.provider(search_seconds=0.3).search(request())
    assert (result.status, result.reason, result.attempts) == ("unreachable", "timeout", 1)
    assert result.latency_ms < 1000


async def test_an_answer_over_2_mb_is_refused():
    big = json.dumps(answer([{**ONE[0], "content": "x" * (MAX_BYTES + 10)}])).encode()
    result = await FakeSearXNG(httpx.Response(200, content=big)).provider().search(request())
    assert (result.status, result.reason) == ("failed", "oversized") and result.data == {}
    assert result.sent  # SearXNG ran the search: it counts against the caps


async def test_an_oversized_stream_without_length_is_cut_off():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                yield b"x" * (MAX_BYTES // 2)

    fake = FakeSearXNG(httpx.Response(200, stream=Stream()))
    result = await fake.provider().search(request())
    assert (result.reason, result.sent) == ("oversized", True)


@pytest.mark.parametrize("body", [b"<html>not json</html>", b"\xff\xfe", b"[]", b'{"results": "x"}',
                                  b'{"results": [], "infoboxes": {}}',
                                  b'{"results": [], "unresponsive_engines": [["brave"]]}',
                                  b'{"results": [], "unresponsive_engines": [["Brave!!", "timeout"]]}'])
async def test_malformed_answers_fail_without_retry(body):
    fake = FakeSearXNG(httpx.Response(200, content=body))
    result = await fake.provider().search(request())
    assert (result.status, result.reason) == ("failed", "malformed_json") and len(fake.requests) == 1


async def test_health_reads_healthz_and_config():
    config = {"version": "2026.10.4+d48c4b555", "engines": [{"name": "brave", "enabled": True}]}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, text="OK")
        return ok(config)

    fake = FakeSearXNG(handle)
    health = await fake.provider().health(with_config=True)
    assert health.ok and health.config == config
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/healthz"), ("GET", "/config")]
    assert not (await FakeSearXNG(httpx.Response(503)).provider().health()).ok
    assert (await FakeSearXNG(httpx.ConnectError("x")).provider().health()).reason == "unreachable"


async def test_fetch_is_not_supported():
    fake = FakeSearXNG(ok(answer()))
    result = await fake.provider().fetch("https://example.com/")
    assert (result.status, result.reason) == ("refused_policy", "not_supported") and fake.requests == []


async def test_the_mock_provider_keeps_nothing_of_what_it_was_asked():
    # The worker holds one provider for its whole life: a query must not stay in its memory.
    mock = MockProvider()
    assert (await mock.search(request())).status == "ok"
    assert vars(mock) == {}
