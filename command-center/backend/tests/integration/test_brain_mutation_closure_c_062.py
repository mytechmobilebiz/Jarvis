"""0.6.2 option C (rebuild R1): closing tests of the groups guards the mutation matrix names (m18 rows): the title
trigger by membership alone, the R9 repair of rollback copies written without the sweep, a pending hold never seeding
a group, and a gateway reset never joining by copied history. Each fails with its guard switched off. Synthetic data
only."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.brain import groups, identity, review
from app.db import session_scope
from app.models import ArchiveSession, BrainWithheldSession, ImportRecord

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def group() -> dict[str, tuple[str, str]]:
    async with session_scope() as s:
        return {w.source_session_id: (w.how, w.state)
                for w in (await s.scalars(select(BrainWithheldSession))).all()}


async def join(sid: str, anchor: str = "jarvis:message:uid:anchor") -> None:
    async with session_scope() as s:
        await groups.record(s, anchor, {sid: groups.Member("child", groups.MEMBER, "elsewhere")})
        await s.commit()


async def test_title_trigger_hides_the_title_of_a_group_conversation_by_membership_alone(client, fx):
    """m18:title_trig_groups: a conversation recorded in a group loses its title at once (no withhold in it, no title
    choice), its rollback copies too, and every later write (a re-import) keeps it hidden."""
    db = fx["db"]
    conversation(db, "tt_a", T, [("user", "Plan the garden party.")], title="Garden party plans")
    await run(fx)
    bj.change(db, "UPDATE sessions SET title = 'Garden party plans, revised' WHERE id = 'tt_a'")
    bj.add_row(db, "tt_a", "assistant", "Revised.", T + 50)
    await run(fx)
    async with session_scope() as s:
        copies = (await s.scalars(select(ImportRecord.before["title"].astext).where(
            ImportRecord.target_table == "archive_sessions", ImportRecord.before.has_key("title")))).all()
    assert "Garden party plans" in copies
    await join("tt_a")
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "tt_a"))
        assert sess.title == "" and sess.flags.get("title_withheld") is True
        assert set((await s.scalars(select(ImportRecord.before["title"].astext).where(
            ImportRecord.target_table == "archive_sessions", ImportRecord.before.has_key("title")))).all()) == {""}
    bj.change(db, "UPDATE sessions SET title = 'Garden party plans, third' WHERE id = 'tt_a'")
    await run(fx)
    async with session_scope() as s:
        assert await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == "tt_a")) == ""
        await s.execute(text("UPDATE archive_sessions SET title = 'Written by any code' WHERE source_session_id = "
                             "'tt_a'"))
        await s.commit()
        assert await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == "tt_a")) == ""


async def test_r9_the_repair_blanks_a_rollback_copy_of_a_group_row_written_without_the_sweep(client, fx):
    """m18:r9_off: a rollback copy of a row of a group conversation that still holds text (no sweep wrote it) is
    blanked by the repair, whatever row of the conversation it copies."""
    db = fx["db"]
    mark = "zqr9"
    ids = conversation(db, "r9_a", T, [("user", "Notes about %s, first draft." % mark), ("assistant", "Noted.")])
    await run(fx)
    bj.change(db, "UPDATE messages SET content = 'Notes, second draft.' WHERE id = ?", ids[0])
    await run(fx)

    async def with_text() -> int:
        async with session_scope() as s:
            return int(await s.scalar(text("SELECT count(*) FROM import_records WHERE record_type = 'message' AND "
                                           "before::text LIKE '%' || :m || '%'"), {"m": mark}))
    assert await with_text() == 1
    await join("r9_a")
    async with session_scope() as s:
        out = await identity.repair_rows(s, cause="startup-check")
        await s.commit()
    assert out["group_rollback_copies_blanked"] == 1 and await with_text() == 0


async def test_a_pending_hold_never_seeds_a_group(client, fx):
    """m18:seed_pending_too: a row of another conversation held for review in a withhold's group (pending) never puts
    its conversation in the group; a member (certain) does."""
    db = fx["db"]
    pa = conversation(db, "sp_a", T, [("user", "What is the deal worth?"), ("assistant", secret("zqsp"))])
    ba = conversation(db, "sp_b", T + 30 * DAY, [("user", "Unrelated: book a table.")])
    ca = conversation(db, "sp_c", T + 31 * DAY, [("user", "Unrelated: call the plumber.")])
    await run(fx)
    [w] = await message_of("sp_a", pa[1])
    await withhold(client, w.id)
    async with session_scope() as s:
        b = (await message_of("sp_b", ba[0]))[0]
        c = (await message_of("sp_c", ca[0]))[0]
        await review.hold_message(s, await s.get(type(b), b.id), anchor=w.source_key, actor="admin@test.local",
                                  cause="test", rule="same message id")
        await review.hold_message(s, await s.get(type(c), c.id), anchor=w.source_key, actor="admin@test.local",
                                  cause="test", rule=identity.FOLD, review=identity.CERTAIN)
        await groups.refresh(s)
        await s.commit()
    found = await group()
    assert "sp_b" not in found and found["sp_c"] == ("withheld_here", "member")


async def test_a_reset_with_copied_looking_rows_never_joins_by_copied_history(client, fx):
    """m18:j8_reset_copies: a gateway reset copies no rows; one whose parent no backup holds, with rows at the very
    role and time of the withheld conversation's rows, joins only as 'uncertain' (held, never a member)."""
    db = fx["db"]
    pa = conversation(db, "rc_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqrc")),
                                      ("user", "Thanks.")])
    bj.add_session(db, "rc_r", started=T + DAY, model_config={"_reset_from": "rc_gone"})
    for n, (role, content) in enumerate([("user", "What is the deal worth?"), ("user", "Thanks.")]):
        bj.add_row(db, "rc_r", role, content, T + (1 if n == 0 else 3))
    await run(fx)
    await withhold(client, (await message_of("rc_p", pa[1]))[0].id)
    assert (await group())["rc_r"] == ("copied_rows", "uncertain")
