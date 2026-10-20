"""JARVIS Brain 0.6.2: a withheld message stays withheld whatever JARVIS (Hermes) does to it (spec 2.9, 17 and 19.4).

Every confirmed 0.6.1 re-import leak is replayed here against the real importer, through the real admin API, with
backups in the shapes Hermes writes (tests/brain_jarvis.py): A (a carried-on copy after a second compaction, the
original pruned), A-tail (one in-place compaction, the withheld original rewound), B (an adjacent repeat after an
edit, a rewind or a delete), C1 (uids backfilled), C2 (a uid reused), C3 (a shared uid that stopped being shared),
D (decisions carried between differently keyed backups), the gateway's and the CLI's /branch, rotation, pruning, folds,
renumbering, repeated imports, a rollback, and findings 2 and 3 (a restricting import that deletes a promoted row,
or a row the source no longer holds). Each positive test asserts that the withheld text is nowhere (every text, JSONB,
array and search column of every table), that no person, agent, the Supervisor's recall or verify can see it, that
every withheld row refuses to be promoted, and that status counts nothing left; and ends with its inverse: what is not
the same message stays readable. Synthetic data only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.brain import identity, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainTombstone, ImportRecord, ImportRun, MemoryItem

from .. import brain_jarvis as bj
from ..conftest import ADMIN, SERVICE_TOKEN
from .test_brain_agents import allow_ai_context, ask
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture

pytestmark = pytest.mark.usefixtures("clean_db")

ALL = imp.Selection(all=True)
BACKEND = Path(__file__).resolve().parents[2]
T = 1795000000.0  # 2026-11-18
DAY = 86400.0
IDENTITY_CHECKS = ("withheld_identity_holds", "withheld_hold_no_fingerprint", "purged_stay_purged",
                   "database_guard_quiet")
SAME_TEXT_OTHER = "Thanks, that is all."  # identical short text in another conversation (O1): always readable


# --- Helpers -------------------------------------------------------------------------------------------------------


def secret(mark: str) -> str:
    return "The new vendor contract with %s is worth forty thousand dollars a year." % mark


def conversation(db: str, sid: str, start: float, turns: list[tuple[str, str]], *, uid: bool = True,
                 **session_kw) -> list[int]:
    bj.add_session(db, sid, started=start, **session_kw)
    return [bj.add_row(db, sid, role, content, start + n + 1, uid=None if uid else False)
            for n, (role, content) in enumerate(turns)]


def other_conversation(db: str) -> None:
    """Identical short text and similar text elsewhere: never the same message."""
    conversation(db, "other_0001", T + 30 * DAY, [("user", SAME_TEXT_OTHER), ("assistant", "Glad to help.")])


async def row_with(text_: str) -> ArchiveMessage | None:
    async with session_scope() as s:
        return await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content.contains(text_))
                              .order_by(ArchiveMessage.id).limit(1))


async def message_of(sid: str, source_id: int) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveSession.source_session_id == sid, ArchiveMessage.source_message_id == str(source_id))
            .order_by(ArchiveMessage.id))).all())


async def withhold(client, message_id: int) -> None:
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % message_id)
    assert r.status_code == 200, r.text


async def withhold_text(client, text_: str) -> ArchiveMessage:
    row = await row_with(text_)
    assert row is not None and not row.withheld
    await withhold(client, row.id)
    return row


async def stones() -> dict[str, BrainTombstone]:
    async with session_scope() as s:
        return {t.source_key: t for t in (await s.scalars(select(BrainTombstone).where(
            BrainTombstone.reason == identity.WITHHELD_BY_ADMIN))).all()}


async def withheld_ids() -> list[int]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage.id).where(ArchiveMessage.withheld.is_(True))
                                     .order_by(ArchiveMessage.id))).all())


async def readable(text_: str) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).where(
            ArchiveMessage.content == text_, ArchiveMessage.withheld.is_(False)))).all())


async def run(fx: dict[str, str]) -> dict:
    return await imp.run(bj.paths(fx), ALL)


async def leak_free(client, mark: str, fx: dict[str, str] | None = None, runs: tuple[int, ...] = ()) -> None:
    """The withheld text is nowhere, and no reader shows it: people (every role, search and sessions), the Supervisor
    (history and recall), promotion (every withheld row refuses), verify (checks 9-12 included) and status."""
    assert await bj.nowhere(mark) == []
    async with session_scope() as s:
        sessions = list((await s.scalars(select(ArchiveSession.id))).all())
    for who in ("viewer", "operator", "operator+", "admin", "admin+"):
        await as_(client, who)
        r = await client.get("/api/brain/search", params={"q": mark})
        assert r.status_code == 200 and mark not in r.text, who
        if who != "viewer":
            r = await client.get("/api/brain/sessions", params={"q": mark, "limit": 200})
            assert r.status_code == 200 and mark not in r.text, who
            for sid in sessions:
                r = await client.get("/api/brain/sessions/%d" % sid)
                assert mark not in r.text, (who, sid)
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
        assert not any(mark in (h.message.content or "") for h in hits)
    recall = await ask("what did JARVIS say about %s" % mark)
    assert mark not in json.dumps(recall.data) and mark not in (recall.summary or "")
    await as_(client, "admin")
    for mid in await withheld_ids():  # refused (400), or not even shown (404: a restricted conversation)
        r = await client.post("/api/brain/messages/%d/promote" % mid, json={"kind": "project", "title": "Probe"})
        assert r.status_code in (400, 404), (mid, r.text)
    for run_id in runs:
        assert fx is not None
        v = await imp.verify(run_id, bj.paths(fx))
        checks = v["verification"]["checks"]
        assert v["passed"], {k: c for k, c in checks.items() if not c["ok"]}
        assert set(IDENTITY_CHECKS) <= set(checks)
    st = await imp.status()
    assert (st["readable_rows_matching_a_withhold"], st["withheld_rows_with_a_text_fingerprint"],
            st["rows_blanked_by_the_database_guard"]) == (0, 0, 0), st
    assert st["identity_repair_done"] is True


async def inverse_readable() -> None:
    """The identical short text of another conversation stays readable, with no stone on its key (O1)."""
    [other] = [m for m in await message_of("other_0001", 1)]
    assert (other.content, other.withheld) == (SAME_TEXT_OTHER, False)
    assert other.source_key not in await stones()


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    out = bj.mini(tmp_path / "fx")
    other_conversation(out["db"])
    return out


# --- A: carried-on copies after compaction ---------------------------------------------------------------------------


def _head_copy_backup(fx: dict[str, str], mark: str) -> None:
    conversation(fx["db"], "a_head_01", T, [("user", "Please summarise the vendor situation."),
                                            ("assistant", secret(mark)), ("user", "And the timeline?"),
                                            ("assistant", "Next quarter.")])
    bj.hermes_compact(fx["db"], "a_head_01", head_n=2, tail_n=0, ts=T + 100)  # the carried-on copy of the secret


async def test_a_head_copy_after_a_second_compaction(client, fx):
    """Judge T2, inverted: the withheld original is archived, its live carried-on copy was a duplicate; a second
    compaction archives the copy, which 0.6.1 then imported with its text."""
    mark = "zqahead62"
    _head_copy_backup(fx, mark)
    await run(fx)
    original = await withhold_text(client, mark)
    assert original.state == "compacted"
    bj.hermes_compact(fx["db"], "a_head_01", head_n=0, tail_n=0, ts=T + 200)
    second = await run(fx)
    copy_key = [k for k, st in (await stones()).items() if st.anchor_key == original.source_key]
    assert copy_key, "the carried-on copy's key joined the withhold"
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


async def test_a_head_copy_when_the_original_is_pruned(client, fx):
    mark = "zqaprune62"
    _head_copy_backup(fx, mark)
    await run(fx)
    original = await withhold_text(client, mark)
    bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", int(original.source_message_id))
    second = await run(fx)
    assert all(m.withheld for m in await message_of("a_head_01", int(original.source_message_id)))
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


# --- A-tail: one in-place compaction rewinds the withheld original -----------------------------------------------


def _plain_backup(fx: dict[str, str], mark: str, *, sid: str = "a_tail_01", uid: bool = True, tail_only: bool = False
                  ) -> list[int]:
    turns = [("user", "Please summarise the vendor situation."), ("assistant", "Sure, here it is."),
             ("user", "Give me the headline number."), ("assistant", secret(mark))]
    if not tail_only:
        turns += [("user", "And the other vendor?"), ("assistant", secret(mark[:-1] + "x")),
                  ("user", SAME_TEXT_OTHER), ("assistant", "You are welcome.")]
    return conversation(fx["db"], sid, T, turns, uid=uid)


@pytest.mark.parametrize("tail_n", [2, 6], ids=["tail2", "tail6"])
async def test_a_tail_single_compaction_rewound_original_live_copy_same_uid(client, fx, tail_n):
    """Judge T1 (tail2, tail6), inverted: the withheld answer is in Hermes's protected tail; one compaction rewinds it
    (the re-import deletes its withheld row) and writes a live copy with the same uid under a new key."""
    mark = "zqatail62%d" % tail_n
    _plain_backup(fx, mark, tail_only=tail_n == 2)
    await run(fx)
    original = await withhold_text(client, mark)
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=tail_n, ts=T + 100)
    second = await run(fx)
    assert await message_of("a_tail_01", int(original.source_message_id)) == []  # rewound: its row is gone
    copies = [m for m in bj.rows(fx["db"], "a_tail_01") if m["content"] == secret(mark) and m["active"] == 1]
    stored = [m for c in copies for m in await message_of("a_tail_01", c["id"])]
    assert copies and stored and all(m.withheld for m in stored)
    reasons = (await records(second["run_id"]))
    assert any(r.action == "withheld" and r.reason.startswith(identity.WITHHELD_BY_ADMIN) for r in reasons.values())
    await leak_free(client, mark, fx, (second["run_id"],))
    # Inverse: the similar sentence (one character differs) stays readable, and so does identical text elsewhere.
    if tail_n == 6:
        assert await readable(secret(mark[:-1] + "x"))
    await inverse_readable()


async def records(run_id: int) -> dict[str, ImportRecord]:
    async with session_scope() as s:
        return {r.source_key: r for r in (await s.scalars(select(ImportRecord).where(
            ImportRecord.run_id == run_id))).all()}


async def test_a_tail_second_generation(client, fx):
    mark = "zqatailgen62"
    _plain_backup(fx, mark)
    await run(fx)
    await withhold_text(client, mark)
    for n in range(2):  # the copy itself is rewound by the next compaction, and copied again
        bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100 * (n + 1))
        last = await run(fx)
        assert await bj.nowhere(mark) == []
    await leak_free(client, mark, fx, (last["run_id"],))
    await inverse_readable()


async def test_a_tail_without_uids_by_rewound_twin(client, fx):
    """A backup with no uids at all: the copy is linked to the rewound original by its text (a rewound twin)."""
    mark = "zqatwin62"
    _plain_backup(fx, mark, uid=False)
    await run(fx)
    await withhold_text(client, mark)
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100)
    second = await run(fx)
    held = {r.reason for r in (await records(second["run_id"])).values() if r.action == "withheld"}
    assert "%s: copied by JARVIS" % identity.WITHHELD_BY_ADMIN in held
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


async def test_a_tail_through_resume_cli_and_rerun(client, fx, monkeypatch):
    """The re-import that rewinds the withheld original fails in the middle, is resumed, then run again from the
    command line (another process): never a moment with the text stored."""
    mark = "zqatailcli62"
    _plain_backup(fx, mark)
    await run(fx)
    await withhold_text(client, mark)
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100)
    original = imp.Processor.do_session

    async def fail_after_withholding(self, ss, index):
        out = await original(self, ss, index)
        if ss.id == "a_tail_01":
            raise RuntimeError("stopped for this test")
        return out
    monkeypatch.setattr(imp.Processor, "do_session", fail_after_withholding)
    with pytest.raises(imp.ImportFailed):
        await run(fx)
    monkeypatch.undo()
    assert await bj.nowhere(mark) == []  # the failed batch left nothing behind
    async with session_scope() as s:
        failed = await s.scalar(select(func.max(ImportRun.id)))
    resumed = await imp.resume(failed, bj.paths(fx))
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
    done = subprocess.run([sys.executable, "-m", "app.cli", "brain-import", "run", "--db", fx["db"], "--profile",
                           fx["profile"], "--content", fx["content"], "--all"], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and mark not in done.stdout + done.stderr, done.stdout + done.stderr
    async with session_scope() as s:
        cli_run = await s.scalar(select(func.max(ImportRun.id)))
    await leak_free(client, mark, fx, (resumed["run_id"], cli_run))


# --- B: an adjacent repeat ------------------------------------------------------------------------------------------


def _repeat_backup(fx: dict[str, str], mark: str) -> list[int]:
    return conversation(fx["db"], "b_repeat_01", T, [
        ("user", "Can you check the contract?"), ("assistant", "Yes."), ("user", secret(mark)),
        ("user", secret(mark)), ("assistant", "Noted, I will keep it in mind.")])


@pytest.mark.parametrize("variant", ["edit_original", "edit_repeat", "rewind_original", "delete_original"])
async def test_b_adjacent_repeat(client, fx, variant):
    """The repeat right after the withheld message was stored only as a duplicate of it; once the original or the
    repeat changes in JARVIS, 0.6.1 imported the repeat with its text."""
    mark = "zqb%s62" % variant.replace("_", "")
    ids = _repeat_backup(fx, mark)
    await run(fx)
    await withhold(client, (await message_of("b_repeat_01", ids[2]))[0].id)
    if variant == "edit_original":
        bj.change(fx["db"], "UPDATE messages SET content = ? WHERE id = ?", secret(mark) + " (edited)", ids[2])
    elif variant == "edit_repeat":
        bj.change(fx["db"], "UPDATE messages SET content = ? WHERE id = ?", secret(mark) + " Really.", ids[3])
    elif variant == "rewind_original":
        bj.change(fx["db"], "UPDATE messages SET active = 0 WHERE id = ?", ids[2])
    else:
        bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", ids[2])
    second = await run(fx)
    assert all(m.withheld for m in await message_of("b_repeat_01", ids[3]))
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


async def test_b_prime_repeat_after_withhold(client, fx):
    """A verbatim repeat that first appears right after the withheld message is a member (documented fail-closed);
    a later, non-adjacent repeat with its own uid stays readable (the withheld message's uid was known: O4)."""
    mark = "zqbprime62"
    ids = conversation(fx["db"], "b_prime_01", T, [("user", "Can you check the contract?"),
                                                   ("assistant", secret(mark))])
    await run(fx)
    withheld = (await message_of("b_prime_01", ids[1]))[0]
    await withhold(client, withheld.id)
    # JARVIS writes it again right after (a resend), and much later an independent answer with the same words.
    repeat = bj.add_row(fx["db"], "b_prime_01", "assistant", secret(mark), T + 3)
    bj.add_row(fx["db"], "b_prime_01", "user", "And the other contract?", T + DAY)
    later = bj.add_row(fx["db"], "b_prime_01", "assistant", secret(mark), T + DAY + 1)
    second = await run(fx)
    repeat_key = "jarvis:message:uid:%s" % bj.rows(fx["db"], "b_prime_01")[2]["message_uid"]
    assert await message_of("b_prime_01", repeat) == []  # a duplicate: no row of its own
    stone = (await stones())[repeat_key]  # its key joined the withhold
    assert (stone.anchor_key, stone.via, stone.review) == (withheld.source_key, "duplicate", "")
    [independent] = await message_of("b_prime_01", later)
    assert not independent.withheld and independent.content == secret(mark)
    allowed = ("archive_messages.content", "archive_messages.search", "archive_sessions.search",
               "archive_sessions.summary")
    assert await bj.nowhere(mark, allow=allowed) == []
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}


# --- C: keys that change ---------------------------------------------------------------------------------------------


async def _archive_count(sid: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(func.count()).select_from(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveSession.source_session_id == sid)) or 0)


@pytest.mark.parametrize("absent", [False, True], ids=["uid_null", "column_absent"])
async def test_c1_uid_null_then_filled_adopts_keys_without_duplicating_the_archive(client, fx, absent):
    """C1 (K1A, K1B): the first backup has no uids (NULL, or no column at all), a later one has them for every row:
    every key changes. The stored rows are re-keyed in place: the archive is not duplicated and the withheld one stays
    withheld."""
    mark = "zqcone%s62" % ("absent" if absent else "null")
    if absent:
        bj.drop_uid_column(fx["db"])
    ids = conversation(fx["db"], "c1_01", T, [("user", "What is the contract worth?"), ("assistant", secret(mark)),
                                              ("user", "Thanks.")], uid=False)
    await run(fx)
    before = await _archive_count("c1_01")
    [row] = await message_of("c1_01", ids[1])
    await withhold(client, row.id)
    bj.backfill_uids(fx["db"], "c1_01")
    second = await run(fx)
    assert await _archive_count("c1_01") == before
    [back] = await message_of("c1_01", ids[1])
    assert back.id == row.id and back.withheld and back.source_key.startswith("jarvis:message:uid:")
    assert second["report"]["keys_adopted"] == len(ids)
    assert back.source_key in await stones() and row.source_key in await stones()
    await leak_free(client, mark, fx, (second["run_id"],))
    [thanks] = await message_of("c1_01", ids[2])
    assert not thanks.withheld and thanks.content == "Thanks."
    await inverse_readable()


async def test_c2_uid_reused_excludes_the_unrelated_row_and_withholds_165(client, fx):
    """C2: an unrelated user row takes the withheld answer's uid (so its key); the answer gets a row-form key. The
    unrelated row is never stored under, or withheld by, that key; the answer is withheld by its JARVIS row."""
    mark = "zqctwo62"
    unrelated = "Unrelated words about the team lunch on Friday."
    uids = [bj.new_uid(), bj.new_uid()]
    bj.add_session(fx["db"], "c2_01", started=T)
    r164 = bj.add_row(fx["db"], "c2_01", "user", unrelated, T + 1, uid=uids[0])
    r165 = bj.add_row(fx["db"], "c2_01", "assistant", secret(mark), T + 2, uid=uids[1])
    await run(fx)
    await withhold(client, (await message_of("c2_01", r165))[0].id)
    bj.change(fx["db"], "UPDATE messages SET message_uid = ? WHERE id = ?", uids[1], r164)
    second = await run(fx)
    [answer] = [m for m in await message_of("c2_01", r165) if m.source_key == "jarvis:message:c2_01:%d" % r165]
    assert answer.withheld
    collided = (await records(second["run_id"]))["jarvis:message:uid:%s" % uids[1]]
    assert (collided.action, collided.reason.startswith("key collision")) == ("excluded", True)
    await leak_free(client, mark, fx, (second["run_id"],))
    [kept] = await message_of("c2_01", r164)  # O6: the unrelated row keeps its text, readable
    assert not kept.withheld and kept.content == unrelated


async def test_c3_never_overwrites_158_and_165_never_readable(client, fx):
    """C3: two rows share a uid (the first keeps the uid key); the second is withheld; later the first gets its own uid
    and the second the shared one. The first row's stored row is never overwritten with the withheld text."""
    mark = "zqcthree62"
    plain = "Plain user words about the weekly report."
    shared = bj.new_uid()
    bj.add_session(fx["db"], "c3_01", started=T)
    r164 = bj.add_row(fx["db"], "c3_01", "user", plain, T + 1, uid=shared)
    r165 = bj.add_row(fx["db"], "c3_01", "assistant", secret(mark), T + 2, uid=shared)
    await run(fx)
    [first] = await message_of("c3_01", r164)
    await withhold(client, (await message_of("c3_01", r165))[0].id)
    bj.change(fx["db"], "UPDATE messages SET message_uid = ? WHERE id = ?", bj.new_uid(), r164)
    second = await run(fx)
    async with session_scope() as s:
        again = await s.get(ArchiveMessage, first.id)
    assert (again.content, again.withheld, again.source_message_id) == (plain, False, str(r164))
    collided = (await records(second["run_id"]))["jarvis:message:uid:%s" % shared]
    assert collided.action == "excluded" and collided.reason.startswith("key collision")
    await leak_free(client, mark, fx, (second["run_id"],))
    assert len(await readable(plain)) >= 1


# --- D: decisions carried between differently keyed backups (version 3 files) -------------------------------------


async def _fresh_production() -> None:
    """The live installation's database: neither decision, nothing imported."""
    async with session_scope() as s:
        for table in ("import_records", "archive_messages", "archive_sessions", "brain_tombstones",
                      "brain_withhold_exemptions", "brain_purged_content", "brain_maintenance", "memory_items",
                      "import_runs"):
            await s.execute(text("TRUNCATE %s RESTART IDENTITY CASCADE" % table))


def as_v3(path: Path) -> None:
    """The decisions file as 0.6.0 and 0.6.1 wrote it (version 3): each removal its key and reason only."""
    data = json.loads(path.read_text())
    data.update(version=3, removals=[{"source_key": r["source_key"], "reason": r["reason"]}
                                     for r in data["removals"]])
    data.pop("releases", None)
    path.write_text(json.dumps(data))


async def test_d_v3_file_still_applies(client, tmp_path):
    """The test copy's backup has no uids (key <session>:<row>); the live one has them (key uid:...). A version 3
    decisions file (0.6.1) names the row-form key only: the key gives the JARVIS row, and decisions-apply withholds the
    live row stored for it under its uid key at once (0.6.1 kept it for a later import, which brought the text back)."""
    mark = "zqdthree62"
    test_copy = bj.mini(tmp_path / "test")
    ids = conversation(test_copy["db"], "d_01", T, [("user", "What is it worth?"), ("assistant", secret(mark))],
                       uid=False)
    await run(test_copy)
    await withhold(client, (await message_of("d_01", ids[1]))[0].id)
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    as_v3(out)
    await _fresh_production()
    live = bj.mini(tmp_path / "live")
    conversation(live["db"], "d_01", T, [("user", "What is it worth?"), ("assistant", secret(mark))])
    production = await run(live)
    result = await imp.decisions_apply(str(out), production["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"withheld": 1}
    assert await row_with(mark) is None
    later = await run(live)
    await leak_free(client, mark, live, (later["run_id"],))


async def test_d_reverse_v3_closed_at_next_import(client, tmp_path):
    """The reverse: the test copy keys the message by its uid, the live backup by its row (an earlier row there shares
    the uid and keeps the uid key). A version 3 file names the uid key only, so decisions-apply withholds the row
    holding that key; the next live import holds the message sharing its uid for review (its text differs from the
    row the key names). Exported again with 0.6.2 (version 4) the JARVIS row is named."""
    mark = "zqdrev62"
    shared = bj.new_uid()
    test_copy = bj.mini(tmp_path / "test")
    bj.add_session(test_copy["db"], "d_02", started=T)
    bj.add_row(test_copy["db"], "d_02", "user", "What is it worth?", T + 1)
    r = bj.add_row(test_copy["db"], "d_02", "assistant", secret(mark), T + 2, uid=shared)
    await run(test_copy)
    await withhold(client, (await message_of("d_02", r))[0].id)
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    as_v3(out)
    await _fresh_production()
    live = bj.mini(tmp_path / "live")
    bj.add_session(live["db"], "d_02", started=T)
    bj.add_row(live["db"], "d_02", "assistant", "An earlier answer.", T + 0.5, uid=shared)  # keeps the uid key
    bj.add_row(live["db"], "d_02", "user", "What is it worth?", T + 1)
    bj.add_row(live["db"], "d_02", "assistant", secret(mark), T + 2, uid=shared)
    production = await run(live)
    assert (await imp.decisions_apply(str(out), production["run_id"], actor=ADMIN[0], apply=True))["removals"]
    later = await run(live)
    assert (await row_with(mark)) is None
    [held] = await message_of("d_02", 3)
    assert held.withheld and (await stones())[held.source_key].review == "pending"
    await leak_free(client, mark, live, (later["run_id"],))


@pytest.mark.parametrize("test_uids", [False, True], ids=["row_key_to_uid_key", "uid_key_to_row_key"])
async def test_d_both_directions_v4(client, tmp_path, test_uids):
    """D in both directions with a version 4 file: the test copy keys the message by its row and the live
    installation by its uid, or the reverse (a backup without uids). The file carries the JARVIS row the admin
    withheld, so decisions-apply finds the live row at once, whatever key it is stored under, and later imports keep
    it withheld."""
    mark = "zqdfour%s62" % ("u" if test_uids else "r")
    turns = [("user", "What is it worth?"), ("assistant", secret(mark))]
    test_copy = bj.mini(tmp_path / "test")
    ids = conversation(test_copy["db"], "d_04", T, turns, uid=test_uids)
    await run(test_copy)
    [withheld] = await message_of("d_04", ids[1])
    await withhold(client, withheld.id)
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    exported = json.loads(out.read_text())
    assert exported["version"] == 4
    [removal] = [r for r in exported["removals"] if r["source_key"] == withheld.source_key]
    assert (removal["session"], removal["row"], removal["role"], removal["anchor"]) == (
        "d_04", str(ids[1]), "assistant", "")
    assert removal["uid_known"] is test_uids and bool(removal["uid"]) is test_uids
    await _fresh_production()
    live = bj.mini(tmp_path / "live")
    assert conversation(live["db"], "d_04", T, turns, uid=not test_uids) == ids  # the same JARVIS rows
    production = await run(live)
    [stored] = await message_of("d_04", ids[1])
    assert stored.source_key != withheld.source_key and not stored.withheld  # keyed otherwise here
    result = await imp.decisions_apply(str(out), production["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"withheld": 1}
    assert await row_with(mark) is None
    assert (await stones())[stored.source_key].source_row_id == str(ids[1])
    later = await run(live)
    await leak_free(client, mark, live, (later["run_id"],))
    [question] = await message_of("d_04", ids[0])
    assert (question.withheld, question.content) == (False, "What is it worth?")


async def test_decisions_v4_file_holds_no_text_and_no_fingerprint(client, fx, tmp_path):
    """A version 4 decisions file (and its --removals-only form, which restore.ps1 writes) carries keys, JARVIS ids,
    roles, uids and the rule that joined each stone: never text, never a fingerprint of text (I5). Holds still waiting
    for review are not exported (an import there finds them again); a hold an admin kept is, as kept; a release
    travels as its group and JARVIS row."""
    from app.brain import review

    from .test_brain_holds import STUB, _tool_backup

    mark, tool_mark, kept_mark, purge_mark = "zqdfile62", "zqdtool62", "zqdkept62", "zqdpurge62"
    _head_copy_backup(fx, mark)  # a carried-on copy: a rowless duplicate stone joins the withhold
    tool = _tool_backup(fx, tool_mark)
    twin = bj.prune_twin(fx["db"], "prune_01", tool[1], STUB)
    bj.add_session(fx["db"], "prune_02", started=T + DAY)
    bj.add_row(fx["db"], "prune_02", "user", "Run the second report.", T + DAY + 1)
    kept_tool = bj.add_row(fx["db"], "prune_02", "tool", "REPORT " + secret(kept_mark), T + DAY + 2,
                           tool_call_id="call_2", tool_name="terminal")
    kept_twin = bj.prune_twin(fx["db"], "prune_02", kept_tool, STUB)
    os.makedirs(os.path.join(fx["profile"], "knowledge"), exist_ok=True)
    with open(os.path.join(fx["profile"], "knowledge", "vendors.md"), "w", encoding="utf-8") as f:
        f.write("# Vendors\nThe courier code is %s.\n" % purge_mark)
    await run(fx)
    await withhold_text(client, mark)
    for sid, twin_id, row_id, how in (("prune_01", twin, tool[1], "release"),
                                      ("prune_02", kept_twin, kept_tool, "keep")):
        [full] = await message_of(sid, twin_id)
        await withhold(client, full.id)
        [stub] = await message_of(sid, row_id)
        assert (await stones())[stub.source_key].review == identity.PENDING
        r = await client.post("/api/brain/holds/%d/%s" % (stub.id, how))
        assert r.status_code == 200, r.text
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key.like("%vendors.md")))
        await store.delete_item(s, item, actor=ADMIN[0])
        await review.purge(s, item, actor=ADMIN[0])
    async with session_scope() as s:  # what the database keeps and the file must not
        kept = set((await s.scalars(text("SELECT fp FROM brain_purged_content"))).all())
        kept |= set((await s.scalars(text("SELECT source_fp FROM archive_messages"))).all()) - {""}
        kept |= set((await s.scalars(text("SELECT source_sha256 FROM memory_items WHERE source_sha256 <> ''"))).all())
    assert any(v.startswith("k1:") for v in kept)
    pending = {k for k, st in (await stones()).items() if st.review == identity.PENDING}
    for name, removals_only in (("full.json", False), ("removals.json", True)):
        out = tmp_path / name
        result = await imp.decisions_export(str(out), removals_only=removals_only)
        flat = out.read_text()
        data = json.loads(flat)
        assert ("decisions" in data) is not removals_only and data.get("removals_only", False) is removals_only
        assert result["releases"] == 1 and len(data["releases"]) == 1
        assert set(data["releases"][0]) == {"anchor", "session", "row"} and data["releases"][0]["session"] == "prune_01"
        withholds = [r for r in data["removals"] if r["reason"] == identity.WITHHELD_BY_ADMIN]
        purges = [r for r in data["removals"] if r["reason"] != identity.WITHHELD_BY_ADMIN]
        assert all(set(r) == set(imp.WITHHOLD_FIELDS) for r in withholds)
        assert [set(r) for r in purges] == [{"source_key", "reason"}]
        assert not pending & {r["source_key"] for r in withholds}  # holds waiting for review stay here
        assert {r["review"] for r in withholds} == {"", identity.KEPT}
        assert any(r["via"] == "duplicate" for r in withholds)  # the carried-on copy's key, with no row
        for value in (mark, tool_mark, kept_mark, purge_mark, STUB, "REPORT", "w1:", "k1:", *kept):
            assert value not in flat, value
        ids_only = json.dumps([data["removals"], data["releases"]])
        assert not re.search(r"[0-9a-f]{40}", ids_only)  # no hash at all (uids are 32 hex digits)


async def test_owner_flow_test_copy_withhold_export_live_apply_later_backup(client, tmp_path):
    """Judge T3, inverted: the test copy imports backup 1 and an admin withholds the answer; decisions go to the live
    installation, which imports the same backup 1 and applies them; a later backup (one compaction later) is imported
    into live."""
    mark = "zqowner62"
    fx = bj.mini(tmp_path / "fx")
    _plain_backup(fx, mark)
    await run(fx)
    await withhold_text(client, mark)
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    await _fresh_production()
    live = await run(fx)
    assert (await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True))["removals"] == {
        "withheld": 1}
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100)
    later = await run(fx)
    await leak_free(client, mark, fx, (later["run_id"],))


# --- Branches, rotation, pruning, folds, renumbering ---------------------------------------------------------------


async def test_gateway_branch_copy_with_fresh_uid_is_withheld_and_new_turns_stay_readable(client, fx):
    mark = "zqgateway62"
    _plain_backup(fx, mark, sid="gw_parent")
    await run(fx)
    await withhold_text(client, mark)
    bj.gateway_branch(fx["db"], "gw_parent", "gw_child", started_at=T + DAY)
    new_turn = "A new question in the branch about the budget."
    bj.add_row(fx["db"], "gw_child", "user", new_turn, T + DAY + 10)
    repeat = bj.add_row(fx["db"], "gw_child", "assistant", secret(mark), T + DAY + 20)  # its own uid, after the start
    second = await run(fx)
    copies = [m for m in bj.rows(fx["db"], "gw_child") if m["content"] == secret(mark) and m["id"] != repeat]
    for c in copies:
        assert all(m.withheld for m in await message_of("gw_child", c["id"]))
    assert await readable(new_turn)
    [later] = await message_of("gw_child", repeat)  # O10: a new turn of the branch with its own uid
    assert not later.withheld
    allowed = ("archive_messages.content", "archive_messages.search", "archive_sessions.search",
               "archive_sessions.summary")
    assert await bj.nowhere(mark, allow=allowed) == []
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}
    await inverse_readable()


@pytest.mark.parametrize("how", ["cli_branch", "rotation"])
async def test_cli_branch_and_rotation_child_copies_same_uid(client, fx, how):
    mark = "zq%s62" % how.replace("_", "")
    _plain_backup(fx, mark, sid="br_parent")
    await run(fx)
    await withhold_text(client, mark)
    (bj.cli_branch if how == "cli_branch" else bj.rotate)(fx["db"], "br_parent", "br_child", started_at=T + DAY)
    second = await run(fx)
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


def _tool_backup(fx: dict[str, str], mark: str) -> list[int]:
    bj.add_session(fx["db"], "prune_01", started=T)
    return [bj.add_row(fx["db"], "prune_01", "user", "Run the vendor report.", T + 1),
            bj.add_row(fx["db"], "prune_01", "tool", "REPORT " + secret(mark), T + 2, tool_call_id="call_1",
                       tool_name="terminal"),
            bj.add_row(fx["db"], "prune_01", "assistant", "The report ran.", T + 3)]


async def test_prune_twin_after_withhold_is_member(client, fx):
    mark = "zqprunetwin62"
    ids = _tool_backup(fx, mark)
    await run(fx)
    await withhold(client, (await message_of("prune_01", ids[1]))[0].id)
    twin = bj.prune_twin(fx["db"], "prune_01", ids[1])
    second = await run(fx)
    [archived] = await message_of("prune_01", twin)
    assert archived.withheld and archived.state == "compacted"
    assert (await stones())[archived.source_key].review == ""
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


async def test_prune_stub_after_withholding_the_twin_is_a_hold(client, fx):
    """The admin withholds the archived full text; the live stub with the same uid holds other text: it is held for
    review (released only by an admin), never a member."""
    mark = "zqprunestub62"
    ids = _tool_backup(fx, mark)
    twin = bj.prune_twin(fx["db"], "prune_01", ids[1])
    await run(fx)
    await withhold(client, (await message_of("prune_01", twin))[0].id)
    second = await run(fx)
    [stub] = await message_of("prune_01", ids[1])
    assert stub.withheld
    assert (await stones())[stub.source_key].review == "pending"
    await leak_free(client, mark, fx, (second["run_id"],))
    st = await imp.status()
    assert st["held_for_review"] >= 1


async def test_fold_absorbed_and_survivor_are_members_never_releasable(client, fx):
    mark = "zqfold62"
    ids = conversation(fx["db"], "fold_01", T, [("user", secret(mark)), ("assistant", "Noted."),
                                                ("user", "Anything else?"), ("assistant", "No.")])
    await run(fx)
    await withhold(client, (await message_of("fold_01", ids[0]))[0].id)
    bj.fold(fx["db"], "fold_01", survivor=ids[2], dropped=[ids[0]])  # the withheld text folded into a survivor
    second = await run(fx)
    [survivor] = await message_of("fold_01", ids[2])
    assert survivor.withheld
    stone = (await stones())[survivor.source_key]
    assert (stone.review, stone.via) == ("", "merged into another message")
    await leak_free(client, mark, fx, (second["run_id"],))
    await inverse_readable()


async def test_replace_messages_renumbering_keeps_withheld_and_adopts_readable(client, fx):
    mark = "zqrenumber62"
    shared = bj.new_uid()
    bj.add_session(fx["db"], "renum_01", started=T)
    bj.add_row(fx["db"], "renum_01", "user", "Question one.", T + 1, uid=shared)
    secret_row = bj.add_row(fx["db"], "renum_01", "assistant", secret(mark), T + 2)
    copy_row = bj.add_row(fx["db"], "renum_01", "assistant", "Readable answer carried on.", T + 3, uid=shared)
    await run(fx)
    before = await _archive_count("renum_01")
    [carried] = await message_of("renum_01", copy_row)  # its key is row-form: the uid belongs to the first row
    await withhold(client, (await message_of("renum_01", secret_row))[0].id)
    mapping = bj.replace_renumber(fx["db"], "renum_01")
    second = await run(fx)
    assert await _archive_count("renum_01") == before  # nothing stored twice
    [adopted] = await message_of("renum_01", mapping[copy_row])
    assert adopted.id == carried.id and not adopted.withheld and adopted.content == "Readable answer carried on."
    assert second["report"]["keys_adopted"] >= 1
    await leak_free(client, mark, fx, (second["run_id"],))


async def test_uid_backfill_mid_way(client, fx):
    """N3: withheld while its uid was NULL (key <session>:<row>); then a compaction copies it (Hermes mints the copy a
    uid of its own) and a backfill gives the original another: the copy is held for review (no uid was known, so the
    independence guard does not apply)."""
    mark = "zqbackfill62"
    _plain_backup(fx, mark, uid=False)
    await run(fx)
    await withhold_text(client, mark)
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100)
    bj.backfill_uids(fx["db"], "a_tail_01")
    second = await run(fx)
    held = (await records(second["run_id"]))
    assert any(r.action == "withheld" for r in held.values())
    await leak_free(client, mark, fx, (second["run_id"],))


# --- Repeated imports, rollback, promotion --------------------------------------------------------------------------


async def test_repeated_imports_same_backup_three_times_no_updates_no_new_stones(client, fx):
    mark = "zqrepeat62"
    _plain_backup(fx, mark)
    await run(fx)
    await withhold_text(client, mark)
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100)
    await run(fx)
    rows = await bj.table_counts(exclude=bj.HISTORY_TABLES)
    known = await stones()
    for _ in range(3):
        again = await run(fx)
        assert set(again["report"]["by_action"]) <= {"duplicate", "excluded"}, again["report"]["by_action"]
        assert again["report"]["stones_added"] == 0
    assert await bj.table_counts(exclude=bj.HISTORY_TABLES) == rows
    assert set(await stones()) == set(known)
    await leak_free(client, mark, fx, (again["run_id"],))


async def test_later_backups_three_times(client, fx):
    mark = "zqlater62"
    _plain_backup(fx, mark)
    await run(fx)
    await withhold_text(client, mark)
    for n in range(3):
        bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100 * (n + 1))
        bj.add_row(fx["db"], "a_tail_01", "user", "Follow-up %d about the schedule." % n, T + 100 * (n + 1) + 50)
        last = await run(fx)
        assert await bj.nowhere(mark) == []
        v = await imp.verify(last["run_id"], bj.paths(fx))
        assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}
    await leak_free(client, mark, fx, (last["run_id"],))
    await inverse_readable()


async def test_rollback_of_a_run_that_inherited_stones_and_adopted_keys_is_exact(client, fx):
    mark = "zqrollback62"
    ids = conversation(fx["db"], "c1_01", T, [("user", "What is the contract worth?"), ("assistant", secret(mark)),
                                              ("user", "Thanks.")], uid=False)
    await run(fx)
    [row] = await message_of("c1_01", ids[1])
    await withhold(client, row.id)
    async with session_scope() as s:
        keys_before = dict((await s.execute(select(ArchiveMessage.id, ArchiveMessage.source_key))).all())
    counts = await bj.table_counts(exclude=bj.HISTORY_TABLES + ("brain_tombstones",))
    bj.backfill_uids(fx["db"], "c1_01")
    second = await run(fx)
    assert second["report"]["keys_adopted"] == len(ids)
    inherited = set(await stones())
    rolled = await imp.rollback(second["run_id"], confirm=True)
    assert rolled["status"] == "rolled_back"
    assert await bj.table_counts(exclude=bj.HISTORY_TABLES + ("brain_tombstones",)) == counts
    async with session_scope() as s:
        assert dict((await s.execute(select(ArchiveMessage.id, ArchiveMessage.source_key))).all()) == keys_before
        back = await s.get(ArchiveMessage, row.id)
    assert back.withheld and back.source_fp.startswith("w1:")
    assert set(await stones()) == inherited  # stones are never deleted by a rollback
    again = await run(fx)  # the same backup again: the same outcome
    await leak_free(client, mark, fx, (again["run_id"],))


async def test_promote_refused_for_every_member_and_hold(client, fx):
    mark = "zqpromote62"
    ids = _tool_backup(fx, mark)
    twin = bj.prune_twin(fx["db"], "prune_01", ids[1])
    await run(fx)
    await withhold(client, (await message_of("prune_01", twin))[0].id)
    await run(fx)
    rows_ = [m for i in (ids[1], twin) for m in await message_of("prune_01", i)]
    assert {(await stones())[m.source_key].review for m in rows_} == {"", "pending"}
    for who in ("operator", "admin"):
        await as_(client, who)
        for m in rows_:
            r = await client.post("/api/brain/messages/%d/promote" % m.id, json={"kind": "project", "title": "P"})
            assert r.status_code in (400, 403), (who, m.id, r.text)
    await as_(client, "operator")
    [plain] = await message_of("prune_01", ids[0])
    r = await client.post("/api/brain/messages/%d/promote" % plain.id, json={"kind": "project", "title": "P"})
    assert r.status_code == 201, r.text  # the request in the same conversation is not the same message


async def test_api_access_sweep_after_each_trigger(client, fx):
    """Every GET route of the API, as every role and as the n8n service token, after the A-tail trigger: none shows
    the withheld text; the service token reads nothing of the brain."""
    mark = "zqsweep62"
    _plain_backup(fx, mark)
    await run(fx)
    original = await withhold_text(client, mark)
    bj.hermes_compact(fx["db"], "a_tail_01", head_n=1, tail_n=6, ts=T + 100)
    second = await run(fx)
    await allow_ai_context()
    async with session_scope() as s:
        ids = {"session_id": original.session_id, "message_id": original.id,
               "run_id": second["run_id"], "item_id": await s.scalar(select(func.max(MemoryItem.id))) or 1}
    await as_(client, "admin")
    spec = (await client.get("/api/openapi.json")).json()
    paths = []
    for path, methods in spec["paths"].items():
        if "get" not in methods:
            continue
        filled = path
        for name, value in ids.items():
            filled = filled.replace("{%s}" % name, str(value))
        if "{" not in filled:
            paths.append(filled)
    assert len(paths) > 30 and "/api/brain/search" in paths
    for who in ("viewer", "operator", "operator+", "admin", "admin+"):
        await as_(client, who)
        for path in paths:
            r = await client.get(path, params={"q": mark})
            assert mark not in r.text, (who, path)
    client.cookies.clear()
    for path in paths:
        r = await client.get(path, headers={"Authorization": "Bearer %s" % SERVICE_TOKEN}, params={"q": mark})
        assert mark not in r.text, path
        if path.startswith("/api/brain/"):
            assert r.status_code in (401, 403), (path, r.status_code)


# --- Findings 2 and 3: a restricted conversation -------------------------------------------------------------------


SSN = "Her SSN is 219-09-9999"


async def _promoted_copy(client, message_id: int, title: str) -> int:
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % message_id, json={"kind": "project", "title": title})
    assert r.status_code == 201, r.text
    item = r.json()["item"]["id"]
    await as_(client, "admin")
    r = await client.post("/api/brain/items/%d/review" % item, json={"decision": "approve"})
    assert r.status_code == 200, r.text
    return item


async def _emptied(item_id: int) -> bool:
    async with session_scope() as s:
        item = await s.get(MemoryItem, item_id)
        return item.title == store.WITHHELD_COPY_TITLE and item.body == "" and item.status == "quarantined"


@pytest.mark.parametrize("later", [False, True], ids=["K2A_same_import", "K2B_later_import"])
async def test_finding_2_restricting_import_empties_copies_of_a_rewound_row(client, fx, later):
    mark = "zqktwo%s62" % ("b" if later else "a")
    ids = conversation(fx["db"], "k2_01", T, [("user", "Report please."), ("assistant", "Shop report " + mark),
                                              ("user", "Thanks.")])
    await run(fx)
    copy = await _promoted_copy(client, (await message_of("k2_01", ids[1]))[0].id, "Report " + mark)
    bj.change(fx["db"], "UPDATE messages SET active = 0 WHERE id = ?", ids[1])  # rewound in JARVIS
    if later:
        await run(fx)
    bj.add_row(fx["db"], "k2_01", "user", SSN, T + 10)
    last = await run(fx)
    assert await _emptied(copy)
    await leak_free(client, mark, fx, (last["run_id"],))


@pytest.mark.parametrize("later", [False, True], ids=["K3A_same_import", "K3B_later_import"])
async def test_finding_3_a_row_the_source_no_longer_holds_is_withheld_in_a_restricted_conversation(client, fx, later):
    mark = "zqkthree%s62" % ("b" if later else "a")
    ids = conversation(fx["db"], "k3_01", T, [("user", "Report please."), ("assistant", "Shop report " + mark),
                                              ("user", "Thanks.")])
    await run(fx)
    bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", ids[1])  # deleted in JARVIS
    if later:
        await run(fx)
    bj.add_row(fx["db"], "k3_01", "user", SSN, T + 10)
    last = await run(fx)
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "k3_01"))
        assert sess.sensitivity == "restricted" and sess.imported_messages == 0
    await leak_free(client, mark, fx, (last["run_id"],))
