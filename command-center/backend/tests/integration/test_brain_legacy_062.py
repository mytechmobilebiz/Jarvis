"""JARVIS Brain 0.6.2 plan steps 7 and 8: the legacy lock, the legacy reset, version 3 files and version redactions
through a restore.

JARVIS Brain data that an importer older than 1.1.0 (Command Center 0.6.0 or 0.6.1) stored is locked by migration 0008
(brain_maintenance 'legacy_brain_data'): no reader shows it, an admin included; the importer, Settle, the decisions
export and the withhold API refuse with a message that names the reset. brain-import legacy-reset writes every
decision to a version 5 file, rolls the legacy runs back and lifts the lock; the same backup imported again with that
file, then Settle, brings back every decision. Here a 0.6.2 import is made legacy by its importer version (what 0008
finds) and a withhold is made unbound (what 0.6.1 stored); test_brain_contract_062.py runs the real 0.6.1 release.
Synthetic data only."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select, text

from app.brain import identity, review, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, AuditLog, BrainTombstone, ImportRun, MemoryItem, MemoryItemVersion

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_agents import allow_ai_context, ask
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_decisions_v4 import restore_point  # noqa: F401 - a fixture
from .test_brain_schema import needs_docker
from .test_brain_withhold_identity import (
    DAY,
    T,
    conversation,
    leak_free,
    message_of,
    row_with,
    secret,
    stones,
    withhold,
)

pytestmark = pytest.mark.usefixtures("clean_db")

ALL = imp.Selection(all=True)
ROLES = ("viewer", "operator", "operator+", "admin", "admin+")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def _world(fx: dict[str, str], mark: str, *, uid: bool = True) -> dict[str, str]:
    """A conversation whose answer an admin withheld (with 0.6.1), a readable conversation, and a knowledge file.
    uid False: the first conversation's rows have no Hermes message id (row-form keys, as older JARVIS backups)."""
    conversation(fx["db"], "leg_01", T, [("user", "What is the vendor deal worth?"), ("assistant", secret(mark))],
                 uid=uid)
    words = {"readable": mark.rstrip("0123456789") + "readword", "item": mark.rstrip("0123456789") + "knowword"}
    readable = "The %s pricing note says the courier ships on Mondays." % words["readable"]
    conversation(fx["db"], "leg_02", T + DAY, [("user", "When does the courier ship?"), ("assistant", readable)])
    os.makedirs(os.path.join(fx["profile"], "knowledge"), exist_ok=True)
    with open(os.path.join(fx["profile"], "knowledge", "vendors.md"), "w", encoding="utf-8") as f:
        f.write("# Vendors\nThe %s courier code is printed on every label.\n" % words["item"])
    return words  # letters only: verify's search sample cuts a word at its first digit


async def _make_legacy(run_id: int) -> None:
    """What migration 0008 finds on a 0.6.1 database: the run is by an importer older than 1.1.0, and the admin's
    withhold names no JARVIS row and no decision backup (0.6.1 kept a key only); 0008 sets the lock."""
    async with session_scope() as s:
        await s.execute(text("UPDATE import_runs SET importer_version = '1.0.4' WHERE id = :r"), {"r": run_id})
        await s.execute(text(
            "UPDATE brain_tombstones SET source_session_id = '', source_row_id = '', role = '', tool_call_id = '', "
            "message_uid = '', uid_known = false, decided_on = '', bound = false, source_at = NULL "
            "WHERE reason = :r AND anchor_key = ''"), {"r": identity.WITHHELD_BY_ADMIN})
        await s.execute(text("INSERT INTO brain_maintenance (name, detail) VALUES ('legacy_brain_data', "
                             "'{\"found_by\": \"test\"}'::jsonb)"))


async def _nothing_shows(client, *words: str) -> None:
    """No reader shows these words: people of every role (search, sessions list and each session, items), the agents'
    memory (history and relevant) and the Supervisor's recall."""
    for who in ROLES:
        await as_(client, who)
        for word in words:
            r = await client.get("/api/brain/search", params={"q": word})
            assert r.status_code == 200 and word not in r.text, (who, word)
            r = await client.get("/api/brain/items", params={"q": word, "include_deleted": "true"})
            assert word not in r.text, (who, word)
            if who != "viewer":
                r = await client.get("/api/brain/sessions", params={"q": word, "limit": 200})
                assert r.status_code == 200 and word not in r.text, (who, word)
        if who != "viewer":
            async with session_scope() as s:
                ids = list((await s.scalars(text("SELECT id FROM archive_sessions"))).all())
            for sid in ids:
                r = await client.get("/api/brain/sessions/%d" % sid)
                assert not any(w in r.text for w in words), (who, sid)
    for word in words:
        async with session_scope() as s:
            memory = store.AgentMemory(s, "supervisor")
            hits, _ = await memory.history(word)
            assert not any(word in (h.message.content or "") for h in hits), word
            assert word not in json.dumps([i.body for i in await memory.relevant(word)], default=str), word
        recall = await ask("what did JARVIS say about %s" % word)
        assert word not in json.dumps(recall.data, default=str) and word not in (recall.summary or ""), word


async def _shows(client, word: str) -> bool:
    await as_(client, "admin")
    return word in (await client.get("/api/brain/search", params={"q": word})).text


async def _audit_ids() -> list[int]:
    async with session_scope() as s:
        return list((await s.scalars(select(AuditLog.id).order_by(AuditLog.id))).all())


async def test_legacy_data_is_locked_for_every_reader_then_reset_and_imported_again(client, fx, tmp_path):
    """Plan 8.1, 8.2 (S6/S8/S12/R14/R26/S22 group): while locked, nothing of the legacy data shows to anyone, the
    importer, the decisions export and the withhold API refuse (naming the reset), and status says so. The reset
    without --yes changes nothing. With --yes it binds the 0.6.1 withhold to its exact JARVIS row, writes a version 5
    file with every decision (the withhold, the reviewed item), rolls the legacy run back, lifts the lock and deletes
    no audit entry; the same backup imported again with that file, then Settle, brings back the decisions: the
    withheld answer withheld (nowhere readable), the item approved, the rest readable."""
    await allow_ai_context()
    mark = "zqlegacy62"
    words = _world(fx, mark)
    first = await imp.run(bj.paths(fx), ALL)
    [answer] = await message_of("leg_01", 2)
    await withhold(client, answer.id)
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key.like("%knowledge/vendors.md")))
        await store.review_item(s, item, decision="approve", actor=ADMIN[0])
        item_key = item.source_key
    readable = await row_with(words["readable"])
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/promote" % readable.id, json={"kind": "project", "title": "Courier"})
    assert r.status_code == 201, r.text
    await _make_legacy(first["run_id"])
    assert await _shows(client, words["readable"]) is False
    async with session_scope() as s:  # the message-level guard on its own: a conversation (no text of its own) that
        await s.execute(text(  # is not a legacy run's holds a legacy run's message
            "UPDATE archive_sessions SET import_run_id = NULL, title = '', summary = '' "
            "WHERE source_session_id = 'leg_02'"))

    # Locked: no reader shows it (promoted copies included), and everything that decides refuses.
    await _nothing_shows(client, words["readable"], words["item"])
    for who in ("operator", "admin+"):  # the legacy conversations themselves (titles, summaries) are not listed
        await as_(client, who)
        listed = (await client.get("/api/brain/sessions", params={"limit": 200})).json()["sessions"]
        assert [x["source_session_id"] for x in listed] == ["leg_02"], (who, listed)
        assert (await client.get("/api/brain/sessions/%d" % answer.session_id)).status_code == 404, who
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % readable.id)
    assert r.status_code == 409 and "legacy-reset" in r.text, r.text
    with pytest.raises(imp.Refused, match="legacy-reset"):
        await imp.run(bj.paths(fx), ALL)
    with pytest.raises(imp.Refused, match="legacy-reset"):
        await imp.decisions_export(str(tmp_path / "refused.json"))
    st = await imp.status()
    assert st["legacy_lock"] is True and st["legacy_runs"] == [first["run_id"]]
    before = await _audit_ids()
    export = tmp_path / "legacy-decisions.json"
    with pytest.raises(imp.Refused, match="nothing was changed"):
        await imp.legacy_reset(str(export))
    assert not export.exists() and (await imp.status())["legacy_lock"] is True
    async with session_scope() as s:
        assert (await s.get(ImportRun, first["run_id"])).status != "rolled_back"
        assert (await s.get(BrainTombstone, answer.source_key)).source_row_id == ""

    out = await imp.legacy_reset(str(export), confirm=True)
    assert out["runs_rolled_back"] == [first["run_id"]] and out["lock_lifted"] is True
    assert out["withholds bound to their exact JARVIS row"] == 1 and out["reviewed_items_deleted"] == 1
    data = json.loads(export.read_text())
    assert data["version"] == imp.DECISIONS_VERSION and data["legacy_reset"]["runs_rolled_back"] == [first["run_id"]]
    [own] = [r for r in data["removals"] if r["source_key"] == answer.source_key]
    assert (own["session"], own["row"], own["role"]) == ("leg_01", "2", "assistant") and own["at_row"]
    assert [d["status"] for d in data["decisions"] if d["source_key"] == item_key] == ["approved"]
    assert mark not in export.read_text()  # keys, ids and times only
    after = await _audit_ids()
    assert set(before) <= set(after) and len(after) > len(before)  # nothing deleted; the reset is audited
    async with session_scope() as s:
        assert (await s.get(ImportRun, first["run_id"])).status == "rolled_back"
        assert (await s.scalar(text("SELECT count(*) FROM brain_maintenance WHERE name = 'legacy_brain_data'"))) == 0

    again = await imp.run(bj.paths(fx), ALL, actor=ADMIN[0], decisions=str(export))
    await bj.settle(fx)
    [back] = await message_of("leg_01", 2)
    assert back.withheld and back.content == ""
    assert (await stones())[back.source_key].review != identity.PENDING
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == item_key,
                                                       MemoryItem.deleted_at.is_(None)))
        assert item is not None and item.status == "approved" and item.reviewed_by == ADMIN[0]
    assert await _shows(client, words["readable"]) is True  # unrelated rows are readable again
    await leak_free(client, mark + " is worth", fx, (again["run_id"],))


async def test_a_version_3_file_applies_only_through_the_legacy_reset(client, fx, tmp_path):
    """Plan 7.1: decisions-apply refuses a version 3 file (0.6.0, 0.6.1: keys only) before any change. Under the
    legacy lock the reset applies it first (the rows it names are withheld, then rolled back with their run) and
    holds no other row of the family: the re-import and Settle decide those. The key stays withheld through the
    re-import."""
    mark = "zqlegv362"
    _world(fx, mark, uid=False)  # a row-form key names its conversation: the family a fail-safe hold would reach
    first = await imp.run(bj.paths(fx), ALL)
    [answer] = await message_of("leg_01", 2)
    v3 = tmp_path / "v3.json"
    v3.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 3, "decisions": [],
                              "removals": [{"source_key": answer.source_key, "reason": identity.WITHHELD_BY_ADMIN}]}))
    with pytest.raises(imp.Refused, match="legacy-reset"):
        await imp.decisions_apply(str(v3), first["run_id"], actor=ADMIN[0], apply=True)
    assert await stones() == {}
    await _make_legacy(first["run_id"])
    with pytest.raises(imp.Refused, match="active admin"):
        await imp.legacy_reset(str(tmp_path / "out.json"), confirm=True, decisions=str(v3))
    out = await imp.legacy_reset(str(tmp_path / "out.json"), actor=ADMIN[0], confirm=True, decisions=str(v3))
    assert answer.source_key == "jarvis:message:leg_01:2"
    assert out["version_3_file"] == {"withheld": 1} and out["runs_rolled_back"] == [first["run_id"]]
    assert {st.review for st in (await stones()).values()} == {identity.CERTAIN}  # nothing else held
    await imp.run(bj.paths(fx), ALL)
    [back] = await message_of("leg_01", 2)
    assert back.withheld and back.content == ""
    assert await row_with(mark + " is worth") is None


@needs_docker
async def test_a_redaction_of_the_restored_current_text_survives_a_restore(client, restore_point, tmp_path):  # noqa: F811
    """Plan 7.2 (rv3-dur-3): the restored backup predates the edit, so the version an admin redacted later is the
    item's current text there. --removals-only carries the redaction; applied again, it empties that text from the
    item and its version and deletes the item (soft), audited as a redaction via the restore with who decided it."""
    word = "zqredact62"
    async with session_scope() as s:
        item = await store.create_item(s, kind="business", title="Vendor terms", body="First body %s." % word,
                                       actor=ADMIN[0])
        item_id, created = item.id, item.created_at
    restore_point.take()
    async with session_scope() as s:
        item = await s.get(MemoryItem, item_id)
        await store.edit_item(s, item, body="Second body, nothing sensitive.", actor=ADMIN[0])
    await as_(client, "admin")
    r = await client.post("/api/brain/items/%d/versions/1/redact" % item_id, json={})
    assert r.status_code == 200, r.text
    out = tmp_path / "removals.json"
    assert (await imp.decisions_export(str(out), removals_only=True))["redactions"] == 1
    [entry] = json.loads(out.read_text())["redactions"]
    assert (entry["item"], entry["version"], entry["by"]) == (item_id, 1, ADMIN[0])
    await restore_point.restore()
    async with session_scope() as s:
        assert (await s.get(MemoryItem, item_id)).version == 1  # the restored state: the redacted text is current
    result = await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    assert result["redactions"] == {"current text redacted, item deleted": 1}, result
    assert result["redactions_not_applied"] == {}
    async with session_scope() as s:
        item = await s.get(MemoryItem, item_id)
        assert item.created_at == created and item.deleted_at is not None and word not in item.body + item.title
        versions = (await s.scalars(select(MemoryItemVersion).where(MemoryItemVersion.item_id == item_id))).all()
        assert versions and all(word not in (v.body + v.title) for v in versions)
        assert review.is_redacted([v for v in versions if v.version == 1][0])
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "memory.version_redacted")
                               .order_by(AuditLog.id.desc()).limit(1))
        assert entry.detail["via"] == "restore" and entry.detail["decided_by"] == ADMIN[0]
    again = await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    assert again["redactions"] == {"already done": 1}
