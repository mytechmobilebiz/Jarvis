"""Runs the vendored Hermes's own functions on scratch state.db files and prints every row before and after.

Usage (an isolated subprocess, so Hermes's top-level modules never mix with the Command Center's):
    python -I hermes_driver.py <HERMES_SRC> <out.json>

Third-party modules Hermes imports at module level but these code paths never call (ruamel.yaml for its config file,
rich for console output) are replaced by inert stubs; nothing is installed. Each scenario uses its own fresh
SessionDB. Synthetic data only.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SRC, OUT = sys.argv[1], sys.argv[2]
sys.path.insert(0, SRC)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes_home_")
warnings.filterwarnings("ignore")
STUB_ROOTS = {"ruamel", "rich"}


class _Stub(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = type(name, (), {"__init__": lambda self, *a, **k: None, "__call__": lambda self, *a, **k: None})
        setattr(self, name, value)
        return value


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):  # noqa: ANN001
        if name.split(".")[0] in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):  # noqa: ANN001
        module = _Stub(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):  # noqa: ANN001
        pass


sys.meta_path.append(_StubFinder())

import hermes_state  # noqa: E402
import hermes_state_schema  # noqa: E402
from agent import context_compressor as cc  # noqa: E402
from agent.message_metadata import MESSAGE_UID, message_identity, record_absorbed_message  # noqa: E402
from agent.micro_compaction import MicroCompactionMixin  # noqa: E402
from gateway import run_inbound  # noqa: E402
from gateway.slash_commands_session import _branch_row  # noqa: E402
from hermes_cli.cli_commands_mixin import _BRANCH_COPY_KEYS  # noqa: E402

T = 1_795_000_000.0
SUMMARY = "[CONTEXT COMPACTION] Summary of earlier turns: the work so far is recorded above."
STUB = "[tool output pruned to save context]"
ROW_COLS = ("id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp", "active",
            "compacted", "message_uid", "absorbed_message_uids")
SESSION_COLS = ("id", "source", "parent_session_id", "model_config", "end_reason", "title", "started_at")


def fresh() -> tuple[Any, str]:
    d = tempfile.mkdtemp(prefix="hermes_conf_")
    path = os.path.join(d, "state.db")
    return hermes_state.SessionDB(Path(path)), path


def dump(path: str) -> dict[str, Any]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = [{k: r[k] for k in ROW_COLS} for r in con.execute("SELECT * FROM messages ORDER BY id")]
        sessions = [{k: r[k] for k in SESSION_COLS} for r in con.execute("SELECT * FROM sessions ORDER BY id")]
        seq = con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
        return {"rows": rows, "sessions": sessions, "seq": seq[0] if seq else 0}
    finally:
        con.close()


def raw(path: str, sql: str, *args: Any) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def turn(db: Any, sid: str, t: float, user: str, answer: str, *, tool: str | None = None,
         call: str = "call_aa") -> None:
    db.append_message(sid, "user", user, timestamp=t)
    if tool is not None:
        db.append_message(sid, "assistant", "", timestamp=t + 1, tool_calls=[
            {"id": call, "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}}])
        db.append_message(sid, "tool", tool, timestamp=t + 2, tool_call_id=call, tool_name="web_fetch")
    db.append_message(sid, "assistant", answer, timestamp=t + 3)


def live(db: Any, sid: str) -> list[dict[str, Any]]:
    return [m for m in db.get_messages(sid) if m.get("role") != "system"]


def base(sid: str = "s1", source: str = "cli") -> tuple[Any, str]:
    db, path = fresh()
    db.create_session(sid, source, model="m")
    turn(db, sid, T + 10, "Please finish the vendor review before the weekly call.",
         "Done, the vendor review is ready.", tool="Fetched page for the vendor review: headings and notes.")
    turn(db, sid, T + 20, "Please summarise the quarterly plan for the team meeting.",
         "Here is the update on the quarterly plan.")
    return db, path


RESULTS: dict[str, Any] = {}


def scenario(name: str):  # noqa: ANN201
    def wrap(fn):  # noqa: ANN001, ANN202
        try:
            RESULTS[name] = fn()
        except Exception as exc:  # reported, never hidden: the test for this scenario fails with it
            import traceback
            RESULTS[name] = {"error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-2000:]}
        return fn
    return wrap


@scenario("append")
def _append() -> dict:
    db, path = fresh()
    db.create_session("s1", "cli", model="m")
    before = dump(path)
    turn(db, "s1", T + 10, "Please prepare the content calendar.", "Finished the content calendar.",
         tool="Read file for the content calendar.")
    return {"before": before, "after": dump(path)}


@scenario("same_second_repeat")
def _same_second() -> dict:
    db, path = fresh()
    db.create_session("s1", "telegram", model="m")
    db.append_message("s1", "user", "yes please", timestamp=T + 5)
    before = dump(path)
    db.append_message("s1", "user", "yes please", timestamp=T + 5)  # an independent message in the same second
    return {"before": before, "after": dump(path)}


def _compaction_list(db: Any, sid: str, head_n: int, tail_n: int, restate: str | None) -> tuple[list, int]:
    msgs = live(db, sid)
    head, tail = msgs[:head_n], (msgs[len(msgs) - tail_n:] if tail_n else [])
    out = [dict(m) for m in head]
    summary = {"role": "assistant", "content": SUMMARY}
    out.append(summary)
    out += [dict(m) for m in tail]
    if restate:
        users = [m for m in msgs[:len(msgs) - tail_n] if m["role"] == "user"]
        inflight = users[-1]
        task = cc._restated_request_text(inflight.get("content"))
        if restate == "row":
            replay = cc._fresh_compaction_message_copy(inflight)
            replay.pop("timestamp", None)
            replay["content"] = cc._INFLIGHT_TASK_REPLAY_HEADER + "\n" + task
            uid = replay.get(MESSAGE_UID)
            out = [m for m in out if not (m is not summary and m.get("role") == "user" and m.get(MESSAGE_UID) == uid)]
            out.append(replay)
        else:
            summary["content"] = SUMMARY + "\n\n" + cc._INFLIGHT_TASK_REPLAY_HEADER + "\n" + task
            record_absorbed_message(summary, inflight)
    return out, len(tail)


def _compact(head_n: int, tail_n: int, restate: str | None, legacy_row: int | None) -> dict:
    db, path = base()
    if legacy_row is not None:
        raw(path, "UPDATE messages SET message_uid = NULL WHERE id = ?", legacy_row)
    before = dump(path)
    msgs, tail_count = _compaction_list(db, "s1", head_n, tail_n, restate)
    db.archive_and_compact("s1", msgs, tail_count=tail_count)
    return {"before": before, "after": dump(path),
            "params": {"head_n": head_n, "tail_n": tail_n, "restate": restate}}


scenario("compact_head_tail")(lambda: _compact(1, 2, None, 1))
scenario("compact_no_head")(lambda: _compact(0, 3, None, None))
scenario("compact_restate_row")(lambda: _compact(1, 1, "row", None))
scenario("compact_restate_carrier")(lambda: _compact(0, 1, "carrier", None))


def _rotate(donor: bool, legacy: bool) -> dict:
    db, path = fresh()
    db.create_session("r1", "cli", model="m")
    # r1 says the same words twice (two independent answers); only the later one is carried
    db.append_message("r1", "user", "Please compare the pricing table.", timestamp=T + 1)
    db.append_message("r1", "assistant", "Sounds good.", timestamp=T + 2)
    db.append_message("r1", "user", "And the landing page copy too.", timestamp=T + 3)
    db.append_message("r1", "assistant", "Sounds good.", timestamp=T + 4)
    if legacy:
        raw(path, "UPDATE messages SET message_uid = NULL")
    before = dump(path)
    msgs = live(db, "r1")
    carried = [dict(msgs[0])] + [{"role": "assistant", "content": SUMMARY}] + [dict(m) for m in msgs[-2:]]
    for m in carried:
        m.pop("timestamp", None)  # handoff rows arrive without a timestamp
    cls = type(db)
    saved = cls._carry_parent_timestamps
    if not donor:
        cls._carry_parent_timestamps = lambda self, conn, parent, messages: None  # an older Hermes
    try:
        db._execute_write(lambda conn: db._publish_compression_child_on_conn(
            conn, parent_session_id="r1", child_session_id="r1c", source="cli", messages=carried, model="m",
            model_config={"_compressed_from": "r1"}, require_compression_lease=False))
    finally:
        cls._carry_parent_timestamps = saved
    return {"before": before, "after": dump(path), "params": {"head_n": 1, "tail_n": 2, "donor": donor}}


scenario("rotate_donor")(lambda: _rotate(True, False))
scenario("rotate_no_donor")(lambda: _rotate(False, False))
scenario("rotate_legacy_rows")(lambda: _rotate(True, True))


def _prune(legacy: bool) -> dict:
    db, path = base()
    tool_id = [r["id"] for r in dump(path)["rows"] if r["role"] == "tool"][0]
    if legacy:
        raw(path, "UPDATE messages SET message_uid = NULL WHERE id = ?", tool_id)
    before = dump(path)
    original = [m for m in live(db, "s1") if m["id"] == tool_id][0]
    original["_row_id"] = tool_id
    replacement = dict(original, content=STUB)
    db.rewrite_pruned_rows("s1", [(original, replacement)])
    return {"before": before, "after": dump(path), "params": {"row": tool_id}}


scenario("prune_with_uid")(lambda: _prune(False))
scenario("prune_null_uid")(lambda: _prune(True))


def _fold(second_has_uid: bool) -> dict:
    db, path = fresh()
    db.create_session("f1", "cli", model="m")
    db.append_message("f1", "user", "Please tidy up the support backlog.", timestamp=T + 1)
    db.append_message("f1", "assistant", "Done, the backlog is tidy.", timestamp=T + 2)
    db.append_message("f1", "user", "Please look again at the backup schedule.", timestamp=T + 3)  # no reply yet
    db.append_message("f1", "user", "Please archive the partner agreement.", timestamp=T + 4)
    db.append_message("f1", "assistant", "Finished both of them.", timestamp=T + 5)
    ids = [r["id"] for r in dump(path)["rows"]]
    if not second_has_uid:
        raw(path, "UPDATE messages SET message_uid = NULL WHERE id = ?", ids[3])
    before = dump(path)
    msgs = live(db, "f1")
    for m in msgs:
        m["_row_id"] = m["id"]
        m[cc._DB_PERSISTED_MARKER] = True
    merged = MicroCompactionMixin._merge_adjacent_user_turns(SimpleNamespace(), msgs)
    carried = [m for m in merged if m.get(cc._DB_PERSISTED_MARKER)]
    db.archive_and_compact("f1", merged, carried_messages=carried)
    return {"before": before, "after": dump(path), "params": {"pair": [ids[2], ids[3]]}}


scenario("fold_with_uid")(lambda: _fold(True))
scenario("fold_no_uid")(lambda: _fold(False))


def _branch(how: str, legacy_row: int | None) -> dict:
    db, path = base()
    if legacy_row is not None:
        raw(path, "UPDATE messages SET message_uid = NULL WHERE id = ?", legacy_row)
    before = dump(path)
    history = live(db, "s1")
    db.create_session("s1b", "cli", model="m", parent_session_id="s1", model_config={"_branched_from": "s1"})
    if how == "cli":
        rows = [{"role": m.get("role", "user"), "tool_name": m.get("tool_name"),
                 **{k: m.get(k) for k in _BRANCH_COPY_KEYS}, **message_identity(m, with_tool_uids=True)}
                for m in history]
        raw(path, "UPDATE sessions SET end_reason = 'branched' WHERE id = 's1'")
    else:
        rows = [_branch_row(m) for m in history]
    db.append_messages_batch("s1b", rows, chunk_rows=500)
    return {"before": before, "after": dump(path)}


scenario("cli_branch")(lambda: _branch("cli", 1))
scenario("gateway_branch")(lambda: _branch("gateway", None))


@scenario("cli_retry")
def _cli_retry() -> dict:
    db, path = base()
    before = dump(path)
    out = db.rewind_user_turn("s1", -1)
    db.append_message("s1", "user", out.live_text, timestamp=T + 100)
    db.append_message("s1", "assistant", "Again: the quarterly plan update.", timestamp=T + 101.5)
    return {"before": before, "after": dump(path)}


@scenario("gateway_retry")
def _gateway_retry() -> dict:
    db, path = base("g1", "telegram")
    before = dump(path)
    msgs = live(db, "g1")
    last = max(i for i, m in enumerate(msgs) if m["role"] == "user")
    truncated = [dict(m) for m in msgs[:last]]
    db.replace_messages("g1", truncated, active_only=True, reject_active_turn_lease=True)
    db.append_message("g1", "user", msgs[last]["content"], timestamp=T + 100)
    db.append_message("g1", "assistant", "Again: the quarterly plan update.", timestamp=T + 101.5)
    return {"before": before, "after": dump(path)}


def _replace(archive_dropped: bool) -> dict:
    db, path = base()
    # an earlier compaction left archived rows behind: a destructive replace deletes them too
    msgs, tail_count = _compaction_list(db, "s1", 1, 2, None)
    db.archive_and_compact("s1", msgs, tail_count=tail_count)
    before = dump(path)
    rows = live(db, "s1")
    last = max(i for i, m in enumerate(rows) if m["role"] == "user")
    kept = [dict(m) for m in rows[:last]]
    db.replace_messages("s1", kept, archive_dropped=archive_dropped)
    return {"before": before, "after": dump(path), "params": {"archive_dropped": archive_dropped}}


scenario("replace_destructive")(lambda: _replace(False))
scenario("replace_archive_dropped")(lambda: _replace(True))


@scenario("delete_session")
def _delete() -> dict:
    db, path = base("d1")
    db.create_session("d1b", "telegram", model="m", parent_session_id="d1", model_config={"_branched_from": "d1"})
    db.append_message("d1b", "user", "Please rewrite the landing page copy.", timestamp=T + 50)
    db.create_session("d1r", "cli", model="m", parent_session_id="d1", model_config={"_compressed_from": "d1"})
    db.append_message("d1r", "user", "Please send over the newsletter draft.", timestamp=T + 60)
    before = dump(path)
    db.delete_session("d1")
    return {"before": before, "after": dump(path)}


@scenario("reset")
def _reset() -> dict:
    db, path = base("e1", "whatsapp")
    before = dump(path)
    # gateway/session_recovery.py: the new session row carries parent and _reset_from in its INSERT; nothing copied
    db.create_session("e1n", "whatsapp", model="m", parent_session_id="e1", model_config={"_reset_from": "e1"})
    return {"before": before, "after": dump(path),
            "fork_child": db.is_explicit_fork_child("e1n") if hasattr(db, "is_explicit_fork_child") else None}


@scenario("backfill_slice")
def _backfill() -> dict:
    db, path = base()
    raw(path, "UPDATE messages SET message_uid = NULL")
    raw(path, "DELETE FROM state_meta WHERE key IN (?, ?)", hermes_state_schema._MESSAGE_UID_BACKFILL_DONE,
        hermes_state_schema._MESSAGE_UID_BACKFILL_CURSOR)
    before = dump(path)
    hermes_state_schema._MESSAGE_UID_BACKFILL_CHUNK = 2
    hermes_state_schema._MESSAGE_UID_BACKFILL_BUDGET_S = 0.0
    con = sqlite3.connect(path, isolation_level=None)
    try:
        db._advance_message_uid_backfill(con.cursor())
    finally:
        con.close()
    return {"before": before, "after": dump(path), "params": {"chunk": 2}}


@scenario("snapshot_restore")
def _snapshot() -> dict:
    db, path = fresh()
    db.create_session("k1", "cli", model="m")
    db.append_message("k1", "user", "Please finish the keyword sweep.", timestamp=T + 1)
    db.append_message("k1", "assistant", "Done, the keyword sweep is ready.", timestamp=T + 2)
    before = dump(path)
    db.close() if hasattr(db, "close") else None
    shutil.copyfile(path, path + ".snap")
    db = hermes_state.SessionDB(Path(path))
    db.append_message("k1", "user", "Please archive the partner agreement.", timestamp=T + 3)
    lost = dump(path)
    db.close() if hasattr(db, "close") else None
    shutil.copyfile(path + ".snap", path)  # hermes backup import: sqlite_sequence comes back with the file
    db = hermes_state.SessionDB(Path(path))
    db.append_message("k1", "user", "Please rewrite the landing page copy.", timestamp=T + 4)
    return {"before": before, "lost": lost, "after": dump(path)}


@scenario("wrappers")
def _wrappers() -> dict:
    event = SimpleNamespace(reply_to_text="the vendor review before the weekly call", reply_to_message_id="m1",
                            reply_to_is_own_message=False, message_id="1234567890")
    own = SimpleNamespace(reply_to_text="Done, the vendor review is ready.", reply_to_message_id="m2",
                          reply_to_is_own_message=True, message_id="1234567891")
    discord = SimpleNamespace(platform=run_inbound.Platform.DISCORD)
    import gateway.session as gateway_session
    gateway_session._discord_tools_loaded = lambda: True  # a gateway with the discord tools loaded
    return {
        "reply": run_inbound.GatewayInboundMixin._prepend_inbound_reply_context(event, None, "Is it done?"),
        "reply_own": run_inbound.GatewayInboundMixin._prepend_inbound_reply_context(own, None, "Thanks."),
        "discord": run_inbound.GatewayInboundMixin._prepend_inbound_reply_context(
            SimpleNamespace(reply_to_text=None, reply_to_message_id=None, message_id="1234567892"), discord,
            "Please archive it."),
        "discord_strip": run_inbound.strip_discord_triggering_note(
            SimpleNamespace(message_id="1234567892"), run_inbound.discord_triggering_note("1234567892") +
            "\n\nPlease archive it."),
        "replay_header": cc._INFLIGHT_TASK_REPLAY_HEADER,
        "restated": cc._restated_request_text(cc._INFLIGHT_TASK_REPLAY_HEADER + "\n  Please finish it.  "),
        "fresh_copy_keeps_uid": cc._fresh_compaction_message_copy(
            {"role": "user", "content": "x", MESSAGE_UID: "u1", cc._DB_PERSISTED_MARKER: True}),
        "branch_copy_keys": list(_BRANCH_COPY_KEYS),
        "gateway_branch_row": _branch_row({"role": "user", "content": "x", "timestamp": T, MESSAGE_UID: "u1",
                                           "tool_call_id": None}),
        "backfill_chunk": hermes_state_schema._MESSAGE_UID_BACKFILL_CHUNK,
    }


with open(OUT, "w") as f:
    json.dump(RESULTS, f, default=str)
