"""Later JARVIS backups (0.6.2 plan step 5; RECOMMENDATION.md section 4, step 5): the later-backup classifier.

A later backup, for a conversation family that holds an admin's decision, is any JARVIS backup other than the one the
decision was taken on (brain_tombstones.decided_on; a decision on no known backup, from a version 4 file or legacy
data, too). The decision backup itself is replayed word for word (importer.Processor.replay_rows, plan 3.2) and
finished once by Settle (settle.py, plan 4). In a later backup Hermes's history between the two backups is out of
sight, so nothing is inferred: identity is exact, and what is new waits for an admin.

5.1 The family F(X) of a later backup X (family(), joined_families()): the union of the sessions bound by stones; the
    closure over brain_session_links (never a delegated sub-agent's or a gateway reset's link: neither copies rows);
    X's own parent links and its _branched_from / _compressed_from markers; the sessions of X with a row that carries
    or absorbs a certain stone's uid; and the sessions of X with a row, at or before the session's start, at the role,
    tool call and time of a stored row or stone of F (copied history: Hermes keeps a copied row's time). A link found
    the last two ways is recorded ('shared_uid', 'copied_rows'), so the family never forgets it.
5.2 This import never writes text into a stored row of F, and never re-keys, deletes or adopts one; blanking it (a
    withhold, a restriction) is always allowed. A row of X that is a stored row (its key, or its exact JARVIS row
    under an earlier key: Hermes's uid backfill) is skipped: the stored row stays as it is.
5.3 Every other row of a session of F in X gets exactly one outcome (Classifier):
    - CERTAIN, withheld for good: the exact identity of a stone (identity.exact); the same uid, time, role and call as
      a certain stone, where that uid is not on a readable or released stored row (Hermes's copy identity); a row
      that absorbs a certain stone's uid (a fold stone is never a uid source).
    - SAFE COPY, readable: its role, call, time and readable text equal a readable stored row of F that is readable
      by at least this row's readers (the same agent and staff, a sensitivity no stricter than the copy's). In the
      same session it is an alias of that row (recorded, not stored twice).
    - HELD FOR REVIEW: everything else, a pending stone 'new in a later backup' with its computed relations to the
      family's withheld texts (plan 4.6), decided on X.
    An admin's release of that exact row decided on X stands. A new session of F keeps its title hidden while a row
    of it is withheld by an admin. A session an admin stopped importing (brain_frozen_sessions) is not imported, and
    is counted.
5.4 A session outside every family gets a plain import; the database guard still applies, and owner question 8 (a)
    holds a reply that quotes a withheld text through JARVIS's reply pointer.
5.5 The tripwire: every withheld text X holds (its rows matched by exact or copy identity) is in memory for the run
    only. A readable row this run would write in F that is identical to one or contains it is held instead and the
    run's verify fails (inside F the count must be 0); outside F such rows are listed to admins by location (archive
    session and row ids), never hidden. An admin's own release, or a safe copy of a row an admin released or left
    readable, is the admin's decision and is not counted. No text and no fingerprint is stored.
5.6 jarvis-import -Mode Plan (importer.plan) reports, per withheld conversation, how many rows would be held.

The classifier is pure (it reads the backup, the stones and what the importer loaded from the database); the importer
writes its outcomes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ArchiveMessage,
    ArchiveSession,
    BrainFrozenSession,
    BrainMaintenance,
    BrainSessionLink,
    BrainWithholdExemption,
)
from . import identity
from . import source as src
from . import text as brain_text

# Import record reasons and counters of this module (no text).
FROZEN = "not imported: an admin stopped importing this conversation"
KEPT_AS_STORED = "kept as stored: a withheld conversation's rows never change"
SAME_ROW = "the same JARVIS row, stored under an earlier key"
SAFE_ALIAS = "a safe copy: the same text as a readable stored row of this conversation"
TITLE_HELD = "new in a later backup"  # archive_sessions.flags.title_held: hidden until no row of it is withheld
TRIPWIRE = "last_tripwire"  # brain_maintenance name: the newest run's tripwire (counts and locations only)
TRIPWIRE_LIST_CAP = 200  # locations listed per run
SENSITIVITY = {"general": 0, "customer": 1, "restricted": 2}
LINK_HOWS = ("shared_uid", "copied_rows")


# --- 5.1 The family -------------------------------------------------------------------------------------------------


async def session_families(s: AsyncSession, backup: src.Backup) -> dict[str, str]:
    """Every JARVIS session's family (a union-find root), over the links that copy rows: the backup's own parent
    links of continuations and branches and its _branched_from / _compressed_from markers, the links recorded by any
    earlier import (brain_session_links, never forgotten) and the archive's lineage. A gateway reset and a delegated
    sub-agent are never family links (0.6.2 plan 5.1, 2.1)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        a, b = a[:200], b[:200]
        if a and b:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    for ss in backup.sessions():
        if ss.lineage in identity.LINEAGE and ss.parent_id:
            union(ss.id, ss.parent_id)
        for marker in ("_branched_from", "_compressed_from"):
            if ss.markers.get(marker):
                union(ss.id, ss.markers[marker])
    for a, b in (await s.execute(select(BrainSessionLink.a, BrainSessionLink.b))).all():
        union(a, b)
    for a, b in (await s.execute(select(ArchiveSession.source_session_id,
                                        ArchiveSession.parent_source_session_id).where(
            ArchiveSession.lineage.in_(identity.LINEAGE),
            ArchiveSession.parent_source_session_id != ""))).all():
        union(a, b)
    return {x: find(x) for x in set(parent) | set(parent.values())}


@dataclass(frozen=True)
class Join:
    """A session of the backup that joins a decision's family although no recorded link or marker puts it there
    (plan 5.1): through a row that carries or absorbs a certain stone's uid ('shared_uid'), or through copied history
    ('copied_rows'). Recorded as a family link (brain_session_links) by the import that finds it."""
    session: str
    group: str
    how: str


async def joined_families(s: AsyncSession, backup: src.Backup, ids: identity.Identities, family: dict[str, str],
                          decision_roots: dict[str, list[str]], joins: list[Join] | None = None
                          ) -> dict[str, list[str]]:
    """Family root -> the decisions whose conversation family it joins although no recorded link or marker puts it
    there (plan 5.1), to a fixed point: a family of this backup with a row that carries or absorbs a certain stone's
    uid (any certain stone of the decision, a fold's too: joining only widens what is held, it never makes a row
    certain), or with a row at or before its session's start at the very role, tool call and time of a stone, a stored
    row or a backup row of a withheld family (copied history: Hermes keeps a copied row's time; never a delegated
    sub-agent's or a gateway reset's session). A family that joins brings its own rows' times along. `joins`, when
    given, receives each joining session with the decision and how it joined (a family link to record)."""
    by_root: dict[str, list[src.SourceSession]] = {}
    for ss in backup.sessions():
        by_root.setdefault(family.get(ss.id[:200], ss.id[:200]), []).append(ss)
    groups_of: dict[str, set[str]] = {root: set(gs) for root, gs in decision_roots.items()}
    if not groups_of:
        return {}
    times: dict[tuple[str, str, Any], set[str]] = {}
    uids: dict[str, set[str]] = {}
    for st in ids.by_key.values():
        if st.group not in ids.by_key:
            continue
        if st.at is not None and st.role and st.session and st.via not in identity.FOLD_VIAS:
            times.setdefault((st.role, st.tool_call_id, st.at), set()).add(st.group)
        if st.uid and st.certain:
            uids.setdefault(st.uid, set()).add(st.group)
    members = {sid for sid, root in family.items() if root in groups_of} | set(groups_of)
    members |= {st.session for st in ids.by_key.values() if st.session}
    sids = sorted(members)
    for i in range(0, len(sids), 500):
        for role, call, at, sid in (await s.execute(
                select(ArchiveMessage.role, ArchiveMessage.tool_call_id, ArchiveMessage.at,
                       ArchiveSession.source_session_id)
                .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                .where(ArchiveSession.source_session_id.in_(sids[i:i + 500]),
                       ArchiveMessage.at.is_not(None)))).all():
            found = groups_of.get(family.get(sid, sid), set())
            if found:
                times.setdefault((role or "", call or "", at), set()).update(found)
    rows: dict[str, list[tuple[str, str, Any, str, tuple[str, ...], bool]]] = {}

    def rows_of(ss: src.SourceSession) -> list[tuple[str, str, Any, str, tuple[str, ...], bool]]:
        if ss.id not in rows:
            rows[ss.id] = [(brain_text.sanitize(m.role)[:40], brain_text.sanitize(m.tool_call_id)[:200], m.at,
                            m.uid[:200], tuple(u[:200] for u in m.absorbed),
                            m.at is not None and ss.started_at is not None and m.at <= ss.started_at)
                           for m in backup.messages(ss.id)]
        return rows[ss.id]

    def add_times(root: str, found: set[str]) -> None:
        for ss in by_root.get(root, ()):
            for role, call, at, _, _, _ in rows_of(ss):
                if at is not None:
                    times.setdefault((role, call, at), set()).update(found)
    for root in list(groups_of):
        add_times(root, groups_of[root])
    joined: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for root, sessions in by_root.items():
            if root in groups_of:
                continue
            found: set[str] = set()
            how: dict[tuple[str, str], str] = {}
            for ss in sessions:
                # a delegated sub-agent's or a gateway reset's copied history is never a family link (plan 5.1)
                linked = ss.lineage not in ("delegated", "reset")
                for role, call, at, uid, absorbed, copied in rows_of(ss):
                    for u in (uid, *absorbed):
                        for g in uids.get(u, set()) if u else set():
                            found.add(g)
                            how.setdefault((ss.id[:200], g), "shared_uid")
                    if copied and linked:
                        for g in times.get((role, call, at), set()):
                            found.add(g)
                            how.setdefault((ss.id[:200], g), "copied_rows")
            if found:
                groups_of[root] = joined[root] = found
                add_times(root, found)
                changed = True
                if joins is not None:
                    joins.extend(Join(sid, g, h) for (sid, g), h in sorted(how.items()))
    return {root: sorted(g) for root, g in joined.items()}


async def frozen_sessions(s: AsyncSession) -> set[str]:
    """The JARVIS sessions an admin stopped importing ("stop importing this conversation", plan 9.3)."""
    return set((await s.scalars(select(BrainFrozenSession.source_session_id))).all())


async def record_joins(s: AsyncSession, joins: Iterable[Join], ids: identity.Identities, run_id: int | None,
                       session_id: str) -> None:
    """The family links this session's joins give (plan 5.1: 'new links are recorded'), recorded once and never
    forgotten: from the session a decision is bound to, to this session, 'shared_uid' or 'copied_rows'."""
    for j in joins:
        if j.session != session_id[:200]:
            continue
        st = ids.by_key.get(j.group)
        if st is None or not st.session or st.session == j.session:
            continue
        await s.execute(pg_insert(BrainSessionLink).values(a=st.session, b=j.session, how=j.how, first_run=run_id)
                        .on_conflict_do_nothing())


# --- 5.3 The classifier ---------------------------------------------------------------------------------------------


@dataclass
class Decision:
    """One decision of a family as this backup shows it: the admin's own stone, its withheld text in this backup
    (None: not in it), the uids and tool calls of its certain stones (a fold's own uid is another message), and
    whether X is a later backup for it."""
    group: str
    stone: identity.Stone
    w_text: str | None
    uids: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    later: bool = True


def decision_of(ids: identity.Identities, group: str, w_text: str | None, backup_sha: str) -> Decision | None:
    own = ids.by_key.get(group)
    if own is None:
        return None
    certain = [st for st in ids.groups.get(group, ()) if st.certain]
    return Decision(group, own, w_text,
                    uids={st.uid for st in certain if st.uid and st.via not in identity.FOLD_VIAS},
                    calls={st.tool_call_id for st in certain if st.tool_call_id},
                    later=own.decided_on != backup_sha)


def relations(m: src.SourceMessage, here: identity.Exact, d: Decision) -> set[str]:
    """The relations of a backup row to one decision's withheld message (plan 4.6): its text against the withheld
    text in this backup, and the same uid, tool call or role and time with other text, a reused row number; 'not in
    this backup' when the withheld text is not here. Empty: no relation."""
    own = d.stone
    rel = identity.text_relations(m.content, d.w_text) if d.w_text is not None else set()
    same_words = bool(rel & {identity.IDENTICAL_REL, identity.RESTATED_REL})
    if here.uid and (here.uid in d.uids or (own.uid and here.uid == own.uid and own.via not in identity.FOLD_VIAS)) \
            and not same_words:
        rel.add(identity.SAME_UID)
    if here.tool_call_id and here.tool_call_id in d.calls and not same_words:
        rel.add(identity.SAME_CALL)
    if own.at is not None and here.at == own.at and here.role == own.role \
            and (here.session, here.row) != (own.session, own.row):
        rel.add(identity.SAME_TIME_REL)
    if own.session and own.row and (here.session, here.row) == (own.session, own.row) \
            and not identity.same_row(identity.exact_of_stone(own), here):
        rel.add(identity.REUSED_ROW)
    if d.w_text is None:
        rel.add(identity.NOT_IN_BACKUP)
    return rel


def strongest(m: src.SourceMessage, here: identity.Exact, decisions: list[Decision]
              ) -> tuple[Decision, tuple[str, ...]]:
    """The decision a held row is listed under, and its relations to that decision's withheld message: one whose
    text it is, holds or quotes first; then one whose withheld text is not in this backup (so 'none' is never said
    while a withheld text of the family could not be compared: the one-click release of rows with no relation
    is never offered then); then one with a structural relation; else the first. 'none' when there is no relation."""
    scored = []
    for n, d in enumerate(decisions):
        rel = relations(m, here, d)
        text = bool(rel & set(identity.TEXT_RELATIONS))
        rank = (0 if text else 1 if identity.NOT_IN_BACKUP in rel else 2 if rel else 3,
                0 if identity.IDENTICAL_REL in rel else 1, n)
        scored.append((rank, d, rel))
    _, d, rel = min(scored, key=lambda x: x[0])
    return d, tuple(identity.relation_order(rel or {identity.NO_RELATION}))


def copy_identity(st: identity.Stone, here: identity.Exact) -> bool:
    """Hermes's copy identity of a stone: the same uid, time, role and tool call (a copy keeps all four)."""
    return (bool(st.uid) and st.at is not None and here.uid == st.uid and here.at == st.at
            and (here.role, here.tool_call_id) == (st.role, st.tool_call_id))


MOVED = identity.MOVED


def certain(ids: identity.Identities, session_id: str, m: src.SourceMessage, has_row: Callable[[str, str], bool],
            readable_uids: set[str], *, here: identity.Exact | None = None,
            absorbed: Iterable[str] | None = None) -> identity.Match | None | object:
    """Plan 5.3's CERTAIN for a backup row, and a stone that already names it: a Match (a member, or the hold a
    pending stone keeps), None, or MOVED (its key's stone is another message: the row is left out as a key
    collision, never withheld for good by that key). For a row of the backup that is a stored row (plan 5.2: its key,
    or its exact JARVIS row under an earlier key), `here` and `absorbed` are the STORED row's identity and absorbed
    uids: a stored row is judged by what it holds, never by what JARVIS changed in it since (a fold of it, a
    renumbering: owner question 7), so a stored row is never withheld for words it does not hold.
    - the key's stone, bound to this exact JARVIS row (identity.exact) or unbound (a key-only decision); a certain
      stone bound to a row the backup no longer holds passes to the key's next holder only with that stone's copy
      identity (a hold passes as the hold it is); bound to another row still in the backup, or to this row id with
      another identity (a reused row id): MOVED;
    - the exact JARVIS row of a stone (identity.Identities.row_stones);
    - a row a hold (a pending stone) names stays that hold: nothing below makes it a member without an admin;
    - a row that absorbs a certain stone's uid (never a fold stone's own uid);
    - the same uid, time, role and call as a certain stone (never a fold stone), unless a readable or released stored
      row carries that uid."""
    here = here if here is not None else identity.exact_of_source(session_id, m)
    absorbed = tuple(absorbed) if absorbed is not None else m.absorbed
    key_match: identity.Match | None = None
    st = ids.by_key.get(m.key)
    if st is not None:
        review = identity.PENDING if st.review == identity.PENDING else identity.CERTAIN
        if not st.row or not st.session:
            key_match = identity.Match(st, "key", review)
        elif (st.session, st.row) == (here.session, here.row):
            if not identity.same_row(identity.exact_of_stone(st), here):
                return MOVED
            key_match = identity.Match(st, "key", review)
        elif has_row(st.session, st.row):
            return MOVED
        elif review == identity.PENDING or copy_identity(st, here):
            key_match = identity.Match(st, "key", review)
        else:
            return MOVED
    if key_match is not None and key_match.review == identity.CERTAIN:
        return key_match
    found = ids.row_stones(here)
    if found and found[0].certain:
        return identity.Match(found[0], "same JARVIS row", identity.CERTAIN)
    # a hold stays the hold it is until an admin decides: no rule makes a held row a member without review
    if key_match is not None:
        return key_match
    if found:
        return identity.Match(found[0], "same JARVIS row", identity.PENDING)
    for u in absorbed:
        hit = [x for x in ids.by_uid.get(u[:200], ()) if x.certain and x.via not in identity.FOLD_VIAS]
        if hit:
            return identity.Match(hit[0], identity.FOLD, identity.CERTAIN)
    if here.uid and here.uid not in readable_uids:
        hit = [x for x in ids.uid_stones(here.uid, here.role) if copy_identity(x, here)]
        if hit:
            return identity.Match(hit[0], "copied by JARVIS", identity.CERTAIN)
    return None


@dataclass(frozen=True)
class Readable:
    """A readable stored row of the family (load_family_readable), for a safe copy: its row and archive session, its
    readable text (identity.message_text: in memory only), who reads it (the conversation's agent and staff, its own
    and its conversation's sensitivity), and whether an admin released it or left it readable."""
    row_id: int
    session_id: int
    text: tuple[str, str, str, str]
    agent: str
    staff: str
    sensitivity: str
    session_sensitivity: str
    admin_decided: bool

    def read_by_at_least(self, agent: str, staff: str, sensitivity: str) -> bool:
        """Whoever may read a row of a conversation of this agent and staff with this sensitivity may read this row:
        the same agent (an agent reads its own conversations) and staff, and no stricter sensitivity (the row's or
        its conversation's: an operator reads general rows of general conversations only)."""
        want = SENSITIVITY.get(sensitivity, 2)
        return ((self.agent, self.staff) == (agent, staff) and SENSITIVITY.get(self.sensitivity, 2) <= want
                and SENSITIVITY.get(self.session_sensitivity, 2) <= want)


async def load_family_readable(s: AsyncSession, sids: Iterable[str]
                               ) -> dict[tuple[str, str, Any], list[Readable]]:
    """The readable stored rows of these JARVIS sessions by (role, tool call, time), with their readable text (in
    memory only), readers, and whether an admin's release or 'left readable' covers that exact row."""
    out: dict[tuple[str, str, Any], list[Readable]] = {}
    found = (await s.execute(
        select(ArchiveMessage, ArchiveSession.id, ArchiveSession.source_session_id, ArchiveSession.agent,
               ArchiveSession.staff, ArchiveSession.sensitivity)
        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .where(ArchiveSession.source_session_id.in_(sorted(set(sids))), ArchiveMessage.withheld.is_(False),
               ArchiveMessage.at.is_not(None)))).all()
    if not found:
        return out
    decided: dict[tuple[str, str], list[identity.Binding]] = {}
    for sid, rid, role, call, at, uid in (await s.execute(select(
            BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id,
            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id, BrainWithholdExemption.source_at,
            BrainWithholdExemption.source_uid).where(
            BrainWithholdExemption.source_session_id.in_(sorted({r[2] for r in found}))))).all():
        decided.setdefault((sid, rid), []).append(identity.Binding(role or "", call or "", at, uid or ""))
    for row, sess_id, sid, agent, staff, sess_sens in found:
        here = identity.exact_of_row(row, sid)
        admin = any(b.covers(here) for b in decided.get((here.session, here.row), ()))
        out.setdefault((row.role or "", row.tool_call_id or "", row.at), []).append(Readable(
            int(row.id), int(sess_id), identity.message_text(row), agent or "", staff or "", row.sensitivity or "",
            sess_sens or "", admin))
    return out


def safe_copy(here: identity.Exact, text: tuple[str, str, str, str], agent: str, staff: str, sensitivity: str,
              family_readable: dict[tuple[str, str, Any], list[Readable]]) -> Readable | None:
    """Plan 5.3's SAFE COPY: the readable stored row of the family with this row's role, tool call, time and readable
    text, readable by at least this row's readers; None when there is none."""
    if here.at is None:
        return None
    for r in family_readable.get((here.role, here.tool_call_id, here.at), ()):
        if r.text == text and r.read_by_at_least(agent, staff, sensitivity):
            return r
    return None


def tripwire_relations(text: str | None, w_texts: Iterable[str]) -> set[str]:
    """Plan 5.5: whether a readable text is identical to a withheld text the backup holds, or contains it (the
    relation codes found; empty: no hit). Compared in memory only."""
    out: set[str] = set()
    for w in w_texts:
        rel = identity.text_relations(text, w) & {identity.IDENTICAL_REL, identity.CONTAINS, identity.RESTATED_REL}
        out |= rel
    return out


# --- 5.5, 5.6: what a run reports ---------------------------------------------------------------------------------


@dataclass
class Tripwire:
    """The run's tripwire (plan 5.5): counts and locations only."""
    inside: int = 0  # readable rows of F this run would have written with a withheld text: held instead
    outside: int = 0  # such rows outside every family: listed to admins
    admin_decided: int = 0  # readable because an admin released them (or left them readable): not counted
    inside_at: list[dict[str, Any]] = field(default_factory=list)
    outside_at: list[dict[str, Any]] = field(default_factory=list)
    texts: int = 0  # withheld texts in memory (how many decisions' texts this backup holds)

    @classmethod
    def from_tally(cls, data: dict[str, Any] | None) -> Tripwire:
        d = dict(data or {})
        return cls(int(d.get("inside_family") or 0), int(d.get("outside_family") or 0),
                   int(d.get("admin_decided") or 0), list(d.get("inside") or []), list(d.get("outside") or []),
                   int(d.get("withheld_texts_in_backup") or 0))

    def to_tally(self) -> dict[str, Any]:
        return {"inside_family": self.inside, "outside_family": self.outside, "admin_decided": self.admin_decided,
                "inside": self.inside_at[:TRIPWIRE_LIST_CAP], "outside": self.outside_at[:TRIPWIRE_LIST_CAP],
                "withheld_texts_in_backup": self.texts}


async def note_tripwire(s: AsyncSession, run_id: int, data: dict[str, Any]) -> None:
    """brain_maintenance('last_tripwire'): the newest import run's tripwire (counts and locations: archive session and
    row ids; never text), shown to admins in the status (brain_withhold_pending().last_tripwire)."""
    detail = {"run": run_id, **{k: v for k, v in (data or {}).items()}}
    stmt = pg_insert(BrainMaintenance).values(name=TRIPWIRE, detail=detail)
    await s.execute(stmt.on_conflict_do_update(index_elements=[BrainMaintenance.name],
                                               set_={"detail": stmt.excluded.detail,
                                                     "done_at": stmt.excluded.done_at}))


def plan_entry(report: dict[str, Any], conversation: int | str, field_name: str, n: int = 1) -> None:
    """Plan 5.6: one counter of one withheld conversation in the report's 'later_backup' section (counts only)."""
    found = report.setdefault("later_backup", {}).setdefault(str(conversation), {})
    found[field_name] = int(found.get(field_name) or 0) + n

