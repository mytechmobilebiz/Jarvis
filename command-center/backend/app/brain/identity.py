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
CONTAINS_SHORT = "same message id, contains the withheld text"  # a hold: the withheld text inside its own
COPY_UNCOMPARED = "copied by JARVIS, nothing to compare"  # a hold: a copy of a withheld row the backup lost
# A hold: it carries the withheld message's uid, but the withheld original is not in this backup (or not stored
# here), so its text could not be compared. Never a member: only provenance makes one (spec 2.9, rules062-confirm).
UID_UNCOMPARED = "same message id, nothing to compare"
# A hold: Hermes restated the withheld request after a compaction boundary (REPLAY_HEADER) as a NEW row with a new
# uid (an earlier carrier's request): it contains the withheld text, and no uid or fold links it.
RESTATED = "restated after a compaction, new message id"
# A fold member (Hermes absorbed_message_uids): its own uid is the SURVIVOR's message, not the withheld one, so no
# rule follows that uid except to the fold's own copies (the same text).
FOLD = "merged into another message"
FOLD_VIAS = (FOLD, "upgrade: " + FOLD)
# A member (the lead's decision, deviation 61 of 2026-10-21): Hermes's proactive prune rewrote the admin's own withheld
# JARVIS row in place (hermes_state_prune_rewrite.rewrite_pruned_rows: the row keeps its id, uid, role, call id and
# timestamp, its content becomes the stub) and first INSERTed its pre-prune self as an archived twin (every column
# copied, compacted=1). That twin is the very row the admin withheld: Hermes provenance, not text matching.
PRUNE_TWIN = "pruned by JARVIS: the original"
PRUNABLE_ROLES = ("tool", "assistant")  # the only rows rewrite_pruned_rows rewrites in place
# A member the upgrade made from a carried-on link an importer older than 1.1.0 recorded (0.6.0, 0.6.1 paired rows by
# their text alone, with no uid check: review 2, S6). Certain until an import compares its uid with the backup
# (importer.Processor._check_text_only_carried): another Hermes message is then released, audited.
TEXT_ONLY_CARRIED = "upgrade: carried copy, text only"
CARRIED_UID_SINCE = (1, 1, 0)  # the first importer whose carried-on pairing checks uids (0.6.2)
# A hold (review 2, S2): a stored row of the withheld message's own JARVIS conversation with its role, call id and very
# time, when no row of the withheld message is stored here (decisions-apply on live; an import whose backup lost the
# withheld row). Hermes keeps a row's timestamp when it writes it again (a compaction's tail, replace_messages, /retry),
# but nothing here can compare the text.
SAME_TIME = "same conversation, role and time; nothing to compare"
HOLD_VIA = ("same message id, different text", CONTAINS_SHORT, "same text", COPY_UNCOMPARED, UID_UNCOMPARED,
            RESTATED, "same message id", "upgrade: same message id", "upgrade: same text", SAME_TIME)


def importer_older(version: str | None, floor: tuple[int, ...] = CARRIED_UID_SINCE) -> bool:
    """Whether an import run's importer_version ('1.0.1') is older than floor (none or unparsable: older)."""
    found = re.match(r"^(\d+)\.(\d+)\.(\d+)", version or "")
    return found is None or tuple(int(x) for x in found.groups()) < floor
# What a hold's rule tells the admin (the holds list): the evidence, honestly; never text.
EVIDENCE = {
    "same message id, different text": "same message id; its text differs from the withheld message's",
    CONTAINS_SHORT: "same message id; its text contains the withheld text and more",
    UID_UNCOMPARED: "same message id; the withheld original is not in this backup (its row is gone, or JARVIS "
                    "rewrote it since), so the text could not be compared; probably the same message",
    "same message id": "same message id; the text could not be compared here; probably the same message",
    "upgrade: same message id": "same message id (found by the upgrade); the text could not be compared yet",
    RESTATED: "a compaction restated the withheld request under a new message id: it contains the withheld text",
    "same text": "identical text, but nothing links it to the withheld message",
    "upgrade: same text": "identical text (found by the upgrade), but nothing links it to the withheld message",
    COPY_UNCOMPARED: "copied by JARVIS from a withheld row this backup no longer holds; nothing to compare",
    SAME_TIME: "the withheld message's own JARVIS conversation, role and time (JARVIS keeps a row's time when it "
               "writes it again), but no row of the withheld message is here to compare the text with; probably the "
               "same message",
}
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


RELEASE_KEY_LABEL = b"infinite-ai/jarvis-brain/release-binding/v1"


def release_key(settings: Settings | None = None) -> bytes | None:
    """The key of release digests, derived from the database password like purge_key() (pg_dump never holds it).
    None without a password: a release is then bound by its uid and length only."""
    password = make_url((settings or get_settings()).database_url).password
    if not password:
        return None
    return hmac.new(RELEASE_KEY_LABEL, str(password).encode("utf-8"), hashlib.sha256).digest()


def release_prefix(key: bytes | None) -> str:
    """'r1:<key id>:', the prefix of the release digests made with this key ('' without a key): a digest made with
    another database password (an owner's rotation, a restore onto another installation, the test copy seeded from
    live) is recognised as such, never taken for other content (review 2, S11). The key id as purge_key_id()."""
    return "r1:%s:" % hmac.new(key, b"key id", hashlib.sha256).hexdigest()[:8] if key else ""


def release_fp(text: tuple[str, str, str, str], key: bytes | None = None) -> str:
    """The keyed digest of a RELEASED row's stored readable text (message_text()): 'r1:<key id>:<hex>', or '' without
    a key. Only ever computed from readable text (the released row is readable); an exemption whose row is withheld
    again loses it (unbind_withheld)."""
    key = key if key is not None else release_key()
    if not key:
        return ""
    return release_prefix(key) + hmac.new(key, json.dumps(list(text), ensure_ascii=False).encode("utf-8"),
                                          hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Binding:
    """The content an admin's release covers (brain_withhold_exemptions): the released row's uid, its length and,
    once readable, the keyed digest of its stored readable text. A field not known yet (-1, '') does not narrow it."""
    uid: str = ""
    chars: int = -1
    fp: str = ""

    def covers(self, chars: int | None, text: tuple[str, str, str, str] | None = None,
               key: bytes | None = None) -> bool:
        """Whether a row with this length (and, when it is readable, this stored text) is the content released. A
        digest made with another key (another database password, or a 0.6.2 build before key ids) cannot be compared:
        the release is bound by its uid and length only until bind_exemptions() binds it again under this key, as a
        release carried from another installation is."""
        if self.chars >= 0 and chars is not None and self.chars != chars:
            return False
        if self.fp and text is not None:
            key = key if key is not None else release_key()
            if not key or not self.fp.startswith(release_prefix(key)):
                return True
            return release_fp(text, key) == self.fp
        return True


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
    at: datetime | None = None  # the time of the JARVIS row it is bound to (Hermes keeps it in every copy)
    uid_learned_at: datetime | None = None  # a learned uid: Hermes's time of the newest row in the learning backup

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
                 at=r.source_at, uid_learned_at=r.uid_learned_at)


class Identities:
    """Every stone and exemption, indexed for the rules (a few hundred rows). An import adds the stones it inherits
    (add()), so later sessions of the same run see them."""

    def __init__(self, stones: Iterable[Stone] = (), exempt: Iterable[tuple[str, str, str]] = (),
                 bindings: Mapping[tuple[str, str, str], Binding] | None = None) -> None:
        self.by_key: dict[str, Stone] = {}
        self.by_row: dict[tuple[str, str], list[Stone]] = {}
        self.by_uid: dict[str, list[Stone]] = {}
        self.groups: dict[str, list[Stone]] = {}
        self.exempt: set[tuple[str, str, str]] = set(exempt)
        # (group, session, row) -> the content that release covers (Binding; absent: not known, the row id only)
        self.bindings: dict[tuple[str, str, str], Binding] = dict(bindings or {})
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
        rows = (await s.execute(select(
            BrainWithholdExemption.anchor_key, BrainWithholdExemption.source_session_id,
            BrainWithholdExemption.source_row_id, BrainWithholdExemption.source_uid,
            BrainWithholdExemption.content_chars, BrainWithholdExemption.content_fp))).all()
        return cls(stones, [(r[0], r[1], r[2]) for r in rows],
                   {(r[0], r[1], r[2]): Binding(r[3] or "", int(r[4] if r[4] is not None else -1), r[5] or "")
                    for r in rows})

    def released(self, group: str, session_id: str, row_id: str, chars: int | None,
                 text: Callable[[], tuple[str, str, str, str] | None] | None = None,
                 key: bytes | None = None) -> bool:
        """Whether an admin's release for this group covers JARVIS row (session_id, row_id) holding this content: the
        release names the row, and the row holds the content released (its length; its stored readable text once
        the release's digest is known: `text` gives it, None when it cannot be read). A later version of the row
        with other text is not covered."""
        ident = (group, session_id[:200], str(row_id)[:200])
        if ident not in self.exempt:
            return False
        b = self.bindings.get(ident)
        if b is None:
            return True
        if b.chars >= 0 and chars is not None and b.chars != chars:
            return False
        return b.covers(chars, text() if (b.fp and text is not None) else None, key)

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

    def learn_uid(self, st: Stone, uid: str, absorbed: tuple[str, ...] = (),
                  learned_at: datetime | None = None) -> Stone:
        """A stone bound to a row whose uid it did not know: the uid of that row in this backup (learned, so
        uid_known stays false; learned_at: Hermes's time of the newest row in that backup), and the uids that row
        absorbed."""
        new = st
        if uid and not st.uid:
            new = Stone(**{**st.__dict__, "uid": uid, "uid_learned_at": learned_at})
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

    def independent(self, anchor: str, uid: str, at: datetime | None = None) -> bool:
        """R-text's guard: this row carries its own uid, another one than every uid of the withheld message's group (a
        separate Hermes message), when that message's uid was known when the admin decided; or, when it was only
        learned since (Hermes's uid backfill gives every legacy row a random uid, so a uid-less-era copy or repeat
        carries one too), when the row was written after the backup the uid was learned from: its time `at` is after
        the newest row there (Hermes's own clock), so its uid is its own, never a backfilled one (review 2, S9). A
        row of the uid-less era (at or before that time) stays held: nothing tells it apart."""
        if not uid or uid in self.group_uids(anchor):
            return False
        if self.known_uids(anchor):
            return True
        learned = [st.uid_learned_at for st in self.groups.get(anchor, ())
                   if st.uid and not st.uid_known and st.uid_learned_at is not None]
        return bool(learned) and at is not None and at > max(learned)

    def uid_stones(self, uid: str, role: str, *, folds: bool = False) -> list[Stone]:
        """The certain stones with this uid and an equal or unknown role, the admin's own first. A hold's uid is the
        held row's own and says nothing about the withheld message, so it never matches by itself; nor does a fold
        member's (FOLD_VIAS: its uid is the survivor's own message) unless `folds` (its own copies, by equal text)."""
        return sorted((st for st in self.by_uid.get(uid, ()) if st.certain and st.role in ("", role)
                       and (folds or st.via not in FOLD_VIAS)),
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
            # a fold member's own uid is another message (FOLD_VIAS): only the withheld message's uid, absorbed
            found = [s for s in self.by_uid.get(u, ()) if s.certain and s.via not in FOLD_VIAS] or (
                [self.absorbed[u]] if u in self.absorbed else [])
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
              "title_hidden": binding.title_hidden, "source_at": binding.at, "uid_learned_at": binding.uid_learned_at}
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
    if binding.at is not None and binding.row:  # the bound row's time, once known (never changed after)
        await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == binding.key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.source_at.is_(None), BrainTombstone.source_session_id == binding.session[:200],
            BrainTombstone.source_row_id == binding.row[:200]).values(source_at=binding.at))
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
                 via=via, review=review, bound=True, after_run=after_run, at=row.at)


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
    """Every stored row that is the same JARVIS message as `message`, to a fixed point from each member. A member
    (withheld for good, never listed) needs provenance; everything uncertain is a hold (withheld, listed, releasable):
    - the same JARVIS row (session, row id, role): a member (a key the importer changed: uid backfill, renumbering);
    - the same uid (source_uid, or the uid its key names) and role: a member when its stored text equals `text` (or
      is Hermes's restatement of it under that uid: REPLAY_HEADER, then the text); otherwise a hold: 'contains the
      withheld text' when it does, 'different text' else. A row an admin released for this group is left as it is
      while it holds the content released (Binding), unless its text is the message's own. A same-uid row that is
      already withheld is left alone (no text to compare: the next import compares it);
    - a copy JARVIS made in a DIRECT continuation or branch child (a gateway branch, a rotation of uid-less rows: a
      fresh uid): the same role and text at the member's very time, at or before the child's start, and not carrying
      the uid of another stored row of the family (that row is what was copied): a member ('copied by JARVIS'). A
      grandchild's copy is reached through the member copy in the child, never directly (a donor timestamp gives the
      child's copy of ANOTHER row this member's time);
    - a fold that absorbed a member's uid (Hermes absorbed_message_uids: a compaction carrier, a micro-compaction
      merge): it holds the text, a member ('merged into another message'). A fold's own uid is the survivor's message
      (another Hermes message): from a fold only its own copies (the same uid AND its own text) are followed;
    - the importer's duplicates of any row the member's key was stored as (a carried-on copy, an adjacent repeat): a
      member, or a key with no row (Group.rowless);
    - a row of it an import deleted, kept only as a rollback copy under a key no stored row holds (deleted_copies():
      the same uid with the same text; without uids, the rewound twin with the same text; from a fold, only its own
      text): a key with no row (Group.rowless), so the withhold blanks that copy and a rollback puts the row back
      withheld. A deleted row that only shares the uid (other text, an archived generation included) gets no stone:
      its rollback copy loses the text at once (Group.uid_records, as the start-up sweep's uid rule does), and a
      rollback puts it back as a hold (match_row's uid branch), never a member;
    - Hermes's restatement of the request under a NEW uid (an earlier carrier restated after a second compaction:
      REPLAY_HEADER, then the text) in the message's conversation family: a hold ('restated ..., new message id').
    Holds are not followed. Identical text elsewhere is never part of the group (identical_rows: the admin's choice).
    Locked FOR UPDATE with lock (a promote of one waits, then is refused). BrainError past GROUP_CAP rows."""
    from . import archive
    from .store import BrainError

    out = Group()
    found: dict[int, GroupRow] = {}
    keys: set[str] = {message.source_key}
    rowless: dict[str, Stone] = {}
    uid_records: set[tuple[str, str]] = set()
    # (row, its JARVIS session, fold: its uid is another message's, own: its text when fold and readable)
    frontier: list[tuple[ArchiveMessage, str, bool, tuple[str, str, str, str] | None]] = [
        (message, sess.source_session_id, False, None)]
    released: dict[tuple[str, str], Binding] = {
        (sid, rid): Binding(uid or "", int(chars if chars is not None else -1), fp or "")
        for sid, rid, uid, chars, fp in (await s.execute(select(
            BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id,
            BrainWithholdExemption.source_uid, BrainWithholdExemption.content_chars,
            BrainWithholdExemption.content_fp).where(
            BrainWithholdExemption.anchor_key == message.source_key))).all()} if message.source_key else {}
    key = release_key() if any(b.fp for b in released.values()) else None
    family = await lineage(s, [sess.source_session_id])
    uid_seen: dict[str, bool] = {}  # (uid, conversations) -> whether a stored row there carries it (copy guard)
    request = restated(text[2]) or text[2]  # what a restatement of this message repeats

    def locked(q: Any) -> Any:
        return q.with_for_update(of=ArchiveMessage) if lock else q

    def exempt(row: ArchiveMessage, sid: str) -> bool:
        b = released.get((sid, (row.source_message_id or "")[:200]))
        return b is not None and b.covers(row.content_chars, None if row.withheld else message_text(row), key)

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

    def same_uid(uid: str, role: str, *, readable: bool = False) -> Any:
        q = (select(ArchiveMessage, ArchiveSession.source_session_id)
             .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
             .where(or_(ArchiveMessage.source_uid == uid, ArchiveMessage.source_key == UID_KEY + uid),
                    ArchiveMessage.role == role))
        if readable:
            q = q.where(ArchiveMessage.withheld.is_(False))
        return locked(q.order_by(ArchiveMessage.id))

    while frontier:
        nxt: list[tuple[ArchiveMessage, str, bool, tuple[str, str, str, str] | None]] = []
        for row, sid, fold, own in frontier:
            # The same JARVIS row (any archive row stored for it, whatever its key).
            if sid and row.source_message_id:
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveSession.source_session_id == sid,
                               ArchiveMessage.source_message_id == row.source_message_id,
                               ArchiveMessage.role == row.role).order_by(ArchiveMessage.id)))).all():
                    if take(other, other_sid, "same JARVIS row", CERTAIN):
                        nxt.append((other, other_sid, fold, own))
            uid = row_uid(row)
            if uid and fold:
                # A fold's own uid is the survivor's message: only the fold's own copies (its text) hold the text.
                if own is not None:
                    for other, other_sid in (await s.execute(same_uid(uid, row.role, readable=True))).all():
                        if message_text(other) == own and take(other, other_sid, FOLD, CERTAIN):
                            nxt.append((other, other_sid, True, own))
            elif uid:
                # The same Hermes uid (copies keep it: compaction generations, rotation, a CLI branch, prune twins).
                for other, other_sid in (await s.execute(same_uid(uid, row.role))).all():
                    if other.id == message.id or (other.id in found and found[other.id].review == CERTAIN):
                        continue
                    if other.withheld:
                        continue  # nothing to compare here: the next import compares it with the backup
                    again = restated(other.content)
                    content = norm(other.content)
                    if message_text(other) == text or (again and again == request and other.role == text[0]):
                        if take(other, other_sid, "same message id", CERTAIN):
                            nxt.append((other, other_sid, False, None))
                    elif exempt(other, other_sid):
                        continue  # an admin released this content: no shortcut overrides that (I4)
                    elif text[2] and text[2] in content:
                        take(other, other_sid, CONTAINS_SHORT, PENDING)
                    else:
                        take(other, other_sid, "same message id, different text", PENDING)
                # A fold that absorbed this uid holds the text.
                for other, other_sid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveMessage.absorbed_uids.contains([uid])).order_by(ArchiveMessage.id)))).all():
                    if take(other, other_sid, FOLD, CERTAIN):
                        nxt.append((other, other_sid, True, None if other.withheld else message_text(other)))
            # Copies JARVIS made into a DIRECT continuation or branch child with a fresh uid: the same text at this
            # row's very time (Hermes keeps a copied row's timestamp), at or before the child's start.
            want = own if fold else text
            if sid and row.at is not None and want is not None:
                kids = sorted(c for c in (await s.scalars(select(ArchiveSession.source_session_id).where(
                    ArchiveSession.parent_source_session_id == sid, ArchiveSession.lineage.in_(LINEAGE)))).all() if c)
                for other, other_sid in ((await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveSession.source_session_id.in_(kids), ArchiveSession.lineage.in_(LINEAGE),
                               ArchiveSession.started_at.is_not(None), ArchiveMessage.at == row.at,
                               ArchiveMessage.at <= ArchiveSession.started_at, ArchiveMessage.role == row.role,
                               ArchiveMessage.withheld.is_(False)).order_by(ArchiveMessage.id)))).all()
                        if kids else []):
                    if message_text(other) != want or exempt(other, other_sid):
                        continue
                    other_uid = row_uid(other)
                    if other_uid and other_uid != uid and await uid_elsewhere(other_uid, [
                            c for c, how in (await lineage(s, [other_sid])).items() if how == "ancestor"]):
                        continue  # the copy of another row with this text and time: that row's uid says so
                    if take(other, other_sid, "copied by JARVIS", CERTAIN):
                        nxt.append((other, other_sid, fold, own))
            # The ORIGIN of a copy JARVIS made (the rule above, upwards; review 2, S14): this row sits in a DIRECT
            # continuation or branch child, at or before the child's start, so the parent's readable row with the
            # same role, call and text is the row JARVIS copied (a gateway /branch and a rotation of uid-less rows give
            # the copy a fresh uid or none: the uid rule cannot reach it). A /branch copies each row's time verbatim
            # (gateway _BRANCH_COPIED_FIELDS): the parent's row at this very time. A rotation gives a carried row the
            # time of the FIRST parent row with the same content (Hermes _carry_parent_timestamps, first match wins),
            # so the origin is the parent's row at this time or a later one with that text. A member only when exactly
            # one parent row can be it; otherwise each candidate is a hold (which one was copied cannot be told).
            # Text alone never decides: the copy's position and time must match a parent row's.
            if sid and row.at is not None and want is not None:
                me = (await s.execute(select(ArchiveSession.lineage, ArchiveSession.parent_source_session_id,
                                             ArchiveSession.started_at)
                                      .where(ArchiveSession.source_session_id == sid))).first()
                if (me is not None and me.lineage in LINEAGE and me.parent_source_session_id
                        and me.started_at is not None and row.at <= me.started_at):
                    same_text = [(o, osid) for o, osid in (await s.execute(locked(
                        select(ArchiveMessage, ArchiveSession.source_session_id)
                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                        .where(ArchiveSession.source_session_id == me.parent_source_session_id,
                               ArchiveMessage.role == row.role,
                               ArchiveMessage.tool_call_id == (row.tool_call_id or ""),
                               ArchiveMessage.withheld.is_(False)).order_by(ArchiveMessage.id)))).all()
                        if message_text(o) == want]
                    own_uid = uid if not fold else ""
                    timed = [(o, osid) for o, osid in same_text if o.at == row.at]
                    cands = (timed if me.lineage == "branch" else
                             [(o, osid) for o, osid in same_text if o.at is not None and o.at >= row.at])
                    if not timed or (own_uid and any(row_uid(o) == own_uid for o, _ in same_text)):
                        cands = []  # no parent row at this time; or one with its uid, which the uid rule took
                    sure = len(cands) == 1 and len(timed) == 1 and (me.lineage == "branch" or len(same_text) == 1)
                    for o, osid in cands:
                        if exempt(o, osid):
                            continue  # an admin released this content: no shortcut overrides that (I4)
                        if take(o, osid, "copied by JARVIS", CERTAIN if sure else PENDING):
                            nxt.append((o, osid, fold, own))
            # Rows of it an import deleted, kept only as a rollback copy (review 1 R29), under the same principle: a
            # member by provenance only (its uid and the very text, or without uids the importer's rewound twin);
            # from a fold, its own copies only (the fold's uid is the survivor's message).
            for key_, gone_sid, gone in (await deleted_copies(s, row, uid)) if want is not None else []:
                if key_ in keys or key_ in rowless or gone.get("withheld") is True:
                    continue
                gone_row = str(gone.get("source_message_id") or "")[:200]
                b = released.get((gone_sid, gone_row)) if gone_sid else None
                if b is not None and b.covers(gone.get("content_chars"), gone_text(gone), key):
                    continue  # an admin released that content of the JARVIS row for this withhold (I4)
                if gone_text(gone) != want:
                    if uid and not fold:  # only the uid links it (other text): no stone, its copy loses the text
                        uid_records.add((key_, uid))
                    continue
                parts = parse_key(key_)
                rowless[key_] = Stone(key=key_, session=gone_sid or parts.session or sess.source_session_id,
                                      row=gone_row or parts.row, role=row.role,
                                      tool_call_id=gone.get("tool_call_id") or "", uid=uid,
                                      via=FOLD if fold else "same message id" if uid else "copied by JARVIS",
                                      bound=True, at=when(gone.get("at")))
        # The importer's duplicates of any row the members' keys were stored as.
        members = [message] + [g.row for g in found.values() if g.review == CERTAIN]
        targets = {int(m.id) for m in members} | await archive._older_rows(
            s, sorted({m.source_key for m in members if m.source_key}))
        dup_keys: set[str] = set()
        # Carried-on links an importer older than 1.1.0 made (0.6.0, 0.6.1): paired by the text alone, with no uid
        # check, so never provenance against a uid (review 2, S6).
        text_only: set[str] = set()
        for chunk in (sorted(targets)[i:i + 1000] for i in range(0, len(targets), 1000)):
            for key_, reason_, version_ in (await s.execute(select(
                    ImportRecord.source_key, ImportRecord.reason, ImportRun.importer_version)
                    .outerjoin(ImportRun, ImportRun.id == ImportRecord.run_id).where(
                    ImportRecord.record_type == "message", ImportRecord.action == "duplicate",
                    ImportRecord.target_table == "archive_messages", ImportRecord.reason.in_(PROVENANCE_REASONS),
                    ImportRecord.target_id.in_(chunk)).order_by(ImportRecord.id))).all():
                if key_ and key_ not in keys and key_ not in rowless:
                    dup_keys.add(key_)
                    if reason_ == CARRIED and importer_older(version_):
                        text_only.add(key_)
        if dup_keys:
            by_key = {r.source_key: (r, sid) for r, sid in (await s.execute(locked(
                select(ArchiveMessage, ArchiveSession.source_session_id)
                .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                .where(ArchiveMessage.source_key.in_(sorted(dup_keys)))))).all()}
            group_uids = {u for u in (row_uid(m) for m in members) if u}
            for key_ in sorted(dup_keys):
                if key_ in text_only:
                    mine = row_uid(by_key[key_][0]) if key_ in by_key else parse_key(key_).uid
                    if mine and group_uids and mine not in group_uids:
                        continue  # paired by its text alone, and its own uid says it is another Hermes message
                if key_ in by_key:
                    row, sid = by_key[key_]
                    if take(row, sid, TEXT_ONLY_CARRIED if key_ in text_only else "duplicate", CERTAIN):
                        # a duplicate repeats its target's text: of a fold when that is not the message's own
                        mine = None if row.withheld else message_text(row)
                        dup_fold = mine is None or mine != text
                        nxt.append((row, sid, dup_fold, mine if dup_fold else None))
                else:  # a duplicate repeats its target's role (Processor.dup_fp): every member has the message's
                    parts = parse_key(key_)
                    rowless[key_] = Stone(key=key_, session=parts.session or sess.source_session_id, row=parts.row,
                                          role=message.role, uid=parts.uid,
                                          via=TEXT_ONLY_CARRIED if key_ in text_only else "duplicate",
                                          bound=bool(parts.row or parts.uid))
        frontier = nxt
    if request:
        # Hermes's restatement of this request under a NEW uid (an earlier carrier restated after a second
        # compaction: nothing absorbed, no uid shared) in the message's conversation family: it contains the withheld
        # text, but no provenance links it, so it is held for an admin.
        for other, other_sid in (await s.execute(locked(
                select(ArchiveMessage, ArchiveSession.source_session_id)
                .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                .where(ArchiveSession.source_session_id.in_(sorted(family)), ArchiveMessage.role == "user",
                       ArchiveMessage.withheld.is_(False),
                       func.strpos(ArchiveMessage.content, REPLAY_HEADER) > 0).order_by(ArchiveMessage.id)))).all():
            if other.id in found or restated(other.content) != request or exempt(other, other_sid):
                continue
            ouid = row_uid(other)
            owner = await s.scalar(select(ArchiveMessage.id).where(
                or_(ArchiveMessage.source_uid == ouid, ArchiveMessage.source_key == UID_KEY + ouid),
                ArchiveMessage.id != other.id).order_by(ArchiveMessage.id).limit(1)) if ouid else None
            if owner is not None:
                # Hermes restated the in-flight row under its OWN uid (_fresh_compaction_message_copy keeps it): the
                # restatement is that row's copy (a member's was taken above, by the same uid); a hold's restatement
                # is held with it; another message's stays as that message is (review 2, S7).
                if owner in found and found[owner].review == PENDING:
                    take(other, other_sid, found[owner].rule, PENDING)
                continue
            take(other, other_sid, RESTATED, PENDING)
    out.rows = [found[k] for k in sorted(found)]
    out.rowless = [rowless[k] for k in sorted(rowless)]
    out.uid_records = sorted(x for x in uid_records if x[0] not in rowless and x[0] not in keys)
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
            BrainTombstone.via.not_in(FOLD_VIAS),  # a fold's uid is the survivor's own message
            BrainTombstone.role.in_(("", message.role))))).all():
        if not await exempt_covers(s, anchor or key, sess.source_session_id, message):
            return True
    return False


async def exempt_covers(s: AsyncSession, group: str, session_id: str, row: ArchiveMessage,
                        key: bytes | None = None) -> bool:
    """Whether an admin's release for this group covers this stored row: the release names its JARVIS row and the row
    holds the content released (its length; its stored readable text once the release's digest is known). A withheld
    row is judged by its length (its text is not there to compare)."""
    e = await s.get(BrainWithholdExemption, (group, session_id or "", (row.source_message_id or "")[:200]))
    if e is None:
        return False
    b = Binding(e.source_uid or "", int(e.content_chars if e.content_chars is not None else -1), e.content_fp or "")
    return b.covers(row.content_chars, None if row.withheld else message_text(row),
                    key if key is not None or not b.fp else release_key())


async def bind_exemptions(s: AsyncSession, *, archive_session_id: int | None = None,
                          source_session_ids: Iterable[str] = ()) -> int:
    """Binds each release of these conversations to the content it released (Binding): an exemption whose JARVIS
    row is stored here gets that row's uid and length when it had none, and, once the row is readable (the release
    took effect), the keyed digest of its stored readable text. Never re-bound: a later version of the row with other
    text is not covered. Returns how many changed."""
    sids = set(source_session_ids)
    if archive_session_id is not None:
        sid = await s.scalar(select(ArchiveSession.source_session_id).where(ArchiveSession.id == archive_session_id))
        if sid:
            sids.add(sid)
    if not sids:
        return 0
    key = release_key()
    prefix = release_prefix(key)
    # A digest made with another key (another database password; a 0.6.2 build before key ids) is bound again under
    # this one once the released content is readable here (review 2, S11).
    todo = (await s.scalars(select(BrainWithholdExemption).where(
        BrainWithholdExemption.source_session_id.in_(sorted(sids)),
        or_(BrainWithholdExemption.content_chars < 0, BrainWithholdExemption.content_fp == "",
            *((~BrainWithholdExemption.content_fp.startswith(prefix),) if prefix else ()))))).all()
    if not todo:
        return 0
    changed = 0
    for e in todo:
        q = (select(ArchiveMessage).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
             .where(ArchiveSession.source_session_id == e.source_session_id,
                    ArchiveMessage.source_message_id == e.source_row_id))
        if archive_session_id is not None:
            q = q.where(ArchiveMessage.session_id == archive_session_id)
        rows = (await s.scalars(q.order_by(ArchiveMessage.id))).all()
        if len(rows) != 1:
            continue  # not stored here, or ambiguous: bound when it is
        row = rows[0]
        if e.content_chars >= 0 and e.content_chars != row.content_chars:
            continue  # another version of the row: the release does not cover it, and stays as it was
        if e.source_uid and row_uid(row) and e.source_uid != row_uid(row):
            continue
        new_chars = row.content_chars if e.content_chars < 0 else e.content_chars
        new_uid = e.source_uid or row_uid(row)
        stale = bool(e.content_fp) and bool(prefix) and not e.content_fp.startswith(prefix)
        if stale and row.withheld:
            continue  # bound again once the released content is readable here
        new_fp = (e.content_fp if not stale else "") or ("" if row.withheld else release_fp(message_text(row), key))
        if (new_chars, new_uid, new_fp) != (e.content_chars, e.source_uid, e.content_fp):
            e.content_chars, e.source_uid, e.content_fp = new_chars, new_uid[:200], new_fp
            changed += 1
    if changed:
        await s.flush()
    return changed


async def unbind_withheld(s: AsyncSession, rows: Iterable[tuple[str, str, tuple[str, str, str, str]]],
                          key: bytes | None = None) -> int:
    """The released content itself is withheld now ((JARVIS session, row id, the text it held), read before it was
    blanked): an exemption whose digest is of that very text keeps no digest of it (no fingerprint of withheld text;
    its length stays, which is none). A digest of OTHER content (the release covered another version) stays."""
    key = key if key is not None else release_key()
    prefix = release_prefix(key)
    n = 0
    for sid, rid, text in rows:
        fp = release_fp(text, key)
        if sid and rid and fp:
            # the digest of that very text, or one made with another key (it cannot be compared: it may be of it)
            n += (await s.execute(update(BrainWithholdExemption).where(
                BrainWithholdExemption.source_session_id == sid[:200],
                BrainWithholdExemption.source_row_id == rid[:200],
                or_(BrainWithholdExemption.content_fp == fp,
                    and_(BrainWithholdExemption.content_fp != "",
                         ~BrainWithholdExemption.content_fp.startswith(prefix)))).values(content_fp=""))).rowcount or 0
    return n


async def rows_for(s: AsyncSession, binding: Stone) -> tuple[list[ArchiveMessage], list[ArchiveMessage]]:
    """The stored rows a withhold carried in a decisions file names (decisions-apply, spec 9.2): the certain ones (its
    key; its JARVIS row: the binding's session and row id, with an equal or unknown role) and the rows that only share
    its uid (another JARVIS row: held for review, since nothing here can compare the text; the next import does).
    A row a release of its group covers (its JARVIS row, holding the content released) is left out of the uid ones
    (I4); a fold binding has none (its uid is the survivor's own message)."""
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
    if binding.uid and binding.via not in FOLD_VIAS:  # a fold's uid is the survivor's own message
        query = (select(ArchiveMessage, ArchiveSession.source_session_id)
                 .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                 .where(or_(ArchiveMessage.source_uid == binding.uid[:200],
                            ArchiveMessage.source_key == UID_KEY + binding.uid)))
        if binding.role:
            query = query.where(ArchiveMessage.role == binding.role)
        for r, sid in (await s.execute(query.order_by(ArchiveMessage.id))).all():
            if r.id not in certain and not await exempt_covers(s, binding.group, sid, r):
                uid_only.append(r)
    return [certain[k] for k in sorted(certain)], uid_only


async def folds_of(s: AsyncSession, binding: Stone) -> list[ArchiveMessage]:
    """The readable stored rows a Hermes fold merged a withheld message into (absorbed_uids holds its uid: a
    compaction carrier, a micro-compaction merge): they hold its text. None for a hold (its uid is the held row's)."""
    if not binding.uid or binding.review == PENDING or binding.via in FOLD_VIAS:
        return []  # a hold's uid is the held row's own; a fold's is the survivor's (another message)
    return list((await s.scalars(select(ArchiveMessage).where(
        ArchiveMessage.absorbed_uids.contains([binding.uid[:200]]), ArchiveMessage.withheld.is_(False))
        .order_by(ArchiveMessage.id))).all())


async def rewound_twins(s: AsyncSession, binding: Stone) -> list[tuple[ArchiveMessage, str]]:
    """The importer's twin link from stored data (decisions-apply, when no row of a carried withhold is stored here):
    an import deleted the message's row as rewound (Hermes's in-place compaction, a retry) and kept its rollback copy
    with its text; Hermes writes the copy in the very operation that rewinds the original, so only the readable rows
    of that conversation with that very text stored by THAT run are candidates (review 2, S10). The twin, a member:
    the candidate with the original's uid, else the candidate at this original's rank among that run's rewound
    originals of the same text (the importer pairs the n-th original with the n-th copy) unless their uids differ.
    That rank's candidate under another uid is a hold (a /retry re-send), and every other candidate a hold ('same text':
    nothing links it), unless it carries a uid of its own that differs from the original's (another Hermes message:
    nothing). Compared in memory only, while the rollback copy still holds the text."""
    if binding.review == PENDING:
        return []
    out: dict[int, tuple[ArchiveMessage, str]] = {}
    for rec_id, run_id, before in (await s.execute(select(ImportRecord.id, ImportRecord.run_id, ImportRecord.before)
                                                   .where(ImportRecord.source_key == binding.key,
                                                          ImportRecord.record_type == "message",
                                                          ImportRecord.reason == REWOUND_REASON)
                                                   .order_by(ImportRecord.id))).all():
        gone = (before or {}).get("deleted_row") or {}
        if not gone or gone.get("withheld") or not gone.get("session_id"):
            continue
        text = gone_text(gone)
        if not text[2] and text[3] == "[]":
            continue
        words = [w for w in text[2].split(" ") if w]
        query = select(ArchiveMessage).where(
            ArchiveMessage.session_id == int(gone["session_id"]), ArchiveMessage.withheld.is_(False),
            ArchiveMessage.role == text[0], ArchiveMessage.import_run_id == run_id)
        if words:
            query = query.where(func.strpos(ArchiveMessage.content, max(words, key=len)) > 0)
        sid = await s.scalar(select(ArchiveSession.source_session_id).where(
            ArchiveSession.id == int(gone["session_id"])))
        cands = [row for row in (await s.scalars(query.order_by(ArchiveMessage.position, ArchiveMessage.id))).all()
                 if message_text(row) == text]
        originals = [rid for rid, b in (await s.execute(select(ImportRecord.id, ImportRecord.before).where(
            DELETED_COPY, deleted_field("session_id") == str(gone["session_id"]), ImportRecord.run_id == run_id,
            ImportRecord.reason == REWOUND_REASON).order_by(ImportRecord.id))).all()
            if gone_text((b or {}).get("deleted_row") or {}) == text]
        rank = originals.index(rec_id) if rec_id in originals else -1
        theirs = gone.get("source_uid") or parse_key(binding.key).uid
        same = [row for row in cands if theirs and row_uid(row) == theirs]
        at_rank = cands[rank] if 0 <= rank < len(cands) else None
        twin = same[0] if same else (at_rank if at_rank is not None and not (
            row_uid(at_rank) and theirs and row_uid(at_rank) != theirs) else None)
        resend = at_rank if twin is None else None  # its twin under another uid: a re-send (/retry)
        for row in cands:
            if row.id in out or await exempt_covers(s, binding.group, sid or "", row):
                continue  # a row an admin released for this withhold
            if twin is not None and row.id == twin.id:
                out[row.id] = (row, CERTAIN)  # the copy Hermes wrote again
            elif resend is not None and row.id == resend.id:
                out[row.id] = (row, PENDING)
            elif not (row_uid(row) and theirs and row_uid(row) != theirs):
                out[row.id] = (row, PENDING)  # the same text, nothing links it: held (as an import's R-text)
    return [out[k] for k in sorted(out)]


def readable_origin(m: Any, s_: Any) -> Any:
    """SQL: the readable row of the parent conversation that copy-position row m of session s_ copies verbatim (the
    same role, call, time and text): JARVIS copied a readable row, never a withheld one."""
    o = ArchiveMessage.__table__.alias("origin")
    p = ArchiveSession.__table__.alias("origin_session")
    return select(o.c.id).select_from(o.join(p, p.c.id == o.c.session_id)).where(
        p.c.source_session_id == s_.parent_source_session_id, o.c.withheld.is_(False), o.c.role == m.role,
        o.c.tool_call_id == m.tool_call_id, o.c.at == m.at, o.c.content == m.content,
        o.c.tool_calls == m.tool_calls).exists()


async def same_time_rows(s: AsyncSession, binding: Stone) -> list[tuple[ArchiveMessage, str]]:
    """decisions-apply, when no stored row of a carried withhold was found by its key, its JARVIS row, its uid or the
    importer's twin link (review 2, S1/S2): the readable rows at the very role, call id and time of the JARVIS row it
    names (at_row; Hermes keeps a row's time in every copy it writes: a compaction's tail, replace_messages, /retry, a
    /branch, a rotation), each a hold, never a member (nothing here can compare the text):
    - in that JARVIS conversation: SAME_TIME, unless its own uid differs from the withheld message's;
    - in the copied history of a continuation or branch (at or before its start) elsewhere: COPY_UNCOMPARED, unless
      its parent holds the readable row it copies (the same role, call, time and text: a copy of that row), or its own
      uid is another stored conversation's row's (a uid-keeping copy of that row).
    Never a row an admin released for the group."""
    if binding.at is None or not binding.role or not binding.session or binding.review == PENDING:
        return []
    out: list[tuple[ArchiveMessage, str]] = []
    for row, sess_ in (await s.execute(
            select(ArchiveMessage, ArchiveSession).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .where(ArchiveMessage.at == binding.at, ArchiveMessage.role == binding.role,
                   ArchiveMessage.tool_call_id == binding.tool_call_id, ArchiveMessage.withheld.is_(False))
            .order_by(ArchiveMessage.id))).all():
        uid = row_uid(row)
        if sess_.source_session_id == binding.session:
            if uid and binding.uid and uid != binding.uid:
                continue
            rule = SAME_TIME
        elif (sess_.lineage in LINEAGE and sess_.started_at is not None and row.at <= sess_.started_at
              and sess_.parent_source_session_id):
            if await s.scalar(select(readable_origin(row, sess_))):
                continue
            if uid and uid != binding.uid and await s.scalar(select(ArchiveMessage.id).where(
                    ArchiveMessage.source_uid == uid, ArchiveMessage.session_id != row.session_id).limit(1)):
                continue
            rule = COPY_UNCOMPARED
        else:
            continue
        if await exempt_covers(s, binding.group, sess_.source_session_id, row):
            continue
        out.append((row, rule))
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
                                                + int(out.get("readable_in_restricted_sessions", 0))
                                                + int(out.get("readable_copies_at_a_withheld_time", 0)))
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
