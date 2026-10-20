"""JARVIS Brain 0.6.2: the identity of a withheld message (app/brain/identity.py), without a database.

The rules that decide whether a source row is the same JARVIS message as a withheld one: the key's parts, the
fingerprint a withheld row keeps (its state only), the certain rules (key, JARVIS row, fold), the independence guard,
copy positions, the importer's links and the purge fingerprint key. Synthetic values only.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.brain import identity
from app.brain.importer import MessagePlan, Processor
from app.brain.source import SourceMessage, parse_uid_list

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def msg(row_id: int, content: str = "hello there", *, role: str = "user", session: str = "S1", uid: str = "",
        key: str | None = None, active: int | None = 1, compacted: int | None = 0, at: datetime | None = T0,
        absorbed: tuple[str, ...] = (), tool_call_id: str = "") -> SourceMessage:
    key = key or ("jarvis:message:uid:%s" % uid if uid else "jarvis:message:%s:%d" % (session, row_id))
    return SourceMessage(id=row_id, session_id=session, role=role, content=content, tool_name="",
                         tool_call_id=tool_call_id, tool_calls_raw=None, at=at, time_assumed_local=False,
                         active=active, compacted=compacted, key=key, uid=uid, absorbed=absorbed)


def stone(key: str, **kw) -> identity.Stone:
    return identity.Stone(key=key, **kw)


def nowhere(session: str, row: str) -> bool:
    return False


def present(session: str, row: str) -> bool:
    return True


def test_parse_key_row_and_uid_forms_and_colons_in_session_ids():
    assert identity.parse_key("jarvis:message:20261101_0101:42") == ("20261101_0101", "42", "")
    assert identity.parse_key("jarvis:message:uid:abc-123") == ("", "", "abc-123")
    # A session id holding colons (a gateway id): the row id is after the last one.
    assert identity.parse_key("jarvis:message:agent:main:slack:dm:U1:7") == ("agent:main:slack:dm:U1", "7", "")
    assert identity.parse_key("jarvis:message:uid:") == ("", "", "")
    for other in ("", None, "jarvis:session:S1", "jarvis:file:profile/a.md", "jarvis:message:nocolon"):
        assert identity.parse_key(other) == ("", "", "")


def test_withheld_fp_distinguishes_live_compacted_and_rewound():
    assert identity.withheld_fp(msg(1)) == "w1:live"
    assert identity.withheld_fp(msg(1, compacted=1, active=0)) == "w1:compacted"
    assert identity.withheld_fp(msg(1, compacted=0, active=0)) == "w1:rewound"
    assert identity.withheld_fp(msg(1, compacted=None, active=0)) == "w1:rewound"
    # No fingerprint of the text: two different texts in the same state give the same value.
    assert identity.withheld_fp(msg(1, "a secret")) == identity.withheld_fp(msg(2, "another one"))


def test_same_source_w1_row_is_unchanged_when_content_changes_and_changed_when_rewound():
    m = msg(1, "the text JARVIS holds now")
    withheld = {"source_fp": "w1:live", "withheld": True}
    assert identity.same_source(withheld, m)  # the text changed, the row is withheld either way
    assert not identity.same_source(withheld, msg(1, active=0))  # rewound since: the row goes
    assert not identity.same_source(withheld, msg(1, compacted=1, active=0))  # compacted since: updated
    assert not identity.same_source({"source_fp": "w1:live", "withheld": False}, m)
    assert identity.same_source({"source_fp": m.fp, "withheld": False}, m)
    assert not identity.same_source({"source_fp": msg(1, "older text").fp, "withheld": False}, m)
    assert not identity.same_source({"source_fp": "", "withheld": True}, m)  # a released hold comes back


def test_certain_key_bound_same_row_matches():
    ids = identity.Identities([stone("jarvis:message:uid:U", session="S1", row="5", role="user", uid="U",
                                     uid_known=True, bound=True)])
    found = ids.certain("S1", msg(5, uid="U"), present)
    assert isinstance(found, identity.Match) and (found.via, found.review) == ("key", "")
    # An unbound stone (no row known) matches by its key alone.
    ids = identity.Identities([stone("jarvis:message:S1:9")])
    assert ids.certain("S1", msg(9), present).via == "key"


def test_certain_key_bound_row_gone_passes_to_copy_only_with_same_role():
    ids = identity.Identities([stone("jarvis:message:uid:U", session="S1", row="5", role="assistant", uid="U",
                                     bound=True)])
    # Row 5 is gone from the backup (replace_messages renumbered it): the uid key passed to its next holder.
    passed = ids.certain("S1", msg(8, uid="U", role="assistant"), nowhere)
    assert isinstance(passed, identity.Match) and passed.via == "key"
    # ...but not to a row of another role: that is another message, left out as a key collision.
    assert ids.certain("S1", msg(8, uid="U", role="user"), nowhere) is identity.MOVED


def test_certain_key_bound_row_present_is_moved():
    """C2: the withheld message's key now names another JARVIS row, and the row it was bound to is still there."""
    ids = identity.Identities([stone("jarvis:message:uid:X", session="S3", row="165", role="assistant", uid="X",
                                     bound=True)])
    assert ids.certain("S3", msg(164, uid="X", role="user", session="S3"), present) is identity.MOVED
    # The withheld row itself, under its new row-form key, is matched by its JARVIS row.
    found = ids.certain("S3", msg(165, role="assistant", session="S3"), present)
    assert isinstance(found, identity.Match) and found.via == "same JARVIS row"


def test_certain_row_binding_role_equal_unknown_other():
    for role, matched in (("assistant", True), ("", True)):
        ids = identity.Identities([stone("jarvis:message:S1:7", session="S1", row="7", role=role, bound=True)])
        found = ids.certain("S1", msg(7, role="assistant", key="jarvis:message:uid:new"), present)
        assert isinstance(found, identity.Match) == matched and found.via == "same JARVIS row"
    ids = identity.Identities([stone("jarvis:message:S1:7", session="S1", row="7", role="user", bound=True)])
    assert ids.certain("S1", msg(7, role="tool", key="jarvis:message:uid:new"), present) is None
    assert ids.certain("S2", msg(7, role="user", session="S2"), present) is None  # another conversation


def test_certain_absorbed_uid_and_fold_survivor():
    ids = identity.Identities([stone("jarvis:message:uid:U", session="S1", row="3", role="user", uid="U",
                                     bound=True)])
    folded = ids.certain("S1", msg(40, uid="V", absorbed=("Q", "U")), present)
    assert isinstance(folded, identity.Match) and folded.via == "merged into another message"
    assert folded.review == "" and folded.stone.key == "jarvis:message:uid:U"
    # The survivor that keeps the withheld uid and absorbed others holds the withheld text too.
    survivor = ids.certain("S1", msg(41, uid="U", key="jarvis:message:S1:41", absorbed=("Q",)), present)
    assert isinstance(survivor, identity.Match) and survivor.via == "merged into another message"
    # A hold's uid is the held row's own: it never matches anything by itself.
    held = identity.Identities([stone("jarvis:message:S1:9", session="S1", row="9", uid="H", review="pending",
                                      bound=True, anchor="jarvis:message:uid:U")])
    assert held.certain("S1", msg(50, uid="Z", absorbed=("H",)), present) is None
    assert parse_uid_list('["a", "", "b", "a", 3]') == ("a", "b")
    assert parse_uid_list("not json") == () and parse_uid_list(None) == () and parse_uid_list('{"a": 1}') == ()


def test_a_kept_hold_matches_as_a_member_and_a_pending_one_as_the_hold_itself():
    kept = identity.Identities([stone("jarvis:message:S1:9", session="S1", row="9", role="user", review="kept",
                                      bound=True, anchor="jarvis:message:uid:U")])
    found = kept.certain("S1", msg(9), present)
    assert (found.via, found.review) == ("key", "")
    by_row = kept.certain("S1", msg(9, key="jarvis:message:uid:other"), present)
    assert (by_row.via, by_row.review) == ("same JARVIS row", "")
    assert kept.match_row("jarvis:message:S1:9", "S1", "9", "user", "").review == ""
    pending = identity.Identities([stone("jarvis:message:S1:9", session="S1", row="9", role="user", review="pending",
                                         bound=True, anchor="jarvis:message:uid:U")])
    assert pending.certain("S1", msg(9), present).review == "pending"
    # A certain rule makes a hold a member, in memory as put_stone() does in the database; never the reverse.
    upgraded = pending.add(stone("jarvis:message:S1:9", review="", via="same message id",
                                 anchor="jarvis:message:uid:U"))
    assert upgraded.review == "" and upgraded.row == "9"
    assert pending.add(stone("jarvis:message:S1:9", review="pending")).review == ""


def test_independent_requires_known_uid():
    known = identity.Identities([stone("jarvis:message:uid:U", uid="U", uid_known=True, bound=True, row="1",
                                       session="S1")])
    assert known.independent("jarvis:message:uid:U", "OTHER")  # its own uid: another Hermes message
    assert not known.independent("jarvis:message:uid:U", "U")
    assert not known.independent("jarvis:message:uid:U", "")  # no uid: nothing says it is another message
    # The uid was only learned (a row-form key withheld while its uid was NULL): no row is independent of it.
    learned = identity.Identities([stone("jarvis:message:S1:1", uid="U", uid_known=False, bound=True, row="1",
                                         session="S1")])
    assert not learned.independent("jarvis:message:S1:1", "OTHER")


def test_copied_by_history_uses_child_started_at():
    child = SimpleNamespace(lineage="branch", started_at=T0)
    assert identity.copied_by_history(msg(1, at=T0 - timedelta(days=3)), child)
    assert identity.copied_by_history(msg(1, at=T0), child)
    assert not identity.copied_by_history(msg(1, at=T0 + timedelta(seconds=1)), child)  # a new turn of the child
    assert not identity.copied_by_history(msg(1, at=None), child)
    assert identity.copied_by_history(msg(1, at=T0), SimpleNamespace(lineage="continuation", started_at=T0))
    for lineage in ("delegated", ""):  # a subagent session or a plain one never copied history
        assert not identity.copied_by_history(msg(1, at=T0), SimpleNamespace(lineage=lineage, started_at=T0))
    assert not identity.copied_by_history(msg(1, at=T0), SimpleNamespace(lineage="branch", started_at=None))


def test_twin_links_skip_conflicting_uids():
    withheld = stone("jarvis:message:uid:U", uid="U", bound=True, session="S1", row="1")
    original = msg(1, "the words", uid="U", active=0)
    same_uid = msg(5, "the words", uid="U", key="jarvis:message:S1:5")
    no_uid = msg(6, "the words")
    other = msg(7, "the words", uid="W")
    by_key = {m.key: m for m in (original, same_uid, no_uid, other)}
    plans = [MessagePlan(original, "excluded", "rewound or superseded"),
             MessagePlan(same_uid, "import", twin_of=original.key),
             MessagePlan(no_uid, "import", twin_of=original.key),
             MessagePlan(other, "import", twin_of=original.key)]
    held = {original.key: identity.Match(withheld, "key", "")}
    Processor._links(plans, held, by_key)
    assert held[same_uid.key].via == "copied by JARVIS" and held[no_uid.key].via == "copied by JARVIS"
    assert other.key not in held  # both carry uids and they differ: two Hermes messages
    # A carried-on copy or an adjacent repeat inherits the review of the row it repeats.
    repeat = msg(8, "the words", key="jarvis:message:S1:8")
    hold = {original.key: identity.Match(withheld, "same text", "pending")}
    Processor._links([MessagePlan(repeat, "duplicate", identity.SAME_AS_PREVIOUS, duplicate_of=original.key)], hold,
                     {**by_key, repeat.key: repeat})
    assert (hold[repeat.key].via, hold[repeat.key].review) == ("duplicate", "pending")
    unrelated = msg(9, "the words", key="jarvis:message:S1:9")
    Processor._links([MessagePlan(unrelated, "duplicate", "the same text appears earlier in the file",
                                  duplicate_of=original.key)], hold, {**by_key, unrelated.key: unrelated})
    assert unrelated.key not in hold  # only the importer's provenance links


def test_purge_key_known_answer_key_id_absent_password():
    settings = SimpleNamespace(database_url="postgresql+asyncpg://cc:s3cret-pw@db.local:5432/cc")
    key = identity.purge_key(settings)
    assert key == hmac.new(b"infinite-ai/jarvis-brain/purge-fingerprint/v1", b"s3cret-pw", hashlib.sha256).digest()
    key_id = hmac.new(key, b"key id", hashlib.sha256).hexdigest()[:8]
    assert identity.purge_key_id(key) == key_id
    sha = hashlib.sha256(b"a purged file").hexdigest()
    fp = identity.purge_fp(key, sha)
    assert fp == "k1:%s:%s" % (key_id, hmac.new(key, sha.encode(), hashlib.sha256).hexdigest())
    assert sha not in fp  # never the plain hash
    other = identity.purge_key(SimpleNamespace(database_url="postgresql+asyncpg://cc:another@db.local/cc"))
    assert other != key and identity.purge_key_id(other) != key_id
    assert identity.purge_key(SimpleNamespace(database_url="postgresql+asyncpg://cc@db.local/cc")) is None
    # An index without a key still holds a purge by an entry's digest and by a copy in the same backup.
    index = identity.PurgeIndex(entry_digests={"0123456789abcdef"})
    entry = SimpleNamespace(key="jarvis:file:profile/memories/USER.md#entry-0123456789abcdef",
                            record_type="memory_entry", sha256="")
    assert index.same_content(entry, set())
    copy = SimpleNamespace(key="jarvis:file:profile/knowledge/c.md", record_type="file", sha256=sha)
    assert index.same_content(copy, {sha}) and not index.same_content(copy, set())
    keyed = identity.PurgeIndex(fps={fp}, key=key, key_id=key_id)
    assert keyed.same_content(copy, set())


def test_quote_line_matches_recall_shapes_only():
    """quotes.QUOTE_LINE (spec 11): exactly the two lines supervisor.recall writes (_message_line, _item_line), at the
    end of a line; never a digest line of another agent's result that says "(source: ...)" in passing."""
    from zoneinfo import ZoneInfo

    from app.brain import quotes
    from app.supervisor import recall

    session = SimpleNamespace(title="Website check", source_session_id="abc", staff="jarvis", started_at=T0)
    message = SimpleNamespace(content="The certificate was renewed (source: the vendor)", at=T0, position=4)
    item = SimpleNamespace(title="Renewal", body="Renewed on time.", source_ref="JARVIS session abc · message 4",
                           source_at=T0, created_at=T0, kind="project", id=17)
    tz = ZoneInfo("UTC")
    message_line = recall._message_line(SimpleNamespace(message=message, session=session), ["certificate"], tz)
    item_line = recall._item_line(item, ["renewed"], tz)
    untitled = recall._message_line(SimpleNamespace(message=message, session=SimpleNamespace(
        **{**session.__dict__, "title": ""})), [], tz)
    for line in (message_line, item_line, untitled):
        assert quotes.QUOTE_LINE.search(line), line
    for line in ("- seo_content.draft_blog_post: Draft written (source: the site's blog feed)",
                 "- monitoring.uptime: all up (source: monitoring · uptime)",
                 "(source: Website check · message 4) and then more words", quotes.REMOVED, "### Accomplishments",
                 "- 2026-10-01 · jarvis · \"renewed\" (source: Website check · message four)"):
        assert not quotes.QUOTE_LINE.search(line), line
    # strip_summary removes only the quoting lines no task holds; strip_dead the lines and notes of dead sources.
    summary = "\n".join(["Results:", "- seo_content.draft_blog_post: Draft written (source: the blog)", message_line,
                         item_line])
    assert quotes.strip_summary(summary, {item_line}).split("\n") == [
        "Results:", "- seo_content.draft_blog_post: Draft written (source: the blog)", quotes.REMOVED, item_line]
    data = {"answer": ["### Accomplishments", message_line, item_line], "sources": [None, {"message": 9}, {"item": 17}],
            "memory_notes": [{"id": 17, "title": "Renewal"}, {"id": 18, "title": "Other"}]}
    out = quotes.strip_dead(data, {9}, {17})
    assert out["answer"] == ["### Accomplishments", quotes.REMOVED, quotes.REMOVED]
    assert out["sources"] == [None, None, None] and out["memory_notes"] == [{"id": 18, "title": "Other"}]
    assert data["answer"][1] == message_line  # a copy: the stored result is not changed
    assert quotes.strip_dead(data, set(), set()) == data


def test_verify_scans_rollback_copies_without_their_identifiers():
    """verify's leak scan (no_secrets_or_restricted_data) skips identifiers in a rollback copy as it does in a row: a
    sha256 fingerprint can hold a run of digits the card detector accepts, and key adoption (C1) writes a copy of every
    row it re-keys. Text in the copy is still scanned, at its top level and in a deleted_row."""
    from app.brain import importer

    fp = "707abf6f7a409e2f23e31b112332b3379788117195cbfe61b7a8875298a1e65d"  # holds 3379788117195 (Luhn-valid)
    assert importer.leak_types(fp) == ["payment_card"]
    copy = {"source_key": "jarvis:message:S1:130", "source_fp": fp, "source_uid": "", "content": "Plain words.",
            "role": "user", "withheld": False}
    assert importer._row_leaks("import_records", {"id": 1, "before": importer._without_ids(copy)}) == []
    assert importer._row_leaks("import_records", {"id": 2, "before": importer._without_ids(
        {"deleted_row": copy})}) == []
    card = {**copy, "content": "Card 4111 1111 1111 1111"}
    for before in (card, {"deleted_row": card}):
        [leak] = importer._row_leaks("import_records", {"id": 3, "before": importer._without_ids(before)})
        assert leak["column"] == "before" and leak["types"] == ["payment_card"]
