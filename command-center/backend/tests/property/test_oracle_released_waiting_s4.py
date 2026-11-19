"""D-50 (rebuild S4): A4 / B4 excuse a row outside the family the admin released that waits for its text because
the installation's last backup no longer holds that JARVIS row (seeds 13091, 13345, 13376, 13805, 13885, 13887: a
carrier released by the scripted review; the next backups dropped the row, so no import can bring its text back and
the Command Center never kept it). Narrow: the row must be released and waiting (withheld, released, no stone), absent
from the last backup, and the plain import must show exactly the released text. Still reported: the same row when the
last backup holds it, a row held (a stone) or never released, and a plain text other than the released one. No
database, synthetic text only."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k

W = "Please rewrite the landing page copy for the team meeting."
OTHER = "Finished the vendor review; the notes are in the shared folder now."


def _case(tmp, *, in_last: bool = False, released: bool = True, stone: bool = False, text: str = OTHER):
    w = k.world(tmp)
    a, c = w.session(), w.session()
    r1, _ = w.row(a, "user", W)
    x, _ = w.row(c, "assistant", OTHER)
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (c, x)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    if released:
        rx["flags"] = {"withheld_by_admin": {"via": o.CARRIER_VIA, "released_at": "2026-11-19T00:00:00+00:00"}}
        run.released[rx["source_key"]] = [OTHER]
    mine = k.state(rows, [k.stone(rx, via="new in a later backup")] if stone else [])
    plain = k.state([dict(r, withheld=False, content=(text if r is rx else r["content"]) or "x") for r in rows])
    last = w.sim.backup("e")
    if not in_last:
        del last.rows[(c, x)]
    o.compare_outside(run, "A4", mine, plain, {a}, "self-test", last)
    return run


def test_s4_a_released_row_no_later_backup_holds_waits_and_is_noted(tmp_path):
    run = _case(tmp_path)
    assert not run.problems and any("D-50" in n for n in run.notes)


def test_s4_the_same_row_held_by_the_last_backup_is_still_a_difference(tmp_path):
    assert _case(tmp_path, in_last=True).problems


def test_s4_a_held_or_never_released_row_is_still_a_difference(tmp_path):
    assert _case(tmp_path / "a", stone=True).problems
    assert _case(tmp_path / "b", released=False).problems


def test_s4_a_plain_text_other_than_the_released_one_is_still_a_difference(tmp_path):
    assert _case(tmp_path, text="Something else was written here instead.").problems
