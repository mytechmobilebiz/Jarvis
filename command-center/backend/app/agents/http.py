"""The only HTTP client agents get: read-only and barred from internal addresses.

Two guarantees, both enforced on every request including each redirect hop:

* only GET, HEAD and OPTIONS leave the process; anything else raises
  WriteAttemptBlocked before a byte is sent;
* a URL whose host resolves to a loopback, private, link-local or otherwise
  non-public address is refused (BlockedTarget), unless the host is listed in
  MONITOR_ALLOWED_PRIVATE_HOSTS. Monitoring a client site can therefore never
  be turned into a request against PostgreSQL, n8n or the cloud metadata
  endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field

import httpx

from ..config import Settings

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class WriteAttemptBlocked(RuntimeError):
    pass


class BlockedTarget(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: httpx.Headers
    text: str
    elapsed_ms: int
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    def json(self) -> object:
        import json

        return json.loads(self.text)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_public_host(host: str | None, allowed_private: set[str], resolver=None) -> None:
    """Refuse hosts that are, or resolve to, loopback, private, link-local or other non-public addresses.

    Hosts listed in allowed_private are let through (e.g. a local test server).
    Shared by the read-only HTTP client and the SSH client.
    """
    if not host:
        raise BlockedTarget("no host")
    host = host.lower().strip("[]")
    if host in allowed_private:
        return
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(literal):
            raise BlockedTarget(f"{host} is not a public address")
        return
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
        raise BlockedTarget(f"{host} is an internal hostname")
    try:
        addresses = await (resolver or default_resolver)(host)
    except OSError:
        # Unresolvable here: the connection itself will fail (or an
        # egress proxy resolves it outside our network), so let it through.
        return
    for addr in addresses:
        if not _is_public(ipaddress.ip_address(addr.split("%")[0])):
            raise BlockedTarget(f"{host} resolves to non-public address {addr}")


class ReadOnlyHttpClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver=None,
    ) -> None:
        self.settings = settings
        self._allowed_private = settings.allowed_private_hosts
        self._resolver = resolver or self._default_resolver
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.monitor_timeout_seconds),
            headers={"User-Agent": settings.monitor_user_agent, "Accept": "text/html,application/json,*/*"},
            follow_redirects=True,
            max_redirects=5,
            transport=transport,
            event_hooks={"request": [self._guard]},
        )

    async def __aenter__(self) -> ReadOnlyHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    _default_resolver = staticmethod(default_resolver)

    async def check_target(self, host: str | None) -> None:
        if not host:
            raise BlockedTarget("URL has no host")
        await check_public_host(host, self._allowed_private, self._resolver)

    async def _guard(self, request: httpx.Request) -> None:
        if request.method.upper() not in READ_ONLY_METHODS:
            raise WriteAttemptBlocked(f"{request.method} {request.url} refused: agents are read-only")
        await self.check_target(request.url.host)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if method.upper() not in READ_ONLY_METHODS:
            raise WriteAttemptBlocked(f"{method} {url} refused: agents are read-only")
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    async def fetch(
        self, url: str, *, max_bytes: int | None = None, headers: dict[str, str] | None = None, follow_redirects: bool = True
    ) -> FetchResult:
        """GET a URL, keeping at most max_bytes of the body."""
        limit = max_bytes or self.settings.monitor_max_body_bytes
        started = time.perf_counter()
        async with self._client.stream("GET", url, headers=headers, follow_redirects=follow_redirects) as response:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= limit:
                    truncated = True
                    break
            body = b"".join(chunks)[:limit]
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            encoding = response.encoding or "utf-8"
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status=response.status_code,
                headers=response.headers,
                text=body.decode(encoding, errors="replace"),
                elapsed_ms=elapsed_ms,
                truncated=truncated,
                redirects=[str(r.url) for r in response.history],
            )
