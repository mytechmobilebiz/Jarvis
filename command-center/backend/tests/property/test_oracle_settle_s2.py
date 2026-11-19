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
