"""The scenario's Settle as the admin runs it (stabilisation S2, RECON-56, harness gap 13165): Settle runs on the
decision backup; when the Command Center answers that a decision waits on another backup (RECON-12: the newest backup
that wrote its group's conversations), the scenario settles the backup it names, as the answer says. Any other refusal,
or a name that resolves to no backup this test copy imported, still fails the seed (BINDING). No database."""

from __future__ import annotations

import pytest

from . import cc_adapter as cc
from . import scenario as sc
from . import selftest_kit as k

REFUSAL = ("settle (plan 4.1: brain-import settle --backup <file>) failed or does not exist (exit 2): refused: no "
           "decision waits on this JARVIS backup; 1 decision(s) wait on another one (%s: 1): finish them with the "
           "newest backup that wrote their conversations")


def _run(tmp):
    w = k.world(tmp)
    a = w.session()
    w.row(a, "user", "Please rewrite the landing page copy for the team meeting.")
    older = w.sim.backup("tc1")
    w.row(a, "assistant", "Here is the update on the pricing table: two items need a decision.")
    decision = w.sim.backup("decision")
    run = k.run_of(w, decision, older, decision)
    return run, older, decision


def test_waiting_backups_resolves_the_refusal_and_the_finished_run_s_lines(tmp_path):
    run, older, decision = _run(tmp_path)
    assert sc.waiting_backups(run, REFUSAL % older.sha256[:12], decision) == [older]
    answer = "run 5 finished 1 decision(s): ...\ndecisions waiting on another JARVIS backup (%s): 1\n" % older.sha256[:12]
    assert sc.waiting_backups(run, answer, decision) == [older]


def test_waiting_backups_names_nothing_for_another_refusal_an_unknown_backup_or_the_one_just_settled(tmp_path):
    run, older, decision = _run(tmp_path)
    assert sc.waiting_backups(run, "refused: the backup's schema is too new", decision) == []
    assert sc.waiting_backups(run, REFUSAL % ("f" * 12), decision) == []  # no imported backup has that SHA-256
    assert sc.waiting_backups(run, REFUSAL % decision.sha256[:12], decision) == []  # never the one refused
    assert sc.waiting_backups(run, "run 5 finished 1 decision(s): certain_rows 2", decision) == []


async def test_settle_decisions_settles_the_named_backup_and_still_fails_on_any_other_refusal(tmp_path, monkeypatch):
    run, older, decision = _run(tmp_path)
    calls: list[str] = []

    def refuse_decision(backup, profile, content):
        calls.append(backup)
        if backup == decision.path:
            raise cc.Binding(REFUSAL % older.sha256[:12])
        return "run 7 finished 1 decision(s)"
    monkeypatch.setattr(cc, "settle", refuse_decision)
    monkeypatch.setattr(cc, "LENIENT", False)
    assert await sc.settle_decisions(run, decision) is older
    assert calls == [decision.path, older.path]

    def refuse_otherwise(backup, profile, content):
        raise cc.Binding("settle failed: refused: the backup's schema is too new")
    monkeypatch.setattr(cc, "settle", refuse_otherwise)
    with pytest.raises(cc.Binding):  # a real refusal is still a failing seed
        await sc.settle_decisions(run, decision)

    def refuse_unknown(backup, profile, content):
        raise cc.Binding(REFUSAL % ("f" * 12))
    monkeypatch.setattr(cc, "settle", refuse_unknown)
    with pytest.raises(cc.Binding):  # a waiting decision on a backup this test copy never imported
        await sc.settle_decisions(run, decision)


async def test_settle_decisions_also_settles_a_decision_left_waiting_by_a_finished_run(tmp_path, monkeypatch):
    run, older, decision = _run(tmp_path)
    calls: list[str] = []

    def finish(backup, profile, content):
        calls.append(backup)
        if backup == decision.path:
            return "run 7 finished 1 decision(s)\ndecisions waiting on another JARVIS backup (%s): 1\n" % (
                older.sha256[:12])
        return "run 8 finished 1 decision(s)"
    monkeypatch.setattr(cc, "settle", finish)
    assert await sc.settle_decisions(run, decision) is decision
    assert calls == [decision.path, older.path]


# --- C1 (A5) for a released row the decision backup no longer holds (RECON-57) ---------------------------------------

TEXT = "Search results for the pricing table: the first entries match the request closely."


def _c1(tmp, *, live_text: str = TEXT, released: bool = True, stone: bool = False, in_decision: bool = False,
        recorded: bool = True):
    import dataclasses

    from . import oracle as o
    w = k.world(tmp)
    a = w.session()
    w.row(a, "tool", TEXT)
    older = w.sim.backup("tc1")
    decision = older if in_decision else dataclasses.replace(older, rows={}, sha256="d" * 64)
    run = k.run_of(w, decision, older, decision)
    live = k.cc_rows(older, content={next(iter(older.rows)): live_text})
    tc = [dict(live[0], content="", withheld=True,
               flags={"withheld_by_admin": dict({"via": "carries the withheld text", "review": "pending"},
                                                **({"released_at": "2026-11-19T11:24:17+00:00"} if released
                                                   else {}))})]
    stones = [k.stone(tc[0], via="carries the withheld text")] if stone else []
    if recorded:
        run.released[tc[0]["source_key"]] = [TEXT]
    o.compare_c1(run, "A5", k.state(tc, stones), k.state(live), k.state(live), k.state(live), decision)
    return [p for p in run.problems if p.startswith("A5")]


def test_c1_a_released_row_waiting_for_a_text_the_decision_backup_lacks_equals_live_showing_the_released_text(
        tmp_path):
    assert _c1(tmp_path) == []


def test_c1_still_reports_another_text_a_held_or_unreleased_row_a_row_the_decision_backup_holds(tmp_path):
    assert _c1(tmp_path / "a", live_text="Search results for the pricing table: something else entirely here.")
    assert _c1(tmp_path / "b", stone=True)  # still a pending hold on the test copy
    assert _c1(tmp_path / "c", released=False)  # withheld, never released
    assert _c1(tmp_path / "d", in_decision=True)  # the test copy's next import must have brought its text back
    assert _c1(tmp_path / "e", recorded=False)  # no release of this row the review recorded
