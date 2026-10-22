"""The 0.6.2 contract's oracles (RECOMMENDATION.md sections 3 and 5.1), checked against the simulator's ground truth.

Oracle A, decision backup: withhold, Settle, a scripted review that releases exactly the rows the ground truth calls
independent, export, live replay. A1 no readable row of the family carries a withheld message's words; A2 every
certain member derives from a withheld message; A3 every hold's evidence equals a recomputation; A4 rows outside the
family equal a no-withhold import; A5 the test copy and live agree on every row (C1), and no withheld text is readable
on live at any moment of that import.

Oracle B, later backup: more Hermes operations, a later backup X imported on live. B1 no stored family row gained text
or readability; B2 no readable family row carries a withheld message's words; B3 every new certain row has an exact
identity, a copy identity or an absorbed uid of a certain stone (and derives from a withheld message); B4 rows outside
the family equal a plain import; B5 every new readable family row equals a readable stored row of the family; B6 the
one-click 'release rows with no relation', when offered, never releases a row derived from a withheld message and is
refused when the backup lacks a withheld text of that family; B7 re-importing X, a rollback with a re-import and a
restore with a re-import change nothing.

The family is computed the way plan 5.1 defines it, from what the Command Center could see: the sessions bound by
stones, the sticky closure over every copy-kind link any imported backup showed, X's own links and markers, X's sessions
with a row carrying or absorbing a stone uid, and X's sessions with copied history (role, call and time of a stored row
or stone of the family, at or before the session's start). Ground truth (which logical message a row derives from)
never comes from the row's text."""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import cc_adapter as cc
from . import hermes_sim as hs

# --- evidence (plan 4.6) -----------------------------------------------------------------------------------------

TEXT_RELATIONS = ("identical", "restated", "contains", "part_of")
_DISCORD = re.compile(r"^\[Triggering message id: `[^`]*` — use as `message_id` for reply/react/pin via the discord "
                      r"tools\.\]\n\n")
_REPLY = re.compile(r'^\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)


def norm(text: str | None) -> str:
    return " ".join((text or "").split())


def peel(text: str) -> tuple[str, str | None, bool]:
    """Hermes's wrappers off a row's text: the Discord note, the reply pointer (its quote returned), the replay
    header. Returns (body, quote, restated)."""
    body = _DISCORD.sub("", text or "", count=1)
    quote = None
    m = _REPLY.match(body)
    if m:
        quote, body = m.group(1), body[m.end():]
    restated = False
    if hs.REPLAY_HEADER in body:
        body = body.rsplit(hs.REPLAY_HEADER, 1)[1]
        restated = True
    return body.strip(), quote, restated


def text_relations(h: str, w: str) -> set[str]:
    """The relations of a held row's text h to a withheld text w, computed by comparison after the peel."""
    out: set[str] = set()
    hn, wn = norm(h), norm(w)
    if not wn:
        return out
    body, quote, restated = peel(h)
    wbody = norm(peel(w)[0])
    if hn == wn or norm(body) == wbody:
        out.add("identical")
        if hn != wn and (restated or hs.REPLAY_HEADER in (w or "")):  # one side is the other after the replay header
            out.add("restated")
    if len(wbody) >= 16 and wbody in norm(body or h):  # true of an identical or restated row too
        out.add("contains")
    if quote is not None and len(norm(quote)) >= 16 and norm(quote) in wbody:
        out.add("part_of")
    return out


_CODES = (("not_in_backup", r"not in (this|the) backup|not_in_backup|not in backup"),
          ("identical", r"identical|same text|same words"),
          ("restated", r"restat"),
          ("contains", r"contain"),
          ("part_of", r"part of|part_of|reply quote|quote"),
          ("same_uid", r"message id|message_uid|same uid|same_uid"),
          ("same_call", r"tool call|tool_call|same_call"),
          ("same_time", r"same time|same_time|same second"),
          ("reused_row", r"row number|reused|reused_row"),
          ("stale", r"stale"),
          ("orphan", r"orphan"),
          ("differs", r"differ"),
          ("none", r"^none$|no relation"))


def reported_relations(hold: dict[str, Any]) -> tuple[set[str], list[str]]:
    texts: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, str):
            texts.append(x)
        elif isinstance(x, (list, tuple)):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for y in x.values():
                walk(y)
    for key in ("relation", "relations", "relation_codes", "codes", "evidence", "compare"):
        if key in hold:
            walk(hold[key])
    out: set[str] = set()
    for t in texts:
        tl = t.lower().strip()
        for code, pat in _CODES:
            if re.search(pat, tl):
                out.add(code)
    return out, texts


# --- ground truth for a stored row ---------------------------------------------------------------------------------


def epoch(at: Any) -> float | None:
    if at in (None, ""):
        return None
    if isinstance(at, (int, float)):
        return float(at)
    try:
        return datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def versions_for(sim: hs.Sim, row: dict[str, Any]) -> list[hs.Version]:
    rid = str(row.get("source_message_id") or "")
    if not rid.isdigit():
        return []
    cands = sim.versions_of(row["sid"], int(rid))
    at = epoch(row.get("at"))
    uid = row.get("source_uid") or ""
    out = [v for v in cands if v.role == row.get("role") and (at is None or abs(v.ts - at) < 2e-3)
           and (not uid or not v.uid or v.uid == uid)]
    if len(out) > 1 and norm(row.get("content")):
        exact = [v for v in out if norm(v.content) == norm(row.get("content"))]
        out = exact or out
    return out or cands


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


@dataclass
class Withhold:
    cc_id: int
    key: str
    sid: str
    row: int
    text: str
    lms: set[int]  # the logical messages the withheld row carries (its words)
    origin: int


@dataclass
class Run:
    seed: int
    sim: hs.Sim
    profile: str
    content: str
    api: cc.Api
    rng: random.Random
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    withholds: list[Withhold] = field(default_factory=list)
    imported: list[hs.Snapshot] = field(default_factory=list)  # every backup this installation imported
    runs: list[int] = field(default_factory=list)

    def bad(self, prop: str, msg: str) -> None:
        self.problems.append("%s seed %d: %s" % (prop, self.seed, msg))

    @property
    def wlms(self) -> set[int]:
        out: set[int] = set()
        for w in self.withholds:
            out |= w.lms
        return out

    def derives(self, versions: list[hs.Version]) -> bool:
        return any(self.sim.derives(v.tag, self.wlms) for v in versions)

    def leaks(self, versions: list[hs.Version]) -> bool:
        return any(self.sim.leaks(v.tag, self.wlms) for v in versions)

    async def import_(self, snap: hs.Snapshot, **kw: Any) -> dict[str, Any]:
        out = await cc.import_backup(snap.path, self.profile, self.content, **kw)
        self.imported.append(snap)
        if out.get("run_id"):
            self.runs.append(out["run_id"])
        return out


# --- the family (plan 5.1) -----------------------------------------------------------------------------------------


def family(run: Run, current: hs.Snapshot, stored: list[dict[str, Any]], stones: list[dict[str, Any]]) -> set[str]:
    links: set[tuple[str, str]] = set()
    for snap in run.imported + [current]:
        for child, parent, how in snap.edges:
            if how in hs.COPY_LINKS:
                links.add((child, parent))
    fam = {w.sid for w in run.withholds} | {s.get("source_session_id") for s in stones if s.get("source_session_id")}
    stone_uids = {s.get("message_uid") for s in stones if s.get("message_uid") and s.get("review") != "pending"}
    for w in run.withholds:
        for snap in run.imported:
            v = snap.rows.get((w.sid, w.row))
            if v is not None and v.uid and v.tag.origin == w.origin:
                stone_uids.add(v.uid)
    while True:
        before = set(fam)
        for child, parent in links:
            if child in fam or parent in fam:
                fam |= {child, parent}
        keys = set()
        for r in stored:
            if r["sid"] in fam:
                keys.add((r.get("role"), r.get("tool_call_id") or "", round(epoch(r.get("at")) or 0.0, 3)))
        for s in stones:
            if s.get("source_session_id") in fam and s.get("source_at"):
                keys.add((s.get("role"), s.get("tool_call_id") or "", round(epoch(s.get("source_at")) or 0.0, 3)))
        for sid, sess in current.sessions.items():
            if sid in fam:
                continue
            for (rsid, _), v in current.rows.items():
                if rsid != sid:
                    continue
                if (v.uid and v.uid in stone_uids) or (set(v.absorbed) & stone_uids):
                    fam.add(sid)
                    break
                if v.ts <= (sess.get("started_at") or 0) + 1e-6 and (v.role, v.call or "", round(v.ts, 3)) in keys:
                    fam.add(sid)
                    break
        if fam == before:
            return fam


# --- stored state -------------------------------------------------------------------------------------------------


async def state() -> dict[str, Any]:
    rows = await cc.rows()
    stones = await cc.stones()
    by_key = {s["source_key"]: s for s in stones}
    return {"rows": rows, "stones": stones, "stone_by_key": by_key}


def admin_withheld(row: dict[str, Any], st: dict[str, Any]) -> bool:
    return bool(row.get("withheld")) and ("withheld_by_admin" in (row.get("flags") or {})
                                          or row.get("source_key") in st["stone_by_key"])


def row_state(row: dict[str, Any], st: dict[str, Any]) -> tuple:
    stone = st["stone_by_key"].get(row.get("source_key"))
    flag = (row.get("flags") or {}).get("withheld_by_admin") or {}
    review = stone.get("review") if stone else flag.get("review", "-")
    via = stone.get("via") if stone else flag.get("via", "-")
    return (bool(row.get("withheld")), review, via, content_hash(row.get("content")))


def by_key(st: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["source_key"]: r for r in st["rows"]}


def compare_states(run: Run, prop: str, what: str, want: dict[str, Any], got: dict[str, Any],
                   keys: set[str] | None = None) -> None:
    a, b = by_key(want), by_key(got)
    keys = set(a) | set(b) if keys is None else keys
    diffs = []
    for k in sorted(keys):
        ra, rb = a.get(k), b.get(k)
        sa = row_state(ra, want) if ra else None
        sb = row_state(rb, got) if rb else None
        if sa != sb:
            diffs.append("%s: %s -> %s" % (k, sa, sb))
    if diffs:
        run.bad(prop, "%s: %d rows differ: %s" % (what, len(diffs), "; ".join(diffs[:6])))


# --- the checks ---------------------------------------------------------------------------------------------------


def check_no_readable_leak(run: Run, prop: str, st: dict[str, Any], fam: set[str], snap: hs.Snapshot,
                           where: str) -> None:
    for r in st["rows"]:
        if r["sid"] not in fam or r.get("withheld") or not norm(r.get("content")):
            continue
        vs = versions_for(run.sim, r)
        if run.leaks(vs):
            run.bad(prop, "%s: readable row %s (%s:%s %s) carries withheld words: %r" % (
                where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"), (r.get("content") or "")[:70]))
    # titles of family sessions (C4): a title derived from a withheld message must not be readable
    seen = set()
    for r in st["rows"]:
        sid = r["sid"]
        if sid in seen or sid not in fam:
            continue
        seen.add(sid)
        tag = snap.title_tags.get(sid)
        title = norm(r.get("session_title"))
        if tag is None or not title:
            continue
        if run.sim.lineage(tag) & run.wlms:
            src_title = norm((snap.sessions.get(sid) or {}).get("title"))
            if src_title and src_title[:20] in title:
                run.bad(prop, "%s: session %s title readable although derived from a withheld message: %r" % (
                    where, sid, title[:60]))


def check_certain_derive(run: Run, prop: str, st: dict[str, Any], listed: set[int], fam: set[str] | None,
                         where: str, only: set[str] | None = None) -> None:
    for r in st["rows"]:
        if not admin_withheld(r, st) or r["id"] in listed:
            continue
        if only is not None and r["source_key"] not in only:
            continue
        stone = st["stone_by_key"].get(r["source_key"])
        if stone is not None and stone.get("review") == "pending":
            run.bad(prop, "%s: row %s has a pending stone but is not listed among the holds" % (where, r["id"]))
            continue
        vs = versions_for(run.sim, r)
        if not vs:
            run.notes.append("%s: no ground truth for certain row %s (%s:%s)" % (where, r["id"], r["sid"],
                                                                                  r.get("source_message_id")))
            continue
        if not run.derives(vs):
            run.bad(prop, "%s: row %s (%s:%s %s) withheld for good (review %r, via %r) but derives from no "
                          "withheld message" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"),
                                                stone.get("review") if stone else None,
                                                stone.get("via") if stone else None))


def w_text_present(run: Run, w: Withhold, snap: hs.Snapshot) -> bool:
    for v in snap.rows.values():
        if norm(w.text) and norm(w.text) in norm(v.content) and run.sim.leaks(v.tag, w.lms):
            return True
    return False


def check_evidence(run: Run, prop: str, holds: list[dict[str, Any]], st: dict[str, Any], snap: hs.Snapshot,
                   where: str) -> None:
    rows = {r["id"]: r for r in st["rows"]}
    for h in holds:
        rid = h.get("message")
        r = rows.get(rid)
        if r is None:
            continue
        rep, raw = reported_relations(h)
        if not rep:
            run.bad(prop, "%s: hold %s shows no evidence (fields %s)" % (where, rid, sorted(h)))
            continue
        vs = versions_for(run.sim, r)
        if not vs:
            continue
        stone = st["stone_by_key"].get(r["source_key"]) or {}
        anchor = stone.get("anchor_key") or ""
        ws = [w for w in run.withholds if w.key == anchor] or run.withholds
        ok = False
        why = []
        for w in ws:
            present = w_text_present(run, w, snap)
            if not present:
                if "not_in_backup" in rep or not (rep & set(TEXT_RELATIONS)):
                    ok = True
                why.append("W absent")
                continue
            computed = set()
            for v in vs:
                computed |= text_relations(v.content, w.text)
            texts = " held %r vs withheld %r" % (vs[-1].content[:90], w.text[:90])
            rep_text = rep & set(TEXT_RELATIONS)
            honest = rep_text <= computed and (not computed or bool(rep_text & computed)) and not (
                computed and rep & {"differs", "none"}) and "not_in_backup" not in rep
            if honest:
                ok = True
            why.append("computed %s;%s" % (sorted(computed), texts))
        if not ok:
            run.bad(prop, "%s: hold %s (%s:%s %s) evidence %s %r does not match the recomputation (%s)" % (
                where, rid, r["sid"], r.get("source_message_id"), r.get("role"), sorted(rep), raw[:3],
                "; ".join(why)))


async def scripted_review(run: Run, holds: list[dict[str, Any]], st: dict[str, Any], where: str) -> list[int]:
    """Release exactly the holds the ground truth calls independent; keep the rest. Returns released row ids."""
    rows = {r["id"]: r for r in st["rows"]}
    released = []
    for h in holds:
        rid = h.get("message")
        r = rows.get(rid)
        if r is None:
            continue
        vs = versions_for(run.sim, r)
        if vs and not run.derives(vs):
            resp = await run.api.release(rid)
            if resp.status_code >= 400:
                run.bad("review", "%s: release of independent hold %s refused: %s %s" % (
                    where, rid, resp.status_code, resp.text[:200]))
            else:
                released.append(rid)
        else:
            resp = await run.api.keep(rid)
            if resp.status_code >= 400:
                run.bad("review", "%s: keep of hold %s refused: %s %s" % (where, rid, resp.status_code,
                                                                           resp.text[:200]))
    return released


def check_watch(run: Run, prop: str, records: list[dict[str, Any]], where: str) -> None:
    for rec in records:
        if rec["withheld"] or not norm(rec.get("content")):
            continue
        vs = versions_for(run.sim, rec)
        if run.leaks(vs):
            run.bad(prop, "%s: row %s (%s:%s) was committed readable with withheld words during the import" % (
                where, rec["id"], rec["sid"], rec["source_message_id"]))


def is_reply_quote_hold(row: dict[str, Any], st: dict[str, Any], run: Run) -> bool:
    """Owner Q8(a) default: a reply quoting a withheld message is held even outside a withheld conversation."""
    stone = st["stone_by_key"].get(row.get("source_key"))
    if not (row.get("withheld") and stone and stone.get("review") == "pending"):
        return False
    for v in versions_for(run.sim, row):
        _, quote, _ = peel(v.content)
        if quote and any(norm(quote) in norm(w.text) and len(norm(quote)) >= 16 for w in run.withholds):
            return True
    return False


def compare_outside(run: Run, prop: str, mine: dict[str, Any], plain: dict[str, Any], fam: set[str],
                    where: str) -> None:
    a, b = by_key(mine), by_key(plain)
    diffs = []
    for k in sorted(set(a) | set(b)):
        ra, rb = a.get(k), b.get(k)
        sid = (ra or rb)["sid"]
        if sid in fam:
            continue
        if ra is not None and is_reply_quote_hold(ra, mine, run):
            continue
        sa = (bool(ra.get("withheld")), content_hash(ra.get("content"))) if ra else None
        sb = (bool(rb.get("withheld")), content_hash(rb.get("content"))) if rb else None
        if sa != sb:
            diffs.append("%s (%s) %s vs plain %s" % (k, sid, sa, sb))
    if diffs:
        run.bad(prop, "%s: %d rows outside the family differ from a plain import: %s" % (
            where, len(diffs), "; ".join(diffs[:5])))


def check_stored_unchanged(run: Run, prop: str, pre: dict[str, Any], post: dict[str, Any], fam: set[str],
                           where: str) -> None:
    a, b = by_key(pre), by_key(post)
    for k, ra in a.items():
        if ra["sid"] not in fam:
            continue
        rb = b.get(k)
        if rb is None:
            run.bad(prop, "%s: stored family row %s (%s) disappeared" % (where, k, ra["sid"]))
            continue
        if ra.get("withheld") and not rb.get("withheld"):
            run.bad(prop, "%s: stored family row %s (%s) became readable" % (where, k, ra["sid"]))
        elif not rb.get("withheld") and norm(rb.get("content")) != norm(ra.get("content")):
            run.bad(prop, "%s: stored family row %s (%s) changed its text %r -> %r" % (
                where, k, ra["sid"], (ra.get("content") or "")[:40], (rb.get("content") or "")[:40]))
        elif not norm(ra.get("content")) and norm(rb.get("content")):
            run.bad(prop, "%s: stored family row %s (%s) gained text" % (where, k, ra["sid"]))


def certain_stone_identities(st: dict[str, Any]) -> tuple[set, set, set]:
    exact, copy, uids = set(), set(), set()
    for s in st["stones"]:
        if s.get("review") == "pending":
            continue
        at = round(epoch(s.get("source_at")) or -1.0, 3)
        exact.add((s.get("source_session_id"), str(s.get("source_row_id")), s.get("role"), s.get("tool_call_id") or "",
                   at))
        if s.get("message_uid"):
            copy.add((s.get("message_uid"), at, s.get("role"), s.get("tool_call_id") or ""))
            uids.add(s.get("message_uid"))
    return exact, copy, uids


def check_new_certain(run: Run, prop: str, pre: dict[str, Any], post: dict[str, Any], listed: set[int],
                      fam: set[str], where: str) -> None:
    before = by_key(pre)
    exact, copy, uids = certain_stone_identities(post)
    for r in post["rows"]:
        if r["sid"] not in fam or not admin_withheld(r, post) or r["id"] in listed:
            continue
        old = before.get(r["source_key"])
        if old is not None and old.get("withheld"):
            continue
        vs = versions_for(run.sim, r)
        if not vs:
            continue
        if not run.derives(vs):
            run.bad(prop, "%s: new certain row %s (%s:%s) derives from no withheld message" % (
                where, r["id"], r["sid"], r.get("source_message_id")))
            continue
        ok = False
        for v in vs:
            at = round(v.ts, 3)
            if (v.sid, str(v.row), v.role, v.call or "", at) in exact:
                ok = True
            if v.uid and (v.uid, at, v.role, v.call or "") in copy:
                ok = True
            if set(v.absorbed) & uids:
                ok = True
        if not ok:
            run.bad(prop, "%s: new certain row %s (%s:%s %s) has no exact, copy or absorbed identity of a certain "
                          "stone" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role")))


def check_safe_copies(run: Run, prop: str, pre: dict[str, Any], post: dict[str, Any], fam: set[str],
                      where: str) -> None:
    before = by_key(pre)
    readable = set()
    for r in pre["rows"]:
        if r["sid"] in fam and not r.get("withheld"):
            readable.add((r.get("role"), r.get("tool_call_id") or "", round(epoch(r.get("at")) or 0.0, 3),
                          norm(r.get("content"))))
    for r in post["rows"]:
        if r["sid"] not in fam or r.get("withheld") or r["source_key"] in before:
            continue
        k = (r.get("role"), r.get("tool_call_id") or "", round(epoch(r.get("at")) or 0.0, 3), norm(r.get("content")))
        if k not in readable:
            run.bad(prop, "%s: new family row %s (%s:%s %s) is readable but equals no readable stored row of the "
                          "family: %r" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"),
                                         (r.get("content") or "")[:60]))
