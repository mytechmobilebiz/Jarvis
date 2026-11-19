"""D-52 (rebuild S7, fresh seeds 13315 and 13418, P3 / O2): O2/O7 read the Command Center's stored cut of a released
text as that text. The spec stores a tool result longer than 4,000 characters cut near the limit with the cut note; a
released session_search read of a group conversation then shows the beginning of the text the admin released, which
the oracle reported as "readable and was never released as it is". Self-tests on crafted states (no database): the
stored cut of the released text passes; a cut of another text, a beginning without the note, a beginning that stops
far before the limit, and the note on a text the contract does not cut are still reported. Synthetic text only."""

from __future__ import annotations

import json

from . import oracle as o
from . import selftest_kit as k

W1 = "Please rewrite the landing page copy for the team meeting."


def _read(n: int, word: str = "newsletter") -> str:
    """A session_search read longer than the 4,000 characters the spec stores of a tool result."""
    return json.dumps({"success": True, "mode": "read", "session_id": "s_other",
                       "messages": [{"id": i, "role": "tool", "content": "Fetched page for the %s draft: section "
                                     "headings, a short table and the closing notes." % word} for i in range(n)]})


def _cut(text: str) -> str:
    """The Command Center's stored form of a tool result (D-49's stored_form)."""
    room = o.CC_STORED["tool"] - len(o.CC_CUT_NOTE)
    head = text[:room]
    space = max(head.rfind(" "), head.rfind("\n"))
    if space > room - 400:
        head = head[:space]
    return head.rstrip() + o.CC_CUT_NOTE


def _setup(tmp, stored: str, released: str):
    w = k.world(tmp)
    a = w.session()
    r1, _ = w.row(a, "user", W1)
    x, _ = w.row(a, "tool", released)
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    rx["content"] = stored
    run.released[rx["source_key"]] = [released]
    return run, rows, snap, a, rx


def _o2(run, rows, snap, a, rx) -> list[str]:
    o.check_c(run, "self-test", k.state(rows), k.groups({a}), snap)
    return [p for p in run.problems if p.startswith("O2") and "row %d " % rx["id"] in p]


def test_d52_the_stored_cut_of_a_released_tool_result_is_the_released_text(tmp_path):
    full = _read(60)
    assert len(full) > o.CC_STORED["tool"]
    run, rows, snap, a, rx = _setup(tmp_path, _cut(full), full)
    assert not _o2(run, rows, snap, a, rx), run.problems


def test_d52_a_cut_of_another_text_is_still_reported(tmp_path):
    full = _read(60)
    other = _read(60, "pricing")
    run, rows, snap, a, rx = _setup(tmp_path, _cut(other), full)
    assert _o2(run, rows, snap, a, rx), run.problems


def test_d52_a_beginning_without_the_cut_note_or_far_before_the_limit_is_still_reported(tmp_path):
    full = _read(60)
    run, rows, snap, a, rx = _setup(tmp_path, _cut(full)[:-len(o.CC_CUT_NOTE)], full)
    assert _o2(run, rows, snap, a, rx), run.problems
    run, rows, snap, a, rx = _setup(tmp_path / "b", full[:1000] + o.CC_CUT_NOTE, full)
    assert _o2(run, rows, snap, a, rx), run.problems


def test_d52_the_cut_note_on_a_text_the_contract_does_not_cut_is_still_reported(tmp_path):
    short = _read(5)
    assert len(short) < o.CC_STORED["tool"]
    run, rows, snap, a, rx = _setup(tmp_path, short[:len(short) // 2] + o.CC_CUT_NOTE, short)
    assert _o2(run, rows, snap, a, rx), run.problems


def test_d52_a_cut_released_row_carrying_another_withheld_text_is_still_a_leak(tmp_path):
    """The narrowing is O7's 'released as it is' only: a stored cut that carries a withheld text (O8) is reported."""
    w = k.world(tmp_path)
    a, b = w.session(), w.session()
    r1, _ = w.row(a, "user", W1)
    w2 = "Please double check the partner agreement before lunch."
    r2, lm2 = w.row(b, "user", w2)
    full = json.dumps({"messages": [{"id": 1, "role": "user", "content": w2}]}) + " " + _read(60)
    x, _ = w.row(a, "tool", full, carries={lm2})
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (b, r2)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    k.withhold(run, rows, b, r2)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    rx["content"] = _cut(full)
    run.released[rx["source_key"]] = [full]
    o.check_c(run, "self-test", k.state(rows), k.groups({a, b}), snap)
    assert [p for p in run.problems if p.startswith("O8") and "row %d " % rx["id"] in p], run.problems
