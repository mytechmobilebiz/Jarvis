"""One seed of the oracle harness: Oracle A on the test copy and live, then Oracle B on live (plan 5.1)."""

from __future__ import annotations

import os
import random
from typing import Any

from . import cc_adapter as cc
from . import hermes_sim as hs
from . import oracle as o
from .oracle import Run, Withhold, norm, state, versions_for

RESTORE_EVERY = int(os.environ.get("ORACLE_RESTORE_EVERY", "25"))  # a docker restore round trip every N seeds


async def _step(run: Run, label: str, coro: Any) -> Any:
    """A Command Center entry point; a missing one fails the seed (BINDING), or is noted under ORACLE_LENIENT=1."""
    try:
        return await coro
    except cc.Binding as exc:
        if cc.LENIENT:
            run.notes.append("BINDING (lenient, skipped): %s: %s" % (label, exc))
            return None
        raise


def _sync_step(run: Run, label: str, fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except cc.Binding as exc:
        if cc.LENIENT:
            run.notes.append("BINDING (lenient, skipped): %s: %s" % (label, exc))
            return None
        raise


async def withhold_some(run: Run, snap: hs.Snapshot) -> None:
    st = await state()
    cands = []
    for r in st["rows"]:
        text = r.get("content") or ""
        if r.get("withheld") or len(norm(text)) < 16 or text.startswith("[CONTEXT") or text == hs.STUB:
            continue
        vs = versions_for(run.sim, r)
        if len(vs) != 1 or not vs[0].tag.carries:
            continue
        in_d = snap.rows.get((r["sid"], int(r["source_message_id"]))) is not None
        cands.append((r, vs[0], in_d))
    if not cands:
        run.notes.append("no row to withhold")
        return
    in_backup = [c for c in cands if c[2]]
    pool = in_backup if (in_backup and run.rng.random() < 0.9) else cands
    # pressure: most of the time withhold a message that already has copies (another row of the same logical
    # message, or a row carrying its words) in the decision backup
    copied = [c for c in pool if sum(1 for v in snap.rows.values() if c[1].tag.origin == v.tag.origin
                                     or c[1].tag.origin in v.tag.carries) > 1]
    if copied and run.rng.random() < 0.7:
        pool = copied
    for _ in range(1 if run.rng.random() < 0.75 else 2):
        r, v, _ = run.rng.choice(pool)
        if any(w.cc_id == r["id"] for w in run.withholds):
            continue
        resp = await run.api.withhold(r["id"])
        if resp.status_code != 200:
            run.notes.append("withhold of %s refused: %s %s" % (r["id"], resp.status_code, resp.text[:160]))
            continue
        run.withholds.append(Withhold(r["id"], r["source_key"], r["sid"], int(r["source_message_id"]),
                                      r.get("content") or "", set(v.tag.carries), v.tag.origin))
        run.sim.log.append("WITHHOLD cc %s = %s:%s %r" % (r["id"], r["sid"], r["source_message_id"],
                                                          (r.get("content") or "")[:50]))
        run.sim.hot |= set(v.tag.carries)


async def run_seed(seed: int, tmp: Any, client: Any, restore: Any = None) -> Run:
    sim = hs.new_world(str(tmp / "jarvis"), seed)
    profile, content = str(tmp / "profile"), str(tmp / "content")
    os.makedirs(profile, exist_ok=True)
    os.makedirs(content, exist_ok=True)
    rng = random.Random(seed * 31 + 5)
    run = Run(seed, sim, profile, content, cc.Api(client), rng)
    steps = rng.randint(16, 40)
    a_steps = rng.randint(steps // 3, (2 * steps) // 3)

    # --- Oracle A: the test copy ----------------------------------------------------------------------------------
    tc_seq: list[hs.Snapshot] = []
    for i in range(a_steps):
        sim.random_op()
        if rng.random() < 0.15:
            snap = sim.backup("tc%d" % i)
            await run.import_(snap)
            tc_seq.append(snap)
    decision = sim.backup("decision")
    await run.import_(decision)
    tc_seq.append(decision)
    await withhold_some(run, decision)
    if not run.withholds:
        return run
    await _step(run, "settle", _async(cc.settle, decision.path, profile, content))
    st = await state()
    holds = await run.api.holds()
    listed = {h.get("message") for h in holds}
    fam_a = o.family(run, decision, st["rows"], st["stones"])
    o.check_certain_derive(run, "A2", st, listed, fam_a, "test copy after Settle")
    o.check_evidence(run, "A3", holds, st, decision, "test copy after Settle")
    await o.scripted_review(run, holds, st, "test copy")
    tc = await state()
    o.check_no_readable_leak(run, "A1", tc, fam_a, decision, "test copy after the review")
    o.check_certain_derive(run, "A2", tc, {h.get("message") for h in await run.api.holds()}, fam_a,
                           "test copy after the review")
    out = str(tmp / "decisions.json")
    try:
        await cc.export_decisions(out)
        exported = True
    except Exception as exc:  # the export refuses while anything is unsettled (4.7): after the review it must not
        run.bad("A5", "decisions export refused after Settle and a complete review: %s" % exc)
        exported = False

    # --- Oracle A: live ---------------------------------------------------------------------------------------------
    live_seq: list[hs.Snapshot] = [decision]
    await cc.wipe()
    if exported:
        await cc.watch_start()
        try:
            await _step(run, "import with decisions", run.import_(decision, decisions=out))
        finally:
            records = await cc.watch_stop()
        o.check_watch(run, "A5", records, "live import with the decisions file (C1, any moment)")
    else:
        await run.import_(decision)
    live = await state()
    o.compare_states(run, "A5", "test copy vs live", tc, live, keys=set(o.by_key(live)))
    missing = [k for k, r in o.by_key(tc).items()
               if (r["sid"], int(r["source_message_id"])) in decision.rows and k not in o.by_key(live)]
    if missing:
        run.bad("A5", "rows of the decision backup the test copy stored but live did not: %s" % missing[:5])
    o.check_no_readable_leak(run, "A1", live, o.family(run, decision, live["rows"], live["stones"]), decision,
                             "live after the decisions import")

    # --- Oracle B: later backups on live -------------------------------------------------------------------------
    sim.focus |= o.family(run, decision, live["rows"], live["stones"])
    point = None
    if restore is not None and seed % RESTORE_EVERY == 0:
        point = restore.take("seed%d" % seed)
    for i in range(steps - a_steps):
        sim.random_op()
        if rng.random() < 0.12:
            snap = sim.backup("x%d" % i)
            pre = await state()
            await run.import_(snap)
            live_seq.append(snap)
            post = await state()
            fam = o.family(run, snap, post["rows"], post["stones"])
            o.check_stored_unchanged(run, "B1", pre, post, fam, "later backup %s" % snap.n)
            o.check_no_readable_leak(run, "B2", post, fam, snap, "later backup %s" % snap.n)
    later = sim.backup("later")
    pre = await state()
    x_run = await run.import_(later)
    live_seq.append(later)
    post = await state()
    holds = await run.api.holds()
    listed = {h.get("message") for h in holds}
    fam = o.family(run, later, post["rows"], post["stones"])
    o.check_stored_unchanged(run, "B1", pre, post, fam, "later backup X")
    o.check_no_readable_leak(run, "B2", post, fam, later, "later backup X")
    o.check_new_certain(run, "B3", pre, post, listed, fam, "later backup X")
    o.check_safe_copies(run, "B5", pre, post, fam, "later backup X")
    o.check_evidence(run, "C3-evidence", [h for h in holds if h.get("message") in
                                          {r["id"] for r in post["rows"] if r["source_key"] not in o.by_key(pre)}],
                     post, later, "holds of the later backup")
    await b6_release_unrelated(run, post, holds, fam, later)
    st = await state()
    await o.scripted_review(run, await run.api.holds(), st, "later backup")
    s2 = await state()

    # B7: a rollback with a re-import, a re-import, a restore with a re-import change nothing
    if x_run and x_run.get("run_id"):
        try:
            await cc.rollback(x_run["run_id"])
        except Exception as exc:  # the contract does not promise every rollback; it promises what follows one
            run.notes.append("rollback of the X run refused: %s" % str(exc)[:200])
        else:
            await run.import_(later)
            o.compare_states(run, "B7", "rollback of X, then a re-import", s2, await state())
    await run.import_(later)
    o.compare_states(run, "B7", "re-import of X", s2, await state())
    if point is not None:
        removals = str(tmp / "removals.json")
        await cc.export_decisions(removals, removals_only=True)
        await restore.restore(point)
        await cc.apply_removals_only(removals)
        await run.import_(later)
        o.compare_states(run, "B7", "restore of a backup taken before X, removals, re-import", s2, await state())

    # --- A4 and B4: the same imports with no decision ------------------------------------------------------------
    await cc.wipe()
    for snap in tc_seq:
        await cc.import_backup(snap.path, profile, content)
    o.compare_outside(run, "A4", tc, await state(), fam_a, "test copy vs a no-withhold import")
    await cc.wipe()
    for snap in live_seq:
        await cc.import_backup(snap.path, profile, content)
    o.compare_outside(run, "B4", s2, await state(), fam, "live vs a plain import of the same backups")
    return run


async def b6_release_unrelated(run: Run, post: dict[str, Any], holds: list[dict[str, Any]], fam: set[str],
                               later: hs.Snapshot) -> None:
    rows = {r["id"]: r for r in post["rows"]}
    sessions = {}
    for h in holds:
        r = rows.get(h.get("message"))
        if r is not None and r["sid"] in fam:
            sessions[r["session_id"]] = r["sid"]
    for cc_sid, sid in sorted(sessions.items()):
        before = await state()
        resp = await run.api.release_unrelated(cc_sid)
        if resp is None:
            run.notes.append("B6 not exercised: the API has no 'release rows with no relation' route")
            return
        after = await state()
        b = o.by_key(before)
        freed = [r for r in after["rows"] if not r.get("withheld") and b.get(r["source_key"], {}).get("withheld")]
        for r in freed:
            if run.derives(versions_for(run.sim, r)):
                run.bad("B6", "'release rows with no relation' in %s released row %s (%s:%s) derived from a withheld "
                              "message" % (sid, r["id"], r["sid"], r.get("source_message_id")))
        lacking = [w for w in run.withholds if not o.w_text_present(run, w, later)]
        if resp.status_code < 400 and freed and lacking:
            run.bad("B6", "'release rows with no relation' in %s was offered although the backup lacks a withheld "
                          "text (%d of %d)" % (sid, len(lacking), len(run.withholds)))


async def _async(fn: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args)


# --- the round-3 bug-finding seeds, replayed exactly (rv3_replay.py) ------------------------------------------------


async def run_replay(seed: int, tmp: Any, client: Any) -> Run:
    """A round-3 seed's exact Hermes history and admin actions, on one installation: after each withhold the admin
    runs Settle on the newest backup; a 'release' of the round-3 run becomes the scripted review (release exactly the
    holds the ground truth calls independent); every import is checked like a later backup (B1, B2, B3, B5) once a
    decision exists, and every Settle like a decision backup (A2, A3)."""
    from . import rv3_replay as rr

    data = rr.load()[str(seed)]
    events: list[tuple[rr.Event, hs.Snapshot | None]] = []

    def on_event(ev: rr.Event, w: rr.Old) -> None:
        events.append((ev, w.sim.backup("imp") if ev.kind == "import" else None))
    old = rr.generate(seed, str(tmp / "jarvis"), data["words"], data["env"], data["log"], on_event)
    sim = old.sim
    profile, content = str(tmp / "profile"), str(tmp / "content")
    os.makedirs(profile, exist_ok=True)
    os.makedirs(content, exist_ok=True)
    run = Run(seed, sim, profile, content, cc.Api(client), random.Random(seed))
    last: hs.Snapshot | None = None
    for ev, snap in events:
        if ev.kind == "import":
            pre = await state()
            await run.import_(snap)
            last = snap
            post = await state()
            if run.withholds:
                holds = await run.api.holds()
                listed = {h.get("message") for h in holds}
                fam = o.family(run, snap, post["rows"], post["stones"])
                where = "rv3 replay, after %s" % ev.line
                o.check_stored_unchanged(run, "RV3-B1", pre, post, fam, where)
                o.check_no_readable_leak(run, "RV3-B2", post, fam, snap, where)
                o.check_new_certain(run, "RV3-B3", pre, post, listed, fam, where)
                o.check_safe_copies(run, "RV3-B5", pre, post, fam, where)
        elif ev.kind == "withhold":
            rows = [r for r in (await state())["rows"] if r["sid"] == ev.sid
                    and str(r.get("source_message_id")) == str(ev.row)]
            if not rows or rows[-1].get("withheld"):
                run.notes.append("%s: the row is not stored readable here (%s)" % (ev.line, len(rows)))
                continue
            r = rows[-1]
            resp = await run.api.withhold(r["id"])
            if resp.status_code != 200:
                run.notes.append("%s: refused %s %s" % (ev.line, resp.status_code, resp.text[:120]))
                continue
            vs = versions_for(sim, r)
            run.withholds.append(Withhold(r["id"], r["source_key"], r["sid"], ev.row, r.get("content") or "",
                                          set(vs[-1].tag.carries) if vs else set(),
                                          vs[-1].tag.origin if vs else -1))
            if last is not None:
                await _step(run, "settle", _async(cc.settle, last.path, profile, content))
                st = await state()
                holds = await run.api.holds()
                fam = o.family(run, last, st["rows"], st["stones"])
                o.check_certain_derive(run, "RV3-A2", st, {h.get("message") for h in holds}, fam,
                                       "rv3 replay, Settle after %s" % ev.line)
                o.check_evidence(run, "RV3-A3", holds, st, last, "rv3 replay, Settle after %s" % ev.line)
        elif ev.kind == "release":
            st = await state()
            await o.scripted_review(run, await run.api.holds(), st, "rv3 replay, %s" % ev.line)
        elif ev.kind == "rollback":
            from sqlalchemy import text as sql

            from app.db import session_scope
            async with session_scope() as s:
                run_id = await s.scalar(sql("SELECT id FROM import_runs WHERE status <> 'rolled_back' "
                                            "ORDER BY id DESC LIMIT 1"))
            if run_id is not None:
                try:
                    await cc.rollback(run_id)
                except Exception as exc:
                    run.notes.append("%s: rollback refused: %s" % (ev.line, str(exc)[:120]))
    if run.withholds and last is not None:
        st = await state()
        fam = o.family(run, last, st["rows"], st["stones"])
        o.check_no_readable_leak(run, "RV3-B2", st, fam, last, "rv3 replay, end")
        o.check_certain_derive(run, "RV3-A2", st, {h.get("message") for h in await run.api.holds()}, fam,
                               "rv3 replay, end")
    return run
