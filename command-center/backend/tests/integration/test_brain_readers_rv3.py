"""JARVIS Brain 0.6.2: the round-3 READERS findings (scratchpad rv3-attack-readers-privacy.json), each as a regression
test. Synthetic data only.

- rp-c (medium, confirmed): an AI-written draft keeps the text of approved memory its prompt held after the admin
  withholds the message that memory was promoted from, or purges the item. Fixed: the worker records the prompt's
  memory items on the task and on every draft (artifacts.data.memory_items, result.data.memory_prompt_items); a removal
  replaces the draft, its task's summary and the approval cards naming it (quotes.scrub_drafts); the artifact and task
  readers guard at read time (quotes.guard_drafts, work.task_views); a removal while the model writes is caught when the
  task stores the draft (under the quote lock, shared).
- rp-d (medium, confirmed in a changed form): a purge of an incoming file version made on 0.6.1 kept that version's
  PLAIN SHA-256 in the existing item's flags; on 0.6.2 the legacy reset then rolled the item back and the purged version
  came back at the next import. Fixed: the start-up check (and the legacy reset, before its rollback) turns such a hash
  into a keyed fingerprint and drops it (identity.legacy_rejected_hashes).
- rp-e (medium, confirmed): a source_changed conflict kept the plain SHA-256 (and the title) of a version the admin
  redacted, and GET /api/brain/conflicts returned it. Fixed: the API never returns a version hash; a redaction blanks
  the conflict's copies of that version (review.redact_conflict_copies; the start-up check for older redactions).
- rp-b (high, not confirmed on the 0.6.2 engine): the review-2 S6 rule it attacked is gone (plan step 6) and 0.6.1
  data is locked until the legacy reset; the true carried-on copy stays held after Hermes's uid backfill. Kept as a
  regression.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

import pytest
from sqlalchemy import select, text

from app.bootstrap import check_withheld_copies
from app.brain import importer as imp
from app.brain import quotes, review, store
from app.db import session_scope
from app.llm.base import LLMError, LLMProvider
from app.models import Approval, Artifact, MemoryConflict, MemoryItem, Task

from .. import brain_jarvis as bj
from .test_brain_agents import _item, _message, _session, allow_ai_context
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_review2_062 import cli, import062, migrate061, op062, q, startup
from .test_brain_rules_062r import BACKEND, _env, _legacy, rel061, scratch  # noqa: F401 - fixtures
from .test_brain_schema import _migrate
from .test_brain_withhold_identity import T, conversation

pytestmark = pytest.mark.usefixtures("clean_db")
ROLES = ("viewer", "operator", "operator+", "admin", "admin+")
ALL = imp.Selection(all=True)


# ======================= rp-c: AI drafts written with memory that is removed later =======================


class FactUsingModel(LLMProvider):
    """A fake writer: planning fails (the rule planner plans); a draft uses each fact of the <memory> block, as a real
    writer would. `during` runs while the model 'writes' (a removal committed in its own transaction)."""
    name, available = "fact-model", True

    def __init__(self, during=None) -> None:
        self.prompts: list[str] = []
        self.during = during

    async def json(self, *, purpose, system, prompt, schema, max_tokens=16000):
        raise LLMError("planning by the model is off in this test")

    async def text(self, *, purpose, system, prompt, max_tokens=16000):
        self.prompts.append(prompt)
        if self.during is not None:
            await self.during()
        facts = re.findall(r"<item [^>]*>\n(.*?)\n</item>", prompt, flags=re.S)
        lines = ["# %s" % " ".join(facts[0].split()) if facts else "# Office security", ""]
        lines += ["- %s" % " ".join(f.split()) for f in facts]
        return "\n".join(lines + ["", "Book a free consultation."])


DRAFT_TABLES = ("artifacts", "tasks", "approvals", "events", "task_logs", "supervisor_requests", "outbox", "audit_log")


async def _draft_copies(mark: str) -> list[str]:
    """The columns of the tables a draft reaches (the draft, its task, approval cards, events, logs) holding the mark;
    the conversation and the memory item have their own tests."""
    return [w for w in await bj.nowhere(mark) if w.split(".")[0] in DRAFT_TABLES]


async def _seen(client, mark: str) -> dict[str, list[str]]:
    async with session_scope() as s:
        arts = list((await s.scalars(text("SELECT id FROM artifacts ORDER BY id"))).all())
        tasks = list((await s.scalars(text("SELECT id FROM tasks ORDER BY id"))).all())
    pages = ["/api/artifacts", "/api/tasks?limit=200", "/api/approvals?status=all"]
    pages += ["/api/artifacts/%d" % a for a in arts] + ["/api/tasks/%d" % t for t in tasks]
    out: dict[str, list[str]] = {}
    for who in ROLES:
        await as_(client, who)
        for page in pages:
            r = await client.get(page)
            if mark.lower() in r.text.lower():
                out.setdefault(who, []).append(page)
    return out


async def _draft_world(client, removal: str, mark: str) -> tuple[int, int]:
    """An approved memory item holding the mark (promoted from a conversation message, or a manual item), memory in AI
    prompts allowed. (message id, item id)."""
    secret = "The office alarm disarm code is 7342 %s; never share it." % mark
    async with session_scope() as s:
        sess = _session("rv3c_%s" % mark, title="Office security")
        s.add(sess)
        await s.flush()
        m = _message(sess, 1, secret, [], role="assistant")
        s.add(m)
        await s.flush()
        mid = m.id
        item_id = 0
        if removal == "item_purged":
            item_id = (await _item(s, "knowledge", "Office alarm security", secret, approve=True)).id
    if removal == "source_message_withheld":
        await as_(client, "operator")
        r = await client.post("/api/brain/messages/%d/promote" % mid,
                              json={"kind": "knowledge", "title": "Office alarm security", "agent": ""})
        assert r.status_code == 201, r.text
        item_id = r.json()["item"]["id"]
        await as_(client, "admin")
        assert (await client.post("/api/brain/items/%d/review" % item_id,
                                  json={"decision": "approve"})).status_code == 200
    await allow_ai_context()
    return mid, item_id


async def _remove(client, removal: str, mid: int, item_id: int) -> None:
    await as_(client, "admin")
    if removal == "source_message_withheld":
        assert (await client.post("/api/brain/messages/%d/withhold" % mid)).status_code == 200
    else:
        assert (await client.delete("/api/brain/items/%d" % item_id)).status_code == 200
        assert (await client.delete("/api/brain/items/%d/purge" % item_id)).status_code == 200


async def _draft(client, make_worker, model: FactUsingModel) -> None:
    await as_(client, "operator")
    r = await client.post("/api/agents/seo_content/tasks",
                          json={"capability": "draft_blog_post", "params": {"topic": "office alarm security"}})
    assert r.status_code == 201, r.text
    await make_worker(llm=model).run_until_idle()


@pytest.mark.parametrize("removal", ["source_message_withheld", "item_purged"])
async def test_rv3_rp_c_an_ai_draft_loses_the_memory_text_its_prompt_held_when_that_memory_is_removed(
        client, make_worker, removal):
    """rp-c: the draft names the memory items its prompt held; the withhold of their source message (or the purge of
    the item) replaces the draft, its task's summary and the publish approval's card, for every role, in the database
    too (not only at read time)."""
    mark = "zqrv3c" + removal[:4]
    mid, item_id = await _draft_world(client, removal, mark)
    model = FactUsingModel()
    await _draft(client, make_worker, model)
    assert any(mark in p for p in model.prompts), "the memory block did not reach the model"
    async with session_scope() as s:
        [art] = (await s.scalars(select(Artifact))).all()
        assert mark in art.content and art.data.get("memory_items") == [item_id]
        task = await s.get(Task, art.task_id)
        assert task.result["data"]["memory_prompt_items"] == [item_id]
        # the operator approves the draft and asks to publish it: the approval card shows its title and an excerpt
        art.status = "approved"
        publish = Task(agent="seo_content", capability="publish_draft", risk="external", status="awaiting_approval",
                       params={"artifact_id": art.id, "destination": "wordpress"})
        s.add(publish)
        await s.flush()
        s.add(Approval(task_id=publish.id, agent="seo_content", capability="publish_draft", status="pending",
                       risk="external", summary="Publish blog draft #%d" % art.id,
                       details={"target": 'a draft post "%s" from blog draft #%d' % (art.title, art.id),
                                "draft": {"artifact_id": art.id, "title": art.title, "excerpt": art.content[:300]},
                                "binding": {"artifact_id": art.id}}))
    assert await _seen(client, mark), "the draft did not hold the memory's text before the removal"
    await _remove(client, removal, mid, item_id)
    assert await _draft_copies(mark) == [], "a stored copy of the removed memory's text remains"
    assert await _seen(client, mark) == {}
    async with session_scope() as s:
        [art] = (await s.scalars(select(Artifact))).all()
        assert (art.title, art.content) == (quotes.DRAFT_REMOVED_TITLE, quotes.DRAFT_REMOVED)
        assert (await s.get(Task, art.task_id)).summary == quotes.DRAFT_TASK_REMOVED
    await check_withheld_copies()  # the start-up check changes nothing more and serves nothing
    assert await _seen(client, mark) == {}


async def test_rv3_rp_c_a_removal_while_the_model_writes_reaches_the_draft(client, make_worker):
    """rp-c, the race: the item is purged after the prompt was built and before the draft is stored; the task stores
    the draft already removed."""
    mark = "zqrv3crace"
    _, item_id = await _draft_world(client, "item_purged", mark)

    async def purge() -> None:
        async with session_scope() as s:
            item = await s.get(MemoryItem, item_id)
            await store.delete_item(s, item, actor="admin@test.local")
            await s.flush()
            await review.purge(s, item, actor="admin@test.local")

    model = FactUsingModel(during=purge)
    await _draft(client, make_worker, model)
    assert any(mark in p for p in model.prompts)
    async with session_scope() as s:
        assert (await s.get(MemoryItem, item_id)) is None
        [art] = (await s.scalars(select(Artifact))).all()
        assert art.content == quotes.DRAFT_REMOVED and art.data.get("memory_items") == [item_id]
    assert await _draft_copies(mark) == []
    assert await _seen(client, mark) == {}


async def test_rv3_rp_c_the_read_time_guard_serves_no_draft_whose_memory_is_gone(client, make_worker):
    """rp-c, the read-time guard on its own: a draft row that still holds the text (a removal that did not reach it:
    a database restored from before the fix's scrub) is served removed while its memory item is gone."""
    mark = "zqrv3cguard"
    _, item_id = await _draft_world(client, "item_purged", mark)
    await _draft(client, make_worker, FactUsingModel())
    async with session_scope() as s:
        item = await s.get(MemoryItem, item_id)
        item.deleted_at = item.updated_at  # deleted, never scrubbed (the row is written directly)
    assert await _seen(client, mark) == {}
    async with session_scope() as s:
        assert mark in (await s.scalar(select(Artifact.content)))  # stored as it was: only the readers hide it
    await check_withheld_copies()  # the start-up check (refresh_quotes) then removes it from the row too
    assert await _draft_copies(mark) == []


# ======================= rp-d: a 0.6.1 purge of an incoming version =======================

ALARM = "knowledge/office-alarm.md"
V1 = "# Office alarm\nThe alarm company is SecureCo, call them on weekdays.\n"


def _v2(pin: int) -> str:
    return V1 + "Disarm PIN: %04d.\n" % pin


OPS061 = r'''
import asyncio, sys
from sqlalchemy import select

async def main():
    from app.brain import review, store
    from app.db import dispose_engine, session_scope
    from app.models import MemoryItem
    op = sys.argv[1]
    async with session_scope() as s:
        if op == "approve":
            item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == sys.argv[2]))
            await store.review_item(s, item, decision="approve", actor="admin@test.local")
        elif op == "purge_incoming":  # what 0.6.1's DELETE /items/{id} then DELETE /items/{id}/purge did
            item = await s.scalar(select(MemoryItem).where(MemoryItem.flags["replaces_source_key"].astext == sys.argv[2]))
            await store.delete_item(s, item, actor="admin@test.local")
            await s.flush()
            await review.purge(s, item, actor="admin@test.local")
    await dispose_engine()

asyncio.run(main())
'''


def _op061(rel, url, *args):
    script = rel.parent / "rv3_ops_061.py"
    script.write_text(OPS061)
    done = subprocess.run([sys.executable, str(script), *args], cwd=str(rel), env=_env(url, PYTHONPATH=str(rel)),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr


def _write(fx, rel, text_):
    path = os.path.join(fx["profile"], rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text_)


async def _columns_with(url: str, needle: str) -> list[str]:
    cols = await q(url, "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                        "AND data_type IN ('text', 'character varying', 'jsonb', 'json', 'ARRAY') ORDER BY 1, 2")
    out = []
    for c in cols:
        if await q(url, 'SELECT 1 FROM "%s" WHERE "%s"::text LIKE $1 LIMIT 1' % (c["table_name"], c["column_name"]),
                   "%" + needle + "%"):
            out.append("%s.%s" % (c["table_name"], c["column_name"]))
    return out


async def test_rv3_rp_d_a_061_purge_of_an_incoming_version_keeps_no_plain_hash_and_survives_the_legacy_reset(
        scratch, rel061, tmp_path):
    """rp-d with the real 0.6.1 release: import, approve, JARVIS changes the file, the admin purges the incoming
    version. After the 0.6.2 migration the start-up check (while the data is locked) keeps that purge as a keyed
    fingerprint only; the legacy reset, the re-import of the same backup and another import never bring the purged
    version back, and no column holds its plain SHA-256 at any point after the start-up check."""
    migrate061(rel061, scratch)
    fx = bj.mini(tmp_path / "fx")
    key = "jarvis:file:profile/" + ALARM
    _write(fx, ALARM, V1)
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    _op061(rel061, scratch, "approve", key)
    _write(fx, ALARM, _v2(7342))
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    _op061(rel061, scratch, "purge_incoming", key)
    sha = hashlib.sha256(_v2(7342).encode()).hexdigest()
    assert await _columns_with(scratch, sha) == ["memory_items.flags"]  # what 0.6.1 left
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    await startup(scratch)
    assert await _columns_with(scratch, sha) == [], "the start-up check kept the plain hash"
    keyed = await q(scratch, "SELECT fp, source_key, via FROM brain_purged_content")
    assert [(k["source_key"], k["via"]) for k in keyed] == [("", "upgrade")] and keyed[0]["fp"].startswith("k1:")
    export = tmp_path / "legacy.json"
    cli(scratch, "brain-import", "legacy-reset", "--export", str(export), "--yes")
    assert sha not in export.read_text()
    import062(scratch, fx)
    import062(scratch, fx)
    assert await _columns_with(scratch, sha) == []
    assert await q(scratch, "SELECT id FROM memory_items WHERE source_sha256 = $1", sha) == [], \
        "the version an admin purged came back"


async def test_rv3_rp_d_a_rejected_version_an_item_still_holds_keeps_its_hash(client, tmp_path):
    """rp-d's boundary: a hash in flags.rejected_source_sha256 whose version an item still holds (keep_existing: that
    item is readable, nothing was purged) is not a purge; it stays, and no keyed fingerprint is made."""
    from app.brain import identity

    async with session_scope() as s:
        a = await _item(s, "knowledge", "Courier days", "The courier ships on Mondays.", approve=True)
        b = await _item(s, "knowledge", "Courier days v2", "The courier ships on Tuesdays.", approve=False)
        a.source_sha256, b.source_sha256 = "a" * 64, "b" * 64
        a.flags = {**(a.flags or {}), "rejected_source_sha256": ["b" * 64, "c" * 64]}
    async with session_scope() as s:
        assert await identity.legacy_rejected_hashes(s, actor="test") == 1  # "c": no item holds it
    async with session_scope() as s:
        a = await s.scalar(select(MemoryItem).where(MemoryItem.title == "Courier days"))
        assert a.flags["rejected_source_sha256"] == ["b" * 64]
        assert (await s.scalar(text("SELECT count(*) FROM brain_purged_content"))) == 1


# ======================= rp-e: a conflict's copy of a redacted version =======================

DOOR = "knowledge/door.md"


def _door(pin: int) -> str:
    return "# Door\nThe back door PIN is %04d, zqrv3e.\n" % pin


DOOR_V2 = "# Door\nThe back door PIN was changed; ask the office.\n"


async def _changed_and_redacted(client, fx) -> tuple[int, str]:
    _write(fx, DOOR, _door(4821))
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        item = (await s.scalar(select(MemoryItem).where(MemoryItem.source_key == "jarvis:file:profile/" + DOOR))).id
    await as_(client, "admin")
    assert (await client.post("/api/brain/items/%d/review" % item, json={"decision": "approve"})).status_code == 200
    _write(fx, DOOR, DOOR_V2)
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        conflict = (await s.scalar(select(MemoryConflict).where(MemoryConflict.kind == "source_changed"))).id
    r = await client.post("/api/brain/conflicts/%d/resolve" % conflict, json={"resolution": "use_incoming"})
    assert r.status_code == 200, r.text
    return item, hashlib.sha256(_door(4821).encode()).hexdigest()


async def _hash_served(client, item: int) -> dict[str, list[int]]:
    out = {}
    for who in ("operator", "operator+", "admin", "admin+"):
        await as_(client, who)
        body = (await client.get("/api/brain/conflicts")).text + (await client.get("/api/brain/items/%d" % item)).text
        hashes = set(re.findall(r"[0-9a-f]{64}", body))
        found = [n for n in range(10000) if hashlib.sha256(_door(n).encode()).hexdigest() in hashes]
        if found:
            out[who] = found
    return out


async def test_rv3_rp_e_a_redacted_version_leaves_no_plain_hash_in_conflicts(client, tmp_path):
    """rp-e: after use_incoming the admin redacts the old versions; the conflicts API never returns a version hash,
    and the stored conflict no longer holds the redacted version's hash or title."""
    fx = bj.mini(tmp_path / "fx")
    item, sha = await _changed_and_redacted(client, fx)
    await as_(client, "admin")
    assert await _hash_served(client, item) == {}  # the API: no version hash, before any redaction too
    detail = (await client.get("/api/brain/items/%d" % item)).json()
    for v in detail["versions"]:
        if v["version"] != detail["item"]["version"] and "zqrv3e" in (v["body"] or ""):
            r = await client.post("/api/brain/items/%d/versions/%d/redact" % (item, v["version"]))
            assert r.status_code == 200, r.text
    assert await bj.nowhere(sha) == [] and await bj.nowhere("zqrv3e") == []
    async with session_scope() as s:
        c = await s.scalar(select(MemoryConflict).where(MemoryConflict.kind == "source_changed"))
        assert c.existing["source_sha256"] == "" and c.existing["title"] == "[redacted]"


async def test_rv3_rp_e_the_start_up_check_blanks_a_conflict_copy_of_a_version_redacted_before(client, tmp_path):
    """rp-e for a redaction made before the fix (the conflict still holds the hash): the start-up check blanks it."""
    fx = bj.mini(tmp_path / "fx")
    item, sha = await _changed_and_redacted(client, fx)
    async with session_scope() as s:
        c = await s.scalar(select(MemoryConflict).where(MemoryConflict.kind == "source_changed"))
        version = int(c.existing["version"])
        await s.execute(text("UPDATE memory_item_versions SET title = '[redacted]', body = '', tags = '{}' "
                             "WHERE item_id = :i AND version <= :v"), {"i": item, "v": version})
    assert await bj.nowhere(sha) == ["memory_conflicts.existing"]
    await check_withheld_copies()
    assert await bj.nowhere(sha) == []


# ======================= rp-b (not confirmed on 0.6.2): a 0.6.1 carried-on copy after Hermes's uid backfill ====

MARK_B = "zqrv3bk61"
SECRET_B = "The new vendor contract with %s is worth forty thousand dollars a year." % MARK_B
READERS_B = r'''
import asyncio, json, sys

async def main():
    from app.brain import store
    from app.brain.policy import PersonScope, person_messages, person_sessions
    from app.db import dispose_engine, session_scope
    mark = sys.argv[1]
    out = {}
    async with session_scope() as s:
        for role, sens in (("operator", False), ("admin", False), ("admin", True)):
            scope = PersonScope(role, sens)
            hits, _ = await store.search_archive(s, session_where=person_sessions(scope),
                                                 message_where=person_messages(scope), q=mark)
            out["search %s%s" % (role, "+" if sens else "")] = any(mark in (h.message.content or "") for h in hits)
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
        out["supervisor history"] = any(mark in (h.message.content or "") for h in hits)
    print("READERS=" + json.dumps(out))
    await dispose_engine()

asyncio.run(main())
'''


@pytest.mark.parametrize("when", ["withheld_on_061", "withheld_on_062"])
async def test_rv3_rp_b_a_carried_on_copy_stays_withheld_after_the_uid_backfill(scratch, rel061, tmp_path, when):
    """rp-b with the real 0.6.1 release: the 0.6.1 A-leak shape (a head copy archived by a second compaction) on a
    JARVIS without message ids; the upgrade locks the data, the legacy reset and the re-import with Settle hold the
    copy ('found when finishing decisions'); after Hermes's uid backfill gives the original and the copy different
    uids, the next import releases nothing and no reader shows the text."""
    migrate061(rel061, scratch)
    fx = bj.mini(tmp_path / "fx")
    db = fx["db"]
    conversation(db, "b_head_01", T, [("user", "Please summarise the vendor situation."), ("assistant", SECRET_B),
                                      ("user", "And the timeline?"), ("assistant", "Next quarter.")], uid=False)
    bj.hermes_compact(db, "b_head_01", head_n=2, tail_n=0, ts=T + 100)
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    if when == "withheld_on_061":
        _legacy(rel061, scratch, "withhold", MARK_B)
    bj.hermes_compact(db, "b_head_01", head_n=0, tail_n=0, ts=T + 200)
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    await startup(scratch)
    cli(scratch, "brain-import", "legacy-reset", "--export", str(tmp_path / "legacy.json"), "--yes")
    import062(scratch, fx)
    if when == "withheld_on_062":
        op062(scratch, "withhold", "b_head_01", "2")
    cli(scratch, "brain-import", "settle", "--backup", fx["db"])
    bj.backfill_uids(db, "b_head_01")
    import062(scratch, fx)
    rows = await q(scratch, "SELECT m.withheld, m.content LIKE '%' || $1 || '%' AS has_mark FROM archive_messages m "
                            "WHERE m.content LIKE '%' || $1 || '%' OR m.withheld", MARK_B)
    assert rows and not any(r["has_mark"] and not r["withheld"] for r in rows), rows
    assert await q(scratch, "SELECT 1 FROM brain_withhold_exemptions") == []
    done = subprocess.run([sys.executable, "-c", READERS_B, MARK_B], cwd=str(BACKEND), env=_env(scratch),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    seen = json.loads(next(x for x in done.stdout.splitlines() if x.startswith("READERS="))[len("READERS="):])
    assert not any(seen.values()), seen
