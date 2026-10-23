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

import copy
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
# a pointer that starts a turn Hermes's fold merged in (prev + '\n\n' + msg) is that turn's own wrapper (deviation 258)
_REPLY_ANY = re.compile(r'(?:\A|\n\n)\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)


def message_lms(sim: hs.Sim, tag: hs.Tag) -> set[int]:
    """The withheld message for A1/A2/B2 (plan 5.1: rows that derive from W): the words the withheld row carries
    that are its own logical message or what that message re-sends or copies (its lineage). A fold, or a summary that
    carries a restated request, also carries the words of the message it absorbed: that message is its own, not a
    copy of the withheld one, and plan 4.3 holds no plain row that is a part of the withheld text (only a reply quote,
    owner Q8 (a)); so a readable original of an absorbed part is not a leak of the withheld message (deviation 249;
    listed as a residual in the integration report). For any other row this is exactly the words it carries."""
    return {tag.origin} | (set(tag.carries) & sim.lineage(tag.origin))


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
        # the restated request keeps its own wrappers after the header (Hermes: HEADER + "\n" + the request; deviation
        # 280): its Discord note, then its reply pointer
        body = _DISCORD.sub("", body.rsplit(hs.REPLAY_HEADER, 1)[1].lstrip(), count=1)
        m = _REPLY.match(body)
        if m:
            quote, body = (quote if quote is not None else m.group(1)), body[m.end():]
        restated = True
    return body.strip(), quote, restated


def reply_quotes(text: str | None) -> list[str]:
    """Every reply pointer's quote that starts the text (after the Discord note), a turn a fold merged into it, or a
    request restated after the replay header (deviation 280)."""
    body = _DISCORD.sub("", text or "", count=1)
    out = [m.group(1) for m in _REPLY_ANY.finditer(body)]
    for part in body.split(hs.REPLAY_HEADER)[1:]:
        m = _REPLY.match(_DISCORD.sub("", part.lstrip(), count=1))
        if m:
            out.append(m.group(1))
    return out


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
        # one side is the other after the replay header, or the held row is the withheld text restated after the
        # replay header (plan 4.3 'W restated after the replay header'; true also when W itself is such a
        # restatement and the held row is its copy: integrator, deviation 277)
        if restated or (hn != wn and hs.REPLAY_HEADER in (w or "")):
            out.add("restated")
    if len(wbody) >= 16 and wbody in norm(body or h):  # true of an identical or restated row too
        out.add("contains")
    quotes = ([quote] if quote is not None else []) + reply_quotes(h)
    if any(len(q) >= 16 and norm(q) and norm(q) in wbody for q in quotes):  # as JARVIS wrote it (deviation 273)
        out.add("part_of")
    # used for holds inside a withheld family only: a row whose own text is part of W (deviation 274)
    if len(norm(body)) >= 16 and norm(body) != wbody and norm(body) in wbody:
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


def tkey(t: float | None) -> float | None:
    """A time as a comparison key, at the microsecond the Command Center stores (source.py: datetime.fromtimestamp,
    microseconds). Milliseconds were used before; a time at a half millisecond rounded one way as JARVIS's float and
    the other way as the stored microsecond value, so the same row did not match itself (integrator, deviation 276)."""
    return None if t is None else round(t, 6)


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


def backup_versions(run: "Run", row: dict[str, Any], snap: hs.Snapshot) -> list[hs.Version]:
    """The row's version in the backup a hold's evidence was computed on (deviation 241): a held row is blank, so
    versions_for cannot tell its versions apart by text and would compare every text the row ever had."""
    vs = versions_for(run.sim, row)
    rid = str(row.get("source_message_id") or "")
    v = snap.rows.get((row["sid"], int(rid))) if rid.isdigit() else None
    if v is None and rid.isdigit():
        # a stale stored row (this backup no longer holds it): the text the Command Center stored for it is the one
        # the newest imported backup that held it brought (deviation 270)
        for older in reversed(run.imported):
            if (row["sid"], int(rid)) in older.rows:
                v = older.rows[(row["sid"], int(rid))]
                break
    if v is not None and any(x is v or (x.role, tkey(x.ts), x.uid, x.content) == (v.role, tkey(v.ts), v.uid,
                                                                                    v.content) for x in vs):
        return [v]
    return vs


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


@dataclass
class Withhold:
    cc_id: int
    key: str
    sid: str
    row: int
    text: str
    lms: set[int]  # the logical message the withheld row is a version or copy of, and what that copies (message_lms)
    origin: int
    role: str = ""  # the withheld row's own identity when withheld (deviation 245)
    at: float | None = None
    uid: str = ""
    absorbed: tuple[str, ...] = ()  # the uids the withheld row had absorbed (a withheld fold; deviation 275)


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
                keys.add((r.get("role"), r.get("tool_call_id") or "", tkey(epoch(r.get("at")) or 0.0)))
        for s in stones:
            if s.get("source_session_id") in fam and s.get("source_at"):
                keys.add((s.get("role"), s.get("tool_call_id") or "", tkey(epoch(s.get("source_at")) or 0.0)))
        for sid, sess in current.sessions.items():
            if sid in fam:
                continue
            for (rsid, _), v in current.rows.items():
                if rsid != sid:
                    continue
                if (v.uid and v.uid in stone_uids) or (set(v.absorbed) & stone_uids):
                    fam.add(sid)
                    break
                if v.ts <= (sess.get("started_at") or 0) + 1e-6 and (v.role, v.call or "", tkey(v.ts)) in keys:
                    fam.add(sid)
                    break
        if fam == before:
            return fam


def decision_family(run: Run, w: Withhold, current: hs.Snapshot, st: dict[str, Any]) -> set[str]:
    """The family (5.1) of one decision: family() started from this withhold and its own group's stones only."""
    proxy = copy.copy(run)
    proxy.withholds = [w]
    stones = [s for s in st["stones"] if w.key in (s.get("source_key"), s.get("anchor_key"))]
    return family(proxy, current, st["rows"], stones)


# --- stored state -------------------------------------------------------------------------------------------------


async def state() -> dict[str, Any]:
    rows = await cc.rows()
    stones = await cc.stones()
    by_key = {s["source_key"]: s for s in stones}
    return {"rows": rows, "stones": stones, "stone_by_key": by_key}


def released_pending(row: dict[str, Any], st: dict[str, Any]) -> bool:
    """An admin released this held row; its text comes back at the next import of a backup holding it (owner Q6:
    nothing computed from it is stored), so until then it is still withheld, with no stone (integrator, deviation
    240)."""
    flag = (row.get("flags") or {}).get("withheld_by_admin") or {}
    return bool(row.get("withheld")) and bool(flag.get("released_at")) and row.get("source_key") not in st[
        "stone_by_key"]


def hide_title_choice(run: "Run", row: dict[str, Any], snap: hs.Snapshot, carries: set[int]) -> dict[str, Any]:
    """The admin's 'hide the conversation title' choice in the withhold dialog (C6 lists it among the admin's
    decisions; the dialog shows the conversation's title): ticked when the title shown derives from the message being
    withheld, else the dialog's default (deviation 246)."""
    tag = snap.title_tags.get(row["sid"])
    if norm(row.get("session_title")) and tag is not None and run.sim.lineage(tag) & set(carries):
        return {"hide_title": True}
    return {}


def check_released_blank(run: "Run", prop: str, st: dict[str, Any], where: str) -> None:
    """Until the next import, a released row stays hidden and holds no text (fail-closed; deviation 240)."""
    for r in st["rows"]:
        if released_pending(r, st) and norm(r.get("content")):
            run.bad(prop, "%s: released row %s (%s:%s) holds text before the next import" % (
                where, r["id"], r["sid"], r.get("source_message_id")))


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


def compare_c1(run: Run, prop: str, tc: dict[str, Any], live: dict[str, Any], plain_tc: dict[str, Any],
               plain_live: dict[str, Any], decision: hs.Snapshot) -> None:
    """C1: every JARVIS row live stores has the test copy's state, and live stores every row of the decision backup
    the test copy stores. The test copy may have imported earlier backups first; where a plain import of the test
    copy's backups and a plain import of the decision backup alone already store a row differently (the importer's
    own history handling, with no decision at all), that difference is not C1's: it is excused only when both sides
    are exactly the plain imports' (readable with the plain content, or not stored on either live side), and noted
    (deviation 250)."""
    a, b, pa, pb = by_key(tc), by_key(live), by_key(plain_tc), by_key(plain_live)

    def plain(r: dict[str, Any] | None) -> tuple | None:
        return None if r is None else (bool(r.get("withheld")), content_hash(r.get("content")))
    diffs, excused = [], []
    keys = set(b) | {k for k, r in a.items() if (r["sid"], int(r["source_message_id"])) in decision.rows}
    for k in sorted(keys):
        ra, rb = a.get(k), b.get(k)
        sa = row_state(ra, tc) if ra else None
        sb = row_state(rb, live) if rb else None
        if sa == sb:
            continue
        same_as_plain = plain(pa.get(k)) != plain(pb.get(k)) and (
            (rb is None and pb.get(k) is None and (ra is None or plain(ra) == plain(pa.get(k))
                                                    or bool(ra.get("withheld"))))
            or (ra is not None and rb is not None and not ra.get("withheld") and not rb.get("withheld")
                and plain(ra) == plain(pa.get(k)) and plain(rb) == plain(pb.get(k))))
        (excused if same_as_plain else diffs).append("%s: %s -> %s" % (k, sa, sb))
    if excused:
        run.notes.append("%s: %d rows differ exactly as the plain imports do (no decision involved): %s" % (
            prop, len(excused), "; ".join(excused[:4])))
    if diffs:
        run.bad(prop, "test copy vs live: %d rows differ: %s" % (len(diffs), "; ".join(diffs[:6])))


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
        if not admin_withheld(r, st) or r["id"] in listed or released_pending(r, st):
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


def w_text_in(run: Run, w: Withhold, snap: hs.Snapshot, st: dict[str, Any] | None = None) -> str | None:
    """The withheld text as this backup holds it, found where the plan says the Command Center finds it (4.6, 5.3 R4,
    5.5, owner Q6; deviation 245): the withheld JARVIS row itself (session, row, role, time, and uid when both have
    one), else a Hermes copy identity of it (the same uid, time and role), else the JARVIS row of a certain member of
    its decision that holds the very words (A2 checks those derive from it). None when the backup holds none of them:
    a copy that only text, or a tool call and time without a uid, links to it does not count, and with no original
    left the evidence is 'the withheld text is not in this backup' (5.3 R4). An edit JARVIS made in place to the
    withheld row (a prune) is what the backup holds: edits inside a withheld family are out of contract (section 3)."""
    if not norm(w.text):
        return None
    for (sid, row), v in snap.rows.items():
        if (sid, row, v.role, tkey(v.ts)) == (w.sid, int(w.row), w.role, tkey(w.at)) and not (
                w.uid and v.uid and w.uid != v.uid) and norm(v.content) != norm(hs.STUB) and not (
                set(v.absorbed) - set(w.absorbed)):
            return v.content
    if w.uid:  # Hermes's prune placeholder is not the withheld text; its archived twin is (deviation 265)
        for v in snap.rows.values():
            if v.uid == w.uid and (tkey(v.ts), v.role) == (tkey(w.at), w.role) and norm(v.content) != norm(hs.STUB) \
                    and not set(v.absorbed) - set(w.absorbed):  # a later fold of W holds more than W (deviation 275)
                return v.content
    rows, copies = set(), set()
    for s in (st or {}).get("stones", []):
        if s.get("review") == "pending" or w.key not in (s.get("source_key"), s.get("anchor_key")):
            continue
        if s.get("review") == "kept" and "identical" not in (s.get("relations") or []):
            continue  # a kept hold is known to hold the withheld words only when its evidence said so (C3)
        at = tkey(epoch(s.get("source_at")))
        if str(s.get("source_row_id") or "").isdigit():
            rows.add((s.get("source_session_id"), int(s["source_row_id"]), s.get("role"), at))
        if s.get("message_uid"):
            copies.add((s["message_uid"], at, s.get("role")))
    wbody = norm(peel(w.text)[0])
    for (sid, row), v in snap.rows.items():
        # the very words: equal, or 'identical' as plan 4.6 defines it (the same authored text inside Hermes's
        # wrappers, e.g. W restated after the replay header under W's own uid), as the member's evidence said
        # (fixer, deviation 288)
        same = norm(v.content) == norm(w.text) or (bool(wbody) and norm(peel(v.content)[0]) == wbody)
        if not same or norm(v.content) == norm(hs.STUB) or not run.sim.leaks(v.tag, w.lms):
            continue
        if (sid, row, v.role, tkey(v.ts)) in rows or (v.uid and (v.uid, tkey(v.ts), v.role) in copies):
            return v.content
    return None


def w_text_present(run: Run, w: Withhold, snap: hs.Snapshot, st: dict[str, Any] | None = None) -> bool:
    return w_text_in(run, w, snap, st) is not None




def check_evidence(run: Run, prop: str, holds: list[dict[str, Any]], st: dict[str, Any], snap: hs.Snapshot,
                   where: str) -> None:
    rows = {r["id"]: r for r in st["rows"]}
    comps = {w.key: decision_family(run, w, snap, st) for w in run.withholds}
    for h in holds:
        rid = h.get("message")
        r = rows.get(rid)
        if r is None:
            continue
        rep, raw = reported_relations(h)
        if not rep:
            run.bad(prop, "%s: hold %s shows no evidence (fields %s)" % (where, rid, sorted(h)))
            continue
        vs = backup_versions(run, r, snap)
        if not vs:
            continue
        stone = st["stone_by_key"].get(r["source_key"]) or {}
        decided = stone.get("decided_on") or ""
        if decided and snap.sha256 and decided != snap.sha256:
            # a hold decided on another backup (an earlier decision's, untouched by this Settle) carries the
            # evidence computed on that backup (plan 4.4, 4.6; deviation 248): recomputed there, not here
            continue
        anchor = stone.get("anchor_key") or ""
        # the hold is listed under one decision; its relations are computed against every decision of the family
        # (core-3 deviation 201): honest when it matches the recomputation against any of them (deviation 248)
        own = [w for w in run.withholds if w.key == anchor] or run.withholds
        # only the decisions of this row's own family (plan section 2 correction 4: 'every withheld text of that
        # family'): a decision in an unlinked conversation is not this hold's (integrator, deviation 253)
        fam_ws = [w for w in run.withholds if r["sid"] in comps[w.key]]
        ws = own + [w for w in fam_ws if w not in own]
        ok = False
        why = []
        for w in ws:
            wt = w_text_in(run, w, snap, st)
            rep_text = rep & set(TEXT_RELATIONS)
            # the withheld text as the backup holds it, and as the admin withheld it: a relation computed earlier
            # with the withheld text in memory (the withhold dialog, an earlier Settle) stays on a hold when this
            # backup no longer holds the text (settle.evidence), and a prune JARVIS made in place changes what the
            # backup holds (deviation 245)
            views = [w.text] if wt is None else [wt] + ([w.text] if norm(wt) != norm(w.text) else [])
            for view in views:
                computed = set()
                for v in vs:
                    computed |= text_relations(v.content, view)
                if w not in own:  # another decision of the family: only a text relation it really has
                    honest = bool(rep_text) and rep_text <= computed and not rep & {"differs", "none"}
                elif wt is None:
                    honest = ("not_in_backup" in rep and not rep_text) or (not rep_text and not computed) or (
                        bool(rep_text) and rep_text <= computed and not rep & {"differs", "none"})
                else:
                    honest = rep_text <= computed and (not computed or bool(rep_text & computed)) and not (
                        computed and rep & {"differs", "none"}) and "not_in_backup" not in rep
                if honest:
                    ok = True
                why.append("%scomputed %s; held %r vs withheld %r" % ("W absent, " if wt is None else "",
                                                                      sorted(computed), vs[-1].content[:90],
                                                                      view[:90]))
        if ok and "none" in rep:
            # 'none' gates the one-click release (plan section 2 correction 4; core-3 deviation 201): no withheld text
            # of the family that this backup holds relates to the row (deviation 251)
            for w in own + [w for w in fam_ws if w not in own]:
                wt = w_text_in(run, w, snap, st)
                related = set()
                for v in vs:
                    related |= text_relations(v.content, wt) if wt is not None else set()
                if related:
                    ok = False
                    why.append("'none' but %s to the withheld %r" % (sorted(related), (wt or "")[:60]))
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


def check_watch(run: Run, prop: str, records: list[dict[str, Any]], where: str, final: dict[str, Any],
                fam: set[str], tc: dict[str, Any], fam_tc: set[str]) -> None:
    """C1 at any moment: no withheld text is ever committed readable on live during the import with the decisions
    file. Every record is judged unless its row is provably a plain import's readable row on both installations: in
    no family (live's or the test copy's: A1's scope), readable on live when the import ends, and readable (or not
    stored) on the test copy. Such a row is a verbatim copy in a conversation with no recorded link (plan 5.4, owner
    Q8 (b); section 8 residual, e.g. a rotation whose parent JARVIS deleted before any import saw it): C1 requires it
    to end readable exactly as on the test copy, so it is necessarily committed readable at some moment and a check
    that flags it cannot be met by any implementation of the contract. A4 judges it against a plain import. A row
    that ends withheld on either side, a family row, and a row the import wrote and then removed are all judged
    (integrator deviation 279; fixer deviation 284: the test copy's state is used too, fail-closed)."""
    live_rows = {r["id"]: r for r in final["rows"]}
    tc_rows = {(r["sid"], str(r.get("source_message_id"))): r for r in tc["rows"]}
    for rec in records:
        if rec["withheld"] or not norm(rec.get("content")):
            continue
        end = live_rows.get(rec["id"])
        mine = tc_rows.get((rec["sid"], str(rec.get("source_message_id"))))
        plain = (rec["sid"] not in fam and rec["sid"] not in fam_tc and end is not None and not end.get("withheld")
                 and (mine is None or not mine.get("withheld")))
        if plain and norm(rec.get("content")) == norm(end.get("content")):
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
        for quote in reply_quotes(v.content):
            if quote and any(norm(quote) and norm(quote) in norm(w.text) and len(quote) >= 16 for w in run.withholds):
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
        if released_pending(ra, pre):  # C2(a): the admin released it; its text comes back now (deviation 240)
            continue
        if ra.get("withheld") and not rb.get("withheld"):
            run.bad(prop, "%s: stored family row %s (%s) became readable" % (where, k, ra["sid"]))
        elif not rb.get("withheld") and norm(rb.get("content")) != norm(ra.get("content")):
            run.bad(prop, "%s: stored family row %s (%s) changed its text %r -> %r" % (
                where, k, ra["sid"], (ra.get("content") or "")[:40], (rb.get("content") or "")[:40]))
        elif not norm(ra.get("content")) and norm(rb.get("content")):
            run.bad(prop, "%s: stored family row %s (%s) gained text" % (where, k, ra["sid"]))


ANY_TIME = "any time"


def certain_stone_identities(st: dict[str, Any]) -> tuple[set, set, set]:
    exact, copy, uids = set(), set(), set()
    for s in st["stones"]:
        if s.get("review") == "pending":
            continue
        # a stone with no time is the exact JARVIS row at any time, as the 0008 trigger matches it (t_at IS NULL): a
        # recorded duplicate with no row of its own at the withhold (plan 4.5 member) (integrator, deviation 281)
        at = tkey(epoch(s.get("source_at"))) if s.get("source_at") else ANY_TIME
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
            at = tkey(v.ts)
            if (v.sid, str(v.row), v.role, v.call or "", at) in exact or (
                    v.sid, str(v.row), v.role, v.call or "", ANY_TIME) in exact:
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
            readable.add((r.get("role"), r.get("tool_call_id") or "", tkey(epoch(r.get("at")) or 0.0),
                          norm(r.get("content"))))
    for r in post["rows"]:
        if r["sid"] not in fam or r.get("withheld") or r["source_key"] in before:
            continue
        k = (r.get("role"), r.get("tool_call_id") or "", tkey(epoch(r.get("at")) or 0.0), norm(r.get("content")))
        if k not in readable:
            run.bad(prop, "%s: new family row %s (%s:%s %s) is readable but equals no readable stored row of the "
                          "family: %r" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"),
                                         (r.get("content") or "")[:60]))
