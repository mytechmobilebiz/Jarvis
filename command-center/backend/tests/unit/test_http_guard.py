"""The agents' HTTP client must be read-only and refuse internal targets."""

from __future__ import annotations

import httpx
import pytest

from app.agents.http import BlockedTarget, ReadOnlyHttpClient, WriteAttemptBlocked
from app.config import Settings


def client(transport, **overrides) -> ReadOnlyHttpClient:
    return ReadOnlyHttpClient(Settings(**overrides), transport=transport)


def ok_transport(seen: list):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
async def test_write_methods_are_refused_before_sending(method):
    seen: list = []
    async with client(ok_transport(seen)) as http:
        with pytest.raises(WriteAttemptBlocked):
            await http.request(method, "https://example.test/")
    assert seen == []


async def test_write_through_underlying_client_is_still_refused():
    seen: list = []
    async with client(ok_transport(seen)) as http:
        with pytest.raises(WriteAttemptBlocked):
            await http._client.post("https://example.test/wp-admin/admin-ajax.php", data={"x": 1})
    assert seen == []


async def test_get_and_head_are_allowed():
    seen: list = []
    async with client(ok_transport(seen)) as http:
        assert (await http.get("https://example.test/")).status_code == 200
        assert (await http.head("https://example.test/")).status_code == 200
    assert [r.method for r in seen] == ["GET", "HEAD"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:5432/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://host.docker.internal/",
    ],
)
async def test_private_targets_are_blocked(url):
    seen: list = []
    async with client(ok_transport(seen)) as http:
        with pytest.raises(BlockedTarget):
            await http.get(url)
    assert seen == []


async def test_hostname_resolving_privately_is_blocked():
    seen: list = []

    async def resolver(host):
        return ["10.1.2.3"]

    http = ReadOnlyHttpClient(Settings(), transport=ok_transport(seen), resolver=resolver)
    with pytest.raises(BlockedTarget):
        await http.get("https://sneaky.example.test/")
    await http.aclose()
    assert seen == []


async def test_redirect_to_private_address_is_blocked():
    seen: list = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "example.test":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/api/admin"})
        return httpx.Response(200, text="internal!")

    async with client(httpx.MockTransport(handle)) as http:
        with pytest.raises(BlockedTarget):
            await http.get("https://example.test/")
    assert seen == ["example.test"]


async def test_allowlisted_private_host_is_permitted():
    seen: list = []
    async with client(ok_transport(seen), monitor_allowed_private_hosts="wp-test, localhost") as http:
        assert (await http.get("http://wp-test/")).status_code == 200
        assert (await http.get("http://localhost:8081/")).status_code == 200


async def test_fetch_caps_body_size():
    def handle(request):
        return httpx.Response(200, content=b"x" * 50_000)

    async with client(httpx.MockTransport(handle)) as http:
        result = await http.fetch("https://example.test/", max_bytes=1000)
    assert len(result.text) == 1000
    assert result.truncated
