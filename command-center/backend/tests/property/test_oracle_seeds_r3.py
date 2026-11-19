"""The option C oracle's 'rekeyed' (round 3, step 3, 13560): a stored row whose key moved to the uid key the backup
gives that very JARVIS row is the same row; anything else that loses its key has disappeared (B1). A mutant in
docs/rebuild-062/progress/R5-mutation/r5mut.py loosens the rule; these tests kill it. No database."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k


def _pre_post(tmp, *, uid_key: str | None, same_id: bool = True):
    w = k.world(tmp)
    a = w.session()
    r, _ = w.row(a, "user", "Please rewrite the landing page copy for the team meeting.", uid="u" * 32)
    snap = w.sim.backup("d")
    pre = k.cc_rows(snap)
    post = [dict(pre[0], source_key=uid_key or pre[0]["source_key"] + "x", id=pre[0]["id"] if same_id else 99)]
    return k.run_of(w, snap), k.state(pre), k.state(post), snap, a


def test_rekeyed_the_same_archive_row_under_the_uid_the_backup_gives_it_is_the_same_row(tmp_path):
    run, pre, post, snap, a = _pre_post(tmp_path, uid_key="jarvis:message:uid:" + "u" * 32)
    o.check_stored_unchanged(run, "B1", pre, post, {a}, "self-test", snap)
    assert not run.problems, run.problems


def test_rekeyed_another_uid_another_row_or_no_backup_is_still_disappeared(tmp_path):
    for kw in (dict(uid_key="jarvis:message:uid:" + "v" * 32), dict(uid_key="jarvis:message:uid:" + "u" * 32,
                                                                   same_id=False), dict(uid_key=None)):
        run, pre, post, snap, a = _pre_post(tmp_path / str(len(kw)) / (kw.get("uid_key") or "n")[-1], **kw)
        o.check_stored_unchanged(run, "B1", pre, post, {a}, "self-test", snap)
        assert any("disappeared" in p for p in run.problems), kw
    run, pre, post, snap, a = _pre_post(tmp_path / "nosnap", uid_key="jarvis:message:uid:" + "u" * 32)
    o.check_stored_unchanged(run, "B1", pre, post, {a}, "self-test")
    assert any("disappeared" in p for p in run.problems)
