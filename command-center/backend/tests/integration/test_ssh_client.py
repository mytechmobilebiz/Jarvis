"""What reaches the audit trail and the events when a server (or a site on it) answers badly or goes away mid-change."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import asyncssh
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Event, Server, ServerCommand
from app.ssh.client import MAX_ANSWER

from ..sshfake import FakeGateServer
from ..unit.test_ssh_client_unit import _packet, _string, key_line, openssh_without, ssh_server
from .test_agents import run_task
from .test_repairs import approve, linked_site
from .test_ssh import ssh_key

PLUGIN_LIST_WRAPPER = """#!/bin/sh
# What malware in wp-config.php or a must-use plugin can make WP-CLI print for 'plugin list'.
D="$(dirname "$0")"
case " $* " in *" plugin list "*) cat "$D/plugin-json"; exit 0 ;; esac
exec "$D/wp-cli-real" "$@"
"""


def site_prints_plugin_list(srv: FakeGateServer, text: str) -> None:
    (srv.dir / "wp-cli").rename(srv.dir / "wp-cli-real")
    (srv.dir / "wp-cli").write_text(PLUGIN_LIST_WRAPPER)
    (srv.dir / "wp-cli").chmod(0o755)
    (srv.dir / "plugin-json").write_text(text)
    (srv.dir / "plugin-json").chmod(0o644)


async def audit_rows(task_id: int | None = None) -> list[ServerCommand]:
    async with session_scope() as s:
        query = select(ServerCommand).order_by(ServerCommand.id)
        if task_id is not None:
            query = query.where(ServerCommand.task_id == task_id)
        return list((await s.scalars(query)).all())


async def events(kind: str) -> list[Event]:
    async with session_scope() as s:
        return list((await s.scalars(select(Event).where(Event.kind == kind).order_by(Event.id))).all())


async def pinned_server(admin, port: int, host_key: asyncssh.SSHKey) -> dict:
    r = await admin.post("/api/servers", json={"name": "srv1", "host": "127.0.0.1", "port": port, "username": "ccagent",
                                               "environment": "test"})
    server = r.json()["server"]
    r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": key_line(host_key)})
    assert r.status_code == 200, r.text
    return server


async def test_a_lone_surrogate_from_the_site_no_longer_breaks_the_diagnosis(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()  # the baseline scan first: run beside the diagnosis, its server part could take the site lock
        site_prints_plugin_list(srv, '[{"name":"evil\\ud800","status":"must-use","version":"","update":"none","update_version":""}]')
        seen = len(srv.gate_log())
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert "wp_core_verify" in [e["op"] for e in srv.gate_log()[seen:]]  # the parts after the plugin list still ran
    plugins = diag["result"]["data"]["diagnosis"]["plugins"]
    assert [p["name"] for p in plugins] == ["evil�"]
    rows = await audit_rows(diag["id"])
    assert rows and all(r.finished_at for r in rows)
    assert next(r for r in rows if r.op == "wp_plugin_list").result["plugins"][0]["name"] == "evil�"


# (Integers over 4300 digits, which a Python 3.6 gate relays, are covered in tests/unit/test_ssh_client_unit.py.)
@pytest.mark.parametrize("text", ["[" * 985 + "]" * 985, '[{"name": NaN}]'])
async def test_answers_the_backend_cannot_parse_spoil_only_their_own_part(admin, web, make_worker, text):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()  # the baseline scan first (see above)
        site_prints_plugin_list(srv, text)  # the gate relays both
        seen = len(srv.gate_log())
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert "wp_core_verify" in [e["op"] for e in srv.gate_log()[seen:]]
    assert diag["result"]["data"]["diagnosis"]["errors"]["plugins"].startswith("wp_plugin_list failed")
    row = next(r for r in await audit_rows(diag["id"]) if r.op == "wp_plugin_list")
    assert row.finished_at and row.ok is False and row.error.startswith("unexpected answer")  # not "cancelled"


async def gate_session(admin, failure: BaseException, name: str = "srv9"):
    from app.config import get_settings
    from app.ssh.gateway import GateSession, SSHGateway

    server = (await admin.post("/api/servers", json={"name": name, "host": f"{name}.example", "username": "ccagent"})).json()["server"]
    async with session_scope() as s:
        row = await s.get(Server, server["id"])
    ctx = SimpleNamespace(approved_by="admin@test.local", requested_by="admin@test.local", task_id=None, agent_name="server_management",
                          time_left=lambda: None, log=lambda *a, **k: None)
    session = GateSession(SSHGateway(get_settings()), ctx, row)

    async def request(command, timeout):
        raise failure

    session._conn = SimpleNamespace(request=request)
    return session


async def test_an_unexpected_error_is_recorded_as_such_and_raised_as_an_ssh_error(admin):
    from app.ssh.gateway import GateFailed

    for op, args in (("wp_plugin_list", {"site": "site-one"}), ("wp_cache_flush", {"site": "site-one"})):
        session = await gate_session(admin, RuntimeError("something nobody expected"), name=op.replace("_", "-"))
        with pytest.raises(GateFailed) as raised:  # what callers (e.g. the diagnosis, part by part) handle
            await session.run(op, **args)
        assert "unexpected error (RuntimeError)" in str(raised.value)
        audit = (await audit_rows())[-1]
        assert audit.op == op and audit.finished_at and audit.error == "outcome unknown: unexpected error: RuntimeError"
    assert len(await events("server.change_outcome_unknown")) == 1  # for the change only


async def test_keyboard_interrupt_is_not_swallowed(admin):
    session = await gate_session(admin, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        await session.run("wp_plugin_list", site="site-one")


async def change_against(admin, make_worker, handle) -> tuple[dict, list[str]]:
    """restart_service, approved, against an SSH server that runs handle(process) for the service_restart request."""
    await ssh_key(admin)
    seen: list[str] = []

    async def dispatch(process):
        seen.append(process.command)
        if "service_restart" in (process.command or ""):
            await handle(process)
            return
        process.stdout.write(b'{"ok": true, "result": {"service": "httpd", "state": "active"}}\n')
        process.exit(0)

    async with ssh_server(dispatch) as (port, host_key):
        server = await pinned_server(admin, port, host_key)
        task = await run_task(admin, make_worker, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})
        return await approve(admin, make_worker, task), seen


async def test_a_change_whose_connection_drops_is_recorded_as_outcome_unknown(admin, make_worker):
    async def handle(process):
        await asyncio.sleep(0.3)  # sshd keeps a forced command running when the client goes away
        process.channel.get_connection().abort()

    task, seen = await change_against(admin, make_worker, handle)
    assert task["status"] == "failed" and "lost after the request was sent" in task["error"]
    assert seen == ["cc-gate v1 service_restart httpd"]
    audit = await audit_rows(task["id"])
    assert audit[0].op == "service_restart" and audit[0].error.startswith("outcome unknown: connection to 127.0.0.1 lost")
    unknown = await events("server.change_outcome_unknown")
    assert len(unknown) == 1 and unknown[0].severity == "critical" and unknown[0].detail["audit_id"] == audit[0].id


async def test_a_change_whose_channel_closes_without_an_answer_is_outcome_unknown(admin, make_worker):
    async def handle(process):
        process.stdout.write(b'{"ok": tr')  # cut off: no full answer and no exit status
        process.channel.close()

    task, _ = await change_against(admin, make_worker, handle)
    assert task["status"] == "failed"
    audit = await audit_rows(task["id"])
    assert audit[0].exit_status is None and audit[0].error.startswith("outcome unknown: unexpected answer")
    assert len(await events("server.change_outcome_unknown")) == 1


@pytest.mark.parametrize("how", ["killed", "unreadable"])
async def test_a_change_whose_gate_is_killed_or_answers_unreadably_is_outcome_unknown(admin, make_worker, how):
    async def handle(process):
        if how == "killed":
            process.channel.exit_with_signal("KILL")  # the OOM killer, or kill -9, part-way through: exit status -1
        else:
            process.stdout.write(b'{"ok": true, "result": "' + b"x" * (MAX_ANSWER + 10) + b'"}\n')  # ran, but too large to read
            process.exit(0)

    task, _ = await change_against(admin, make_worker, handle)
    assert task["status"] == "failed"
    audit = await audit_rows(task["id"])
    assert audit[0].exit_status == (-1 if how == "killed" else 0) and audit[0].error.startswith("outcome unknown: unexpected answer")
    unknown = await events("server.change_outcome_unknown")
    assert len(unknown) == 1 and unknown[0].severity == "critical" and unknown[0].detail["audit_id"] == audit[0].id


async def test_an_answer_postgresql_refuses_still_closes_the_row_and_keeps_its_outcome(admin, make_worker, monkeypatch):
    from app.ssh import gateway

    monkeypatch.setattr(gateway, "_clip", lambda result: {"unstorable": float("nan")})  # whatever the database refuses
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await pinned_server(admin, srv.port, srv.host_key)
        task = await run_task(admin, make_worker, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})
        task = await approve(admin, make_worker, task)
        assert task["status"] == "succeeded", task["error"]  # the restart worked, and nobody is told otherwise
        assert srv.calls("systemctl")[-2:] == ["restart httpd", "is-active httpd"]
    audit = await audit_rows(task["id"])
    assert [r.op for r in audit] == ["service_restart", "service_status"]
    assert all(r.finished_at and r.ok and r.result is None and r.error == "could not store the answer (DBAPIError)" for r in audit)


async def test_a_host_key_mismatch_with_nul_in_the_servers_text_is_still_reported(admin, make_worker):
    await ssh_key(admin)

    async def handle(reader, writer):
        writer.write(b"SSH-2.0-OpenSSH_9.6p1\r\n")
        await reader.readline()
        writer.write(_packet(bytes([1]) + struct.pack(">I", 3) + _string(b"no matching host key\x00type") + _string(b"")))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        srv = await pinned_server(admin, server.sockets[0].getsockname()[1], asyncssh.generate_private_key("ssh-ed25519"))
        task = await run_task(admin, make_worker, "server_management", "test_connection", {"server_id": srv["id"]})
    assert task["status"] == "failed" and "did not offer the pinned host key type" in task["error"]
    mismatch = await events("server.host_key_mismatch")
    assert len(mismatch) == 1 and mismatch[0].severity == "critical" and "host key�type" in mismatch[0].detail["error"]


async def test_an_impostor_that_just_drops_the_connection_raises_the_host_key_alarm(admin, make_worker):
    await ssh_key(admin)
    impostor = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    async with ssh_server(key=impostor) as (backend, _), openssh_without(backend, b"rsa-sha2-") as (port, _):
        srv = await pinned_server(admin, port, asyncssh.generate_private_key("ssh-ed25519"))
        task = await run_task(admin, make_worker, "server_management", "test_connection", {"server_id": srv["id"]})
    assert task["status"] == "failed" and "does not offer the pinned host key" in task["error"]
    mismatch = await events("server.host_key_mismatch")
    assert len(mismatch) == 1 and impostor.get_fingerprint("sha256") in mismatch[0].detail["error"]
    assert await audit_rows() == []  # nothing was sent
