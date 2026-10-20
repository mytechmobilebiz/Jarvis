"""Imported JARVIS content is reference data, never authority: instructions that would bypass the Command Center's
approvals are quarantined, only an admin can approve them, they never reach an AI prompt even when approved, the
import changes no setting, agent state, schedule or approval, and a send still waits for a person's approval."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.brain import AGENTS
from app.brain import importer as imp
from app.brain import store
from app.db import session_scope
from app.models import AgentState, AppSetting, Approval, MemoryItem, OutboxEntry, Schedule, User, WorkflowDraft
from app.security import CSRF_HEADER, CSRF_VALUE, hash_password

from .. import brain_jarvis as bj
from .test_agents import approve_all, run_task

pytestmark = pytest.mark.usefixtures("clean_db")

BYPASS = ("without asking", "pre-approved")
OPERATOR = ("operator@test.local", "operator-password")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bj.build(tmp_path_factory.mktemp("jarvis") / "fx")


async def _authority() -> dict[str, object]:
    """Everything that decides what may run without a person: none of it may change with an import."""
    async with session_scope() as s:
        return {
            "settings": sorted((r.key, str(r.value)) for r in (await s.scalars(select(AppSetting))).all()),
            "agents": sorted((r.name, r.enabled) for r in (await s.scalars(select(AgentState))).all()),
            "schedules": await s.scalar(select(func.count()).select_from(Schedule)),
            "approvals": sorted((r.id, r.status) for r in (await s.scalars(select(Approval))).all()),
        }


async def _bypass_items() -> list[MemoryItem]:
    async with session_scope() as s:
        items = (await s.scalars(select(MemoryItem).where(MemoryItem.origin == "import"))).all()
    return [i for i in items if any(p in i.body.lower() for p in BYPASS)]


async def test_imported_approval_bypass_never_overrides_command_center_approvals(built, admin, make_worker):
    before = await _authority()
    result = await imp.run(bj.paths(built), imp.Selection(all=True), actor="admin@test.local")
    assert (await imp.verify(result["run_id"], bj.paths(built)))["passed"]
    assert await _authority() == before

    # Every imported text that would let JARVIS act without asking is quarantined and flagged.
    bypass = await _bypass_items()
    assert {b.source_ref.split("#")[0] for b in bypass} >= {"SOUL.md", "operations.md", "knowledge/sarah/leads.md"}
    for item in bypass:
        assert item.status == "quarantined" and item.flags.get("instruction_like"), item.source_ref
        assert item.kind != "instruction" or item.flags.get("approval_conflict") is True

    # Nothing imported was enabled or scheduled.
    async with session_scope() as s:
        drafts = (await s.scalars(select(WorkflowDraft))).all()
        assert drafts and {d.status for d in drafts} == {"draft"} and not any(d.schedule_id for d in drafts)
        s.add(User(email=OPERATOR[0], password_hash=hash_password(OPERATOR[1]), role="operator"))

    # An operator can neither see nor approve them; an admin can approve one (a deliberate, audited act)...
    target = bypass[0]
    admin_cookies = dict(admin.cookies)
    admin.cookies.clear()
    r = await admin.post("/api/auth/login", json={"email": OPERATOR[0], "password": OPERATOR[1]})
    assert r.status_code == 200, r.text
    admin.headers[CSRF_HEADER] = CSRF_VALUE
    r = await admin.post("/api/brain/items/%d/review" % target.id, json={"decision": "approve"})
    assert r.status_code in (403, 404), r.text
    async with session_scope() as s:
        assert (await s.get(MemoryItem, target.id)).status == "quarantined"
    admin.cookies.clear()
    for name, value in admin_cookies.items():
        admin.cookies.set(name, value)
    for item in bypass:
        r = await admin.post("/api/brain/items/%d/review" % item.id, json={"decision": "approve"})
        assert r.status_code == 200, r.text

    # ...and even approved, with memory allowed in prompts, no agent's prompt ever carries it.
    async with session_scope() as s:
        s.add(AppSetting(key=store.AI_CONTEXT_KEY, value="approved_general"))
    async with session_scope() as s:
        for agent in AGENTS:
            block = await store.AgentMemory(s, agent).prompt_block("send proposals email approval asking")
            assert not any(p in block.lower() for p in BYPASS), agent

    # A send still stops for a person, exactly as before the import.
    r = await admin.post("/api/leads", json={"name": "Pat", "company": "Example Co", "email": "pat@example.test",
                                              "service_interest": "website redesign", "message": "New site please"})
    lead_id = r.json()["lead"]["id"]
    drafted = await run_task(admin, make_worker, "communications", "draft_proposal", {"lead_id": lead_id})
    assert drafted["status"] == "succeeded", drafted["error"]
    send = await run_task(admin, make_worker, "communications", "send_proposal", {"lead_id": lead_id})
    assert send["status"] == "awaiting_approval"
    async with session_scope() as s:
        assert (await s.scalars(select(OutboxEntry))).all() == []
    assert await approve_all(admin) == 1
