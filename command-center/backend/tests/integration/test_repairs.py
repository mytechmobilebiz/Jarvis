"""WP-CLI diagnosis and approval-based repairs through cc-gate (fake WP-CLI behind the real gate)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Artifact, ServerCommand, User
from app.security import hash_password

from ..fakes import FakeWeb, wordpress_site
from ..sshfake import FakeGateServer
from .test_agents import run_task
from .test_ssh import add_server, ssh_key


async def linked_site(admin, srv: FakeGateServer, web: FakeWeb, *, environment: str = "test", server_env: str = "test") -> tuple[dict, dict]:
    server = await add_server(admin, srv, environment=server_env)
    web.add("site.test", wordpress_site("http://site.test", exposed=False))
    site = (await admin.post("/api/sites", json={"name": "Site One", "url": "http://site.test", "environment": environment})).json()["site"]
    r = await admin.patch(f"/api/sites/{site['id']}", json={"server_id": server["id"], "server_site_key": "site-one"})
    assert r.status_code == 200, r.text
    return site, server


async def approve(admin, make_worker, task: dict, confirm: str = "") -> dict:
    assert task["status"] == "awaiting_approval" and task["risk"] == "change"
    approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == task["id"])
    r = await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True, "confirm": confirm})
    assert r.status_code == 200, r.text
    await make_worker().run_until_idle()
    return (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]


def put(path, text: str) -> None:
    """Write a file the gate (running as "nobody") may change."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o777)
    path.write_text(text)
    path.chmod(0o666)


async def ops_for(task_id: int) -> list[tuple[str, str]]:
    async with session_scope() as s:
        rows = (await s.scalars(select(ServerCommand).where(ServerCommand.task_id == task_id).order_by(ServerCommand.id))).all()
    return [(r.op, r.kind) for r in rows]


async def test_diagnosis_finds_the_redirect_hack_and_the_repair_fixes_it(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()  # the baseline scan, which now includes the server-side check
        put(srv.dir / "option-home", "http://evil.example")
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        codes = {f["code"]: f["severity"] for f in diag["result"]["findings"]}
        assert codes["site_url_mismatch"] == "critical" and codes["plugin_update_available"] == "warning"
        assert all(kind == "read" for _, kind in await ops_for(diag["id"]))
        events = (await admin.get("/api/events")).json()["events"]
        assert any(e["kind"] == "site.server_finding" and e["severity"] == "critical" for e in events)

        task = await run_task(admin, make_worker, "wordpress_security", "repair_restore_site_url", {"site_id": site["id"]})
        approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == task["id"])
        assert approval["details"]["target"] == "site Site One (http://site.test) on server srv1" and not approval["details"]["production"]
        done = await approve(admin, make_worker, task)
        assert done["status"] == "succeeded", done["error"]
        assert done["result"]["data"]["changed"] is True
        assert "database backup:" in done["summary"] and "was home http://evil.example" in done["summary"]
        assert (srv.dir / "option-home").read_text() == "http://site.test"
        # Check, back up, re-check just before changing, change, verify.
        assert await ops_for(done["id"]) == [("wp_site_urls", "read"), ("wp_db_export", "change"), ("wp_site_urls", "read"),
                                              ("wp_set_site_urls", "change"), ("wp_site_urls", "read")]
        assert any(e["kind"] == "repair.completed" for e in (await admin.get("/api/events")).json()["events"])

        again = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_restore_site_url",
                                                                 {"site_id": site["id"]}))
        assert again["result"]["data"]["changed"] is False and "nothing was changed" in again["summary"]
        assert await ops_for(again["id"]) == [("wp_site_urls", "read")]  # no backup, no change when nothing is wrong


async def test_deactivating_a_plugin_and_noticing_the_site_went_down(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        healthy = wordpress_site("http://site.test", exposed=False)
        # The site breaks once the plugin is deactivated.
        web.add("site.test", lambda r: httpx.Response(500, text="<html>Fatal error</html>") if (srv.dir / "plugin-evil-cloak").exists()
                else healthy(r))
        task = await run_task(admin, make_worker, "wordpress_security", "repair_deactivate_plugin", {"site_id": site["id"], "slug": "evil-cloak"})
        done = await approve(admin, make_worker, task)
        assert done["status"] == "succeeded"
        assert (srv.dir / "plugin-evil-cloak").read_text().strip() == "inactive"
        assert {f["code"] for f in done["result"]["findings"]} == {"site_worse_after_repair"}
        assert "(was up)" in done["result"]["findings"][0]["title"]
        events = (await admin.get("/api/events")).json()["events"]
        assert any(e["kind"] == "repair.site_down" and e["severity"] == "critical" for e in events)


async def test_debug_log_is_quarantined_not_deleted(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        put(srv.dir / "wp" / "wp-content" / "debug.log", "PHP Warning: x")
        task = await run_task(admin, make_worker, "wordpress_security", "repair_quarantine_debug_log", {"site_id": site["id"]})
        done = await approve(admin, make_worker, task)
        assert done["status"] == "succeeded", done["error"]
        assert (srv.dir / "backups" / "debug-moved.log").read_text() == "PHP Warning: x"
        assert not (srv.dir / "wp" / "wp-content" / "debug.log").exists()


async def test_production_sites_need_the_typed_site_name(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web, environment="production")
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == task["id"])
        assert approval["details"]["production"] and approval["details"]["confirm"] == "Site One"
        r = await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True, "confirm": "site one"})
        assert r.status_code == 400  # exact name required
        done = await approve(admin, make_worker, task, confirm="Site One")
        assert done["status"] == "succeeded", done["error"]


async def test_a_test_site_on_a_production_server_counts_as_production(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web, environment="test", server_env="production")
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == task["id"])
        assert approval["details"]["production"] is True and approval["details"]["environment"] == "production"


async def test_only_admins_link_sites_to_servers(admin, app_client, web):
    site = (await admin.post("/api/sites", json={"name": "S", "url": "http://s.test"})).json()["site"]
    async with session_scope() as s:
        s.add(User(email="op@test.local", password_hash=hash_password("operator-password"), role="operator"))
    await app_client.post("/api/auth/logout")
    await app_client.post("/api/auth/login", json={"email": "op@test.local", "password": "operator-password"})
    for body in ({"server_site_key": "x"}, {"environment": "test"}, {"unlink_server": True}):
        assert (await app_client.patch(f"/api/sites/{site['id']}", json=body)).status_code == 403
    assert (await app_client.patch(f"/api/sites/{site['id']}", json={"notes": "fine"})).status_code == 200


async def test_security_report_includes_the_server_side_check(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        put(srv.dir / "option-siteurl", "http://evil.example")
        scan = await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site["id"]})
        assert "Server-side check done" in scan["summary"]
        assert "site_url_mismatch" in {f["code"] for f in scan["result"]["findings"]}
        await run_task(admin, make_worker, "wordpress_security", "security_report", {"site_id": site["id"]})
    async with session_scope() as s:
        report = await s.scalar(select(Artifact).where(Artifact.kind == "security_report"))
    assert "### Server-side checks (WP-CLI on srv1" in report.content
    assert "repair_restore_site_url" in report.content and "**AT RISK**" in report.content


async def test_a_repair_that_fails_part_way_is_checked_and_reported_loudly(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        put(srv.dir / "update-fails", "1")
        task = await run_task(admin, make_worker, "wordpress_security", "repair_update_plugin", {"site_id": site["id"], "slug": "akismet"})
        done = await approve(admin, make_worker, task)
        assert done["status"] == "failed" and "Download failed" in done["error"]
        failed = next(e for e in (await admin.get("/api/events")).json()["events"] if e["kind"] == "repair.failed")
        assert failed["severity"] == "critical" and "it responds: up" in failed["title"]
        assert any(n.startswith("database backup:") for n in failed["detail"]["notes"])


async def test_a_plugin_update_is_confirmed_and_a_refused_reinstall_changes_nothing(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_update_plugin",
                                                                {"site_id": site["id"], "slug": "akismet"}))
        assert done["status"] == "succeeded" and "now version 5.4" in done["summary"]
        put(srv.dir / "update-uri-evil-cloak", "https://updates.example.com/evil-cloak")
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_reinstall_plugin",
                                                                {"site_id": site["id"], "slug": "evil-cloak"}))
        assert done["status"] == "failed" and "not a WordPress.org plugin" in done["error"]
        assert not any(e["kind"] == "repair.failed" for e in (await admin.get("/api/events")).json()["events"])
        assert not any("plugin install" in c for c in srv.calls("wp"))


async def test_a_diagnosis_keeps_what_it_could_read_and_notices_new_must_use_plugins(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()  # baseline (includes the first server-side check)
        put(srv.dir / "users-broken", "1")
        put(srv.dir / "mu-plugin", "wp-cache-helper")
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        codes = {f["code"]: f for f in diag["result"]["findings"]}
        assert codes["server_check_incomplete"]["severity"] == "warning" and "admins" in codes["server_check_incomplete"]["title"]
        assert codes["new_mu_plugin"]["severity"] == "critical" and "wp-cache-helper" in codes["new_mu_plugin"]["title"]
        assert "plugin_update_available" in codes  # the parts that could be read still count
        stored = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]
        assert [a["user_login"] for a in stored["last_diagnosis"]["admins"]] == ["owner"]  # kept from last time


async def test_a_change_whose_answer_never_came_is_recorded_as_outcome_unknown(admin, web):
    import asyncio
    from types import SimpleNamespace

    from app.config import get_settings
    from app.models import Server
    from app.ssh.client import GateTimeout
    from app.ssh.gateway import GateSession, SSHGateway

    server = (await admin.post("/api/servers", json={"name": "srv9", "host": "srv9.example", "username": "ccagent"})).json()["server"]
    async with session_scope() as s:
        row = await s.get(Server, server["id"])
    ctx = SimpleNamespace(approved_by="admin@test.local", requested_by="admin@test.local", task_id=None, agent_name="server_management",
                          time_left=lambda: None, log=lambda *a, **k: None)
    for failure, reason in ((GateTimeout("no answer within 1 seconds; the operation may still be running"), "may still be running"),
                            (asyncio.CancelledError(), "cancelled before the server answered")):
        session = GateSession(SSHGateway(get_settings()), ctx, row)

        async def request(command, timeout, failure=failure):
            raise failure

        session._conn = SimpleNamespace(request=request)
        try:
            await session.run("wp_cache_flush", site="site-one")
        except (GateTimeout, asyncio.CancelledError):
            pass
        async with session_scope() as s:
            audit = (await s.scalars(select(ServerCommand).order_by(ServerCommand.id.desc()))).first()
        assert audit.finished_at and audit.ok is False and audit.error.startswith("outcome unknown") and reason in audit.error
    events = [e for e in (await admin.get("/api/events")).json()["events"] if e["kind"] == "server.change_outcome_unknown"]
    assert len(events) == 2 and all(e["severity"] == "critical" for e in events)


async def test_a_change_is_not_started_without_time_to_finish_it(admin):
    from types import SimpleNamespace

    from app.config import get_settings
    from app.models import Server
    from app.ssh.client import SSHError
    from app.ssh.gateway import GateSession, SSHGateway

    server = (await admin.post("/api/servers", json={"name": "srv8", "host": "srv8.example", "username": "ccagent"})).json()["server"]
    async with session_scope() as s:
        row = await s.get(Server, server["id"])
    ctx = SimpleNamespace(approved_by="admin@test.local", requested_by="", task_id=None, agent_name="", time_left=lambda: 30.0,
                          log=lambda *a, **k: None)
    session = GateSession(SSHGateway(get_settings()), ctx, row)
    session._conn = SimpleNamespace()
    try:
        await session.run("wp_db_export", site="site-one")
        raise AssertionError("should have refused")
    except SSHError as exc:
        assert "not enough time left" in str(exc)
    async with session_scope() as s:
        assert (await s.scalars(select(ServerCommand))).all() == []


async def test_an_interrupted_repair_is_reported_when_the_worker_comes_back(admin, make_worker):
    from datetime import UTC, datetime, timedelta

    from app.models import Server, Task

    server = (await admin.post("/api/servers", json={"name": "srv7", "host": "srv7.example", "username": "ccagent"})).json()["server"]
    async with session_scope() as s:
        task = Task(agent="server_management", capability="restart_service", params={"server_id": server["id"], "service": "httpd"},
                    risk="change", status="running", heartbeat_at=datetime.now(UTC) - timedelta(hours=1), locked_by="gone")
        s.add(task)
        await s.flush()
        s.add(ServerCommand(server_id=server["id"], task_id=task.id, op="service_restart", args={"service": "httpd"}, kind="change"))
        task_id = task.id
    await make_worker().reap_stale()
    async with session_scope() as s:
        audit = await s.scalar(select(ServerCommand).where(ServerCommand.task_id == task_id))
        assert (await s.get(Task, task_id)).status == "failed"
    assert audit.finished_at and audit.error.startswith("outcome unknown")
    event = next(e for e in (await admin.get("/api/events")).json()["events"] if e["kind"] == "task.failed")
    assert event["severity"] == "critical" and "service_restart may not have finished" in event["title"]
    assert Server  # imported for the model registry
