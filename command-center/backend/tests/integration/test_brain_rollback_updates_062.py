"""0.6.2 option C (rebuild R2): a rollback never brings back the older text of a row of a withheld group that an import
changed in place (m18:hgr_updated_copies_off). The central sweep (review.hold_group_rows) blanks the rollback copies of
every row of a group conversation, also those of a row already withheld by older code whose copies kept their text
(the 'legacy' shape: nothing else reaches that copy before the startup repair). Synthetic data only."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.brain import importer as imp
from app.db import session_scope

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import state
from .test_brain_withhold_identity import T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")
PRUNED = "[Old tool output cleared to save context space]"


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def copies_with(mark: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(text("SELECT count(*) FROM import_records WHERE record_type = 'message' "
                                       "AND before::text LIKE '%' || :m || '%'"), {"m": mark}))


async def readable_with(mark: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(text("SELECT count(*) FROM archive_messages WHERE NOT withheld "
                                       "AND content LIKE '%' || :m || '%'"), {"m": mark}))


@pytest.mark.parametrize("shape", ["current", "legacy"])
async def test_a_rollback_never_restores_a_pruned_tool_row_that_echoed_the_withheld_text(client, fx, shape):
    """A tool row of the withheld conversation echoed the withheld text; Hermes pruned it in place (a later import
    keeps the echo only in the update copy). After the withhold, a rollback of that import never puts the echo back:
    the sweep blanked the copy (current: the row is held now; legacy: the row was withheld by older code, its copy
    kept the text)."""
    db = fx["db"]
    mark = "zqrbu" + shape[:3]
    w = secret(mark)
    ids = conversation(db, "rbu_" + shape, T, [("user", "What is the deal worth?"), ("assistant", w)])
    tool = bj.add_row(db, "rbu_" + shape, "tool", "Result: %s" % w, T + 10, tool_call_id="call_rbu",
                      tool_name="web_search")
    await run(fx)
    bj.change(db, "UPDATE messages SET content = ? WHERE id = ?", PRUNED, tool)
    pruning = await run(fx)
    assert await copies_with(mark) >= 1  # the update copy keeps the echo
    if shape == "legacy":
        # 0.6.x withheld the row itself and left its rollback copies as they were
        async with session_scope() as s:
            await s.execute(text("UPDATE archive_messages SET withheld = true, content = '', source_fp = 'w1:live' "
                                 "WHERE id = :id"), {"id": (await message_of("rbu_" + shape, tool))[0].id})
            await s.commit()
    await withhold(client, (await message_of("rbu_" + shape, ids[1]))[0].id)
    assert await copies_with(mark) == 0
    await imp.rollback(pruning["run_id"], actor=ADMIN[0], confirm=True)
    assert await readable_with(mark) == 0 and await copies_with(mark) == 0
    assert await state("rbu_" + shape, tool) in ("held", "certain")
