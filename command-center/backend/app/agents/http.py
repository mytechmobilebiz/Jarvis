"""The only HTTP client agents get: read-only and barred from internal addresses.

Two guarantees, both enforced on every request including each redirect hop:

* only GET, HEAD and OPTIONS leave the process; anything else raises
  WriteAttemptBlocked before a byte is sent;
* a URL whose host resolves to a loopback, private, link-local, shared
  (100.64/10) or otherwise non-global address is refused (BlockedTarget),
  unless the host is listed in MONITOR_ALLOWED_PRIVATE_HOSTS. Monitoring a
  client site can therefore never be turned into a request against
  PostgreSQL, n8n or the cloud metadata endpoint.

The name is resolved once per connection and the connection is made to an
address that passed the check (PublicOnlyBackend), so a DNS answer that
changes between the check and the connection (rebinding) cannot slip through.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field

import httpcore
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


NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local:
            return False
        # An IPv4 address carried inside IPv6 is judged as that IPv4 address.
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour:
            ip = ip.sixtofour
        elif ip.teredo:
            ip = ip.teredo[1]
        elif ip in NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def resolve_public(host: str | None, allowed_private: set[str], resolver=None) -> list[str]:
    """The addresses to connect to for host, all of them public.

    Raises BlockedTarget for internal names and for names with any non-public
    address (unless listed in allowed_private, e.g. a local test server), and
    OSError when the name does not resolve.
    """
    if not host:
        raise BlockedTarget("no host")
    host = host.lower().strip("[]")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if host not in allowed_private and not _is_public(literal):
            raise BlockedTarget(f"{host} is not a public address")
        return [host]
    if host not in allowed_private and (host == "localhost" or host.endswith((".localhost", ".internal", ".local"))):
        raise BlockedTarget(f"{host} is an internal hostname")
    addresses = await (resolver or default_resolver)(host)
    if not addresses:
        raise socket.gaierror(socket.EAI_NONAME, f"{host} has no addresses")
    if host not in allowed_private:
        for addr in addresses:
            if not _is_public(ipaddress.ip_address(addr.split("%")[0])):
                raise BlockedTarget(f"{host} resolves to non-public address {addr}")
    return addresses


async def check_public_host(host: str | None, allowed_private: set[str], resolver=None) -> None:
    """Early refusal of internal and non-public hosts (see resolve_public).

    A name that does not resolve is let through here: the connection itself
    resolves it again through resolve_public and fails closed.
    """
    try:
        await resolve_public(host, allowed_private, resolver)
    except OSError:
        return


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects only to addresses resolve_public accepted."""

    def __init__(self, allowed_private: set[str], resolver=None) -> None:
        self._allowed_private = allowed_private
        self._resolver = resolver
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await resolve_public(host, self._allowed_private, self._resolver)
        except OSError as exc:
            raise httpcore.ConnectError(f"getaddrinfo failed for {host}: {exc}") from exc
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        assert error is not None
        raise error

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None) -> httpcore.AsyncNetworkStream:
        raise BlockedTarget("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_only_transport(allowed_private: set[str], resolver=None) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(  # same pool httpx builds, with the checking backend
        ssl_context=httpx.create_ssl_context(trust_env=False), network_backend=PublicOnlyBackend(allowed_private, resolver),
    )
    return transport


async def open_public_connection(host: str, port: int, allowed_private: set[str], **kwargs) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """asyncio.open_connection to host, connecting only to a checked address (kwargs as for open_connection)."""
    error: OSError | None = None
    for address in await resolve_public(host, allowed_private):
        try:
            return await asyncio.open_connection(address, port, **kwargs)
        except OSError as exc:
            error = exc
    assert error is not None
    raise error


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
            transport=transport or public_only_transport(self._allowed_private, resolver),
            trust_env=False,  # an environment proxy would bypass the address checks
            event_hooks={"request": [self._guard]},
        )

    async def __aenter__(self) -> ReadOnlyHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    _default_resolver = staticmethod(default_resolver)

    @property
    def allowed_private(self) -> set[str]:
        return set(self._allowed_private)

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
        """GET a URL, keeping at most max_bytes of the body.

        The whole exchange must finish within a few timeouts: the per-read timeout
        alone lets a server that trickles a byte at a time hold a check for hours.
        """
        limit = max_bytes or self.settings.monitor_max_body_bytes
        deadline = self.settings.monitor_timeout_seconds * 3
        started = time.perf_counter()
        try:
            async with (
                asyncio.timeout(deadline),
                self._client.stream("GET", url, headers=headers, follow_redirects=follow_redirects) as response,
            ):
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
        except TimeoutError:
            raise httpx.ReadTimeout(f"no complete response within {deadline:g} seconds") from None
