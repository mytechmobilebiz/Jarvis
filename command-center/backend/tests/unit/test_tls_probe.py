"""probe_tls against real local TLS servers with generated certificates."""

from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime, timedelta

import pytest
import trustme

from app.agents.monitoring.checks import probe_tls


async def serve(cert: trustme.LeafCert):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert.configure_cert(ctx)

    async def handle(reader, writer):
        try:
            await reader.read(10)
        except (ConnectionError, ssl.SSLError):
            pass
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


def client_ctx(ca: trustme.CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca.configure_trust(ctx)
    return ctx


@pytest.fixture
def ca():
    return trustme.CA(organization_name="Test CA")


async def test_valid_certificate_days_left(ca):
    cert = ca.issue_cert("localhost", not_after=datetime.now(UTC) + timedelta(days=40, hours=1))
    server, port = await serve(cert)
    async with server:
        info = await probe_tls("localhost", port, 5, client_ctx(ca))
    assert info.error == ""
    assert info.days_left == 40
    assert info.issuer == "Test CA"


async def test_expired_certificate_fails_verification(ca):
    cert = ca.issue_cert(
        "localhost",
        not_before=datetime.now(UTC) - timedelta(days=100),
        not_after=datetime.now(UTC) - timedelta(days=2),
    )
    server, port = await serve(cert)
    async with server:
        info = await probe_tls("localhost", port, 5, client_ctx(ca))
    assert info.days_left is None
    assert "expired" in info.error


async def test_untrusted_certificate_is_reported(ca):
    cert = ca.issue_cert("localhost")
    server, port = await serve(cert)
    async with server:
        info = await probe_tls("localhost", port, 5)  # default trust store: test CA unknown
    assert info.error and "probe failed" not in info.error


async def test_wrong_hostname_is_reported(ca):
    cert = ca.issue_cert("other.example")
    server, port = await serve(cert)
    async with server:
        info = await probe_tls("localhost", port, 5, client_ctx(ca))
    assert "hostname" in info.error.lower() or "mismatch" in info.error.lower()


async def test_closed_port_is_a_probe_failure():
    info = await probe_tls("127.0.0.1", 1, 2)
    assert info.error.startswith("probe failed")
