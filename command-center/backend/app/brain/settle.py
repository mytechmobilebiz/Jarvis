"""Finishing decisions, "Settle" (0.6.2 plan 4; RECOMMENDATION.md section 4, step 4): the decision backup decided once.

Inside one JARVIS backup Hermes's whole history is visible at once, and the withheld text is in memory (the backup's
own row of it). Settle reads every session of the conversation family of each unfinished decision (importer.settle()
imports them: also sessions a -Sample run skipped) and decides, by provenance only, which rows are the withheld message
for good (plan 4.2, "certain", and only these):
  (1) the exact row (identity.exact: session, row id, role, tool_call_id, time, and the uid when both have one);
  (2) the same non-empty uid, role and normalised text as a certain row;
  (3) a row whose absorbed uids include a certain row's (or certain stone's) uid; a fold's own uid is the survivor's
      message, so fold rows are never uid sources for further matches (review 3, rv3-res-6);
  (4) a tool row with the same non-empty tool_call_id and the same text as a certain tool row, inside the family;
  (5) the importer's own duplicate or carried link when both rows carry the same non-empty uid (importer.Processor:
      settle_rows);
  (6) copy position: a row of a continuation or branch child, at or before the child's start, whose time and text
      equal exactly one row of the parent (active or not; for a continuation, the only parent row with that text:
      Hermes's donor timestamps give a carried row the time of the FIRST parent row with the same content), that
      parent row being certain, with no uid conflict (the copy carries the uid of another row of the parent: a
      uid-keeping copy of that row). And in reverse: the unique same-time origin of a certain copy is certain (S14).
Every other row of the family that relates to a withheld text is held for review with its computed evidence (plan 4.3,
4.6: identity.text_relations after Hermes's wrappers are peeled, and the structural relations: the same uid, tool
call or time with other text, a reused row number); nothing else is held. There is no independence guard, no time
rule, no 'admin saw it' guard, no text-only link and no lost origin (plan 4.3). A hold that looks independent (its own
uid, another time, no copy position) is pre-selected for release; the admin confirms.

Pure: this module reads the backup and the stones in memory and returns outcomes; the importer writes them."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import identity
from . import source as src
from . import text as brain_text

RULE_VIA = {
    1: "same JARVIS row",
    2: "same message id",
    3: identity.FOLD,
    4: "same tool call",
    6: "copied by JARVIS",
    7: "the original JARVIS copied",
}


@dataclass
class Row:
    """A row of the backup in the family (every state: live, archived, rewound)."""
    sid: str
    m: src.SourceMessage
    exact: identity.Exact
    tk: tuple[str, str, str]  # identity.text_key: role, call, normalised text and tool calls
    copy_pos: bool  # a continuation or branch child's copied history: at or before the child's start
    parent: str  # the session's parent ('' none)
    lineage: str

    @property
    def key(self) -> str:
        return self.m.key

    @property
    def uid(self) -> str:
        return self.exact.uid

    @property
    def role(self) -> str:
        return self.exact.role

    @property
    def call(self) -> str:
        return self.exact.tool_call_id

    @property
    def at(self) -> datetime | None:
        return self.exact.at


@dataclass
class Outcome:
    """What Settle decided for one row key of the family."""
    group: str
    review: str  # identity.CERTAIN or identity.PENDING
    via: str
    relations: tuple[str, ...] = ()
    preselect: bool = False  # a hold that looks independent: pre-selected for release (plan 4.6)


@dataclass
class Decision:
    """One unfinished decision (an admin's own withhold and its group) as this backup shows it."""
    group: str
    w: Row | None = None  # the withheld row itself in this backup (None: not in this backup)
    certain: dict[str, int] = field(default_factory=dict)  # row key -> rule number
    uids: set[str] = field(default_factory=set)  # the uids of the same message (rules 1, 2; never a fold's)
    calls: set[str] = field(default_factory=set)

    @property
    def w_text(self) -> str | None:
        return self.w.m.content if self.w is not None else None


class Engine:
    """The decisions of one backup, decided once (plan 4.2, 4.3). sessions: every session of the backup by id;
    family: the JARVIS sessions of the decisions' families; groups: the admin's own stones (their keys) to decide;
    stored(m): whether a backup row is stored as a row of its own (not excluded, not a duplicate)."""

    def __init__(self, backup: src.Backup, sessions: dict[str, src.SourceSession], ids: identity.Identities,
                 groups: Iterable[str], family: Iterable[str], stored: Callable[[src.SourceMessage], bool],
                 families: dict[str, Iterable[str]] | None = None) -> None:
        self.backup = backup
        self.sessions = sessions
        self.ids = ids
        self.groups = sorted(set(groups))
        self.family = sorted({sid for sid in family if sid in sessions})
        self.stored = stored
        self.rows: list[Row] = []
        self.by_key: dict[str, Row] = {}
        self.by_sid: dict[str, list[Row]] = {}
        self.by_row: dict[tuple[str, str], list[Row]] = {}
        self.by_text: dict[tuple[str, tuple[str, str, str]], list[Row]] = {}  # (session, text) -> rows
        self.uids_of: dict[str, dict[str, int]] = {}  # session -> uid -> how many rows carry it
        self.decisions: dict[str, Decision] = {}
        self.outcomes: dict[str, Outcome] = {}
        self.children: dict[str, list[str]] = {}
        # each decision's own family: its rules and holds never reach another decision's conversations (plan 4.3:
        # "every other row of the family"); without it (an older run's scope) the whole scope
        self.families = {g: set(v) for g, v in (families or {}).items()}

    def rows_of(self, group: str) -> list[Row]:
        """The backup rows of a decision's own family."""
        fam = self.families.get(group)
        return self.rows if fam is None else [r for r in self.rows if r.sid in fam]

    # Reading the backup ---------------------------------------------------------------------------------------------

    def _load(self) -> None:
        for sid in self.family:
            ss = self.sessions[sid]
            for m in self.backup.messages(sid):
                here = identity.exact_of_source(sid, m)
                copy_pos = (ss.lineage in identity.LINEAGE and bool(ss.parent_id) and m.at is not None
                            and ss.started_at is not None and m.at <= ss.started_at)
                r = Row(sid, m, here, identity.text_key(m), copy_pos, ss.parent_id or "", ss.lineage)
                self.rows.append(r)
                self.by_key.setdefault(m.key, r)
                self.by_sid.setdefault(sid, []).append(r)
                self.by_row.setdefault((here.session, here.row), []).append(r)
                self.by_text.setdefault((sid, r.tk), []).append(r)
                if here.uid:
                    counts = self.uids_of.setdefault(sid, {})
                    counts[here.uid] = counts.get(here.uid, 0) + 1
            if ss.lineage in identity.LINEAGE and ss.parent_id:
                self.children.setdefault(ss.parent_id, []).append(sid)

    def _rows_of_stone(self, st: identity.Stone) -> list[Row]:
        """Rule (1): the backup rows a stone names: its key (an unbound stone, or bound to that very row) or the exact
        JARVIS row it is bound to."""
        out: dict[str, Row] = {}
        r = self.by_key.get(st.key)
        if r is not None and (not st.row or not st.session or (st.session, st.row) == (r.sid[:200], r.exact.row)):
            out[r.key] = r
        if st.session and st.row:
            for r in self.by_row.get((st.session, st.row), ()):
                if identity.same_row(identity.exact_of_stone(st), r.exact):
                    out[r.key] = r
        return list(out.values())

    # Plan 4.2: certain, to a fixed point --------------------------------------------------------------------------

    def _unique_origin(self, child: Row) -> Row | None:
        """Rule (6): the one parent row a copy-position row copies (same text, same time), or None. For a continuation
        the text must also be unique in the parent (a donor timestamp gives the copy of ANOTHER row with that text the
        first one's time); a copy carrying the uid of another row of the parent is that row's copy (no origin here)."""
        if not child.copy_pos or child.at is None:
            return None
        same_text = self.by_text.get((child.parent, child.tk), ())
        timed = [o for o in same_text if o.at == child.at]
        if len(timed) != 1 or (child.lineage != "branch" and len(same_text) != 1):
            return None
        origin = timed[0]
        if child.uid and origin.uid and child.uid != origin.uid and self.uids_of.get(child.parent, {}).get(
                child.uid, 0):
            return None  # a uid-keeping copy of another row of the parent
        return origin

    def _decide_certain(self, d: Decision) -> None:
        group_stones = self.ids.groups.get(d.group, ())
        certain_stones = [st for st in group_stones if st.certain]
        own = self.ids.by_key.get(d.group)
        if own is not None:
            found = self._rows_of_stone(own)
            d.w = found[0] if found else None
        for st in certain_stones:  # a fold's own stone names a fold row: never a uid source (rule 3)
            for r in self._rows_of_stone(st):
                d.certain.setdefault(r.key, 3 if st.via in identity.FOLD_VIAS else 1)
        stone_uids = {st.uid for st in certain_stones if st.uid and st.via not in identity.FOLD_VIAS}
        changed = True
        while changed:
            changed = False
            members = [self.by_key[k] for k, rule in d.certain.items() if k in self.by_key]
            sources = [r for r in members if d.certain[r.key] != 3]
            by_uid: dict[str, set[tuple[str, tuple[str, str, str]]]] = {}
            for r in sources:
                if r.uid:
                    by_uid.setdefault(r.uid, set()).add((r.role, r.tk))
            absorbed_sources = stone_uids | {r.uid for r in sources if r.uid}
            tool_calls = {(r.call, r.tk) for r in sources if r.role == "tool" and r.call}
            certain_ids = {id(r) for r in members}

            def add(r: Row, rule: int) -> None:
                nonlocal changed
                if r.key not in d.certain:
                    d.certain[r.key] = rule
                    changed = True
            for r in self.rows_of(d.group):
                if id(r) in certain_ids or r.key in d.certain:
                    continue
                if r.uid and (r.role, r.tk) in by_uid.get(r.uid, ()):
                    add(r, 2)
                elif set(r.m.absorbed) & absorbed_sources:
                    add(r, 3)
                elif r.role == "tool" and r.call and (r.call, r.tk) in tool_calls:
                    add(r, 4)
                else:
                    origin = self._unique_origin(r)
                    if origin is not None and id(origin) in certain_ids:
                        add(r, 6)
            for r in members:  # rule (6) in reverse: the unique same-time origin of a certain copy (S14)
                origin = self._unique_origin(r)
                if origin is not None and origin.key not in d.certain:
                    add(origin, 7)
        d.uids = {r.uid for k, rule in d.certain.items() if rule in (1, 2) and (r := self.by_key.get(k)) is not None
                  and r.uid}
        if own is not None and own.uid:
            d.uids.add(own.uid)
        d.calls = {r.call for k, rule in d.certain.items() if rule in (1, 2)
                   and (r := self.by_key.get(k)) is not None and r.call}
        if own is not None and own.tool_call_id:
            d.calls.add(own.tool_call_id)

    # Plan 4.3, 4.6: held for review, with computed evidence --------------------------------------------------------

    def relations(self, d: Decision, r: Row) -> set[str]:
        """The relations of a family row to the decision's withheld message (identity.RELATIONS codes)."""
        own = self.ids.by_key.get(d.group)
        rel: set[str] = set()
        if d.w is not None:
            rel |= identity.text_relations(r.m.content, d.w_text)
        same_words = bool(rel & {identity.IDENTICAL_REL, identity.RESTATED_REL})
        if r.uid and r.uid in d.uids and not same_words:
            rel.add(identity.SAME_UID)
        if r.call and r.call in d.calls and not same_words:
            rel.add(identity.SAME_CALL)
        w_at = d.w.at if d.w is not None else (own.at if own is not None else None)
        w_role = d.w.role if d.w is not None else (own.role if own is not None else "")
        if w_at is not None and r.at == w_at and r.role == w_role and (d.w is None or r is not d.w):
            rel.add(identity.SAME_TIME_REL)
        if own is not None and own.session and own.row and (r.sid[:200], r.exact.row) == (own.session, own.row) \
                and not identity.same_row(identity.exact_of_stone(own), r.exact):
            rel.add(identity.REUSED_ROW)
        if rel and d.w is None:
            rel.add(identity.NOT_IN_BACKUP)
        return rel

    def preselect(self, d: Decision, r: Row) -> bool:
        """A hold that looks independent (plan 4.6): its own uid, another time than the withheld message's, and not in
        a copy position. Pre-selected for release; the admin confirms."""
        own = self.ids.by_key.get(d.group)
        w_at = d.w.at if d.w is not None else (own.at if own is not None else None)
        return bool(r.uid) and r.uid not in d.uids and (w_at is None or r.at != w_at) and not r.copy_pos

    # The result ----------------------------------------------------------------------------------------------------

    def decide(self) -> dict[str, Outcome]:
        """key -> Outcome for every row of the family Settle withholds: certain (plan 4.2) or held (4.3); a row
        certain for one decision is never also a hold of another."""
        self._load()
        for group in self.groups:
            d = Decision(group)
            self._decide_certain(d)
            self.decisions[group] = d
        for group in self.groups:
            d = self.decisions[group]
            for key, rule in sorted(d.certain.items()):
                if key not in self.outcomes:
                    self.outcomes[key] = Outcome(group, identity.CERTAIN, RULE_VIA.get(rule, "same JARVIS row"))
        for group in self.groups:
            d = self.decisions[group]
            for r in self.rows_of(group):
                if r.key in self.outcomes or not self.stored(r.m):
                    continue
                rel = self.relations(d, r)
                if rel:
                    self.outcomes[r.key] = Outcome(group, identity.PENDING, identity.SETTLE_HOLD,
                                                   tuple(identity.relation_order(rel)), self.preselect(d, r))
        return self.outcomes

    def evidence(self, key: str, group: str, stored: Iterable[str] = ()) -> tuple[tuple[str, ...], bool]:
        """The relations of a row that is already held for this decision (an earlier withhold, a later backup's
        import), computed again with this backup: (relations, pre-selected). When this backup does not hold the
        withheld text, the text relations computed earlier with it in memory (the withhold dialog's) stay: then the
        withheld text is in the held row, so it is not 'not in this backup'."""
        d = self.decisions.get(group)
        r = self.by_key.get(key)
        if d is None or r is None:
            return tuple(identity.relation_order(stored)), False
        rel = self.relations(d, r)
        if d.w is None:
            earlier = set(stored) & set(identity.TEXT_RELATIONS)
            if earlier:
                rel = (rel - {identity.NOT_IN_BACKUP}) | earlier
        if not rel:
            rel = {identity.NOT_IN_BACKUP if d.w is None else identity.NO_RELATION}
        return tuple(identity.relation_order(rel)), self.preselect(d, r)

    def stale_time(self, group: str) -> tuple[str, str, datetime | None, str] | None:
        """(role, tool_call_id, time, JARVIS session) of the decision's withheld row, for the stale stored rows of the
        family at that role and time (plan 4.3); None when not known."""
        d = self.decisions.get(group)
        own = self.ids.by_key.get(group)
        if d is not None and d.w is not None:
            return d.w.role, d.w.call, d.w.at, d.w.sid[:200]
        if own is not None and own.at is not None and own.role:
            return own.role, own.tool_call_id, own.at, own.session
        return None

    def w_text(self, group: str) -> str | None:
        d = self.decisions.get(group)
        return d.w_text if d is not None else None


def stored_relations(text: str | None, w_text: str | None, *, stale: bool) -> tuple[str, ...]:
    """The relations of a stored row the backup no longer holds (a stale row at the withheld row's role and time:
    plan 4.3) to the withheld text, from its stored text while it is readable."""
    rel = identity.text_relations(text, w_text) if w_text is not None else set()
    if stale:
        rel.add(identity.STALE)
    if w_text is None:
        rel.add(identity.NOT_IN_BACKUP)
    return tuple(identity.relation_order(rel))


def sanitized_role(m: Any) -> str:
    return brain_text.sanitize(m.role)[:40]
