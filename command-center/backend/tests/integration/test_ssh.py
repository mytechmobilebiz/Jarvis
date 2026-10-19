"""SSH to servers through cc-gate: key, host-key pinning, audit, approvals, production confirmation."""

from __future__ import annotations

import asyncio

import asyncssh
from sqlalchemy import select

from app.db import session_scope
from app.models import AuditLog, ServerCommand, Task, User, WorkerHeartbeat
from app.security import hash_password

from ..sshfake import FakeGateServer
from .test_agents import run_task


async def ssh_key(admin) -> dict:
    r = await admin.post("/api/ssh/key", json={})
    assert r.status_code == 201, r.text
    return r.json()["key"]


async def scan(admin, server_id: int):
    """POST .../host-key/scan. The worker reads the key (the API has no route out) and the API waits for it, so one runs meanwhile."""
    from app.config import get_settings
    from app.llm import NullProvider
    from app.worker.runner import Worker
    from app.workflow import N8nEngine

    from ..fakes import FakeN8n

    settings = get_settings()
    worker = Worker(settings, llm=NullProvider(), engine=N8nEngine(settings, transport=FakeN8n().transport), worker_id="test-worker")
    await worker.heartbeat()
    request = asyncio.ensure_future(admin.post(f"/api/servers/{server_id}/host-key/scan"))
    while not request.done():
        await worker.run_until_idle()
        await asyncio.wait([request], timeout=0.05)
    return request.result()


async def add_server(admin, srv: FakeGateServer, *, environment: str = "test", pin: bool = True) -> dict:
    r = await admin.post("/api/servers", json={"name": "srv1", "host": "127.0.0.1", "port": srv.port, "username": "ccagent",
                                               "environment": environment})
    assert r.status_code == 201, r.text
    server = r.json()["server"]
    if pin:
        scanned = (await scan(admin, server["id"])).json()
        assert scanned["fingerprint"] == srv.fingerprint  # what an admin compares on the server
        r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"fingerprint": scanned["fingerprint"]})
        assert r.status_code == 200, r.text
        server = r.json()["server"]
    return server


async def test_key_is_generated_here_and_only_the_public_half_is_shown(admin):
    key = await ssh_key(admin)
    assert key["openssh"].startswith("ssh-ed25519 ") and key["fingerprint"].startswith("SHA256:")
    assert key["authorized_keys_line"] == f'command="/usr/local/bin/cc-gate",restrict {key["openssh"]}'
    assert (await admin.post("/api/ssh/key", json={})).status_code == 409  # never replaced by accident
    for r in [await admin.get("/api/ssh/key"), await admin.get("/api/settings")]:
        assert "PRIVATE KEY" not in r.text


async def test_connection_through_the_gate_is_pinned_and_audited(admin, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv)
        assert server["host_key_pinned"] and server["host_key_fingerprint"] == srv.fingerprint
        task = await run_task(admin, make_worker, "server_management", "test_connection", {"server_id": server["id"]})
        assert task["status"] == "succeeded", task["error"]
        assert "cc-gate 1.2.0 as nobody" in task["summary"]
        overview = await run_task(admin, make_worker, "server_management", "server_overview", {"server_id": server["id"]})
        assert overview["status"] == "succeeded" and "up " in overview["summary"]
        services = await run_task(admin, make_worker, "server_management", "check_services", {"server_id": server["id"]})
        assert services["result"]["data"]["services"] == {"httpd": "active", "php-fpm": "inactive"}
        # Only cc-gate requests were sent, and the server's own log has them too.
        assert all(r and r.startswith("cc-gate v1 ") for r in srv.requests)
        assert [e["op"] for e in srv.gate_log()][:2] == ["hello", "system_overview"]
    async with session_scope() as s:
        rows = (await s.scalars(select(ServerCommand).order_by(ServerCommand.id))).all()
    assert [r.op for r in rows][:2] == ["hello", "system_overview"]
    assert all(r.ok and r.kind == "read" and r.requested_by == "admin@test.local" and r.output_sha256 for r in rows)
    view = (await admin.get(f"/api/servers/{server['id']}")).json()["server"]
    assert view["last_hello"]["user"] == "nobody" and view["last_seen_at"]
    assert (await admin.get(f"/api/servers/{server['id']}/commands")).json()["total"] == len(rows)


async def test_no_command_runs_on_an_unpinned_or_impostor_server(admin, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv, pin=False)
        task = await run_task(admin, make_worker, "server_management", "test_connection", {"server_id": server["id"]})
        assert task["status"] == "failed" and "not been pinned" in task["error"]
        # Pin a different key (as if the real server were swapped for an impostor).
        impostor = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode()
        r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": impostor})
        assert r.status_code == 200
        task = await run_task(admin, make_worker, "server_management", "test_connection", {"server_id": server["id"]})
        assert task["status"] == "failed" and "different host key" in task["error"]
        assert srv.requests == []  # nothing was ever sent
    events = (await admin.get("/api/events")).json()["events"]
    assert any(e["kind"] == "server.host_key_mismatch" and e["severity"] == "critical" for e in events)


async def test_trusting_a_key_needs_the_scanned_fingerprint(admin):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv, pin=False)
        assert (await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"fingerprint": srv.fingerprint})).status_code == 400
        assert (await scan(admin, server["id"])).status_code == 200
        assert (await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"fingerprint": "SHA256:wrong"})).status_code == 400
        r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": srv.host_key_line})
        assert r.json()["server"]["host_key_fingerprint"] == srv.fingerprint


async def test_servers_refuse_root_and_private_addresses(admin, make_worker):
    r = await admin.post("/api/servers", json={"name": "bad", "host": "srv.example", "username": "root"})
    assert r.status_code == 422
    key = await ssh_key(admin)
    r = await admin.post("/api/servers", json={"name": "internal", "host": "10.1.2.3", "username": "ccagent"})
    assert r.status_code == 201
    scanned = await scan(admin, r.json()["server"]["id"])
    assert scanned.status_code == 502 and "not a public address" in scanned.text
    assert key


async def test_the_worker_reads_the_host_key_and_the_api_answers_with_it(admin, make_worker):
    """The API has no route out of the Docker host: the scan is a read-only worker task the API waits for (0.5.0)."""
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv, pin=False)
        async with session_scope() as s:
            await s.execute(WorkerHeartbeat.__table__.delete())
        r = await admin.post(f"/api/servers/{server['id']}/host-key/scan")
        assert r.status_code == 503 and "worker is not running" in r.text  # said at once, and nothing left queued
        r = await scan(admin, server["id"])
        assert r.json() == {"fingerprint": srv.fingerprint, "host_key": srv.host_key_line, "matches_pinned": None, "pinned_fingerprint": None}
        assert srv.requests == []  # read the key only: no login, nothing sent to cc-gate
        r = await admin.patch("/api/agents/server_management", json={"enabled": False})
        assert r.status_code == 200, r.text
        r = await admin.post(f"/api/servers/{server['id']}/host-key/scan")
        assert r.status_code == 409 and "switched off" in r.text
    async with session_scope() as s:
        tasks = (await s.scalars(select(Task).order_by(Task.id))).all()
        scanned = (await s.scalars(select(AuditLog).where(AuditLog.action == "server.host_key_scanned"))).all()
    assert [(t.capability, t.risk, t.status, t.created_by) for t in tasks] == [("scan_host_key", "read", "succeeded", "admin@test.local")]
    assert [(a.actor, a.detail) for a in scanned] == [("admin@test.local", {"fingerprint": srv.fingerprint, "task_id": tasks[0].id})]


async def test_changes_need_approval_and_the_gate_can_still_say_no(admin, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"], allow_changes=False) as srv:
        server = await add_server(admin, srv)
        task = await run_task(admin, make_worker, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})
        assert task["status"] == "awaiting_approval" and task["risk"] == "change"
        approval = (await admin.get("/api/approvals")).json()["approvals"][0]
        assert approval["summary"].endswith(f"on server srv1 (ccagent@127.0.0.1:{srv.port})") and approval["details"]["environment"] == "test"
        assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})).status_code == 200
        await make_worker().run_until_idle()
        task = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
        assert task["status"] == "failed" and "changes are switched off" in task["error"]  # the server's own switch wins
        assert srv.calls("systemctl") == []
    async with session_scope() as s:
        refused = await s.scalar(select(ServerCommand).where(ServerCommand.op == "service_restart"))
    assert refused.refused and refused.approved_by == "admin@test.local"


async def test_production_changes_need_an_admin_who_types_the_name(admin, app_client, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv, environment="production")
        task = await run_task(admin, make_worker, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})
        approval = (await admin.get("/api/approvals")).json()["approvals"][0]
        assert approval["details"]["production"] is True and approval["details"]["confirm"] == "srv1"
        assert "(PRODUCTION)" in approval["summary"]
        # An operator cannot approve it at all.
        async with session_scope() as s:
            s.add(User(email="op@test.local", password_hash=hash_password("operator-password"), role="operator"))
        await app_client.post("/api/auth/logout")
        await app_client.post("/api/auth/login", json={"email": "op@test.local", "password": "operator-password"})
        r = await app_client.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True, "confirm": "srv1"})
        assert r.status_code == 400 and "admin" in r.text
        from ..conftest import ADMIN

        await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
        r = await app_client.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})
        assert r.status_code == 400 and "type 'srv1'" in r.text
        r = await app_client.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True, "confirm": "srv1"})
        assert r.status_code == 200
        await make_worker().run_until_idle()
        task = (await app_client.get(f"/api/tasks/{task['id']}")).json()["task"]
        assert task["status"] == "succeeded", task["error"]
        assert srv.calls("systemctl")[-2:] == ["restart httpd", "is-active httpd"]
        assert srv.calls("sudo")[-1].endswith("systemctl restart httpd")  # restarts go through sudo


async def server_request(admin, *, environment: str = "staging") -> tuple[dict, dict]:
    r = await admin.post("/api/servers", json={"name": "srv2", "host": "srv2.example", "username": "ccagent", "environment": environment})
    server = r.json()["server"]
    r = await admin.post("/api/agents/server_management/tasks", json={"capability": "restart_service",
                                                                     "params": {"server_id": server["id"], "service": "httpd"}})
    assert r.status_code == 201, r.text
    approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == r.json()["task"]["id"])
    return server, approval


async def test_a_target_that_became_production_or_changed_is_not_approved_on_stale_facts(admin):
    server, approval = await server_request(admin)
    assert approval["details"]["production"] is False
    await admin.patch(f"/api/servers/{server['id']}", json={"environment": "production"})
    r = await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})
    assert r.status_code == 400 and "became a production target" in r.text
    # Renamed since the request: the approval no longer describes the target. (Out of production again: the name is typed.)
    r = await admin.patch(f"/api/servers/{server['id']}", json={"environment": "staging", "name": "srv2-renamed", "confirm": "srv2"})
    assert r.status_code == 200, r.text
    r = await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})
    assert r.status_code == 400 and "target changed" in r.text
    assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": False})).status_code == 200


async def test_changes_for_targets_that_do_not_exist_are_refused(admin):
    r = await admin.post("/api/agents/server_management/tasks", json={"capability": "restart_service",
                                                                     "params": {"server_id": 9999, "service": "httpd"}})
    assert r.status_code == 400 and "no server with id 9999" in r.text
    r = await admin.post("/api/agents/wordpress_security/tasks", json={"capability": "repair_flush_caches", "params": {"site_id": 9999}})
    assert r.status_code == 400 and "no monitored site with id 9999" in r.text
    assert (await admin.get("/api/approvals")).json()["approvals"] == []


async def test_the_worker_refuses_an_approved_change_whose_target_changed(admin, make_worker):
    server, approval = await server_request(admin)
    assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})).status_code == 200
    await admin.patch(f"/api/servers/{server['id']}", json={"name": "somewhere-else"})  # before the worker picks it up
    await make_worker().run_until_idle()
    task = (await admin.get(f"/api/tasks/{approval['task_id']}")).json()["task"]
    assert task["status"] == "failed" and task["error"].startswith("refused: the target changed")
    async with session_scope() as s:
        assert (await s.scalars(select(ServerCommand))).all() == []  # nothing was sent


async def test_the_worker_refuses_an_approved_change_whose_target_became_production(admin, make_worker):
    server, approval = await server_request(admin)  # staging: approved without a typed name
    assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})).status_code == 200
    await admin.patch(f"/api/servers/{server['id']}", json={"environment": "production"})  # before the worker picks it up
    await make_worker().run_until_idle()
    task = (await admin.get(f"/api/tasks/{approval['task_id']}")).json()["task"]
    assert task["status"] == "failed" and "became a production target" in task["error"]
    async with session_scope() as s:
        assert (await s.scalars(select(ServerCommand))).all() == []


async def test_a_restart_checks_its_target_again_in_its_own_session(admin, make_worker, monkeypatch):
    from app import tasks as task_service
    from app.models import Server

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv, environment="staging")
        task = await run_task(admin, make_worker, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})
        approval = (await admin.get("/api/approvals")).json()["approvals"][0]
        assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})).status_code == 200
        real = task_service.current_target

        async def check_then_promote(session, approval, params, *, lock=False):
            # The worker's check passes; the server becomes production before the agent loads it.
            fresh = await real(session, approval, params, lock=lock)
            async with session_scope() as s:
                (await s.get(Server, server["id"])).environment = "production"
            return fresh

        monkeypatch.setattr(task_service, "current_target", check_then_promote)
        await make_worker().run_until_idle()
        task = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
        assert task["status"] == "failed" and "became a production target" in task["error"]
        assert srv.calls("systemctl") == []
    async with session_scope() as s:
        assert (await s.scalars(select(ServerCommand))).all() == []  # the gate was never opened


async def test_names_used_for_typed_confirmation_are_trimmed_and_never_empty(admin):
    assert (await admin.post("/api/sites", json={"name": "   ", "url": "https://blank.example"})).status_code == 422
    site = (await admin.post("/api/sites", json={"name": " Shop ", "url": "https://shop.example"})).json()["site"]
    assert site["name"] == "Shop"
    assert (await admin.patch(f"/api/sites/{site['id']}", json={"name": " "})).status_code == 422
    assert (await admin.patch(f"/api/sites/{site['id']}", json={"name": "Shop 2 "})).json()["site"]["name"] == "Shop 2"
    r = await admin.post("/api/servers", json={"name": " cwp ", "host": "cwp.example", "username": "ccagent"})
    assert r.json()["server"]["name"] == "cwp"
    assert (await admin.patch(f"/api/servers/{r.json()['server']['id']}", json={"name": "  "})).status_code == 422


async def test_an_outdated_gate_is_flagged(admin, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        gate = srv.dir / "cc-gate"
        gate.write_text(gate.read_text().replace('GATE_VERSION = "1.2.0"', 'GATE_VERSION = "1.1.0"'))  # cannot pin plugin updates
        server = await add_server(admin, srv)
        task = await run_task(admin, make_worker, "server_management", "test_connection", {"server_id": server["id"]})
        assert task["status"] == "succeeded" and "gate_outdated" in {f["code"] for f in task["result"]["findings"]}
