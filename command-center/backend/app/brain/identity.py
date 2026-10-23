"""The identity of a withheld JARVIS message (docs/JARVIS_BRAIN_SPEC.md 2.9; migration 0008_brain_withhold_identity).

An admin's withhold is bound to the JARVIS message, not only to the importer's derived source_key. Every part of that
identity is one more brain_tombstones row with reason WITHHELD_BY_ADMIN (a "stone"): its key, the JARVIS row it named
(session id, row id, role, tool_call_id, time, message_uid), the admin's own stone it belongs to (anchor_key: '' for the
admin's own decision, which is its own group), how it joined (via), its review state and, for a hold, the computed
evidence (relations: plan 4.6):
- a member ('') is certain: stored without text and never releasable;
- a hold ('pending') is uncertain: stored without text too, and listed for an admin who keeps or releases it;
- 'kept': a hold an admin confirmed, permanent from then on.
Every reader that honours a tombstone by key (the import, a rollback, decisions-export, promote, withheld_source(), the
start-up sweep) therefore honours an inherited stone as well; the database trigger archive_messages_keep_withheld()
keeps any row a stone names without text, whatever code writes it.

No text fingerprint of a withheld message is stored anywhere: a withheld row and its rollback copies keep
source_fp = 'w1:<state>' (withheld_fp()). Text is compared only in memory, against the backup in hand (Settle, an
import) or against stored text that is still readable (an admin's withhold).

0.6.2 "decide once, replay exactly, hold what is new" (RECOMMENDATION.md section 4): an admin's withhold blanks at once
the exact row, the same uid with the same text and the importer's recorded duplicates, and holds (pre-ticked) the
stored rows of the conversation family that equal or contain the withheld text (group_rows, matching_rows: plan 4.5).
Settle (app/brain/settle.py) then decides the decision backup once, by provenance only (plan 4.2), and holds every
other related row with computed evidence (relations(): plan 4.3, 4.6); an import of that backup replays the result word
for word (importer.Processor.replay_rows). This module holds the stones and exemptions in memory (Identities), writes
stones (put_stone), computes the evidence, runs the repair (repair: the SQL function brain_withhold_repair() of 0008,
then the steps only Python can do), and keeps purged items purged when they come back under another key (PurgeIndex:
keyed fingerprints of their bytes, HMAC with a key derived from the database password).

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
    BrainDecisionBackup,
    BrainMaintenance,
    BrainPurgedContent,
    BrainSessionLink,
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
MIN_CONTAINED = 32  # characters, normalised: a memory item "contains the withheld text" (the withhold preview)
CERTAIN, PENDING, KEPT = "", "pending", "kept"
# A fold member (Hermes absorbed_message_uids): its own uid is the SURVIVOR's message, not the withheld one, so no
# rule follows that uid (plan 4.2 (3): fold stones are never uid sources; review 3, rv3-res-6).
FOLD = "merged into another message"
FOLD_VIAS = (FOLD, "upgrade: " + FOLD)
CARRIED_UID_SINCE = (1, 1, 0)  # the first importer whose carried-on pairing checks uids (0.6.2)
# Holds the 0.6.2 engines write (plan 4.3, 4.5, 5.3; the counters of brain_withhold_pending() read the first two):
SETTLE_HOLD = "found when finishing decisions"  # Settle (plan 4.3)
LATER_HOLD = "new in a later backup"  # an import of a later backup in a withheld conversation family (plan 5.3)
MATCHING_HOLD = "matches the withheld text"  # the withhold dialog's pre-ticked rows (plan 4.5)
UID_HOLD = "same message id, other text"  # the withhold dialog: the same uid, other text (best effort)
# decisions-apply on a database whose newest import of the family is of another JARVIS backup than the decision's:
# the fail-safe hold of its stored rows at the withheld row's role, call and time or with its uid (plan 5.3 R5/S2/S1)
OTHER_BACKUP_HOLD = "decided on another backup"
# What a hold's via may be (a decisions file's hold keeps its own; older vias of 0.6.2 development builds are read).
HOLD_VIA = (SETTLE_HOLD, LATER_HOLD, MATCHING_HOLD, UID_HOLD, OTHER_BACKUP_HOLD, "same message id, different text",
            "same message id, contains the withheld text", "same text", "copied by JARVIS, nothing to compare",
            "same message id, nothing to compare", "restated after a compaction, new message id", "same message id",
            "upgrade: same message id", "upgrade: same text", "same conversation, role and time; nothing to compare")


def importer_older(version: str | None, floor: tuple[int, ...] = CARRIED_UID_SINCE) -> bool:
    """Whether an import run's importer_version ('1.0.1') is older than floor (none or unparsable: older)."""
    found = re.match(r"^(\d+)\.(\d+)\.(\d+)", version or "")
    return found is None or tuple(int(x) for x in found.groups()) < floor


LINEAGE = ("continuation", "branch")  # a child that copied its parent's history (never "delegated")
ENTRY = re.compile(r"#entry-([0-9a-f]{16})(?:-[0-9]+)?$")  # a memory entry's key ends with its digest
NOTE = "withheld by an admin"
LEFT_READABLE = "left readable by an admin (identical text seen at the withhold)"  # an exemption's reason (S21)
# Hermes's restatement of an unfinished request after a compaction boundary (agent/context_compressor.py
# _INFLIGHT_TASK_REPLAY_HEADER): the same message (same message_uid), its text after this fixed header.
REPLAY_HEADER = ("[STILL IN PROGRESS \u2014 this is the active request, restated after the compaction boundary "
                 "because it was not finished yet. Continue it; do not start over.]")
TITLE_SUFFIX = re.compile(r" #[0-9]+$")  # Hermes names a branch '<title> #N' (get_next_title_in_lineage)


def now() -> datetime:
    return datetime.now(UTC)


def when(value: Any) -> datetime | None:
    """A time read back from JSON (a rollback copy's 'at', a decisions file's 'at_row'): an aware datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None
    return at if at is None or at.tzinfo is not None else at.replace(tzinfo=UTC)


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


# --- Evidence (0.6.2 plan 4.6): one computed relation, read after peeling Hermes's wrappers ---------------------------
# A hold carries the relations of its text to the withheld text, computed by comparison in memory (never stored but as
# these codes): never a rule's name. Hermes wraps a user turn (gateway/run_inbound.py _prepend_inbound_reply_context):
# the Discord triggering note outermost, then the reply pointer '[Replying to: "<quote>"]' (the replied-to message in
# full or in part: N1), then the authored text; a compaction restates an unfinished request after REPLAY_HEADER.

MIN_RELATED = 16  # characters, normalised: 'contains' and 'part of (reply quote)' need at least this much
IDENTICAL_REL, CONTAINS, PART_OF, RESTATED_REL = "identical", "contains", "part_of", "restated"
SAME_UID, SAME_CALL, SAME_TIME_REL, REUSED_ROW = "same_uid", "same_call", "same_time", "reused_row"
STALE, ORPHAN, NOT_IN_BACKUP, NO_RELATION = "stale", "orphan", "not_in_backup", "none"
TEXT_RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF)
RELATIONS = (*TEXT_RELATIONS, SAME_UID, SAME_CALL, SAME_TIME_REL, REUSED_ROW, STALE, ORPHAN, NOT_IN_BACKUP, NO_RELATION)
# Releasing a hold with one of these needs a typed confirmation (plan 4.6): its text is, holds or quotes the withheld
# text.
CONFIRM_RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF)
CONFIRM_WORD = "RELEASE"
# What each relation says, in words an admin reads (never text of either row).
RELATION_TEXT = {
    IDENTICAL_REL: "identical text",
    RESTATED_REL: "the withheld text restated after a compaction",
    CONTAINS: "contains the withheld text",
    PART_OF: "a reply that quotes part of the withheld text",
    SAME_UID: "the same JARVIS message id, other text",
    SAME_CALL: "the same tool call, other text",
    SAME_TIME_REL: "the same time and role",
    REUSED_ROW: "a reused row number",
    STALE: "a stale stored row at the withheld message's role and time",
    ORPHAN: "an orphan row",
    NOT_IN_BACKUP: "the withheld text is not in this backup",
    NO_RELATION: "no relation to the withheld text was found",
}
_DISCORD_NOTE = re.compile(r"\A\[Triggering message id: [^\n]*\]\n\n")
_REPLY_POINTER = re.compile(r'\A\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)


def flat(text: str | None) -> str:
    """Whitespace collapsed (any Unicode space), as the evidence compares texts."""
    return " ".join((text or "").split())


def peel(text: str | None) -> tuple[str, str | None, bool]:
    """Hermes's wrappers off a row's text: the Discord triggering note, the reply pointer (its quote returned) and the
    replay header (the text after the last one). (body, quote or None, restated)."""
    body = _DISCORD_NOTE.sub("", text or "", count=1)
    quote = None
    found = _REPLY_POINTER.match(body)
    if found:
        quote, body = found.group(1), body[found.end():]
    again = REPLAY_HEADER in body
    if again:
        body = body.rsplit(REPLAY_HEADER, 1)[1]
    return body.strip(), quote, again


def text_relations(held: str | None, withheld: str | None) -> set[str]:
    """The relations of a row's text to a withheld text, computed by comparison after the peel (plan 4.6): identical
    (the same words, or the same authored text inside Hermes's wrappers); restated (identical after the replay header);
    contains (the withheld text, at least MIN_RELATED characters, inside the authored text); part_of (a reply pointer
    quoting at least MIN_RELATED characters of the withheld text: N1). Compared in memory only."""
    out: set[str] = set()
    whole, want = flat(held), flat(withheld)
    if not want:
        return out
    body, quote, again = peel(held)
    wbody = flat(peel(withheld)[0])
    if whole == want or (wbody and flat(body) == wbody):
        out.add(IDENTICAL_REL)
        if whole != want and (again or REPLAY_HEADER in (withheld or "")):
            out.add(RESTATED_REL)
    if len(wbody) >= MIN_RELATED and wbody in flat(body or held):
        out.add(CONTAINS)
    if quote is not None and len(flat(quote)) >= MIN_RELATED and flat(quote) in wbody:
        out.add(PART_OF)
    return out


def relation_order(codes: Iterable[str]) -> list[str]:
    """Relation codes in their fixed order (RELATIONS), duplicates removed."""
    found = set(codes)
    return [c for c in RELATIONS if c in found]


def evidence_text(codes: Iterable[str]) -> str:
    """What a hold's relations say, in one line (never text)."""
    return "; ".join(RELATION_TEXT[c] for c in relation_order(codes)) or RELATION_TEXT[NO_RELATION]


def compare_text(codes: Iterable[str]) -> str:
    """The holds list's one-word comparison of the texts (from the relations)."""
    found = set(codes)
    for code, words in ((IDENTICAL_REL, "identical text"), (RESTATED_REL, "the withheld text restated"),
                        (CONTAINS, "contains the withheld text"),
                        (PART_OF, "part of the withheld text (reply quote)")):
        if code in found:
            return words
    return "nothing to compare" if NOT_IN_BACKUP in found else "other text"


def needs_confirmation(codes: Iterable[str]) -> bool:
    return bool(set(codes) & set(CONFIRM_RELATIONS))


# --- Exact identity (0.6.2 plan 3.1) ---------------------------------------------------------------------------------
# The identity of a JARVIS row: (session, row id, role, tool_call_id, time), plus its uid when both sides have one.
# Every match against a stored decision (a stone, a release) and every 'same JARVIS row' rule uses it, here, in the
# repair and in the database guard (migration 0008: brain_exact(), brain_row_uid()). Hermes reuses row ids when a
# state.db snapshot comes back (sqlite_sequence with it): the new message at a reused row id has another time and its
# own uid, so it is never the withheld one (review 3, rv3-ov-0). Hermes never changes a stored row's time or uid in
# place (a prune keeps them: hermes_state_prune_rewrite._PRUNE_FIXED_COLUMNS; the uid backfill fills NULLs only).


class Exact(NamedTuple):
    session: str
    row: str
    role: str
    tool_call_id: str
    at: datetime | None
    uid: str


def exact(session: str | None, row: Any, role: str | None, tool_call_id: str | None, at: datetime | None,
          uid: str | None) -> Exact:
    """An exact identity, cut to the stored widths (session, row id, tool_call_id and uid 200, role 40)."""
    return Exact((session or "")[:200], str(row if row is not None else "")[:200], (role or "")[:40],
                 (tool_call_id or "")[:200], at, (uid or "")[:200])


def exact_of_source(session_id: str, m: Any) -> Exact:
    """The exact identity of a backup row (source.SourceMessage) as the importer stores it (message_values)."""
    return exact(session_id, m.id, brain_text.sanitize(m.role), brain_text.sanitize(m.tool_call_id), m.at, m.uid)


def exact_of_row(row: Any, source_session_id: str) -> Exact:
    """The exact identity of a stored archive row."""
    return exact(source_session_id, row.source_message_id, row.role, row.tool_call_id, row.at, row_uid(row))


def exact_of_stone(st: Stone) -> Exact:
    return exact(st.session, st.row, st.role, st.tool_call_id, st.at, st.uid)


def binding_matches(b_role: str, b_call: str, b_at: datetime | None, b_uid: str,
                    role: str, call: str, at: datetime | None, uid: str) -> bool:
    """brain_exact() of migration 0008: a binding (a stone's, a release's) and a row of the same JARVIS session and row
    id are the same JARVIS row. A binding whose role is not known (a version 3 file's key) matches any role and call;
    a time is compared when both have one; two different uids never match."""
    if b_role and (b_role != (role or "") or (b_call or "") != (call or "")):
        return False
    if b_at is not None and at is not None and b_at != at:
        return False
    return not (b_uid and uid and b_uid != uid)


def same_row(binding: Exact, row: Exact) -> bool:
    """Whether a binding names this very JARVIS row (identity.exact): the same session and row id, and
    binding_matches()."""
    return (bool(binding.session) and bool(binding.row) and (binding.session, binding.row) == (row.session, row.row)
            and binding_matches(binding.role, binding.tool_call_id, binding.at, binding.uid,
                                row.role, row.tool_call_id, row.at, row.uid))


@dataclass(frozen=True)
class Binding:
    """The exact JARVIS row an admin's release (or 'left readable') covers (brain_withhold_exemptions): its role,
    tool_call_id, time and uid. A field not known ('' or None: a version 4 file's release) does not narrow it. A
    release covers that row only, never another message at a reused row id."""
    role: str = ""
    tool_call_id: str = ""
    at: datetime | None = None
    uid: str = ""
    decided_on: str = ""  # the JARVIS backup the release was decided on: the content it covers is that backup's

    def covers(self, row: Exact) -> bool:
        return binding_matches(self.role, self.tool_call_id, self.at, self.uid, row.role, row.tool_call_id, row.at,
                               row.uid)


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
    uid_known: bool = False  # the uid was known when the admin decided
    via: str = ""
    review: str = CERTAIN
    bound: bool = False
    after_run: int = 0
    title_hidden: bool = False  # the admin hid the conversation's title with this withhold
    at: datetime | None = None  # the time of the JARVIS row it is bound to (Hermes keeps it in every copy)
    decided_on: str = ""  # the JARVIS backup (SHA-256) the decision was taken on ('' unknown: never replayed)
    kept_by: str = ""  # who kept a hold, and when (review 'kept')
    kept_at: datetime | None = None
    relations: tuple[str, ...] = ()  # a hold's computed evidence (relation codes: plan 4.6); never text

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
                 bound=bool(r.bound), after_run=int(r.after_run or 0), title_hidden=bool(r.title_hidden),
                 at=r.source_at, decided_on=r.decided_on or "", kept_by=r.kept_by or "", kept_at=r.kept_at,
                 relations=tuple(getattr(r, "relations", None) or ()))


class Identities:
    """Every stone and exemption, indexed for the rules (a few hundred rows). An import adds the stones it inherits
    (add()), so later sessions of the same run see them."""

    def __init__(self, stones: Iterable[Stone] = (), exempt: Iterable[tuple[str, str, str]] = (),
                 bindings: Mapping[tuple[str, str, str], list[Binding]] | None = None) -> None:
        self.by_key: dict[str, Stone] = {}
        self.by_row: dict[tuple[str, str], list[Stone]] = {}
        self.by_uid: dict[str, list[Stone]] = {}
        self.groups: dict[str, list[Stone]] = {}
        self.exempt: set[tuple[str, str, str]] = set(exempt)
        # (group, session, row) -> the exact rows the releases of that JARVIS row id cover (absent: none known)
        self.bindings: dict[tuple[str, str, str], list[Binding]] = {k: list(v) for k, v in (bindings or {}).items()}
        self.sessions: set[str] = set()
        self.version: Counter[str] = Counter()
        for st in stones:
            self._index(st)

    @classmethod
    async def load(cls, s: AsyncSession) -> Identities:
        stones = [_stone(r) for r in (await s.execute(select(BrainTombstone).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN).order_by(BrainTombstone.source_key))).scalars().all()]
        rows = (await s.execute(select(
            BrainWithholdExemption.anchor_key, BrainWithholdExemption.source_session_id,
            BrainWithholdExemption.source_row_id, BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id,
            BrainWithholdExemption.source_at, BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on)
            .order_by(BrainWithholdExemption.id))).all()
        bindings: dict[tuple[str, str, str], list[Binding]] = {}
        for r in rows:
            bindings.setdefault((r[0], r[1], r[2]), []).append(
                Binding(r[3] or "", r[4] or "", r[5], r[6] or "", r[7] or ""))
        return cls(stones, [(r[0], r[1], r[2]) for r in rows], bindings)

    def released(self, group: str, row: Exact, *, backup: str | None = None, same_content: bool = False) -> bool:
        """Whether an admin's release for this group covers this exact JARVIS row (identity.exact): a release names
        its session and row id, and its binding matches the row's role, tool_call_id, time and uid. Another message at
        a reused row id is not covered. For a row of a backup (backup: its SHA-256) the release covers the content it
        was decided on: only a release decided on that very backup, unless the stored row already shows that very
        content (same_content: readable, from the same source row). Hermes rewrites a row in place (a prune keeps its
        id, uid, role, call and time) and an older backup holds its earlier text: never covered by a release of
        another version (review 2 S27's case without a digest; rules062-confirm c0, x8)."""
        ident = (group, row.session, row.row)
        if ident not in self.exempt:
            return False
        found = self.bindings.get(ident)
        if found is None:
            return backup is None or same_content
        return any(b.covers(row) and (backup is None or same_content or (bool(b.decided_on)
                                                                         and b.decided_on == backup))
                   for b in found)

    def row_stones(self, row: Exact) -> list[Stone]:
        """The stones bound to this exact JARVIS row (identity.exact), certain first, then the admin's own, by key."""
        return sorted((st for st in self.by_row.get((row.session, row.row), ()) if same_row(exact_of_stone(st), row)),
                      key=lambda st: (not st.certain, st.anchor != "", st.key))

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
        if new.at is None and st.at is not None and (new.session, new.row) == (st.session, st.row):
            new = Stone(**{**new.__dict__, "at": st.at})
        if new != old:
            self._unindex(old)
            self._index(new)
        return new

    def uid_stones(self, uid: str, role: str, *, folds: bool = False) -> list[Stone]:
        """The certain stones with this uid and an equal or unknown role, the admin's own first. A hold's uid is the
        held row's own and says nothing about the withheld message, so it never matches by itself; nor does a fold
        member's (FOLD_VIAS: its uid is the survivor's own message) unless `folds` (its own copies, by equal text)."""
        return sorted((st for st in self.by_uid.get(uid, ()) if st.certain and st.role in ("", role)
                       and (folds or st.via not in FOLD_VIAS)),
                      key=lambda st: (st.anchor != "", st.key))

    def match_row(self, key: str, session_id: str, row_id: str, role: str, uid: str, *, tool_call_id: str = "",
                  at: datetime | None = None) -> Match | None:
        """A stored row (a rollback puts it back) against the stones, without its text: its key has a stone, or its
        exact JARVIS row is a stone's binding (the stone's review); or it shares a withheld message's uid (a hold: the
        text cannot be compared here; the next import compares it). The database guard's rules are a subset."""
        st = self.by_key.get(key) if key else None
        if st is not None:
            return Match(st, "key", _review(st))
        for st in self.row_stones(exact(session_id, row_id, role, tool_call_id, at, uid)):
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
        row_stones = self.row_stones(exact_of_source(session_id, m))
        row_match = Match(row_stones[0], "same JARVIS row", _review(row_stones[0])) if row_stones else None
        if row_match is not None and row_match.review == CERTAIN:
            return row_match
        fold = None
        for u in m.absorbed:
            # a fold member's own uid is another message (FOLD_VIAS): only the withheld message's uid, absorbed
            found = [s for s in self.by_uid.get(u, ()) if s.certain and s.via not in FOLD_VIAS]
            if found:
                fold = Match(found[0], FOLD, CERTAIN)
                break
        # A fold that KEPT the withheld uid (Hermes: the composite keeps the uid of the constituent whose text comes
        # first) holds the withheld text and another message's: R-uid holds it for review ('contains the withheld
        # text'), never a member by the uid alone.
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
              "title_hidden": binding.title_hidden, "source_at": binding.at, "decided_on": binding.decided_on[:64],
              "kept_by": binding.kept_by[:200], "kept_at": binding.kept_at,
              "relations": list(relation_order(binding.relations))}
    stmt = pg_insert(BrainTombstone).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BrainTombstone.source_key],
        set_={"review": stmt.excluded.review, "via": stmt.excluded.via, "anchor_key": stmt.excluded.anchor_key,
              "kept_by": stmt.excluded.kept_by, "kept_at": stmt.excluded.kept_at,
              "relations": stmt.excluded.relations},
        where=and_(BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == PENDING,
                   stmt.excluded.review != PENDING))
    changed = bool((await s.execute(stmt)).rowcount)
    if binding.decided_on:  # the decision backup, once known (never replaced: a decision is replayed where it was taken)
        await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == binding.key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.decided_on == "").values(decided_on=binding.decided_on[:64]))
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
    if binding.at is not None and binding.row:  # the bound row's time, once known (never changed after)
        await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == binding.key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.source_at.is_(None), BrainTombstone.source_session_id == binding.session[:200],
            BrainTombstone.source_row_id == binding.row[:200]).values(source_at=binding.at))
    return changed


async def update_hold(s: AsyncSession, key: str, *, via: str, relations: Iterable[str], decided_on: str = "") -> bool:
    """A hold's evidence as Settle computed it again (plan 4.3, 4.6): its via and relations (and the backup it was
    decided on, once known). Only a pending stone; never a member. Whether it changed."""
    values: dict[str, Any] = {"via": via[:200], "relations": relation_order(relations)}
    done = await s.execute(update(BrainTombstone).where(
        BrainTombstone.source_key == key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
        BrainTombstone.review == PENDING).values(**values))
    if decided_on:
        await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.decided_on == "").values(decided_on=decided_on[:64]))
    return bool(done.rowcount)


# --- Decision backups (0.6.2 plan 1.3, 3.2, 4.4) -----------------------------------------------------------------------


async def reopen_decision_backup(s: AsyncSession, sha256: str) -> None:
    """A new decision taken on a backup whose decisions were finished (Settle): that backup needs finishing again
    (brain_decision_backups.settled_run_id back to NULL), so the new decision is never counted as settled and an
    export waits for it (plan 4.7)."""
    if sha256:
        await s.execute(update(BrainDecisionBackup).where(BrainDecisionBackup.sha256 == sha256[:64])
                        .values(settled_run_id=None))


def run_sha256(source: Any) -> str:
    """The JARVIS backup's SHA-256 an import run recorded (import_runs.source -> db -> sha256), or ''."""
    db = (source or {}).get("db") if isinstance(source, dict) else None
    sha = (db or {}).get("sha256") if isinstance(db, dict) else None
    return sha.lower() if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha) else ""


async def decision_backup(s: AsyncSession, source_session_ids: Iterable[str]) -> str:
    """The JARVIS backup a decision on these conversations is taken on: the newest import run (not rolled back) that
    imported, updated or re-read a conversation of their family (identity.lineage), by its backup's SHA-256 ('' when
    none is known). An import of that very file replays the decision (importer.Processor: replay)."""
    family = await lineage(s, [sid for sid in source_session_ids if sid])
    if not family:
        return ""
    ids = list((await s.scalars(select(ArchiveSession.id).where(
        ArchiveSession.source_session_id.in_(sorted(family))))).all())
    if not ids:
        return ""
    run_id = await s.scalar(select(func.max(ImportRecord.run_id)).join(ImportRun, ImportRun.id == ImportRecord.run_id)
                            .where(ImportRecord.record_type == "session",
                                   ImportRecord.target_table == "archive_sessions",
                                   ImportRecord.target_id.in_(ids), ImportRun.status != "rolled_back"))
    if run_id is None:
        return ""
    sha = run_sha256(await s.scalar(select(ImportRun.source).where(ImportRun.id == run_id)))
    if sha:
        await note_decision_backup(s, sha)
    return sha


async def note_decision_backup(s: AsyncSession, sha256: str) -> None:
    """A JARVIS backup a decision was taken on (brain_decision_backups): its label, importer version and newest message
    time as the first import run of that file here recorded them. Never changed once written."""
    if not sha256:
        return
    first = (await s.execute(select(ImportRun.id, ImportRun.label, ImportRun.importer_version, ImportRun.source)
                             .where(ImportRun.source["db"]["sha256"].astext == sha256)
                             .order_by(ImportRun.id).limit(1))).first()
    newest = when(((first.source or {}).get("db") or {}).get("newest_at")) if first is not None else None
    await s.execute(pg_insert(BrainDecisionBackup).values(
        sha256=sha256[:64], label=((first.label if first is not None else "") or "")[:200],
        importer_version=((first.importer_version if first is not None else "") or "")[:40], newest_at=newest,
        first_run_id=first.id if first is not None else None).on_conflict_do_nothing())


async def current_after_run(s: AsyncSession) -> int:
    """The newest import run when a decision is taken: rows a run up to it stored were seen by the admin."""
    return int(await s.scalar(select(func.coalesce(func.max(ImportRun.id), 0))) or 0)


# --- An admin's withhold: the stored rows of the same message (spec 8.1) ---------------------------------------------

GROUP_CAP = 1000  # stored rows of one message a withhold follows (a JARVIS message is stored a handful of times)


def row_uid(row: Any) -> str:
    """The JARVIS uid of a stored row: its source_uid, or the uid its key names ('' unknown)."""
    return (getattr(row, "source_uid", "") or "") or parse_key(row.source_key).uid


def message_text(row: Any) -> tuple[str, str, str, str]:
    """What makes two stored rows the same text: role, tool_call_id, whitespace-normalised content and tool calls.
    Compared in memory only, while the text is still readable; never stored."""
    return (row.role or "", row.tool_call_id or "", norm(row.content),
            json.dumps(row.tool_calls or [], sort_keys=True))


def binding_of(row: Any, source_session_id: str, *, anchor: str = "", via: str = "", review: str = CERTAIN,
               after_run: int = 0, uid_known: bool = False, decided_on: str = "") -> Stone:
    """The stone for a stored row: its key and the exact JARVIS row it is (session, row id, role, tool_call_id, time,
    uid), and the backup the decision was taken on."""
    return Stone(key=row.source_key, anchor=anchor, session=source_session_id or "", row=row.source_message_id or "",
                 role=row.role or "", tool_call_id=row.tool_call_id or "", uid=row_uid(row), uid_known=uid_known,
                 via=via, review=review, bound=True, after_run=after_run, at=row.at, decided_on=decided_on)


@dataclass
class GroupRow:
    row: ArchiveMessage
    source_session_id: str
    rule: str  # how it joined: same JARVIS row, same message id, duplicate; a hold: UID_HOLD, MATCHING_HOLD
    review: str  # '' member, 'pending' hold
    relations: tuple[str, ...] = ()  # a hold's computed evidence (plan 4.6)


@dataclass
class Group:
    """The stored rows that are the same message as the one an admin withholds (the message itself left out), and
    the keys of its copies that have no row (importer duplicates: a carried-on copy, an adjacent repeat)."""
    rows: list[GroupRow] = field(default_factory=list)
    rowless: list[Stone] = field(default_factory=list)
    # (key, uid) of rollback copies of deleted rows that only share the message's uid (other text): no stone (nothing
    # makes them the message), but their copies lose the text at the withhold, as the start-up sweep's uid rule does.
    uid_records: list[tuple[str, str]] = field(default_factory=list)

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
    """The query of deleted_copies() (None when there is nothing to look for: no uid)."""
    if not uid:
        return None
    conds = [DELETED_COPY, ImportRecord.source_key != "", deleted_field("role") == row.role,
             ~select(ArchiveMessage.id).where(ArchiveMessage.source_key == ImportRecord.source_key).exists(),
             or_(ImportRecord.source_key == UID_KEY + uid, deleted_field("source_uid") == uid)]
    return select(ImportRecord.source_key, ImportRecord.before).where(*conds).order_by(ImportRecord.id)


async def deleted_copies(s: AsyncSession, row: ArchiveMessage, uid: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(key, source session id, deleted row) of the rollback copies of rows an import deleted that may be this row's
    message, under a key no stored row holds: those of the same uid and role (Hermes keeps a message's uid in every
    copy: an in-place compaction's rewound original, a CLI branch child's rewound copy). Text is compared by the
    caller, in memory. Found through 0008's partial indexes on deleted rows (never a scan of every rollback copy)."""
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
    """Every stored row that is the same JARVIS message as `message` by provenance, to a fixed point from each member
    (0.6.2 plan 4.5: the immediate, best-effort blanking of a withhold; Settle decides the decision backup with the
    withheld text in memory, plan 4.2). The members (withheld for good, never listed):
    - the same JARVIS row (any archive row stored for it, whatever its key; identity.exact: a reused row id with
      another time or another uid is another message, review 3 rv3-ov-0);
    - the same uid (source_uid, or the uid its key names) and role with the very same text (message_text);
    - the importer's recorded duplicates of a member (a carried-on copy, an adjacent repeat; plan 4.5): a key with no
      row (Group.rowless), or a stored row when both carry the same non-empty uid (plan 4.2 (5));
    - a row of it an import deleted, kept only as a rollback copy under a key no stored row holds, with the same uid
      and the very same text (Group.rowless), so the withhold blanks that copy and a rollback puts the row back
      withheld.
    A stored readable row with the same uid and role but other text is held for review (UID_HOLD, with its computed
    evidence); a deleted row that only shares the uid gets no stone, its rollback copy loses the text at once
    (Group.uid_records). Nothing else joins here: a fold, a copy JARVIS made at a copy position, a restatement and every
    row matched by its text are the withhold dialog's pre-ticked holds (matching_rows) until Settle decides them. A row
    an admin released or left readable for this group stays as it is while it holds the content decided on. Locked FOR
    UPDATE with lock (a promote of one waits, then is refused). BrainError past GROUP_CAP rows."""
    from . import archive
    from .store import BrainError

    out = Group()
    found: dict[int, GroupRow] = {}
    keys: set[str] = {message.source_key}
    rowless: dict[str, Stone] = {}
    uid_records: set[tuple[str, str]] = set()
    frontier: list[tuple[ArchiveMessage, str]] = [(message, sess.source_session_id)]
    released: dict[tuple[str, str], list[Binding]] = {}
    for sid_, rid_, role_, call_, at_, uid_, on_ in (await s.execute(select(
            BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id,
            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id, BrainWithholdExemption.source_at,
            BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on).where(
            BrainWithholdExemption.anchor_key == message.source_key))).all() if message.source_key else ():
        released.setdefault((sid_, rid_), []).append(Binding(role_ or "", call_ or "", at_, uid_ or "", on_ or ""))

    def locked(q: Any) -> Any:
        return q.with_for_update(of=ArchiveMessage) if lock else q

    stored_shas: dict[int, str] = {}

    async def exempt(row: ArchiveMessage, sid: str) -> bool:
        here = exact_of_row(row, sid)
        found_ = [b for b in released.get((here.session, here.row), ()) if b.covers(here)]
        return bool(found_) and await release_covers_stored(s, found_, row, stored_shas)

    def take(row: ArchiveMessage, sid: str, rule: str, review: str, relations: Iterable[str] = ()) -> bool:
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
        found[row.id] = GroupRow(row, sid, rule, review, tuple(relation_order(relations)))
        keys.add(row.source_key)
        return review == CERTAIN

    while frontier:
        nxt: list[tuple[ArchiveMessage, str]] = []
        for row, sid in frontier:
            # The same JARVIS row (any archive row stored for it, whatever its key): its exact identity.
            if sid and row.source_message_id:
                mine = exact_of_row(row, sid)
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveSession.source_session_id == sid,
                               ArchiveMessage.source_message_id == row.source_message_id,
                               ArchiveMessage.role == row.role).order_by(ArchiveMessage.id)))).all():
                    if same_row(mine, exact_of_row(other, other_sid)) and take(other, other_sid, "same JARVIS row",
                                                                                  CERTAIN):
                        nxt.append((other, other_sid))
            uid = row_uid(row)
            if uid:
                # The same Hermes uid (copies keep it: compaction generations, rotation, a CLI branch): a member when
                # its text is the very text; otherwise a hold (a pruned stub, a fold that kept the uid: Settle decides).
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(or_(ArchiveMessage.source_uid == uid, ArchiveMessage.source_key == UID_KEY + uid),
                               ArchiveMessage.role == row.role).order_by(ArchiveMessage.id)))).all():
                    if other.id == message.id or (other.id in found and found[other.id].review == CERTAIN):
                        continue
                    if other.withheld:
                        continue  # nothing to compare here: Settle compares it with the backup
                    if message_text(other) == text:
                        if take(other, other_sid, "same message id", CERTAIN):
                            nxt.append((other, other_sid))
                    elif not await exempt(other, other_sid):
                        take(other, other_sid, UID_HOLD, PENDING,
                             {SAME_UID, *text_relations(other.content, message.content)})
            # Rows of it an import deleted, kept only as a rollback copy (review 1 R29): a member by provenance only
            # (its uid and the very text).
            for key_, gone_sid, gone in (await deleted_copies(s, row, uid)) if uid else []:
                if key_ in keys or key_ in rowless or gone.get("withheld") is True:
                    continue
                gone_row = str(gone.get("source_message_id") or "")[:200]
                gone_exact = exact(gone_sid, gone_row, gone.get("role"), gone.get("tool_call_id"), when(gone.get("at")),
                                   gone.get("source_uid") or parse_key(key_).uid)
                if gone_sid and any(b.covers(gone_exact) for b in released.get((gone_sid, gone_row), ())):
                    continue  # an admin released that exact JARVIS row for this withhold (I4)
                if gone_text(gone) != text:
                    uid_records.add((key_, uid))  # only the uid links it (other text): no stone, its copy loses text
                    continue
                parts = parse_key(key_)
                rowless[key_] = Stone(key=key_, session=gone_sid or parts.session or sess.source_session_id,
                                      row=gone_row or parts.row, role=row.role,
                                      tool_call_id=gone.get("tool_call_id") or "", uid=uid, via="same message id",
                                      bound=True, at=when(gone.get("at")))
        # The importer's recorded duplicates of any row the members' keys were stored as (plan 4.5): a key with no row
        # of its own is a member (nothing readable changes); a stored row only when it carries the member's own uid
        # (plan 4.2 (5)).
        members = [message] + [g.row for g in found.values() if g.review == CERTAIN]
        group_uids = {u for u in (row_uid(m) for m in members) if u}
        targets = {int(m.id) for m in members} | await archive._older_rows(
            s, sorted({m.source_key for m in members if m.source_key}))
        dup_keys: set[str] = set()
        for chunk in (sorted(targets)[i:i + 1000] for i in range(0, len(targets), 1000)):
            for key_ in (await s.scalars(select(ImportRecord.source_key).where(
                    ImportRecord.record_type == "message", ImportRecord.action == "duplicate",
                    ImportRecord.target_table == "archive_messages", ImportRecord.reason.in_(PROVENANCE_REASONS),
                    ImportRecord.target_id.in_(chunk)).order_by(ImportRecord.id))).all():
                if key_ and key_ not in keys and key_ not in rowless:
                    dup_keys.add(key_)
        if dup_keys:
            by_key = {r.source_key: (r, sid) for r, sid in (await s.execute(locked(
                select(ArchiveMessage, ArchiveSession.source_session_id)
                .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                .where(ArchiveMessage.source_key.in_(sorted(dup_keys)))))).all()}
            for key_ in sorted(dup_keys):
                mine = row_uid(by_key[key_][0]) if key_ in by_key else parse_key(key_).uid
                if key_ in by_key:
                    if not mine or mine not in group_uids:
                        continue  # a stored row with no uid or another one: only text links it (Settle decides)
                    row, sid = by_key[key_]
                    if take(row, sid, "duplicate", CERTAIN):
                        nxt.append((row, sid))
                else:  # a recorded duplicate with no row of its own (plan 4.5): its key keeps the withheld text out
                    parts = parse_key(key_)
                    rowless[key_] = Stone(key=key_, session=parts.session or sess.source_session_id, row=parts.row,
                                          role=message.role, uid=mine, via="duplicate",
                                          bound=bool(parts.row or parts.uid))
        frontier = nxt
    out.rows = [found[k] for k in sorted(found)]
    out.rowless = [rowless[k] for k in sorted(rowless)]
    out.uid_records = sorted(x for x in uid_records if x[0] not in rowless and x[0] not in keys)
    return out


async def family_sessions(s: AsyncSession, source_session_ids: Iterable[str]) -> set[str]:
    """The conversation family of these JARVIS sessions as this database knows it (plan 5.1, without a backup in
    hand): the closure over the archive's continuation and branch lineage (lineage(), either way) and the family links
    any import recorded (brain_session_links, never forgotten). A gateway reset and a delegated sub-agent are never
    family links."""
    out = {sid for sid in source_session_ids if sid}
    frontier = set(out)
    while frontier:
        found = set(await lineage(s, sorted(frontier)))
        ids = sorted(frontier)
        for a, b in (await s.execute(select(BrainSessionLink.a, BrainSessionLink.b).where(
                or_(BrainSessionLink.a.in_(ids), BrainSessionLink.b.in_(ids))))).all():
            found |= {a, b}
        frontier = {x for x in found if x and x not in out}
        out |= frontier
    return out


async def matching_rows(s: AsyncSession, message: ArchiveMessage, sess: ArchiveSession, *,
                        exclude: Iterable[int] = ()) -> list[tuple[ArchiveMessage, str, tuple[str, ...]]]:
    """The withhold dialog's pre-ticked rows (plan 4.5): every readable stored row of the message's conversation family
    (family_sessions) whose text has a relation to the withheld text (text_relations: identical, the withheld text
    restated, containing it, or a reply quoting part of it), other than the message and `exclude` (its group), with
    those relations; (row, JARVIS session, relations). Rows an admin released or left readable for this withhold are
    not listed. Compared in memory only, while both texts are readable."""
    sids = await family_sessions(s, [sess.source_session_id])
    sessions = dict((await s.execute(select(ArchiveSession.id, ArchiveSession.source_session_id).where(
        ArchiveSession.source_session_id.in_(sorted(sids))))).all()) or {sess.id: sess.source_session_id}
    skip = {int(message.id)} | {int(i) for i in exclude}
    want = flat(peel(message.content)[0])
    if not flat(message.content):
        return []
    words = [w for w in want.split(" ") if w]
    narrow = [ArchiveMessage.content.like("[Replying to%"), ArchiveMessage.content.like("[Triggering message id%")]
    if words:  # a word survives the normalisation: the database narrows the rows to compare
        narrow.append(func.strpos(ArchiveMessage.content, max(words, key=len)) > 0)
    query = select(ArchiveMessage).where(ArchiveMessage.session_id.in_(sorted(sessions)),
                                         ArchiveMessage.withheld.is_(False), or_(*narrow))
    released: list[tuple[str, str, Binding]] = [
        (sid_, rid_, Binding(role_ or "", call_ or "", at_, uid_ or "", on_ or ""))
        for sid_, rid_, role_, call_, at_, uid_, on_ in (await s.execute(select(
            BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id,
            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id, BrainWithholdExemption.source_at,
            BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on).where(
            BrainWithholdExemption.anchor_key == message.source_key))).all()] if message.source_key else []
    out = []
    for r in (await s.scalars(query.order_by(ArchiveMessage.id))).all():
        if r.id in skip:
            continue
        rel = text_relations(r.content, message.content)
        if not rel:
            continue
        sid = sessions.get(r.session_id, "")
        here = exact_of_row(r, sid)
        covering = [b for rsid, rrow, b in released if (rsid, rrow) == (here.session, here.row) and b.covers(here)]
        if covering and await release_covers_stored(s, covering, r):
            continue
        out.append((r, sid, tuple(relation_order(rel))))
    return out


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
    alone never matches (a row left readable stays promotable)."""
    if message.source_key and await s.scalar(select(BrainTombstone.source_key).where(
            BrainTombstone.source_key == message.source_key, BrainTombstone.reason == WITHHELD_BY_ADMIN)):
        return True
    if sess.source_session_id and message.source_message_id:
        here = exact_of_row(message, sess.source_session_id)
        for r in (await s.scalars(select(BrainTombstone).where(
                BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.source_session_id == here.session,
                BrainTombstone.source_row_id == here.row))).all():
            if same_row(exact_of_stone(_stone(r)), here):
                return True
    uid = row_uid(message)
    if not uid:
        return False
    for key, anchor in (await s.execute(select(BrainTombstone.source_key, BrainTombstone.anchor_key).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.message_uid == uid[:200],
            BrainTombstone.via.not_in(FOLD_VIAS),  # a fold's uid is the survivor's own message
            BrainTombstone.role.in_(("", message.role))))).all():
        if not await exempt_covers(s, anchor or key, sess.source_session_id, message):
            return True
    return False


async def stored_backup(s: AsyncSession, row: Any, cache: dict[int, str] | None = None) -> str:
    """The JARVIS backup (SHA-256) a stored row's content came from: the newest import run that stored or rewrote it
    (its import records), else the run that created it ('' unknown)."""
    if cache is not None and row.id in cache:
        return cache[row.id]
    run_id = await s.scalar(select(func.max(ImportRecord.run_id)).where(
        ImportRecord.target_table == "archive_messages", ImportRecord.target_id == row.id,
        ImportRecord.action.in_(("imported", "updated", "withheld")))) or row.import_run_id
    sha = run_sha256(await s.scalar(select(ImportRun.source).where(ImportRun.id == run_id))) if run_id else ""
    if cache is not None:
        cache[row.id] = sha
    return sha


async def release_covers_stored(s: AsyncSession, bindings: Iterable[Binding], row: Any,
                                cache: dict[int, str] | None = None) -> bool:
    """Whether one of these releases (each already matching the row's exact identity) covers the stored row's
    content: a release decided on the backup the row's content came from (stored_backup), or one with no decision
    backup (a version 4 file's release, or a decision older than the column: the row id alone, as before). Hermes
    rewrites a row in place and an older backup holds its earlier text: a release of another version never covers
    it (rules062-confirm c0, x8)."""
    found = list(bindings)
    if any(not b.decided_on for b in found):
        return True
    if not found:
        return False
    sha = await stored_backup(s, row, cache)
    return bool(sha) and any(b.decided_on == sha for b in found)


async def exempt_covers(s: AsyncSession, group: str, session_id: str, row: ArchiveMessage) -> bool:
    """Whether an admin's release (or 'left readable') for this group covers this exact stored row (identity.exact:
    the release names its JARVIS row, and its role, tool_call_id, time and uid match) and its content (the release was
    decided on the backup the row's content came from: release_covers_stored)."""
    here = exact_of_row(row, session_id or "")
    found = [b for b in (Binding(role or "", call or "", at, uid or "", on or "") for role, call, at, uid, on in (
        await s.execute(select(
            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id, BrainWithholdExemption.source_at,
            BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on).where(
            BrainWithholdExemption.anchor_key == group, BrainWithholdExemption.source_session_id == here.session,
            BrainWithholdExemption.source_row_id == here.row))).all()) if b.covers(here)]
    return bool(found) and await release_covers_stored(s, found, row)


def exemption_values(row: Exact, *, anchor: str, kind: str = "release", decided_on: str = "", reason: str = "",
                     decided_by: str = "", decided_at: datetime | None = None) -> dict[str, Any]:
    """The values of a brain_withhold_exemptions row for this exact JARVIS row (0.6.2 plan 1.2)."""
    out: dict[str, Any] = {"anchor_key": anchor, "source_session_id": row.session, "source_row_id": row.row,
                           "role": row.role, "tool_call_id": row.tool_call_id, "source_at": row.at,
                           "source_uid": row.uid, "kind": kind, "decided_on": (decided_on or "")[:64],
                           "reason": (reason or "released by an admin")[:200], "decided_by": (decided_by or "")[:200]}
    if decided_at is not None:
        out["decided_at"] = decided_at
    return out


async def put_exemption(s: AsyncSession, row: Exact, **kw: Any) -> bool:
    """Records a release (or 'left readable') of this exact JARVIS row for a withhold; one per exact row and group
    (the first decision stays). Whether it was added."""
    return bool((await s.execute(pg_insert(BrainWithholdExemption).values(**exemption_values(row, **kw))
                                 .on_conflict_do_nothing())).rowcount)


async def exact_rows(s: AsyncSession, binding: Stone) -> list[ArchiveMessage]:
    """The stored rows a withhold carried in a decisions file names (decisions-apply, spec 9.2): its key, and its exact
    JARVIS row (the binding's session and row id, identity.exact). Nothing else is re-derived from stored data (0.6.2
    plan step 6): the next import of a later backup holds what is new in the family, Settle decides the rest."""
    certain: dict[int, ArchiveMessage] = {}
    for r in (await s.scalars(select(ArchiveMessage).where(ArchiveMessage.source_key == binding.key))).all():
        certain[r.id] = r
    if binding.session and binding.row:
        query = (select(ArchiveMessage).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                 .where(ArchiveSession.source_session_id == binding.session[:200],
                        ArchiveMessage.source_message_id == binding.row[:200]))
        for r in (await s.scalars(query)).all():
            if same_row(exact_of_stone(binding), exact_of_row(r, binding.session)):
                certain[r.id] = r
    return [certain[k] for k in sorted(certain)]


async def other_backup_rows(s: AsyncSession, binding: Stone, *, exclude: Iterable[int] = ()
                            ) -> list[tuple[ArchiveMessage, str, tuple[str, ...]]]:
    """decisions-apply's fail-safe hold on a database whose newest import of the conversation family is of another
    JARVIS backup than the one the decision was taken on (0.6.2 plan 5.3, R5/S2/rv3-res-3: 'a different backup on live
    means a fail-safe hold'; S1 (c): 'on live with a different backup: held'): the readable stored rows of the
    decision's conversation family (family_sessions) that an import of another backup than binding.decided_on stored
    (a decision on no known backup, a version 4 file: every one), other than `exclude`; (row, JARVIS session,
    relations). Rows the decision backup itself stored are left to the carried stones (an exact replay). No text is
    compared (the decisions file holds none): each relation says the withheld text is not in this backup, with the
    same time and role, or the same uid, where that holds (a fold's own uid is the survivor's message: never)."""
    if not binding.session:
        return []
    sids = await family_sessions(s, [binding.session])
    sessions = dict((await s.execute(select(ArchiveSession.id, ArchiveSession.source_session_id).where(
        ArchiveSession.source_session_id.in_(sorted(sids))))).all())
    if not sessions:
        return []
    uid = binding.uid if binding.uid and binding.via not in FOLD_VIAS else ""
    skip = {int(i) for i in exclude}
    shas: dict[int | None, str] = {}
    out = []
    for r in (await s.scalars(select(ArchiveMessage).where(
            ArchiveMessage.session_id.in_(sorted(sessions)), ArchiveMessage.withheld.is_(False))
            .order_by(ArchiveMessage.id))).all():
        if r.id in skip:
            continue
        if r.import_run_id not in shas:
            shas[r.import_run_id] = run_sha256(await s.scalar(select(ImportRun.source).where(
                ImportRun.id == r.import_run_id))) if r.import_run_id is not None else ""
        if binding.decided_on and shas[r.import_run_id] == binding.decided_on:
            continue
        rel = {NOT_IN_BACKUP}
        if binding.at is not None and (r.role, r.at, r.tool_call_id or "") == (
                binding.role, binding.at, binding.tool_call_id):
            rel.add(SAME_TIME_REL)
        if uid and (r.source_uid or "") == uid[:200]:
            rel.add(SAME_UID)
        out.append((r, sessions[r.session_id], tuple(relation_order(rel))))
    return out


async def rewound_originals(s: AsyncSession, row: ArchiveMessage, text: tuple[str, str, str, str]) -> list[int]:
    """The rollback copies (import_records ids) of the rewound original that Hermes wrote this stored row again
    from (an in-place compaction's protected tail, a CLI /retry): the run that stored this row deleted that original
    as rewound, in the same conversation, and the copy holds this very text. The importer's twin link
    (plan_messages), from stored data, for a backup with no uid key to follow: the original with this row's uid when
    both have one (another uid is another message: never), else the one of this row's rank among that run's rows of
    the same text in the conversation. Compared in memory only, while this row's text is still readable. Found
    through 0008's partial index of deleted rows by conversation (the keys written literally: DELETED_COPY,
    deleted_field), never a scan of every rollback copy."""
    if not row.import_run_id or (not text[2] and text[3] == "[]"):
        return []
    cands: list[tuple[int, dict[str, Any]]] = []
    for rec_id, before in (await s.execute(select(ImportRecord.id, ImportRecord.before).where(
            DELETED_COPY, deleted_field("session_id") == str(row.session_id),
            ImportRecord.run_id == row.import_run_id, ImportRecord.reason == REWOUND_REASON)
            .order_by(ImportRecord.id))).all():
        gone = (before or {}).get("deleted_row") or {}
        if gone.get("withheld"):
            continue
        if (gone.get("role") or "", gone.get("tool_call_id") or "", norm(gone.get("content")),
                json.dumps(gone.get("tool_calls") or [], sort_keys=True)) == text:
            cands.append((int(rec_id), gone))
    mine = row_uid(row)
    if mine:
        same = [rid for rid, g in cands if (g.get("source_uid") or "") == mine]
        if same:
            return same
        cands = [(rid, g) for rid, g in cands if not g.get("source_uid")]
    if len(cands) <= 1:
        return [rid for rid, _ in cands]
    # Several originals with this text: the importer paired them with their copies in order (the n-th copy with the
    # n-th original). This row's rank among the run's rows of the conversation with the same role, call and length.
    peers = list((await s.scalars(select(ArchiveMessage.id).where(
        ArchiveMessage.session_id == row.session_id, ArchiveMessage.import_run_id == row.import_run_id,
        ArchiveMessage.role == row.role, ArchiveMessage.tool_call_id == (row.tool_call_id or ""),
        ArchiveMessage.content_chars == row.content_chars).order_by(ArchiveMessage.position, ArchiveMessage.id)))
        .all())
    if len(peers) == len(cands) and row.id in peers:
        return [cands[peers.index(row.id)][0]]
    return [rid for rid, _ in cands]  # cannot tell them apart: every rollback copy of this text loses it


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


async def legacy_rejected_hashes(s: AsyncSession, *, actor: str) -> int:
    """Review 3 (readers): 0.6.0 and 0.6.1 purged the incoming version of a changed file by appending its PLAIN
    SHA-256 to the existing item's flags.rejected_source_sha256 (0.6.2 keeps only a keyed fingerprint: S16). A version
    no memory item holds any more was purged: it is kept out by its keyed fingerprint instead (remember_purge, via
    'upgrade'), and the plain hash goes; a version an item still holds (keep_existing rejected it: its text is
    readable) stays as it is. Runs at the start-up check and before the legacy reset rolls the legacy runs back (which
    would delete the item, and the purge with it: the purged version came back at the next import). Without a
    database password nothing changes. How many hashes became keyed fingerprints."""
    key = purge_key()
    if not key:
        return 0
    n = 0
    for item in (await s.scalars(select(MemoryItem).where(MemoryItem.flags.has_key("rejected_source_sha256"))
                                 .order_by(MemoryItem.id))).all():
        flags = dict(item.flags or {})
        rejected = [h for h in (flags.get("rejected_source_sha256") or []) if isinstance(h, str) and h]
        keep = []
        for sha in rejected:
            if await s.scalar(select(MemoryItem.id).where(MemoryItem.source_sha256 == sha).limit(1)) is not None:
                keep.append(sha)
                continue
            await remember_purge(s, source_key="", sha256_hex=sha, kind=purge_kind(item.source_key or ""),
                                 actor=actor, via="upgrade", key=key)
            n += 1
        if keep != list(flags.get("rejected_source_sha256") or []):
            if keep:
                flags["rejected_source_sha256"] = keep
            else:
                flags.pop("rejected_source_sha256", None)
            item.flags = flags
    if n:
        await s.flush()
    return n


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
    no withheld row or rollback copy keeps a fingerprint of its text. Marks what finish_repair() finishes. Never takes the
    quote lock. Counts only (the function audits them when something changed)."""
    return dict((await s.execute(sql("SELECT brain_withhold_repair(:c)"), {"c": cause})).scalar() or {})


async def finish_repair(s: AsyncSession, *, actor: str, cause: str) -> Counter[str]:
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
    # Review 3 (readers): plain hashes a purge made before 0.6.2 left, and copies of redacted versions in conflicts.
    py["legacy_purge_hashes_keyed"] += await legacy_rejected_hashes(s, actor=actor)
    from .review import redact_all_conflict_copies

    py["conflict_copies_of_redacted_versions"] += await redact_all_conflict_copies(s)
    first = await s.get(BrainMaintenance, "purge_identity") is None
    py["purged_items_quarantined"] += await _purged_back(s, actor=actor, first=first)
    if first:
        await s.execute(pg_insert(BrainMaintenance).values(name="purge_identity", detail={"cause": cause})
                        .on_conflict_do_nothing(index_elements=[BrainMaintenance.name]))
    return py


async def repair(s: AsyncSession, *, actor: str, cause: str, refresh_quotes: bool = True) -> dict[str, Any]:
    """The identity repair (spec 12.1), idempotent: repair_rows(), finish_repair(), then the start-up sweeps (promoted items
    of withheld messages that still hold text, rollback copies that still hold it) and, with refresh_quotes, every
    quote of a removed source. The caller holds the import lock (importer.GLOBAL_LOCK) and commits. An import repairs
    without refreshing quotes (a first import never takes the quote lock when it has nothing to remove; the start-up
    check refreshes them even while an import runs). Counts only, audited when the Python steps changed something."""
    from . import archive, quotes

    counts = await repair_rows(s, cause=cause)
    py = await finish_repair(s, actor=actor, cause=cause)
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
