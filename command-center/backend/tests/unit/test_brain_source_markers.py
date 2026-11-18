"""0.6.2 option C, round 3 fix A (rebuild R1): a fork marker counts only when it names the session's own parent.

Hermes copies a session's model_config onto its compression child (hermes_state_compression
_publish_child_session_row), so a rotation of a sub-agent or of a branch carries the parent's own _delegate_from /
_branched_from naming an older conversation. Hermes reads such a child as a continuation (its fork test
_is_explicit_fork_child_row: "markers only count when they point at parent_session_id"; with no parent any marker
counts). source.Backup.sessions() reads lineage the same way (source._markers); a marker naming a conversation that is
neither the parent nor copied from the parent keeps its kind and marks two parents (conflict: groups.py 'uncertain').
Synthetic data only."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.brain import source

from .. import brain_jarvis as bj

T = 1795000000.0
UTC = ZoneInfo("UTC")


def _sessions(tmp_path, shapes: list[tuple[str, str | None, str, dict]]) -> dict[str, source.SourceSession]:
    fx = bj.mini(tmp_path / "fx")
    for n, (sid, parent, end_reason, config) in enumerate(shapes):
        bj.add_session(fx["db"], sid, started=T + n, parent=parent, end_reason=end_reason, model_config=config)
    b = source.Backup(fx["db"], UTC, {}, {})
    try:
        return {s.id: s for s in b.sessions()}
    finally:
        b.close()


# (id, the sessions, the session read, its lineage, its parent, two parents?)
CASES = [
    # controls: a marker naming the parent, any marker with no parent, the compression marker, a plain child
    ("bound_delegate", [("P", None, "completed", {}), ("D", "P", "completed", {"_delegate_from": "P"})],
     "D", "delegated", "P", False),
    ("bound_branch", [("P", None, "branched", {}), ("B", "P", "completed", {"_branched_from": "P"})],
     "B", "branch", "P", False),
    ("bound_reset", [("P", None, "new_session", {}), ("R", "P", "completed", {"_reset_from": "P"})],
     "R", "reset", "P", False),
    ("null_parent_any_marker", [("P", None, "completed", {}), ("D", None, "completed", {"_delegate_from": "P"})],
     "D", "delegated", "P", False),
    ("hermes_v16_orphan_tag", [("C2", None, "completed", {"_delegate_from": "__orphaned__"})],
     "C2", "delegated", "__orphaned__", False),
    ("compressed_from", [("P", None, "compression", {}), ("C", "P", "completed", {"_compressed_from": "P"})],
     "C", "continuation", "P", False),
    # an inherited fork marker (the parent's own, copied by Hermes onto its rotation): a continuation
    ("inherited_delegate", [("P", None, "completed", {}), ("D", "P", "compression", {"_delegate_from": "P"}),
                            ("D2", "D", "completed", {"_delegate_from": "P"})],
     "D2", "continuation", "D", False),
    ("inherited_branch", [("A", None, "branched", {}), ("B", "A", "compression", {"_branched_from": "A"}),
                          ("C", "B", "completed", {"_branched_from": "A"})],
     "C", "continuation", "B", False),
    ("inherited_reset", [("A", None, "new_session", {}), ("R", "A", "compression", {"_reset_from": "A"}),
                         ("R2", "R", "completed", {"_reset_from": "A"})],
     "R2", "continuation", "R", False),
    # the parent was reopened since (its end_reason says nothing): the copied marker still reads as a continuation
    ("copied_after_reopen", [("P", None, "completed", {}), ("D", "P", "", {"_delegate_from": "P"}),
                             ("D2", "D", "completed", {"_delegate_from": "P"})],
     "D2", "continuation", "D", False),
    # a reset carrying its parent's own _compressed_from is a reset (its own marker names its parent)
    ("reset_with_inherited_compressed", [("X", None, "compression", {}),
                                         ("P", "X", "new_session", {"_compressed_from": "X"}),
                                         ("R", "P", "completed", {"_reset_from": "P", "_compressed_from": "X"})],
     "R", "reset", "P", False),
    # a marker naming another conversation, not the parent's own: it keeps its kind, and the session has two parents
    ("other_conversation_keeps_kind", [("P", None, "completed", {}), ("Y", None, "completed", {}),
                                       ("Q", "P", "completed", {"_delegate_from": "Y"})],
     "Q", "delegated", "P", True),
]


@pytest.mark.parametrize("sessions, sid, lineage, parent, conflict", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_a_fork_marker_counts_only_when_it_names_the_sessions_own_parent(tmp_path, sessions, sid, lineage, parent,
                                                                          conflict):
    ss = _sessions(tmp_path, sessions)[sid]
    assert (ss.lineage, ss.parent_id) == (lineage, parent)
    assert getattr(ss, "conflict", None) is conflict


def test_two_parents_name_the_other_conversation_and_a_copied_marker_is_no_conflict(tmp_path):
    """marker_parents lists the conversations a marker names besides the parent (the second parent of a conflict); a
    marker copied from the parent names the grandparent and is no conflict."""
    found = _sessions(tmp_path, CASES[-1][1] + [("D", "P", "", {"_delegate_from": "Q"}),
                                               ("D2", "D", "completed", {"_delegate_from": "Q"})])
    assert found["Q"].marker_parents == ("Y",) and found["Q"].conflict
    assert found["D2"].lineage == "continuation" and not found["D2"].conflict
