"""The agents' HTTP client must be read-only and refuse internal targets."""

from __future__ import annotations

import asyncio
import ssl
import time
from datetime import UTC, datetime, timedelta

import httpcore
import httpx
import pytest
import trustme

from app.agents import http as http_module
from app.agents.http import BlockedTarget, PublicOnlyBackend, ReadOnlyHttpClient, WriteAttemptBlocked
from app.agents.monitoring.checks import describe_fetch_error, probe_tls
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


async def test_fetch_gives_up_on_a_body_that_trickles_in():
    async def trickle():
        for _ in range(100):
            await asyncio.sleep(0.05)  # each read is quick, so the read timeout alone never fires
            yield b"x"

    async with client(httpx.MockTransport(lambda r: httpx.Response(200, content=trickle())), monitor_timeout_seconds=0.1) as http:
        started = time.perf_counter()
        with pytest.raises(httpx.TimeoutException) as raised:
            await http.fetch("https://example.test/")
    assert time.perf_counter() - started < 1.5
    assert describe_fetch_error(raised.value, 0.1).startswith("no response within")


async def test_a_location_with_tabs_is_read_the_way_a_browser_reads_it():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "acme.test":
            return httpx.Response(302, headers={"location": "https://ac\tme.test/n\new"})
        return httpx.Response(200, text="ok")

    async def resolver(host):
        return ["93.184.215.14"]

    http = ReadOnlyHttpClient(Settings(), transport=httpx.MockTransport(handler), resolver=resolver)
    result = await http.fetch("https://acme.test/", follow_redirects=False)
    assert result.status == 302 and result.headers["location"] == "https://acme.test/new"
    await http.aclose()


# ----------------------------------------------------------------- several addresses


class RecordingBackend:
    """Stands in for httpcore's AnyIO backend and records each attempt: `dead` addresses wait out their timeout, `refused`
    ones fail at once, and the `slow` one connects only after an address has been refused (so only if it is still trying)."""

    def __init__(self, dead: set[str], refused: set[str] = frozenset(), slow: str = "") -> None:
        self.dead, self.refused, self.slow, self.calls = dead, refused, slow, []
        self.a_refusal = asyncio.Event()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, timeout))
        if host in self.refused:
            self.a_refusal.set()
            raise httpcore.ConnectError("All connection attempts failed")
        try:
            async with asyncio.timeout(timeout):
                if host in self.dead:
                    await asyncio.Event().wait()
                if host == self.slow:
                    await self.a_refusal.wait()
        except TimeoutError:
            raise httpcore.ConnectTimeout(f"{host} did not answer") from None
        return f"stream to {host}"


V6, V4 = "2606:2800:220:1:248:1893:25c8:1946", "93.184.215.14"


def backend_for(addresses: list[str], inner: RecordingBackend) -> PublicOnlyBackend:
    async def resolver(host):
        return list(addresses)

    backend = PublicOnlyBackend(set(), resolver)
    backend._inner = inner
    return backend


async def test_a_dead_first_address_costs_only_a_moment():
    backend = backend_for([V6, V4], RecordingBackend({V6}))
    started = time.perf_counter()
    assert await backend.connect_tcp("example.test", 443, timeout=20) == f"stream to {V4}"
    assert time.perf_counter() - started < 1  # the next address is tried after 0.25 s (RFC 8305), not after 20 s
    assert [host for host, _ in backend._inner.calls] == [V6, V4] and all(19 < t <= 20 for _, t in backend._inner.calls)


async def test_a_slow_first_address_keeps_trying_while_the_next_one_fails_at_once():
    # An IPv4-only worker (the Docker default) and a dual-stack site slow to accept: IPv4 sorts first, IPv6 fails at once.
    backend = backend_for([V4, V6], RecordingBackend(set(), refused={V6}, slow=V4))
    assert await backend.connect_tcp("example.test", 443, timeout=20) == f"stream to {V4}"


async def test_when_no_address_connects_in_time_the_timeout_is_raised():
    backend = backend_for([V4, V6], RecordingBackend({V4}, refused={V6}))
    started = time.perf_counter()
    with pytest.raises(httpcore.ConnectTimeout):  # "no response in time", not the IPv6 address's "cannot be reached"
        await backend.connect_tcp("example.test", 443, timeout=3.5)
    assert time.perf_counter() - started > 3.4  # IPv4 had all of it


async def test_the_first_connection_wins_and_one_made_as_well_is_closed():
    both, closed = asyncio.Event(), []

    async def connect(address, seconds):
        if address == "b":
            both.set()  # "a" connects at the same moment
        else:
            await both.wait()
        return address

    async def close(connection):
        closed.append(connection)

    winner, _ = await http_module.first_connection(["a", "b"], 5, connect, close)
    assert sorted([winner, *closed]) == ["a", "b"]


async def test_all_addresses_share_one_connect_deadline():
    backend = backend_for([V6, V4, "93.184.215.15"], RecordingBackend({V6, V4, "93.184.215.15"}))
    started = time.perf_counter()
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("example.test", 443, timeout=0.3)
    assert time.perf_counter() - started < 0.6  # not 0.3 s per address


async def test_tls_certificate_errors_are_not_retried_on_the_next_address(monkeypatch):
    ca = trustme.CA(organization_name="Test CA")
    expired = ca.issue_cert("site.test", not_before=datetime.now(UTC) - timedelta(days=100),
                            not_after=datetime.now(UTC) - timedelta(days=2))
    valid = ca.issue_cert("site.test", not_after=datetime.now(UTC) + timedelta(days=40, hours=1))
    client_ctx = ssl.create_default_context()
    ca.configure_trust(client_ctx)
    for cert, addresses, expected in ((expired, ["127.0.0.1", "127.0.0.2"], "certificate has expired"),
                                      (valid, ["127.0.0.2", "127.0.0.1"], "")):  # a refused TCP connect still moves on
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        cert.configure_cert(server_ctx)

        async def handle(reader, writer):
            try:
                await reader.read(10)
            except (ConnectionError, ssl.SSLError):
                pass
            writer.close()

        async def resolver(host, addresses=addresses):
            return list(addresses)

        monkeypatch.setattr(http_module, "default_resolver", resolver)
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
        async with server:
            info = await probe_tls("site.test", server.sockets[0].getsockname()[1], 5, client_ctx, {"site.test"})
        assert info.error == expected
        assert expected or info.days_left == 40
