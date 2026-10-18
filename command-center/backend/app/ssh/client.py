"""The SSH client: one pinned host key, one key, public-key auth only, cc-gate requests only.

Nothing here can open a shell, forward ports or agents, read ~/.ssh/config or
accept a host key that was not pinned by a person.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from ..agents.http import BlockedTarget, NoConnection, first_connection, resolve_public
from ..config import Settings


class SSHError(RuntimeError):
    """Could not get an answer from the gate."""


class HostKeyNotTrusted(SSHError):
    pass


class HostKeyMismatch(SSHError):
    pass


class AuthFailed(SSHError):
    pass


class Unreachable(SSHError):
    pass


class OutcomeUnknown(SSHError):
    """The request reached the server, but no answer came back: it may have run, fully or partly."""


class GateTimeout(OutcomeUnknown):
    """No answer in time; the operation may still be running on the server."""


# The gate caps each program's output at 1 MB, which JSON escaping can roughly triple; an honest answer is far
# smaller. Parsed JSON takes up to ~50 times its size in memory, so only an answer line of up to half this is parsed.
MAX_STDOUT = 4 * 1024 * 1024
MAX_ANSWER = MAX_STDOUT // 2
MAX_STDERR = 1024 * 1024
MAX_DEPTH = 64  # nesting levels in an answer; a gate answer has fewer than ten

# PostgreSQL text and JSONB refuse NUL and lone surrogates (json.loads joins valid surrogate pairs, so any left are lone).
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


def strip_nul(value: Any, _depth: int = 0) -> Any:
    """Replace the characters PostgreSQL refuses in a decoded answer; ValueError if it is nested more than MAX_DEPTH deep."""
    if isinstance(value, str):
        return _UNSTORABLE.sub("\ufffd", value)
    if isinstance(value, (list, dict)) and _depth >= MAX_DEPTH:
        raise ValueError("answer nested too deeply")
    if isinstance(value, list):
        return [strip_nul(v, _depth + 1) for v in value]
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v, _depth + 1) for k, v in value.items()}
    return value


def _text(exc: BaseException | None) -> str:
    """An exception's message, safe to store: it may carry text the server sent."""
    return strip_nul(str(exc))


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e999: PostgreSQL JSON has no Infinity
        raise ValueError("number out of range")
    return value


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")  # NaN and Infinity, which json.loads would accept and PostgreSQL refuses


@dataclass
class GateAnswer:
    ok: bool
    refused: bool
    exit_status: int | None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    output_sha256: str = ""
    duration_ms: int = 0

    @property
    def result(self) -> Any:
        return self.payload.get("result")


def _pattern(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _options(settings: Settings, connect_timeout: float) -> dict[str, Any]:
    return {
        "config": [],  # never read ~/.ssh/config or /etc/ssh/ssh_config
        "agent_path": None,
        "agent_forwarding": False,
        "x11_forwarding": False,
        "preferred_auth": ["publickey"],
        "password_auth": False,
        "kbdint_auth": False,
        "gss_auth": False,
        "host_based_auth": False,
        "request_pty": False,
        "env": {},
        "send_env": [],
        "connect_timeout": connect_timeout,
        "login_timeout": settings.ssh_connect_timeout_seconds,
    }


async def _resolve(host: str, settings: Settings) -> list[str]:
    """The checked addresses to connect to (so a DNS answer cannot change between the check and the connection)."""
    try:
        return await resolve_public(host, settings.ssh_private_hosts)
    except BlockedTarget as exc:
        raise Unreachable(f"refused to connect: {_text(exc)}") from exc
    except OSError as exc:
        raise Unreachable(f"{host} does not resolve: {_text(exc)}") from exc


async def _tcp(address: str, port: int, seconds: float | None) -> socket.socket:
    """A TCP connection to address, as a socket for asyncssh to run over."""
    sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        async with asyncio.timeout(seconds):
            await asyncio.get_running_loop().sock_connect(sock, (address, port))
    except TimeoutError:
        sock.close()
        raise TimeoutError("timed out") from None
    except BaseException:
        sock.close()
        raise
    return sock


async def _first_tcp(addresses: list[str], port: int, seconds: float) -> tuple[str, socket.socket]:
    """The first address to take a TCP connection (see first_connection), and its socket.

    Only the TCP connection is raced: the SSH handshake and login on it then
    have all the time left, so a slow server is not cut short because another
    of its addresses fails at once.
    """
    async def close(sock: socket.socket) -> None:
        sock.close()

    return await first_connection(addresses, seconds, lambda address, left: _tcp(address, port, left), close)


def _left(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.1)  # asyncssh takes 0 for no timeout at all


HOST_KEY_ALGS = ["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256"]
PROBE_SECONDS = 5.0
# The server let the connection go without a word (OpenSSH does that when it has no key of the type offered).
_DROPPED = (asyncssh.ConnectionLost, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


async def scan_host_key(host: str, port: int, settings: Settings) -> tuple[str, str]:
    """The host key a server presents (for a person to compare and pin), as (openssh line, SHA256 fingerprint)."""
    deadline = time.monotonic() + settings.ssh_connect_timeout_seconds
    try:
        address, sock = await _first_tcp(await _resolve(host, settings), port, settings.ssh_connect_timeout_seconds)
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(address, port, sock=sock, server_host_key_algs=HOST_KEY_ALGS, config=[]), timeout=_left(deadline),
        )
    except (NoConnection, OSError, TimeoutError, asyncssh.Error) as exc:
        raise Unreachable(f"could not reach {host}:{port}: {_text(exc) or 'timed out'}") from exc
    if key is None:
        raise Unreachable(f"{host}:{port} did not present a host key")
    algorithm, blob = key.export_public_key("openssh").decode("ascii").split()[:2]
    return f"{algorithm} {blob}", key.get_fingerprint("sha256")


class GateConnection:
    """One SSH connection to a server's cc-gate; several requests may share it."""

    def __init__(self, *, host: str, port: int, username: str, host_key: str, client_key: asyncssh.SSHKey, settings: Settings) -> None:
        if not host_key:
            raise HostKeyNotTrusted("the server's host key has not been pinned yet")
        if username == "root":
            raise AuthFailed("refusing to log in as root")
        self.host, self.port, self.username = host, port, username
        self.host_key, self.client_key, self.settings = host_key, client_key, settings
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> GateConnection:
        deadline = time.monotonic() + self.settings.ssh_connect_timeout_seconds
        addresses = await _resolve(self.host, self.settings)
        known = {a: asyncssh.import_known_hosts(f"{_pattern(a, self.port)} {self.host_key}\n") for a in addresses}
        while True:
            try:
                address, sock = await _first_tcp(addresses, self.port, deadline - time.monotonic())
            except NoConnection as exc:
                raise Unreachable(f"could not connect to {self.host}:{self.port}: {_text(exc)}") from exc
            try:  # asyncssh owns the socket from here on, and closes it if this fails
                self._conn = await asyncssh.connect(
                    address, port=self.port, sock=sock, username=self.username, client_keys=[self.client_key], known_hosts=known[address],
                    **_options(self.settings, _left(deadline)),
                )
                return self
            except asyncssh.HostKeyNotVerifiable as exc:
                raise HostKeyMismatch(f"{self.host} presented a different host key than the pinned one: {_text(exc)}") from exc
            except asyncssh.KeyExchangeFailed as exc:
                if "host key" in str(exc).lower():
                    # Only the pinned key's type is offered; a server without such a key is not the pinned server.
                    raise HostKeyMismatch(f"{self.host} did not offer the pinned host key type ({_text(exc)})") from exc
                await self._refuse_other_key(address)
                raise Unreachable(f"could not connect to {self.host}:{self.port}: {_text(exc)}") from exc
            except asyncssh.PermissionDenied as exc:
                raise AuthFailed(f"{self.username}@{self.host} refused the Command Center's key") from exc
            except _DROPPED as exc:
                await self._refuse_other_key(address)
                addresses = [a for a in addresses if a != address]  # not an impostor: try the other addresses
                if not addresses or time.monotonic() >= deadline:
                    raise Unreachable(f"could not connect to {self.host}:{self.port}: {_text(exc)}") from exc
            except (OSError, TimeoutError, asyncssh.Error) as exc:
                raise Unreachable(f"could not connect to {self.host}:{self.port}: {_text(exc) or 'timed out'}") from exc

    async def _refuse_other_key(self, address: str) -> None:
        """After a handshake that broke off: HostKeyMismatch if the server presents another key once any type may be used.

        Only the pinned key's type is offered when connecting, and an OpenSSH
        server without a key of that type drops the connection without a
        DISCONNECT, which would otherwise look like a network problem.
        """
        pinned = asyncssh.import_public_key(self.host_key)
        algs = list(dict.fromkeys([a.decode("ascii") for a in pinned.sig_algorithms] + HOST_KEY_ALGS))  # the pinned type first
        try:
            key = await asyncio.wait_for(asyncssh.get_server_host_key(address, self.port, server_host_key_algs=algs, config=[]),
                                         timeout=min(PROBE_SECONDS, self.settings.ssh_connect_timeout_seconds))
        except (OSError, TimeoutError, asyncssh.Error):
            return  # no handshake now either: a network problem after all
        if key is not None and key.public_data != pinned.public_data:
            raise HostKeyMismatch(f"{self.host} does not offer the pinned host key; it presents {key.get_fingerprint('sha256')} instead")

    async def __aexit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.close()
            try:
                await self._conn.wait_closed()
            except (OSError, asyncssh.Error):
                pass

    async def request(self, command: str, timeout: float) -> GateAnswer:
        assert self._conn is not None
        started = time.monotonic()
        sent = False
        try:
            async with asyncio.timeout(timeout):
                process = await self._conn.create_process(command, stdin=asyncssh.DEVNULL, encoding=None)
                sent = True  # the server accepted the request: from here on it may run
                try:
                    # Both at once: an unread stderr would stall the channel's flow control.
                    results = await asyncio.gather(_read(process, process.stdout, MAX_STDOUT), _read(process, process.stderr, MAX_STDERR),
                                                   return_exceptions=True)
                    for r in results:
                        if isinstance(r, BaseException):
                            raise r
                    out_bytes, err_bytes = results
                    await process.wait_closed()
                finally:
                    process.close()
        except TimeoutError as exc:
            raise GateTimeout(f"no answer within {timeout:g} seconds; the operation may still be running") from exc
        except _TooLarge as exc:
            # An honest gate never answers this much. Only this request's channel is closed (by _read), so a site that
            # makes its own answer huge spoils that answer alone, not the requests after it.
            return GateAnswer(ok=False, refused=False, exit_status=None, duration_ms=int((time.monotonic() - started) * 1000),
                              error=f"unexpected answer: {self.host} sent more than {exc.args[0]} bytes; it was not read")
        except (OSError, asyncssh.Error) as exc:
            if sent:
                raise OutcomeUnknown(f"connection to {self.host} lost after the request was sent: {_text(exc)}") from exc
            raise Unreachable(f"connection to {self.host} failed: {_text(exc)}") from exc
        ms = int((time.monotonic() - started) * 1000)
        digest = hashlib.sha256(out_bytes).hexdigest()  # of exactly what the server sent
        out = strip_nul(out_bytes.decode("utf-8", errors="replace"))
        lines = [line for line in out.splitlines() if line.strip()]
        last = lines[-1] if lines else ""
        payload: Any = {}
        problem = "unexpected answer (is cc-gate installed as the forced command?)"
        if len(last) > MAX_ANSWER:
            problem = f"unexpected answer: {len(last)} characters, too large to read"
        elif last:
            try:
                payload = strip_nul(json.loads(last, parse_float=_finite, parse_constant=_no_constant))
            except (ValueError, RecursionError):  # not JSON, nested too deeply, or numbers PostgreSQL cannot store
                payload = {}
        if not isinstance(payload, dict) or "ok" not in payload:
            snippet = (out or strip_nul(err_bytes.decode("utf-8", errors="replace")))[-300:]
            return GateAnswer(ok=False, refused=False, exit_status=process.exit_status, error=f"{problem}: {snippet}",
                              output_sha256=digest, duration_ms=ms)
        return GateAnswer(
            ok=bool(payload.get("ok")), refused=bool(payload.get("refused")), exit_status=process.exit_status, payload=payload,
            error=str(payload.get("error") or ""), output_sha256=digest, duration_ms=ms,
        )


class _TooLarge(Exception):
    pass


async def _read(process: asyncssh.SSHClientProcess, stream: asyncssh.SSHReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(65536):
        size += len(chunk)
        if size > limit:
            process.close()  # also ends the other stream's read
            raise _TooLarge(limit)
        chunks.append(chunk)
    return b"".join(chunks)
