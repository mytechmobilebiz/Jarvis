"""0.6.2 option C (rebuild R1): the closure of a withheld message's conversation group (app/brain/groups.py), without a
database. J1-J9, O-D1 (a markerless child joins from the parent down only), 'uncertain' (a parent that does not
resolve, two parents, a weak copied-history link; it spreads and never becomes a member again), seeds (never a pending
hold). Synthetic ids and times only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.brain import groups, identity
from app.brain.groups import MEMBER, UNCERTAIN, WITHHELD_HERE, Graph, Member

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def t(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def graph(nodes: dict[str, tuple[str, str]], edges: list[tuple[str, str, str]] = (), **kw) -> Graph:
    """nodes: sid -> (parent, lineage); edges: (parent, child, how). Every node is known."""
    g = Graph()
    for sid, (parent, lineage) in nodes.items():
        n = g.node(sid)
        g.known.add(sid)
        n.parent, n.lineage, n.started_at = parent, lineage, t(100)
        if parent:
            n.claims.add(parent)
            g.add_edge(parent, sid, groups.LINEAGE_HOW[lineage])
    for parent, child, how in edges:
        g.add_edge(parent, child, how)
    for sid, value in kw.items():
        for attr, v in value.items():
            setattr(g.node(sid), attr, v)
    return g


def states(found: dict[str, Member]) -> dict[str, tuple[str, str]]:
    return {sid: (m.how, m.state) for sid, m in found.items()}


def test_d1_a_markerless_child_joins_from_the_parent_down_only():
    """O-D1: W in the parent P -> its markerless child C joins (held with P's group); W in C -> P does not join (the
    parent side is a pending owner decision, groups-paths B and D)."""
    g = graph({"P": ("", ""), "C": ("P", "")})
    assert states(g.closure(["P"])) == {"P": (WITHHELD_HERE, MEMBER), "C": ("child", MEMBER)}
    assert states(g.closure(["C"])) == {"C": (WITHHELD_HERE, MEMBER)}


def test_a_delegated_sub_agent_joins_from_the_parent_down_only():
    g = graph({"P": ("", ""), "D": ("P", "delegated")})
    assert set(g.closure(["P"])) == {"P", "D"}
    assert set(g.closure(["D"])) == {"D"}


def test_j1_to_j4_continuations_branches_and_resets_link_both_ways():
    """J1/J2 a continuation, J3 a branch, J4 a gateway reset: linked both ways, to a fixed point down the lineage."""
    g = graph({"P": ("", ""), "C": ("P", "continuation"), "B": ("C", "branch"), "R": ("B", "reset"),
               "X": ("", "")})
    assert states(g.closure(["R"])) == {"R": (WITHHELD_HERE, MEMBER), "B": ("reset", MEMBER),
                                        "C": ("branch", MEMBER), "P": ("continuation", MEMBER)}
    assert set(g.closure(["P"])) == {"P", "C", "B", "R"}


def test_uncertain_a_parent_that_does_not_resolve_makes_the_conversation_uncertain():
    """A conversation reached through a lineage link whose own parent resolves nowhere here is uncertain (its history
    may copy a conversation nobody can compare)."""
    g = graph({"P": ("", ""), "C": ("P", "continuation"), "D": ("C", "continuation")})
    g.node("C").parent = "GONE"  # Hermes re-linked C to a conversation never imported (the link to P stays recorded)
    g.node("C").claims.add("GONE")
    assert g.closure(["D"])["C"].state == UNCERTAIN
    g2 = graph({"P": ("", ""), "C": ("", "")}, [("P", "C", "child")])  # only a recorded link names P
    g2.node("C").parent = "GONE"
    assert g2.closure(["P"])["C"].state == UNCERTAIN


def test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins():
    """Two parents (a marker naming another conversation than the parent): the conversation joins the group of the
    parent it names in the group, as uncertain."""
    g = graph({"P": ("", ""), "Q": ("P", "delegated")}, Q={"conflict": True})
    assert states(g.closure(["P"]))["Q"] == ("delegated", UNCERTAIN)
    g2 = graph({"P": ("", ""), "Q": ("P", "")}, Q={"claims": {"P", "Y"}})  # the sources name two parents
    assert g2.closure(["P"])["Q"].state == UNCERTAIN


def test_uncertain_spreads_to_everything_it_reaches():
    """gd1:uncertain_no_spread: what is reached only through an uncertain conversation is uncertain too."""
    g = graph({"P": ("", ""), "Q": ("P", "branch"), "Y": ("Q", "continuation")}, Q={"conflict": True})
    found = g.closure(["P"])
    assert found["Q"].state == UNCERTAIN and found["Y"].state == UNCERTAIN


def test_a_certain_path_makes_a_member_and_a_doubtful_parentage_stays_uncertain():
    """Reached by a certain path too, a conversation is a member; one whose own parentage is in doubt (two parents) is
    uncertain whatever reaches it; a seed stays withheld here."""
    g = graph({"P": ("", ""), "Q": ("P", "branch"), "Y": ("Q", "continuation")},
              [("P", "Y", "branch"), ("P", "Q", "branch")], Q={"conflict": True})
    found = g.closure(["P"])
    assert found["Y"].state == MEMBER and found["Q"].state == UNCERTAIN and found["P"] == Member(WITHHELD_HERE)


def test_two_copies_of_one_origin_are_weak_evidence_for_each_other():
    """Two unanchored conversations that copied the same row: each joins the origin's group by its own evidence, never
    as a member through the other's copy."""
    g = graph({"P": ("", ""), "B": ("GONE", "branch"), "U": ("", "")})
    _copied(g, "B", "P", 1)
    g.add_row("U", ("user", "", t(0), "", (), True))
    g.node("U").started_at = t(50)
    found = g.closure(["P"])
    assert states(found)["B"] == ("provenance", MEMBER) and states(found)["U"] == ("copied_rows", UNCERTAIN)


def _copied(g: Graph, sid: str, other: str, n: int, *, start: int = 50) -> None:
    """n rows of `sid` at or before its start at the role and time of `other`'s rows."""
    for i in range(n):
        g.add_row(other, ("user", "", t(i), "", (), False))
        g.add_row(sid, ("user", "", t(i), "", (), True))
    g.node(sid).started_at = t(start)


def test_j8_an_unanchored_conversation_with_two_copied_rows_joins_one_row_is_uncertain():
    g = graph({"P": ("", ""), "U": ("", "")})
    _copied(g, "U", "P", 2)
    assert states(g.closure(["P"]))["U"] == ("copied_rows", MEMBER)
    g1 = graph({"P": ("", ""), "U": ("", "")})
    _copied(g1, "U", "P", 1)
    assert states(g1.closure(["P"]))["U"] == ("copied_rows", UNCERTAIN)
    assert states(g1.closure(["U"]))["P"] == ("copied_rows", UNCERTAIN)  # both ways: P holds what U copied


def test_j8_never_for_an_anchored_conversation():
    g = graph({"P": ("", ""), "A": ("", ""), "U": ("A", "")})
    _copied(g, "U", "P", 3)
    assert "U" not in g.closure(["P"])


def test_an_orphaned_branch_with_one_copied_row_joins_its_group_by_provenance():
    """m18:j8_always_two (J9): an orphaned branch (its parent resolves nowhere here) joins with one copied row."""
    g = graph({"P": ("", ""), "B": ("GONE", "branch")})
    _copied(g, "B", "P", 1)
    assert states(g.closure(["P"]))["B"] == ("provenance", MEMBER)


def test_a_reset_with_copied_looking_rows_never_joins_by_copied_history():
    """m18:j8_reset_copies: a gateway reset copies no rows, so copied-looking rows never make it a member: at most
    uncertain (held either way)."""
    g = graph({"P": ("", ""), "R": ("GONE", "reset")})
    _copied(g, "R", "P", 3)
    assert states(g.closure(["P"]))["R"] == ("copied_rows", UNCERTAIN)


def test_an_orphan_read_as_a_delegate_still_has_its_copied_history_read():
    """Round 3 skeptic groups-paths N1/N2 (fail-closed reading, RECON): an orphan whose only parent comes from a delegate
    marker (an older Hermes's inherited _delegate_from, Hermes v16's '__orphaned__' tag) is not anchored by it: its
    copied history joins it to the conversation it copied (both ways)."""
    g = graph({"P": ("", ""), "D": ("P", "delegated"), "D2": ("P", "delegated"), "C2": ("__orphaned__", "delegated")},
              D2={"orphan": True}, C2={"orphan": True})
    _copied(g, "D2", "D", 2)
    _copied(g, "C2", "D", 2)
    assert "D" in g.closure(["D2"]) and "D2" in g.closure(["D"])
    assert "D" in g.closure(["C2"])
    g2 = graph({"P": ("", ""), "D": ("P", "delegated"), "D2": ("P", "delegated")})  # not an orphan: anchored by P
    _copied(g2, "D2", "D", 2)
    assert "D" not in g2.closure(["D2"])


def test_j9_a_shared_uid_joins_its_conversation():
    g = graph({"P": ("", ""), "S": ("", "")})
    g.add_row("S", ("assistant", "", t(5), "u-1", (), False))
    assert states(g.closure(["P"], ["u-1"]))["S"] == ("shared_uid", MEMBER)
    assert "S" not in g.closure(["P"], ["u-2"])


def test_a_pending_hold_never_seeds_a_group():
    """m18:seed_pending_too: the seeds are the conversations of a withhold's certain stones (its own, its members, a
    kept hold), never a pending hold's."""
    ids = identity.Identities([
        identity.Stone(key="w", session="S0", row="1", role="user"),
        identity.Stone(key="m", anchor="w", session="S1", row="2", role="user"),
        identity.Stone(key="h", anchor="w", session="S2", row="3", role="user", review=identity.PENDING),
        identity.Stone(key="k", anchor="w", session="S3", row="4", role="user", review=identity.KEPT)])
    assert groups.seeds(ids) == {"w": {"S0", "S1", "S3"}}


def test_a_recorded_group_forgets_nothing_and_an_uncertain_member_stays_uncertain():
    stored = {"A": Member(WITHHELD_HERE), "B": Member("branch", UNCERTAIN, "A"), "C": Member("child", MEMBER, "A")}
    found = {"A": Member(WITHHELD_HERE), "B": Member("branch", MEMBER, "A"), "D": Member("reset", MEMBER, "A"),
             "C": Member("child", UNCERTAIN, "A")}
    out = groups.merged(found, stored)
    assert out["B"].uncertain and out["C"].uncertain and set(out) == {"A", "B", "C", "D"}
