"""0.5.0 approval exactness (audit findings C3-01 to C3-08, C2-12): every approval names its exact target and value,
binds it, and refuses when it changed; switching an agent off stops its changes; trust changes are deliberate."""

from __future__ import annotations

import asyncssh
import pytest
from sqlalchemy import select

from app import tasks as task_service
from app.agents.wp_server import same_host, site_host
from app.approvals import excerpt
from app.db import session_scope
from app.models import AgentState, Approval, Artifact, AuditLog, Lead, OutboxEntry, Schedule, ServerCommand, Site, SupportTicket, Task, User
from app.security import hash_password

from ..fakes import FakeLLM, wordpress_site
from ..sshfake import FakeGateServer
from .test_agents import run_task
from .test_repairs import approve, gate_hello, linked_site, ops_for, put, wp_prints
from .test_ssh import add_server, server_request, ssh_key


async def approval_for(admin, task_id: int) -> dict:
    return next(a for a in (await admin.get("/api/approvals?status=all")).json()["approvals"] if a["task_id"] == task_id)


async def decide(client, approval: dict, *, approve: bool = True, confirm: str = ""):
    return await client.post(f"/api/approvals/{approval['id']}/decision", json={"approve": approve, "confirm": confirm})


async def request_task(admin, agent: str, capability: str, params: dict):
    return await admin.post(f"/api/agents/{agent}/tasks", json={"capability": capability, "params": params})


async def events_of(admin, kind: str) -> list[dict]:
    return [e for e in (await admin.get("/api/events")).json()["events"] if e["kind"] == kind]


async def as_operator(app_client) -> None:
    async with session_scope() as s:
        if await s.scalar(select(User).where(User.email == "op@test.local")) is None:
            s.add(User(email="op@test.local", password_hash=hash_password("operator-password"), role="operator"))
    await app_client.post("/api/auth/logout")
    assert (await app_client.post("/api/auth/login", json={"email": "op@test.local", "password": "operator-password"})).status_code == 200


# ----------------------------------------------------------------------------------------------- pure helpers


def test_site_hosts_compare_without_www_case_port_or_final_dot():
    assert site_host("https://WWW.Acme.example:8443/shop/") == "acme.example"
    assert same_host("http://site.test", "https://www.site.test.") and same_host("http://site.test/", "http://SITE.test/wp")
    assert not same_host("http://acme.example", "http://acme.example.evil.test") and not same_host("http://a.test", "http://b.test")
    assert not same_host("", "")  # no host is never a match


def test_draft_excerpts_are_plain_text():
    html = Artifact(content="<html><head><style>h1{}</style></head><body><h1>Proposal</h1><p>Dear &amp; welcome</p></body></html>",
                    content_type="text/html")
    assert excerpt(html) == "Proposal Dear & welcome"
    assert excerpt(Artifact(content="x" * 400, content_type="text/plain")).endswith("…")


# --------------------------------------------------------------------------------- C3-08: who asked for it


async def test_approvals_say_who_or_what_asked_for_the_change(admin, app_client, make_worker, service_headers, settings):
    server, approval = await server_request(admin)
    assert approval["details"]["requested"] == {"source": "dashboard", "by": "admin@test.local", "request_id": None, "request_source": None,
                                                "requested_by": None, "message": None, "schedule": None, "retry_of": None}
    # From n8n (the service token) through the AI planner.
    llm = FakeLLM(json_responses=[{"reply": "ok", "steps": [
        {"action": "server_management.restart_service", "params_json": f'{{"server_id": {server["id"]}, "service": "httpd"}}', "rationale": "asked"},
    ]}], text_response="summary")
    message = "Please restart httpd on srv2 " + "x" * 300
    r = await app_client.post("/api/supervisor/requests", json={"message": message}, headers=service_headers)
    assert r.status_code == 202, r.text
    req_id = r.json()["request"]["id"]
    await make_worker(llm=llm).run_until_idle()
    planned = next(a for a in (await admin.get("/api/approvals")).json()["approvals"] if a["id"] != approval["id"])
    assert planned["details"]["requested"] == {
        "source": "supervisor", "by": f"supervisor (request {req_id})", "request_id": req_id, "request_source": "n8n",
        "requested_by": "service:n8n", "message": message[:200], "schedule": None, "retry_of": None}
    # A schedule, and a retry of a cancelled change.
    async with session_scope() as s:
        schedule = Schedule(name="nightly-restart", agent="server_management", capability="restart_service", params={}, cron="0 3 * * *")
        s.add(schedule)
        await s.flush()
        task = await task_service.create_task(s, settings, agent="server_management", capability="restart_service",
                                              params={"server_id": server["id"], "service": "httpd"}, source="schedule",
                                              created_by="schedule:nightly-restart", schedule_id=schedule.id)
        scheduled = await s.scalar(select(Approval).where(Approval.task_id == task.id))
        assert scheduled.details["requested"]["schedule"] == "nightly-restart"
        assert scheduled.details["requested"]["source"] == "schedule"
    assert (await admin.post(f"/api/tasks/{approval['task_id']}/cancel")).status_code == 200
    retried = (await admin.post(f"/api/tasks/{approval['task_id']}/retry")).json()["task"]
    again = await approval_for(admin, retried["id"])
    assert again["details"]["requested"]["retry_of"] == approval["task_id"] and again["details"]["requested"]["source"] == "retry"


# ---------------------------------------------------------------------- C3-02: email names recipient and draft


async def ticket_with_draft(admin, make_worker) -> dict:
    r = await admin.post("/api/support/tickets", json={"customer_name": "Dana Client", "customer_email": "dana@client.example",
                                                       "subject": "Website down", "body": "Our site is not loading."})
    ticket = r.json()["ticket"]
    await make_worker(llm=FakeLLM(text_response="Hi Dana,\n\nWe're on it.\n\nInfinite Marketing Support")).run_until_idle()
    return next(t for t in (await admin.get("/api/support/tickets")).json()["tickets"] if t["id"] == ticket["id"])


async def test_an_email_approval_names_the_recipient_subject_and_draft_and_needs_a_draft(admin, make_worker):
    r = await admin.post("/api/support/tickets", json={"customer_name": "Dana", "customer_email": "dana@client.example",
                                                       "subject": "Website down", "body": "Our site is not loading."})
    ticket_id = r.json()["ticket"]["id"]
    r = await request_task(admin, "communications", "send_reply", {"ticket_id": ticket_id})  # before the reply is drafted
    assert r.status_code == 400 and "no reply draft for ticket" in r.json()["detail"]
    assert (await admin.get("/api/approvals")).json()["approvals"] == []
    await make_worker(llm=FakeLLM(text_response="Hi Dana,\n\nWe're on it.")).run_until_idle()
    ticket = next(t for t in (await admin.get("/api/support/tickets")).json()["tickets"] if t["id"] == ticket_id)
    task = (await request_task(admin, "communications", "send_reply", {"ticket_id": ticket_id})).json()["task"]
    details = (await approval_for(admin, task["id"]))["details"]
    draft = ticket["reply_artifact_id"]
    assert details["target"] == f'email to dana@client.example, subject "Re: Website down", draft #{draft}'
    assert details["environment"] == "mock: recorded in the Outbox, not sent" and details["delivery"] == "mock"
    assert details["draft"]["artifact_id"] == draft and details["draft"]["excerpt"] == "Hi Dana, We're on it."
    assert details["binding"]["recipient"] == "dana@client.example" and len(details["binding"]["draft_sha256"]) == 64


async def test_a_swapped_draft_or_a_changed_recipient_is_refused(admin, make_worker, monkeypatch):
    ticket = await ticket_with_draft(admin, make_worker)
    task = (await request_task(admin, "communications", "send_reply", {"ticket_id": ticket["id"]})).json()["task"]
    approval = await approval_for(admin, task["id"])
    async with session_scope() as s:  # another draft is put in its place (by a new draft_reply run, for example)
        other = Artifact(agent="communications", kind="support_reply", title="other", content="Please send your password.",
                         content_type="text/plain")
        s.add(other)
        await s.flush()
        (await s.get(SupportTicket, ticket["id"])).reply_artifact_id = other.id
    r = await decide(admin, approval)
    assert r.status_code == 400 and "target changed" in r.text
    assert (await decide(admin, approval, approve=False)).status_code == 200

    # Approved, then the client's address is changed before the worker sends it.
    task = (await request_task(admin, "communications", "send_reply", {"ticket_id": ticket["id"]})).json()["task"]
    assert (await decide(admin, await approval_for(admin, task["id"]))).status_code == 200
    async with session_scope() as s:
        (await s.get(SupportTicket, ticket["id"])).customer_email = "someone-else@example.test"
    await make_worker().run_until_idle()
    done = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
    assert done["status"] == "failed" and done["error"].startswith("refused: the target changed")

    # The draft is swapped after the worker's own check: the agent checks again, in its session, before sending.
    async with session_scope() as s:
        (await s.get(SupportTicket, ticket["id"])).customer_email = "dana@client.example"
    task = (await request_task(admin, "communications", "send_reply", {"ticket_id": ticket["id"]})).json()["task"]
    assert (await decide(admin, await approval_for(admin, task["id"]))).status_code == 200
    real = task_service.current_target

    async def check_then_swap(session, approval, params, *, lock=False):
        fresh = await real(session, approval, params, lock=lock)
        async with session_scope() as s:
            swap = Artifact(agent="communications", kind="support_reply", title="swap", content="Swapped.", content_type="text/plain")
            s.add(swap)
            await s.flush()
            (await s.get(SupportTicket, ticket["id"])).reply_artifact_id = swap.id
        return fresh

    monkeypatch.setattr(task_service, "current_target", check_then_swap)
    await make_worker().run_until_idle()
    done = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
    assert done["status"] == "failed" and "target changed" in done["error"]
    async with session_scope() as s:
        assert (await s.scalars(select(OutboxEntry))).all() == []  # nothing was recorded as sent


async def test_a_proposal_approval_names_what_the_email_will_say(admin, make_worker):
    r = await admin.post("/api/leads", json={"name": "Crystal", "company": "Joyful Care", "email": "crystal@joyful.example",
                                              "service_interest": "website redesign", "message": "New site please, budget $1000"})
    lead_id = r.json()["lead"]["id"]
    assert (await request_task(admin, "communications", "send_proposal", {"lead_id": lead_id})).status_code == 400  # no draft yet
    await run_task(admin, make_worker, "communications", "draft_proposal", {"lead_id": lead_id})
    task = (await request_task(admin, "communications", "send_proposal", {"lead_id": lead_id})).json()["task"]
    approval = await approval_for(admin, task["id"])
    assert approval["details"]["target"].startswith('email to crystal@joyful.example, subject "Website Design Proposal for Joyful Care')
    assert "<" not in approval["details"]["draft"]["excerpt"] and "Joyful Care" in approval["details"]["draft"]["excerpt"]
    assert (await decide(admin, approval)).status_code == 200
    await make_worker().run_until_idle()
    entry = (await admin.get("/api/outbox")).json()["entries"][0]
    assert f'subject "{entry["subject"]}"' in approval["details"]["target"]  # exactly the subject that was approved
    async with session_scope() as s:  # a new name for the lead changes the greeting: an approval given before no longer applies
        (await s.get(Lead, lead_id)).name = "Somebody Else"
    task = (await request_task(admin, "communications", "send_proposal", {"lead_id": lead_id})).json()["task"]
    async with session_scope() as s:
        (await s.get(Lead, lead_id)).name = "Crystal"
    assert (await decide(admin, await approval_for(admin, task["id"]))).status_code == 400


# ------------------------------------------------------------------ C3-03: publishing names site and draft


async def blog_draft(admin, make_worker) -> int:
    await run_task(admin, make_worker, "seo_content", "draft_blog_post", {"topic": "why page speed matters"})
    async with session_scope() as s:
        return (await s.scalars(select(Artifact).where(Artifact.kind == "blog_draft").order_by(Artifact.id.desc()))).first().id


async def test_a_wordpress_publish_names_the_site_and_draft_and_production_needs_the_typed_name(admin, app_client, make_worker):
    draft = await blog_draft(admin, make_worker)
    site = (await admin.post("/api/sites", json={"name": "Client Blog", "url": "https://blog.example.test"})).json()["site"]
    params = {"artifact_id": draft, "destination": "wordpress", "site_id": site["id"]}
    r = await request_task(admin, "seo_content", "publish_draft", {**params, "artifact_id": 9999})
    assert r.status_code == 400 and "no blog draft #9999" in r.json()["detail"]
    r = await request_task(admin, "seo_content", "publish_draft", params)
    assert r.status_code == 400 and "has not been approved yet" in r.json()["detail"]
    assert (await admin.patch(f"/api/artifacts/{draft}", json={"status": "approved"})).status_code == 200
    r = await request_task(admin, "seo_content", "publish_draft", {**params, "site_id": 9999})
    assert r.status_code == 400 and "no monitored site with id 9999" in r.json()["detail"]
    task = (await request_task(admin, "seo_content", "publish_draft", params)).json()["task"]
    approval = await approval_for(admin, task["id"])
    details = approval["details"]
    assert details["target"].startswith(f"site Client Blog (https://blog.example.test): a draft post \"") and f"blog draft #{draft}" in details["target"]
    assert details["production"] is True and details["environment"] == "production" and details["confirm"] == "Client Blog"
    assert details["binding"]["url"] == "https://blog.example.test" and details["binding"]["artifact_id"] == draft
    assert "(PRODUCTION)" in approval["summary"]
    await as_operator(app_client)
    r = await decide(app_client, approval, confirm="Client Blog")
    assert r.status_code == 400 and "admin" in r.text
    from ..conftest import ADMIN

    await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert (await decide(app_client, approval)).status_code == 400  # no typed name
    assert (await decide(app_client, approval, confirm="Client Blog")).status_code == 200
    await make_worker().run_until_idle()
    entry = (await app_client.get("/api/outbox")).json()["entries"][0]
    assert entry["integration"] == "wordpress_publish" and entry["recipient"] == "https://blog.example.test"


async def test_a_draft_rejected_after_the_request_is_not_published(admin, make_worker):
    draft = await blog_draft(admin, make_worker)
    site = (await admin.post("/api/sites", json={"name": "Staging Blog", "url": "https://stage.example.test", "environment": "staging"})).json()["site"]
    await admin.patch(f"/api/artifacts/{draft}", json={"status": "approved"})
    task = (await request_task(admin, "seo_content", "publish_draft",
                               {"artifact_id": draft, "destination": "wordpress", "site_id": site["id"]})).json()["task"]
    approval = await approval_for(admin, task["id"])
    assert approval["details"]["production"] is False and approval["details"]["environment"] == "staging"
    await admin.patch(f"/api/artifacts/{draft}", json={"status": "rejected"})
    r = await decide(admin, approval)
    assert r.status_code == 400 and "was rejected" in r.text


async def test_a_social_publish_names_the_platform_and_account(admin, make_worker):
    await run_task(admin, make_worker, "social_marketing", "draft_social_posts", {"topic": "our new chatbot", "platforms": ["facebook"]})
    async with session_scope() as s:
        draft = await s.scalar(select(Artifact).where(Artifact.kind == "social_drafts"))
    assert (await request_task(admin, "social_marketing", "publish_draft", {"artifact_id": draft.id, "destination": "facebook"})).status_code == 400
    await admin.patch(f"/api/artifacts/{draft.id}", json={"status": "approved"})
    assert (await request_task(admin, "social_marketing", "publish_draft", {"artifact_id": draft.id, "destination": "myspace"})).status_code == 400
    task = (await request_task(admin, "social_marketing", "publish_draft", {"artifact_id": draft.id, "destination": "facebook"})).json()["task"]
    approval = await approval_for(admin, task["id"])
    details = approval["details"]
    assert details["target"] == f'facebook through Postonomous (no Postonomous account connected): social draft #{draft.id} "{draft.title}"'
    assert details["environment"] == "mock: recorded in the Outbox, not published" and details["binding"]["platform"] == "facebook"
    assert (await decide(admin, approval)).status_code == 200
    await make_worker().run_until_idle()
    assert (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]["status"] == "succeeded"


# -------------------------------------------------------------------- C3-04: switching an agent off stops it


async def test_switching_an_agent_off_cancels_its_waiting_and_approved_changes(admin, make_worker):
    server, pending = await server_request(admin)
    approved = (await request_task(admin, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})).json()["task"]
    assert (await decide(admin, await approval_for(admin, approved["id"]))).status_code == 200
    read = (await request_task(admin, "server_management", "test_connection", {"server_id": server["id"]})).json()["task"]
    r = await admin.patch("/api/agents/server_management", json={"enabled": False})
    assert r.status_code == 200 and sorted(r.json()["cancelled_tasks"]) == sorted([pending["task_id"], approved["id"]])
    for task_id in (pending["task_id"], approved["id"]):
        task = (await admin.get(f"/api/tasks/{task_id}")).json()["task"]
        assert task["status"] == "cancelled" and task["error"] == "cancelled by admin@test.local: the agent was switched off"
    assert (await approval_for(admin, pending["task_id"]))["decision_note"] == "task cancelled: the agent was switched off"
    async with session_scope() as s:
        entries = (await s.scalars(select(AuditLog).where(AuditLog.action.in_(("task.cancelled", "agent.disabled"))))).all()
    assert {(e.action, e.target_id) for e in entries} >= {("task.cancelled", str(pending["task_id"])), ("task.cancelled", str(approved["id"]))}
    assert next(e for e in entries if e.action == "agent.disabled").detail["cancelled_tasks"]
    # Read-only work already queued does not run either.
    await make_worker().run_until_idle()
    done = (await admin.get(f"/api/tasks/{read['id']}")).json()["task"]
    assert done["status"] == "failed" and done["error"] == "refused: the server_management agent is switched off"
    async with session_scope() as s:
        assert (await s.scalars(select(ServerCommand))).all() == []


async def test_an_agent_switched_off_meanwhile_is_not_approved_and_does_not_run(admin, make_worker):
    server, approval = await server_request(admin)
    approved = (await request_task(admin, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})).json()["task"]
    assert (await decide(admin, await approval_for(admin, approved["id"]))).status_code == 200
    async with session_scope() as s:  # switched off without withdrawing the tasks (as an older version did)
        s.add(AgentState(name="server_management", enabled=False))
    r = await decide(admin, approval)
    assert r.status_code == 400 and "switched off" in r.text
    assert (await decide(admin, approval, approve=False)).status_code == 200  # rejecting still works
    await make_worker().run_until_idle()
    done = (await admin.get(f"/api/tasks/{approved['id']}")).json()["task"]
    assert done["status"] == "failed" and "switched off" in done["error"]


# --------------------------------------------------------------- C3-05: replacing a pinned host key


async def test_replacing_a_pinned_host_key_needs_the_typed_name_and_voids_earlier_approvals(admin, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv, environment="staging")
        scan = (await admin.post(f"/api/servers/{server['id']}/host-key/scan")).json()
        assert scan["matches_pinned"] is True and scan["pinned_fingerprint"] == srv.fingerprint
        # The same key again is not a replacement.
        assert (await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"fingerprint": scan["fingerprint"]})).status_code == 200
        pending = (await request_task(admin, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})).json()["task"]
        approved = (await request_task(admin, "server_management", "restart_service", {"server_id": server["id"], "service": "httpd"})).json()["task"]
        assert (await decide(admin, await approval_for(admin, approved["id"]))).status_code == 200
        other = asyncssh.generate_private_key("ssh-ed25519")
        other_line = other.export_public_key("openssh").decode()
        for confirm in ("", "SRV1"):
            r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": other_line, "confirm": confirm})
            assert r.status_code == 400 and r.json()["detail"].startswith("confirm:") and "CHANGED" in r.json()["detail"]
        assert (await admin.get(f"/api/servers/{server['id']}")).json()["server"]["host_key_fingerprint"] == srv.fingerprint
        r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": other_line, "confirm": "srv1"})
        assert r.status_code == 200 and r.json()["server"]["host_key_fingerprint"] == other.get_fingerprint("sha256")
        replaced = await events_of(admin, "server.host_key_replaced")
        assert len(replaced) == 1 and replaced[0]["severity"] == "critical" and replaced[0]["detail"]["previous"] == srv.fingerprint
        # Approvals given for the machine with the old key no longer apply.
        r = await decide(admin, await approval_for(admin, pending["id"]))
        assert r.status_code == 400 and "target changed" in r.text
        await make_worker().run_until_idle()
        done = (await admin.get(f"/api/tasks/{approved['id']}")).json()["task"]
        assert done["status"] == "failed" and done["error"].startswith("refused: the target changed")
        assert srv.calls("systemctl") == []

        # Unpinning first does not turn a replacement into a first pin.
        assert (await admin.delete(f"/api/servers/{server['id']}/host-key")).status_code == 200
        r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": srv.host_key_line})
        assert r.status_code == 400 and r.json()["detail"].startswith("confirm:")
        r = await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": srv.host_key_line, "confirm": "srv1"})
        assert r.status_code == 200 and len(await events_of(admin, "server.host_key_replaced")) == 2


async def test_a_first_pin_needs_no_confirmation(admin):
    server = (await admin.post("/api/servers", json={"name": "fresh", "host": "fresh.example", "username": "ccagent"})).json()["server"]
    line = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode()
    assert (await admin.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": line})).status_code == 200
    assert await events_of(admin, "server.host_key_replaced") == []


# ------------------------------------------------------------ C3-06: leaving production is deliberate


async def test_taking_a_site_out_of_production_needs_its_typed_name(admin, app_client):
    site = (await admin.post("/api/sites", json={"name": "Shop", "url": "https://shop.example"})).json()["site"]
    for body in ({"environment": "staging"}, {"environment": "test", "confirm": "shop"}, {"environment": "test", "name": "Shop2", "confirm": "Shop2"}):
        r = await admin.patch(f"/api/sites/{site['id']}", json=body)
        assert r.status_code == 400 and r.json()["detail"].startswith("confirm:"), body
    assert (await admin.get(f"/api/sites/{site['id']}")).json()["site"]["environment"] == "production"
    r = await admin.patch(f"/api/sites/{site['id']}", json={"environment": "staging", "confirm": "Shop"})
    assert r.status_code == 200 and r.json()["site"]["environment"] == "staging"
    removed = await events_of(admin, "site.production_removed")
    assert len(removed) == 1 and removed[0]["severity"] == "warning" and removed[0]["detail"]["environment"] == "staging"
    # Back into production, and other edits, need nothing typed.
    assert (await admin.patch(f"/api/sites/{site['id']}", json={"environment": "production"})).status_code == 200
    assert (await admin.patch(f"/api/sites/{site['id']}", json={"notes": "fine", "name": "Shop 2"})).status_code == 200
    await as_operator(app_client)
    assert (await app_client.patch(f"/api/sites/{site['id']}", json={"environment": "test", "confirm": "Shop 2"})).status_code == 403


async def test_unlinking_a_site_from_a_production_server_needs_its_typed_name(admin):
    server = (await admin.post("/api/servers", json={"name": "prod", "host": "prod.example", "username": "ccagent"})).json()["server"]
    site = (await admin.post("/api/sites", json={"name": "Test Site", "url": "https://t.example"})).json()["site"]
    await admin.patch(f"/api/sites/{site['id']}", json={"environment": "test", "confirm": "Test Site"})
    async with session_scope() as s:  # linked by an earlier version (no gate check then)
        row = await s.get(Site, site["id"])
        row.server_id, row.server_site_key = server["id"], "t"
    r = await admin.patch(f"/api/sites/{site['id']}", json={"unlink_server": True})
    assert r.status_code == 400 and r.json()["detail"].startswith("confirm:")
    assert (await admin.patch(f"/api/sites/{site['id']}", json={"unlink_server": True, "confirm": "Test Site"})).status_code == 200


async def test_taking_a_server_out_of_production_needs_its_typed_name(admin):
    server = (await admin.post("/api/servers", json={"name": "cwp1", "host": "cwp1.example", "username": "ccagent"})).json()["server"]
    site = (await admin.post("/api/sites", json={"name": "On cwp1", "url": "https://on.example"})).json()["site"]
    await admin.patch(f"/api/sites/{site['id']}", json={"environment": "test", "confirm": "On cwp1"})
    async with session_scope() as s:
        row = await s.get(Site, site["id"])
        row.server_id, row.server_site_key = server["id"], "on"
    r = await admin.patch(f"/api/servers/{server['id']}", json={"environment": "test"})
    assert r.status_code == 400 and r.json()["detail"].startswith("confirm:")
    r = await admin.patch(f"/api/servers/{server['id']}", json={"environment": "test", "name": "cwp-one", "confirm": "cwp1"})
    assert r.status_code == 200 and r.json()["server"]["environment"] == "test"
    removed = await events_of(admin, "server.production_removed")
    assert len(removed) == 1 and removed[0]["severity"] == "warning" and removed[0]["detail"]["sites_no_longer_production"] == [site["id"]]
    assert (await admin.patch(f"/api/servers/{server['id']}", json={"environment": "production"})).status_code == 200


# ----------------------------------------------------- C3-01: the gate's address for the key is this site's


async def test_a_site_is_linked_only_to_the_key_the_gate_gives_its_own_address(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        server = await add_server(admin, srv)
        site = (await admin.post("/api/sites", json={"name": "Site One", "url": "http://site.test", "environment": "test"})).json()["site"]
        other = (await admin.post("/api/sites", json={"name": "Other", "url": "http://other.test", "environment": "test"})).json()["site"]
        link = {"server_id": server["id"], "server_site_key": "site-one"}
        r = await admin.patch(f"/api/sites/{site['id']}", json=link)  # the server has not reported its keys yet
        assert r.status_code == 400 and "has not reported a site key 'site-one'" in r.json()["detail"]
        await gate_hello(admin, web, server["id"])
        r = await admin.patch(f"/api/sites/{other['id']}", json=link)
        assert r.status_code == 400 and "gives site key 'site-one' the address http://site.test, which is not this site" in r.json()["detail"]
        assert (await admin.patch(f"/api/sites/{other['id']}", json={**link, "server_site_key": "nope"})).status_code == 400
        assert (await admin.patch(f"/api/sites/{site['id']}", json=link)).status_code == 200
        www = (await admin.post("/api/sites", json={"name": "www", "url": "http://www.site.test", "environment": "test"})).json()["site"]
        assert (await admin.patch(f"/api/sites/{www['id']}", json=link)).status_code == 200  # www and non-www are one site


async def test_a_repair_names_the_gate_key_and_refuses_when_the_gate_gives_it_another_address(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, server = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        approval = await approval_for(admin, task["id"])
        assert approval["details"]["binding"]["gate"] == {"key": "site-one", "url": "http://site.test"}
        assert approval["details"]["binding"]["server"]["host_key_fingerprint"] == srv.fingerprint
        assert "cc-gate site key site-one (http://site.test)" in approval["details"]["target"]
        # On the server, the key now points at another install (its config was edited); the Command Center has not noticed.
        srv.config["sites"]["site-one"]["url"] = "http://elsewhere.test"
        srv.write_config()
        done = await approve(admin, make_worker, task)
        assert done["status"] == "failed" and "gives site key 'site-one' the address http://elsewhere.test" in done["error"]
        assert await ops_for(done["id"]) == [("hello", "read")]  # nothing was changed
        # Same host, other address: still not what was approved.
        srv.config["sites"]["site-one"]["url"] = "https://site.test"
        srv.write_config()
        done = await approve(admin, make_worker, await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches",
                                                                {"site_id": site["id"]}))
        assert done["status"] == "failed" and "(approved: http://site.test)" in done["error"]
        assert not any("cache flush" in c for c in srv.calls("wp"))
        # Once the Command Center reads the new address, pending approvals no longer match it.
        task = await run_task(admin, make_worker, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        await gate_hello(admin, web, server["id"])
        r = await decide(admin, await approval_for(admin, task["id"]))
        assert r.status_code == 400 and "target changed" in r.text


async def test_a_diagnosis_flags_a_gate_address_that_is_not_the_site(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        srv.config["sites"]["site-one"]["url"] = "http://elsewhere.test"
        srv.write_config()
        diag = await run_task(admin, make_worker, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        mismatch = next(f for f in diag["result"]["findings"] if f["code"] == "gate_url_mismatch")
        assert mismatch["severity"] == "critical" and "http://elsewhere.test" in mismatch["title"] and "http://site.test" in mismatch["title"]
        r = await request_task(admin, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        assert r.status_code == 201  # the Command Center's own record still matches; the run itself refuses (above)


# --------------------------------------------------- C3-07: the exact version or address a repair applies


async def test_a_plugin_update_approval_names_the_versions_and_refuses_another_one(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        r = await request_task(admin, "wordpress_security", "repair_update_plugin", {"site_id": site["id"], "slug": "akismet"})
        assert r.status_code == 400 and "run the server-side check" in r.json()["detail"]  # nothing known to name yet
        await make_worker().run_until_idle()  # the baseline scan includes the server-side check
        r = await request_task(admin, "wordpress_security", "repair_update_plugin", {"site_id": site["id"], "slug": "evil-cloak"})
        assert r.status_code == 400 and "found no update for evil-cloak 1.0" in r.json()["detail"]
        task = (await request_task(admin, "wordpress_security", "repair_update_plugin", {"site_id": site["id"], "slug": "akismet"})).json()["task"]
        approval = await approval_for(admin, task["id"])
        expected = approval["details"]["expected"]
        assert (expected["plugin"], expected["version"], expected["update_version"]) == ("akismet", "5.3", "5.4")
        assert "(update akismet 5.3 → 5.4)" in approval["summary"]
        # WordPress.org has published 5.5 by the time it runs.
        wp_prints(srv, "plugin list", '[{"name":"akismet","status":"active","version":"5.3","update":"available","update_version":"5.5"}]')
        done = await approve(admin, make_worker, task)
        assert done["status"] == "failed" and "approval names 5.3 → 5.4" in done["error"] and "5.5 available" in done["error"]
        assert await ops_for(done["id"]) == [("hello", "read"), ("wp_plugin_list", "read")]
        assert not any("plugin update" in c for c in srv.calls("wp"))


async def test_core_and_address_repairs_refuse_values_other_than_the_approved_ones(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        task = await run_task(admin, make_worker, "wordpress_security", "repair_reinstall_core", {"site_id": site["id"]})
        assert (await approval_for(admin, task["id"]))["details"]["expected"]["core_version"] == "6.8.3"
        wp = srv.dir / "wp-cli"
        wp.write_text(wp.read_text().replace('"core version") echo "6.8.3"', '"core version") echo "6.9.1"'))
        done = await approve(admin, make_worker, task)
        assert done["status"] == "failed" and "WordPress is now version 6.9.1, but the approval names 6.8.3" in done["error"]
        assert not any("core download" in c for c in srv.calls("wp"))

        put(srv.dir / "option-home", "http://evil.example")
        task = await run_task(admin, make_worker, "wordpress_security", "repair_restore_site_url", {"site_id": site["id"]})
        expected = (await approval_for(admin, task["id"]))["details"]["expected"]
        assert (expected["home"], expected["siteurl"]) == ("http://site.test", "http://site.test")
        srv.config["sites"]["site-one"]["siteurl"] = "http://site.test/wp"  # the gate would now write another siteurl
        srv.write_config()
        done = await approve(admin, make_worker, task)
        assert done["status"] == "failed" and "approval names home http://site.test and siteurl http://site.test" in done["error"]
        assert (srv.dir / "option-home").read_text() == "http://evil.example"  # unchanged
        assert all(kind == "read" for _, kind in await ops_for(done["id"]))


async def test_an_approval_that_names_no_version_is_not_carried_out(admin, web, make_worker):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        await make_worker().run_until_idle()
        task = await run_task(admin, make_worker, "wordpress_security", "repair_reinstall_plugin", {"site_id": site["id"], "slug": "akismet"})
        async with session_scope() as s:  # as if requested before versions were recorded
            row = await s.scalar(select(Approval).where(Approval.task_id == task["id"]))
            row.details = {k: v for k, v in row.details.items() if k != "expected"}
        done = await approve(admin, make_worker, task)
        assert done["status"] == "failed" and "does not name the exact version" in done["error"]
        assert not any("plugin install" in c for c in srv.calls("wp"))


# ------------------------------------------------------------------------------- C2-12: ads sample data


async def test_every_ad_finding_says_it_is_sample_data(admin, make_worker):
    report = await run_task(admin, make_worker, "social_marketing", "ads_report", {"days": 7})
    assert report["status"] == "succeeded" and report["result"]["data"]["sample_data"] is True
    findings = report["result"]["findings"]
    assert findings and all(f["title"].startswith("[Sample data] ") and f["severity"] == "info" for f in findings)
    assert all("no ad account is connected" in f["detail"] for f in findings)


@pytest.mark.parametrize("capability", ["repair_deactivate_plugin", "repair_flush_caches"])
async def test_repairs_without_a_value_to_name_need_no_diagnosis(admin, web, make_worker, capability):
    key = await ssh_key(admin)
    async with FakeGateServer(key["openssh"]) as srv:
        site, _ = await linked_site(admin, srv, web)
        params = {"site_id": site["id"], **({"slug": "evil-cloak"} if "plugin" in capability else {})}
        r = await request_task(admin, "wordpress_security", capability, params)
        assert r.status_code == 201, r.text
        assert "expected" not in (await approval_for(admin, r.json()["task"]["id"]))["details"]
    assert wordpress_site  # imported for the shared helpers' fake site
