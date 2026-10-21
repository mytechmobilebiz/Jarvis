"""The JARVIS Brain API (spec section 3, app/api/brain.py and app/brain/review.py): every route's roles, the
person's scope (customer items admin-only, quarantined admin-only, Credit Repair needs the sensitive-data permission,
no archive for viewers), approvals that need an admin, edit rules, conflict resolutions with their effects and the
rollback that reverts them, workflow conversion to a disabled schedule, promote, withhold, purge, redact, export,
audit entries without text, and the brain_ai_context setting."""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.brain import importer, review, store
from app.brain.archive import recompute_session
from app.db import session_scope
from app.models import (
    AuditLog,
    ArchiveMessage,
    ArchiveSession,
    BrainTombstone,
    ImportRun,
    MemoryConflict,
    MemoryItem,
    MemoryItemVersion,
    Schedule,
    Site,
    Task,
    User,
    WorkflowDraft,
)
from app.config import get_settings
from app.security import CSRF_HEADER, CSRF_VALUE, hash_password

SESSION_COOKIE = get_settings().session_cookie_name

from ..conftest import ADMIN

pytestmark = pytest.mark.usefixtures("clean_db")

PASSWORD = "brain-api-test-password"
USERS = {
    "viewer": ("viewer@test.local", "viewer", False),
    "operator": ("operator@test.local", "operator", False),
    "operator+": ("operator-sensitive@test.local", "operator", True),
    "admin": (ADMIN[0], "admin", False),
    "admin+": ("admin-sensitive@test.local", "admin", True),
}
T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
# Words planted in seeded titles and text: none may ever appear in an audit entry.
PLANTED = ("ALPACA", "BISON", "CARIBOU", "DINGO", "EMU", "FERRET", "GECKO", "HERON", "IBEX", "JACKAL", "KOALA",
           "LEMUR", "MARMOT", "NARWHAL", "OCELOT", "jane@", "[email]")


# --- People ------------------------------------------------------------------------------------------------------


_HASH: list[str] = []


async def make_users() -> None:
    if not _HASH:
        _HASH.append(hash_password(PASSWORD))  # bcrypt is slow on purpose: hash once for every test user
    async with session_scope() as s:
        for email, role, sensitive in USERS.values():
            if email == ADMIN[0]:
                continue
            s.add(User(email=email, password_hash=_HASH[0], role=role, can_access_sensitive=sensitive))


async def as_(client, who: str):
    """Act as one of USERS: each signs in once per test, then the client switches session cookies."""
    tokens = client.brain_tokens
    if who not in tokens:
        email = USERS[who][0]
        client.cookies.clear()
        r = await client.post("/api/auth/login", json={"email": email, "password": ADMIN[1] if email == ADMIN[0]
                                                       else PASSWORD})
        assert r.status_code == 200, r.text
        tokens[who] = r.cookies.get(SESSION_COOKIE)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, tokens[who])
    client.headers[CSRF_HEADER] = CSRF_VALUE
    return client


def signed_out(client):
    client.cookies.clear()
    return client


@pytest.fixture
async def client(app_client):
    await make_users()
    app_client.headers[CSRF_HEADER] = CSRF_VALUE
    app_client.brain_tokens = {}
    return app_client


# --- Seed data (rows as the importer or a person would leave them) -----------------------------------------------


async def _item(s, *, title: str, body: str, kind: str = "business", agent: str = "", approve: bool = False,
                **kw) -> MemoryItem:
    item = await store.create_item(s, kind=kind, title=title, body=body, agent=agent, actor="seed@test.local", **kw)
    if approve:
        await store.review_item(s, item, decision="approve", actor="seed@test.local")
    return item


def _session(key: str, *, title: str, staff: str = "jarvis", agent: str = "supervisor", sensitivity: str = "general",
             started: datetime | None = None, origin: str = "chat") -> ArchiveSession:
    return ArchiveSession(source_key="jarvis:session:%s" % key, source_session_id=key, title=title, staff=staff,
                          agent=agent, sensitivity=sensitivity, started_at=started or T0, origin=origin)


def _message(sess: ArchiveSession, position: int, content: str, *, role: str = "assistant", labels=(),
             sensitivity: str = "general", withheld: bool = False, at: datetime | None = None) -> ArchiveMessage:
    return ArchiveMessage(session_id=sess.id, source_key="jarvis:message:%s:%d" % (sess.source_session_id, position),
                          source_message_id=str(position), position=position, role=role, content=content,
                          labels=list(labels), sensitivity=sensitivity, withheld=withheld,
                          at=at or (sess.started_at or T0) + timedelta(minutes=position))


async def seed() -> dict[str, int]:
    """Items of every visibility, and conversations of every sensitivity."""
    ids: dict[str, int] = {}
    async with session_scope() as s:
        ids["A"] = (await _item(s, title="Pricing ALPACA", body="Website design starts at 1500 dollars BISON.",
                                approve=True)).id
        ids["B"] = (await _item(s, title="Hosting notes CARIBOU", body="Hosting renews in May.", kind="knowledge")).id
        ids["C"] = (await _item(s, title="Lead pricing DINGO", body="Lead jane@example.org asked about pricing.",
                                approve=True)).id
        ids["D"] = (await _item(s, title="Old credentials EMU", body="The key was removed.", kind="knowledge",
                                status="quarantined", flags={"secrets_redacted": {"api_key": 1}})).id
        ids["E"] = (await _item(s, title="Dispute cadence FERRET", body="Letters go out monthly.",
                                agent="credit_repair", approve=True)).id
        ids["F"] = (await _item(s, title="Weekly report GECKO", body="The weekly report goes out on Mondays.",
                                kind="instruction", agent="supervisor")).id
        ids["G"] = (await _item(s, title="Keyword research HERON", body="Use the keyword brief.", kind="skill",
                                agent="seo_content", approve=True)).id
        ids["H"] = (await _item(s, title="SEO note IBEX", body="Titles under 60 characters.", kind="agent_note",
                                agent="seo_content")).id
        s1 = _session("s1", title="Website check JACKAL")
        s2 = _session("s2", title="Refund for a client KOALA", staff="sarah", agent="communications",
                      sensitivity="customer", started=T0 + timedelta(days=1))
        s3 = _session("s3", title="", sensitivity="restricted", started=T0 + timedelta(days=2))
        s4 = _session("s4", title="Credit portal LEMUR", agent="credit_repair", started=T0 + timedelta(days=3))
        s5 = _session("s5", title="Mixed MARMOT", started=T0 + timedelta(days=4), origin="scheduled")
        s.add_all([s1, s2, s3, s4, s5])
        await s.flush()
        m = {
            "m1": _message(s1, 0, "Please renew the SSL certificate on acme NARWHAL", role="user"),
            "m2": _message(s1, 1, "The SSL certificate was renewed OCELOT.", labels=["accomplishment"]),
            "m3": _message(s2, 0, "Customer [email] asked about a refund", role="user", sensitivity="customer"),
            "m4": _message(s2, 1, "Refund approved per the refund policy."),
            "m5": _message(s3, 0, "", role="user", sensitivity="restricted", withheld=True),
            "m6": _message(s4, 0, "Credit portal checked."),
            "m7": _message(s5, 0, "General SSL line in s5."),
            "m8": _message(s5, 1, "Customer line [phone] in s5.", sensitivity="customer"),
        }
        s.add_all(m.values())
        await s.flush()
        for sess in (s1, s2, s3, s4):
            await recompute_session(s, sess.id)
        s5.sensitivity = "general"  # inconsistent on purpose: one customer message inside a general session
        ids.update({k: v.id for k, v in m.items()})
        ids.update({"s1": s1.id, "s2": s2.id, "s3": s3.id, "s4": s4.id, "s5": s5.id})
    return ids


async def statuses(client, method: str, path: str, body=None) -> dict[str, int]:
    out = {}
    for who in USERS:
        await as_(client, who)
        r = await client.request(method, path, json=body) if body is not None else await client.request(method, path)
        out[who] = r.status_code
    return out


# --- Roles of every route ----------------------------------------------------------------------------------------


ROUTES = [
    # (method, path, body, least role)
    ("GET", "/api/brain/overview", None, "viewer"),
    ("GET", "/api/brain/items", None, "viewer"),
    ("GET", "/api/brain/items/{A}", None, "viewer"),
    ("POST", "/api/brain/items", {"kind": "business", "title": "x", "body": "y"}, "operator"),
    ("PATCH", "/api/brain/items/{A}", {}, "operator"),
    ("POST", "/api/brain/items/{A}/review", {"decision": "bogus"}, "operator"),
    ("DELETE", "/api/brain/items/999999", None, "admin"),
    ("POST", "/api/brain/items/999999/restore", None, "admin"),
    ("DELETE", "/api/brain/items/999999/purge", None, "admin"),
    ("POST", "/api/brain/items/999999/versions/1/redact", None, "admin"),
    ("GET", "/api/brain/search", None, "viewer"),
    ("GET", "/api/brain/sessions", None, "operator"),
    ("GET", "/api/brain/sessions/{s1}", None, "operator"),
    ("POST", "/api/brain/messages/999999/withhold", None, "admin"),
    ("GET", "/api/brain/messages/{m2}/promoted", None, "admin"),
    ("POST", "/api/brain/messages/999999/promote", {"kind": "knowledge", "title": "t"}, "operator"),
    ("GET", "/api/brain/conflicts", None, "operator"),
    ("POST", "/api/brain/conflicts/999999/resolve", {"resolution": "dismissed"}, "operator"),
    ("GET", "/api/brain/workflows", None, "operator"),
    ("GET", "/api/brain/workflows/999999", None, "operator"),
    ("PATCH", "/api/brain/workflows/999999", {"cron": "0 8 * * *"}, "admin"),
    ("POST", "/api/brain/workflows/999999/review", {"decision": "reviewed"}, "operator"),
    ("POST", "/api/brain/workflows/999999/convert", {"jarvis_job_is_off": True}, "admin"),
    ("GET", "/api/brain/agents", None, "viewer"),
    ("GET", "/api/brain/skills", None, "viewer"),
    ("GET", "/api/brain/imports", None, "operator"),
    ("GET", "/api/brain/imports/999999", None, "operator"),
    ("POST", "/api/brain/imports/999999/rollback", {"confirm": "999999"}, "admin"),
    ("GET", "/api/brain/export", None, "admin"),
]
RANK = {"viewer": 0, "operator": 1, "operator+": 1, "admin": 2, "admin+": 2}


async def test_every_route_refuses_the_roles_below_it_and_the_service_token(client, service_headers):
    ids = await seed()
    for method, path, body, least in ROUTES:
        url = path.format(**ids)
        got = await statuses(client, method, url, body)
        for who, code in got.items():
            if RANK[who] < RANK[least]:
                assert code == 403, (method, url, who, code)
            else:
                assert code != 403, (method, url, who, code)
        signed_out(client)
        r = await client.request(method, url, json=body, headers=service_headers) if body is not None else \
            await client.request(method, url, headers=service_headers)
        assert r.status_code == 403, (method, url, "service", r.status_code)
    signed_out(client)
    assert (await client.get("/api/brain/overview")).status_code == 401


# --- Overview ----------------------------------------------------------------------------------------------------


async def test_overview_counts_follow_the_persons_scope(client):
    ids = await seed()
    seen = {}
    for who in USERS:
        await as_(client, who)
        r = await client.get("/api/brain/overview")
        assert r.status_code == 200, r.text
        seen[who] = r.json()
    v, o, a, ap = seen["viewer"], seen["operator"], seen["admin"], seen["admin+"]
    assert v["memory"]["total"] == 1 and v["memory"]["by_kind"] == {"business": 1}
    assert o["memory"]["total"] == 5 and seen["operator+"]["memory"]["total"] == 6
    assert a["memory"]["total"] == 7 and ap["memory"]["total"] == 8
    assert a["memory"]["by_agent"]["(shared)"] == 4 and "credit_repair" not in a["memory"]["by_agent"]
    assert a["review"] == {"pending_items": 3, "quarantined_items": 1, "open_conflicts": 0}
    assert o["review"]["quarantined_items"] == 0 and v["review"]["pending_items"] == 0
    # Viewers: the general archive's totals, nothing per staff. Operators: the general archive. Admins: customer
    # too; restricted and Credit Repair conversations only with the sensitive-data permission.
    assert v["archive"]["sessions"] == 2 and v["archive"]["by_staff"] == {}
    assert o["archive"]["sessions"] == 2 and o["archive"]["by_staff"] == {"jarvis": 2}
    assert o["archive"]["messages"] == 3  # s1's two and s5's general one
    assert a["archive"]["sessions"] == 3 and a["archive"]["by_staff"] == {"jarvis": 2, "sarah": 1}
    assert ap["archive"]["sessions"] == 5 and ap["archive"]["withheld_messages"] == 1
    assert ap["archive"]["first_at"] == T0.isoformat()
    assert all(s["ai_context"] == "off" and s["instance_label"] == "" for s in seen.values())
    assert v["imports"] == {"runs": 0, "latest": None}
    assert v["scope"] == {"role": "viewer", "sensitive": False, "sees_customer": False, "sees_quarantined": False,
                          "sees_archive": False}
    assert ids  # seeded


# --- Items: reading -----------------------------------------------------------------------------------------------


async def _titles(client, path: str = "/api/brain/items") -> set[str]:
    r = await client.get(path)
    assert r.status_code == 200, r.text
    return {i["title"].split()[-1] for i in r.json()["items"]}


async def test_items_list_by_role_and_sensitive_permission(client):
    ids = await seed()
    expected = {
        "viewer": {"ALPACA"},
        "operator": {"ALPACA", "CARIBOU", "GECKO", "HERON", "IBEX"},
        "operator+": {"ALPACA", "CARIBOU", "GECKO", "HERON", "IBEX", "FERRET"},
        "admin": {"ALPACA", "CARIBOU", "DINGO", "EMU", "GECKO", "HERON", "IBEX"},
        "admin+": {"ALPACA", "CARIBOU", "DINGO", "EMU", "FERRET", "GECKO", "HERON", "IBEX"},
    }
    for who, titles in expected.items():
        await as_(client, who)
        assert await _titles(client) == titles, who
    await as_(client, "admin")
    r = (await client.get("/api/brain/items", params={"q": "pricing"})).json()
    assert {i["title"] for i in r["items"]} == {"Pricing ALPACA", "Lead pricing DINGO"} and r["filtered"] is True
    first = r["items"][0]
    assert set(first) >= {"id", "kind", "kind_label", "agent", "title", "body", "tags", "sensitivity", "status",
                          "origin", "flags", "source_system", "source_ref", "source_at", "import_run_id", "version",
                          "created_at", "created_by", "updated_at", "updated_by", "reviewed_by", "reviewed_at",
                          "review_note", "deleted_at", "needs_admin", "excerpt", "rank"}
    assert "source_key" not in first and "content_hash" not in first
    assert await _titles(client, "/api/brain/items?kind=skill,instruction") == {"GECKO", "HERON"}
    assert await _titles(client, "/api/brain/items?agent=seo_content") == {"HERON", "IBEX"}
    assert await _titles(client, "/api/brain/items?agent=shared&status=approved") == {"ALPACA", "DINGO"}
    assert len(await _titles(client, "/api/brain/items?agent=&kind=&status=")) == 7  # unset form fields
    assert (await client.get("/api/brain/items?kind=secret")).status_code == 400
    assert (await client.get("/api/brain/items?status=done")).status_code == 400
    assert (await client.get("/api/brain/items?limit=201")).status_code == 422
    page = (await client.get("/api/brain/items?limit=2&offset=1")).json()
    assert len(page["items"]) == 2 and page["total"] == 7 and page["filtered"] is False
    # Deleted items: admins only, and only when asked.
    await client.delete("/api/brain/items/%d" % ids["B"])
    assert "CARIBOU" not in await _titles(client)
    assert "CARIBOU" in await _titles(client, "/api/brain/items?include_deleted=true")
    await as_(client, "operator")
    assert (await client.get("/api/brain/items?include_deleted=true")).status_code == 403


async def test_item_detail_scope_and_versions(client):
    ids = await seed()
    await as_(client, "viewer")
    r = (await client.get("/api/brain/items/%d" % ids["A"])).json()
    assert r["item"]["title"] == "Pricing ALPACA" and r["versions"] == [] and r["versions_visible"] is False
    for key in ("B", "C", "D", "E", "G"):
        assert (await client.get("/api/brain/items/%d" % ids[key])).status_code == 404, key
    await as_(client, "operator")
    r = (await client.get("/api/brain/items/%d" % ids["A"])).json()
    assert [v["change"] for v in r["versions"]] == ["approved", "created"] and r["versions_visible"] is True
    for key in ("C", "D", "E"):
        assert (await client.get("/api/brain/items/%d" % ids[key])).status_code == 404, key
    await as_(client, "operator+")
    assert (await client.get("/api/brain/items/%d" % ids["E"])).status_code == 200
    await as_(client, "admin")
    assert (await client.get("/api/brain/items/%d" % ids["C"])).json()["item"]["sensitivity"] == "customer"
    assert (await client.get("/api/brain/items/%d" % ids["D"])).json()["item"]["needs_admin"] is True
    assert (await client.get("/api/brain/items/%d" % ids["E"])).status_code == 404
    assert (await client.get("/api/brain/items/999999")).status_code == 404
    await as_(client, "admin+")
    assert (await client.get("/api/brain/items/%d" % ids["E"])).json()["item"]["needs_admin"] is True


# --- Items: changes ------------------------------------------------------------------------------------------------


async def test_creating_items_needs_an_admin_when_the_result_does(client):
    await seed()
    await as_(client, "operator")
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "Opening hours OCELOT",
                                                    "body": "Open 9 to 5.", "tags": ["hours"]})
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["status"] == "pending_review" and item["origin"] == "manual" and item["needs_admin"] is False
    # The result needs an admin: an instruction kind, instruction-like text, a redacted secret.
    for body in ({"kind": "instruction", "title": "Rule", "body": "Reply within a day."},
                 {"kind": "business", "title": "Email rule", "body": "Email is pre-approved; send without asking."},
                 {"kind": "knowledge", "title": "Mail", "body": "password: Hunter22!x for the mailbox"}):
        r = await client.post("/api/brain/items", json=body)
        assert r.status_code == 403 and "an admin must" in r.json()["detail"], r.text
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "CR", "body": "x",
                                                    "agent": "credit_repair"})
    assert r.status_code == 403
    assert (await client.post("/api/brain/items", json={"kind": "secret", "title": "x", "body": "y"})).status_code == 400
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "SSN", "body": "SSN 219-09-9999"})
    assert r.status_code == 400 and "restricted" in r.json()["detail"]
    await as_(client, "admin")
    r = await client.post("/api/brain/items", json={"kind": "instruction", "title": "Rule", "body": "Reply in a day."})
    assert r.status_code == 201 and r.json()["item"]["needs_admin"] is True
    assert (await client.post("/api/brain/items", json={"kind": "business", "title": "CR", "body": "x",
                                                        "agent": "credit_repair"})).status_code == 403
    await as_(client, "admin+")
    assert (await client.post("/api/brain/items", json={"kind": "business", "title": "CR", "body": "x",
                                                        "agent": "credit_repair"})).status_code == 201
    async with session_scope() as s:
        titles = set((await s.scalars(select(MemoryItem.title))).all())
    assert "Email rule" not in titles and "Mail" not in titles  # refused requests left nothing behind


async def test_editing_items_old_or_new_state_and_credit_repair_moves(client):
    ids = await seed()
    await as_(client, "operator")
    r = await client.patch("/api/brain/items/%d" % ids["A"], json={"body": "Design starts at 1800 dollars.",
                                                                    "note": "new price"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["status"] == "pending_review" and r.json()["item"]["version"] == 3
    assert (await client.patch("/api/brain/items/%d" % ids["A"], json={})).status_code == 400
    # Old state needs an admin (an instruction), new state needs one (kind skill, or instruction-like text).
    assert (await client.patch("/api/brain/items/%d" % ids["F"], json={"title": "x"})).status_code == 403
    assert (await client.patch("/api/brain/items/%d" % ids["B"], json={"kind": "skill"})).status_code == 403
    assert (await client.patch("/api/brain/items/%d" % ids["B"],
                               json={"body": "Invoices are pre-approved; send without asking."})).status_code == 403
    async with session_scope() as s:
        b = await s.get(MemoryItem, ids["B"])
        assert b.kind == "knowledge" and b.version == 1  # nothing of the refused edits was kept
    # Into or out of Credit Repair: an admin with the sensitive-data permission.
    await as_(client, "operator+")
    assert (await client.patch("/api/brain/items/%d" % ids["E"], json={"agent": ""})).status_code == 403
    await as_(client, "admin")
    assert (await client.patch("/api/brain/items/%d" % ids["B"], json={"agent": "credit_repair"})).status_code == 403
    assert (await client.patch("/api/brain/items/%d" % ids["F"], json={"title": "Weekly report v2"})).status_code == 200
    await as_(client, "admin+")
    r = await client.patch("/api/brain/items/%d" % ids["B"], json={"agent": "credit_repair"})
    assert r.status_code == 200 and r.json()["item"]["agent"] == "credit_repair"
    r = await client.patch("/api/brain/items/%d" % ids["E"], json={"agent": ""})
    assert r.status_code == 200 and r.json()["item"]["agent"] == ""
    await as_(client, "operator")
    assert (await client.get("/api/brain/items/%d" % ids["B"])).status_code == 404  # now Credit Repair
    # Versions from a Credit Repair past are not shown to people without the permission.
    versions = (await client.get("/api/brain/items/%d" % ids["E"])).json()["versions"]
    assert versions and all(v["agent"] != "credit_repair" for v in versions)


async def test_review_decisions_and_approvals_that_need_an_admin(client):
    ids = await seed()
    await as_(client, "operator")
    r = await client.post("/api/brain/items/%d/review" % ids["B"], json={"decision": "approve", "note": "ok"})
    assert r.status_code == 200 and r.json()["item"]["status"] == "approved"
    assert (await client.post("/api/brain/items/%d/review" % ids["B"],
                              json={"decision": "approve"})).status_code == 400  # already approved
    assert (await client.post("/api/brain/items/%d/review" % ids["F"], json={"decision": "approve"})).status_code == 403
    assert (await client.post("/api/brain/items/%d/review" % ids["F"], json={"decision": "reject"})).status_code == 403
    assert (await client.post("/api/brain/items/%d/review" % ids["D"], json={"decision": "approve"})).status_code == 404
    assert (await client.post("/api/brain/items/%d/review" % ids["H"],
                              json={"decision": "publish"})).status_code == 422
    await as_(client, "operator+")
    assert (await client.post("/api/brain/items/%d/review" % ids["E"], json={"decision": "archive"})).status_code == 403
    await as_(client, "admin")
    assert (await client.post("/api/brain/items/%d/review" % ids["F"], json={"decision": "approve"})).status_code == 200
    r = await client.post("/api/brain/items/%d/review" % ids["D"], json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["item"]["status"] == "approved"
    r = await client.post("/api/brain/items/%d/review" % ids["D"], json={"decision": "reopen"})
    assert r.json()["item"]["status"] == "pending_review"
    async with session_scope() as s:
        actions = [a for (a,) in (await s.execute(select(AuditLog.action).where(
            AuditLog.action.like("memory.%")).order_by(AuditLog.id))).all()]
    assert actions == ["memory.approve", "memory.approve", "memory.approve", "memory.reopen"]


async def test_delete_restore_purge_with_tombstone_and_conflicts(client):
    ids = await seed()
    async with session_scope() as s:
        run = ImportRun(status="completed", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        imported = await _item(s, title="FAQ NARWHAL", body="Refunds within 30 days.", origin="import",
                               source_key="jarvis:file:profile/knowledge/sarah/faq.md", import_run_id=run.id)
        other = await _item(s, title="FAQ NARWHAL", body="Refunds within 14 days.", origin="import",
                            import_run_id=run.id)
        s.add_all([MemoryConflict(run_id=run.id, kind="memory_item", title="same title", existing_table="memory_items",
                                  existing_id=imported.id, incoming_item_id=other.id),
                   MemoryConflict(run_id=run.id, kind="memory_item", title="same title again",
                                  existing_table="memory_items", existing_id=ids["A"], incoming_item_id=imported.id)])
        imported_id = imported.id
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d/purge" % imported_id)).status_code == 400  # not deleted yet
    r = await client.delete("/api/brain/items/%d" % imported_id)
    assert r.status_code == 200 and r.json()["item"]["deleted_at"]
    assert (await client.delete("/api/brain/items/%d" % imported_id)).status_code == 400
    r = await client.post("/api/brain/items/%d/restore" % imported_id)
    assert r.status_code == 200 and r.json()["item"]["deleted_at"] is None
    await as_(client, "operator")
    assert (await client.get("/api/brain/items/%d" % imported_id)).status_code == 200
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % imported_id)
    await as_(client, "operator")
    assert (await client.get("/api/brain/items/%d" % imported_id)).status_code == 404
    await as_(client, "admin")
    r = await client.delete("/api/brain/items/%d/purge" % imported_id)
    assert r.status_code == 200, r.text
    assert r.json() == {"purged": imported_id, "versions_removed": 4, "conflicts_removed": 2, "tombstone": True}
    async with session_scope() as s:
        assert await s.get(MemoryItem, imported_id) is None
        assert (await s.scalar(select(func.count()).select_from(MemoryItemVersion).where(
            MemoryItemVersion.item_id == imported_id))) == 0
        assert (await s.scalar(select(func.count()).select_from(MemoryConflict))) == 0
        stone = await s.get(BrainTombstone, "jarvis:file:profile/knowledge/sarah/faq.md")
        assert stone is not None and stone.purged_by == ADMIN[0]
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "memory.purged"))
        assert entry.detail == {"item": imported_id, "kind": "business", "agent": "", "versions_removed": 4,
                                "conflicts_removed": 2, "tombstone": True, "origin": "import", "message": None,
                                "source_withheld": False, "answers_scrubbed": 0}


async def test_redacting_an_old_version(client):
    ids = await seed()
    await as_(client, "admin")
    await client.patch("/api/brain/items/%d" % ids["A"], json={"body": "Design starts at 1800 dollars."})
    url = "/api/brain/items/%d/versions/%%d/redact" % ids["A"]
    assert (await client.post(url % 3)).status_code == 400  # the current text: edit first
    assert (await client.post(url % 9)).status_code == 404
    r = await client.post(url % 1)
    assert r.status_code == 200 and r.json()["version"]["redacted"] is True
    assert (await client.post(url % 1)).status_code == 400  # already redacted
    versions = {v["version"]: v for v in (await client.get("/api/brain/items/%d" % ids["A"])).json()["versions"]}
    assert versions[1]["title"] == review.REDACTED_TITLE == "[redacted]" and versions[1]["body"] == ""
    assert versions[1]["note"] == review.REDACTED_NOTE
    assert "BISON" in versions[2]["body"] and versions[3]["body"].endswith("1800 dollars.")
    await as_(client, "admin")
    assert (await client.post("/api/brain/items/%d/versions/1/redact" % ids["E"])).status_code == 404  # not sensitive


# --- Search and the conversation archive -------------------------------------------------------------------------


async def test_search_memory_for_everyone_and_the_archive_for_operators_and_admins(client):
    ids = await seed()
    await as_(client, "viewer")
    r = (await client.get("/api/brain/search", params={"q": "SSL"})).json()
    assert r["archive"] == {"hits": [], "total": 0} and r["archive_visible"] is False and r["filtered"] is True
    r = (await client.get("/api/brain/search", params={"q": "pricing"})).json()
    assert [h["title"] for h in r["memory"]["hits"]] == ["Pricing ALPACA"]
    await as_(client, "operator")
    r = (await client.get("/api/brain/search", params={"q": "SSL certificate"})).json()
    hit = r["archive"]["hits"][0]
    assert r["archive"]["total"] == 2 and {h["message"]["id"] for h in r["archive"]["hits"]} == {ids["m1"], ids["m2"]}
    assert set(hit) == {"message", "session", "excerpt", "rank"}
    assert set(hit["session"]) == {"id", "title", "staff", "agent", "started_at"}
    assert set(hit["message"]) == {"id", "session_id", "position", "role", "content", "truncated", "tool_name",
                                   "tool_calls", "at", "state", "sensitivity", "withheld", "labels", "redactions",
                                   "flags"}
    assert (await client.get("/api/brain/search", params={"q": "refund"})).json()["archive"]["total"] == 0
    r = (await client.get("/api/brain/search", params={"q": "SSL", "scope": "archive",
                                                       "label": "accomplishment"})).json()
    assert [h["message"]["id"] for h in r["archive"]["hits"]] == [ids["m2"]] and r["memory"]["total"] == 0
    r = (await client.get("/api/brain/search", params={"q": "SSL", "since": (T0 + timedelta(days=4)).isoformat()})
         ).json()
    assert [h["message"]["id"] for h in r["archive"]["hits"]] == [ids["m7"]]
    r = (await client.get("/api/brain/search", params={"q": "SSL", "until": "2026-03-03", "staff": "jarvis"})).json()
    assert r["archive"]["total"] == 2
    assert (await client.get("/api/brain/search", params={"q": "SSL", "staff": "scout"})).json()["archive"]["total"] == 0
    r = (await client.get("/api/brain/search", params={"q": "notes", "kind": "agent_note", "scope": "memory"})).json()
    assert [h["title"] for h in r["memory"]["hits"]] == ["SEO note IBEX"] and r["archive"]["total"] == 0
    assert (await client.get("/api/brain/search", params={"limit": 51})).status_code == 422
    assert (await client.get("/api/brain/search", params={"scope": "everything"})).status_code == 422
    await as_(client, "admin")
    r = (await client.get("/api/brain/search", params={"q": "refund"})).json()
    assert {h["message"]["id"] for h in r["archive"]["hits"]} == {ids["m3"], ids["m4"]}
    r = (await client.get("/api/brain/search", params={"q": "pricing"})).json()
    assert {h["title"] for h in r["memory"]["hits"]} == {"Pricing ALPACA", "Lead pricing DINGO"}
    assert (await client.get("/api/brain/search", params={"q": "portal"})).json()["archive"]["total"] == 0
    await as_(client, "admin+")
    assert (await client.get("/api/brain/search", params={"q": "portal"})).json()["archive"]["total"] == 1


async def test_sessions_list_and_detail_by_scope(client):
    ids = await seed()

    async def listed(params=None) -> set[str]:
        r = await client.get("/api/brain/sessions", params=params or {})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == len(body["sessions"])
        return {s["source_session_id"] for s in body["sessions"]}

    await as_(client, "operator")
    assert await listed() == {"s1", "s5"}
    assert "search" not in (await client.get("/api/brain/sessions")).json()["sessions"][0]
    assert await listed({"q": "website"}) == {"s1"}
    assert await listed({"origin": "scheduled"}) == {"s5"}
    assert await listed({"label": "accomplishment"}) == {"s1"}
    assert await listed({"since": "2026-03-04"}) == {"s5"} and await listed({"until": "2026-03-04"}) == {"s1"}
    assert await listed({"staff": "sarah"}) == set() and await listed({"agent": "supervisor"}) == {"s1", "s5"}
    assert await listed({"agent": "", "origin": "", "staff": "", "label": ""}) == {"s1", "s5"}  # unset form fields
    assert (await client.get("/api/brain/sessions", params={"limit": 201})).status_code == 422
    assert (await client.get("/api/brain/sessions", params={"origin": "email"})).status_code == 422
    for key in ("s2", "s3", "s4"):
        assert (await client.get("/api/brain/sessions/%d" % ids[key])).status_code == 404, key
    r = (await client.get("/api/brain/sessions/%d" % ids["s5"])).json()
    assert [m["id"] for m in r["messages"]] == [ids["m7"]] and r["total"] == 1 and r["hidden"] == 1
    r = (await client.get("/api/brain/sessions/%d" % ids["s1"])).json()
    assert r["session"]["imported_messages"] == 2 and r["session"]["labels"] == {"accomplishment": 1}
    assert [m["position"] for m in r["messages"]] == [0, 1] and r["hidden"] == 0
    assert (await client.get("/api/brain/sessions/%d?limit=1&offset=1" % ids["s1"])).json()["messages"][0]["id"] == \
        ids["m2"]
    assert (await client.get("/api/brain/sessions/%d?limit=501" % ids["s1"])).status_code == 422
    await as_(client, "admin")
    assert await listed() == {"s1", "s2", "s5"}
    r = (await client.get("/api/brain/sessions/%d" % ids["s5"])).json()
    assert r["total"] == 2 and r["hidden"] == 0
    assert (await client.get("/api/brain/sessions/%d" % ids["s3"])).status_code == 404
    await as_(client, "admin+")
    assert await listed() == {"s1", "s2", "s3", "s4", "s5"}
    r = (await client.get("/api/brain/sessions/%d" % ids["s3"])).json()
    assert r["session"]["sensitivity"] == "restricted" and r["session"]["title"] == ""
    assert [(m["withheld"], m["content"]) for m in r["messages"]] == [(True, "")]


async def test_withholding_a_message_recomputes_its_session(client):
    ids = await seed()
    await as_(client, "admin")
    before = (await client.get("/api/brain/sessions/%d" % ids["s1"])).json()["session"]
    assert "OCELOT" in before["summary"] and before["labels"] == {"accomplishment": 1}
    r = await client.post("/api/brain/messages/%d/withhold" % ids["m2"])
    assert r.status_code == 200, r.text
    message, sess = r.json()["message"], r.json()["session"]
    assert message["withheld"] is True and message["content"] == "" and message["labels"] == []
    assert message["flags"]["withheld_by_admin"]["note"]
    assert sess["imported_messages"] == 1 and sess["withheld_messages"] == 1 and sess["labels"] == {}
    assert "OCELOT" not in sess["summary"] and "NARWHAL" in sess["summary"]
    assert (await client.post("/api/brain/messages/%d/withhold" % ids["m2"])).status_code == 400
    assert (await client.get("/api/brain/search", params={"q": "OCELOT"})).json()["archive"]["total"] == 0
    assert (await client.post("/api/brain/messages/%d/withhold" % ids["m6"])).status_code == 404  # Credit Repair
    async with session_scope() as s:
        row = await s.get(ArchiveMessage, ids["m2"])
        assert row.content == "" and row.withheld and row.tool_calls == []
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "archive.message_withheld"))
        assert entry.detail == {"message": ids["m2"], "session": ids["s1"], "imported_messages": 1,
                                "withheld_messages": 1, "tombstone": True, "copies": [], "versions_blanked": 0,
                                "rollback_copies_blanked": 0, "answers_scrubbed": 0, "via": "api",
                                "also_withheld": [], "held_for_review": [], "identical_withheld": 0,
                                "keys_tombstoned": 1, "title_hidden": False}


async def test_promoting_a_message_keeps_the_highest_sensitivity(client):
    ids = await seed()
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % ids["m2"], json={"kind": "accomplishment",
                                                                            "title": "SSL renewed"})
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["status"] == "pending_review" and item["origin"] == "promoted" and item["import_run_id"] is None
    assert item["source_ref"] == "JARVIS session s1 · message 1" and item["sensitivity"] == "general"
    assert item["agent"] == "supervisor" and item["flags"]["message_id"] == ids["m2"] and "OCELOT" in item["body"]
    r = await client.post("/api/brain/messages/%d/promote" % ids["m1"], json={"kind": "knowledge", "title": "t",
                                                                            "agent": ""})
    assert r.status_code == 201 and r.json()["item"]["agent"] == ""
    assert (await client.post("/api/brain/messages/%d/promote" % ids["m4"],
                              json={"kind": "knowledge", "title": "t"})).status_code == 404  # customer session
    assert (await client.post("/api/brain/messages/%d/promote" % ids["m2"],
                              json={"kind": "instruction", "title": "t"})).status_code == 403
    assert (await client.post("/api/brain/messages/%d/promote" % ids["m2"],
                              json={"kind": "knowledge", "title": "t", "agent": "credit_repair"})).status_code == 403
    assert (await client.post("/api/brain/messages/%d/promote" % ids["m2"],
                              json={"kind": "rumour", "title": "t"})).status_code == 400
    await as_(client, "admin")
    # A general message in a customer session becomes a customer item.
    r = await client.post("/api/brain/messages/%d/promote" % ids["m4"], json={"kind": "business",
                                                                            "title": "Refund rule"})
    assert r.status_code == 201 and r.json()["item"]["sensitivity"] == "customer"
    assert r.json()["item"]["agent"] == "communications"
    await as_(client, "admin+")
    r = await client.post("/api/brain/messages/%d/promote" % ids["m5"], json={"kind": "knowledge", "title": "t"})
    assert r.status_code == 400 and "withheld" in r.json()["detail"]
    await as_(client, "operator")
    assert {i["title"] for i in (await client.get("/api/brain/items?q=OCELOT")).json()["items"]} == {"SSL renewed"}


# --- Conflicts ---------------------------------------------------------------------------------------------------


FAQ_KEY = "jarvis:file:profile/knowledge/sarah/faq.md"
OLD_SHA, NEW_SHA = "a" * 64, "b" * 64


async def seed_conflicts() -> dict[str, int]:
    """Run 0 imported an FAQ and a reviewed workflow draft; run 1 raised one conflict of every kind (plus one about
    a customer item)."""
    ids: dict[str, int] = {}
    async with session_scope() as s:
        r0 = ImportRun(status="completed", created_by="importer (cli)", started_at=T0, finished_at=T0)
        s.add(r0)
        await s.flush()
        r1 = ImportRun(status="completed", created_by="importer (cli)")
        s.add(r1)
        await s.flush()
        x = await _item(s, title="Refund policy PELICAN", body="Refunds within 30 days.", approve=True)
        y = await _item(s, title="Refund policy PELICAN", body="Refunds within 14 days.", origin="import",
                        import_run_id=r1.id, flags={"conflict": True})
        p = await _item(s, title="FAQ ROBIN", body="Old FAQ text.", origin="import", import_run_id=r0.id,
                        source_key=FAQ_KEY, source_sha256=OLD_SHA, source_at=T0, approve=True)
        q = await _item(s, title="FAQ ROBIN v2", body="New FAQ text.", origin="import", import_run_id=r1.id,
                        source_sha256=NEW_SHA, source_at=T0 + timedelta(days=1), flags={"replaces_source_key": FAQ_KEY})
        ins = await _item(s, title="Autopilot rule SWIFT", body="Always reply to leads the same day.",
                          kind="instruction", agent="supervisor", origin="import", import_run_id=r1.id,
                          status="quarantined", flags={"approval_conflict": True})
        cust = await _item(s, title="Refund policy PELICAN", body="Ask jane@example.org first.", origin="import",
                           import_run_id=r1.id)
        w = WorkflowDraft(source_key="jarvis:job:a1b2c3", name="morning-brief", staff="jarvis", agent="supervisor",
                          capability="daily_briefing", params={"hours": 24}, compatibility="partial", cron="0 8 * * *",
                          enabled_in_source=True, status="reviewed", reviewed_by="seed@test.local", reviewed_at=T0,
                          source_fp="fp-old", import_run_id=r0.id, spec="Compose the brief.", notes=["old note"])
        w2 = WorkflowDraft(source_key="jarvis:job:d4e5f6", name="roas-report", staff="bobby", agent="social_marketing",
                           capability="ads_report", compatibility="mapped", cron="0 9 * * 1", status="draft",
                           source_fp="fp-roas", import_run_id=r1.id)
        s.add_all([w, w2])
        await s.flush()
        incoming_draft = {"name": "morning-brief", "staff": "jarvis", "agent": "supervisor",
                          "capability": "daily_briefing", "params": {"hours": 12}, "compatibility": "partial",
                          "trigger": "schedule", "cron": "0 7 * * *", "schedule_display": "07:00",
                          "timezone": "America/New_York", "enabled_in_source": True, "deliver": "slack",
                          "approval": {}, "error_handling": {}, "duplicate_guard": {}, "spec": "Compose it earlier.",
                          "history": {"runs": 3}, "notes": ["new note"], "source_fp": "fp-new"}
        conflicts = {
            "c_mem": MemoryConflict(run_id=r1.id, kind="memory_item", title="Same title as an existing item: PELICAN",
                                    existing_table="memory_items", existing_id=x.id, incoming_item_id=y.id,
                                    existing={"id": x.id}, incoming={"id": y.id, "title": y.title}),
            "c_src": MemoryConflict(run_id=r1.id, kind="source_changed", title="JARVIS changed the FAQ",
                                    source_key=FAQ_KEY, existing_table="memory_items", existing_id=p.id,
                                    incoming_item_id=q.id,
                                    existing={"id": p.id, "title": p.title, "version": p.version,
                                              "source_sha256": OLD_SHA, "source_at": T0.isoformat()},
                                    incoming={"id": q.id, "title": q.title, "source_sha256": NEW_SHA}),
            "c_ins": MemoryConflict(run_id=r1.id, kind="instruction", title="JARVIS rule that bypasses approval",
                                    incoming_item_id=ins.id, incoming={"id": ins.id, "rules": ["approval_bypass"]}),
            "c_site": MemoryConflict(run_id=r1.id, kind="site", title="JARVIS watched jarvis-site.example",
                                     existing_table="sites",
                                     incoming={"url": "https://jarvis-site.example/shop", "host": "jarvis-site.example"}),
            "c_wf": MemoryConflict(run_id=r1.id, kind="workflow", title="JARVIS job morning-brief changed",
                                   existing_table="workflow_drafts", existing_id=w.id,
                                   existing={"id": w.id, "source_fp": "fp-old", "status": "reviewed"},
                                   incoming=incoming_draft),
            "c_sched": MemoryConflict(run_id=r1.id, kind="schedule", title="JARVIS job roas-report overlaps",
                                      existing={"automation": "an existing schedule"},
                                      incoming={"draft_id": w2.id, "agent": "social_marketing",
                                                "capability": "ads_report"}),
            "c_cust": MemoryConflict(run_id=r1.id, kind="memory_item", title="Same title as an existing item: PELICAN",
                                     existing_table="memory_items", existing_id=x.id, incoming_item_id=cust.id),
        }
        s.add_all(conflicts.values())
        await s.flush()
        ids.update({k: v.id for k, v in conflicts.items()})
        ids.update({"r0": r0.id, "r1": r1.id, "x": x.id, "y": y.id, "p": p.id, "q": q.id, "ins": ins.id,
                    "cust": cust.id, "w": w.id, "w2": w2.id})
    return ids


async def resolve(client, conflict_id: int, resolution: str, note: str = ""):
    return await client.post("/api/brain/conflicts/%d/resolve" % conflict_id,
                             json={"resolution": resolution, "note": note})


async def test_conflicts_are_listed_within_the_persons_scope(client):
    ids = await seed_conflicts()
    await as_(client, "operator")
    r = (await client.get("/api/brain/conflicts")).json()
    assert {c["id"] for c in r["conflicts"]} == {ids[k] for k in ("c_mem", "c_src", "c_site", "c_wf", "c_sched")}
    assert r["total"] == 5
    by_id = {c["id"]: c for c in r["conflicts"]}
    assert by_id[ids["c_src"]]["needs_admin"] is True and by_id[ids["c_wf"]]["needs_admin"] is True
    assert by_id[ids["c_mem"]]["needs_admin"] is False and by_id[ids["c_site"]]["needs_admin"] is False
    assert by_id[ids["c_mem"]]["existing_item"]["id"] == ids["x"]
    assert by_id[ids["c_mem"]]["incoming_item"]["id"] == ids["y"]
    assert (await resolve(client, ids["c_ins"], "dismissed")).status_code == 404  # quarantined item: admins only
    assert (await resolve(client, ids["c_cust"], "dismissed")).status_code == 404  # customer item: admins only
    assert (await client.get("/api/brain/conflicts?status=closed")).status_code == 422
    assert (await client.get("/api/brain/conflicts?status=")).json()["total"] == 5
    await as_(client, "admin")
    r = (await client.get("/api/brain/conflicts")).json()
    assert r["total"] == 7 and (await client.get("/api/brain/conflicts?status=keep_both")).json()["total"] == 0
    assert (await client.get("/api/brain/conflicts?status=open&limit=2")).json()["total"] == 7


async def test_resolutions_record_effects_and_a_rollback_reverts_them(client):
    ids = await seed_conflicts()
    await as_(client, "operator")
    assert (await resolve(client, ids["c_src"], "use_incoming")).status_code == 403
    assert (await resolve(client, ids["c_wf"], "use_incoming")).status_code == 403
    # memory_item, use_incoming: the existing item is archived; the incoming one stays pending.
    r = await resolve(client, ids["c_mem"], "use_incoming", "the new policy is right")
    assert r.status_code == 200, r.text
    assert r.json()["effects"] == {"items": [{"id": ids["x"], "status_before": "approved", "version_before": 2,
                                              "version_after": 3}], "sites": [], "drafts": [], "purged_items": []}
    assert r.json()["conflict"]["status"] == "use_incoming" and r.json()["conflict"]["decided_by"] == USERS["operator"][0]
    assert (await resolve(client, ids["c_mem"], "keep_existing")).status_code == 400  # decided already
    # site, use_incoming: registered paused, in production.
    r = await resolve(client, ids["c_site"], "use_incoming")
    assert r.status_code == 200, r.text
    site_id = r.json()["effects"]["sites"][0]["id"]
    # schedule overlap, keep_existing: the draft is rejected.
    r = await resolve(client, ids["c_sched"], "keep_existing")
    assert r.json()["effects"]["drafts"][0]["before"]["status"] == "draft"
    await as_(client, "admin")
    # source_changed, use_incoming: the existing item takes the new text and source hash, back to pending; the
    # incoming item is purged and the conflict keeps the record.
    r = await resolve(client, ids["c_src"], "use_incoming")
    assert r.status_code == 200, r.text
    assert r.json()["effects"]["items"] == [{"id": ids["p"], "status_before": "approved", "version_before": 2,
                                             "version_after": 3, "flags_before": {}}]
    assert r.json()["effects"]["purged_items"] == [{"id": ids["q"], "source_key": None}]
    assert r.json()["conflict"]["incoming_item_id"] is None
    # workflow, use_incoming: the draft is replaced and needs a review again.
    r = await resolve(client, ids["c_wf"], "use_incoming")
    assert r.status_code == 200, r.text
    assert r.json()["effects"]["drafts"][0]["before"]["cron"] == "0 8 * * *"
    assert r.json()["effects"]["drafts"][0]["fp_after"] == "fp-new"
    # instruction, keep_existing: the incoming item is rejected. A customer conflict dismissed: nothing changes.
    assert (await resolve(client, ids["c_ins"], "keep_existing")).status_code == 200
    r = await resolve(client, ids["c_cust"], "dismissed")
    assert r.json()["effects"] == {"items": [], "sites": [], "drafts": [], "purged_items": []}
    async with session_scope() as s:
        x, p = await s.get(MemoryItem, ids["x"]), await s.get(MemoryItem, ids["p"])
        assert x.status == "archived" and x.version == 3
        assert (p.title, p.body, p.source_sha256, p.status, p.version) == ("FAQ ROBIN v2", "New FAQ text.", NEW_SHA,
                                                                          "pending_review", 3)
        assert await s.get(MemoryItem, ids["q"]) is None and await s.get(MemoryConflict, ids["c_src"]) is not None
        site = await s.get(Site, site_id)
        assert (site.url, site.name, site.enabled, site.environment) == ("https://jarvis-site.example/shop",
                                                                         "jarvis-site.example", False, "production")
        w, w2 = await s.get(WorkflowDraft, ids["w"]), await s.get(WorkflowDraft, ids["w2"])
        assert (w.cron, w.params, w.source_fp, w.status, w.reviewed_by) == ("0 7 * * *", {"hours": 12}, "fp-new",
                                                                            "draft", "")
        assert w2.status == "rejected"
        assert (await s.get(MemoryItem, ids["ins"])).status == "rejected"
        actions = [a for (a,) in (await s.execute(select(AuditLog.action).order_by(AuditLog.id))).all()]
        assert actions.count("memory.conflict_resolved") == 7 and actions.count("site.created") == 1
    # Rolling back run 1 reverts every effect on rows that existed before it.
    url = "/api/brain/imports/%d/rollback" % ids["r1"]
    r = await client.post(url, json={"confirm": "%d" % ids["r0"]})
    assert r.status_code == 400 and r.json()["detail"] == "confirm: type the run number"
    r = await client.post(url, json={"confirm": str(ids["r1"])})
    assert r.status_code == 409 and str(ids["ins"]) in r.json()["detail"]  # a reviewed item of the run
    r = await client.post(url, json={"confirm": str(ids["r1"]), "discard_reviewed": True})
    assert r.status_code == 200, r.text
    assert r.json()["run"]["status"] == "rolled_back" and r.json()["run"]["report"]["rollback"]
    assert r.json()["rollback"]["reverted"] == {"items": 2, "sites": 1, "drafts": 1, "purged_items_reported": 1}
    async with session_scope() as s:
        x, p = await s.get(MemoryItem, ids["x"]), await s.get(MemoryItem, ids["p"])
        assert (x.status, x.version) == ("approved", 4)
        assert (p.title, p.body, p.source_sha256, p.source_at, p.status) == ("FAQ ROBIN", "Old FAQ text.", OLD_SHA, T0,
                                                                             "approved")
        changes = [v.change for v in await store.versions(s, ids["p"])]
        assert changes[0] == "reverted"
        assert await s.get(Site, site_id) is None
        w = await s.get(WorkflowDraft, ids["w"])
        assert (w.cron, w.params, w.source_fp, w.status, w.reviewed_by, w.reviewed_at) == (
            "0 8 * * *", {"hours": 24}, "fp-old", "reviewed", "seed@test.local", T0)
        assert await s.get(WorkflowDraft, ids["w2"]) is None
        for key in ("y", "ins", "cust"):
            assert await s.get(MemoryItem, ids[key]) is None, key
        assert await s.scalar(select(func.count()).select_from(MemoryConflict)) == 0
        assert await s.scalar(select(AuditLog.id).where(AuditLog.action == "brain.import_rolled_back")) is not None
    assert (await client.post(url, json={"confirm": str(ids["r1"])})).status_code == 409  # already rolled back


async def test_a_rollback_refuses_when_a_resolution_target_moved_on(client):
    ids = await seed_conflicts()
    await as_(client, "admin")
    assert (await resolve(client, ids["c_mem"], "use_incoming")).status_code == 200
    assert (await client.post("/api/brain/items/%d/review" % ids["x"], json={"decision": "reopen"})).status_code == 200
    r = await client.post("/api/brain/imports/%d/rollback" % ids["r1"],
                          json={"confirm": str(ids["r1"]), "discard_reviewed": True})
    assert r.status_code == 409 and str(ids["c_mem"]) in r.json()["detail"]
    async with session_scope() as s:
        assert (await s.get(ImportRun, ids["r1"])).status == "completed"  # nothing changed


async def test_source_changed_keep_existing_remembers_the_rejected_version(client):
    ids = await seed_conflicts()
    await as_(client, "admin")
    r = await resolve(client, ids["c_src"], "keep_existing")
    assert r.status_code == 200
    assert r.json()["effects"]["items"][0]["id"] == ids["q"]
    # The hash added to the existing item is an effect too: a rollback of run 1 takes it out again.
    assert r.json()["effects"]["items"][1] == {"id": ids["p"], "status_before": "approved", "version_before": 2,
                                               "version_after": 2, "rejected_source_sha256": NEW_SHA}
    async with session_scope() as s:
        p, q = await s.get(MemoryItem, ids["p"]), await s.get(MemoryItem, ids["q"])
        assert p.flags["rejected_source_sha256"] == [NEW_SHA] and (p.title, p.version) == ("FAQ ROBIN", 2)
        assert q.status == "rejected"


async def test_conflict_resolution_refusals(client, monkeypatch):
    ids = await seed_conflicts()
    await as_(client, "admin")
    async with session_scope() as s:
        (await s.get(WorkflowDraft, ids["w"])).status = "converted"
        s.add(Site(name="one", url="https://already.example"))
    r = await resolve(client, ids["c_wf"], "use_incoming")
    assert r.status_code == 400 and "converted" in r.json()["detail"]
    assert (await resolve(client, ids["c_wf"], "keep_existing")).status_code == 200
    # The edition's site limit.
    from app import licensing

    async def one_site(session, settings):
        return SimpleNamespace(max_sites=1, edition=SimpleNamespace(title="Personal"))

    monkeypatch.setattr(licensing, "current_entitlements", one_site)
    await as_(client, "operator")
    r = await resolve(client, ids["c_site"], "use_incoming")
    assert r.status_code == 400 and "up to 1 sites" in r.json()["detail"]
    monkeypatch.undo()
    async with session_scope() as s:
        assert (await s.get(MemoryConflict, ids["c_site"])).status == "open"
        (await s.get(MemoryConflict, ids["c_site"])).incoming = {"url": "https://already.example", "host": "already"}
    r = await resolve(client, ids["c_site"], "use_incoming")
    assert r.status_code == 400 and "already registered" in r.json()["detail"]
    assert (await resolve(client, ids["c_site"], "keep_existing")).json()["effects"]["sites"] == []
    assert (await resolve(client, 999999, "dismissed")).status_code == 404
    assert (await client.post("/api/brain/conflicts/%d/resolve" % ids["c_mem"],
                              json={"resolution": "merge"})).status_code == 422


# --- Workflow drafts -----------------------------------------------------------------------------------------------


async def seed_drafts() -> dict[str, int]:
    async with session_scope() as s:
        d1 = WorkflowDraft(source_key="jarvis:job:a1", name="morning-brief", staff="jarvis", agent="supervisor",
                           capability="daily_briefing", params={"hours": 24}, compatibility="partial", cron="0 8 * * *",
                           enabled_in_source=True, spec="Compose the brief.")
        d2 = WorkflowDraft(source_key="jarvis:job:b2", name="custom-watch", staff="jarvis", agent="supervisor",
                           capability="", compatibility="reference_only", cron="0 * * * *", enabled_in_source=True)
        d3 = WorkflowDraft(source_key="jarvis:job:c3", name="weekly-sweep", staff="scout", agent="seo_content",
                           capability="keyword_brief", params={}, compatibility="partial", cron="",
                           enabled_in_source=False)
        s.add_all([d1, d2, d3])
        await s.flush()
        return {"d1": d1.id, "d2": d2.id, "d3": d3.id}


async def test_workflow_drafts_edit_review_and_convert_to_a_disabled_schedule(client, settings):
    ids = await seed_drafts()
    await as_(client, "operator")
    drafts = (await client.get("/api/brain/workflows")).json()["drafts"]
    assert [d["name"] for d in drafts] == ["custom-watch", "morning-brief", "weekly-sweep"]
    assert all(d["schedule_exists"] is False and d["status"] == "draft" for d in drafts)
    r = (await client.get("/api/brain/workflows/%d" % ids["d1"])).json()
    assert r["draft"]["spec"] == "Compose the brief." and r["schedule_exists"] is False
    assert (await client.get("/api/brain/workflows/999999")).status_code == 404
    # Edits (admin): parameters through the capability's model, cron through valid_cron.
    await as_(client, "admin")
    patch = "/api/brain/workflows/%d" % ids["d3"]
    r = await client.patch(patch, json={"params": {"topic": "x"}})
    assert r.status_code == 400 and "topic" in r.json()["detail"]
    assert (await client.patch(patch, json={"params": {"topic": "home care", "budget": 5}})).status_code == 400
    r = await client.patch(patch, json={"params": {"topic": "home care websites"}})
    assert r.status_code == 200 and r.json()["draft"]["params"] == {"topic": "home care websites", "location": ""}
    for bad in ("every monday", "61 * * * *", "* * * * * *"):
        assert (await client.patch(patch, json={"cron": bad})).status_code == 400, bad
    assert (await client.patch(patch, json={"cron": "0 9 * * 1"})).json()["draft"]["cron"] == "0 9 * * 1"
    assert (await client.patch(patch, json={})).status_code == 400
    assert (await client.patch("/api/brain/workflows/%d" % ids["d2"], json={"params": {}})).status_code == 400
    # Review (operator).
    await as_(client, "operator")
    review_url = "/api/brain/workflows/%d/review"
    r = await client.post(review_url % ids["d1"], json={"decision": "reviewed", "note": "matches the n8n brief"})
    assert r.status_code == 200 and r.json()["draft"]["status"] == "reviewed"
    assert r.json()["draft"]["reviewed_by"] == USERS["operator"][0]
    assert r.json()["draft"]["notes"][-1].endswith("matches the n8n brief")
    assert (await client.post(review_url % ids["d1"], json={"decision": "reviewed"})).status_code == 400
    assert (await client.post(review_url % ids["d1"], json={"decision": "approved"})).status_code == 422
    assert (await client.post(review_url % ids["d2"], json={"decision": "rejected"})).json()["draft"]["status"] == \
        "rejected"
    assert (await client.post(review_url % ids["d2"], json={"decision": "draft"})).json()["draft"]["status"] == "draft"
    # Conversion refusals (admin).
    await as_(client, "admin")
    convert = "/api/brain/workflows/%d/convert"

    async def refused(draft: str, words: str, body=None) -> None:
        r = await client.post(convert % ids[draft], json=body if body is not None else {"jarvis_job_is_off": True})
        assert r.status_code == 400 and words in r.json()["detail"], r.text

    await refused("d2", "reference-only")
    await refused("d3", "only a reviewed draft")
    await refused("d1", "still switched on", {"jarvis_job_is_off": False})
    await refused("d1", "still switched on", {})
    async with session_scope() as s:
        s.add(Schedule(name="jarvis-morning-brief", agent="supervisor", capability="daily_briefing", cron="0 6 * * *",
                       enabled=False))
    await refused("d1", "already exists")
    async with session_scope() as s:
        await s.delete(await s.scalar(select(Schedule).where(Schedule.name == "jarvis-morning-brief")))
        d1 = await s.get(WorkflowDraft, ids["d1"])
        d1.cron, d1.params = "", {"hours": 999}
    await refused("d1", "no valid cron")
    async with session_scope() as s:
        (await s.get(WorkflowDraft, ids["d1"])).cron = "0 8 * * *"
    await refused("d1", "parameters do not suit")
    async with session_scope() as s:
        (await s.get(WorkflowDraft, ids["d1"])).params = {"hours": 24}
    # Converted: a disabled schedule, and the draft points at it.
    r = await client.post(convert % ids["d1"], json={"jarvis_job_is_off": True})
    assert r.status_code == 200, r.text
    schedule, draft = r.json()["schedule"], r.json()["draft"]
    assert (schedule["name"], schedule["agent"], schedule["capability"], schedule["params"], schedule["cron"],
            schedule["enabled"], schedule["next_run_at"]) == ("jarvis-morning-brief", "supervisor", "daily_briefing",
                                                              {"hours": 24}, "0 8 * * *", False, None)
    assert schedule["description"] == "From JARVIS job morning-brief (draft %d); created disabled" % ids["d1"]
    assert draft["status"] == "converted" and draft["schedule_id"] == schedule["id"]
    assert (await client.get("/api/brain/workflows/%d" % ids["d1"])).json()["schedule_exists"] is True
    await refused("d1", "already converted")
    assert (await client.patch("/api/brain/workflows/%d" % ids["d1"], json={"cron": "0 9 * * *"})).status_code == 400
    assert (await client.post(review_url % ids["d1"], json={"decision": "rejected"})).status_code == 400
    # A JARVIS job switched off in JARVIS needs no confirmation.
    await as_(client, "operator")
    await client.post(review_url % ids["d3"], json={"decision": "reviewed"})
    await as_(client, "admin")
    r = await client.post(convert % ids["d3"], json={})
    assert r.status_code == 200 and r.json()["schedule"]["enabled"] is False
    # The scheduler never picks up a converted draft's schedule: it is disabled until a person enables it.
    from app.worker.scheduler import enqueue_due

    assert await enqueue_due(settings, now=datetime.now(UTC) + timedelta(days=8)) == 0
    async with session_scope() as s:
        rows = (await s.scalars(select(Schedule).where(Schedule.name.like("jarvis-%")))).all()
        assert len(rows) == 2 and all(not r.enabled and r.next_run_at is None for r in rows)
        assert await s.scalar(select(func.count()).select_from(Task)) == 0
        details = [d for (d,) in (await s.execute(select(AuditLog.detail).where(
            AuditLog.action == "workflow_draft.converted").order_by(AuditLog.id))).all()]
    assert details[0] == {"draft": ids["d1"], "schedule": schedule["id"], "enabled": False}
    listed = {s["name"]: s for s in (await client.get("/api/schedules")).json()["schedules"]}
    assert listed["jarvis-morning-brief"]["enabled"] is False


# --- Agents and skills ----------------------------------------------------------------------------------------------


async def test_agents_say_what_they_read_and_where_and_skills_follow_the_scope(client):
    await seed()
    await as_(client, "viewer")
    agents = {a["agent"]: a for a in (await client.get("/api/brain/agents")).json()["agents"]}
    assert list(agents) == ["supervisor", "wordpress_security", "seo_content", "social_marketing",
                            "server_management", "communications", "monitoring", "credit_repair"]
    assert agents["supervisor"]["used_by"] == ["task results (reference notes)", "answers about JARVIS's history"]
    for key in ("seo_content", "social_marketing", "communications"):
        assert agents[key]["used_by"] == ["task results (reference notes)", "AI drafting prompts (when allowed)"]
    assert agents["monitoring"]["used_by"] == ["task results (reference notes)"]
    assert agents["credit_repair"]["used_by"] == []
    assert agents["supervisor"]["reads_other_agents"] is True and agents["credit_repair"]["archive"] == "none"
    assert agents["supervisor"]["items"] == {"approved": 1, "by_kind": {"business": 1}}
    assert all(a["sessions"] is None for a in agents.values())
    assert (await client.get("/api/brain/skills")).json() == {"skills": []}
    await as_(client, "operator")
    agents = {a["agent"]: a for a in (await client.get("/api/brain/agents")).json()["agents"]}
    assert agents["supervisor"]["items"]["approved"] == 2 and agents["supervisor"]["sessions"] == 2
    assert agents["seo_content"]["items"] == {"approved": 2, "by_kind": {"business": 1, "skill": 1}}
    assert agents["communications"]["sessions"] == 0
    skills = (await client.get("/api/brain/skills")).json()["skills"]
    assert [s["title"] for s in skills] == ["Keyword research HERON"] and skills[0]["needs_admin"] is True
    await as_(client, "admin")
    agents = {a["agent"]: a for a in (await client.get("/api/brain/agents")).json()["agents"]}
    assert agents["supervisor"]["items"]["approved"] == 3 and agents["communications"]["sessions"] == 1
    assert agents["credit_repair"]["items"]["approved"] == 1
    await as_(client, "admin+")
    agents = {a["agent"]: a for a in (await client.get("/api/brain/agents")).json()["agents"]}
    assert agents["credit_repair"]["items"]["approved"] == 2 and agents["credit_repair"]["sessions"] == 0


# --- Import runs ----------------------------------------------------------------------------------------------------


async def test_import_runs_list_and_detail(client):
    ids = await seed_conflicts()
    async with session_scope() as s:
        run = await s.get(ImportRun, ids["r1"])
        run.report, run.counts = {"totals": {"files": 3}}, {"records": 3}
        from app.models import ImportRecord

        s.add_all([ImportRecord(run_id=run.id, source_key="k%d" % n, record_type=t, action=a)
                   for n, (t, a) in enumerate((("file", "imported"), ("file", "excluded"), ("job", "imported")))])
        await store.review_item(s, await s.get(MemoryItem, ids["y"]), decision="approve", actor="seed@test.local")
    await as_(client, "operator")
    runs = (await client.get("/api/brain/imports")).json()["runs"]
    assert [r["id"] for r in runs] == [ids["r1"], ids["r0"]] and "report" not in runs[0]
    assert runs[0]["counts"] == {"records": 3}
    r = (await client.get("/api/brain/imports/%d" % ids["r1"])).json()
    assert r["run"]["report"] == {"totals": {"files": 3}}
    assert r["records_by_action"] == {"imported": 2, "excluded": 1} and r["records_by_type"] == {"file": 2, "job": 1}
    assert r["can_roll_back"] is True and r["reviewed_items"] == 1
    assert (await client.get("/api/brain/imports/999999")).status_code == 404
    await as_(client, "admin")
    r = await client.post("/api/brain/imports/999999/rollback", json={"confirm": "999999"})
    assert r.status_code == 404
    assert (await client.post("/api/brain/imports/%d/rollback" % ids["r1"], json={})).status_code == 400
    await as_(client, "viewer")
    latest = (await client.get("/api/brain/overview")).json()["imports"]["latest"]
    assert set(latest) == {"id", "status", "started_at", "finished_at", "rolled_back_at", "counts"}


# --- Export ---------------------------------------------------------------------------------------------------------


async def test_export_holds_approved_general_items_unless_customer_items_are_asked_for(client):
    ids = await seed()
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % ids["G"])  # deleted items are never exported
    r = await client.get("/api/brain/export")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment; filename=")
    data = r.json()
    assert data["format"] == "jarvis-brain-memory-export" and data["include_customer"] is False
    assert [i["title"] for i in data["items"]] == ["Pricing ALPACA"] and data["count"] == 1
    data = (await client.get("/api/brain/export", params={"include_customer": True})).json()
    assert {i["title"] for i in data["items"]} == {"Pricing ALPACA", "Lead pricing DINGO"}
    await as_(client, "admin+")
    data = (await client.get("/api/brain/export")).json()
    assert {i["title"] for i in data["items"]} == {"Pricing ALPACA", "Dispute cadence FERRET"}
    async with session_scope() as s:
        details = [d for (d,) in (await s.execute(select(AuditLog.detail).where(
            AuditLog.action == "memory.exported").order_by(AuditLog.id))).all()]
    assert details == [{"count": 1, "include_customer": False}, {"count": 2, "include_customer": True},
                       {"count": 2, "include_customer": False}]


# --- Audit entries ---------------------------------------------------------------------------------------------------


async def test_audit_entries_carry_ids_kinds_statuses_and_counts_never_text(client, settings):
    ids = await seed()
    cids = await seed_conflicts()
    dids = await seed_drafts()
    await as_(client, "admin+")
    calls = [
        ("POST", "/api/brain/items", {"kind": "business", "title": "Opening hours QUAIL", "body": "QUAIL 9 to 5."}),
        ("PATCH", "/api/brain/items/%d" % ids["A"], {"title": "Pricing QUAIL", "body": "QUAIL text", "note": "QUAIL"}),
        ("POST", "/api/brain/items/%d/review" % ids["B"], {"decision": "approve", "note": "QUAIL note"}),
        ("DELETE", "/api/brain/items/%d" % ids["H"], None),
        ("POST", "/api/brain/items/%d/restore" % ids["H"], None),
        ("DELETE", "/api/brain/items/%d" % ids["H"], None),
        ("DELETE", "/api/brain/items/%d/purge" % ids["H"], None),
        ("POST", "/api/brain/items/%d/versions/1/redact" % ids["A"], None),
        ("POST", "/api/brain/messages/%d/withhold" % ids["m2"], None),
        ("POST", "/api/brain/messages/%d/promote" % ids["m1"], {"kind": "knowledge", "title": "QUAIL promoted"}),
        ("POST", "/api/brain/conflicts/%d/resolve" % cids["c_src"], {"resolution": "use_incoming", "note": "QUAIL"}),
        ("POST", "/api/brain/conflicts/%d/resolve" % cids["c_site"], {"resolution": "use_incoming"}),
        ("POST", "/api/brain/conflicts/%d/resolve" % cids["c_mem"], {"resolution": "use_incoming"}),
        ("PATCH", "/api/brain/workflows/%d" % dids["d3"], {"params": {"topic": "QUAIL websites"}}),
        ("POST", "/api/brain/workflows/%d/review" % dids["d1"], {"decision": "reviewed", "note": "QUAIL"}),
        ("POST", "/api/brain/workflows/%d/convert" % dids["d1"], {"jarvis_job_is_off": True}),
        ("GET", "/api/brain/export?include_customer=true", None),
        ("POST", "/api/brain/imports/%d/rollback" % cids["r1"], {"confirm": str(cids["r1"]), "discard_reviewed": True}),
    ]
    for method, url, body in calls:
        r = await client.request(method, url, json=body) if body is not None else await client.request(method, url)
        assert r.status_code in (200, 201), (method, url, r.text)
    async with session_scope() as s:
        entries = (await s.scalars(select(AuditLog).where(AuditLog.action.not_like("auth.%")))).all()
    assert len(entries) >= len(calls)
    words = PLANTED + ("QUAIL", "PELICAN", "ROBIN", "SWIFT", "PUFFIN", "jarvis-site.example", "morning-brief",
                       "Refunds", "FAQ")
    for e in entries:
        text = json.dumps({"action": e.action, "target": e.target_type, "id": e.target_id, "detail": e.detail})
        assert not [w for w in words if w in text], (e.action, text)


# --- The brain_ai_context customer setting -----------------------------------------------------------------------------


async def test_the_settings_route_offers_and_accepts_brain_ai_context(client):
    await as_(client, "admin")
    spec = (await client.get("/api/settings")).json()["settings"]["brain_ai_context"]
    assert spec == {"value": "off", "label": "JARVIS Brain in AI prompts", "description": spec["description"],
                    "kind": "choice", "choices": ["off", "approved_general"]}
    async with session_scope() as s:
        assert await store.ai_context_allowed(s) is False
    r = await client.patch("/api/settings", json={"brain_ai_context": "approved_general"})
    assert r.status_code == 200 and r.json()["updated"] == {"brain_ai_context": "approved_general"}
    async with session_scope() as s:
        assert await store.ai_context_allowed(s) is True
    assert (await client.get("/api/brain/overview")).json()["ai_context"] == "approved_general"
    assert (await client.get("/api/settings")).json()["settings"]["brain_ai_context"]["value"] == "approved_general"
    r = await client.patch("/api/settings", json={"brain_ai_context": "everything"})
    assert r.status_code == 400 and "brain_ai_context" in r.json()["detail"]
    await as_(client, "operator")
    assert (await client.patch("/api/settings", json={"brain_ai_context": "off"})).status_code == 403
    await as_(client, "admin")
    await client.patch("/api/settings", json={"brain_ai_context": "off"})
    async with session_scope() as s:
        assert await store.ai_context_allowed(s) is False


# --- A real import of the synthetic JARVIS installation, through the API ---------------------------------------------


FIXTURE = Path(__file__).resolve().parents[3] / "migration" / "jarvis" / "tests" / "make_fixture.py"


def build_fixture(root: Path) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("jarvis_make_fixture_for_api", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(str(root))


async def test_an_imported_jarvis_reviewed_converted_and_rolled_back_through_the_api(client, tmp_path, settings):
    fx = build_fixture(tmp_path)
    result = await importer.run(importer.SourcePaths(db=fx["db"], profile=fx["profile"], content=fx["content"]),
                                importer.Selection(all=True), actor="importer (test)", heartbeat_seconds=3600)
    run_id = result["run_id"]
    await as_(client, "viewer")
    overview = (await client.get("/api/brain/overview")).json()
    assert overview["imports"]["runs"] == 1 and overview["imports"]["latest"]["status"] == "completed"
    assert overview["memory"]["total"] == 0  # nothing is approved yet
    await as_(client, "operator")
    detail = (await client.get("/api/brain/imports/%d" % run_id)).json()
    assert detail["records_by_action"]["imported"] > 0 and detail["can_roll_back"] is True
    assert detail["reviewed_items"] == 0
    assert all(c["kind"] != "instruction" for c in (await client.get("/api/brain/conflicts")).json()["conflicts"])
    sessions = (await client.get("/api/brain/sessions?limit=200")).json()
    assert sessions["total"] > 0 and all(s["sensitivity"] == "general" for s in sessions["sessions"])
    drafts = {d["name"]: d for d in (await client.get("/api/brain/workflows")).json()["drafts"]}
    brief = drafts["morning-brief"]
    assert brief["status"] == "draft" and brief["enabled_in_source"] is True and brief["cron"] == "0 8 * * *"
    assert (await client.post("/api/brain/workflows/%d/review" % brief["id"],
                              json={"decision": "reviewed"})).status_code == 200
    await as_(client, "admin")
    conflicts = (await client.get("/api/brain/conflicts")).json()["conflicts"]
    assert any(c["kind"] == "instruction" and c["needs_admin"] for c in conflicts)
    quarantined = (await client.get("/api/brain/items?status=quarantined")).json()
    assert quarantined["total"] > 0 and all(i["needs_admin"] for i in quarantined["items"])
    r = await client.post("/api/brain/workflows/%d/convert" % brief["id"], json={"jarvis_job_is_off": False})
    assert r.status_code == 400 and "still switched on" in r.json()["detail"]
    r = await client.post("/api/brain/workflows/%d/convert" % brief["id"], json={"jarvis_job_is_off": True})
    assert r.status_code == 200, r.text
    schedule_id = r.json()["schedule"]["id"]
    assert r.json()["schedule"]["enabled"] is False
    from app.worker.scheduler import enqueue_due

    assert await enqueue_due(settings, now=datetime.now(UTC) + timedelta(days=2)) == 0
    # Rolling the run back keeps the converted draft and its disabled schedule (they belong to the Command Center).
    r = await client.post("/api/brain/imports/%d/rollback" % run_id, json={"confirm": str(run_id)})
    assert r.status_code == 200, r.text
    assert r.json()["rollback"]["kept_converted_drafts"] == [brief["id"]]
    async with session_scope() as s:
        schedule = await s.get(Schedule, schedule_id)
        assert schedule is not None and schedule.enabled is False
        assert await s.scalar(select(func.count()).select_from(MemoryItem)) == 0
        assert await s.scalar(select(func.count()).select_from(ArchiveSession)) == 0
        assert await s.scalar(select(func.count()).select_from(Task)) == 0
    assert (await client.get("/api/brain/overview")).json()["imports"]["latest"]["status"] == "rolled_back"
    assert os.path.exists(fx["db"])


# --- Tags, Credit Repair promotion, purge after a decision, whole-day dates ---------------------------------------


async def test_tags_cannot_hold_secrets_contacts_or_restricted_data(client):
    await as_(client, "operator")
    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # built here: no token-shaped literal in Git
    for tag in ("jane.doe@example.org", "SSN 219-09-9999", "+1 555 765 4321", token):
        r = await client.post("/api/brain/items", json={"kind": "business", "title": "Tagged", "body": "Text.",
                                                        "tags": [tag]})
        assert r.status_code == 400 and tag not in r.text, tag
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "Tagged", "body": "Text.",
                                                    "tags": ["pricing"]})
    assert r.status_code == 201
    r = await client.patch("/api/brain/items/%d" % r.json()["item"]["id"], json={"tags": ["jane.doe@example.org"]})
    assert r.status_code == 400


async def test_credit_repair_conversation_text_stays_in_credit_repair(client):
    ids = await seed()
    await as_(client, "admin+")
    url = "/api/brain/messages/%d/promote" % ids["m6"]
    r = await client.post(url, json={"kind": "knowledge", "title": "Portal", "agent": ""})
    assert r.status_code == 400 and "stays in Credit Repair" in r.json()["detail"]
    r = await client.post(url, json={"kind": "knowledge", "title": "Portal", "agent": "credit_repair"})
    assert r.status_code == 201 and r.json()["item"]["agent"] == "credit_repair"


async def test_purge_refuses_an_item_whose_conflict_decision_a_rollback_reverts(client):
    ids = await seed_conflicts()
    await as_(client, "admin")
    assert (await resolve(client, ids["c_mem"], "use_incoming")).status_code == 200
    assert (await client.delete("/api/brain/items/%d" % ids["y"])).status_code == 200
    r = await client.delete("/api/brain/items/%d/purge" % ids["y"])
    assert r.status_code == 400 and "import run %d" % ids["r1"] in r.json()["detail"]
    r = await client.post("/api/brain/imports/%d/rollback" % ids["r1"],
                          json={"confirm": str(ids["r1"]), "discard_reviewed": True})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        assert (await s.get(MemoryItem, ids["x"])).status == "approved"  # the decision was reverted


async def test_purging_a_newer_copy_of_a_changed_file_rejects_that_copy_for_good(client):
    """The purged copy is kept out by its keyed fingerprint (identity.remember_purge), never by a plain SHA-256 of its
    bytes on the item it would replace (0.6.2 review 2, S16: that hash was returned to every reader of the item and
    confirms guesses of the purged text offline); test_brain_review2_062.py imports it again to show it stays out."""
    from app.brain import identity
    from app.models import BrainPurgedContent

    ids = await seed_conflicts()
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d" % ids["q"])).status_code == 200
    assert (await client.delete("/api/brain/items/%d/purge" % ids["q"])).status_code == 200
    async with session_scope() as s:
        assert "rejected_source_sha256" not in ((await s.get(MemoryItem, ids["p"])).flags or {})
        fps = list((await s.scalars(select(BrainPurgedContent.fp))).all())
    assert fps == [identity.purge_fp(identity.purge_key(), NEW_SHA)] and NEW_SHA not in fps[0]


async def test_a_date_alone_covers_the_whole_day(client):
    ids = await seed()
    await as_(client, "operator")
    day = (T0 + timedelta(days=4)).date().isoformat()
    r = (await client.get("/api/brain/sessions", params={"since": day, "until": day})).json()
    assert {s["source_session_id"] for s in r["sessions"]} == {"s5"}
    r = (await client.get("/api/brain/search", params={"q": "SSL", "scope": "archive", "since": day,
                                                       "until": day})).json()
    assert [h["message"]["id"] for h in r["archive"]["hits"]] == [ids["m7"]]
    assert (await client.get("/api/brain/search", params={"until": "not a date"})).status_code == 422
