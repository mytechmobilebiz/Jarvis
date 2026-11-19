"""O2 at rest (rebuild S5; round-1 groups-paths P2 case D, the round-3 oracle's reading of import_records,
gd1:oracle_rollback_records_off): a rollback copy (import_records.before) of a row of a group conversation keeps no
text, whether the row was changed in place or deleted. Reported: such a copy with text (content or tool calls), in a
certain or an uncertain group conversation, also one whose conversation only its source key places, and one holding
other text than an admin released for the row. Not reported: a blank copy, Hermes's prune placeholder, a copy of a
conversation outside the group, and a copy holding exactly the released text. The copies as cc_adapter.rollback_copies
returns them; no database, synthetic text only. The database side (the conversation of a copy whose row was deleted)
is test_brain_groups_d1_062.py::test_p2_the_oracle_names_the_conversation_of_a_copy_whose_row_was_deleted."""

from __future__ import annotations

from . import hermes_sim as hs
from . import oracle as o
from . import selftest_kit as k

W = "Please rewrite the landing page copy for the team meeting."
OLDER = "The older words of the follow-up mail, before JARVIS changed them."


def _setup(tmp):
    w = k.world(tmp)
    p = w.session()
    c = w.session(parent=p, cfg={"_branched_from": p}, kind="branch")
    u = w.session()
    rp, _ = w.row(p, "user", W)
    rc, _ = w.row(c, "user", "Draft the follow-up mail, please.")
    ru, _ = w.row(u, "user", "Book the train for Tuesday.")
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap)
    run = k.run_of(w, snap)
    k.withhold(run, rows, p, rp)
    held = [dict(r, withheld=True, content="") if r["sid"] in (p, c) else r for r in rows]
    return run, held, snap, p, c, u


def copy(sid: str | None, content: str = OLDER, *, n: int = 1, deleted: bool = True, key: str = "",
         calls: list | None = None) -> dict:
    return {"id": n, "run_id": 2, "target_id": 100 + n, "deleted": deleted, "sid": sid,
            "source_key": key or "jarvis:message:uid:copy%d" % n, "row_id": 100 + n, "content": content,
            "tool_calls": calls, "withheld": False, "flags": {}}


def _check(run, held, snap, gs, copies):
    run.problems.clear()
    st = k.state(held)
    st["copies"] = copies
    o.check_c(run, "self-test", st, gs, snap)
    return [x for x in run.problems if "rollback copy" in x]


def test_s5_a_rollback_copy_of_a_group_row_with_text_is_reported(tmp_path):
    run, held, snap, p, c, u = _setup(tmp_path)
    gs = k.groups({p, c})
    for one in (copy(c), copy(c, deleted=False), copy(p, n=2), copy(c, "", calls=[{"arguments": OLDER}], n=3)):
        found = _check(run, held, snap, gs, [one])
        assert len(found) == 1 and found[0].startswith("O2") and one["sid"] in found[0], (one, found)


def test_s5_a_rollback_copy_in_an_uncertain_conversation_is_reported(tmp_path):
    run, held, snap, p, c, u = _setup(tmp_path)
    found = _check(run, held, snap, k.groups({p, c}, uncertain={u}), [copy(u)])
    assert len(found) == 1 and u in found[0], found


def test_s5_a_copy_its_source_key_places_in_the_group_is_reported_and_an_unplaced_one_is_noted(tmp_path):
    run, held, snap, p, c, u = _setup(tmp_path)
    key = next(r["source_key"] for r in held if r["sid"] == c)
    found = _check(run, held, snap, k.groups({p, c}), [copy(None, key=key)])
    assert len(found) == 1 and c in found[0], found
    run.notes.clear()
    assert _check(run, held, snap, k.groups({p, c}), [copy(None, key="jarvis:message:uid:nowhere")]) == []
    assert any("name no stored conversation" in x for x in run.notes), run.notes


def test_s5_a_released_rows_copy_is_admitted_only_with_exactly_the_released_text(tmp_path):
    run, held, snap, p, c, u = _setup(tmp_path)
    one = copy(c, key="jarvis:message:uid:released")
    run.released[one["source_key"]] = [OLDER]
    assert _check(run, held, snap, k.groups({p, c}), [one]) == []
    run.released[one["source_key"]] = ["Some other text an admin released for this row."]
    assert len(_check(run, held, snap, k.groups({p, c}), [one])) == 1


def test_s5_a_blank_copy_a_prune_placeholder_and_another_conversations_copy_are_not_reported(tmp_path):
    run, held, snap, p, c, u = _setup(tmp_path)
    gs = k.groups({p, c})
    assert _check(run, held, snap, gs, [copy(c, ""), copy(c, hs.STUB, n=2), copy(u, n=3)]) == []
    assert _check(run, held, snap, gs, None) == []  # no copies read: nothing to check (the check is in check_c)
