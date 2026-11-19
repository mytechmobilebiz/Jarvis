"""Rebuild S3 (RECON-58, D-45): i22's B1 and B5 promise that a withheld family's stored rows never change and that its
new readable rows are safe copies. Option C's pending per-row carrier holds (RECON-27) are stones i22 never made: they
put a carrier's conversation into the oracle's computed family, so B1/B5 demanded i22's family promise of a
conversation option C holds per row only (fresh seeds 13058, 13100, 13622, 13160). These self-tests show the narrowing
(carrier_only) excuses exactly that and that every real violation is still reported. No database; synthetic data."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k

W_TEXT = "Please keep the vendor pricing sheet for the board between us for now."


def _world(tmp, *, link: bool = False, kept: bool = False):
    """A: W's conversation (W withheld). C: a conversation that quotes W (a pending carrier hold, or a kept one), with a
    later turn. C is a branch of A when `link`."""
    w = k.world(tmp)
    a = w.session()
    w.row(a, "user", "Please draft the agenda for the quarterly planning call.")
    w_row, w_lm = w.row(a, "user", W_TEXT)
    w.row(a, "assistant", "Noted, the pricing sheet stays as it is.")
    c = w.session(parent=a, cfg={"_branched_from": a}, kind="branch") if link else w.session()
    c1, _ = w.row(c, "user", "Please list the open points for the onboarding checklist.")
    c2, _ = w.row(c, "assistant", "In that conversation you wrote:\n> " + W_TEXT, carries={w_lm})
    snap0 = w.sim.backup("b0")
    c3, _ = w.row(c, "assistant", "Done, the onboarding checklist is ready and the open points are listed.")
    a4, _ = w.row(a, "assistant", "Here is the update on the backup schedule: two items need a decision.")
    snap1 = w.sim.backup("b1")
    pre_rows = k.cc_rows(snap0, withheld={(a, w_row), (c, c2)})
    post_rows = k.cc_rows(snap1, withheld={(a, w_row), (c, c2)})
    run = k.run_of(w, snap1, snap0, snap1)
    wh = k.withhold(run, pre_rows, a, w_row)
    stones = []
    for rows in (pre_rows, post_rows):
        rw = next(r for r in rows if r["sid"] == a and r["source_message_id"] == str(w_row))
        rc = next(r for r in rows if r["sid"] == c and r["source_message_id"] == str(c2))
        stones.append([k.stone(rw, review="", via="key"),
                       k.stone(rc, review="kept" if kept else "pending", via=o.CARRIER_VIA, anchor=wh.key,
                               relations=["contains"])])
    pre, post = k.state(pre_rows, stones[0]), k.state(post_rows, stones[1])
    return run, pre, post, snap1, a, c, {"c1": c1, "c2": c2, "c3": c3, "a4": a4}


def _fam(run, snap, st):
    fam = o.family(run, snap, st["rows"], st["stones"])
    return fam, o.carrier_only_sids(run, snap, st["rows"], st["stones"], fam)


def test_s3_a_conversation_only_a_pending_carrier_hold_binds_is_carrier_only(tmp_path):
    run, pre, post, snap, a, c, _ = _world(tmp_path)
    fam, c_only = _fam(run, snap, post)
    assert {a, c} <= fam  # the oracle's family reaches C through the carrier stone (the artifact)
    assert c_only == {c}
    # a group conversation (the ground truth's or recorded) is never carrier-only
    assert o.carrier_only_sids(run, snap, post["rows"], post["stones"], fam, {c}) == set()


def test_s3_b5_a_new_turn_of_a_carrier_only_conversation_follows_a_plain_import(tmp_path):
    run, pre, post, snap, a, c, ids = _world(tmp_path)
    # the new readable row of A is a real B5 violation here; take it out to see C alone
    post = k.state([r for r in post["rows"] if not (r["sid"] == a and r["source_message_id"] == str(ids["a4"]))],
                   post["stones"])
    fam, c_only = _fam(run, snap, post)
    o.check_safe_copies(run, "B5", pre, post, fam, "self-test")
    assert any(str(ids["c3"]) in p and c in p for p in run.problems), "before S3 the oracle reported C's new turn"
    run.problems.clear()
    o.check_safe_copies(run, "B5", pre, post, fam, "self-test", c_only)
    assert not run.problems, run.problems


def test_s3_b5_still_reports_a_withheld_conversations_new_row_a_linked_or_kept_carrier_and_a_leak(tmp_path):
    # (1) a new readable row of W's own conversation, with C carrier-only
    run, pre, post, snap, a, c, ids = _world(tmp_path / "own")
    fam, c_only = _fam(run, snap, post)
    o.check_safe_copies(run, "B5", pre, post, fam, "self-test", c_only)
    assert any(a in p and "readable but equals no readable stored row" in p for p in run.problems), run.problems
    # (2) C is a branch of A: i22's family reaches it by a copy link, it is not carrier-only
    run, pre, post, snap, a, c, ids = _world(tmp_path / "link", link=True)
    fam, c_only = _fam(run, snap, post)
    assert c not in c_only
    o.check_safe_copies(run, "B5", pre, post, fam, "self-test", c_only)
    assert any(c in p for p in run.problems), run.problems
    # (3) a kept carrier hold is a certain stone (it seeds a group, RECON-27): not carrier-only
    run, pre, post, snap, a, c, ids = _world(tmp_path / "kept", kept=True)
    fam, c_only = _fam(run, snap, post)
    assert c not in c_only
    o.check_safe_copies(run, "B5", pre, post, fam, "self-test", c_only)
    assert any(c in p for p in run.problems), run.problems
    # (4) a new readable row of a carrier-only conversation that carries W's words (ground truth) is reported
    run, pre, post, snap, a, c, ids = _world(tmp_path / "leak")
    c3 = next(r for r in post["rows"] if r["sid"] == c and r["source_message_id"] == str(ids["c3"]))
    v = run.sim.versions_of(c, ids["c3"])[-1]
    run.sim.tags[(c, ids["c3"])] = v.tag.__class__(v.tag.origin, frozenset(v.tag.carries | run.wlms))
    run.sim.history[(c, ids["c3"])] = [v.__class__(**{**v.__dict__, "tag": run.sim.tags[(c, ids["c3"])]})]
    c3["content"] = W_TEXT
    fam, c_only = _fam(run, snap, post)
    assert c in c_only
    o.check_safe_copies(run, "B5", pre, post, fam, "self-test", c_only)
    assert any(c in p and "carries withheld words" in p for p in run.problems), run.problems


def test_s3_b1_readable_rows_of_a_carrier_only_conversation_follow_a_plain_import(tmp_path):
    run, pre, post, snap, a, c, ids = _world(tmp_path)
    # C's first row is rewound by Hermes (gone) and nothing else changes in A
    post = k.state([r for r in post["rows"] if not (r["sid"] == c and r["source_message_id"] == str(ids["c1"]))],
                   post["stones"])
    fam, c_only = _fam(run, snap, post)
    o.check_stored_unchanged(run, "B1", pre, post, fam, "self-test", snap)
    assert any("disappeared" in p and c in p for p in run.problems), "before S3 the oracle reported C's row"
    run.problems.clear()
    o.check_stored_unchanged(run, "B1", pre, post, fam, "self-test", snap, carrier_only=c_only)
    assert not run.problems, run.problems


def test_s3_b1_still_reports_a_family_row_gone_a_held_carrier_readable_and_a_carrier_conversation_leak(tmp_path):
    # (1) a readable stored row of W's own conversation disappears
    run, pre, post, snap, a, c, ids = _world(tmp_path / "own")
    a1 = next(r for r in pre["rows"] if r["sid"] == a)
    post = k.state([r for r in post["rows"] if r["source_key"] != a1["source_key"]], post["stones"])
    fam, c_only = _fam(run, snap, post)
    o.check_stored_unchanged(run, "B1", pre, post, fam, "self-test", snap, carrier_only=c_only)
    assert any("disappeared" in p and a in p for p in run.problems), run.problems
    # (2) the held carrier row of a carrier-only conversation becomes readable, or disappears
    for gone in (False, True):
        run, pre, post, snap, a, c, ids = _world(tmp_path / ("gone" if gone else "readable"))
        key = next(r["source_key"] for r in post["rows"] if r["sid"] == c and r["source_message_id"] == str(ids["c2"]))
        rows = [r for r in post["rows"] if r["source_key"] != key] if gone else [
            dict(r, withheld=False, content="In that conversation you wrote:\n> " + W_TEXT) if r["source_key"] == key
            else r for r in post["rows"]]
        post = k.state(rows, post["stones"])
        fam, c_only = _fam(run, snap, post)
        assert c in c_only
        o.check_stored_unchanged(run, "B1", pre, post, fam, "self-test", snap, carrier_only=c_only)
        assert any(("disappeared" if gone else "became readable") in p and c in p for p in run.problems), \
            run.problems
    # (3) a readable row of a carrier-only conversation changes to a text that carries W's words (ground truth)
    run, pre, post, snap, a, c, ids = _world(tmp_path / "leak")
    rb = next(r for r in post["rows"] if r["sid"] == c and r["source_message_id"] == str(ids["c1"]))
    rb["content"] = W_TEXT
    v = run.sim.versions_of(c, ids["c1"])[-1]
    tag = v.tag.__class__(v.tag.origin, frozenset(v.tag.carries | run.wlms))
    run.sim.history[(c, ids["c1"])] = [v, v.__class__(**{**v.__dict__, "content": W_TEXT, "tag": tag})]
    fam, c_only = _fam(run, snap, post)
    o.check_stored_unchanged(run, "B1", pre, post, fam, "self-test", snap, carrier_only=c_only)
    assert any("carrying withheld words" in p and c in p for p in run.problems), run.problems
