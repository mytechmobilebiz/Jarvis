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
        web.add("site.test", lambda r: httpx.Response(500, text="<html>Fatal error</html>"))  # the site breaks after the change
        task = await run_task(admin, make_worker, "wordpress_security", "repair_deactivate_plugin", {"site_id": site["id"], "slug": "evil-cloak"})
        done = await approve(admin, make_worker, task)
        assert done["status"] == "succeeded"
        assert (srv.dir / "plugin-evil-cloak").read_text().strip() == "inactive"
        assert {f["code"] for f in done["result"]["findings"]} == {"site_down_after_repair"}
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
