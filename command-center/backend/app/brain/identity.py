"""The identity of a withheld JARVIS message (docs/JARVIS_BRAIN_SPEC.md 2.9; migration 0008_brain_withhold_identity).

An admin's withhold is bound to the JARVIS message, not only to the importer's derived source_key. Every part of that
identity is one more brain_tombstones row with reason WITHHELD_BY_ADMIN (a "stone"): its key, the JARVIS row it named
(session id, row id, role, tool_call_id, message_uid), the admin's own stone it belongs to (anchor_key: '' for the
admin's own decision, which is its own group), how it joined (via) and its review state:
- a member ('') is certain: stored without text and never releasable;
- a hold ('pending') is uncertain: stored without text too, and listed for an admin who keeps or releases it;
- 'kept': a hold an admin confirmed, permanent from then on.
Every reader that honours a tombstone by key (the import, a rollback, decisions-export, promote, withheld_source(), the
start-up sweep) therefore honours an inherited stone as well; the database trigger archive_messages_keep_withheld()
keeps any row a stone names without text, whatever code writes it.

No text fingerprint of a withheld message is stored anywhere: a withheld row and its rollback copies keep
source_fp = 'w1:<state>' (withheld_fp()). Text is compared only in memory, against the backup in hand (the import) or
against stored text that is still readable.

The rules (how a source row joins a group) run in importer.Processor.held_rows(); this module holds the stones and
exemptions in memory (Identities), writes stones (put_stone), runs the repair (repair: the SQL function
brain_withhold_repair() of 0008, then the steps only Python can do), and keeps purged items purged when they come back
under another key (PurgeIndex: keyed fingerprints of their bytes, HMAC with a key derived from the database password).

Never imports importer.py: the importer imports the constants from here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import String, and_, func, literal_column, or_, select, update
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import Settings, get_settings
from ..models import (
    ArchiveMessage,
    ArchiveSession,
    BrainMaintenance,
    BrainPurgedContent,
    BrainTombstone,
    BrainWithholdExemption,
    ImportRecord,
    ImportRun,
    MemoryItem,
)
from . import policy
from . import text as brain_text

WITHHELD_BY_ADMIN = policy.WITHHELD_BY_ADMIN
PURGED_BY_ADMIN = "purged by an admin"  # review.purge()'s tombstone reason
# The importer's duplicate links (importer.plan_messages): a row stored only as a link to the row it repeats.
CARRIED = "carried-on copy of a compacted message"
SAME_AS_PREVIOUS = "same as the previous message"
PROVENANCE_REASONS = (CARRIED, SAME_AS_PREVIOUS)
UID_KEY = "jarvis:message:uid:"
ROW_KEY = "jarvis:message:"
MIN_CONTAINED = 32  # characters, normalised: "contains the withheld text"
CERTAIN, PENDING, KEPT = "", "pending", "kept"
CONTAINS_SHORT = "same message id, contains the withheld text"  # a hold: a shorter anchor inside its text
COPY_UNCOMPARED = "copied by JARVIS, nothing to compare"  # a hold: a copy of a withheld row the backup lost
HOLD_VIA = ("same message id, different text", CONTAINS_SHORT, "same text", COPY_UNCOMPARED,
            "upgrade: same message id", "upgrade: same text")
LINEAGE = ("continuation", "branch")  # a child that copied its parent's history (never "delegated")
ENTRY = re.compile(r"#entry-([0-9a-f]{16})(?:-[0-9]+)?$")  # a memory entry's key ends with its digest
NOTE = "withheld by an admin"
# Hermes's restatement of an unfinished request after a compaction boundary (agent/context_compressor.py
# _INFLIGHT_TASK_REPLAY_HEADER): the same message (same message_uid), its text after this fixed header.
REPLAY_HEADER = ("[STILL IN PROGRESS \u2014 this is the active request, restated after the compaction boundary "
                 "because it was not finished yet. Continue it; do not start over.]")
TITLE_SUFFIX = re.compile(r" #[0-9]+$")  # Hermes names a branch '<title> #N' (get_next_title_in_lineage)


def now() -> datetime:
    return datetime.now(UTC)


# --- Pure helpers --------------------------------------------------------------------------------------------------


def withheld_fp(m: Any) -> str:
    """The source_fp of a withheld row: its state only, never a fingerprint of its text."""
    return "w1:compacted" if m.compacted == 1 else "w1:rewound" if m.active == 0 else "w1:live"


def same_source(row: Mapping[str, Any], m: Any) -> bool:
    """Whether a stored row was stored from this very source row. A withheld row keeps no fingerprint of its text, so it
    is the same while its state is (whatever the text became: it is withheld either way)."""
    fp = row["source_fp"] or ""
    return (bool(row["withheld"]) and fp == withheld_fp(m)) if fp.startswith("w1:") else fp == m.fp


class KeyParts(NamedTuple):
    session: str
    row: str
    uid: str


def parse_key(key: str | None) -> KeyParts:
    """(JARVIS session id, row id, uid) of a message's source key: jarvis:message:uid:<uid>, or
    jarvis:message:<session>:<row id> (split at the last ':', session ids may hold colons)."""
    key = key or ""
    if key.startswith(UID_KEY):
        return KeyParts("", "", key[len(UID_KEY):])
    if key.startswith(ROW_KEY):
        session, sep, row = key[len(ROW_KEY):].rpartition(":")
        if sep and session and row:
            return KeyParts(session, row, "")
    return KeyParts("", "", "")


def norm(text: str | None) -> str:
    """The whitespace normalisation of text.content_hash()."""
    return brain_text.SPACE.sub(" ", text or "").strip()


def dup_fp(m: Any) -> str:
    """Role, tool name, whitespace-normalised content and normalised tool calls: what makes two rows the same text
    (importer.Processor.dup_fp)."""
    from .source import sha256_text

    raw = m.tool_calls_raw
    try:
        calls = json.dumps(json.loads(raw), sort_keys=True) if isinstance(raw, str) and raw.strip() else raw
    except ValueError:
        calls = raw
    return sha256_text(m.role, m.tool_name, brain_text.content_hash(m.content), calls)


def text_key(m: Any) -> tuple[str, str, str]:
    return (m.role, m.tool_call_id, dup_fp(m))


def uid_conflict(a: Any, b: Any) -> bool:
    """Two rows that both carry a uid, and different ones: two Hermes messages."""
    return bool(a.uid) and bool(b.uid) and a.uid != b.uid


def copied_by_history(m: Any, ss: Any) -> bool:
    """A row a rotation, a CLI branch or a gateway branch copied from the parent with its timestamp: at or before the
    child session's start."""
    return (ss.lineage in LINEAGE and m.at is not None and ss.started_at is not None and m.at <= ss.started_at)


def restated(content: str | None) -> str:
    """The normalised request a Hermes restatement repeats (the text after REPLAY_HEADER), or ''."""
    text = content or ""
    return norm(text.rsplit(REPLAY_HEADER, 1)[1]) if REPLAY_HEADER in text else ""


def title_base(title: str | None) -> str:
    """A conversation title without Hermes's branch suffix (' #2')."""
    return TITLE_SUFFIX.sub("", (title or "").strip())


def title_follows(child_title: str | None, child_lineage: str, ancestor_title: str | None) -> bool:
    """Whether a continuation or branch carries the title of an ancestor: the same title (a branch adds ' #N'), or
    the ancestor has none any more and this is its continuation (Hermes moves the title to the continuation)."""
    mine, theirs = title_base(child_title), title_base(ancestor_title)
    return bool(mine) and ((bool(theirs) and mine == theirs) or (not theirs and child_lineage == "continuation"))


# --- Stones in memory ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stone:
    key: str
    anchor: str = ""  # '' for an admin's own decision (it is its own group); otherwise that decision's key
    session: str = ""
    row: str = ""
    role: str = ""
    tool_call_id: str = ""
    uid: str = ""
    uid_known: bool = False  # the uid was known when the admin decided (learned uids are not)
    via: str = ""
    review: str = CERTAIN
    bound: bool = False
    after_run: int = 0
    title_hidden: bool = False  # the admin hid the conversation's title with this withhold

    @property
    def group(self) -> str:
        return self.anchor or self.key

    @property
    def certain(self) -> bool:
        return self.review != PENDING


@dataclass(frozen=True)
class Match:
    stone: Stone
    via: str
    review: str  # '' (member) or 'pending' (hold)

    @property
    def group(self) -> str:
        return self.stone.group


MOVED = object()  # sentinel: the key has a stone bound to another JARVIS row that is still in the backup


def _review(st: Stone) -> str:
    """A match is a member ('') or a hold ('pending'): a hold an admin kept is a member from then on."""
    return PENDING if st.review == PENDING else CERTAIN


def _stone(r: Any) -> Stone:
    return Stone(key=r.source_key, anchor=r.anchor_key or "", session=r.source_session_id or "",
                 row=r.source_row_id or "", role=r.role or "", tool_call_id=r.tool_call_id or "",
                 uid=r.message_uid or "", uid_known=bool(r.uid_known), via=r.via or "", review=r.review or "",
                 bound=bool(r.bound), after_run=int(r.after_run or 0), title_hidden=bool(r.title_hidden))


class Identities:
    """Every stone and exemption, indexed for the rules (a few hundred rows). An import adds the stones it inherits
    (add()), so later sessions of the same run see them."""

    def __init__(self, stones: Iterable[Stone] = (), exempt: Iterable[tuple[str, str, str]] = ()) -> None:
        self.by_key: dict[str, Stone] = {}
        self.by_row: dict[tuple[str, str], list[Stone]] = {}
        self.by_uid: dict[str, list[Stone]] = {}
        self.groups: dict[str, list[Stone]] = {}
        self.exempt: set[tuple[str, str, str]] = set(exempt)
        self.sessions: set[str] = set()
        self.version: Counter[str] = Counter()
        # uids a withheld fold survivor absorbed (Backup row's absorbed_message_uids), learned when the stone was
        # re-bound from the backup (importer.Processor.load): a later composite that absorbed one holds that text too.
        self.absorbed: dict[str, Stone] = {}
        for st in stones:
            self._index(st)

    @classmethod
    async def load(cls, s: AsyncSession) -> Identities:
        stones = [_stone(r) for r in (await s.execute(select(BrainTombstone).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN).order_by(BrainTombstone.source_key))).scalars().all()]
        exempt = [(r.anchor_key, r.source_session_id, r.source_row_id) for r in (await s.execute(select(
            BrainWithholdExemption.anchor_key, BrainWithholdExemption.source_session_id,
            BrainWithholdExemption.source_row_id))).all()]
        return cls(stones, exempt)

    def _index(self, st: Stone) -> None:
        self.by_key[st.key] = st
        self.groups.setdefault(st.group, []).append(st)
        if st.row and st.session:
            self.by_row.setdefault((st.session, st.row), []).append(st)
            self.sessions.add(st.session)
        if st.uid:
            self.by_uid.setdefault(st.uid, []).append(st)
        self.version[st.session] += 1

    def _unindex(self, st: Stone) -> None:
        self.by_key.pop(st.key, None)
        group = self.groups.get(st.group, [])
        if st in group:
            group.remove(st)
        if st.row and st.session:
            rows = self.by_row.get((st.session, st.row), [])
            if st in rows:
                rows.remove(st)
        if st.uid:
            uids = self.by_uid.get(st.uid, [])
            if st in uids:
                uids.remove(st)
        self.version[st.session] += 1

    def add(self, st: Stone) -> Stone:
        """put_stone() in memory: a new key is added; a hold a certain rule now matches becomes a member; an unbound
        stone gets the binding. Never demotes and never rebinds a bound stone. Returns the stone as it is now."""
        old = self.by_key.get(st.key)
        if old is None:
            self._index(st)
            return st
        new = old
        if old.review == PENDING and st.review != PENDING:
            new = Stone(**{**old.__dict__, "review": st.review, "via": st.via, "anchor": st.anchor})
        if not new.bound and st.bound:
            new = Stone(**{**new.__dict__, "session": st.session, "row": st.row, "role": st.role,
                           "tool_call_id": st.tool_call_id, "uid": st.uid, "uid_known": st.uid_known, "bound": True})
        if new != old:
            self._unindex(old)
            self._index(new)
        return new

    def learn_uid(self, st: Stone, uid: str, absorbed: tuple[str, ...] = ()) -> Stone:
        """A stone bound to a row whose uid it did not know: the uid of that row in this backup (learned, so
        uid_known stays false), and the uids that row absorbed."""
        new = st
        if uid and not st.uid:
            new = Stone(**{**st.__dict__, "uid": uid})
            self._unindex(st)
            self._index(new)
        if new.certain:
            for u in absorbed:
                self.absorbed.setdefault(u, new)
        return new

    def group_uids(self, anchor: str) -> set[str]:
        return {st.uid for st in self.groups.get(anchor, ()) if st.uid}

    def known_uids(self, anchor: str) -> set[str]:
        return {st.uid for st in self.groups.get(anchor, ()) if st.uid and st.uid_known}

    def independent(self, anchor: str, uid: str) -> bool:
        """R-text's guard: this row carries its own uid, the withheld message's uid was known, and it is another one
        (a separate Hermes message)."""
        return bool(uid) and bool(self.known_uids(anchor)) and uid not in self.group_uids(anchor)

    def uid_stones(self, uid: str, role: str) -> list[Stone]:
        """The certain stones with this uid and an equal or unknown role, the admin's own first. A hold's uid is the
        held row's own and says nothing about the withheld message, so it never matches by itself."""
        return sorted((st for st in self.by_uid.get(uid, ()) if st.certain and st.role in ("", role)),
                      key=lambda st: (st.anchor != "", st.key))

    def match_row(self, key: str, session_id: str, row_id: str, role: str, uid: str) -> Match | None:
        """A stored row (a rollback puts it back) against the stones, without its text: its key has a stone, or its
        JARVIS row is a stone's binding (the stone's review); or it shares a withheld message's uid (a hold: the text
        cannot be compared here; the next import compares it). The database guard's rules are a subset."""
        st = self.by_key.get(key) if key else None
        if st is not None:
            return Match(st, "key", _review(st))
        for st in sorted(self.by_row.get((session_id, row_id[:200]), ()), key=lambda s: (not s.certain, s.key)):
            if st.role in ("", role):
                return Match(st, "same JARVIS row", _review(st))
        for st in self.uid_stones(uid, role) if uid else ():
            if (st.group, session_id, row_id[:200]) not in self.exempt:
                return Match(st, "same message id", PENDING)
        return None

    def certain(self, session_id: str, m: Any, has_row: Callable[[str, str], bool]) -> Match | None | object:
        """R-key, R-row and R-fold for source row m of JARVIS session session_id: a Match (a member, or a hold when
        the only stone naming it is a hold), None, or MOVED (its key has a stone bound to another JARVIS row that is
        still in the backup: never this message, the row is excluded as a key collision). A hold is returned even
        when an admin released its JARVIS row (the caller lets the release win and removes that stale hold)."""
        row_id = str(m.id)[:200]
        key_match: Match | None = None
        moved = False
        st = self.by_key.get(m.key)
        if st is not None:
            if not st.row or not st.session or (st.session, st.row) == (session_id, row_id):
                key_match = Match(st, "key", _review(st))
            elif not has_row(st.session, st.row) and st.role in ("", m.role):
                key_match = Match(st, "key", _review(st))  # the bound row is gone: the uid key passed to this row
            else:
                moved = True
        if key_match is not None and key_match.review == CERTAIN:
            return key_match
        row_stones = sorted((s for s in self.by_row.get((session_id, row_id), ()) if s.role in ("", m.role)),
                            key=lambda s: (not s.certain, s.anchor != "", s.key))
        row_match = Match(row_stones[0], "same JARVIS row", _review(row_stones[0])) if row_stones else None
        if row_match is not None and row_match.review == CERTAIN:
            return row_match
        fold = None
        for u in m.absorbed:
            found = [s for s in self.by_uid.get(u, ()) if s.certain] or (
                [self.absorbed[u]] if u in self.absorbed else [])
            if found:
                fold = Match(found[0], "merged into another message", CERTAIN)
                break
        if fold is None and m.uid and m.absorbed:
            found = [s for s in self.by_uid.get(m.uid, ()) if s.certain]
            if found:
                fold = Match(found[0], "merged into another message", CERTAIN)
        if fold is not None:
            return fold
        if key_match is not None:
            return key_match
        if row_match is not None:
            return row_match
        return MOVED if moved else None


# --- Writing stones ------------------------------------------------------------------------------------------------


async def put_stone(s: AsyncSession, binding: Stone, *, actor: str) -> bool:
    """A stone for binding.key: inserted, or a hold a certain rule now matches made a member (or kept, when a
    decisions file carries an admin's keep; never a reason changed, never a demotion, never a bound stone re-bound);
    an unbound stone gets the binding. Whether anything changed."""
    values = {"source_key": binding.key, "purged_at": now(), "purged_by": actor[:200], "reason": WITHHELD_BY_ADMIN,
              "source_session_id": binding.session[:200], "source_row_id": binding.row[:200],
              "role": binding.role[:40], "tool_call_id": binding.tool_call_id[:200], "message_uid": binding.uid[:200],
              "uid_known": binding.uid_known, "anchor_key": binding.anchor, "via": binding.via[:200],
              "review": binding.review, "bound": bool(binding.row or binding.uid), "after_run": binding.after_run,
              "title_hidden": binding.title_hidden}
    stmt = pg_insert(BrainTombstone).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BrainTombstone.source_key],
        set_={"review": stmt.excluded.review, "via": stmt.excluded.via, "anchor_key": stmt.excluded.anchor_key},
        where=and_(BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == PENDING,
                   stmt.excluded.review != PENDING))
    changed = bool((await s.execute(stmt)).rowcount)
    if values["bound"]:
        bind = await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == binding.key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.bound.is_(False)).values(
            source_session_id=values["source_session_id"], source_row_id=values["source_row_id"],
            role=values["role"], tool_call_id=values["tool_call_id"],
            message_uid=func.coalesce(func.nullif(BrainTombstone.message_uid, ""), values["message_uid"]),
            # a stone written before its row was known (decisions-apply's tombstone) gets the decision's run too
            after_run=func.greatest(BrainTombstone.after_run, values["after_run"]),
            bound=True))
        changed = changed or bool(bind.rowcount)
    if binding.title_hidden:  # an admin's title choice is never undone by a later write
        hid = await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == binding.key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.title_hidden.is_(False)).values(title_hidden=True))
        changed = changed or bool(hid.rowcount)
    return changed


async def current_after_run(s: AsyncSession) -> int:
    """The newest import run when a decision is taken: rows a run up to it stored were seen by the admin."""
    return int(await s.scalar(select(func.coalesce(func.max(ImportRun.id), 0))) or 0)


# --- An admin's withhold: the stored rows of the same message (spec 8.1) ---------------------------------------------

GROUP_CAP = 1000  # stored rows of one message a withhold follows (a JARVIS message is stored a handful of times)
IDENTICAL = "identical, chosen by an admin"


def row_uid(row: Any) -> str:
    """The JARVIS uid of a stored row: its source_uid, or the uid its key names ('' unknown)."""
    return (getattr(row, "source_uid", "") or "") or parse_key(row.source_key).uid


def message_text(row: Any) -> tuple[str, str, str, str]:
    """What makes two stored rows the same text: role, tool_call_id, whitespace-normalised content and tool calls.
    Compared in memory only, while the text is still readable; never stored."""
    return (row.role or "", row.tool_call_id or "", norm(row.content),
            json.dumps(row.tool_calls or [], sort_keys=True))


def binding_of(row: Any, source_session_id: str, *, anchor: str = "", via: str = "", review: str = CERTAIN,
               after_run: int = 0, uid_known: bool = False) -> Stone:
    """The stone for a stored row: its key and the JARVIS row it is (session, row id, role, tool_call_id, uid)."""
    return Stone(key=row.source_key, anchor=anchor, session=source_session_id or "", row=row.source_message_id or "",
                 role=row.role or "", tool_call_id=row.tool_call_id or "", uid=row_uid(row), uid_known=uid_known,
                 via=via, review=review, bound=True, after_run=after_run)


@dataclass
class GroupRow:
    row: ArchiveMessage
    source_session_id: str
    rule: str  # how it joined: same JARVIS row, same message id(, different text), duplicate
    review: str  # '' member, 'pending' hold


@dataclass
class Group:
    """The stored rows that are the same message as the one an admin withholds (the message itself left out), and
    the keys of its copies that have no row (importer duplicates: a carried-on copy, an adjacent repeat)."""
    rows: list[GroupRow] = field(default_factory=list)
    rowless: list[Stone] = field(default_factory=list)

    @property
    def members(self) -> list[GroupRow]:
        return [g for g in self.rows if g.review == CERTAIN]

    @property
    def holds(self) -> list[GroupRow]:
        return [g for g in self.rows if g.review == PENDING]


REWOUND_REASON = "rewound since an earlier import"  # importer.Processor: a row deleted as rewound (its rollback copy)
# A rollback copy of a deleted row, with its keys written literally: 0008's partial expression indexes
# (import_records_deleted_uid_idx, import_records_deleted_session_idx) match only literal keys and predicates.
DELETED_COPY = and_(ImportRecord.record_type == literal_column("'message'"),
                    ImportRecord.before.op("?")(literal_column("'deleted_row'")))


def deleted_field(name: str) -> Any:
    """SQL: before -> 'deleted_row' ->> name, the keys written literally (DELETED_COPY)."""
    assert name.isidentifier()
    return (ImportRecord.before.op("->", return_type=JSONB)(literal_column("'deleted_row'"))
            .op("->>", return_type=String)(literal_column(f"'{name}'")))


def gone_text(gone: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """message_text() of a rollback copy's deleted row (a dict)."""
    return (gone.get("role") or "", gone.get("tool_call_id") or "", norm(gone.get("content")),
            json.dumps(gone.get("tool_calls") or [], sort_keys=True))


def deleted_copies_query(row: Any, uid: str) -> Any:
    """The query of deleted_copies() (None when there is nothing to look for)."""
    conds = [DELETED_COPY, ImportRecord.source_key != "", deleted_field("role") == row.role,
             ~select(ArchiveMessage.id).where(ArchiveMessage.source_key == ImportRecord.source_key).exists()]
    if uid:
        conds.append(or_(ImportRecord.source_key == UID_KEY + uid, deleted_field("source_uid") == uid))
    elif row.import_run_id:
        conds += [ImportRecord.reason == REWOUND_REASON, deleted_field("session_id") == str(row.session_id),
                  func.coalesce(deleted_field("source_uid"), "") == "", ~ImportRecord.source_key.like(UID_KEY + "%"),
                  ImportRecord.run_id <= row.import_run_id]
    else:
        return None
    return select(ImportRecord.source_key, ImportRecord.before).where(*conds).order_by(ImportRecord.id)


async def deleted_copies(s: AsyncSession, row: ArchiveMessage, uid: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(key, source session id, deleted row) of the rollback copies of rows an import deleted that may be this row's
    message, under a key no stored row holds: with a uid, those of the same uid and role (Hermes keeps a message's uid
    in every copy: an in-place compaction's rewound original, a CLI branch child's rewound copy); without one, the
    importer's rewound twin (identity.rewound_twins the other way): a row of the same conversation and role, without a
    uid, deleted as rewound by the run that stored this row or an earlier one. Text is compared by the caller, in
    memory. Found through 0008's partial indexes on deleted rows (never a scan of every rollback copy)."""
    query = deleted_copies_query(row, uid)
    if query is None:
        return []
    found = (await s.execute(query)).all()
    gone = [(key, (before or {}).get("deleted_row") or {}) for key, before in found]
    ids = {int(g["session_id"]) for _, g in gone if str(g.get("session_id") or "").isdigit()}
    names = dict((await s.execute(select(ArchiveSession.id, ArchiveSession.source_session_id).where(
        ArchiveSession.id.in_(sorted(ids))))).all()) if ids else {}
    return [(key, names.get(int(g["session_id"]), "") if str(g.get("session_id") or "").isdigit() else "", g)
            for key, g in gone]


async def group_rows(s: AsyncSession, message: ArchiveMessage, sess: ArchiveSession,
                     text: tuple[str, str, str, str], *, lock: bool = True) -> Group:
    """Every stored row that is the same JARVIS message as `message`, to a fixed point from each member:
    - the same JARVIS row (session, row id, role): a member (a key the importer changed: uid backfill, renumbering);
    - the same uid (source_uid, or the uid its key names) and role: a member when its text equals `text`, or when it
      is an archived generation (state compacted) in the message's own conversation family (it, its continuations
      and branches); a hold otherwise ('same message id, different text'). A row an admin released for this group
      (brain_withhold_exemptions) is left as it is unless its text is the message's own;
    - a copy JARVIS made in a continuation or branch child (a gateway branch, a rotation of uid-less rows: a fresh
      uid): the same role and text at the member's very time, at or before the child's start, and not carrying the
      uid of another stored row of the family (that row is what was copied): a member ('copied by JARVIS');
    - a fold that absorbed a member's uid (Hermes absorbed_message_uids: a compaction carrier, a micro-compaction
      merge): it holds the text, a member ('merged into another message');
    - the importer's duplicates of any row the member's key was stored as (a carried-on copy, an adjacent repeat): a
      member, or a key with no row (Group.rowless);
    - a row of it an import deleted, kept only as a rollback copy under a key no stored row holds (deleted_copies():
      the same uid with the same text, or an archived generation in the family; without uids, the rewound twin with
      the same text): a key with no row (Group.rowless), so the withhold blanks that copy and a rollback puts the row
      back withheld.
    Holds are not followed. Rows already withheld are followed and get a stone, never blanked again; one that only
    shares the uid is left alone (no text to compare). Identical text elsewhere is never part of the group
    (identical_rows: the admin's choice). Locked FOR UPDATE with lock (a promote of one waits, then is refused).
    BrainError past GROUP_CAP rows."""
    from . import archive
    from .store import BrainError

    out = Group()
    found: dict[int, GroupRow] = {}
    keys: set[str] = {message.source_key}
    rowless: dict[str, Stone] = {}
    frontier: list[tuple[ArchiveMessage, str]] = [(message, sess.source_session_id)]
    released = {(sid, rid) for sid, rid in (await s.execute(select(
        BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id).where(
        BrainWithholdExemption.anchor_key == message.source_key))).all()} if message.source_key else set()
    family = await lineage(s, [sess.source_session_id])
    uid_seen: dict[str, bool] = {}  # (uid, conversations) -> whether a stored row there carries it (copy guard)

    def locked(q: Any) -> Any:
        return q.with_for_update(of=ArchiveMessage) if lock else q

    def exempt(row: ArchiveMessage, sid: str) -> bool:
        return (sid, (row.source_message_id or "")[:200]) in released

    async def uid_elsewhere(uid: str, sids: Iterable[str]) -> bool:
        """Whether another stored row of these conversations (not a continuation or branch copy) carries this uid."""
        wanted = sorted(sids)
        cache_key = uid + "\x00" + ",".join(wanted)
        if cache_key not in uid_seen:
            uid_seen[cache_key] = bool(wanted) and bool(await s.scalar(
                select(func.count()).select_from(ArchiveMessage)
                .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                .where(ArchiveSession.source_session_id.in_(wanted),
                       or_(ArchiveMessage.source_uid == uid, ArchiveMessage.source_key == UID_KEY + uid))))
        return uid_seen[cache_key]

    def take(row: ArchiveMessage, sid: str, rule: str, review: str) -> bool:
        if row.id == message.id:
            return False
        if row.id in found:  # a hold a certain rule reaches later becomes a member (and is followed)
            if review == CERTAIN and found[row.id].review == PENDING:
                found[row.id] = GroupRow(row, sid, rule, review)
                return True
            return False
        if len(found) >= GROUP_CAP:
            raise BrainError("too many stored copies of this message (more than %d): ask for help before withholding "
                             "it" % GROUP_CAP)
        found[row.id] = GroupRow(row, sid, rule, review)
        keys.add(row.source_key)
        return review == CERTAIN

    while frontier:
        nxt: list[tuple[ArchiveMessage, str]] = []
        for row, sid in frontier:
            # The same JARVIS row (any archive row stored for it, whatever its key).
            if sid and row.source_message_id:
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveSession.source_session_id == sid,
                               ArchiveMessage.source_message_id == row.source_message_id,
                               ArchiveMessage.role == row.role).order_by(ArchiveMessage.id)))).all():
                    if take(other, other_sid, "same JARVIS row", CERTAIN):
                        nxt.append((other, other_sid))
            # The same Hermes uid (copies keep it: compaction generations, rotation, a CLI branch, prune twins).
            uid = row_uid(row)
            if uid:
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(or_(ArchiveMessage.source_uid == uid, ArchiveMessage.source_key == UID_KEY + uid),
                               ArchiveMessage.role == row.role).order_by(ArchiveMessage.id)))).all():
                    if other.id == message.id or (other.id in found and found[other.id].review == CERTAIN):
                        continue
                    if other.withheld:
                        if (other.state == "compacted" and not exempt(other, other_sid) and other_sid in family
                                and take(other, other_sid, "same message id", CERTAIN)):
                            nxt.append((other, other_sid))
                        continue
                    if message_text(other) == text:
                        if take(other, other_sid, "same message id", CERTAIN):
                            nxt.append((other, other_sid))
                    elif exempt(other, other_sid):
                        continue  # an admin released it: no shortcut overrides that (I4)
                    elif other.state == "compacted" and other_sid in family:
                        if take(other, other_sid, "same message id", CERTAIN):
                            nxt.append((other, other_sid))
                    else:
                        take(other, other_sid, "same message id, different text", PENDING)
                # A fold that absorbed this uid holds the text (its own uid's generations follow from it).
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveMessage.absorbed_uids.contains([uid])).order_by(ArchiveMessage.id)))).all():
                    if take(other, other_sid, "merged into another message", CERTAIN):
                        nxt.append((other, other_sid))
            # Copies JARVIS made into a continuation or branch child with a fresh uid: the same text at this row's
            # very time (Hermes keeps a copied row's timestamp), at or before the child's start.
            if sid and row.at is not None:
                kids = sorted(c for c, how in (await lineage(s, [sid])).items() if how == "descendant")
                for other, other_sid in ((await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveSession.source_session_id.in_(kids), ArchiveSession.lineage.in_(LINEAGE),
                               ArchiveSession.started_at.is_not(None), ArchiveMessage.at == row.at,
                               ArchiveMessage.at <= ArchiveSession.started_at, ArchiveMessage.role == row.role,
                               ArchiveMessage.withheld.is_(False)).order_by(ArchiveMessage.id)))).all()
                        if kids else []):
                    if message_text(other) != text or exempt(other, other_sid):
                        continue
                    other_uid = row_uid(other)
                    if other_uid and other_uid != uid and await uid_elsewhere(other_uid, [
                            c for c, how in (await lineage(s, [other_sid])).items() if how == "ancestor"]):
                        continue  # the copy of another row with this text and time: that row's uid says so
                    if take(other, other_sid, "copied by JARVIS", CERTAIN):
                        nxt.append((other, other_sid))
            # Rows of it an import deleted, kept only as a rollback copy (review 1 R29).
            for key, gone_sid, gone in await deleted_copies(s, row, uid):
                if key in keys or key in rowless or gone.get("withheld") is True:
                    continue
                gone_row = str(gone.get("source_message_id") or "")[:200]
                if gone_sid and (gone_sid, gone_row) in released:
                    continue  # an admin released that JARVIS row for this withhold (I4)
                if not (gone_text(gone) == text
                        or (uid and gone.get("state") == "compacted" and gone_sid in family)):
                    continue
                parts = parse_key(key)
                rowless[key] = Stone(key=key, session=gone_sid or parts.session or sess.source_session_id,
                                     row=gone_row or parts.row, role=row.role,
                                     tool_call_id=gone.get("tool_call_id") or "", uid=uid,
                                     via="same message id" if uid else "copied by JARVIS", bound=True)
        # The importer's duplicates of any row the members' keys were stored as.
        members = [message] + [g.row for g in found.values() if g.review == CERTAIN]
        targets = {int(m.id) for m in members} | await archive._older_rows(
            s, sorted({m.source_key for m in members if m.source_key}))
        dup_keys: set[str] = set()
        for chunk in (sorted(targets)[i:i + 1000] for i in range(0, len(targets), 1000)):
            for key in (await s.scalars(select(ImportRecord.source_key).where(
                    ImportRecord.record_type == "message", ImportRecord.action == "duplicate",
                    ImportRecord.target_table == "archive_messages", ImportRecord.reason.in_(PROVENANCE_REASONS),
                    ImportRecord.target_id.in_(chunk)).order_by(ImportRecord.id))).all():
                if key and key not in keys and key not in rowless:
                    dup_keys.add(key)
        if dup_keys:
            by_key = {r.source_key: (r, sid) for r, sid in (await s.execute(locked(
                select(ArchiveMessage, ArchiveSession.source_session_id)
                .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                .where(ArchiveMessage.source_key.in_(sorted(dup_keys)))))).all()}
            for key in sorted(dup_keys):
                if key in by_key:
                    row, sid = by_key[key]
                    if take(row, sid, "duplicate", CERTAIN):
                        nxt.append((row, sid))
                else:  # a duplicate repeats its target's role (Processor.dup_fp): every member has the message's
                    parts = parse_key(key)
                    rowless[key] = Stone(key=key, session=parts.session or sess.source_session_id, row=parts.row,
                                         role=message.role, uid=parts.uid, via="duplicate",
                                         bound=bool(parts.row or parts.uid))
        frontier = nxt
    out.rows = [found[k] for k in sorted(found)]
    out.rowless = [rowless[k] for k in sorted(rowless)]
    return out


async def identical_rows(s: AsyncSession, message: ArchiveMessage, sess: ArchiveSession,
                         text: tuple[str, str, str, str], *, exclude: Iterable[int] = ()) -> list[ArchiveMessage]:
    """Readable rows with exactly this text (role, tool_call_id, normalised content and tool calls) in the message's
    conversation and its continuation and branch ancestors and descendants (lineage(), no depth limit), other than
    the message and `exclude` (its group). Never withheld unless the admin ticks the box (include_identical)."""
    lineage_ids = await lineage(s, [sess.source_session_id])
    session_ids = list((await s.scalars(select(ArchiveSession.id).where(
        ArchiveSession.source_session_id.in_(sorted(lineage_ids))))).all()) or [sess.id]
    skip = {int(message.id)} | {int(i) for i in exclude}
    query = select(ArchiveMessage).where(
        ArchiveMessage.session_id.in_(session_ids), ArchiveMessage.withheld.is_(False),
        ArchiveMessage.role == message.role, ArchiveMessage.tool_call_id == (message.tool_call_id or ""))
    words = [w for w in text[2].split(" ") if w]
    if words:  # a word survives the whitespace normalisation: the database narrows the rows to compare
        query = query.where(func.strpos(ArchiveMessage.content, max(words, key=len)) > 0)
    return [r for r in (await s.scalars(query.order_by(ArchiveMessage.id))).all()
            if r.id not in skip and message_text(r) == text]


async def first_user_message(s: AsyncSession, message: ArchiveMessage) -> bool:
    """Whether this is its conversation's first user message (the lowest position): the title can paraphrase it."""
    if message.role != "user":
        return False
    first = await s.scalar(select(ArchiveMessage.id).where(ArchiveMessage.session_id == message.session_id,
                                                           ArchiveMessage.role == "user")
                           .order_by(ArchiveMessage.position, ArchiveMessage.id).limit(1))
    return first == message.id


async def matches_withheld(s: AsyncSession, message: ArchiveMessage, sess: ArchiveSession) -> bool:
    """Whether a stored row is the same JARVIS message as one an admin withheld (promote refuses it, spec 8.4): its
    key has a stone; its JARVIS row (session, row id) is a stone's binding with an equal or unknown role; or its uid is
    a stone's uid with an equal or unknown role and no release of that group names this JARVIS row. Identical text
    alone never matches (a row the admin saw stays promotable)."""
    if message.source_key and await s.scalar(select(BrainTombstone.source_key).where(
            BrainTombstone.source_key == message.source_key, BrainTombstone.reason == WITHHELD_BY_ADMIN)):
        return True
    if sess.source_session_id and message.source_message_id and await s.scalar(select(BrainTombstone.source_key).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.source_session_id == sess.source_session_id,
            BrainTombstone.source_row_id == message.source_message_id[:200],
            BrainTombstone.role.in_(("", message.role))).limit(1)):
        return True
    uid = row_uid(message)
    if not uid:
        return False
    for key, anchor in (await s.execute(select(BrainTombstone.source_key, BrainTombstone.anchor_key).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.message_uid == uid[:200],
            BrainTombstone.role.in_(("", message.role))))).all():
        if await s.get(BrainWithholdExemption, (anchor or key, sess.source_session_id,
                                                (message.source_message_id or "")[:200])) is None:
            return True
    return False


async def rows_for(s: AsyncSession, binding: Stone) -> tuple[list[ArchiveMessage], list[ArchiveMessage]]:
    """The stored rows a withhold carried in a decisions file names (decisions-apply, spec 9.2): the certain ones (its
    key; its JARVIS row: the binding's session and row id, with an equal or unknown role) and the rows that only share
    its uid (another JARVIS row: held for review, since nothing here can compare the text; the next import does).
    A row a release of its group names is left out of the uid ones (I4)."""
    certain: dict[int, ArchiveMessage] = {}
    for r in (await s.scalars(select(ArchiveMessage).where(ArchiveMessage.source_key == binding.key))).all():
        certain[r.id] = r
    if binding.session and binding.row:
        query = (select(ArchiveMessage).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                 .where(ArchiveSession.source_session_id == binding.session[:200],
                        ArchiveMessage.source_message_id == binding.row[:200]))
        if binding.role:
            query = query.where(ArchiveMessage.role == binding.role)
        for r in (await s.scalars(query)).all():
            certain[r.id] = r
    uid_only: list[ArchiveMessage] = []
    if binding.uid:
        query = (select(ArchiveMessage, ArchiveSession.source_session_id)
                 .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                 .where(or_(ArchiveMessage.source_uid == binding.uid[:200],
                            ArchiveMessage.source_key == UID_KEY + binding.uid)))
        if binding.role:
            query = query.where(ArchiveMessage.role == binding.role)
        for r, sid in (await s.execute(query.order_by(ArchiveMessage.id))).all():
            if r.id not in certain and await s.get(BrainWithholdExemption, (
                    binding.group, sid, (r.source_message_id or "")[:200])) is None:
                uid_only.append(r)
    return [certain[k] for k in sorted(certain)], uid_only


async def folds_of(s: AsyncSession, binding: Stone) -> list[ArchiveMessage]:
    """The readable stored rows a Hermes fold merged a withheld message into (absorbed_uids holds its uid: a
    compaction carrier, a micro-compaction merge): they hold its text. None for a hold (its uid is the held row's)."""
    if not binding.uid or binding.review == PENDING:
        return []
    return list((await s.scalars(select(ArchiveMessage).where(
        ArchiveMessage.absorbed_uids.contains([binding.uid[:200]]), ArchiveMessage.withheld.is_(False))
        .order_by(ArchiveMessage.id))).all())


async def rewound_twins(s: AsyncSession, binding: Stone) -> list[tuple[ArchiveMessage, str]]:
    """The importer's twin link from stored data (decisions-apply, when no row of a carried withhold is stored here):
    an import deleted the message's row as rewound (Hermes's in-place compaction, a retry) and kept its rollback copy
    with its text, and the same conversation holds a readable row with that very text stored by that run or later
    (the copy Hermes wrote again). Its review: a member, or a hold when both rows carry uids and they differ (another
    Hermes message, or a re-send). Compared in memory only, while the rollback copy still holds the text."""
    if binding.review == PENDING:
        return []
    out: dict[int, tuple[ArchiveMessage, str]] = {}
    for run_id, before in (await s.execute(select(ImportRecord.run_id, ImportRecord.before).where(
            ImportRecord.source_key == binding.key, ImportRecord.record_type == "message",
            ImportRecord.reason == "rewound since an earlier import").order_by(ImportRecord.id))).all():
        gone = (before or {}).get("deleted_row") or {}
        if not gone or gone.get("withheld") or not gone.get("session_id"):
            continue
        text = (gone.get("role") or "", gone.get("tool_call_id") or "", norm(gone.get("content")),
                json.dumps(gone.get("tool_calls") or [], sort_keys=True))
        if not text[2] and text[3] == "[]":
            continue
        words = [w for w in text[2].split(" ") if w]
        query = select(ArchiveMessage).where(
            ArchiveMessage.session_id == int(gone["session_id"]), ArchiveMessage.withheld.is_(False),
            ArchiveMessage.role == text[0], ArchiveMessage.import_run_id >= run_id)
        if words:
            query = query.where(func.strpos(ArchiveMessage.content, max(words, key=len)) > 0)
        sid = await s.scalar(select(ArchiveSession.source_session_id).where(
            ArchiveSession.id == int(gone["session_id"])))
        for row in (await s.scalars(query.order_by(ArchiveMessage.id))).all():
            if message_text(row) != text or row.id in out or await s.get(BrainWithholdExemption, (
                    binding.group, sid or "", (row.source_message_id or "")[:200])) is not None:
                continue  # another text, or a row an admin released for this withhold
            mine, theirs = row_uid(row), gone.get("source_uid") or parse_key(binding.key).uid
            out[row.id] = (row, PENDING if mine and theirs and mine != theirs else CERTAIN)
    return [out[k] for k in sorted(out)]


async def items_with_text(s: AsyncSession, message: ArchiveMessage, where: Any) -> list[dict[str, Any]]:
    """Imported memory items (not deleted, within `where`: the admin's scope) whose whitespace-normalised body
    contains the message's normalised text, when that text is at least MIN_CONTAINED characters: listed in the
    withhold preview so an admin can purge them; never removed automatically. Ids and titles only."""
    needle = norm(message.content)
    if len(needle) < MIN_CONTAINED:
        return []
    word = max(needle.split(" "), key=len)
    rows = (await s.execute(select(MemoryItem.id, MemoryItem.title, MemoryItem.body).where(
        MemoryItem.origin == "import", MemoryItem.deleted_at.is_(None), where,
        func.strpos(MemoryItem.body, word) > 0).order_by(MemoryItem.id))).all()
    return [{"id": int(item_id), "title": title} for item_id, title, body in rows if needle in norm(body)]


async def lineage(s: AsyncSession, source_session_ids: Iterable[str]) -> dict[str, str]:
    """The conversations these JARVIS sessions belong to: each one ('self'), its continuation and branch ancestors
    ('ancestor') and descendants ('descendant'). Walked through archive_sessions with a visited set and no depth
    limit (a cycle ends the walk)."""
    out: dict[str, str] = {sid: "self" for sid in source_session_ids if sid}
    frontier = set(out)
    while frontier:  # up
        rows = (await s.execute(select(ArchiveSession.source_session_id, ArchiveSession.parent_source_session_id)
                                .where(ArchiveSession.source_session_id.in_(sorted(frontier)),
                                       ArchiveSession.lineage.in_(LINEAGE),
                                       ArchiveSession.parent_source_session_id != ""))).all()
        frontier = {p for _, p in rows if p and p not in out}
        out.update({p: "ancestor" for p in frontier})
    frontier = {sid for sid, how in out.items() if how == "self"}
    while frontier:  # down
        rows = (await s.scalars(select(ArchiveSession.source_session_id).where(
            ArchiveSession.parent_source_session_id.in_(sorted(frontier)),
            ArchiveSession.lineage.in_(LINEAGE)))).all()
        frontier = {c for c in rows if c and c not in out}
        out.update({c: "descendant" for c in frontier})
    return out


# --- Purges: keyed fingerprints of purged items (HMAC; never a plain hash) -------------------------------------------

PURGE_KEY_LABEL = b"infinite-ai/jarvis-brain/purge-fingerprint/v1"


class PurgeKeyMismatch(Exception):
    """Stored purge fingerprints were made with another database password (or there is no password now). The message
    is safe to show; the importer refuses with it."""


OTHER_KEY = ("purged-item fingerprints were made with another database password: restore it, or run brain-import "
             "fingerprints --forget-other-key --yes; the purges themselves stay, by their keys")


def purge_key(settings: Settings | None = None) -> bytes | None:
    """The key of purge fingerprints, derived from the database password (every Command Center process that reads the
    brain holds it through DATABASE_URL; pg_dump never contains it). None without a password."""
    password = make_url((settings or get_settings()).database_url).password
    if not password:
        return None
    return hmac.new(PURGE_KEY_LABEL, str(password).encode("utf-8"), hashlib.sha256).digest()


def purge_key_id(key: bytes) -> str:
    return hmac.new(key, b"key id", hashlib.sha256).hexdigest()[:8]


def purge_fp(key: bytes, sha256_hex: str) -> str:
    """'k1:<key id>:<hex>': the keyed fingerprint of an item's raw source_sha256 (byte-identical text only)."""
    return "k1:%s:%s" % (purge_key_id(key), hmac.new(key, sha256_hex.encode("ascii"), hashlib.sha256).hexdigest())


def live_fingerprints() -> Any:
    """SQL: the purge fingerprints whose purge still stands (its tombstone is there; an incoming version has none).
    A purge a restore undid keeps no fingerprint (0008 deletes them; until then they are not read)."""
    return or_(BrainPurgedContent.source_key == "", BrainPurgedContent.source_key.in_(
        select(BrainTombstone.source_key).where(BrainTombstone.reason == PURGED_BY_ADMIN)))


@dataclass
class PurgeIndex:
    fps: set[str] = field(default_factory=set)
    entry_digests: set[str] = field(default_factory=set)
    key: bytes | None = None
    key_id: str = ""
    other_key: int = 0

    @classmethod
    async def load(cls, s: AsyncSession, settings: Settings | None = None, *, check: bool = True) -> PurgeIndex:
        """The stored fingerprints and the purged entry digests. With check, PurgeKeyMismatch when a fingerprint was
        made with another key (or there is no key now): the importer refuses then."""
        key = purge_key(settings)
        key_id = purge_key_id(key) if key else ""
        fps = set((await s.scalars(select(BrainPurgedContent.fp).where(live_fingerprints()))).all())
        other = sum(1 for fp in fps if not key or not fp.startswith("k1:%s:" % key_id))
        if other and check:
            raise PurgeKeyMismatch(OTHER_KEY)
        digests = {m.group(1) for k in (await s.scalars(select(BrainTombstone.source_key).where(
            BrainTombstone.reason == PURGED_BY_ADMIN))).all() if (m := ENTRY.search(k))}
        return cls(fps=fps, entry_digests=digests, key=key, key_id=key_id, other_key=other)

    def fingerprint(self, sha256_hex: str) -> str | None:
        return purge_fp(self.key, sha256_hex) if self.key and sha256_hex else None

    def same_content(self, p: Any, shas_here: set[str]) -> bool:
        """An item an admin purged, back under another key: a memory entry with a purged entry's digest (moved or
        copied to another memory file), a copy in this same backup of a purged file (shas_here), or the bytes of an
        item purged since 0.6.2 (its keyed fingerprint)."""
        found = ENTRY.search(p.key) if p.record_type == "memory_entry" else None
        if found and found.group(1) in self.entry_digests:
            return True
        if p.sha256 and p.sha256 in shas_here:
            return True
        fp = self.fingerprint(p.sha256)
        return fp is not None and fp in self.fps


def purge_kind(key: str) -> str:
    """The record type a purged item's key names (brain_purged_content.kind)."""
    return "memory_entry" if ENTRY.search(key) else "role_block" if "#role-" in key else "file"


async def remember_purge(s: AsyncSession, *, source_key: str, sha256_hex: str, kind: str, actor: str, via: str,
                         key: bytes | None = None) -> bool:
    """Stores the keyed fingerprint of a purged item's bytes (no key: nothing, the purge holds by its key alone)."""
    key = key if key is not None else purge_key()
    if not key or not sha256_hex:
        return False
    stmt = pg_insert(BrainPurgedContent).values(
        fp=purge_fp(key, sha256_hex), source_key=source_key or "", kind=kind[:40], purged_by=actor[:200],
        via=via[:40]).on_conflict_do_nothing(index_elements=[BrainPurgedContent.fp])
    return bool((await s.execute(stmt)).rowcount)


async def forget_other_key(s: AsyncSession, *, actor: str, apply: bool) -> int:
    """Deletes the purge fingerprints made with another key (the purges stay, by their keys); audited."""
    key = purge_key()
    prefix = "k1:%s:" % purge_key_id(key) if key else None
    query = select(BrainPurgedContent.fp)
    if prefix:
        query = query.where(~BrainPurgedContent.fp.startswith(prefix))
    other = list((await s.scalars(query)).all())
    if apply and other:
        for i in range(0, len(other), 1000):
            await s.execute(BrainPurgedContent.__table__.delete().where(
                BrainPurgedContent.fp.in_(other[i:i + 1000])))
        await audit.record(s, actor, "brain.purge_fingerprints_forgotten", "brain", "", {"count": len(other)})
    return len(other)


# --- Repair and its counts -----------------------------------------------------------------------------------------

PURGED_BEFORE = "purged_before"  # policy.RISK_FLAGS: an item an admin purged that came back


async def _purged_back(s: AsyncSession, *, actor: str, first: bool) -> int:
    """R8: imported items that are the same text as an item an admin purged quarantine (admin only, never in a
    prompt; nothing is purged automatically): (a) its key has a purge tombstone (a restore of a dump older than the
    purge); (b) once, after the upgrade: a memory entry with a purged entry's digest, imported after that purge; (c)
    its bytes' keyed fingerprint, imported after that purge."""
    from . import store

    live = and_(MemoryItem.origin == "import", MemoryItem.deleted_at.is_(None),
                ~MemoryItem.flags.has_key(PURGED_BEFORE))
    ids: set[int] = set((await s.scalars(select(MemoryItem.id).where(live, MemoryItem.source_key.in_(
        select(BrainTombstone.source_key).where(BrainTombstone.reason == PURGED_BY_ADMIN))))).all())

    async def run_at(at: datetime) -> int:
        return int(await s.scalar(select(func.coalesce(func.max(ImportRun.id), 0)).where(
            ImportRun.started_at <= at)) or 0)

    if first:
        purged: dict[str, int] = {}
        for key, at in (await s.execute(select(BrainTombstone.source_key, BrainTombstone.purged_at).where(
                BrainTombstone.reason == PURGED_BY_ADMIN, BrainTombstone.source_key.like("%#entry-%")))).all():
            found = ENTRY.search(key)
            if found:
                purged[found.group(1)] = max(purged.get(found.group(1), 0), await run_at(at))
        if purged:
            for item_id, key, run_id in (await s.execute(select(MemoryItem.id, MemoryItem.source_key,
                                                                MemoryItem.import_run_id).where(
                    live, MemoryItem.source_key.like("%#entry-%")))).all():
                found = ENTRY.search(key or "")
                if found and found.group(1) in purged and (run_id or 0) > purged[found.group(1)]:
                    ids.add(item_id)
    key = purge_key()
    if key:
        fps = {fp: at for fp, at in (await s.execute(select(BrainPurgedContent.fp, BrainPurgedContent.purged_at)
                                                       .where(live_fingerprints()))).all()}
        if fps:
            for item_id, sha, run_id in (await s.execute(select(MemoryItem.id, MemoryItem.source_sha256,
                                                                MemoryItem.import_run_id).where(
                    live, MemoryItem.source_sha256 != ""))).all():
                at = fps.get(purge_fp(key, sha))
                if at is not None and (run_id or 0) > await run_at(at):
                    ids.add(item_id)
    for item_id in sorted(ids):
        item = await s.get(MemoryItem, item_id)
        if item is not None:
            await store.quarantine_item(s, item, actor=actor, flag=PURGED_BEFORE,
                                        note="the same text as an item an admin purged")
    return len(ids)


async def repair_rows(s: AsyncSession, *, cause: str) -> dict[str, Any]:
    """brain_withhold_repair() (0008): every readable row that is the same message as a withheld one is withheld, and
    no withheld row or rollback copy keeps a fingerprint of its text. Marks what settle() finishes. Never takes the
    quote lock. Counts only (the function audits them when something changed)."""
    return dict((await s.execute(sql("SELECT brain_withhold_repair(:c)"), {"c": cause})).scalar() or {})


async def settle(s: AsyncSession, *, actor: str, cause: str) -> Counter[str]:
    """What only Python can do after repair_rows(): the memory items promoted from the rows it withheld are emptied
    and the quotes of them removed (archive.withhold_copies, which takes the quote lock only when there is something
    to remove), the marks cleared, the sessions it changed recomputed; and imported items that came back after a
    purge quarantined (R8). Counts only."""
    from . import archive

    py: Counter[str] = Counter()
    rows = (await s.execute(select(ArchiveMessage.id, ArchiveMessage.source_key, ArchiveMessage.session_id,
                                   ArchiveMessage.position).where(
        ArchiveMessage.withheld.is_(True),
        or_(ArchiveMessage.flags["withheld_by_admin"].has_key("pending_copies"),
            ArchiveMessage.flags.has_key("withheld_pending_copies"))).order_by(ArchiveMessage.id))).all()
    if rows:
        done = await archive.withhold_copies(s, messages=rows, actor=actor, cause="repair")
        py["copies_emptied"] += len(done.copies)
        py["copy_versions_blanked"] += done.versions_blanked
        py["rollback_copies_blanked"] += done.rollback_copies_blanked
        py["answers_scrubbed"] += done.answers_scrubbed
        await s.execute(sql(
            "UPDATE archive_messages SET flags = (flags - 'withheld_pending_copies') || CASE WHEN flags ? "
            "'withheld_by_admin' THEN jsonb_build_object('withheld_by_admin', (flags -> 'withheld_by_admin') - "
            "'pending_copies') ELSE '{}'::jsonb END WHERE id = ANY(:ids)"), {"ids": [int(r.id) for r in rows]})
        py["rows_settled"] += len(rows)
    pending_sessions = list((await s.scalars(select(ArchiveSession.id).where(
        ArchiveSession.flags.has_key("withhold_repair_pending")).order_by(ArchiveSession.id))).all())
    s.expire_all()
    for sid in pending_sessions:
        sess = await archive.recompute_session(s, sid)
        if sess is not None:
            flags = dict(sess.flags or {})
            flags.pop("withhold_repair_pending", None)
            sess.flags = flags
            py["sessions_recomputed"] += 1
    await s.flush()
    first = await s.get(BrainMaintenance, "purge_identity") is None
    py["purged_items_quarantined"] += await _purged_back(s, actor=actor, first=first)
    if first:
        await s.execute(pg_insert(BrainMaintenance).values(name="purge_identity", detail={"cause": cause})
                        .on_conflict_do_nothing(index_elements=[BrainMaintenance.name]))
    return py


async def repair(s: AsyncSession, *, actor: str, cause: str, refresh_quotes: bool = True) -> dict[str, Any]:
    """The identity repair (spec 12.1), idempotent: repair_rows(), settle(), then the start-up sweeps (promoted items
    of withheld messages that still hold text, rollback copies that still hold it) and, with refresh_quotes, every
    quote of a removed source. The caller holds the import lock (importer.GLOBAL_LOCK) and commits. An import repairs
    without refreshing quotes (a first import never takes the quote lock when it has nothing to remove; the start-up
    check refreshes them even while an import runs). Counts only, audited when the Python steps changed something."""
    from . import archive, quotes

    counts = await repair_rows(s, cause=cause)
    py = await settle(s, actor=actor, cause=cause)
    swept = await archive.sweep_withheld_copies(s, actor=actor, cause=cause)
    py["copies_emptied"] += len(swept)
    py["rollback_copies_blanked"] += await archive.sweep_withheld_records(s)
    if refresh_quotes:
        py["answers_scrubbed"] += await quotes.refresh_quotes(s)
    py = Counter({k: v for k, v in py.items() if v})
    if py:
        await audit.record(s, actor, "brain.withhold_identity_repaired", "brain", "", {"cause": cause, **dict(py)})
    await s.flush()
    return {**counts, **dict(py)}


async def pending(s: AsyncSession) -> dict[str, Any]:
    """What the repair would change now and what is left to look at, read-only (brain_withhold_pending() and the
    counts only Python makes). Counts only."""
    from . import quotes

    if not await s.scalar(sql("SELECT to_regprocedure('brain_withhold_pending()') IS NOT NULL")):
        return {"installed": False}
    out: dict[str, Any] = {"installed": True,
                           **dict((await s.execute(sql("SELECT brain_withhold_pending()"))).scalar() or {})}
    out["readable_rows_matching_a_withhold"] = (int(out.get("readable_by_key", 0))
                                                + int(out.get("readable_by_jarvis_row", 0))
                                                + int(out.get("readable_by_fold", 0))
                                                + int(out.get("readable_in_restricted_sessions", 0)))
    out["withheld_copies_with_text_fingerprint"] = int(await s.scalar(sql(
        "SELECT count(*) FROM import_records x WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb "
        "AND coalesce(x.before -> 'deleted_row' ->> 'withheld', x.before ->> 'withheld') = 'true' "
        "AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp', '') NOT IN ('') "
        "AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp') NOT LIKE 'w1:%'")) or 0)
    out["rows_blanked_by_the_database_guard"] = int(await s.scalar(select(func.count()).select_from(ArchiveMessage)
                                                                    .where(ArchiveMessage.flags["withheld_by_admin"]
                                                                           .has_key("guard"))) or 0)
    out["quotes_of_removed_sources"] = await quotes.removed_sources(s)
    out["purge_fingerprints_other_key"] = (await PurgeIndex.load(s, check=False)).other_key
    return out
