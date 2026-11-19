"""O1's ground truth (rebuild R5): the group of a withheld message over the links any imported backup showed;
continuation, branch and reset both ways; a delegated sub-agent and a markerless child from the parent down only
(O-D1; the parent side is a pending owner decision, groups-paths B and D, and is not demanded); a shared uid; copied
history for a conversation no backup gave a parent. A missing recorded conversation is reported. No database."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k

W = "Please rewrite the landing page copy for the team meeting."


def _setup(tmp, kind: str, *, w_in_child: bool):
    w = k.world(tmp)
    p = w.session()
    cfg = {"delegated": {"_delegate_from": p}, "branch": {"_branched_from": p}, "reset": {"_reset_from": p},
           "continuation": {"_compressed_from": p}, "child": None}[kind]
    c = w.session(parent=p, cfg=cfg, kind=kind)
    rp, _ = w.row(p, "user", "Please archive the partner agreement before lunch.")
    rc, _ = w.row(c, "user", W)
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap)
    run = k.run_of(w, snap)
    k.withhold(run, rows, *((c, rc) if w_in_child else (p, rp)))
    return run, rows, snap, p, c


def test_o1_down_only_kinds_join_from_the_parent_down_only(tmp_path):
    for kind in ("delegated", "child"):
        run, rows, snap, p, c = _setup(tmp_path / kind / "p", kind, w_in_child=False)
        assert o.truth_group(run, snap, k.state(rows)) == {p, c}
        run, rows, snap, p, c = _setup(tmp_path / kind / "c", kind, w_in_child=True)
        assert o.truth_group(run, snap, k.state(rows)) == {c}, kind  # the parent side is pending (B, D)


def test_o1_both_ways_kinds(tmp_path):
    for kind in ("branch", "reset", "continuation"):
        run, rows, snap, p, c = _setup(tmp_path / kind, kind, w_in_child=True)
        assert o.truth_group(run, snap, k.state(rows)) == {p, c}, kind


def test_o1_a_group_conversation_not_recorded_is_reported(tmp_path):
    run, rows, snap, p, c = _setup(tmp_path, "delegated", w_in_child=False)
    held = [dict(r, withheld=True, content="") for r in rows]
    o.check_c(run, "self-test", k.state(held), k.groups({p}), snap)
    assert any(x.startswith("O1") and c in x for x in run.problems), run.problems
    run.problems.clear()
    o.check_c(run, "self-test", k.state(held), k.groups({p}, uncertain={c}), snap)
    assert not run.problems, run.problems


def test_o1_a_link_no_imported_backup_showed_is_not_demanded(tmp_path):
    """13621's shape (pending owner): a lineage no backup ever held is not in the demanded group."""
    w = k.world(tmp_path)
    p = w.session()
    rp, _ = w.row(p, "user", W)
    c = w.session(parent=None, cfg=None)
    w.sim.links[c] = ("vanished-session", "branch")
    w.row(c, "user", "Please archive the partner agreement before lunch.")
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap)
    run = k.run_of(w, snap)
    k.withhold(run, rows, p, rp)
    assert o.truth_group(run, snap, k.state(rows)) == {p}


def test_o11_a_group_conversations_title_is_hidden(tmp_path):
    run, rows, snap, p, c = _setup(tmp_path, "branch", w_in_child=True)
    held = [dict(r, withheld=True, content="", session_title="Landing page" if r["sid"] == p else "")
            for r in rows]
    o.check_c(run, "self-test", k.state(held), k.groups({p, c}), snap)
    assert any(x.startswith("O11") and p in x for x in run.problems), run.problems
