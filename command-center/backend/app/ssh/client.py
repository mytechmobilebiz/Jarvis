"""The SSH client: one pinned host key, one key, public-key auth only, cc-gate requests only.

Nothing here can open a shell, forward ports or agents, read ~/.ssh/config or
accept a host key that was not pinned by a person.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from ..agents.http import BlockedTarget, check_public_host
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


def _options(settings: Settings) -> dict[str, Any]:
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
        "connect_timeout": settings.ssh_connect_timeout_seconds,
        "login_timeout": settings.ssh_connect_timeout_seconds,
    }


async def _check_target(host: str, settings: Settings) -> None:
    try:
        await check_public_host(host, settings.ssh_private_hosts)
    except BlockedTarget as exc:
        raise Unreachable(f"refused to connect: {exc}") from exc


HOST_KEY_ALGS = ["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256"]


async def scan_host_key(host: str, port: int, settings: Settings) -> tuple[str, str]:
    """The host key a server presents (for a person to compare and pin), as (openssh line, SHA256 fingerprint)."""
    await _check_target(host, settings)
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port, server_host_key_algs=HOST_KEY_ALGS, config=[]),
            timeout=settings.ssh_connect_timeout_seconds,
        )
    except (OSError, TimeoutError, asyncssh.Error) as exc:
        raise Unreachable(f"could not reach {host}:{port}: {exc}") from exc
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
        await _check_target(self.host, self.settings)
        known = asyncssh.import_known_hosts(f"{_pattern(self.host, self.port)} {self.host_key}\n")
        try:
            self._conn = await asyncssh.connect(
                self.host, port=self.port, username=self.username, client_keys=[self.client_key], known_hosts=known,
                **_options(self.settings),
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            raise HostKeyMismatch(f"{self.host} presented a different host key than the pinned one: {exc}") from exc
        except asyncssh.PermissionDenied as exc:
            raise AuthFailed(f"{self.username}@{self.host} refused the Command Center's key") from exc
        except (OSError, TimeoutError, asyncssh.Error) as exc:
            raise Unreachable(f"could not connect to {self.host}:{self.port}: {exc}") from exc
        return self

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
        try:
            done = await asyncio.wait_for(
                self._conn.run(command, check=False, stdin=asyncssh.DEVNULL, encoding="utf-8", errors="replace"), timeout=timeout
            )
        except TimeoutError as exc:
            raise SSHError(f"no answer within {timeout:g} seconds") from exc
        except (OSError, asyncssh.Error) as exc:
            raise Unreachable(f"connection to {self.host} failed: {exc}") from exc
        ms = int((time.monotonic() - started) * 1000)
        out = str(done.stdout or "")
        digest = hashlib.sha256(out.encode("utf-8")).hexdigest()
        lines = [line for line in out.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict) or "ok" not in payload:
            snippet = (out or str(done.stderr or ""))[-300:]
            return GateAnswer(ok=False, refused=False, exit_status=done.exit_status, error=f"unexpected answer (is cc-gate installed as the forced command?): {snippet}",
                              output_sha256=digest, duration_ms=ms)
        return GateAnswer(
            ok=bool(payload.get("ok")), refused=bool(payload.get("refused")), exit_status=done.exit_status, payload=payload,
            error=str(payload.get("error") or ""), output_sha256=digest, duration_ms=ms,
        )
