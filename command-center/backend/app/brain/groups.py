"""Option C (0.6.2): a withheld message is held with its conversation group (rebuild R1; docs/rebuild-062).

The group of an admin's withhold (its anchor: the admin's own stone) is the closure, from the conversations its certain
stones are bound to (seeds(): never a pending hold's), over the links between JARVIS conversations (J1-J9):

J1 a compression continuation by the parent's end ('compression'): linked both ways (it copies the parent's rows);
J2 a continuation by Hermes's _compressed_from marker (a rotation, hermes_state_mutation_compress), also when Hermes
   deleted the parent since (parent NULL, the marker stays): both ways;
J3 a branch (_branched_from naming the parent, or the parent ended 'branched'): both ways;
J4 a gateway reset (_reset_from naming the parent): both ways;
J5 a delegated sub-agent (_delegate_from naming the parent): from the parent down only;
J6 a rotation that inherited its parent's own fork marker (source._markers, round 3 fix A): a continuation, both ways;
J7 a markerless child of a conversation (O-D1): from the parent down only (its parent side is a pending owner decision,
   groups-paths B and D: not implemented);
J8 copied history, for a conversation with no anchoring parent (none, one that resolves nowhere here, or an orphan whose
   only parent is a delegate reading: Hermes v16's '__orphaned__' tag and an older Hermes's inherited _delegate_from):
   rows at or before its start at the very role, tool call and time of another conversation's rows (Hermes keeps a
   copied row's time); J8_MIN_ROWS such rows join it as a member, fewer as 'uncertain'; a gateway reset copies no rows,
   so copied-looking rows join it only as 'uncertain';
J9 provenance: an orphaned branch (its parent resolves nowhere here) joins with one copied row; a conversation with a
   row that carries or absorbs a certain stone's uid joins ('shared_uid').

Links come from the JARVIS backup in hand (each session's parent and every lineage marker), from the archive's
lineage, and from the links any import recorded (brain_session_links, never forgotten). A conversation is 'uncertain'
(held, never released in one click, the export waits) when its parent does not resolve here, when it has two parents
(a marker naming another conversation, or the sources disagree), when a copied-history link is weak, or when it is
reached only through an uncertain conversation; an uncertain conversation never becomes a member again. Each member is
recorded (brain_withheld_sessions, durable): the title trigger of migration 0008 hides its title, the startup repair
(R9) blanks its rollback copies, and the hold sweep (R2) holds its rows. The rows of an uncertain conversation are held
here already (hold_uncertain)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text as sql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ArchiveMessage,
    ArchiveSession,
    BrainHiddenTitle,
    BrainSessionLink,
    BrainTombstone,
    BrainWithheldSession,
    BrainWithholdExemption,
    ImportRecord,
    ImportRun,
)
from . import identity
from . import source as src
from . import text as brain_text

MEMBER, UNCERTAIN = "member", "uncertain"
WITHHELD_HERE = "withheld_here"
HOWS = ("withheld_here", "continuation", "branch", "reset", "delegated", "child", "linked", "shared_uid", "copied_rows",
        "provenance")  # brain_withheld_sessions.how
# How a conversation joins a group, and which links are followed both ways (a parent holds what its copying child
# copied); a delegated sub-agent and a markerless child join from the parent down only (O-D1).
BOTH_WAYS = frozenset({"continuation", "branch", "reset", "linked", "shared_uid", "copied_rows", "provenance"})
DOWN_ONLY = frozenset({"delegated", "child"})
COPIED = frozenset({"shared_uid", "copied_rows", "provenance"})  # links by evidence, not by a parent
LINEAGE_HOW = {"continuation": "continuation", "branch": "branch", "reset": "reset", "delegated": "delegated",
               "": "child"}
MARKER_HOW = {"_compressed_from": "continuation", "_branched_from": "branch", "_reset_from": "reset",
              "_delegate_from": "delegated"}
LINK_HOW = {"parent": "linked", "branched_from": "branch", "compressed_from": "continuation",
            "shared_uid": "shared_uid", "copied_rows": "copied_rows", "delegate_from": "delegated",
            "reset_from": "reset", "child": "child", "provenance": "provenance"}
PARENT_LINKS = ("parent", "branched_from", "compressed_from", "delegate_from", "reset_from", "child")
# i22's family links (later.session_families, identity.family_sessions): the links that copy rows. The kinds option C
# adds (a delegate, a reset, a markerless child, provenance) never widen those families (their parent side is pending).
FAMILY_LINKS = ("parent", "branched_from", "compressed_from", "shared_uid", "copied_rows")
MARKER_LINKS = {"_branched_from": "branched_from", "_compressed_from": "compressed_from",
                "_delegate_from": "delegate_from", "_reset_from": "reset_from"}
# A hold an admin kept that was held only with its conversation (rebuild R2, RECON-16): it says nothing of the withheld
# message, so it never seeds a group (a seed would make an uncertain conversation a member, and spread as one)
GROUP_VIAS = frozenset({identity.GROUP_HOLD, identity.UNCERTAIN_HOLD})
# Holds whose evidence was computed on one backup's content (their stone's decided_on): a release covers that content
RELEASE_ON_EVIDENCE = frozenset({identity.GROUP_HOLD, identity.UNCERTAIN_HOLD, identity.COPY_HOLD,
                                 identity.CARRIER_HOLD})
J8_MIN_ROWS = 2  # copied rows that make an unanchored conversation a member (fewer: uncertain; J9 provenance: one)
UNCERTAIN_HOLD = identity.UNCERTAIN_HOLD


@dataclass(frozen=True)
class Member:
    how: str
    state: str = MEMBER
    via: str = ""  # the group conversation it joined from ('' for a conversation withheld here)

    @property
    def uncertain(self) -> bool:
        return self.state == UNCERTAIN


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    how: str
    uncertain: bool = False
    copied: bool = False  # by copied-history evidence (J8, J9): the child's own parent doubt does not apply


@dataclass
class Node:
    sid: str
    parent: str = ""
    lineage: str = ""
    orphan: bool = False  # Hermes stored no parent (NULL): the parent read comes from a marker only
    conflict: bool = False
    branch_marker: bool = False
    started_at: datetime | None = None
    claims: set[str] = field(default_factory=set)  # every parent any source names


Row = tuple[str, str, Any, str, tuple[str, ...], bool]  # role, call, time, uid, absorbed uids, copy position


class Graph:
    """The conversations this database and the backup in hand know, and the links between them."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.known: set[str] = set()
        self.edges: list[Edge] = []
        self.adj: dict[str, list[Edge]] = defaultdict(list)
        self.rows: dict[str, list[Row]] = defaultdict(list)
        self.uids: dict[str, set[str]] = defaultdict(set)
        self._j8 = False

    def node(self, sid: str) -> Node:
        sid = sid[:200]
        if sid not in self.nodes:
            self.nodes[sid] = Node(sid)
        return self.nodes[sid]

    def add_edge(self, parent: str, child: str, how: str, *, uncertain: bool = False, copied: bool = False) -> None:
        parent, child = parent[:200], child[:200]
        if not parent or not child or parent == child:
            return
        e = Edge(parent, child, how, uncertain, copied or how in COPIED)
        self.edges.append(e)
        self.adj[parent].append(e)
        self.adj[child].append(e)

    def add_row(self, sid: str, row: Row) -> None:
        self.rows[sid[:200]].append(row)
        for u in (row[3], *row[4]):
            if u:
                self.uids[u].add(sid[:200])

    # The doubts ------------------------------------------------------------------------------------------------

    def unresolved(self, sid: str) -> bool:
        """Its parent names a conversation neither the backup nor this database holds."""
        n = self.nodes.get(sid)
        return bool(n is not None and n.parent and n.parent not in self.known)

    def conflicting(self, sid: str) -> bool:
        """Two parents: a marker naming another conversation, or the sources name different parents."""
        n = self.nodes.get(sid)
        return bool(n is not None and (n.conflict or len({c for c in n.claims if c}) > 1))

    def _joined_past_its_parent(self, sid: str, e: Edge) -> bool:
        """It joins as a child through a lineage link from a conversation that is not its parent, while its parent
        resolves nowhere here (Hermes re-linked it, or the sources disagree): uncertain. A copied-history link (J8,
        J9) is about exactly such conversations, and a join from its own child or its own parent is certain."""
        n = self.nodes.get(sid)
        return bool(not e.copied and e.child == sid and n is not None and e.parent != n.parent
                    and self.unresolved(sid))

    def unanchored(self, sid: str) -> bool:
        """No parent anchors it, so its copied history decides (J8): none, one that resolves nowhere here, or (an
        orphan) a parent read only from a delegate marker (round 3 skeptic groups-paths N1, N2: an inherited
        _delegate_from, Hermes v16's '__orphaned__')."""
        n = self.nodes.get(sid)
        if n is None:
            return False
        return not n.parent or n.parent not in self.known or (n.orphan and n.lineage == "delegated")

    # J8 and J9 ------------------------------------------------------------------------------------------------

    def copied_history(self) -> None:
        """The copied-history links (J8, J9) of every unanchored conversation, once: each conversation with rows at
        the very role, tool call and time of its rows at or before its start. A row that is itself a copy in another
        unanchored conversation (two copies of one origin) is weaker evidence: such a link is uncertain."""
        if self._j8:
            return
        self._j8 = True
        unanchored = {sid for sid in self.nodes if self.unanchored(sid)}
        index: dict[tuple[str, str, Any], dict[str, bool]] = defaultdict(dict)
        for sid, rows in self.rows.items():
            for role, call, at, _, _, copy_position in rows:
                if at is not None:
                    key = (role, call, at)
                    weak = copy_position and sid in unanchored
                    index[key][sid] = index[key].get(sid, True) and weak
        for sid in sorted(unanchored):
            n = self.nodes[sid]
            strong: Counter[str] = Counter()
            weak: Counter[str] = Counter()
            copied = {(role, call, at) for role, call, at, _, _, copy_position in self.rows.get(sid, ())
                      if copy_position and at is not None}  # distinct rows (the archive and the backup hold one twice)
            for key in copied:
                for other, is_copy in index.get(key, {}).items():
                    if other != sid:
                        (weak if is_copy else strong)[other] += 1
            for other in sorted(set(strong) | set(weak)):
                count = strong.get(other, 0)
                if n.lineage == "reset":  # a reset copies no rows: copied-looking rows are no proof (J4, J8)
                    self.add_edge(other, sid, "copied_rows", uncertain=True, copied=True)
                elif n.lineage == "branch" or n.branch_marker:  # J9: an orphaned branch's provenance
                    self.add_edge(other, sid, "provenance", uncertain=not count, copied=True)
                else:
                    self.add_edge(other, sid, "copied_rows", uncertain=count < J8_MIN_ROWS, copied=True)

    # The closure ------------------------------------------------------------------------------------------------

    def _steps(self, g: str, extra: dict[str, list[Edge]]) -> Iterable[tuple[str, Edge]]:
        """The conversations a group conversation reaches in one link: down every link, up only a link that copies
        rows (O-D1: a delegate and a markerless child join from the parent down only)."""
        for e in (*self.adj.get(g, ()), *extra.get(g, ())):
            if e.parent == g:
                yield e.child, e
            elif e.how in BOTH_WAYS:
                yield e.parent, e

    def closure(self, seeds: Iterable[str], uids: Iterable[str] = ()) -> dict[str, Member]:
        """The group of these seed conversations (J1-J9, O-D1, uncertain). A conversation is a member when a path of
        certain links reaches it and its own parentage is not in doubt; everything else the links reach is uncertain
        (an uncertain conversation's relatives too: it spreads)."""
        self.copied_history()
        out: dict[str, Member] = {s[:200]: Member(WITHHELD_HERE) for s in seeds if s}
        if not out:
            return out
        extra: dict[str, list[Edge]] = defaultdict(list)
        anchor_sid = sorted(out)[0]
        for u in sorted({u for u in uids if u}):
            for sid in sorted(self.uids.get(u, ())):
                if sid not in out:
                    e = Edge(anchor_sid, sid, "shared_uid", False, True)
                    extra[anchor_sid].append(e)
                    extra[sid].append(e)
        work = sorted(out)
        while work:  # certain links
            g = work.pop()
            for other, e in self._steps(g, extra):
                if other in out or e.uncertain or self.conflicting(other) or self._joined_past_its_parent(other, e):
                    continue
                out[other] = Member(e.how, MEMBER, g)
                work.append(other)
        work = sorted(out)
        while work:  # what else they reach: uncertain
            g = work.pop()
            for other, e in self._steps(g, extra):
                if other not in out:
                    out[other] = Member(e.how, UNCERTAIN, g)
                    work.append(other)
        return out


async def build(s: AsyncSession, backup: src.Backup | None = None) -> Graph:
    """The graph from the archive (every stored conversation's lineage, parent and rows' times and uids), the
    recorded links and, when given, the JARVIS backup in hand (its sessions, parents, markers and rows)."""
    g = Graph()
    for sid, parent, lineage, started, flags in (await s.execute(select(
            ArchiveSession.source_session_id, ArchiveSession.parent_source_session_id, ArchiveSession.lineage,
            ArchiveSession.started_at, ArchiveSession.flags))).all():
        n = g.node(sid)
        g.known.add(n.sid)
        n.parent, n.lineage, n.started_at = (parent or "")[:200], lineage or "", started
        flags = flags or {}
        n.orphan = bool(flags.get("orphan"))
        n.conflict = bool(flags.get("two_parents"))
        if n.parent:
            n.claims.add(n.parent)
            g.add_edge(n.parent, n.sid, LINEAGE_HOW.get(n.lineage, "child"))
    for a, b, how in (await s.execute(select(BrainSessionLink.a, BrainSessionLink.b, BrainSessionLink.how))).all():
        if how not in LINK_HOW:
            continue
        g.add_edge(a, b, LINK_HOW[how])
        if how in PARENT_LINKS:
            g.node(b).claims.add(a[:200])
    started = {sid: n.started_at for sid, n in g.nodes.items()}
    for sid, role, call, at, uid, absorbed in (await s.execute(select(
            ArchiveSession.source_session_id, ArchiveMessage.role, ArchiveMessage.tool_call_id, ArchiveMessage.at,
            ArchiveMessage.source_uid, ArchiveMessage.absorbed_uids)
            .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id))).all():
        start = started.get(sid[:200])
        g.add_row(sid, ((role or "")[:40], (call or "")[:200], at, (uid or "")[:200],
                        tuple(u[:200] for u in (absorbed or ())),
                        at is not None and start is not None and at <= start))
    if backup is not None:
        for ss in backup.sessions():
            n = g.node(ss.id)
            g.known.add(n.sid)
            n.parent, n.lineage, n.started_at = ss.parent_id[:200], ss.lineage, ss.started_at
            n.orphan = n.orphan or (not ss.hermes_parent and bool(ss.parent_id))
            n.conflict = n.conflict or ss.conflict
            n.branch_marker = n.branch_marker or bool(ss.markers.get("_branched_from"))
            if n.parent:
                n.claims.add(n.parent)
                g.add_edge(n.parent, n.sid, LINEAGE_HOW.get(ss.lineage, "child"))
            for marker, value in sorted(ss.markers.items()):  # every marker names a conversation it came from
                if value and value[:200] != n.parent:
                    g.add_edge(value, n.sid, MARKER_HOW[marker])
            for m in backup.messages(ss.id):
                g.add_row(n.sid, (brain_text.sanitize(m.role)[:40], brain_text.sanitize(m.tool_call_id)[:200], m.at,
                                  m.uid[:200], tuple(u[:200] for u in m.absorbed),
                                  m.at is not None and ss.started_at is not None and m.at <= ss.started_at))
    return g


def seeds(ids: identity.Identities) -> dict[str, set[str]]:
    """Each withhold's seeds (its anchor -> conversations): the conversations its certain stones are bound to (the
    admin's own, its members, a kept hold). A pending hold never seeds a group (its row is only held for review)."""
    out: dict[str, set[str]] = defaultdict(set)
    for st in ids.by_key.values():
        if st.session and st.certain and st.group in ids.by_key and st.via not in GROUP_VIAS:
            out[st.group].add(st.session[:200])
    return dict(out)


def certain_uids(ids: identity.Identities) -> dict[str, set[str]]:
    """Each withhold's certain uids (a row carrying or absorbing one joins its group: J9 'shared_uid')."""
    out: dict[str, set[str]] = defaultdict(set)
    for st in ids.by_key.values():
        if st.uid and st.certain and st.group in ids.by_key and st.via not in GROUP_VIAS:
            out[st.group].add(st.uid[:200])
    return dict(out)


async def recorded(s: AsyncSession, anchors: Iterable[str] | None = None) -> dict[str, dict[str, Member]]:
    """The recorded groups (anchor -> conversation -> how it joined)."""
    q = select(BrainWithheldSession)
    if anchors is not None:
        q = q.where(BrainWithheldSession.anchor_key.in_(sorted(set(anchors))))
    out: dict[str, dict[str, Member]] = defaultdict(dict)
    for w in (await s.scalars(q)).all():
        out[w.anchor_key][w.source_session_id] = Member(w.how, w.state, w.via_session or "")
    return dict(out)


def merged(found: dict[str, Member], stored: dict[str, Member]) -> dict[str, Member]:
    """A recorded group with what a closure found now: nothing is forgotten, and uncertain wins."""
    out = dict(stored)
    for sid, m in found.items():
        was = out.get(sid)
        if was is None:
            out[sid] = m
        elif m.uncertain and not was.uncertain and was.how != WITHHELD_HERE:
            out[sid] = Member(was.how, UNCERTAIN, was.via)
    return out


async def record(s: AsyncSession, anchor: str, members: dict[str, Member], *, run_id: int | None = None) -> int:
    """Records a group's conversations (never one forgotten; an uncertain one stays uncertain: the table's trigger).
    How many rows were added or became uncertain."""
    n = 0
    for sid, m in sorted(members.items()):
        stmt = pg_insert(BrainWithheldSession).values(
            anchor_key=anchor[:400], source_session_id=sid[:200], how=m.how, state=m.state,
            via_session=(m.via or "")[:200], first_run=run_id)
        if m.uncertain:
            stmt = stmt.on_conflict_do_update(index_elements=["anchor_key", "source_session_id"],
                                              set_={"state": UNCERTAIN},
                                              where=BrainWithheldSession.state != UNCERTAIN)
        else:
            stmt = stmt.on_conflict_do_nothing()
        n += (await s.execute(stmt)).rowcount or 0
    return n


async def refresh(s: AsyncSession, *, backup: src.Backup | None = None, run_id: int | None = None,
                  ids: identity.Identities | None = None, anchors: Iterable[str] | None = None, write: bool = True
                  ) -> dict[str, dict[str, Member]]:
    """Every withhold's group (or those of `anchors`): the closure from its seeds over what this database and the
    backup in hand know, merged with what was recorded; recorded when `write`. Anchor -> conversation -> Member."""
    ids = ids if ids is not None else await identity.Identities.load(s)
    roots = seeds(ids)
    if anchors is not None:
        want = set(anchors)
        roots = {a: v for a, v in roots.items() if a in want}
    stored = await recorded(s, roots.keys() if anchors is not None else None)
    if not roots and not stored:
        return {}
    graph = await build(s, backup)
    uids = certain_uids(ids)
    out: dict[str, dict[str, Member]] = {}
    for anchor in sorted(set(roots) | set(stored)):
        found = graph.closure(sorted(roots.get(anchor, ())), uids.get(anchor, ()))
        out[anchor] = merged(found, stored.get(anchor, {}))
        if write:
            await record(s, anchor, out[anchor], run_id=run_id)
    return out


async def closure_for(s: AsyncSession, seed_sessions: Iterable[str], uids: Iterable[str] = ()) -> dict[str, Member]:
    """The group these conversations would have (read only, over what this database knows): the withhold, before
    its stones are written, takes its decision on the newest backup that wrote the group's certain conversations."""
    graph = await build(s)
    return graph.closure(sorted({x[:200] for x in seed_sessions if x}), uids)


def sessions_of(groups: dict[str, dict[str, Member]], *, definite: bool = False) -> dict[str, set[str]]:
    """Conversation -> the anchors whose group holds it (definite: members only, never an uncertain one)."""
    out: dict[str, set[str]] = defaultdict(set)
    for anchor, members in groups.items():
        for sid, m in members.items():
            if not (definite and m.uncertain):
                out[sid].add(anchor)
    return dict(out)


async def definite_sessions(s: AsyncSession, anchor: str, extra: Iterable[str] = ()) -> list[str]:
    """The conversations of a group whose link is certain (an uncertain member never moves the decision to another
    backup: identity.decision_backup)."""
    found = (await recorded(s, [anchor])).get(anchor, {})
    return sorted({sid for sid, m in found.items() if not m.uncertain} | {x for x in extra if x})


async def uncertain_sessions(s: AsyncSession) -> set[str]:
    """Every conversation an uncertain link holds, in any group."""
    return set((await s.scalars(select(BrainWithheldSession.source_session_id).where(
        BrainWithheldSession.state == UNCERTAIN))).all())


async def hide_titles(s: AsyncSession, source_session_ids: Iterable[str], *, anchor: str, reason: str) -> int:
    """Conversations whose title an admin's withhold hid (outside the group too): kept for good, so the title trigger
    hides them again after a rollback, a restore or a re-import."""
    n = 0
    for sid in sorted({x[:200] for x in source_session_ids if x}):
        n += (await s.execute(pg_insert(BrainHiddenTitle).values(
            source_session_id=sid, anchor_key=anchor[:400], reason=reason[:200]).on_conflict_do_nothing())).rowcount or 0
    return n


async def hold_uncertain(s: AsyncSession, *, actor: str, cause: str) -> int:
    """Uncertain rows are held (O-D1): every readable row of a conversation an uncertain link puts in a group is held
    for an admin's review in that group ('uncertain'), unless an admin released that exact row for that group. Never
    released in one click (review.release_unrelated), released per row only with the typed word, and the export waits
    while one waits (brain_withhold_pending: uncertain_waiting). How many rows were held."""
    from . import review

    found = (await s.execute(
        select(ArchiveMessage, ArchiveSession.source_session_id, BrainWithheldSession.anchor_key)
        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .join(BrainWithheldSession, BrainWithheldSession.source_session_id == ArchiveSession.source_session_id)
        .where(BrainWithheldSession.state == UNCERTAIN, ArchiveMessage.withheld.is_(False))
        .order_by(ArchiveMessage.id, BrainWithheldSession.anchor_key))).all()
    done: set[int] = set()
    for row, sid, anchor in found:
        if row.id in done or row.withheld:
            continue
        if await _released(s, anchor, sid, row):
            continue
        await review.hold_message(s, row, anchor=anchor, actor=actor, cause=cause, rule=UNCERTAIN_HOLD,
                                  relations=[identity.UNCERTAIN_REL],
                                  decided_on=await identity.stored_backup(s, row))  # RECON-20
        done.add(row.id)
    return len(done)


async def hold_group(s: AsyncSession, anchor: str, members: dict[str, Member], *, texts: Mapping[str, str],
                     all_in_hand: bool, actor: str) -> list[ArchiveMessage]:
    """The central sweep for one group (option C, rebuild R2): every readable stored row of a conversation the group
    holds by a certain link is held with its conversation (a pending stone in this group, GROUP_HOLD, and the row
    without its text), unless an admin's release (or 'left readable') for this group covers that exact row and its
    content (identity.exempt_covers). A row of two groups stays held until each releases it. Its evidence:
    identity.group_relations against the group's own withheld text and every other one in hand (texts: anchor -> text,
    in memory only); 'none' only when every withheld text was in hand (all_in_hand). The conversations an uncertain
    link holds are hold_uncertain's. The caller empties what copied the rows (review.hold_group_rows). The rows held."""
    from . import review

    sids = sorted(sid for sid, m in members.items() if not m.uncertain)
    if not sids:
        await list_unlisted(s, anchor, members, actor=actor)
        return []
    own = await s.get(BrainTombstone, anchor)
    w_text = texts.get(anchor)
    others = [t for a, t in sorted(texts.items()) if a != anchor and t is not None]
    after = await identity.current_after_run(s)
    at = datetime.now(timezone.utc).isoformat()
    held: list[ArchiveMessage] = []
    for row, sid in (await s.execute(
            select(ArchiveMessage, ArchiveSession.source_session_id)
            .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .where(ArchiveSession.source_session_id.in_(sids), ArchiveMessage.withheld.is_(False))
            .order_by(ArchiveMessage.id).with_for_update(of=ArchiveMessage))).all():
        if row.withheld or (row.source_key and row.source_key == anchor):
            continue
        if await identity.exempt_covers(s, anchor, sid, row):
            # an admin released (or left readable) this exact row for this group; N-B (rebuild R4): it is compared
            # with every other withheld text in hand, and held for the first one it carries (that group's release)
            fields = identity.row_fields(row)
            hit: tuple[str, tuple[str, ...]] | None = None
            for other, t in sorted(texts.items()):
                if other == anchor or t is None:
                    continue
                rel = identity.carrier_relations(fields, t) | (
                    identity.family_relations(fields, t) & set(identity.TEXT_RELATIONS))
                if rel and not await identity.exempt_covers(s, other, sid, row):
                    hit = (other, tuple(identity.relation_order(rel)))
                    break
            if hit is None:
                continue
            if row.source_key:
                await identity.put_stone(s, replace(identity.binding_of(
                    row, sid, anchor=hit[0] if hit[0] != row.source_key else "", via=identity.CARRIER_HOLD,
                    review=identity.PENDING, after_run=after,
                    decided_on=await identity.stored_backup(s, row)), relations=hit[1]), actor=actor)
            review._blank(row, {"at": at, "note": identity.NOTE, "via": identity.CARRIER_HOLD,
                                "review": identity.PENDING})
            held.append(row)
            continue
        unchecked = await identity.unchecked_covers(s, anchor, sid, row)
        structural: set[str] = set()
        if own is not None:
            uid = identity.row_uid(row)
            if uid and own.message_uid and uid == own.message_uid and own.via not in identity.FOLD_VIAS:
                structural.add(identity.SAME_UID)
            if row.tool_call_id and own.tool_call_id and row.tool_call_id == own.tool_call_id:
                structural.add(identity.SAME_CALL)
            if own.source_at is not None and row.at == own.source_at and row.role == own.role:
                structural.add(identity.SAME_TIME_REL)
        relations = identity.group_relations(identity.row_fields(row), w_text, others, all_in_hand=all_in_hand,
                                             structural=structural)
        if unchecked and all_in_hand and list(relations) == [identity.NO_RELATION]:
            # rebuild R4 (B6, 6725): an untrusted release of this exact row (an older file's) is checked here, every
            # withheld text in hand: only the release decided on the backup this content came from is marked
            if await identity.mark_exemptions(s, anchor, identity.exact_of_row(row, sid),
                                              decided_on=await identity.stored_backup(s, row),
                                              kinds={b.kind for b in unchecked}):
                continue
        if row.source_key:
            # decided on the backup the compared content came from (RECON-20): a release of this hold covers that
            # content only, never a later backup's other text for this row (a fold JARVIS made in place)
            sha = await identity.stored_backup(s, row) or (own.decided_on if own is not None else "")
            await identity.put_stone(s, replace(identity.binding_of(
                row, sid, anchor=anchor, via=identity.GROUP_HOLD, review=identity.PENDING, after_run=after,
                decided_on=sha), relations=relations), actor=actor)
        review._blank(row, {"at": at, "note": identity.NOTE, "via": identity.GROUP_HOLD, "review": identity.PENDING})
        held.append(row)
    await s.flush()
    await list_unlisted(s, anchor, members, actor=actor)
    return held


async def list_unlisted(s: AsyncSession, anchor: str, members: dict[str, Member], *, actor: str) -> int:
    """A row of a group conversation stored without text and without any stone (a rollback put back a copy the sweep
    blanked, older code withheld it, a restore of an older dump) is listed as the hold it is: a pending stone in the
    group ('not in this backup': its text is not here to compare), never an unlisted withheld row an admin cannot
    review. A row withheld for restricted data in its conversation is not (it stays withheld as it is). How many."""
    sids = sorted(members)
    if not sids:
        return 0
    n = 0
    after = await identity.current_after_run(s)
    for row, sid in (await s.execute(
            select(ArchiveMessage, ArchiveSession.source_session_id)
            .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .where(ArchiveSession.source_session_id.in_(sids), ArchiveMessage.withheld.is_(True),
                   ArchiveMessage.source_key != "", ArchiveMessage.source_key != anchor,
                   ArchiveSession.sensitivity != "restricted", ~ArchiveMessage.flags.has_key("withheld"),
                   ~select(BrainTombstone.source_key).where(
                       BrainTombstone.source_key == ArchiveMessage.source_key).exists())
            .order_by(ArchiveMessage.id))).all():
        uncertain = members[sid].uncertain
        waits, seen_on = await _release_waits(s, anchor, sid, row, uncertain=uncertain)
        if waits:
            continue  # an admin released this exact row for this group: it waits for its text (RECON-52)
        relations = (identity.UNCERTAIN_REL,) if uncertain else (identity.NOT_IN_BACKUP,)
        # decided on the newest backup that held this row (S2, RECON-55; it was '': a release then took the newest
        # backup of the conversation, which may not hold the row, and covered a content no one had seen)
        await identity.put_stone(s, replace(identity.binding_of(
            row, sid, anchor=anchor, via=identity.UNCERTAIN_HOLD if uncertain else identity.GROUP_HOLD,
            review=identity.PENDING, after_run=after,
            decided_on=seen_on or await identity.newest_holding_backup(s, row)), relations=relations), actor=actor)
        flag = dict((row.flags or {}).get("withheld_by_admin") or {})
        flag.update(note=identity.NOTE, via=identity.UNCERTAIN_HOLD if uncertain else identity.GROUP_HOLD,
                    review=identity.PENDING)
        row.flags = {**(row.flags or {}), "withheld_by_admin": flag}
        n += 1
    await s.flush()
    return n


async def _release_waits(s: AsyncSession, anchor: str, sid: str, row: ArchiveMessage, *, uncertain: bool = False
                         ) -> tuple[bool, str]:
    """Stabilisation S1 (RECON-52): a withheld row an admin released for this group (its flag says so, and a trusted
    release of this exact JARVIS row for this group exists) is not an unlisted row: the admin decided it, and it
    stays without its text until an import of a backup that holds the content the release covers brings it back
    (the release branch; the sweep after that import compares it with every withheld text in hand, N-B). It is
    not listed again as a hold of its own ('not in this backup', which asked the admin again and kept the export
    and the one-click release waiting) until an import run after the release left it without its text (that backup
    held other content than the release covers, or not the row: listed again, decided on the newest such run's
    backup, so an import of an older backup never ends it: RECON-54). (waits, that backup or '')."""
    flag = (row.flags or {}).get("withheld_by_admin") or {}
    if not row.withheld or not flag.get("released_at"):
        return False, ""
    seen_on = ""
    here = identity.exact_of_row(row, sid)
    for e in (await s.scalars(select(BrainWithholdExemption).where(
            BrainWithholdExemption.anchor_key == anchor, BrainWithholdExemption.source_session_id == here.session,
            BrainWithholdExemption.source_row_id == here.row, BrainWithholdExemption.kind == "release"))).all():
        b = identity.Binding(e.role or "", e.tool_call_id or "", e.source_at, e.source_uid or "", e.decided_on or "",
                             bool(e.trusted))
        if not (b.covers(here) and b.trusted):
            continue
        # still waiting only until the next import run: one that did not bring its text back held other content than
        # the release covers, or not the row (RECON-20), and the row is listed again for the admin
        since = e.decided_at or identity.when(flag.get("released_at"))
        if since is None:
            continue  # when it was released is not known: listed again (as before)
        seen = (await s.execute(select(ImportRun.status, ImportRun.source).where(
            ImportRun.status != "rolled_back", ImportRun.started_at > since)
            .order_by(ImportRun.id.desc()).limit(1))).first()
        if seen is None:
            return True, ""
        if uncertain and not await _held_since(s, row, since):
            # S3 (RECON-59): a row of an uncertain conversation the admin released one by one with the typed word
            # (O-D1, B6) waits for its text while no import run since the release held the row (no backup since
            # held it: JARVIS dropped, compacted or renumbered it, or the run has read nothing yet). Listed again it
            # asked the admin a second time for a row they decided, with nothing new to compare, and kept the export
            # waiting for good (13075, 13159). It stays withheld meanwhile; a backup holding it again lists it as
            # before (below) when its text does not come back.
            return True, ""
        # S2 (RECON-55): listed again, decided on the newest backup whose import held this stored row (it recorded
        # the row), never on a backup that does not hold it (a stale stored row JARVIS dropped, compacted or
        # renumbered since: a release of that hold covered a content no one compared, and no import of that backup
        # could bring its text back). The running import's own start-up sweep (it has read nothing yet) decides it
        # on that import's backup, fail-closed (RECON-54), and the import decides it again at its end when its backup
        # did not hold the row (Processor.redecide_unheld)
        seen_on = seen_on or (identity.run_sha256(seen[1]) if seen[0] == "running"
                              else await identity.newest_holding_backup(s, row))
    return False, seen_on


async def _held_since(s: AsyncSession, row: ArchiveMessage, since: Any) -> bool:
    """Whether an import run started after `since` (not rolled back) held this stored row (recorded it: S2's
    identity.run_holds_row, RECON-55)."""
    return bool(await s.scalar(select(ImportRecord.id).join(ImportRun, ImportRun.id == ImportRecord.run_id).where(
        ImportRun.status != "rolled_back", ImportRun.started_at > since,
        ImportRecord.target_table == "archive_messages", ImportRecord.target_id == row.id).limit(1)))


async def _released(s: AsyncSession, anchor: str, sid: str, row: ArchiveMessage) -> bool:
    """An admin released this exact JARVIS row for this group (a release, not identical text left readable)."""
    here = identity.exact_of_row(row, sid)
    for e in (await s.scalars(select(BrainWithholdExemption).where(
            BrainWithholdExemption.anchor_key == anchor, BrainWithholdExemption.source_session_id == here.session,
            BrainWithholdExemption.source_row_id == here.row, BrainWithholdExemption.kind == "release"))).all():
        b = identity.Binding(e.role or "", e.tool_call_id or "", e.source_at, e.source_uid or "", e.decided_on or "",
                             bool(e.trusted))
        # rebuild R4 (F1): a trusted release that covers the stored content (an uncertain row is released per row
        # with the typed word)
        if b.covers(here) and b.trusted and await identity.releases_covering_stored(s, [b], row):
            return True
    return False


async def in_uncertain(s: AsyncSession, archive_session_id: int) -> bool:
    """Whether an uncertain link holds this archive conversation (in any group)."""
    return bool(await s.scalar(sql(
        "SELECT EXISTS (SELECT 1 FROM brain_withheld_sessions w JOIN archive_sessions s "
        "ON s.source_session_id = w.source_session_id WHERE s.id = :id AND w.state = 'uncertain')"),
        {"id": archive_session_id}))

