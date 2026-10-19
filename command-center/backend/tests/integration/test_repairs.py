"""WP-CLI diagnosis and approval-based repairs through cc-gate (fake WP-CLI behind the real gate)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Artifact, ServerCommand, Site, User
from app.security import hash_password

from ..fakes import FakeWeb, wordpress_site
from ..sshfake import FakeGateServer
from .test_agents import run_task
from .test_ssh import add_server, scan, ssh_key


async def gate_hello(admin, web: FakeWeb, server_id: int) -> None:
    """Test connection, as an admin does before linking a site: the server's cc-gate reports its site keys and their addresses."""
    from app.config import get_settings
    from app.llm import NullProvider
    from app.worker.runner import Worker
    from app.workflow import N8nEngine

    from ..fakes import FakeN8n

    r = await admin.post("/api/agents/server_management/tasks", json={"capability": "test_connection", "params": {"server_id": server_id}})
    assert r.status_code == 201, r.text
    settings = get_settings()
    await Worker(settings, llm=NullProvider(), engine=N8nEngine(settings, transport=FakeN8n().transport), http_transport=web.transport,
                 worker_id="test-worker").run_until_idle()
    assert (await admin.get(f"/api/tasks/{r.json()['task']['id']}")).json()["task"]["status"] == "succeeded"


async def linked_site(admin, srv: FakeGateServer, web: FakeWeb, *, environment: str = "test", server_env: str = "test") -> tuple[dict, dict]:
    server = await add_server(admin, srv, environment=server_env)
    await gate_hello(admin, web, server["id"])
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


def wp_prints(srv: FakeGateServer, command: str, output: str) -> None:
    """Make the fake WP-CLI print output (no single quotes) for command, e.g. "plugin list", as the site's own code could."""
    wp = srv.dir / "wp-cli"
    script = wp.read_text()
    assert f'  "{command}")\n' in script
    wp.write_text(script.replace(f'  "{command}")\n', f"  \"{command}\") echo '{output}'; exit 0 ;;\n  \"{command} (real)\")\n", 1))


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
        assert approval["details"]["target"] == (f"site Site One (http://site.test) on server srv1 (ccagent@127.0.0.1:{srv.port}), "
                                                 "cc-gate site key site-one (http://site.test)") and not approval["details"]["production"]
        assert approval["details"]["expected"]["home"] == "http://site.test"  # the exact address the repair writes
        done = await approve(admin, make_worker, task)
        assert done["status"] == "succeeded", done["error"]
        assert done["result"]["data"]["changed"] is True
        assert "database backup:" in done["summary"] and "was home http://evil.example" in done["summary"]
        assert (srv.dir / "option-home").read_text() == "http://site.test"
        # Check, back up, re-check just before changing, change, verify.
        assert await ops_for(done["id"]) == [("hello", "read"), ("wp_site_urls", "read"), ("wp_db_export", "change"), ("wp_site_urls", "read"),
                                              ("wp_set_site_urls", "change"), ("wp_site_urls", "read")]
        assert any(e["kind"] == "repair.completed" for e in (await admin.get("/api/events")).json()["events"])

        again = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_restore_site_url",
                                                                 {"site_id": site["id"]}))
        assert again["result"]["data"]["changed"] is False and "nothing was changed" in again["summary"]
        assert await ops_for(again["id"]) == [("hello", "read"), ("wp_site_urls", "read")]  # no backup, no change when nothing is wrong


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
        await make_worker().run_until_idle()  # the baseline scan first (two at once: the second skips the server part)
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
        await make_worker().run_until_idle()  # the baseline scan: the server-side check the approval takes the versions from
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
        await make_worker().run_until_idle()  # the baseline scan: the server-side check the approval takes the versions from
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


async def test_a_repair_checks_its_target_again_after_taking_the_site_lock(admin, web, make_worker, monkeypatch):
    from app.agents import wp_server
    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web, environment="staging")
        await make_worker().run_until_idle()
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        real = wp_server.lock_site

        async def lock_then_promote(ctx, row):
            # The site was loaded (and the worker's check passed); it becomes production before the gate is opened.
            await real(ctx, row)
            async with session_scope() as s:
                (await s.get(Site, site["id"])).environment = "production"

        monkeypatch.setattr(wp_server, "lock_site", lock_then_promote)
        done = await approve(admin, make_worker, task)  # approved as staging: no typed name
        assert done["status"] == "failed" and "became a production target" in done["error"]
        assert await ops_for(done["id"]) == []  # the gate was never opened
        assert not any("cache flush" in c for c in srv.calls("wp"))


async def test_a_site_relinked_after_the_worker_check_sends_nothing_anywhere(admin, web, make_worker, monkeypatch):
    from app import tasks as task_service
    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv_a, FakeGateServer(key["openssh"]) as srv_b:
        site, _ = await linked_site(admin, srv_a, web, environment="staging")
        r = await admin.post("/api/servers", json={"name": "prod-b", "host": "127.0.0.1", "port": srv_b.port, "username": "ccagent",
                                                   "environment": "production"})
        b = r.json()["server"]
        scanned = (await scan(admin, b["id"])).json()
        assert (await admin.post(f"/api/servers/{b['id']}/host-key/trust", json={"fingerprint": scanned["fingerprint"]})).status_code == 200
        await make_worker().run_until_idle()
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        real = task_service.current_target

        async def check_then_relink(session, approval, params, *, lock=False):
            fresh = await real(session, approval, params, lock=lock)
            async with session_scope() as s:
                (await s.get(Site, site["id"])).server_id = b["id"]
            return fresh

        approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == task["id"])
        assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})).status_code == 200
        monkeypatch.setattr(task_service, "current_target", check_then_relink)
        await make_worker().run_until_idle()
        done = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
        assert done["status"] == "failed" and "target changed" in done["error"]
        assert srv_b.gate_log() == [] and not any(e["op"].startswith("wp_cache") for e in srv_a.gate_log())


async def test_a_busy_install_sends_tasks_back_to_the_queue(admin, web, make_worker, settings):
    import asyncio
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.agents.wp_server import lock_site
    from app.db import sessionmaker
    from app.models import Task, TaskLog

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, server = await linked_site(admin, srv, web)
        # A second row for the same install (www and non-www, say): it shares the lock.
        web.add("www.site.test", wordpress_site("http://www.site.test", exposed=False))
        other = (await admin.post("/api/sites", json={"name": "Site One www", "url": "http://www.site.test", "environment": "test"})).json()["site"]
        assert (await admin.patch(f"/api/sites/{other['id']}", json={"server_id": server["id"], "server_site_key": "site-one"})).status_code == 200
        await make_worker().run_until_idle()
        worker = make_worker(settings=settings.model_copy(update={"worker_concurrency": 1}))
        async with sessionmaker()() as holder:  # a repair of "Site One" is running
            await lock_site(SimpleNamespace(session=holder), SimpleNamespace(server_id=server["id"], server_site_key="site-one", name="Site One"))
            diag = (await admin.post("/api/agents/wordpress_security/tasks",
                                     json={"capability": "wp_diagnose", "params": {"site_id": other["id"]}})).json()["task"]
            scan = (await admin.post("/api/agents/wordpress_security/tasks",
                                     json={"capability": "security_scan", "params": {"site_id": site["id"]}})).json()["task"]
            check = (await admin.post(f"/api/sites/{site['id']}/check", json={"deep": False})).json()["task"]
            await asyncio.wait_for(worker.run_until_idle(), 60)  # nothing waits for the lock, so one slot runs them all
            async with session_scope() as s:
                waiting = await s.get(Task, diag["id"])
                logs = [r.message for r in (await s.scalars(select(TaskLog).where(TaskLog.task_id == diag["id"]))).all()]
            assert waiting.status == "queued" and waiting.attempts == 0 and waiting.locked_by == "" and waiting.started_at is None
            assert waiting.run_after > datetime.now(UTC) + timedelta(seconds=20)
            assert any("another repair or server-side check is running for Site One www" in m for m in logs)
            scanned = (await admin.get(f"/api/tasks/{scan['id']}")).json()["task"]
            assert scanned["status"] == "succeeded"
            failed = next(f for f in scanned["result"]["findings"] if f["code"] == "server_check_failed")
            assert "another repair or server-side check is running" in failed["title"]
            assert (await admin.get(f"/api/tasks/{check['id']}")).json()["task"]["status"] == "succeeded"
            await holder.rollback()
        assert not any(e["kind"] == "task.failed" for e in (await admin.get("/api/events")).json()["events"])
        async with session_scope() as s:
            (await s.get(Task, diag["id"])).run_after = datetime.now(UTC)
        await worker.run_until_idle()
        assert (await admin.get(f"/api/tasks/{diag['id']}")).json()["task"]["status"] == "succeeded"


async def test_a_diagnosis_that_could_not_run_is_reported_and_keeps_the_baseline(admin, web, make_worker):
    from datetime import UTC, datetime, timedelta

    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, server = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        baseline = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        await admin.patch(f"/api/servers/{server['id']}", json={"enabled": False})
        scan = await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site["id"]})
        assert "server_check_failed" in {f["code"] for f in scan["result"]["findings"]}
        stored = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert {k: v for k, v in stored.items() if k != "last_failed_attempt"} == baseline  # the baseline is untouched
        assert "srv1 is disabled" in stored["last_failed_attempt"]["error"]
        await run_task(admin, make_worker, "wordpress_security", "security_report", {"site_id": site["id"]})
        async with session_scope() as s:
            report = (await s.scalars(select(Artifact).where(Artifact.kind == "security_report"))).all()[-1].content
        assert "_The latest server-side check (" in report and "failed: server srv1 is disabled; these results are from" in report
        assert "not recent" not in report.split("### Server-side checks")[1]

        async with session_scope() as s:  # three days later
            row = await s.get(Site, site["id"])
            row.last_diagnosis = {**row.last_diagnosis, "at": (datetime.now(UTC) - timedelta(days=3)).isoformat()}
        await run_task(admin, make_worker, "wordpress_security", "security_report", {"site_id": site["id"]})
        async with session_scope() as s:
            report = (await s.scalars(select(Artifact).where(Artifact.kind == "security_report").order_by(Artifact.id))).all()[-1].content
        assert "_These server-side results are not recent" in report

        await admin.patch(f"/api/servers/{server['id']}", json={"enabled": True})
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert "last_failed_attempt" not in (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]


async def test_the_report_says_which_server_side_parts_could_not_be_read(admin, web, make_worker):
    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        put(srv.dir / "users-broken", "1")
        await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
    async with session_scope() as s:  # as if home/siteurl and the plugin list could not be read either
        row = await s.get(Site, site["id"])
        diag = {k: v for k, v in row.last_diagnosis.items() if k not in ("site_urls", "plugins")}
        row.last_diagnosis = {**diag, "errors": {**diag["errors"], "site_urls": "database down", "plugins": "database down"}}
    await run_task(admin, make_worker, "wordpress_security", "security_report", {"site_id": site["id"]})
    async with session_scope() as s:
        report = (await s.scalar(select(Artifact).where(Artifact.kind == "security_report"))).content
    assert "- home/siteurl: could not be checked" in report and "None" not in report
    assert "- Plugins: could not be read" in report
    assert "- Administrator accounts: 1 (could not be read; showing the previous check)" in report


async def test_parts_a_diagnosis_never_read_are_not_a_baseline(admin, web, make_worker):
    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        put(srv.dir / "users-broken", "1")
        await make_worker().run_until_idle()  # the first diagnosis cannot read the administrators
        first = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert "admins" in first["errors"] and "admins" not in first and "super_admins" not in first
        (srv.dir / "users-broken").unlink()
        put(srv.dir / "super-admins", "owner")
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert "new_admin_account" not in {f["code"] for f in diag["result"]["findings"]}

        async with session_scope() as s:  # an older diagnosis without these parts (e.g. from before super admins were read)
            row = await s.get(Site, site["id"])
            row.last_diagnosis = {k: v for k, v in row.last_diagnosis.items() if k not in ("plugins", "super_admins")}
        put(srv.dir / "super-admins", "owner\nnetwork-admin")
        put(srv.dir / "mu-plugin", "host-mu")
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert not {"new_admin_account", "new_mu_plugin"} & {f["code"] for f in diag["result"]["findings"]}

        put(srv.dir / "super-admins", "owner\nnetwork-admin\nintruder")  # now there is a full baseline to compare with
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        new = next(f for f in diag["result"]["findings"] if f["code"] == "new_admin_account")
        assert new["severity"] == "critical" and new["title"].endswith(": intruder")


async def test_failures_before_anything_reached_the_site_are_plain_failures(admin, web, make_worker, settings, monkeypatch):
    from app.ssh.gateway import GateFailed, GateSession

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()

        # debug.log is a link: the helper would refuse to move it, so the dry run already says there is nothing to do.
        helper = srv.dir / "cc-site-helper"
        helper.write_text('#!/bin/sh\ncase "$1" in\n'
                          '  debug_log_status) echo \'{"ok": true, "result": {"exists": true, "type": "symlink", "bytes": 0}}\' ;;\n'
                          '  *) echo \'{"ok": false, "error": "wp-content/debug.log is not a regular file (symlink); not touching it"}\'; exit 1 ;;\n'
                          'esac\n')
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_quarantine_debug_log",
                                                                {"site_id": site["id"]}))
        assert done["status"] == "succeeded" and done["result"]["data"]["changed"] is False
        assert "debug.log is a symlink, not a file" in done["summary"]
        assert await ops_for(done["id"]) == [("hello", "read"), ("wp_debug_log_status", "read")]

        # Not enough time left for the change: it is never sent.
        slow = make_worker(settings=settings.model_copy(update={"ssh_command_timeout_seconds": 4000}))
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        approval = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["task_id"] == task["id"])
        assert (await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})).status_code == 200
        await slow.run_until_idle()
        done = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
        assert done["status"] == "failed" and "nothing was sent" in done["error"]
        assert await ops_for(done["id"]) == [("hello", "read")]  # only the read that checks the gate's address for the key

        # Backed up, then a read fails before the change: the backup alone changed nothing on the site.
        real, lists = GateSession.run, []

        async def run(self, op, **args):
            if op == "wp_plugin_list":
                lists.append(op)
                if len(lists) == 2:
                    raise GateFailed("wp_plugin_list failed on srv1: Error establishing a database connection.")
            return await real(self, op, **args)

        monkeypatch.setattr(GateSession, "run", run)
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_deactivate_plugin",
                                                                {"site_id": site["id"], "slug": "evil-cloak"}))
        assert done["status"] == "failed" and "database connection" in done["error"]
        assert await ops_for(done["id"]) == [("hello", "read"), ("wp_plugin_list", "read"), ("wp_db_export", "change")]
    assert not any(e["kind"] == "repair.failed" for e in (await admin.get("/api/events")).json()["events"])


async def test_wordpress_in_its_own_folder_is_a_warning_and_its_siteurl_is_left_alone(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        put(srv.dir / "option-siteurl", "http://site.test/wp")  # the site's path ends in /wp, and no "siteurl" is configured
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        codes = {f["code"]: f["severity"] for f in diag["result"]["findings"]}
        assert codes.get("subdirectory_install_suspected") == "warning" and "site_url_mismatch" not in codes
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_restore_site_url",
                                                                {"site_id": site["id"]}))
        assert done["status"] == "failed" and 'set "siteurl" for this site' in done["error"]
        assert await ops_for(done["id"]) == [("hello", "read"), ("wp_site_urls", "read")]  # no backup, no change
        assert (srv.dir / "option-siteurl").read_text() == "http://site.test/wp"
        await run_task(admin, make_worker, "wordpress_security", "security_report", {"site_id": site["id"]})
    async with session_scope() as s:
        report = (await s.scalar(select(Artifact).where(Artifact.kind == "security_report"))).content
    assert "| warning | WordPress seems to live in its own folder" in report and "WordPress in its own folder?" in report


async def test_lists_of_the_wrong_shape_count_as_not_read_and_hide_nothing_else(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()  # a clean baseline
        baseline = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        wp_prints(srv, "plugin list", "[1]")
        wp_prints(srv, "user list", '[{"ID": 2}]')
        put(srv.dir / "option-siteurl", "http://evil.example")  # and the site is hijacked meanwhile
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        codes = {f["code"]: f for f in diag["result"]["findings"]}
        assert codes["site_url_mismatch"]["severity"] == "critical" and "new_admin_account" not in codes
        assert codes["server_check_incomplete"]["title"].endswith("could not check: admins, plugins")
        stored = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert "unexpected answer" in stored["errors"]["plugins"] and "unexpected answer" in stored["errors"]["admins"]
        assert stored["plugins"] == baseline["plugins"] and stored["admins"] == baseline["admins"]  # the baseline is kept


async def test_a_diagnosis_that_cannot_be_evaluated_is_recorded_and_a_bad_stored_baseline_is_dropped(admin, web, make_worker, monkeypatch):
    from app.agents import wp_server
    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        async with session_scope() as s:  # an administrator list an older version stored unchecked
            row = await s.get(Site, site["id"])
            row.last_diagnosis = {**row.last_diagnosis, "admins": [1]}
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert "new_admin_account" not in {f["code"] for f in diag["result"]["findings"]}  # no baseline: this one becomes it
        stored = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert [a["user_login"] for a in stored["admins"]] == ["owner"]

        def unreadable(diag, previous):
            raise RuntimeError("the answers made no sense")

        monkeypatch.setattr(wp_server, "diagnosis_findings", unreadable)
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "failed"
        failed = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert failed["last_failed_attempt"]["error"] == "the answers made no sense"
        assert {k: v for k, v in failed.items() if k != "last_failed_attempt"} == stored


async def test_plugin_entries_of_the_wrong_shape_never_break_the_report(admin, web, make_worker):
    from app.models import Site

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        wp_prints(srv, "plugin list", '[{"name": 1, "status": "active", "update": "available"}]')
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert "plugins" in diag["result"]["data"]["diagnosis"]["errors"]
    async with session_scope() as s:  # as an older version stored them, unchecked
        row = await s.get(Site, site["id"])
        row.last_diagnosis = {**row.last_diagnosis, "errors": {}, "plugins": [{"name": 1, "status": "active", "update": "available"}, 2]}
    report = await run_task(admin, make_worker, "wordpress_security", "security_report", {})
    assert report["status"] == "succeeded", report["error"]
    async with session_scope() as s:
        content = (await s.scalar(select(Artifact).where(Artifact.kind == "security_report"))).content
    assert "- Plugins: 2, updates available: 1" in content


async def test_a_server_side_check_that_runs_out_of_time_says_so_in_the_scan(admin, web, make_worker, monkeypatch):
    from app.agents.base import AgentContext

    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        wp = srv.dir / "wp-cli"
        wp.write_text(wp.read_text().replace('  "core verify-checksums")', '  "core verify-checksums") sleep 6;'))
        monkeypatch.setattr(AgentContext, "time_left", lambda self: 33.0)  # 3 s for the server part
        scan = await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site["id"]})
        assert scan["status"] == "succeeded", scan["error"]
        failed = next(f for f in scan["result"]["findings"] if f["code"] == "server_check_failed")
        assert failed["title"] == "Server-side check failed: the check ran out of time"
        stored = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert stored["last_failed_attempt"]["error"] == "the check ran out of time"


async def test_a_new_user_with_administrator_powers_under_another_role_is_noticed(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()  # baseline: only "owner" (capability holders are the same user)
        put(srv.dir / "capability-users", '[{"ID":1,"user_login":"owner","user_registered":""},'
                                          '{"ID":7,"user_login":"shop-manager-x","user_registered":""}]')
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        codes = {f["code"]: f for f in diag["result"]["findings"]}
        assert codes["new_admin_account"]["severity"] == "critical" and "shop-manager-x" in codes["new_admin_account"]["title"]
        stored = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["last_diagnosis"]
        assert [u["user_login"] for u in stored["capability_admins"]] == ["shop-manager-x"]  # owner is already an admin


async def test_capability_holders_first_seen_after_a_gate_upgrade_are_a_baseline_not_an_alarm(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        async with session_scope() as s:  # a diagnosis from a gate that did not report capability holders yet
            row = await s.get(Site, site["id"])
            row.last_diagnosis = {k: v for k, v in row.last_diagnosis.items() if k != "capability_admins"}
        put(srv.dir / "capability-users", '[{"ID":9,"user_login":"long-time-editor","user_registered":""}]')
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert "new_admin_account" not in {f["code"] for f in diag["result"]["findings"]}


async def test_a_hijacked_home_on_a_wordpress_in_its_own_folder_is_restored_and_siteurl_kept(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        put(srv.dir / "option-siteurl", "http://site.test/wp")
        put(srv.dir / "option-home", "http://evil.example")
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_restore_site_url",
                                                                {"site_id": site["id"]}))
        assert done["status"] == "succeeded", done["error"]
        assert (srv.dir / "option-home").read_text() == "http://site.test"
        assert (srv.dir / "option-siteurl").read_text() == "http://site.test/wp" and "siteurl kept" in done["summary"]
