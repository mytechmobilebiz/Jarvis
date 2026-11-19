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

import functools
import hashlib
import hmac
import json
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import String, and_, any_, func, literal, literal_column, or_, select, update
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
# Option C (O-D1): a row of a conversation an uncertain link puts in a withheld group (app/brain/groups.py): held, never
# released in one click, and the export waits while one waits
UNCERTAIN_HOLD = "held with its conversation: an uncertain link to a withheld conversation"
# Option C (rebuild R2): a row of a conversation of a withheld group, held with its conversation by the central sweep
# (review.hold_group_rows), an import (importer.Processor._group_holds) or the database guard (0008)
GROUP_HOLD = "held with its conversation: a withheld conversation group"
# A rollback copy whose older text relates to a withheld text (review.hold_updated_copies, hold_deleted_copies): a
# rollback puts its row back as this hold, listed on its own (importer.rollback_run)
COPY_HOLD = "an earlier version related to the withheld text"
# Option C (rebuild R3): a row outside the withheld groups (or released for another withheld text) that carries a
# withheld text in a derived form (review.hold_carriers): held on its own, listed, released per row
CARRIER_HOLD = "carries the withheld text"
# What a hold's via may be (a decisions file's hold keeps its own; older vias of 0.6.2 development builds are read).
HOLD_VIA = (SETTLE_HOLD, LATER_HOLD, MATCHING_HOLD, UID_HOLD, OTHER_BACKUP_HOLD, UNCERTAIN_HOLD, GROUP_HOLD, COPY_HOLD,
            CARRIER_HOLD,
            "same message id, different text",
            "same message id, contains the withheld text", "same text", "copied by JARVIS, nothing to compare",
            "same message id, nothing to compare", "restated after a compaction, new message id", "same message id",
            "upgrade: same message id", "upgrade: same text", "same conversation, role and time; nothing to compare")


def importer_older(version: str | None, floor: tuple[int, ...] = CARRIED_UID_SINCE) -> bool:
    """Whether an import run's importer_version ('1.0.1') is older than floor (none or unparsable: older)."""
    found = re.match(r"^(\d+)\.(\d+)\.(\d+)", version or "")
    return found is None or tuple(int(x) for x in found.groups()) < floor


FAMILY_LINKS = ("parent", "branched_from", "compressed_from", "shared_uid", "copied_rows")  # never option C's own
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
UNCERTAIN_REL = "uncertain"  # option C (O-D1): the row's conversation is in a withheld group by an uncertain link
# option C (rebuild R2, RECON-14): a group row compared with its own withheld text but not with every other withheld
# text ('none' needs every withheld text in hand): never 'none', so never released in one click
OTHER_NOT_COMPARED = "other_not_compared"
# option C (rebuild R4, D3, N-A): a hold whose 'none' was computed before a later withhold (its withheld text was never
# compared with it): stale at once (mark_later_withhold), never 'none' again until every withheld text was in hand
LATER_WITHHOLD = "later_withhold"
# option C (rebuild R3, derived copies): a beginning of the withheld text cut short (Hermes's generic prune line, an
# f-string summary at its width, a truncated preview, a cut JSON or repr literal); and a text the canonical decode
# could not read in full (its caps: canon()), held fail-closed whenever a withheld text is in hand
CUT, NOT_CHECKED = "cut", "not_checked"
TEXT_RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF, CUT, NOT_CHECKED)
RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF, CUT, SAME_UID, SAME_CALL, SAME_TIME_REL, REUSED_ROW, STALE,
             ORPHAN, NOT_IN_BACKUP, NOT_CHECKED, UNCERTAIN_REL, LATER_WITHHOLD, OTHER_NOT_COMPARED, NO_RELATION)
# The relations that say a hold's text was not compared with every withheld text (D3): never 'none', never one click.
NOT_COMPARED = (NOT_IN_BACKUP, LATER_WITHHOLD, OTHER_NOT_COMPARED, NOT_CHECKED)
# Releasing a hold with one of these needs a typed confirmation (plan 4.6): its text is, holds or quotes the withheld
# text (or a cut beginning of it, or could not be read in full); or (O-D1) its conversation's link to the withheld one
# is uncertain: never released in one click.
CONFIRM_RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF, CUT, NOT_CHECKED, UNCERTAIN_REL)
CONFIRM_WORD = "RELEASE"
# What each relation says, in words an admin reads (never text of either row).
RELATION_TEXT = {
    IDENTICAL_REL: "identical text",
    RESTATED_REL: "the withheld text restated after a compaction",
    CONTAINS: "contains the withheld text",
    PART_OF: "part of the withheld text (a reply quote, or in its conversation family the row's own text)",
    CUT: "the beginning of the withheld text, cut short (a summary, a prune line, a preview or a cut literal)",
    NOT_CHECKED: "could not be read in full (nested or encoded past the comparison's limits): held to be safe",
    SAME_UID: "the same JARVIS message id, other text",
    SAME_CALL: "the same tool call, other text",
    SAME_TIME_REL: "the same time and role",
    REUSED_ROW: "a reused row number",
    STALE: "a stale stored row at the withheld message's role and time",
    ORPHAN: "an orphan row",
    NOT_IN_BACKUP: "the withheld text is not in this backup",
    UNCERTAIN_REL: "its conversation's link to the withheld conversation is uncertain (a parent not found, or two)",
    LATER_WITHHOLD: "a later withhold: not compared with its withheld text yet (Settle with every text in hand)",
    OTHER_NOT_COMPARED: "not compared with every other withheld text (not all of them were in hand)",
    NO_RELATION: "no relation to the withheld text was found",
}
_DISCORD_NOTE = re.compile(r"\A\[Triggering message id: [^\n]*\]\n\n")
_REPLY_POINTER = re.compile(r'\A\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)
# A reply pointer that starts the text or one of the turns Hermes merged into it (a micro-compaction fold writes
# prev + '\n\n' + msg, so the second turn's own pointer follows a blank line; integrator, deviation 258)
_REPLY_POINTER_ANY = re.compile(r'(?:\A|\n\n)\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)


# The text Hermes's proactive prune writes over a tool row in place (agent/context_compressor.py
# _PRUNED_TOOL_PLACEHOLDER; rewrite_pruned_rows keeps the original in an archived twin): never a withheld text
PRUNE_PLACEHOLDERS = ("[Old tool output cleared to save context space]",)


def is_placeholder(text: str | None) -> bool:
    """Whether a row holds only Hermes's prune placeholder (integrator, deviation 264)."""
    return flat(text) in {flat(p) for p in PRUNE_PLACEHOLDERS}


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
        body = _restated_wrappers(body.rsplit(REPLAY_HEADER, 1)[1])
        found = _REPLY_POINTER.match(body)
        if found:
            quote, body = (quote if quote is not None else found.group(1)), body[found.end():]
    return body.strip(), quote, again


def _restated_wrappers(part: str) -> str:
    """A restated request is the original message's text after the header (Hermes: HEADER + '\n' + the request),
    with that message's own wrappers: the Discord note first (integrator, deviation 280)."""
    return _DISCORD_NOTE.sub("", part.lstrip(), count=1)


def reply_quotes(text: str | None) -> list[str]:
    """Every quote of a reply pointer that starts the text (after a Discord note), a turn Hermes merged into it, or a
    request Hermes restated after the replay header (integrator, deviation 280)."""
    body = _DISCORD_NOTE.sub("", text or "", count=1)
    out = [m.group(1) for m in _REPLY_POINTER_ANY.finditer(body)]
    for part in body.split(REPLAY_HEADER)[1:]:
        found = _REPLY_POINTER.match(_restated_wrappers(part))
        if found:
            out.append(found.group(1))
    return out


# --- Derived copies (option C, rebuild R3): fold, generic-v1, the canonical decode and the relations of every field --
# Hermes and the tools copy a message into other rows in derived forms: JSON (escaped, ensure_ascii, nested in a
# session_search read), Python repr (a FAILED suffix's str(dict), execute_code), its f-string summaries of a tool call
# (agent/context_compressor.py _TOOL_RESULT_SUMMARIZERS: vision_analyze, web_search, search_files, delegate_task), the
# generic prune line ' k=<str(v)[:40]>' (_summarize_tool_result_unguarded), large-result previews cut at a line
# (tools/tool_result_storage.py generate_preview, '[Truncated: ...]'), reply quotes and '> ' quote markers. Every
# comparison reads each field through canon() (the decoded readings) and compares under fold() (O-CI). Compared in
# memory only; nothing of either text is stored.

# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties: 4,174 code points): invisible formatting removed by fold()
_IGNORABLE_RANGES = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
                     (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
                     (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
                     (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
IGNORABLE = frozenset(c for a, b in _IGNORABLE_RANGES for c in range(a, b + 1))
_IGNORE_TABLE = dict.fromkeys(IGNORABLE)
_WORD = re.compile(r"\w+")


def fold(text: str | None) -> str:
    """O-CI: the form every comparison uses: default-ignorable code points removed, NFKC, casefold, and the Turkish
    dotted and dotless i read as i (İ, ı, i with a combining dot above)."""
    text = text or ""
    return _fold_small(text) if len(text) <= 4096 else _fold(text)


@functools.lru_cache(maxsize=16384)
def _fold_small(text: str) -> str:
    return _fold(text)


def _fold(text: str) -> str:
    t = text.translate(_IGNORE_TABLE)
    t = unicodedata.normalize("NFKC", t).replace("\u0130", "i").replace("\u0131", "i")
    t = t.casefold().replace("i\u0307", "i")
    return unicodedata.normalize("NFKC", t).translate(_IGNORE_TABLE)


def fflat(text: str | None) -> str:
    """fold(), whitespace collapsed."""
    return " ".join(fold(text).split())


def fwords(text: str | None) -> str:
    """fold(), words only (punctuation and quote markers gone), one space between them."""
    return " ".join(_WORD.findall(fold(text)))


# O-D4, list 'generic-v1': a piece of the withheld text that is one of these (greetings, thanks, short replies, a bare
# common URL), or shorter than MIN_RELATED characters, is neither demanded nor allowed as a carrier; anything longer is
# held. The whole withheld text is generic only when it is one of these.
GENERIC_LIST = "generic-v1"
_GENERIC_PHRASES = frozenset(" ".join(_WORD.findall(p)) for p in (
    "hello", "hi", "hey", "hello there", "hi there", "hey there", "good morning", "good afternoon", "good evening",
    "good night", "thanks", "thank you", "thanks a lot", "thank you very much", "thank you so much", "many thanks",
    "thanks so much", "thanks very much", "you are welcome", "you're welcome", "no problem", "ok", "okay", "ok thanks",
    "okay thanks", "ok thank you", "yes", "no", "yes please", "no thanks", "no thank you", "sure", "great", "perfect",
    "got it", "sounds good", "please", "bye", "goodbye", "see you", "see you later", "have a nice day",
    "have a great day", "how are you", "how are you doing", "what's up", "can you help me", "please help me",
    "hallo", "guten morgen", "guten tag", "guten abend", "gute nacht", "danke", "danke schön", "danke sehr",
    "vielen dank", "bitte", "bitte schön", "ja", "nein", "ja bitte", "nein danke", "tschüss", "wie geht es dir",
    "merhaba", "teşekkürler", "teşekkür ederim", "çok teşekkür ederim", "günaydın", "iyi akşamlar", "evet", "hayır",
    "bonjour", "bonsoir", "merci", "merci beaucoup", "oui", "non", "salut", "hola", "gracias", "muchas gracias",
    "buenos días", "buenas noches", "sí", "ciao", "grazie", "grazie mille"))
_GENERIC_URL = re.compile(r"(?:(?:https?|ftp)\s*:\s*/\s*/\s*)?(?:www\s*\.\s*)?[a-z0-9-]+(?:\s*\.\s*[a-z0-9-]+)+\s*/?\s*"
                          r"(?:(?:search\s*\?\s*q\s*=|index\s*\.\s*html?|wiki\s*/|en\s*/)\s*)?")


def generic(piece: str | None, *, whole: bool = False) -> bool:
    """O-D4 (generic-v1): whether a piece of a withheld text says nothing of it. Shorter than MIN_RELATED characters
    (fflat), or (whole or piece) a greeting, a thanks, a short reply or a bare common URL."""
    f = fflat(piece)
    if not f or (not whole and len(f) < MIN_RELATED):
        return True
    return generic_phrase(f)


def generic_phrase(piece: str | None) -> bool:
    """generic-v1's list alone (a greeting, a thanks, a short reply, a bare common URL), whatever its length: a reply
    quote keeps i22's own minimum (16 characters as JARVIS wrote it)."""
    f = fflat(piece)
    words = " ".join(_WORD.findall(f))
    return not words or words in _GENERIC_PHRASES or bool(_GENERIC_URL.fullmatch(f.strip(" .,;:!?'\"")))


# The canonical decode: an iterative work list of readings with caps; a text it cannot read in full is held as
# not_checked (fail-closed). Steps count decode operations (a JSON parse, an unescape, a literal read, an open literal).
CANON_STEPS = 64
CANON_DEPTH = 32
CANON_BUDGET = 4_000_000  # characters of readings (the size budget)
CANON_MAX_TEXT = 2_000_000  # characters: a longer field is not decoded at all (not_checked)
CANON_MAX_STRINGS = 20_000  # JSON strings read from one document (the rest: the whole-text unescape still reads them)


@dataclass(frozen=True)
class Reading:
    """One reading of a field: the text, whether it is a whole value (the field itself, a JSON string, a literal, a
    quote span, a quoted block, a prune value: compared whole), whether the withheld text would start where it starts
    (a summary's quoted argument, a prune value, a literal: lead), and whether it was cut at its end without a mark
    (a summary or prune value at its width, an open literal: cut_end)."""
    text: str
    unit: bool = False
    lead: bool = False
    cut_end: bool = False


@dataclass(frozen=True)
class Canon:
    readings: tuple[Reading, ...]
    capped: bool


class _Capped(Exception):
    pass


# Hermes's f-string summaries of a tool call (agent/context_compressor.py), read as quote spans: from the opening quote
# to the closing one the summary's fixed tail follows, across apostrophes and line breaks inside the argument. (regex,
# width: the argument's cut at that many characters without a mark, or 0).
_SPANS = (
    (re.compile(r"\[vision_analyze\] '(.*?)' \([\d,]+ chars\)", re.S), 50),
    (re.compile(r"\[web_search\] query='(.*?)' \([\d,]+ chars result\)", re.S), 0),
    (re.compile(r"\[search_files\] \S+ search for '(.*?)' in .*? -> \S+ matches", re.S), 0),
    (re.compile(r"\[delegate_task\] '(.*?)' \([\d,]+ chars result\)", re.S), 0),
    (re.compile(r"\[execute_code\] `(.*?)` \(\d+ lines output\)", re.S), 0),
    (re.compile(r"\[terminal\] ran `(.*?)` -> exit ", re.S), 0),
    (re.compile(r"\[clarify\] user responded: (.*)", re.S), 0),
)
# The generic prune line: '[<tool>] k=<str(v)[:40]> k2=<str(v)[:40]> (N chars result)' (+ a FAILED suffix)
_PRUNE_LINE = re.compile(r"\[[\w.:/-]+\]( [A-Za-z_][\w-]*=.*?) \([\d,]+ chars result\)", re.S)
_PRUNE_KEY = re.compile(r"(?:^| )([A-Za-z_][\w-]*)=")
PRUNE_WIDTH = 40
_QUOTE_LINE = re.compile(r"(?m)^[ \t]*(?:>[ \t]?)+")
# Marks a cut text ends with (Hermes's previews and summaries, common truncation notes)
_CUT_MARK = re.compile(r"…|\.\.\.|\[(?:output |content |message )?truncated|\((?:output )?truncated|<truncated", re.I)
_TRUNCATED = re.compile(r"\[truncated|truncated: tool response|</persisted-output>|\n\.\.\.(?:\n|\Z)", re.I)
# A snapshot or summary of earlier turns (Hermes's compaction summary, its historical snapshot headings): restated
# user asks in quote shapes; never skipped by the quick filter
SNAPSHOT_LEAD = re.compile(r"\[context summary\]|context compaction|## historical |summary of (?:the )?earlier",
                           re.I)
_JSON_DOC = re.compile(r'\A\s*(?:\{.*\}|\[.*\]|".*")\s*\Z', re.S)
_VALUE_OPEN = re.compile(r"""(?:\A|[:\[{(,=]\s?)(['"])""")


def _unescape(text: str) -> str:
    """JSON and Python escapes read back, tolerant of a cut text: an escape cut at the end is dropped (the repr or
    JSON escape cut: a preview cut inside '\\u00df' keeps what came before), a lone surrogate is dropped, an unknown
    escape keeps its character."""
    out: list[str] = []
    i, n = 0, len(text)
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "a": "\a", "v": "\v", "0": "\0", '"': '"',
              "'": "'", "\\": "\\", "/": "/"}
    while i < n:
        c = text[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break  # a cut escape at the end
        e = text[i + 1]
        width = {"u": 4, "x": 2, "U": 8}.get(e)
        if width:
            digits = text[i + 2:i + 2 + width]
            if len(digits) < width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                # an escape cut short (at the end, or before the cut mark that follows it): dropped with its digits,
                # so what came before reads as the cut text it is (the repr / JSON escape cut)
                part = re.match(r"[0-9a-fA-F]*", digits)
                i += 2 + (part.end() if part else 0)
                continue
            code = int(digits, 16)
            i += 2 + width
            if e == "u" and 0xD800 <= code <= 0xDBFF:
                low = re.match(r"\\u([dD][c-fC-F][0-9a-fA-F]{2})", text[i:i + 6])
                if low:
                    code = 0x10000 + ((code - 0xD800) << 10) + (int(low.group(1), 16) - 0xDC00)
                    i += 6
                elif re.fullmatch(r"\\(?:u(?:[dD](?:[c-fC-F][0-9a-fA-F]{0,1})?)?)?", text[i:i + 6]) and i + 6 > n:
                    break  # its low half was cut at the end
                else:
                    continue  # a lone high surrogate: dropped
            if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
                continue
            out.append(chr(code))
            continue
        out.append(simple.get(e, e))
        i += 2
    return "".join(out)


def _json_strings_of(value: Any) -> list[Reading]:
    """Every string (values and keys) of a parsed JSON document, as whole values (walked without recursion)."""
    out: list[Reading] = []
    stack = [value]
    while stack and len(out) < CANON_MAX_STRINGS:
        v = stack.pop()
        if isinstance(v, str):
            out.append(Reading(v, unit=True, lead=True))
        elif isinstance(v, dict):
            for k, x in v.items():
                out.append(Reading(str(k), unit=True, lead=True))
                stack.append(x)
        elif isinstance(v, list):
            stack.extend(v)
    return out


def _literals(text: str) -> list[Reading]:
    """The string literals of a JSON or Python value (a str(dict), a repr in code, a cut JSON text), read back with
    their escapes, and the literal left open at the end of a cut text (the open-literal reading: cut at its end)."""
    out: list[Reading] = []
    i, n = 0, len(text)
    count = 0
    while i < n and count < CANON_MAX_STRINGS:
        found = _VALUE_OPEN.search(text, i)
        if not found:
            break
        q, start = found.group(1), found.end()
        j = start
        while j < n and text[j] != q:
            j += 2 if text[j] == "\\" else 1
        body = text[start:min(j, n)]
        if j >= n:  # open at the end: a cut literal
            out.append(Reading(_unescape(body), unit=True, lead=True, cut_end=True))
            break
        if "\\" in body:
            out.append(Reading(_unescape(body), unit=True, lead=True))
        count += 1
        i = j + 1
    return out


def _cheap(text: str) -> list[Reading]:
    """Readings that need no decode step: the text without '> ' quote markers (and each quoted block whole: fqw), the
    quote spans of Hermes's f-string summaries, the values of a generic prune line (each at PRUNE_WIDTH)."""
    out: list[Reading] = []
    if _QUOTE_LINE.search(text):
        out.append(Reading(_QUOTE_LINE.sub("", text)))
        block: list[str] = []
        for line in text.splitlines() + [""]:
            if _QUOTE_LINE.match(line):
                block.append(_QUOTE_LINE.sub("", line, count=1))
            elif block:
                out.append(Reading("\n".join(block), unit=True, lead=True))
                block = []
    if "] " in text:
        for rx, width in _SPANS:
            for m in rx.finditer(text):
                span = m.group(1)
                cut = bool(width) and len(span) >= width
                if span.endswith("..."):
                    span, cut = span[:-3], True
                out.append(Reading(span, unit=True, lead=True, cut_end=cut))
                if "\n" in span:  # each line of a span as well (a summary inside a longer text)
                    out.extend(Reading(ln, unit=True, lead=i == 0) for i, ln in enumerate(span.split("\n")) if ln)
        for m in _PRUNE_LINE.finditer(text):
            args = m.group(1)
            keys = list(_PRUNE_KEY.finditer(args))
            for k, key in enumerate(keys):
                start = key.end()
                window = args[start:start + PRUNE_WIDTH]
                out.append(Reading(window, lead=True, cut_end=len(window) >= PRUNE_WIDTH))
                end = keys[k + 1].start() if k + 1 < len(keys) else len(args)
                value = args[start:end]
                out.append(Reading(value, unit=True, lead=True, cut_end=len(value) >= PRUNE_WIDTH))
    return out


def _decodes(text: str) -> list[Callable[[], list[Reading]]]:
    """The decode steps a reading needs: a JSON document is parsed (its strings; a cut one is read as below instead);
    otherwise a text with escapes is unescaped as a whole, and its literals (and an open one) are read."""
    rest: list[Callable[[], list[Reading]]] = []
    if "\\" in text:
        rest.append(lambda: [Reading(_unescape(text))])
    if _VALUE_OPEN.search(text) and ("\\" in text or _open_literal(text)):
        rest.append(lambda: _literals(text))
    if _JSON_DOC.match(text) and (text.lstrip()[:1] != '"' or "\\" in text):
        def parse() -> list[Reading]:
            try:
                value = json.loads(text)
            except RecursionError as exc:
                raise _Capped from exc
            except ValueError:
                return [x for fn in rest for x in fn()]
            return _json_strings_of(value)
        return [parse]
    return rest


def _open_literal(text: str) -> bool:
    """Whether a text ends inside a value's string literal (a cut JSON or repr text)."""
    i, n = 0, len(text)
    while i < n:
        found = _VALUE_OPEN.search(text, i)
        if not found:
            return False
        q, j = found.group(1), found.end()
        while j < n and text[j] != q:
            j += 2 if text[j] == "\\" else 1
        if j >= n:
            return True
        i = j + 1
    return False


def canon(text: str | None) -> Canon:
    """The readings of a field (the field itself first): iterative, with caps (CANON_STEPS decode steps, CANON_DEPTH
    nested decodes, CANON_BUDGET characters, CANON_MAX_TEXT for the field); capped when a cap stopped it or a JSON
    parse ran out of nesting (fail-closed: the row is held as not_checked)."""
    text = text or ""
    if len(text) <= 20_000:
        return _canon_cached(text)
    return _canon(text)


@functools.lru_cache(maxsize=4096)
def _canon_cached(text: str) -> Canon:
    return _canon(text)


def _canon(text: str) -> Canon:
    if len(text) > CANON_MAX_TEXT:
        return Canon((Reading(text, unit=True, lead=True),), True)
    out: list[Reading] = []
    seen: set[Reading] = set()
    queue: deque[tuple[Reading, int]] = deque([(Reading(text, unit=True, lead=True), 0)])
    steps, budget, capped = 0, CANON_BUDGET, False
    while queue:
        r, depth = queue.popleft()
        if r in seen or not r.text:
            continue
        seen.add(r)
        budget -= len(r.text)
        if budget < 0:
            capped = True
            break
        out.append(r)
        for x in _cheap(r.text):
            if x.text != r.text:
                queue.append((x, depth))
        decodes = _decodes(r.text)
        if not decodes:
            continue
        if depth >= CANON_DEPTH:
            capped = True
            continue
        for fn in decodes:
            steps += 1
            if steps > CANON_STEPS:
                capped = True
                break
            try:
                found = fn()
            except (_Capped, RecursionError, MemoryError):
                capped = True
                continue
            queue.extend((x, depth + 1) for x in found if x.text != r.text or x.cut_end != r.cut_end)
        if steps > CANON_STEPS:
            break
    return Canon(tuple(out), capped)


def canon_capped(text: str | None) -> bool:
    return canon(text).capped


@dataclass(frozen=True)
class WForm:
    """A withheld text as every comparison reads it (in memory only)."""
    w: str  # fflat of its authored text (Hermes's wrappers peeled)
    whole: str  # fflat of the whole text
    words: str  # fwords of its authored text
    generic: bool  # O-D4: the whole text is generic
    grams: frozenset[str]  # every QUICK_Q characters of w (a cut point's quick test)

    @property
    def long(self) -> bool:
        return len(self.w) >= MIN_RELATED

    @property
    def short_words(self) -> bool:
        return len(self.words) < MIN_RELATED


@functools.lru_cache(maxsize=1024)
def wform(withheld: str) -> WForm | None:
    body = peel(withheld)[0]
    w = fflat(body) or fflat(withheld)
    if not w:
        return None
    return WForm(w, fflat(withheld), fwords(body) or fwords(withheld), generic(body or withheld, whole=True),
                 frozenset(w[i:i + QUICK_Q] for i in range(max(len(w) - QUICK_Q + 1, 0))))


def _suffix_prefix(tail: str, w: str) -> int:
    """The length of the longest suffix of tail that is a prefix of w (the prefix function of w + NUL + tail)."""
    if not tail or not w:
        return 0
    tail = tail[-len(w):]
    s = w + "\0" + tail
    pi = [0] * len(s)
    for i in range(1, len(s)):
        k = pi[i - 1]
        while k and s[i] != s[k]:
            k = pi[k - 1]
        if s[i] == s[k]:
            k += 1
        pi[i] = k
    return pi[-1]


def _word_seq(hay: str, needle: str) -> bool:
    """Whether needle (fwords) appears in hay (fwords) as whole words."""
    return bool(needle) and f" {needle} " in f" {hay} "


def _cut_points(text: str, cut_end: bool) -> list[int]:
    """Where a reading was cut: before each cut mark, at its end when cut without a mark, and (cut-per-line) at the end
    of each line of a truncated preview."""
    points = [m.start() for m in _CUT_MARK.finditer(text)]
    if cut_end:
        points.append(len(text))
    if _TRUNCATED.search(text):
        points += [m.start() for m in re.finditer(r"\n", text)]
    return points


def _piece(rel: str, piece: str, out: set[str]) -> None:
    if not generic(piece):
        out.add(rel)


def _tail(text: str, p: int, want: int) -> str:
    """fflat of the text before p, at least `want` characters of it when the text has them (a window grown from p)."""
    size = 4 * want + 64
    while True:
        start = max(0, p - size)
        tail = fflat(text[start:p])
        if start == 0 or len(tail) >= want + 1:
            return tail
        size *= 4


def _reading_relations(r: Reading, wf: WForm, *, family: bool, carrier: bool = False) -> set[str]:
    out: set[str] = set()
    f = fflat(r.text)
    if not f:
        return out
    if r.unit and (f in (wf.w, wf.whole) or (wf.words and fwords(r.text) == wf.words)):
        out.add(IDENTICAL_REL)
    if wf.long and wf.w in f:
        out.add(CONTAINS)
    if family and wf.short_words and _word_seq(fwords(r.text), wf.words):
        out.add(CONTAINS)  # a short withheld text's words in another case or wrapping: ambiguous, never 'none'
    if not wf.long:
        return out
    if (r.unit or r.lead) and len(f) < len(wf.w) and wf.w.startswith(f):
        _piece(CUT, f, out)  # a beginning of the withheld text: a value cut short (with or without a mark)
    for p in _cut_points(r.text, r.cut_end):
        tail = _tail(r.text, p, len(wf.w))
        if len(tail) < MIN_RELATED or tail[-QUICK_Q:] not in wf.grams:
            continue
        k = _suffix_prefix(tail, wf.w)
        if MIN_RELATED <= k < len(wf.w):
            _piece(CUT, wf.w[:k], out)
    if family and r.unit and len(f) >= MIN_RELATED and f != wf.w and f in wf.w:
        _piece(PART_OF, f, out)
    for q in reply_quotes(r.text) if "[Replying to" in r.text else ():
        if len(q) >= MIN_RELATED and fflat(q) and fflat(q) in wf.w and not (carrier and generic_phrase(q)):
            out.add(PART_OF)
    return out


def _field_relations(held: str, withheld: str, wf: WForm, *, family: bool, carrier: bool = False) -> set[str]:
    """The relations of one field: i22's rules on the field as written (identical, restated, contains, a reply quote;
    in a family the row's own text being part of it), under fold, then every reading of canon()."""
    out: set[str] = set()
    whole = fflat(held)
    body, quote, again = peel(held)
    fbody = fflat(body)
    if whole == wf.whole or (wf.w and fbody == wf.w):
        out.add(IDENTICAL_REL)
        if whole != wf.whole and (again or REPLAY_HEADER in withheld):
            out.add(RESTATED_REL)
    if wf.long and wf.w in fflat(body or held):
        out.add(CONTAINS)
    # a quote of at least MIN_RELATED characters as JARVIS wrote it (integrator, deviation 273)
    if any(len(q) >= MIN_RELATED and fflat(q) and fflat(q) in wf.w and not (carrier and generic_phrase(q))
           for q in [*([quote] if quote is not None else []), *reply_quotes(held)]):
        out.add(PART_OF)
    if family and len(fbody) >= MIN_RELATED and fbody != wf.w and fbody in wf.w:
        out.add(PART_OF)  # i22's rule, under fold
    c = canon(held)
    if c.capped:
        out.add(NOT_CHECKED)
    for r in c.readings:
        out |= _reading_relations(r, wf, family=family, carrier=carrier)
    return out


def _fields(held: str | Iterable[str] | None) -> list[str]:
    if held is None:
        return []
    if isinstance(held, str):
        return [held]
    return [h for h in held if h]


def text_relations(held: str | Iterable[str] | None, withheld: str | None) -> set[str]:
    """The relations of a row's text (one field, or every field of a row: row_fields()) to a withheld text, computed
    by comparison under fold() after the peel (plan 4.6) and through every reading of canon() (rebuild R3): identical
    (the same words, also a whole value: a JSON string, a quote span, a quoted block read without its '> ' markers);
    restated (identical after the replay header); contains (the withheld text, at least MIN_RELATED characters, in
    any reading); part_of (a reply pointer quoting at least MIN_RELATED characters of it: N1); cut (a beginning of it
    of at least MIN_RELATED characters, cut short: a summary, a prune line, a preview, a cut literal); not_checked (a
    field canon() could not read in full). Pieces on generic-v1 never count (O-D4). Compared in memory only."""
    want = withheld or ""
    wf = wform(want) if flat(want) else None
    if wf is None:
        return set()
    out: set[str] = set()
    for h in _fields(held):
        out |= _field_relations(h, want, wf, family=False)
    return out


def family_relations(held: str | Iterable[str] | None, withheld: str | None) -> set[str]:
    """text_relations() for a row of a withheld conversation family or group (Settle, later backups, the interim
    import, the group sweep): also 'part of' when the row's own text, or a whole value of it, of at least MIN_RELATED
    characters is a part of the withheld text, e.g. the original of a message JARVIS later folded into the withheld one;
    and 'contains' for a withheld text of fewer than MIN_RELATED characters of words whose words the row has as a
    sequence in any case (ambiguous, never 'none'). Outside every family a middle piece counts only as a reply quote
    (owner question 8; middle pieces N3/R5b are pending)."""
    want = withheld or ""
    wf = wform(want) if flat(want) else None
    if wf is None:
        return set()
    out: set[str] = set()
    for h in _fields(held):
        out |= _field_relations(h, want, wf, family=True)
    return out


def carrier_relations(held: str | Iterable[str] | None, withheld: str | None) -> set[str]:
    """The relations that make a row (or a title, or a memory item) outside every withheld group a carrier of the
    withheld text, held on its own (rebuild R3): text_relations() over every field, without a whole withheld text that
    is generic (O-D4: neither demanded nor allowed as a carrier)."""
    want = withheld or ""
    wf = wform(want) if flat(want) else None
    if wf is None:
        return set()
    out: set[str] = set()
    for h in _fields(held):
        out |= _field_relations(h, want, wf, family=False, carrier=True)
    if wf.generic:
        out -= {IDENTICAL_REL, CONTAINS, RESTATED_REL}
    return out


def row_fields(row: Any) -> list[str]:
    """Every field of a stored row (an ArchiveMessage, or a rollback copy's dict) a comparison reads: its content and
    its tool calls (their arguments, read by canon())."""
    get = row.get if isinstance(row, Mapping) else (lambda k, d=None: getattr(row, k, d))
    out = [get("content") or ""]
    calls = get("tool_calls")
    if calls:
        out.append(calls if isinstance(calls, str) else json.dumps(calls, ensure_ascii=False, sort_keys=True))
    return [x for x in out if x]


def source_fields(m: Any) -> list[str]:
    """Every field of a backup row (source.SourceMessage) a comparison reads: its content and its raw tool calls."""
    raw = getattr(m, "tool_calls_raw", "") or ""
    if raw and not isinstance(raw, str):
        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else json.dumps(raw, ensure_ascii=False)
    return [x for x in (m.content or "", raw) if x]


def row_relations(row: Any, withheld: str | None, *, family: bool = False) -> set[str]:
    """The relations of every field of a stored row to a withheld text (family_relations in a family or group, else
    carrier_relations)."""
    return (family_relations if family else carrier_relations)(row_fields(row), withheld)


# The quick filter of the full stored-row scan (review.hold_carriers): a cheap test that never skips a row a reading
# could relate (no prefilter by W's longest words: every row is read). A row is compared unless no 8 characters of W's
# skeleton (fold() without whitespace and combining marks) and no 8 of its word skeleton appear in the row's; a row
# that needs decoding (escapes, a JSON or repr value), a snapshot of earlier turns (SNAPSHOT_LEAD) or a quote shape (a
# reply pointer, '> ' markers: a quote of MIN_RELATED characters as written may hold fewer than 8 of the skeleton) is
# always compared.
QUICK_Q = 8


@functools.cache
def _skel_table() -> dict[int, None]:
    return dict.fromkeys(c for c in range(0x20000) if unicodedata.category(chr(c)) in (
        "Mn", "Me", "Mc", "Zs", "Zl", "Zp", "Cc", "Cf") or chr(c).isspace())


def skeleton(text: str | None) -> str:
    """fold() without any whitespace, control, format or combining character."""
    return fold(text).translate(_skel_table())


def word_skeleton(text: str | None) -> str:
    return "".join(_WORD.findall(fold(text)))


def _grams(text: str, q: int = QUICK_Q) -> set[str]:
    return {text[i:i + q] for i in range(max(len(text) - q + 1, 0))}


@dataclass(frozen=True)
class Quick:
    grams: frozenset[str]
    word_grams: frozenset[str]
    whole: tuple[str, ...]  # skeletons too short for grams: compared whole


def quick_of(withheld: str) -> Quick:
    """The quick filter's forms of a withheld text: its whole text and its authored text (peeled), as skeletons and
    word skeletons."""
    forms = {withheld, peel(withheld)[0]}
    grams: set[str] = set()
    word_grams: set[str] = set()
    whole: set[str] = set()
    for f in forms:
        for sk, into in ((skeleton(f), grams), (word_skeleton(f), word_grams)):
            if len(sk) >= QUICK_Q:
                into |= _grams(sk)
            elif sk:
                whole.add(sk)
    return Quick(frozenset(grams), frozenset(word_grams), tuple(sorted(whole)))


_NEEDS_DECODE = re.compile(r"""\\|\A\s*[\[{"']|["'][:,]\s*["'\[{]|[:\[{(,=]\s?['"]""")
_QUOTE_SHAPE = re.compile(r"\[Replying to|^[ \t]*>", re.M)


def quick_related(fields: Iterable[str], quick: Quick) -> bool:
    """False only when no reading of these fields can relate to the withheld text (the full scan's quick filter)."""
    for f in fields:
        if not f:
            continue
        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):
            return True
        sk, wsk = skeleton(f), word_skeleton(f)
        if any(w in sk or w in wsk for w in quick.whole):
            return True
        if quick.grams and not _grams(sk).isdisjoint(quick.grams):
            return True
        if quick.word_grams and not _grams(wsk).isdisjoint(quick.word_grams):
            return True
    return False


def group_relations(held: str | Iterable[str] | None, own: str | None, others: Iterable[str] = (), *,
                    all_in_hand: bool, uncertain: bool = False, structural: Iterable[str] = ()) -> tuple[str, ...]:
    """The evidence of a row held with its conversation group (option C, rebuild R2): its text (every field: R3)
    against the group's own withheld text (family_relations; 'not in this backup' when that text is not in hand) and
    against every other withheld text in hand (a relation to any of them is shown), the structural relations the
    caller found, and 'uncertain' for a conversation an uncertain link holds. 'none' only when nothing relates and
    every withheld text was in hand (all_in_hand); otherwise 'other_not_compared' (never released in one click).
    Compared in memory only."""
    out: set[str] = set(structural)
    if own is None:
        out.add(NOT_IN_BACKUP)
    else:
        out |= family_relations(held, own)
    for w in others:
        out |= family_relations(held, w) & set(TEXT_RELATIONS)
    if uncertain:
        out.add(UNCERTAIN_REL)
    if not out:
        out.add(NO_RELATION if all_in_hand else OTHER_NOT_COMPARED)
    return tuple(relation_order(out))


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
                        (PART_OF, "part of the withheld text"), (CUT, "the withheld text, cut short"),
                        (NOT_CHECKED, "not checked in full")):
        if code in found:
            return words
    return "nothing to compare" if NOT_IN_BACKUP in found else "other text"


def needs_confirmation(codes: Iterable[str]) -> bool:
    return bool(set(codes) & set(CONFIRM_RELATIONS))


# --- Option C (rebuild R4): D3 states, the form a hold's text was read in, and 'none' against every text -----------

# The form a hold's text was read in when its evidence was computed (brain_tombstones.form): only 'plain' (authored
# words read as they are) is ever released in one click; a tool row, a Hermes summary, a wrapped or encoded text, or a
# row whose text was never read here ('') needs a per-row release with the typed word (B6).
PLAIN_FORM, SUMMARY_FORM, WRAPPED_FORM, ENCODED_FORM, TOOL_FORM = "plain", "summary", "wrapped", "encoded", "tool"
FORMS = (PLAIN_FORM, SUMMARY_FORM, WRAPPED_FORM, ENCODED_FORM, TOOL_FORM)


def text_form(content: Any, tool_calls: Any = None) -> str:
    """The form of a row's text as the comparison read it (never the text): 'tool' (tool calls), 'summary' (a Hermes
    summary, snapshot, prune line or restated request), 'wrapped' (a reply pointer, a quote, a triggering note around
    the authored words), 'encoded' (JSON, escapes, literals: read only through the canonical decode, or capped),
    'plain' (authored words read as they are); '' when there is nothing to read (a blank row, a placeholder)."""
    if tool_calls:
        return TOOL_FORM
    text = content if isinstance(content, str) else ""
    if not text.strip() or is_placeholder(text):
        return ""
    if (SNAPSHOT_LEAD.search(text) or REPLAY_HEADER in text or _PRUNE_LINE.search(text)
            or any(rx.search(text) for rx, _ in _SPANS)):
        return SUMMARY_FORM
    body, quote, again = peel(text)
    if (quote is not None or again or body != text.strip() or _REPLY_POINTER_ANY.search(text)
            or _QUOTE_LINE.search(text)):
        return WRAPPED_FORM
    c = canon(text)
    if c.capped or any(r.text != text for r in c.readings):
        return ENCODED_FORM
    return PLAIN_FORM


def row_form(row: Any) -> str:
    """text_form() of a stored row (a mapping or a model) while it still holds its text; '' once withheld."""
    get = row.get if isinstance(row, Mapping) else (lambda k, d=None: getattr(row, k, d))
    if get("withheld", False):
        return ""
    return text_form(get("content", ""), get("tool_calls", None))


def settled_relations(new: Iterable[str], old: Iterable[str] = (), *, all_in_hand: bool) -> tuple[str, ...]:
    """A hold's evidence computed again (an import, Settle, a sweep): 'none' needs every withheld text in hand (D3:
    none_every, else 'other_not_compared'), and a later withhold's mark stays until every text was in hand
    (keep_later: Settle without the later text keeps the evidence stale)."""
    out = set(new)
    if LATER_WITHHOLD in set(old) and not all_in_hand:
        out.add(LATER_WITHHOLD)  # keep_later
    if NO_RELATION in out and not all_in_hand:
        out.discard(NO_RELATION)  # none_every
        out.add(OTHER_NOT_COMPARED)
    if len(out) > 1:
        out.discard(NO_RELATION)
    if not out:
        out.add(NO_RELATION if all_in_hand else OTHER_NOT_COMPARED)
    return tuple(relation_order(out))


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
    trusted: bool = True  # rebuild R4 (B6, 6725, F1): typed, checked or left readable; an unchecked release is not
    kind: str = "release"

    def covers(self, row: Exact) -> bool:
        return binding_matches(self.role, self.tool_call_id, self.at, self.uid, row.role, row.tool_call_id, row.at,
                               row.uid)


def title_base(title: str | None) -> str:
    """A conversation title without Hermes's branch suffix (' #2'), compared as Hermes stores titles: whitespace
    collapsed (hermes_state_titles.sanitize_title). A title that reached the backup another way (a trailing space
    kept, a branch named '<title>  #2') is the same title (fixer, deviation 282)."""
    return TITLE_SUFFIX.sub("", flat(title))


def title_relations(title: str | None, withheld: str | None) -> set[str]:
    """The relations of a conversation title to a withheld text (plan 4.6 codes; compared in memory only, never
    stored): Hermes derives a title from the first user message (agent/title_generator.derive_title: its first line,
    whitespace collapsed, cut at a word boundary with a trailing '…') and a branch adds ' #N'. The title, without
    those, is related when it is identical to the withheld text, contains it, is a part of it of at least
    MIN_RELATED characters (identity.family_relations), or is its first line. 'not in this backup' when the withheld
    text is not known here: nothing could be compared (fixer, deviation 282)."""
    if withheld is None:
        return {NOT_IN_BACKUP}
    text = title_base(title)
    while text.endswith("…"):
        text = text[:-1].rstrip()
    if not text:
        return set()
    out = family_relations(text, withheld)
    first = next((ln for ln in peel(withheld)[0].splitlines() if ln.strip()), "")
    if flat(first) and text == flat(first):
        out.add(PART_OF)
    if not out and _shares_run(text, flat(peel(withheld)[0])):
        out.add(PART_OF)
    return out


def _shares_run(title: str, wbody: str) -> bool:
    """Whether a title and the withheld text share a run of at least MIN_RELATED characters that starts at a word
    of the title: a title derived from a first message that has other words before the withheld text ('Please
    summarize: <W>…'). Fail-closed: it only keeps a held title hidden (review 284/289, deviation 289)."""
    if len(title) < MIN_RELATED or len(wbody) < MIN_RELATED:
        return False
    starts = [0] + [i + 1 for i, ch in enumerate(title) if ch == " "]
    return any(len(title) - i >= MIN_RELATED and title[i:i + MIN_RELATED] in wbody for i in starts)


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
    # rebuild R4: the withhold anchors a 'none' was computed against (None: every admin's withhold now, the caller
    # computed it with every text in hand) and the form its text was read in ('' never read)
    compared: tuple[str, ...] | None = None
    form: str = ""

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
                 relations=tuple(getattr(r, "relations", None) or ()),
                 compared=tuple(getattr(r, "compared", None) or ()), form=getattr(r, "form", "") or "")


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
            BrainWithholdExemption.source_at, BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on,
            BrainWithholdExemption.trusted, BrainWithholdExemption.kind)
            .order_by(BrainWithholdExemption.id))).all()
        bindings: dict[tuple[str, str, str], list[Binding]] = {}
        for r in rows:
            bindings.setdefault((r[0], r[1], r[2]), []).append(
                Binding(r[3] or "", r[4] or "", r[5], r[6] or "", r[7] or "", bool(r[8]), r[9] or "release"))
        return cls(stones, [(r[0], r[1], r[2]) for r in rows], bindings)

    def released(self, group: str, row: Exact, *, backup: str | None = None, same_content: bool = False) -> bool:
        """Whether an admin's release for this group covers this exact JARVIS row (identity.exact): a release names
        its session and row id, and its binding matches the row's role, tool_call_id, time and uid. Another message at
        a reused row id is not covered. For a row of a backup (backup: its SHA-256) the release covers the content it
        was decided on: only a release decided on that very backup, unless the stored row already shows that very
        content (same_content: readable, from the same source row). Hermes rewrites a row in place (a prune keeps its
        id, uid, role, call and time) and an older backup holds its earlier text: never covered by a release of
        another version (review 2 S27's case without a digest; rules062-confirm c0, x8). Option C (rebuild R4, 6725 /
        F1): the trust comes from that very covering release (typed, checked or left readable): an unchecked release
        never stands on another backup's typed one."""
        ident = (group, row.session, row.row)
        if ident not in self.exempt:
            return False
        found = self.bindings.get(ident)
        if found is None:
            return backup is None or same_content
        return any(b.covers(row) and b.trusted and (backup is None or same_content or (
            bool(b.decided_on) and b.decided_on == backup)) for b in found)

    def unchecked(self, group: str, row: Exact, *, backup: str | None = None, same_content: bool = False
                  ) -> list[Binding]:
        """The untrusted releases for this group that cover this exact row and its content (released() without the
        trust): a check of that content with every withheld text in hand may mark them (mark_exemptions)."""
        found = self.bindings.get((group, row.session, row.row)) or []
        return [b for b in found if b.covers(row) and not b.trusted and (
            backup is None or same_content or (bool(b.decided_on) and b.decided_on == backup))]

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
              "relations": list(relation_order(binding.relations)),
              "compared": await _compared(s, binding.relations, binding.compared),
              "form": binding.form if binding.form in FORMS else ""}
    stmt = pg_insert(BrainTombstone).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BrainTombstone.source_key],
        set_={"review": stmt.excluded.review, "via": stmt.excluded.via, "anchor_key": stmt.excluded.anchor_key,
              "kept_by": stmt.excluded.kept_by, "kept_at": stmt.excluded.kept_at,
              "relations": stmt.excluded.relations, "compared": stmt.excluded.compared,
              "form": stmt.excluded.form},
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


async def update_hold(s: AsyncSession, key: str, *, via: str, relations: Iterable[str], decided_on: str = "",
                      form: str | None = None, compared: Iterable[str] | None = None) -> bool:
    """A hold's evidence as Settle computed it again (plan 4.3, 4.6): its via and relations (and the backup it was
    decided on, once known; rebuild R4: the withheld texts a 'none' was computed against, and the form the text was
    read in). Only a pending stone; never a member. Whether it changed."""
    relations = relation_order(relations)
    values: dict[str, Any] = {"via": via[:200], "relations": relations,
                              "compared": await _compared(s, relations, None if compared is None else tuple(compared))}
    if form is not None:
        values["form"] = form if form in FORMS else ""
    done = await s.execute(update(BrainTombstone).where(
        BrainTombstone.source_key == key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
        BrainTombstone.review == PENDING).values(**values))
    if decided_on:
        await s.execute(update(BrainTombstone).where(
            BrainTombstone.source_key == key, BrainTombstone.reason == WITHHELD_BY_ADMIN,
            BrainTombstone.decided_on == "").values(decided_on=decided_on[:64]))
    return bool(done.rowcount)


async def admin_anchor_keys(s: AsyncSession) -> list[str]:
    """Every admin's own withhold (its key, the anchor of its group): what 'none' must have been compared with."""
    return sorted((await s.scalars(select(BrainTombstone.source_key).where(
        BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.anchor_key == "",
        BrainTombstone.review != PENDING))).all())


async def _compared(s: AsyncSession, relations: Iterable[str], compared: tuple[str, ...] | None) -> list[str]:
    """The anchors a hold's 'none' was computed against: given (a decisions file carries them), or, for a 'none' the
    caller computed with every withheld text in hand (D3: none_every), every admin's withhold now; [] otherwise."""
    if compared is not None:
        return sorted({a[:400] for a in compared if isinstance(a, str) and a})
    if NO_RELATION not in set(relations):
        return []
    return await admin_anchor_keys(s)


async def mark_later_withhold(s: AsyncSession, anchor: str) -> list[str]:
    """N-A (rebuild R4, D3): a new admin's withhold (its anchor) was never compared with the holds other withholds
    computed 'none' for: each of them says 'later_withhold' at once (never released in one click, a per-row release
    needs the typed word). The decision backups of those withholds (theirs and the holds'), to finish again: Settle
    with every withheld text in hand computes the evidence again, and the export waits until then."""
    rows = (await s.execute(select(BrainTombstone.source_key, BrainTombstone.decided_on, BrainTombstone.anchor_key)
                            .where(BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == PENDING,
                                   func.coalesce(func.nullif(BrainTombstone.anchor_key, ""),
                                                 BrainTombstone.source_key) != anchor,
                                   literal(NO_RELATION) == any_(BrainTombstone.relations)))).all()
    if not rows:
        return []
    await s.execute(update(BrainTombstone).where(BrainTombstone.source_key.in_(sorted(r[0] for r in rows)))
                    .values(relations=func.array_replace(BrainTombstone.relations, NO_RELATION, LATER_WITHHOLD),
                            compared=[]))
    groups = sorted({r[2] or r[0] for r in rows})
    own = set((await s.scalars(select(BrainTombstone.decided_on).where(
        BrainTombstone.source_key.in_(groups), BrainTombstone.decided_on != ""))).all())
    return sorted(own | {r[1] for r in rows if r[1]})


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
                 via=via, review=review, bound=True, after_run=after_run, at=row.at, decided_on=decided_on,
                 form=row_form(row))


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
    for sid_, rid_, role_, call_, at_, uid_, on_, tr_, kind_ in (await s.execute(select(
            BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id,
            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id, BrainWithholdExemption.source_at,
            BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on, BrainWithholdExemption.trusted,
            BrainWithholdExemption.kind).where(
            BrainWithholdExemption.anchor_key == message.source_key))).all() if message.source_key else ():
        released.setdefault((sid_, rid_), []).append(Binding(role_ or "", call_ or "", at_, uid_ or "", on_ or "",
                                                             bool(tr_), kind_ or "release"))

    def locked(q: Any) -> Any:
        return q.with_for_update(of=ArchiveMessage) if lock else q

    stored_shas: dict[int, str] = {}

    async def exempt(row: ArchiveMessage, sid: str) -> bool:
        here = exact_of_row(row, sid)
        found_ = [b for b in released.get((here.session, here.row), ()) if b.covers(here)]
        # rebuild R4 (F1): the trust of the very release that covers the stored content
        return any(b.trusted for b in await releases_covering_stored(s, found_, row, stored_shas))

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
                             {SAME_UID, *text_relations(row_fields(other), message.content)})
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
                or_(BrainSessionLink.a.in_(ids), BrainSessionLink.b.in_(ids)),
                BrainSessionLink.how.in_(FAMILY_LINKS)))).all():
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
    if not flat(message.content):
        return []
    # rebuild R3 (O-CI): no case-sensitive narrowing by the longest word in the database; every readable row of the
    # family is read, through the quick filter that never skips a row a reading could relate (another case, a form)
    quick = quick_of(message.content)
    query = select(ArchiveMessage).where(ArchiveMessage.session_id.in_(sorted(sessions)),
                                         ArchiveMessage.withheld.is_(False))
    released: list[tuple[str, str, Binding]] = [
        (sid_, rid_, Binding(role_ or "", call_ or "", at_, uid_ or "", on_ or "", bool(tr_)))
        for sid_, rid_, role_, call_, at_, uid_, on_, tr_ in (await s.execute(select(
            BrainWithholdExemption.source_session_id, BrainWithholdExemption.source_row_id,
            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id, BrainWithholdExemption.source_at,
            BrainWithholdExemption.source_uid, BrainWithholdExemption.decided_on,
            BrainWithholdExemption.trusted).where(
            BrainWithholdExemption.anchor_key == message.source_key))).all()] if message.source_key else []
    out = []
    for r in (await s.scalars(query.order_by(ArchiveMessage.id))).all():
        if r.id in skip:
            continue
        fields = row_fields(r)
        if not quick_related(fields, quick):
            continue
        rel = text_relations(fields, message.content)
        if not rel:
            continue
        # a family row: its evidence is every relation a family row has (a cut beginning that is also its own text
        # being part of the withheld one says both)
        rel |= family_relations(fields, message.content)
        sid = sessions.get(r.session_id, "")
        here = exact_of_row(r, sid)
        covering = [b for rsid, rrow, b in released if (rsid, rrow) == (here.session, here.row) and b.covers(here)]
        if covering and any(b.trusted for b in await releases_covering_stored(s, covering, r)):
            continue
        out.append((r, sid, tuple(relation_order(rel))))
    return out


async def first_user_message(s: AsyncSession, message: ArchiveMessage) -> bool:
    """Whether this is its conversation's first user message: the title can paraphrase it. The lowest position, or
    the earliest time: Hermes titles a conversation from its first user turn, and a fold or a rewrite re-inserts the
    history with new row ids but the same times, so a stale stored row can come first by position (integrator,
    deviation 252; fail-closed: either order counts)."""
    if message.role != "user":
        return False
    base = select(ArchiveMessage.id).where(ArchiveMessage.session_id == message.session_id,
                                           ArchiveMessage.role == "user")
    first = await s.scalar(base.order_by(ArchiveMessage.position, ArchiveMessage.id).limit(1))
    if first == message.id:
        return True
    if message.at is None:
        return False
    earlier = await s.scalar(base.where(ArchiveMessage.id != message.id, ArchiveMessage.at.is_not(None),
                                        ArchiveMessage.at < message.at).limit(1))
    return earlier is None


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


async def releases_covering_stored(s: AsyncSession, bindings: Iterable[Binding], row: Any,
                                   cache: dict[int, str] | None = None) -> list[Binding]:
    """The releases (each already matching the row's exact identity) that cover the stored row's content: decided on
    the backup the row's content came from (stored_backup), or with no decision backup (a version 4 file's). The
    trust of a release is read from these only (rebuild R4, 6725 / F1: never from another backup's release)."""
    found = list(bindings)
    if not found:
        return []
    undecided = [b for b in found if not b.decided_on]
    if len(undecided) == len(found):
        return undecided
    sha = await stored_backup(s, row, cache)
    return undecided + [b for b in found if b.decided_on and sha and b.decided_on == sha]


async def _row_exemptions(s: AsyncSession, group: str, here: Exact) -> list[Binding]:
    return [b for b in (Binding(role or "", call or "", at, uid or "", on or "", bool(tr), kind or "release")
                        for role, call, at, uid, on, tr, kind in (await s.execute(select(
                            BrainWithholdExemption.role, BrainWithholdExemption.tool_call_id,
                            BrainWithholdExemption.source_at, BrainWithholdExemption.source_uid,
                            BrainWithholdExemption.decided_on, BrainWithholdExemption.trusted,
                            BrainWithholdExemption.kind).where(
                            BrainWithholdExemption.anchor_key == group,
                            BrainWithholdExemption.source_session_id == here.session,
                            BrainWithholdExemption.source_row_id == here.row))).all()) if b.covers(here)]


async def exempt_covers(s: AsyncSession, group: str, session_id: str, row: ArchiveMessage) -> bool:
    """Whether an admin's release (or 'left readable') for this group covers this exact stored row (identity.exact:
    the release names its JARVIS row, and its role, tool_call_id, time and uid match) and its content (the release was
    decided on the backup the row's content came from: release_covers_stored), and is trusted (rebuild R4, B6: typed,
    checked or left readable), the trust read only from a release that covers the stored content (6725 / F1)."""
    found = await releases_covering_stored(s, await _row_exemptions(s, group, exact_of_row(row, session_id or "")),
                                           row)
    return any(b.trusted for b in found)


async def unchecked_covers(s: AsyncSession, group: str, session_id: str, row: ArchiveMessage) -> list[Binding]:
    """The untrusted releases for this group that cover this stored row and its content (exempt_covers without the
    trust): a check of that content with every withheld text in hand marks them (mark_exemptions)."""
    found = await releases_covering_stored(s, await _row_exemptions(s, group, exact_of_row(row, session_id or "")),
                                           row)
    return [b for b in found if not b.trusted]


async def mark_exemptions(s: AsyncSession, group: str, row: Exact, *, decided_on: str, kinds: Iterable[str]) -> int:
    """A check of one backup's content of this exact row found no relation to any withheld text, every one in hand:
    the untrusted releases for this group of that exact row, decided on that very backup and of these kinds, become
    trusted (rebuild R4, 6725: a check marks only the release of the content it checked, never another backup's).
    How many."""
    kinds = sorted(set(kinds))
    if not decided_on or not kinds:
        return 0
    n = 0
    for e in (await s.scalars(select(BrainWithholdExemption).where(
            BrainWithholdExemption.anchor_key == group, BrainWithholdExemption.source_session_id == row.session,
            BrainWithholdExemption.source_row_id == row.row, BrainWithholdExemption.trusted.is_(False),
            BrainWithholdExemption.decided_on == decided_on[:64],
            BrainWithholdExemption.kind.in_(kinds)))).all():
        if Binding(e.role or "", e.tool_call_id or "", e.source_at, e.source_uid or "", e.decided_on or "").covers(
                row):
            e.trusted = True
            n += 1
    if n:
        await s.flush()
    return n


def exemption_values(row: Exact, *, anchor: str, kind: str = "release", decided_on: str = "", reason: str = "",
                     decided_by: str = "", decided_at: datetime | None = None, trusted: bool = False
                     ) -> dict[str, Any]:
    """The values of a brain_withhold_exemptions row for this exact JARVIS row (0.6.2 plan 1.2; rebuild R4: its
    trust, never a client's flag: the server's typed word, a check, or an admin's 'left readable')."""
    out: dict[str, Any] = {"anchor_key": anchor, "source_session_id": row.session, "source_row_id": row.row,
                           "role": row.role, "tool_call_id": row.tool_call_id, "source_at": row.at,
                           "source_uid": row.uid, "kind": kind, "decided_on": (decided_on or "")[:64],
                           "trusted": bool(trusted),
                           "reason": (reason or "released by an admin")[:200], "decided_by": (decided_by or "")[:200]}
    if decided_at is not None:
        out["decided_at"] = decided_at
    return out


async def put_exemption(s: AsyncSession, row: Exact, **kw: Any) -> bool:
    """Records a release (or 'left readable') of this exact JARVIS row for a withhold; one per exact row and group
    (the first decision stays; a trusted decision of the same row, group and backup makes it trusted, never the other
    way). Whether it was added."""
    values = exemption_values(row, **kw)
    added = bool((await s.execute(pg_insert(BrainWithholdExemption).values(**values)
                                  .on_conflict_do_nothing())).rowcount)
    if not added and values["trusted"]:
        await s.execute(update(BrainWithholdExemption).where(
            BrainWithholdExemption.anchor_key == values["anchor_key"],
            BrainWithholdExemption.source_session_id == values["source_session_id"],
            BrainWithholdExemption.source_row_id == values["source_row_id"],
            BrainWithholdExemption.role == values["role"], BrainWithholdExemption.tool_call_id == values["tool_call_id"],
            BrainWithholdExemption.source_uid == values["source_uid"],
            func.coalesce(BrainWithholdExemption.source_at, literal_column("'-infinity'::timestamptz"))
            == func.coalesce(values["source_at"], literal_column("'-infinity'::timestamptz")),
            BrainWithholdExemption.decided_on == values["decided_on"], BrainWithholdExemption.trusted.is_(False))
            .values(trusted=True))
    return added


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
    """Imported and promoted memory items (not deleted, within `where`: the admin's scope) that carry the message's
    text: whose body contains its normalised text of at least MIN_CONTAINED characters, or any of whose fields carries
    it in another case or a derived form (carrier_relations, rebuild R3: the withhold then quarantines them,
    review.hold_carriers). Listed in the withhold preview. Ids and titles only."""
    needle = norm(message.content)
    if not flat(message.content):
        return []
    quick = quick_of(message.content)
    out = []
    for item_id, title, body in (await s.execute(select(MemoryItem.id, MemoryItem.title, MemoryItem.body).where(
            MemoryItem.origin.in_(("import", "promoted")), MemoryItem.deleted_at.is_(None), where)
            .order_by(MemoryItem.id))).all():
        fields = [x for x in (title or "", body or "") if x]
        if (len(needle) >= MIN_CONTAINED and fflat(needle) in fflat(body)) or (
                quick_related(fields, quick) and carrier_relations(fields, message.content)):
            # a title that itself carries the withheld text is never shown (rebuild R3: ids, never the text)
            shown = "" if title and carrier_relations([title], message.content) else title
            out.append({"id": int(item_id), "title": shown})
    return out


async def form_rows(s: AsyncSession, message: ArchiveMessage, *, exclude: Iterable[int] = ()
                    ) -> list[tuple[ArchiveMessage, str, tuple[str, ...]]]:
    """The withhold preview's carriers (rebuild R3): every readable stored row anywhere (other than the message and
    `exclude`) that carries the withheld text in another case or a derived form (carrier_relations over every field),
    unless an admin released it for this withhold; (row, JARVIS session, relations). The withhold holds each of them on
    its own (review.hold_carriers). Read-only, compared in memory only."""
    if not flat(message.content):
        return []
    skip = {int(message.id)} | {int(i) for i in exclude}
    quick = quick_of(message.content)
    names = dict((await s.execute(select(ArchiveSession.id, ArchiveSession.source_session_id))).all())
    out = []
    last = 0
    while True:
        batch = (await s.scalars(select(ArchiveMessage).where(ArchiveMessage.withheld.is_(False),
                                                              ArchiveMessage.id > last)
                                 .order_by(ArchiveMessage.id).limit(2000))).all()
        if not batch:
            break
        last = int(batch[-1].id)
        for r in batch:
            if r.id in skip:
                continue
            fields = row_fields(r)
            if not quick_related(fields, quick):
                continue
            rel = carrier_relations(fields, message.content)
            if not rel:
                continue
            sid = names.get(r.session_id, "")
            if message.source_key and await exempt_covers(s, message.source_key, sid, r):
                continue
            out.append((r, sid, tuple(relation_order(rel))))
    return out


async def titles_with_text(s: AsyncSession, message: ArchiveMessage) -> list[int]:
    """The conversations (archive ids) whose title carries the message's text in any case or form (title_relations and
    carrier_relations): hidden at the withhold (review.hold_carriers). Read-only."""
    wf = wform(message.content or "") if flat(message.content) else None
    if wf is None or wf.generic:
        return []
    out = []
    for sid, title in (await s.execute(select(ArchiveSession.id, ArchiveSession.title).where(
            ArchiveSession.title != "").order_by(ArchiveSession.id))).all():
        if carrier_relations([title_base(title)], message.content) or (
                title_relations(title, message.content) - {NOT_IN_BACKUP}):
            out.append(int(sid))
    return out


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
    from . import archive, groups, quotes

    # Option C first: every withhold's conversation group over what this database knows (recorded, never forgotten;
    # its titles hidden by the trigger), and the rows an uncertain link holds; repair_rows() then blanks the rollback
    # copies of the groups' rows (R9).
    from .review import hold_group_rows

    await groups.refresh(s, run_id=None)
    held = await groups.hold_uncertain(s, actor=actor, cause=cause)
    counts = await repair_rows(s, cause=cause)
    py = await finish_repair(s, actor=actor, cause=cause)
    if held:
        py["uncertain_rows_held"] += held
    # rebuild R2: the central sweep after R9/R10 (no withheld text in hand here): what they left, and the rollback
    # copies of the groups' deleted rows and of rows changed in place
    for k, v in (await hold_group_rows(s, actor=actor, cause=cause)).items():
        py[k] += v
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
                                                + int(out.get("group_rows_readable", 0)))
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
