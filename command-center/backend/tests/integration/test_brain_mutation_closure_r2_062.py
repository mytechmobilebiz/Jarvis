"""0.6.2 option C (rebuild R1): closing tests of round 2's groups guards (m18:j8_always_two). Synthetic data only."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import BrainWithheldSession

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def test_an_orphaned_branch_with_one_copied_row_joins_its_group_by_provenance(client, fx):
    """J9: a gateway branch whose parent (a middle branch, deleted between two backups) no backup ever held: its branch
    marker is its provenance, so one row at the very role and time of a withheld conversation's row joins it as a
    member (a markerless unanchored conversation would need two: J8)."""
    db = fx["db"]
    pa = conversation(db, "ob_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqob"))])
    bj.add_session(db, "ob_b", started=T + 2 * DAY, model_config={"_branched_from": "ob_middle"})
    bj.add_row(db, "ob_b", "user", "What is the deal worth?", T + 1, uid=False)
    bj.add_row(db, "ob_b", "user", "A new question in the branch.", T + 2 * DAY + 5, uid=False)
    bj.add_session(db, "ob_u", started=T + 3 * DAY)  # the same single copied row, no marker
    bj.add_row(db, "ob_u", "user", "What is the deal worth?", T + 1, uid=False)
    await run(fx)
    await withhold(client, (await message_of("ob_p", pa[1]))[0].id)
    async with session_scope() as s:
        found = {w.source_session_id: (w.how, w.state)
                 for w in (await s.scalars(select(BrainWithheldSession))).all()}
    assert found["ob_b"] == ("provenance", "member")
    assert found["ob_u"] == ("copied_rows", "uncertain")
