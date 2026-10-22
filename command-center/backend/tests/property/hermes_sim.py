"""A Hermes-faithful simulator of a JARVIS state.db, with ground truth that never appears in a row's text.

Every operation changes the backup's rows the way the vendored Hermes function does (test_hermes_conformance.py checks
each one against the real function where it runs standalone). Next to the database, the simulator keeps, for every
physical row, the logical message it is a version of (``origin``) and the logical messages whose words its content
carries (``carries``). Logical messages record what they were derived from (a /retry re-send from the original, a
restatement from the in-flight request, a reply quote from the quoted message). An independent message that happens to
say the same words is a new logical message with no parent: the text is equal, the ground truth is not.

Synthetic data only. Nothing here imports the Command Center.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

# --- Hermes constants (vendored text, checked by the conformance tests) -------------------------------------------

STUB = "[tool output pruned to save context]"
SUMMARY = "[CONTEXT COMPACTION] Summary of earlier turns: the work so far is recorded above."
MICRO_MARKER = "[CONTEXT COMPACTION] Micro summary of the tool exchange."
# agent/context_compressor.py _INFLIGHT_TASK_REPLAY_HEADER
REPLAY_HEADER = ("[STILL IN PROGRESS — this is the active request, restated after the compaction boundary because it "
                 "was not finished yet. Continue it; do not start over.]")


def reply_pointer(quote: str, text: str, own: bool = False) -> str:
    """gateway/run_inbound.py _prepend_inbound_reply_context."""
    who = " your previous message" if own else ""
    return '[Replying to%s: "%s"]\n\n%s' % (who, quote, text)


def discord_note(message_id: str) -> str:
    """gateway/run_inbound.py discord_triggering_note."""
    return ("[Triggering message id: `%s` — use as `message_id` for reply/react/pin via the discord tools.]"
            % message_id)


def derive_title(text: str) -> str:
    """Hermes title_generator.derive_title: the first 48 characters of the first user message, verbatim."""
    return text.strip()[:48]


SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT, model_config TEXT,
    system_prompt TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT,
    message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    title TEXT, display_name TEXT, archived INTEGER DEFAULT 0, estimated_cost_usd REAL, actual_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
    token_count INTEGER, finish_reason TEXT, reasoning TEXT, reasoning_details TEXT,
    active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, message_uid TEXT, tool_call_uids TEXT,
    absorbed_message_uids TEXT);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
"""

COPY_LINKS = ("parent", "branched_from", "compressed_from")  # lineage kinds that copy rows (plan 5.1)

# --- Synthetic words (no digits, addresses or names: nothing a restriction rule would act on) ---------------------

_SUBJECTS = ("the vendor review", "the quarterly plan", "the onboarding checklist", "the newsletter draft",
             "the hosting migration", "the pricing table", "the support backlog", "the content calendar",
             "the keyword sweep", "the backup schedule", "the landing page copy", "the partner agreement")
_VERBS = ("finish", "summarise", "double check", "rewrite", "send over", "prepare", "tidy up", "compare",
          "look again at", "archive")
_TAILS = ("before the weekly call", "for the team meeting", "and flag anything odd", "with the usual care",
          "so we can close it out", "and keep it short", "before lunch", "when you have a moment")
_ANSWERS = ("Done, {s} is ready and the open points are listed at the end.",
            "I went through {s} and nothing looks out of place.",
            "Here is the update on {s}: two items need a decision from you.",
            "Finished {s}; the notes are in the shared folder now.",
            "Quick summary of {s}: on track, no blockers today.")
_TOOL_OUT = ("Fetched page for {s}: section headings, a short table and the closing notes.",
             "Search results for {s}: the first entries match the request closely.",
             "Read file for {s}: plain text, several paragraphs, nothing unusual in it.")
_SHORT = ("ok", "Thanks, that is all.", "yes please", "Sounds good.")
_GATEWAYS = ("telegram", "whatsapp", "discord")


def _hex(rng: random.Random, n: int = 32) -> str:
    return "%0*x" % (n, rng.getrandbits(4 * n))


# --- Ground truth ---------------------------------------------------------------------------------------------------


@dataclass
class Logical:
    """One logical message: something a person or the model said once."""
    id: int
    role: str
    text: str
    parents: frozenset[int] = frozenset()  # derived from (a copy of their words made by JARVIS or quoted)
    kind: str = "typed"  # typed | tool | answer | summary | resend | restate | quote | repeat


@dataclass(frozen=True)
class Tag:
    origin: int  # the logical message this physical row is a version of
    carries: frozenset[int]  # the logical messages whose words are in this row's content


@dataclass
class Version:
    """A physical row as one backup snapshot had it (the Command Center may have stored it then)."""
    sid: str
    row: int
    role: str
    ts: float
    uid: str | None
    call: str | None
    content: str
    tag: Tag
    active: int
    compacted: int
    absorbed: tuple[str, ...]


@dataclass
class Snapshot:
    path: str
    n: int
    sha256: str
    rows: dict[tuple[str, int], Version]
    sessions: dict[str, dict[str, Any]]
    edges: set[tuple[str, str, str]]  # (child, parent, how) visible in this backup
    title_tags: dict[str, int | None]


class Sim:
    """One JARVIS installation's state.db, changed only by Hermes operations."""

    def __init__(self, root: str, seed: int, *, uids: str = "modern", donor: bool = True,
                 compressed_marker: bool = True, discord_note_persisted: bool = False,
                 backfill_chunk: int = 3, uid_trigger: bool = True) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.db = os.path.join(root, "state.db")
        self.rng = random.Random(seed)
        self.seed = seed
        self.uids = uids  # "modern" (every new row gets a uid) or "legacy" (Hermes older than message_uid)
        self.donor = donor  # _carry_parent_timestamps exists (newer Hermes) or not (older)
        self.compressed_marker = compressed_marker  # rotation writes model_config._compressed_from
        self.discord_note_persisted = discord_note_persisted  # older gateway persisted the triggering note
        self.backfill_chunk = backfill_chunk
        # hermes_state_common messages_message_uid_insert (newest Hermes): an INSERT that binds no uid gets one at
        # once; an older Hermes with the column but without the trigger leaves it NULL until the backfill
        self.uid_trigger = uid_trigger
        self.backfill_done_through = 0
        self.clock = 1_795_000_000.0
        self.lms: dict[int, Logical] = {}
        self.tags: dict[tuple[str, int], Tag] = {}
        self.title_tags: dict[str, int | None] = {}
        self.history: dict[tuple[str, int], list[Version]] = {}
        self.snapshots: list[Snapshot] = []
        self.log: list[str] = []
        self.nsess = 0
        self.ncall = 0
        self.checkpoints: list[tuple[str, dict, dict, int]] = []
        self._saved: dict[tuple[str, int], Tag] = {}
        # Where the harness wants pressure (the withheld conversations, once there is a decision): operations pick
        # these sessions three times out of four, and an independent repeat reuses these words half the time.
        self.focus: set[str] = set()
        self.hot: set[int] = set()
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.execute("PRAGMA journal_mode = DELETE")
        con.close()

    # --- plumbing -----------------------------------------------------------------------------------------------

    def con(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def write(self, fn: Any) -> Any:
        c = self.con()
        try:
            out = fn(c)
            c.commit()
            c.execute("PRAGMA journal_mode = DELETE")
            return out
        finally:
            c.close()

    def tick(self, d: float | None = None) -> float:
        self.clock += d if d is not None else self.rng.uniform(5.0, 90.0)
        return self.clock

    def lm(self, role: str, text: str, *, parents: Iterable[int] = (), kind: str = "typed") -> int:
        i = len(self.lms) + 1
        self.lms[i] = Logical(i, role, text, frozenset(parents), kind)
        return i

    def lineage(self, lm: int) -> set[int]:
        out, todo = set(), [lm]
        while todo:
            x = todo.pop()
            if x in out:
                continue
            out.add(x)
            todo.extend(self.lms[x].parents)
        return out

    def new_uid(self) -> str | None:
        return _hex(self.rng) if self.uids == "modern" else None

    def copy_uid(self, uid: str | None) -> str | None:
        """The uid a copy writes: Hermes keeps the dict's uid and mints one when the dict has none
        (stamp_message_uid); a Hermes older than message_uid writes none."""
        if self.uids != "modern":
            return None
        return uid or _hex(self.rng)

    def insert(self, c: sqlite3.Connection, sid: str, role: str, content: str, ts: float, uid: str | None, tag: Tag,
               *, call: str | None = None, tool_calls: str | None = None, active: int = 1, compacted: int = 0,
               absorbed: Iterable[str] = ()) -> int:
        absorbed = [a for a in absorbed if a]
        if uid is None and self.uids == "modern" and self.uid_trigger:
            uid = _hex(self.rng)
        rid = c.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, active, "
            "compacted, message_uid, absorbed_message_uids) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, role, content, call, tool_calls, "web_fetch" if role == "tool" else None, ts, active, compacted,
             uid, json.dumps(absorbed) if absorbed else None)).lastrowid
        self.tags[(sid, rid)] = tag
        return rid

    def copy_row(self, c: sqlite3.Connection, r: sqlite3.Row, sid: str, *, uid: Any = "keep", ts: float | None = None,
                 active: int = 1, compacted: int = 0) -> int:
        tag = self.tags[(r["session_id"], r["id"])]
        return self.insert(c, sid, r["role"], r["content"], r["timestamp"] if ts is None else ts,
                           self.copy_uid(r["message_uid"]) if uid == "keep" else uid, tag, call=r["tool_call_id"],
                           tool_calls=r["tool_calls"], active=active, compacted=compacted,
                           absorbed=json.loads(r["absorbed_message_uids"] or "[]"))

    def active(self, c: sqlite3.Connection, sid: str) -> list[sqlite3.Row]:
        return c.execute("SELECT * FROM messages WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id",
                         (sid,)).fetchall()

    def session(self, c: sqlite3.Connection, sid: str) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()

    def sessions(self) -> list[str]:
        c = self.con()
        try:
            return [r[0] for r in c.execute("SELECT id FROM sessions ORDER BY started_at, id")]
        finally:
            c.close()

    def open_sessions(self) -> list[str]:
        """Sessions a person can still write to (a compression parent is closed)."""
        c = self.con()
        try:
            return [r[0] for r in c.execute("SELECT id FROM sessions WHERE COALESCE(end_reason, '') NOT IN "
                                            "('compression') ORDER BY started_at, id")]
        finally:
            c.close()

    def gateway(self, sid: str) -> bool:
        c = self.con()
        try:
            s = self.session(c, sid)
            return s is not None and s["source"] in _GATEWAYS
        finally:
            c.close()

    def load_state(self, sessions: list[dict[str, Any]], rows: list[dict[str, Any]], seq: int) -> None:
        """Start from another state.db's rows (same ids, uids, times and contents; sqlite_sequence too). Each row is
        its own logical message (the conformance tests compare rows, not ground truth)."""
        def run(c: sqlite3.Connection) -> None:
            for s in sessions:
                c.execute("INSERT INTO sessions (id, source, model, model_config, system_prompt, parent_session_id, "
                          "started_at, end_reason, message_count, tool_call_count, title, archived) "
                          "VALUES (?,?,?,?,?,?,?,?,0,0,?,0)",
                          (s["id"], s.get("source") or "cli", "m", s.get("model_config") or "{}", "sys",
                           s.get("parent_session_id"), s.get("started_at") or 0.0, s.get("end_reason"),
                           s.get("title")))
                self.title_tags[s["id"]] = None
            for r in rows:
                lm = self.lm(r["role"], r.get("content") or "")
                c.execute("INSERT INTO messages (id, session_id, role, content, tool_call_id, tool_calls, tool_name, "
                          "timestamp, active, compacted, message_uid, absorbed_message_uids) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (r["id"], r["session_id"], r["role"], r.get("content"), r.get("tool_call_id"),
                           r.get("tool_calls"), r.get("tool_name"), r["timestamp"], r.get("active", 1),
                           r.get("compacted", 0), r.get("message_uid"), r.get("absorbed_message_uids")))
                self.tags[(r["session_id"], r["id"])] = Tag(lm, frozenset({lm}))
            c.execute("DELETE FROM sqlite_sequence WHERE name = 'messages'")
            c.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('messages', ?)", (seq,))
            self.clock = max([self.clock] + [r["timestamp"] for r in rows])
        self.write(run)

    # --- words ----------------------------------------------------------------------------------------------------

    def request_text(self) -> str:
        r = self.rng
        return "Please %s %s %s." % (r.choice(_VERBS), r.choice(_SUBJECTS), r.choice(_TAILS))

    def answer_text(self) -> str:
        return self.rng.choice(_ANSWERS).format(s=self.rng.choice(_SUBJECTS))

    def tool_text(self) -> str:
        return self.rng.choice(_TOOL_OUT).format(s=self.rng.choice(_SUBJECTS))

    def any_text(self, role: str) -> str:
        if self.rng.random() < 0.12:
            return self.rng.choice(_SHORT)
        return {"user": self.request_text, "assistant": self.answer_text, "tool": self.tool_text}[role]()

    # --- sessions -------------------------------------------------------------------------------------------------

    def new_session(self, *, source: str | None = None, parent: str | None = None, model_config: dict | None = None,
                    title: str | None = None, started: float | None = None, end_reason: str | None = None,
                    sid: str | None = None) -> str:
        self.nsess += 1
        source = source or self.rng.choice(("cli", "cli") + _GATEWAYS)
        sid = sid or "2026%04d_%06d_%s" % (1100 + self.nsess, self.nsess * 7, _hex(self.rng, 6))
        started = self.tick() if started is None else started
        self.write(lambda c: c.execute(
            "INSERT INTO sessions (id, source, model, model_config, system_prompt, parent_session_id, started_at, "
            "end_reason, message_count, tool_call_count, title, archived) VALUES (?,?,?,?,?,?,?,?,0,0,?,0)",
            (sid, source, "claude-opus-5", json.dumps(model_config or {}), "sys", parent, started, end_reason,
             title)))
        self.title_tags.setdefault(sid, None)
        if parent in self.focus:
            self.focus.add(sid)
        self.log.append("session %s %s parent=%s cfg=%s" % (sid, source, parent, sorted((model_config or {}))))
        return sid

    def set_title(self, c: sqlite3.Connection, sid: str, title: str | None, tag: int | None) -> None:
        c.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
        self.title_tags[sid] = tag

    # --- operations (each mirrors one Hermes function; conformance: test_hermes_conformance.py) -------------------

    def user_ts(self, sid: str, same_second_as: float | None = None) -> float:
        if same_second_as is not None:
            return float(int(same_second_as))
        t = self.tick()
        return float(int(t)) if self.gateway(sid) else t

    def append(self, sid: str, *, text: str | None = None, repeat_of: int | None = None, tool: bool | None = None,
               same_second_as: float | None = None, answer: bool = True, quote: tuple[int, bool] | None = None
               ) -> dict[str, int]:
        """One turn: SessionDB.append_message for the user row, the model's tool call and result, and its answer.
        repeat_of: an independent message with the very words of that logical message (not a copy).
        quote: (logical message, partial) for a gateway reply pointer to it."""
        out: dict[str, int] = {}
        gw = self.gateway(sid)
        c0 = self.con()
        src = self.session(c0, sid)["source"]
        c0.close()

        def run(c: sqlite3.Connection) -> None:
            ts = self.user_ts(sid, same_second_as)
            if repeat_of is not None:
                words = self.lms[repeat_of].text
                own = self.lm("user", words, kind="repeat")  # independent: no parents
            else:
                words = text if text is not None else self.any_text("user")
                own = self.lm("user", words)
            content, carries = words, {own}
            if quote is not None and gw:
                q_lm, partial = quote
                qt = self.lms[q_lm].text
                if partial and len(qt) > 24:
                    a = self.rng.randrange(0, len(qt) - 16)
                    qt = qt[a:a + self.rng.randint(16, len(qt) - a)]
                content = reply_pointer(qt, content)
                self.lms[own].parents = frozenset({q_lm})
                self.lms[own].kind = "quote"
            if gw and src == "discord" and self.discord_note_persisted:
                content = discord_note(_hex(self.rng, 18)) + "\n\n" + content
            out["user"] = self.insert(c, sid, "user", content, ts, self.new_uid(), Tag(own, frozenset(carries)))
            s = self.session(c, sid)
            if not s["title"]:
                if self.rng.random() < 0.6:
                    self.set_title(c, sid, derive_title(words), own)
                else:
                    self.set_title(c, sid, "Chat about %s" % self.rng.choice(_SUBJECTS), None)
            t = ts
            use_tool = (self.rng.random() < 0.45) if tool is None else tool
            if use_tool and answer:
                self.ncall += 1
                call = "call_%s" % _hex(self.rng, 12)
                t += self.rng.uniform(0.5, 3.0)
                a = self.lm("assistant", "", kind="answer")
                out["call"] = self.insert(c, sid, "assistant", "", t, self.new_uid(), Tag(a, frozenset()),
                                          tool_calls=json.dumps([{"id": call, "type": "function", "function": {
                                              "name": "web_fetch", "arguments": "{}"}}]))
                t += self.rng.uniform(0.5, 3.0)
                words_t = self.tool_text()
                tl = self.lm("tool", words_t, kind="tool")
                out["tool"] = self.insert(c, sid, "tool", words_t, t, self.new_uid(), Tag(tl, frozenset({tl})),
                                          call=call)
            if answer:
                t += self.rng.uniform(0.5, 3.0)
                words_a = self.any_text("assistant")
                al = self.lm("assistant", words_a, kind="answer")
                out["answer"] = self.insert(c, sid, "assistant", words_a, t, self.new_uid(), Tag(al, frozenset({al})))
            self.clock = max(self.clock, t)
        self.write(run)
        self.log.append("append %s %s" % (sid, out))
        return out

    def compact(self, sid: str, head_n: int, tail_n: int, *, restate: str | None = None) -> list[int]:
        """In-place compaction (SessionDB.archive_and_compact, watermark None, tail_count=tail_n): the tail originals
        get rewind flags (active 0, compacted 0), every other active row is archived (compacted 1), then the head
        copies, the summary and the tail copies are inserted as new live rows. Copies keep the dict's uid (minted
        when it had none) and its timestamp. restate="row": the in-flight request (the last user row, not in the
        tail) is restated after the summary under its own uid and a new time (context_compressor: replay keeps
        message_uid, drops timestamp); restate="carrier": merged onto the summary row, which absorbs its uid."""
        def run(c: sqlite3.Connection) -> list[int]:
            rows = self.active(c, sid)
            if len(rows) < 2:
                return []
            h = min(head_n, len(rows) - 1)
            tl = min(tail_n, len(rows) - h)
            head, tail = rows[:h], (rows[len(rows) - tl:] if tl else [])
            inflight = None
            if restate and not any(r["role"] == "user" for r in tail):
                # context_compressor: only when no real user turn follows the summary; the in-flight request is the
                # last user turn before the tail
                users = [r for r in rows[:len(rows) - tl] if r["role"] == "user"]
                inflight = users[-1] if users else None
            for r in tail:
                c.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r["id"],))
            c.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
            new = []
            for r in head:
                if inflight is not None and restate == "row" and r["id"] == inflight["id"]:
                    continue  # the replay replaces the in-flight row (same uid) instead of a second copy
                new.append(self.copy_row(c, r, sid))
            summ = self.lm("assistant", SUMMARY, kind="summary")
            content, carries, absorbed = SUMMARY, {summ}, []
            uid = self.new_uid()
            if inflight is not None and restate == "carrier":
                task = _restated(inflight["content"])
                content = SUMMARY + "\n\n" + REPLAY_HEADER + "\n" + task
                carries |= set(self.tags[(sid, inflight["id"])].carries)
                absorbed = [inflight["message_uid"]] if inflight["message_uid"] else []
            ts = self.tick(1.0)
            new.append(self.insert(c, sid, "assistant", content, ts, uid, Tag(summ, frozenset(carries)),
                                   absorbed=absorbed))
            for r in tail:
                new.append(self.copy_row(c, r, sid))
            if inflight is not None and restate == "row":
                # compressed.append(replay): after the surviving tail, its own uid, no timestamp (now)
                task = _restated(inflight["content"])
                itag = self.tags[(sid, inflight["id"])]
                new.append(self.insert(c, sid, "user", REPLAY_HEADER + "\n" + task, self.tick(1.0),
                                       self.copy_uid(inflight["message_uid"]), Tag(itag.origin, itag.carries)))
            return new
        new = self.write(run)
        self.log.append("compact %s head %d tail %d restate %s -> %s" % (sid, head_n, tail_n, restate, new))
        return new

    def rotate(self, sid: str, head_n: int, tail_n: int, *, donor: bool | None = None,
               marker: bool | None = None, child: str | None = None) -> str | None:
        """Rotating compaction (publish_compression_child): the parent ends 'compression', a continuation child
        (parent_session_id, model_config._compressed_from when this Hermes writes it) gets the head copies, a summary
        and the tail copies. The handoff dicts keep their uid (minted when none) and arrive without a timestamp:
        _carry_parent_timestamps gives each the time of the FIRST unconsumed active parent row with the same content
        (role, content, tool_call_id, tool_calls, tool_name); an older Hermes stamps them 'now'. The title moves to the
        child (_carry_session_state_to_child)."""
        donor = self.donor if donor is None else donor
        marker = self.compressed_marker if marker is None else marker
        c = self.con()
        try:
            rows = self.active(c, sid)
            parent = self.session(c, sid)
        finally:
            c.close()
        if len(rows) < 2 or parent is None:
            return None
        child = self.new_session(source=parent["source"], parent=sid,
                                 model_config={"_compressed_from": sid} if marker else None, title=None, sid=child)

        def run(c: sqlite3.Connection) -> list[int]:
            h = min(head_n, len(rows) - 1)
            tl = min(tail_n, len(rows) - h)
            carried = list(rows[:h]) + (list(rows[len(rows) - tl:]) if tl else [])
            donors: dict[tuple, list[float]] = {}
            for r in rows:
                donors.setdefault((r["role"], r["content"], r["tool_call_id"], r["tool_calls"], "web_fetch"
                                   if r["role"] == "tool" else None), []).append(r["timestamp"])
            now = self.tick(1.0)
            new = []

            def ts_of(r: sqlite3.Row) -> float:
                nonlocal now
                if donor:
                    q = donors.get((r["role"], r["content"], r["tool_call_id"], r["tool_calls"],
                                    "web_fetch" if r["role"] == "tool" else None))
                    if q:
                        return q.pop(0)
                now += 1e-3
                return now
            for r in carried[:h]:
                new.append(self.copy_row(c, r, child, ts=ts_of(r)))
            summ = self.lm("assistant", SUMMARY, kind="summary")
            now += 1e-3
            new.append(self.insert(c, child, "assistant", SUMMARY, now, self.new_uid(), Tag(summ, frozenset({summ}))))
            for r in carried[h:]:
                new.append(self.copy_row(c, r, child, ts=ts_of(r)))
            c.execute("UPDATE sessions SET end_reason = 'compression', ended_at = ? WHERE id = ?", (now, sid))
            p = self.session(c, sid)
            tt = self.title_tags.get(sid)
            self.set_title(c, child, p["title"], tt)
            self.set_title(c, sid, None, None)
            self.clock = max(self.clock, now)
            return new
        new = self.write(run)
        self.log.append("rotate %s -> %s head %d tail %d donor %s marker %s rows %s" % (
            sid, child, head_n, tail_n, donor, marker, new))
        return child

    def prune(self, sid: str, row: int) -> int | None:
        """Proactive prune (rewrite_pruned_rows): an archived twin INSERT ... SELECT of every column (a NULL uid
        stays NULL; active 0, compacted 1), then the live row's content becomes the stub in place. Only an
        assistant or tool row is rewritten."""
        def run(c: sqlite3.Connection) -> int | None:
            r = c.execute("SELECT * FROM messages WHERE id = ? AND session_id = ? AND active = 1", (row, sid)
                          ).fetchone()
            if r is None or r["role"] not in ("assistant", "tool") or r["content"] in ("", STUB):
                return None
            tag = self.tags[(sid, row)]
            twin = self.insert(c, sid, r["role"], r["content"], r["timestamp"], r["message_uid"], tag,
                               call=r["tool_call_id"], tool_calls=r["tool_calls"], active=0, compacted=1,
                               absorbed=json.loads(r["absorbed_message_uids"] or "[]"))
            c.execute("UPDATE messages SET content = ? WHERE id = ?", (STUB, row))
            self.tags[(sid, row)] = Tag(tag.origin, frozenset())
            return twin
        twin = self.write(run)
        self.log.append("prune %s %s -> twin %s" % (sid, row, twin))
        return twin

    def fold(self, sid: str, pair: tuple[int, int] | None = None) -> int | None:
        """Micro-compaction that supersedes a marker and merges two now-adjacent user turns
        (_merge_adjacent_user_turns: prev + '\\n\\n' + msg; record_absorbed_message: prev keeps its uid and records
        msg's when it has one), committed by archive_and_compact with carried_messages: the carried originals get
        rewind flags, everything else is archived, and the whole new list is inserted (copies keep uid and time)."""
        def run(c: sqlite3.Connection) -> int | None:
            rows = list(self.active(c, sid))
            users = [i for i, r in enumerate(rows) if r["role"] == "user"]
            pairs = [(a, b) for a, b in zip(users, users[1:])]
            if pair is not None:
                pairs = [(i, j) for i, j in pairs if (rows[i]["id"], rows[j]["id"]) == tuple(pair)]
            if not pairs:
                return None
            ia, ib = self.rng.choice(pairs)
            a, b = rows[ia], rows[ib]
            between = rows[ia + 1:ib]
            carried = rows[:ia] + rows[ib + 1:]
            for r in carried:
                c.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r["id"],))
            c.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
            for r in rows[:ia]:
                self.copy_row(c, r, sid)
            if between:
                m = self.lm("assistant", MICRO_MARKER, kind="summary")
                self.insert(c, sid, "assistant", MICRO_MARKER, self.tick(0.5), self.new_uid(),
                                        Tag(m, frozenset({m})))
            ta, tb = self.tags[(sid, a["id"])], self.tags[(sid, b["id"])]
            composite = "\n\n".join(x for x in (a["content"], b["content"]) if x)
            uid = self.copy_uid(a["message_uid"])
            absorbed = [b["message_uid"]] if b["message_uid"] and b["message_uid"] != uid else []
            absorbed = json.loads(a["absorbed_message_uids"] or "[]") + absorbed + json.loads(
                b["absorbed_message_uids"] or "[]")
            new = self.insert(c, sid, "user", composite, a["timestamp"], uid,
                              Tag(ta.origin, frozenset(ta.carries | tb.carries)), absorbed=absorbed)
            for r in rows[ib + 1:]:
                self.copy_row(c, r, sid)
            return new
        new = self.write(run)
        self.log.append("fold %s -> %s" % (sid, new))
        return new

    def cli_branch(self, sid: str, child: str | None = None) -> str | None:
        """CLI /branch: the child (parent_session_id and model_config._branched_from) copies the live history with
        message_identity (uid kept, minted when the row has none; the parent's row stays NULL until the backfill)
        and the timestamps; the parent ends 'branched'; title '<title> #2'."""
        return self._branch(sid, "cli", child)

    def gateway_branch(self, sid: str, child: str | None = None) -> str | None:
        """Gateway /branch (_branch_row): content, tool calls and timestamp copied, no uid (a fresh one is minted
        at insert); model_config._branched_from; the parent stays open; title '<title> #2'."""
        return self._branch(sid, "gateway", child)

    def _branch(self, sid: str, how: str, child_id: str | None = None) -> str | None:
        c = self.con()
        try:
            rows = self.active(c, sid)
            p = self.session(c, sid)
        finally:
            c.close()
        if not rows or p is None:
            return None
        title = ("%s #2" % p["title"]) if p["title"] else None
        child = self.new_session(source=p["source"] if how == "gateway" else "cli", parent=sid,
                                 model_config={"_branched_from": sid}, title=None, sid=child_id)

        def run(c: sqlite3.Connection) -> None:
            if how == "cli":
                c.execute("UPDATE sessions SET end_reason = 'branched' WHERE id = ?", (sid,))
            for r in rows:
                if how == "cli":
                    self.copy_row(c, r, child)
                else:
                    self.copy_row(c, r, child, uid=self.new_uid())
            self.set_title(c, child, title, self.title_tags.get(sid))
        self.write(run)
        self.log.append("%s_branch %s -> %s" % (how, sid, child))
        return child

    def cli_retry(self, sid: str) -> dict[str, int] | None:
        """CLI /retry: rewind_user_turn(-1) soft-deletes the last user turn and what follows (active 0); the text is
        re-sent as a new message (new uid, new time) and answered."""
        c = self.con()
        try:
            rows = self.active(c, sid)
        finally:
            c.close()
        users = [r for r in rows if r["role"] == "user"]
        if not users:
            return None
        last = users[-1]

        def run(c: sqlite3.Connection) -> None:
            c.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND id >= ? AND active = 1",
                      (sid, last["id"]))
        self.write(run)
        return self._resend(sid, last, "cli_retry")

    def gateway_retry(self, sid: str) -> dict[str, int] | None:
        """Gateway /retry without a session authority: rewrite_transcript(truncated, active_only=True), i.e.
        replace_messages DELETEs the live rows and re-inserts the history before the last user turn (same uids and
        times, new ids); the last user text is then sent again as a new turn."""
        c = self.con()
        try:
            rows = self.active(c, sid)
        finally:
            c.close()
        users = [i for i, r in enumerate(rows) if r["role"] == "user"]
        if not users:
            return None
        keep, last = rows[:users[-1]], rows[users[-1]]

        def run(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM messages WHERE session_id = ? AND active = 1", (sid,))
            for r in rows:
                self.tags.pop((sid, r["id"]), None)
            for r in keep:
                self._reinsert(c, r)
        saved = {(sid, r["id"]): self.tags[(sid, r["id"])] for r in rows}
        self._saved = saved
        self.write(run)
        return self._resend(sid, last, "gateway_retry")

    def _reinsert(self, c: sqlite3.Connection, r: sqlite3.Row) -> int:
        tag = self._saved[(r["session_id"], r["id"])]
        return self.insert(c, r["session_id"], r["role"], r["content"], r["timestamp"], self.copy_uid(r["message_uid"]),
                           tag, call=r["tool_call_id"], tool_calls=r["tool_calls"],
                           absorbed=json.loads(r["absorbed_message_uids"] or "[]"))

    def _resend(self, sid: str, last: sqlite3.Row, what: str) -> dict[str, int]:
        ltag = self.tags.get((sid, last["id"])) or self._saved[(sid, last["id"])]
        text = last["content"]
        # The re-sent turn is new: a new logical message derived from the original (its words are a copy).
        own = self.lm("user", text, parents=ltag.carries | {ltag.origin}, kind="resend")
        out = {}

        def run(c: sqlite3.Connection) -> None:
            ts = self.user_ts(sid)
            out["user"] = self.insert(c, sid, "user", text, ts, self.new_uid(),
                                      Tag(own, frozenset({own}) | ltag.carries))
            words = self.answer_text()
            al = self.lm("assistant", words, kind="answer")
            out["answer"] = self.insert(c, sid, "assistant", words, ts + 1.5, self.new_uid(), Tag(al, frozenset({al})))
            self.clock = max(self.clock, ts + 1.5)
        self.write(run)
        self.log.append("%s %s (row %s) -> %s" % (what, sid, last["id"], out))
        return out

    def undo(self, sid: str) -> list[int]:
        """CLI /undo: rewind_user_turn soft-deletes from the last user turn on (active 0)."""
        def run(c: sqlite3.Connection) -> list[int]:
            rows = self.active(c, sid)
            users = [r for r in rows if r["role"] == "user"]
            if not users:
                return []
            ids = [r["id"] for r in rows if r["id"] >= users[-1]["id"]]
            c.execute("UPDATE messages SET active = 0 WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
            return ids
        ids = self.write(run)
        self.log.append("undo %s %s" % (sid, ids))
        return ids

    def replace(self, sid: str, *, archive_dropped: bool = False, drop_last_turn: bool = True) -> None:
        """SessionDB.replace_messages with the live history (minus the last turn when drop_last_turn). Destructive
        (default): every row of the session is DELETEd, archived ones included, and the kept dicts are inserted again
        with their uids and times (new ids). archive_dropped: the matching live prefix keeps its rows; the divergent
        live suffix gets active 0 (compacted untouched)."""
        c = self.con()
        try:
            rows = self.active(c, sid)
        finally:
            c.close()
        users = [i for i, r in enumerate(rows) if r["role"] == "user"]
        keep = rows[:users[-1]] if (drop_last_turn and users) else rows

        def run(c: sqlite3.Connection) -> None:
            if archive_dropped:
                kept_ids = {r["id"] for r in keep}
                for r in rows:
                    if r["id"] not in kept_ids:
                        c.execute("UPDATE messages SET active = 0 WHERE id = ?", (r["id"],))
                return
            self._saved = {k: v for k, v in self.tags.items() if k[0] == sid}
            allrows = c.execute("SELECT id FROM messages WHERE session_id = ?", (sid,)).fetchall()
            c.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            for r in allrows:
                self.tags.pop((sid, r[0]), None)
            for r in keep:
                self._reinsert(c, r)
        self.write(run)
        self.log.append("replace %s archive_dropped=%s drop_last=%s kept %d" % (sid, archive_dropped,
                                                                                 drop_last_turn, len(keep)))

    def delete_session(self, sid: str) -> None:
        """hermes_state_sessions.delete_session: the session and its messages are deleted; branch and compression
        children are orphaned (parent_session_id NULL; their model_config markers stay)."""
        def run(c: sqlite3.Connection) -> None:
            c.execute("UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?", (sid,))
            for r in c.execute("SELECT id FROM messages WHERE session_id = ?", (sid,)).fetchall():
                self.tags.pop((sid, r[0]), None)
            c.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        self.write(run)
        self.log.append("delete %s" % sid)

    def reset(self, sid: str, child: str | None = None) -> str:
        """Gateway reset (session_recovery): a new session with parent_session_id and model_config._reset_from; no
        row is copied."""
        c = self.con()
        try:
            src = self.session(c, sid)["source"]
        finally:
            c.close()
        child = self.new_session(source=src, parent=sid, model_config={"_reset_from": sid}, sid=child)
        self.log.append("reset %s -> %s" % (sid, child))
        return child

    def upgrade(self) -> None:
        """This JARVIS updates to a Hermes with message_uid: new rows get uids, old ones wait for the backfill."""
        self.uids = "modern"
        self.log.append("upgrade to uids")

    def backfill(self, chunk: int | None = None) -> int:
        """hermes_state_schema._advance_message_uid_backfill with a tiny budget: one id-range slice per open
        (id > done_through AND id <= done_through + chunk AND message_uid IS NULL)."""
        if self.uids != "modern":
            return 0
        chunk = chunk or self.backfill_chunk

        def run(c: sqlite3.Connection) -> int:
            high = c.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
            if self.backfill_done_through >= high:
                return 0
            upper = self.backfill_done_through + chunk
            ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE id > ? AND id <= ? AND message_uid IS NULL",
                                           (self.backfill_done_through, upper))]
            for i in ids:
                c.execute("UPDATE messages SET message_uid = ? WHERE id = ?", (_hex(self.rng), i))
            self.backfill_done_through = upper
            return len(ids)
        n = self.write(run)
        self.log.append("backfill slice -> %d rows (through %d)" % (n, self.backfill_done_through))
        return n

    def checkpoint(self) -> int:
        """Hermes's quick snapshot of state.db (hermes update / backup): restored later with restore_checkpoint."""
        path = os.path.join(self.root, "checkpoint-%d.db" % len(self.checkpoints))
        shutil.copyfile(self.db, path)
        self.checkpoints.append((path, dict(self.tags), dict(self.title_tags), self.backfill_done_through))
        self.log.append("checkpoint %d" % (len(self.checkpoints) - 1))
        return len(self.checkpoints) - 1

    def restore_checkpoint(self, n: int) -> None:
        """hermes backup import: state.db comes back whole, sqlite_sequence included, so row ids are reused."""
        path, tags, titles, through = self.checkpoints[n]
        shutil.copyfile(path, self.db)
        self.tags, self.title_tags, self.backfill_done_through = dict(tags), dict(titles), through
        self.log.append("restore checkpoint %d" % n)

    # --- backups and ground truth -----------------------------------------------------------------------------

    def backup(self, label: str) -> Snapshot:
        """The JARVIS backup the Command Center imports: a copy of state.db and the ground truth of that moment."""
        path = os.path.join(self.root, "backup-%02d-%s.db" % (len(self.snapshots), label))
        shutil.copyfile(self.db, path)
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        c = self.con()
        try:
            rows: dict[tuple[str, int], Version] = {}
            for r in c.execute("SELECT * FROM messages ORDER BY id"):
                key = (r["session_id"], r["id"])
                v = Version(r["session_id"], r["id"], r["role"], r["timestamp"], r["message_uid"], r["tool_call_id"],
                            r["content"] or "", self.tags[key], r["active"], r["compacted"],
                            tuple(json.loads(r["absorbed_message_uids"] or "[]")))
                rows[key] = v
                hist = self.history.setdefault(key, [])
                if not hist or (hist[-1].content, hist[-1].uid, hist[-1].ts, hist[-1].tag) != (v.content, v.uid,
                                                                                             v.ts, v.tag):
                    hist.append(v)
            sessions = {r["id"]: dict(r) for r in c.execute("SELECT * FROM sessions")}
        finally:
            c.close()
        edges = set()
        for sid, s in sessions.items():
            cfg = json.loads(s["model_config"] or "{}")
            if s["parent_session_id"]:
                edges.add((sid, s["parent_session_id"], "reset" if "_reset_from" in cfg else "parent"))
            for k, how in (("_branched_from", "branched_from"), ("_compressed_from", "compressed_from"),
                           ("_reset_from", "reset"), ("_delegate_from", "delegate")):
                if cfg.get(k):
                    edges.add((sid, cfg[k], how))
        snap = Snapshot(path, len(self.snapshots), sha, rows, sessions, edges, dict(self.title_tags))
        self.snapshots.append(snap)
        self.log.append("backup %d %s sha %s rows %d" % (snap.n, label, sha[:12], len(rows)))
        return snap

    # --- ground-truth questions ---------------------------------------------------------------------------------

    def derives(self, tag: Tag, withheld: set[int]) -> bool:
        """The row is a version or copy of a withheld message, or carries its words (A2: certainty must hold this)."""
        reach = self.lineage(tag.origin)
        for c in tag.carries:
            reach |= self.lineage(c)
        return bool(reach & withheld)

    def leaks(self, tag: Tag, withheld: set[int]) -> bool:
        """The row's content carries words of a withheld message (A1/B2: it must not be readable)."""
        reach: set[int] = set()
        for c in tag.carries:
            reach |= self.lineage(c)
        return bool(reach & withheld)

    def versions_of(self, sid: str, row: int) -> list[Version]:
        return list(self.history.get((sid, row), []))

    def random_op(self, weights: dict[str, float] | None = None) -> str:
        """One random Hermes operation (the property harness's step)."""
        w = dict(DEFAULT_WEIGHTS)
        if weights:
            w.update(weights)
        names = list(w)
        while True:
            name = self.rng.choices(names, [w[n] for n in names])[0]
            done = getattr(self, "_op_" + name)()
            if done is not False:
                return name

    def _pick(self, open_only: bool = True) -> str | None:
        ss = self.open_sessions() if open_only else self.sessions()
        hot = [s for s in ss if s in self.focus]
        if hot and self.rng.random() < 0.75:
            return self.rng.choice(hot)
        return self.rng.choice(ss) if ss else None

    def _op_append(self) -> bool:
        sid = self._pick()
        if sid is None:
            return False
        self.append(sid)
        return True

    def _op_new_session(self) -> bool:
        sid = self.new_session()
        self.append(sid)
        return True

    def _op_compact(self) -> bool:
        sid = self._pick()
        if sid is None:
            return False
        r = self.rng.random()
        restate = "row" if r < 0.15 else ("carrier" if r < 0.25 else None)
        return bool(self.compact(sid, self.rng.randint(0, 3), self.rng.randint(0, 4), restate=restate))

    def _op_rotate(self) -> bool:
        sid = self._pick()
        if sid is None:
            return False
        return self.rotate(sid, self.rng.randint(0, 2), self.rng.randint(1, 4),
                           donor=self.donor if self.rng.random() < 0.85 else (not self.donor)) is not None

    def _op_prune(self) -> bool:
        sid = self._pick(open_only=False)
        if sid is None:
            return False
        c = self.con()
        try:
            cands = [r["id"] for r in self.active(c, sid) if r["role"] in ("assistant", "tool")
                     and r["content"] not in ("", STUB) and not r["content"].startswith("[CONTEXT COMPACTION]")]
        finally:
            c.close()
        return bool(cands) and self.prune(sid, self.rng.choice(cands)) is not None

    def _op_fold(self) -> bool:
        sid = self._pick()
        return sid is not None and self.fold(sid) is not None

    def _op_cli_branch(self) -> bool:
        sid = self._pick()
        return sid is not None and self.cli_branch(sid) is not None

    def _op_gateway_branch(self) -> bool:
        sid = self._pick()
        return sid is not None and self.gateway_branch(sid) is not None

    def _op_cli_retry(self) -> bool:
        sid = self._pick()
        return sid is not None and self.cli_retry(sid) is not None

    def _op_gateway_retry(self) -> bool:
        sid = self._pick()
        return sid is not None and self.gateway_retry(sid) is not None

    def _op_undo(self) -> bool:
        sid = self._pick()
        return sid is not None and bool(self.undo(sid))

    def _op_replace(self) -> bool:
        sid = self._pick()
        if sid is None:
            return False
        self.replace(sid, archive_dropped=self.rng.random() < 0.4, drop_last_turn=self.rng.random() < 0.6)
        return True

    def _op_delete(self) -> bool:
        ss = self.sessions()
        if len(ss) < 3:
            return False
        self.delete_session(self.rng.choice(ss))
        return True

    def _op_backfill(self) -> bool:
        return self.backfill() >= 0 and self.uids == "modern"

    def _op_upgrade(self) -> bool:
        if self.uids == "modern":
            return False
        self.upgrade()
        return True

    def _op_reset(self) -> bool:
        sid = self._pick()
        if sid is None:
            return False
        child = self.reset(sid)
        self.append(child)
        return True

    def _op_repeat(self) -> bool:
        """An independent message with the same words as an earlier one (maybe in the same whole second)."""
        sid = self._pick()
        cands = [i for i, m in self.lms.items() if m.role == "user" and m.kind in ("typed", "repeat") and m.text]
        if sid is None or not cands:
            return False
        hot = [i for i in self.hot if i in self.lms and self.lms[i].text]
        src = self.rng.choice(hot) if hot and self.rng.random() < 0.5 else self.rng.choice(cands)
        same = None
        if self.gateway(sid) and self.rng.random() < 0.5:
            c = self.con()
            try:
                hit = c.execute("SELECT timestamp FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT 1"
                                ).fetchone()
            finally:
                c.close()
            same = hit[0] if hit else None
        self.append(sid, repeat_of=src, same_second_as=same)
        return True

    def _op_reply_quote(self) -> bool:
        sid = self._pick()
        if sid is None or not self.gateway(sid):
            return False
        c = self.con()
        try:
            rows = [r for r in self.active(c, sid) if r["role"] in ("user", "assistant") and len(r["content"] or "")
                    >= 24 and not (r["content"] or "").startswith("[")]
        finally:
            c.close()
        if not rows:
            return False
        r = self.rng.choice(rows)
        tag = self.tags[(sid, r["id"])]
        if len(tag.carries) != 1:
            return False
        (q,) = tag.carries
        self.append(sid, quote=(q, self.rng.random() < 0.5))
        return True

    def _op_checkpoint(self) -> bool:
        self.checkpoint()
        return True

    def _op_restore(self) -> bool:
        if not self.checkpoints:
            return False
        self.restore_checkpoint(self.rng.randrange(len(self.checkpoints)))
        return True


def _restated(content: str) -> str:
    """context_compressor._restated_request_text for plain text: the words after the last replay header."""
    text = (content or "").strip()
    if REPLAY_HEADER in text:
        text = text.rsplit(REPLAY_HEADER, 1)[1].strip()
    return text


DEFAULT_WEIGHTS: dict[str, float] = {
    "append": 6, "new_session": 1.2, "compact": 2.5, "rotate": 1.5, "prune": 1.5, "fold": 0.8, "cli_branch": 1,
    "gateway_branch": 1.2, "cli_retry": 0.8, "gateway_retry": 0.8, "undo": 0.5, "replace": 0.7, "delete": 0.5,
    "backfill": 0.8, "upgrade": 0.4, "reset": 0.4, "repeat": 1.2, "reply_quote": 0.8, "checkpoint": 0.3,
    "restore": 0.2,
}


def new_world(root: str, seed: int) -> Sim:
    """The property harness's starting state for a seed: the Hermes generation, uid mode and timestamp behaviour are
    drawn from the seed, then two or three conversations with a few turns each."""
    rng = random.Random(seed * 7919 + 17)
    sim = Sim(root, seed, uids="legacy" if rng.random() < 0.3 else "modern", donor=rng.random() < 0.8,
              compressed_marker=rng.random() < 0.8, discord_note_persisted=rng.random() < 0.3,
              backfill_chunk=rng.randint(2, 6), uid_trigger=rng.random() < 0.5)
    for _ in range(rng.randint(2, 3)):
        sid = sim.new_session()
        for _ in range(rng.randint(2, 4)):
            sim.append(sid)
    return sim
