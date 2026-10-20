"""Who may read which memory: each agent by its role, and each person by their dashboard role.

The rules are code, not data, so nothing imported can widen them.

Agents (through AgentMemory, read-only):
- read only approved, non-deleted items: the shared ones (agent = '') and their own. The Supervisor also reads every
  other agent's, except Credit Repair's;
- "customer" items and messages (contact details masked, or customer work) reach only the Supervisor and Email &
  Customer Communications, and never an AI model;
- Credit Repair's items are read by that agent alone; it never searches the conversation archive;
- the archive is searched by the Supervisor (all of it) and by an agent for the sessions mapped to it; a session
  that held restricted data is withheld entirely and never searched.

People (the JARVIS Brain page):
- viewers: approved, general items of the business kinds only; no conversation archive, no review queues;
- operators: also items waiting for review and the general archive; no customer items or messages;
- admins: everything, including customer items and messages and quarantined items;
- Credit Repair's items need the sensitive-data permission, whatever the role.
Restricted data itself (SSNs, card and bank details, a client's credit file) is never stored anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import BigInteger, ColumnElement, Text, and_, cast, exists, false, func, not_, or_, true

from ..models import ArchiveMessage, ArchiveSession, BrainTombstone, ImportRecord, MemoryItem
from . import AGENTS

SENSITIVE_AGENT = "credit_repair"
# What a viewer may read, and what may ever go into an AI prompt: facts, not rules or code.
BUSINESS_KINDS = ("business", "knowledge", "agent_note", "project", "decision", "accomplishment", "problem")
PROMPT_KINDS = ("business", "knowledge", "agent_note", "project")
# Flags that keep an item out of AI prompts and need an admin to approve it. source_withheld: an item promoted from a
# message that was withheld since, emptied (store.empty_withheld_copy); it is never approved again. purged_before: an
# imported item that is the same text as an item an admin purged (identity.repair, R8): an admin purges or approves it.
RISK_FLAGS = ("instruction_like", "approval_conflict", "secrets_redacted", "script", "plugin_or_hook", "custom",
              "owner_profile", "source_withheld", "purged_before")
# How an item promoted from an archived message names it (review.promote_message), and the brain_tombstones reason of
# a message an admin withheld (archive.py uses them too; they live here because archive imports store, which imports
# this module), and that of an item an admin purged (review.purge).
WITHHELD_BY_ADMIN = "withheld by an admin"
PURGED_BY_ADMIN = "purged by an admin"
PROMOTED_REF = "JARVIS session %s · message %s"  # Python's % and PostgreSQL's format() read it alike
# The title of a copy emptied because its message was withheld (store.empty_withheld_copy).
WITHHELD_COPY_TITLE = "Withheld: the archived message it was promoted from was withheld"
SOURCE_REMOVED = " (source removed)"  # added to a promoted item's source_ref when a rollback deletes its message
# PROMOTED_REF read back in SQL (a 0.6.0 copy names its message only so): SOURCE_REMOVED taken off, then the session
# id and the position.
PROMOTED_REF_REMOVED = r" \(source removed\)$"
PROMOTED_REF_SESSION = "^JARVIS session (.*) · message [0-9]+$"
PROMOTED_REF_POSITION = " · message ([0-9]{1,18})$"
# The import_records actions that name a row a message's source key was stored as (archive._older_rows; never a
# 'duplicate' record's: it names the row of the message it duplicates).
OLD_ROW_ACTIONS = ("imported", "withheld", "updated")


@dataclass(frozen=True)
class AgentPolicy:
    agent: str
    other_agents: bool = False  # the Supervisor: every agent's items except Credit Repair's
    customer: bool = False  # may read "customer" items and messages (never sent to an AI model)
    archive: str = "own"  # "all", "own" (sessions mapped to this agent) or "none"

    def describe(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "title": AGENTS.get(self.agent, self.agent),
            "reads_shared": True,
            "reads_own": True,
            "reads_other_agents": self.other_agents,
            "reads_customer_items": self.customer,
            "archive": self.archive,
            "statuses": ["approved"],
        }


POLICIES: dict[str, AgentPolicy] = {
    "supervisor": AgentPolicy("supervisor", other_agents=True, customer=True, archive="all"),
    "wordpress_security": AgentPolicy("wordpress_security"),
    "seo_content": AgentPolicy("seo_content"),
    "social_marketing": AgentPolicy("social_marketing"),
    "server_management": AgentPolicy("server_management"),
    "communications": AgentPolicy("communications", customer=True),
    "monitoring": AgentPolicy("monitoring"),
    # Shared business knowledge and its own notes only; no customer items, no archive.
    "credit_repair": AgentPolicy("credit_repair", archive="none"),
}


def policy_for(agent: str) -> AgentPolicy:
    try:
        return POLICIES[agent]
    except KeyError:
        raise ValueError(f"no memory policy for agent '{agent}'") from None


def withheld_source() -> ColumnElement[bool]:
    """SQL: an item promoted from a message that is withheld, matched as archive.withheld_copies() matches it: the row
    its flags.message_id names is withheld; a row with its flags.message_source_key is withheld (a rollback and a new
    import gave the message a new id), or that key has an admin's withhold tombstone (no row of it is left); or,
    promoted by 0.6.0 (no message_source_key), its message_id names no row and either its source_ref names a withheld
    row, or the import records name the source key that row held and that key is withheld (a withheld row has it, or
    an admin's withhold: the row was rolled back or rewound away). Also any item promoted from a conversation that is
    withheld for restricted data (flags.archive_session_id), whatever became of its row (finding 2)."""
    message_id = MemoryItem.flags["message_id"].as_integer()
    key = MemoryItem.flags["message_source_key"].astext
    # The legacy rule reads the session id and the position out of the item's source_ref, so the message is found
    # through the indexes (archive_sessions_source_session_idx, archive_messages_session_idx): comparing the
    # source_ref with one formatted for every withheld message read all of them, for each such item, in every query.
    ref = func.regexp_replace(MemoryItem.source_ref, PROMOTED_REF_REMOVED, "")
    position = cast(func.substring(ref, PROMOTED_REF_POSITION), BigInteger)
    legacy = and_(not_(MemoryItem.flags.has_key("message_source_key")),
                  not_(exists().where(ArchiveMessage.id == message_id).correlate(MemoryItem)),
                  exists().where(ArchiveSession.source_session_id == func.substring(ref, PROMOTED_REF_SESSION),
                                 ArchiveMessage.session_id == ArchiveSession.id,
                                 ArchiveMessage.position == position,
                                 ArchiveMessage.withheld.is_(True)).correlate(MemoryItem))
    # The import records name the source key the gone row held (record_type 'message', never a 'duplicate' record's).
    old_keys = exists().where(
        ImportRecord.record_type == "message", ImportRecord.target_table == "archive_messages",
        ImportRecord.target_id == message_id, ImportRecord.action.in_(OLD_ROW_ACTIONS),
        or_(exists().where(BrainTombstone.source_key == ImportRecord.source_key,
                           BrainTombstone.reason == WITHHELD_BY_ADMIN),
            exists().where(ArchiveMessage.source_key == ImportRecord.source_key,
                           ArchiveMessage.withheld.is_(True)))).correlate(MemoryItem)
    legacy_by_record = and_(not_(MemoryItem.flags.has_key("message_source_key")),
                            not_(exists().where(ArchiveMessage.id == message_id).correlate(MemoryItem)), old_keys)
    restricted = exists().where(ArchiveSession.id == MemoryItem.flags["archive_session_id"].as_integer(),
                                ArchiveSession.sensitivity == "restricted").correlate(MemoryItem)
    return and_(MemoryItem.origin == "promoted", or_(
        legacy_by_record, restricted,
        exists().where(ArchiveMessage.id == message_id, ArchiveMessage.withheld.is_(True)).correlate(MemoryItem),
        exists().where(ArchiveMessage.source_key == key, ArchiveMessage.withheld.is_(True)).correlate(MemoryItem),
        exists().where(BrainTombstone.source_key == key, BrainTombstone.reason == WITHHELD_BY_ADMIN
                       ).correlate(MemoryItem),
        legacy))


def purged_key() -> ColumnElement[bool]:
    """SQL: an item whose source key an admin purged (a restore of a dump older than the purge brought it back;
    identity.repair quarantines it, R8). A primary-key probe of brain_tombstones."""
    return exists().where(BrainTombstone.source_key == MemoryItem.source_key,
                          BrainTombstone.reason == PURGED_BY_ADMIN).correlate(MemoryItem)


def agent_items(policy: AgentPolicy, *, general_only: bool = False) -> ColumnElement[bool]:
    """The memory items an agent may read. Never an item promoted from a withheld message, whatever its status: one
    that was emptied (source_withheld), and one a withhold has not reached yet (withheld_source(): a database 0.6.0
    left behind, until the start-up check empties it, or a message withheld under another row id); never an item
    whose source an admin purged (purged_key(): back after a restore, until the start-up check quarantines it)."""
    scope = [MemoryItem.agent == "", MemoryItem.agent == policy.agent]
    if policy.other_agents:
        scope.append(MemoryItem.agent != SENSITIVE_AGENT)
    clauses = [MemoryItem.status == "approved", MemoryItem.deleted_at.is_(None), or_(*scope),
               not_(MemoryItem.flags.has_key("source_withheld")), not_(withheld_source()), not_(purged_key())]
    if general_only or not policy.customer:
        clauses.append(MemoryItem.sensitivity == "general")
    return and_(*clauses)


def agent_archive(policy: AgentPolicy, *, general_only: bool = False) -> ColumnElement[bool]:
    """The archived sessions an agent may search (combine with archive_messages through session_id)."""
    if policy.archive == "none":
        return false()
    clauses = [ArchiveSession.agent != SENSITIVE_AGENT, ArchiveSession.sensitivity != "restricted"]
    if general_only or not policy.customer:
        clauses.append(ArchiveSession.sensitivity == "general")
    if policy.archive == "own":
        clauses.append(ArchiveSession.agent == policy.agent)
    return and_(*clauses)


def agent_messages(policy: AgentPolicy, *, general_only: bool = False) -> ColumnElement[bool]:
    """Archived messages an agent may read: never withheld ones or copies, customer ones only where allowed, and never
    a row whose key an admin's withhold names (a row with such a key is always withheld, by the importer, the
    identity repair and the database guard; this holds even if one were not)."""
    clauses = [ArchiveMessage.withheld.is_(False),
               not_(exists().where(BrainTombstone.source_key == ArchiveMessage.source_key,
                                   BrainTombstone.reason == WITHHELD_BY_ADMIN).correlate(ArchiveMessage))]
    if general_only or not policy.customer:
        clauses.append(ArchiveMessage.sensitivity == "general")
    return and_(*clauses)


def needs_admin(kind: str, agent: str, status: str, flags: dict[str, object] | None) -> bool:
    """Whether approving (or editing into this state) needs an admin rather than an operator."""
    return (kind in ("instruction", "skill", "workflow") or agent == SENSITIVE_AGENT or status == "quarantined"
            or any((flags or {}).get(f) for f in RISK_FLAGS))


# --- People (the dashboard) -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonScope:
    role: str  # admin, operator or viewer
    sensitive: bool  # the user's can_access_sensitive permission

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def operates(self) -> bool:
        return self.role in ("admin", "operator")

    @property
    def sees_customer(self) -> bool:
        return self.role == "admin"

    @property
    def sees_archive(self) -> bool:
        return self.operates


def person_items(scope: PersonScope, *, include_deleted: bool = False) -> ColumnElement[bool]:
    """Items a person may list. Nobody, an admin included, sees an item promoted from a withheld message that still
    holds its text, until the start-up check or a rescan empties it (status and the pre-check count it meanwhile):
    one whose message is withheld (withheld_source()), and one flagged source_withheld that is not empty (0.6.0's
    withhold flagged a copy quarantined at promotion and kept its text). Once emptied (store.emptied()) it holds
    none, and the role rules alone apply. Only an admin sees an imported item whose source key an admin purged
    (purged_key(): a restore brought it back; the start-up check quarantines it for the admin to decide)."""
    clauses: list[ColumnElement[bool]] = []
    if not (include_deleted and scope.is_admin):
        clauses.append(MemoryItem.deleted_at.is_(None))
    if not scope.operates:
        clauses += [MemoryItem.status == "approved", MemoryItem.kind.in_(BUSINESS_KINDS)]
    elif not scope.is_admin:
        clauses.append(MemoryItem.status != "quarantined")
    flagged = MemoryItem.flags.has_key("source_withheld")
    emptied = and_(flagged, MemoryItem.title == WITHHELD_COPY_TITLE, MemoryItem.body == "",
                   cast(MemoryItem.tags, Text) == "[]")
    clauses.append(or_(emptied, and_(not_(flagged), not_(withheld_source()))))
    if not scope.is_admin:
        clauses.append(not_(purged_key()))
    if not scope.sees_customer:
        clauses.append(MemoryItem.sensitivity == "general")
    if not scope.sensitive:
        clauses.append(MemoryItem.agent != SENSITIVE_AGENT)
    return and_(*clauses) if clauses else true()


def person_sessions(scope: PersonScope) -> ColumnElement[bool]:
    """Sessions a person may list. A restricted session is listed (dates, staff, counts) for admins with the
    sensitive-data permission only; its text was never stored."""
    if not scope.sees_archive:
        return false()
    clauses: list[ColumnElement[bool]] = []
    if not scope.sensitive:
        clauses += [ArchiveSession.agent != SENSITIVE_AGENT, ArchiveSession.sensitivity != "restricted"]
    if not scope.sees_customer:
        clauses.append(ArchiveSession.sensitivity == "general")
    return and_(*clauses) if clauses else true()


def person_messages(scope: PersonScope) -> ColumnElement[bool]:
    """Messages a person may read. Withheld messages show as withheld (no text is stored for them)."""
    if not scope.sees_archive:
        return false()
    if scope.sees_customer:
        return true()
    return ArchiveMessage.sensitivity == "general"
