"""GateConnection and scan_host_key against in-process SSH servers: answer parsing, sanitising, address fallback, impostors."""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from contextlib import asynccontextmanager

import asyncssh
import pytest

from app.agents import http
from app.config import Settings
from app.ssh import client
from app.ssh.client import GateConnection, HostKeyMismatch, OutcomeUnknown, Unreachable, scan_host_key, strip_nul

CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")


def settings(**overrides) -> Settings:
    return Settings(**{"ssh_allowed_private_hosts": "127.0.0.1,ssh.test", "ssh_connect_timeout_seconds": 5, **overrides})


def key_line(key: asyncssh.SSHKey) -> str:
    return " ".join(key.export_public_key("openssh").decode().split()[:2])


class AnyKey(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return False

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return True


@asynccontextmanager
async def ssh_server(handle=None, *, host: str = "127.0.0.1", port: int = 0, key: asyncssh.SSHKey | None = None,
                     server_factory=AnyKey):
    """An SSH server answering each request with handle(process) (raw bytes; None: a hello answer, False: the server
    factory's own session_requested decides); yields (port, host key)."""
    key = key or asyncssh.generate_private_key("ssh-ed25519")

    async def default(process):
        process.stdout.write(b'{"ok": true, "result": {"hello": 1}}\n')
        process.exit(0)

    listener = await asyncssh.listen(host, port, server_host_keys=[key], server_factory=server_factory,
                                     process_factory=None if handle is False else handle or default, encoding=None, allow_pty=False)
    try:
        yield listener.sockets[0].getsockname()[1], key
    finally:
        listener.close()
        await listener.wait_closed()


def gate(port: int, key: asyncssh.SSHKey, host: str = "127.0.0.1", **overrides) -> GateConnection:
    return GateConnection(host=host, port=port, username="ccagent", host_key=key_line(key), client_key=CLIENT_KEY,
                          settings=settings(**overrides))


def answering(*outputs: bytes):
    """Answer the n-th request with outputs[n] (the last one repeats)."""
    seen: list[str] = []

    async def handle(process):
        seen.append(process.command)
        process.stdout.write(outputs[min(len(seen), len(outputs)) - 1])
        process.exit(0)

    return handle


@pytest.fixture
def resolve_to(monkeypatch):
    """Make every name resolve to the given addresses, in that order."""
    def set_addresses(*addresses: str) -> None:
        async def resolver(host):
            return list(addresses)

        monkeypatch.setattr(http, "default_resolver", resolver)

    return set_addresses


# ----------------------------------------------------------------- answers


def test_strip_nul_replaces_lone_surrogates_and_keeps_real_characters():
    assert strip_nul({"a\ud800": ["x\udfff", "\x00", "\U0001F600 ok"]}) == {"a�": ["x�", "�", "\U0001F600 ok"]}
    assert strip_nul(json.loads('"\\ud83d\\ude00 \\ud800"')) == "\U0001F600 �"  # a valid pair stays one character


def test_strip_nul_refuses_deep_nesting_with_value_error():
    with pytest.raises(ValueError):
        strip_nul(json.loads("[" * 900 + "]" * 900))
    assert strip_nul(json.loads("[" * 60 + "]" * 60))  # deeper than any gate answer, still fine


@pytest.mark.parametrize("line", [
    b'{"ok": true, "result": ' + b"[" * 985 + b"]" * 985 + b"}",  # RecursionError in the old strip_nul
    b'{"ok": true, "result": ' + b"[" * 5000 + b"]" * 5000 + b"}",  # RecursionError in json.loads itself
    b'{"ok": true, "result": ' + b"7" * 5000 + b"}",  # ValueError: more than 4300 digits
    b'{"ok": true, "result": NaN}',  # PostgreSQL JSON has no NaN ...
    b'{"ok": true, "result": [1e999]}',  # ... and no Infinity
    b'{"ok": true, "result": "' + b"x" * (client.MAX_ANSWER + 10) + b'"}',  # too large to parse
])
async def test_malformed_or_oversized_answers_are_unexpected_answers(line):
    async with ssh_server(answering(line + b"\n")) as (port, key), gate(port, key) as conn:
        answer = await conn.request("cc-gate v1 hello", 10)
    assert answer.ok is False and answer.payload == {} and answer.error.startswith("unexpected answer")
    assert answer.exit_status == 0 and answer.output_sha256


async def test_the_answer_size_limits():
    assert client.MAX_STDOUT == 4 * 1024 * 1024 and client.MAX_ANSWER == client.MAX_STDOUT // 2
    fits = json.dumps({"ok": True, "result": "x" * (client.MAX_ANSWER - 100)}).encode() + b"\n"
    async with ssh_server(answering(fits)) as (port, key), gate(port, key) as conn:
        answer = await conn.request("cc-gate v1 hello", 10)
    assert answer.ok and len(answer.result) == client.MAX_ANSWER - 100


async def test_a_lone_surrogate_in_an_answer_is_replaced():
    line = b'{"ok": true, "result": [{"name": "evil\\ud800", "\\udc00": "\\u0000"}], "error": "e\\ud800"}\n'
    async with ssh_server(answering(line)) as (port, key), gate(port, key) as conn:
        answer = await conn.request("cc-gate v1 wp_plugin_list site-one", 10)
    assert answer.ok and answer.result == [{"name": "evil�", "�": "�"}] and answer.error == "e�"


async def test_a_flood_spoils_only_its_own_answer():
    flood = b"x" * (client.MAX_STDOUT + 1)
    async with ssh_server(answering(flood, b'{"ok": true, "result": 2}\n')) as (port, key), gate(port, key) as conn:
        answer = await conn.request("cc-gate v1 wp_admins site-one", 10)
        assert answer.ok is False and answer.exit_status is None and "more than" in answer.error
        assert (await conn.request("cc-gate v1 hello", 10)).result == 2  # the connection still serves the next request


# ----------------------------------------------------------------- lost connections and server text


async def test_a_connection_lost_after_the_request_was_sent_is_outcome_unknown():
    async def handle(process):
        await asyncio.sleep(0.2)  # the request runs on the server ...
        process.channel.get_connection().abort()  # ... and the connection is gone

    async with ssh_server(handle) as (port, key), gate(port, key) as conn:
        with pytest.raises(OutcomeUnknown) as raised:
            await conn.request("cc-gate v1 service_restart httpd", 10)
    assert "lost after the request was sent" in str(raised.value)


async def test_a_refused_channel_is_unreachable_and_server_text_is_sanitised():
    class Refusing(AnyKey):
        def session_requested(self):
            raise asyncssh.ChannelOpenError(asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, "refused\x00here")

    async with ssh_server(False, server_factory=Refusing) as (port, key), gate(port, key) as conn:
        with pytest.raises(Unreachable) as raised:  # nothing was sent: not "outcome unknown"
            await conn.request("cc-gate v1 hello", 10)
    assert "refused\ufffdhere" in str(raised.value)


def _packet(payload: bytes) -> bytes:
    pad = 8 - ((len(payload) + 5) % 8)
    pad += 8 if pad < 4 else 0
    return struct.pack(">IB", len(payload) + pad + 1, pad) + payload + b"\0" * pad


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


async def test_a_disconnect_reason_with_nul_is_sanitised():
    async def handle(reader, writer):
        writer.write(b"SSH-2.0-OpenSSH_9.6p1\r\n")
        await reader.readline()
        # DISCONNECT, reason 3 (key exchange failed), with a NUL in the description
        writer.write(_packet(bytes([1]) + struct.pack(">I", 3) + _string(b"no matching host key\x00type") + _string(b"")))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        with pytest.raises(HostKeyMismatch) as raised:
            await gate(server.sockets[0].getsockname()[1], asyncssh.generate_private_key("ssh-ed25519")).__aenter__()
    assert "\x00" not in str(raised.value) and "host key�type" in str(raised.value)


# ----------------------------------------------------------------- impostors that just drop the connection


@asynccontextmanager
async def openssh_without(backend_port: int, needs: bytes, *, drop_first: bool = False, reset: bool = False):
    """A proxy in front of an asyncssh server that behaves like OpenSSH without a host key of the type the client offers.

    It reads the client's KEXINIT and, unless it lists `needs` (or always, for the first connection with drop_first),
    drops the connection without a DISCONNECT (FIN, or RST with reset). Otherwise it passes the bytes through.
    """
    connections = []

    async def pump(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def handle(reader, writer):
        connections.append(writer)
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", backend_port)
        down = asyncio.ensure_future(pump(up_reader, writer))  # the server's banner and KEXINIT go out at once
        data = b""
        while chunk := await reader.read(65536):  # the client's banner and KEXINIT
            data += chunk
            end = data.find(b"\n")
            if end >= 0 and len(data) >= end + 5 and len(data) >= end + 5 + int.from_bytes(data[end + 1:end + 5], "big"):
                break
        if (drop_first and len(connections) == 1) or needs not in data:
            if reset:
                writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            writer.transport.abort()
            up_writer.close()
            down.cancel()
            return
        up_writer.write(data)
        await asyncio.gather(pump(reader, up_writer), down, return_exceptions=True)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1], connections


@pytest.mark.parametrize("reset", [False, True])
async def test_an_impostor_without_the_pinned_key_type_is_a_host_key_mismatch(reset):
    impostor_key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    pinned = asyncssh.generate_private_key("ssh-ed25519")  # what the real server had
    async with ssh_server(key=impostor_key) as (backend, _), openssh_without(backend, b"rsa-sha2-", reset=reset) as (port, seen):
        with pytest.raises(HostKeyMismatch) as raised:
            await gate(port, pinned).__aenter__()
    assert impostor_key.get_fingerprint("sha256") in str(raised.value)
    assert len(seen) == 2  # the real attempt, dropped, then the probe that offered every key type


async def test_a_dropped_connection_to_the_real_server_is_still_unreachable():
    async with ssh_server() as (backend, key), openssh_without(backend, b"", drop_first=True) as (port, seen):
        with pytest.raises(Unreachable):  # the probe saw the pinned key: a network problem, not an impostor
            await gate(port, key).__aenter__()
    assert len(seen) == 2


# ----------------------------------------------------------------- several addresses


async def test_every_address_is_tried_until_one_connects(resolve_to):
    async with ssh_server() as (port, key):
        resolve_to("127.0.0.2", "127.0.0.1")  # nothing listens on 127.0.0.2
        async with gate(port, key, host="ssh.test") as conn:
            assert (await conn.request("cc-gate v1 hello", 10)).ok
        line, fingerprint = await scan_host_key("ssh.test", port, settings())
    assert line == key_line(key) and fingerprint == key.get_fingerprint("sha256")


async def test_a_host_key_mismatch_is_never_hidden_by_the_next_address(resolve_to):
    async with ssh_server(host="127.0.0.2") as (port, real_key), ssh_server(host="127.0.0.1", port=port):
        resolve_to("127.0.0.1", "127.0.0.2")  # an impostor on the first address, the real server on the second
        with pytest.raises(HostKeyMismatch):
            await gate(port, real_key, host="ssh.test").__aenter__()


async def test_a_refused_key_is_never_retried_on_the_next_address(resolve_to):
    class NoKey(AnyKey):
        def validate_public_key(self, username, key):
            return False

    async with ssh_server(host="127.0.0.2", server_factory=NoKey) as (port, key), ssh_server(host="127.0.0.1", port=port, key=key):
        resolve_to("127.0.0.2", "127.0.0.1")
        with pytest.raises(client.AuthFailed):
            await gate(port, key, host="ssh.test").__aenter__()


async def test_a_slow_login_keeps_the_whole_connect_timeout_when_another_address_fails_at_once(resolve_to):
    class SlowLogin(AnyKey):
        async def begin_auth(self, username):
            await asyncio.sleep(5.5)  # a loaded server; more than the 5 s an address used to get when it was not the last
            return True

    async with ssh_server(server_factory=SlowLogin) as (port, key):
        resolve_to("127.0.0.1", "2001:db8::1")  # A and AAAA, on an IPv4-only worker (the Docker default): IPv6 fails at once
        async with gate(port, key, host="ssh.test", ssh_connect_timeout_seconds=15) as conn:
            assert (await conn.request("cc-gate v1 hello", 10)).ok


async def test_when_no_address_connects_each_ones_error_is_reported(resolve_to, monkeypatch):
    loop = asyncio.get_running_loop()
    real_connect = loop.sock_connect

    async def sock_connect(sock, address):
        if address[0] == "127.0.0.2":
            await asyncio.Event().wait()  # SYNs lost: never connects
        return await real_connect(sock, address)

    monkeypatch.setattr(loop, "sock_connect", sock_connect)
    async with ssh_server() as (port, key):
        resolve_to("127.0.0.2", "127.0.0.3")  # nothing listens on 127.0.0.3: refused at once
        with pytest.raises(Unreachable) as raised:
            await gate(port, key, host="ssh.test", ssh_connect_timeout_seconds=1).__aenter__()
    assert "127.0.0.2: timed out; 127.0.0.3: [Errno 111] Connect call failed" in str(raised.value)  # not just the last one's
