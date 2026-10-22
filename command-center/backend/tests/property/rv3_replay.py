"""Round-3 bug-finding seeds, replayed exactly (plan 5.1: "plus every seed that found a bug in rounds 2-3").

The round-3 driver (scratchpad rv3/resurrection rv3res_fuzz.py) drew its Hermes operations and its admin choices from
one random.Random(seed). Its Hermes side is ported here verbatim (same operations, same order of random draws, same
texts), on the simulator's database so every row keeps its ground truth. Its admin side picked a stored row or a hold
from the Command Center (rng.choice over a list whose length the Command Center decided), so a seed alone cannot
replay it on another implementation. rv3_replays.json records, for each bug-finding seed, how many 32-bit words each of
those admin choices consumed (reconstructed offline from the run's own log, every logged Hermes operation, row id and
withheld row matched) and the env switches of that run. With them the exact Hermes history replays, and the admin's
withholds land on the very JARVIS rows the round-3 run withheld.

The texts carry the round-3 markers (mk0001q...), as the original runs did; the oracle never reads them: ground truth
comes from the simulator's tags."""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import hermes_sim as hs

MARK = re.compile(r"mk\d{4}q")
OPS = ["turn", "compact", "gbranch", "cbranch", "rotate", "prune", "fold", "renumber", "backfill", "cli_retry",
       "undo", "delete", "gateway_retry"]
WEIGHTS = [3, 3, 2, 1, 2, 2, 1, 1, 1, 1, 1, 1, 1]
REPLAYS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rv3_replays.json")


class Diverged(Exception):
    pass


@dataclass
class Event:
    kind: str  # import | withhold | release | rollback | hermes
    line: str = ""
    sid: str = ""
    row: int = 0


@dataclass
class Old:
    """rv3res_fuzz.World, ported: its operations change the simulator's database and keep ground truth."""
    sim: hs.Sim
    rng: random.Random
    uidless: bool
    env: dict[str, bool]
    sessions: list[str] = field(default_factory=list)
    n: int = 0
    nsess: int = 0
    clock: float = 1_790_000_000.0

    def tick(self, d: float = 10.0) -> float:
        self.clock += d
        return self.clock

    def marker(self) -> str:
        self.n += 1
        return "mk%04dq" % self.n

    def uid(self) -> str | None:
        return None if self.uidless else hs._hex(self.sim.rng)  # bj.new_uid: never the scenario's rng

    def ins(self, c: Any, sid: str, role: str, content: str, ts: float, uid: str | None, tag: hs.Tag, *,
            call: str | None = None, active: int = 1, compacted: int = 0, absorbed: Any = None,
            tool_calls: str | None = None) -> int:
        ab = json.loads(absorbed) if isinstance(absorbed, str) else (absorbed or [])
        rid = c.execute("INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, "
                        "timestamp, active, compacted, absorbed_message_uids, tool_calls) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, role, content, uid, call, "web_fetch" if role == "tool" else None, ts, active, compacted,
                         json.dumps(ab) if ab else (absorbed if isinstance(absorbed, str) else None),
                         tool_calls)).lastrowid
        self.sim.tags[(sid, rid)] = tag
        return rid

    def new(self, role: str, content: str) -> hs.Tag:
        lm = self.sim.lm(role, content)
        return hs.Tag(lm, frozenset({lm}) if content else frozenset())

    def add_session(self, sid: str, *, parent: str | None = None, model_config: dict | None = None,
                    title: str = "Probe conversation") -> None:
        started = self.tick()
        self.sim.write(lambda c: c.execute(
            "INSERT INTO sessions (id, source, model, model_config, system_prompt, parent_session_id, started_at, "
            "ended_at, end_reason, message_count, tool_call_count, title, archived) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (sid, "cli", "claude-opus-5", json.dumps(model_config or {}), "sys", parent, started, None, "completed",
             0, 0, title)))
        self.sim.title_tags[sid] = None

    def active(self, c: Any, sid: str) -> list:
        return c.execute("SELECT * FROM messages WHERE session_id = ? AND active = 1 AND role != 'system' "
                         "ORDER BY id", (sid,)).fetchall()

    # --- the Hermes operations of rv3res_fuzz, in its order of random draws ------------------------------------

    def op_turn(self, sid: str | None = None) -> str:
        sid = sid or self.rng.choice(self.sessions)

        def run(c: Any) -> None:
            t = self.tick()
            text = "Please handle %s for the quarterly vendor review today" % self.marker()
            self.ins(c, sid, "user", text, t, self.uid(), self.new("user", text))
            if self.rng.random() < 0.5:
                call = "call_%d" % self.n
                calls = json.dumps([{"id": call, "type": "function", "function": {"name": "web_fetch",
                                                                                  "arguments": "{}"}}])
                self.ins(c, sid, "assistant", "", t + 1, self.uid(), self.new("assistant", ""), tool_calls=calls)
                tool = "Fetched page %s with the full vendor pricing table and notes" % self.marker()
                self.ins(c, sid, "tool", tool, t + 2, self.uid(), self.new("tool", tool), call=call)
            ans = "Done: summary %s of the vendor terms is ready for you" % self.marker()
            self.ins(c, sid, "assistant", ans, t + 3, self.uid(), self.new("assistant", ans))
        self.sim.write(run)
        return "turn %s" % sid

    def _copy(self, c: Any, r: Any, sid: str, *, uid: Any = "keep", absorbed: Any = "keep", active: int = 1,
              compacted: int = 0) -> int:
        tag = self.sim.tags[(r["session_id"], r["id"])]
        return self.ins(c, sid, r["role"], r["content"], r["timestamp"], r["message_uid"] if uid == "keep" else uid,
                        tag, call=r["tool_call_id"], active=active, compacted=compacted,
                        absorbed=r["absorbed_message_uids"] if absorbed == "keep" else absorbed,
                        tool_calls=r["tool_calls"])

    def op_compact(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> str:
            rows = self.active(c, sid)
            if len(rows) < 3:
                return "skip"
            head_n = self.rng.randint(0, min(2, len(rows) - 1))
            tail_n = self.rng.randint(0, min(4, len(rows) - head_n))
            head, tail = rows[:head_n], (rows[len(rows) - tail_n:] if tail_n else [])
            for r in tail:
                c.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r["id"],))
            c.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
            for r in head:
                self._copy(c, r, sid)
            summary = "[CONTEXT COMPACTION] Summary of earlier turns."
            self.ins(c, sid, "assistant", summary, self.tick(), self.uid(), self.new("assistant", summary))
            for r in tail:
                self._copy(c, r, sid)
            return "head %d tail %d" % (head_n, tail_n)
        return "compact %s %s" % (sid, self.sim.write(run))

    def _child(self, kind: str) -> str:
        parent = self.rng.choice(self.sessions)
        self.nsess += 1
        child = "%s_%s%d" % (parent.split("_")[0], kind, self.nsess)
        if kind == "gbr":
            self.add_session(child, parent=parent, model_config={"_branched_from": parent}, title="Probe branch")
        elif kind == "cbr":
            self.sim.write(lambda c: c.execute("UPDATE sessions SET end_reason = 'branched' WHERE id = ?", (parent,)))
            self.add_session(child, parent=parent, title="Probe cli branch")
        else:
            self.sim.write(lambda c: c.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = ?",
                                               (parent,)))
            self.add_session(child, parent=parent, title="Probe",
                             model_config={"_compressed_from": parent} if self.env.get("compressed_from") else None)

        def run(c: Any) -> None:
            for r in self.active(c, parent):
                uid = ((None if self.uidless and r["message_uid"] is None else hs._hex(self.sim.rng))
                       if kind == "gbr" else r["message_uid"])
                self._copy(c, r, child, uid=uid, absorbed=None if kind == "gbr" else "keep")
        self.sim.write(run)
        self.sessions.append(child)
        return "%s %s -> %s" % (kind, parent, child)

    def op_gbranch(self) -> str:
        return self._child("gbr")

    def op_cbranch(self) -> str:
        return self._child("cbr")

    def op_rotate(self) -> str:
        return self._child("rot")

    def op_prune(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> str:
            cands = [r for r in self.active(c, sid) if r["role"] in ("tool", "assistant")
                     and MARK.search(r["content"] or "") and (r["message_uid"] or self.env.get("uidless_prune"))]
            if not cands:
                return "skip"
            r = self.rng.choice(cands)
            self._copy(c, r, sid, active=0, compacted=1, absorbed=None)
            c.execute("UPDATE messages SET content = ? WHERE id = ?", (hs.STUB, r["id"]))
            tag = self.sim.tags[(sid, r["id"])]
            self.sim.tags[(sid, r["id"])] = hs.Tag(tag.origin, frozenset())
            return str(r["id"])
        return "prune %s %s" % (sid, self.sim.write(run))

    def op_fold(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> str:
            rows = [r for r in self.active(c, sid) if r["role"] == "user"]
            if len(rows) < 2:
                return "skip"
            i = self.rng.randrange(len(rows) - 1)
            first, second = rows[i], rows[i + 1]
            text = (first["content"] or "") + "\n" + (second["content"] or "")
            c.execute("UPDATE messages SET content = ?, absorbed_message_uids = ? WHERE id = ?",
                      (text, json.dumps([second["message_uid"]] if second["message_uid"] else []), first["id"]))
            t1, t2 = self.sim.tags[(sid, first["id"])], self.sim.tags[(sid, second["id"])]
            self.sim.tags[(sid, first["id"])] = hs.Tag(t1.origin, frozenset(t1.carries | t2.carries))
            c.execute("DELETE FROM messages WHERE id = ?", (second["id"],))
            self.sim.tags.pop((sid, second["id"]), None)
            return "%s<-%s" % (first["id"], second["id"])
        return "fold %s %s" % (sid, self.sim.write(run))

    def op_renumber(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> None:
            old = [dict(r) for r in c.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))]
            tags = {r["id"]: self.sim.tags.pop((sid, r["id"])) for r in old}
            c.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            for r in old:
                self.ins(c, sid, r["role"], r["content"], r["timestamp"], r["message_uid"], tags[r["id"]],
                         call=r["tool_call_id"], active=r["active"], compacted=r["compacted"],
                         absorbed=r["absorbed_message_uids"], tool_calls=r["tool_calls"])
        self.sim.write(run)
        return "renumber %s" % sid

    def op_backfill(self) -> str:
        def run(c: Any) -> int:
            ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE message_uid IS NULL ORDER BY id")]
            n = self.rng.randint(1, max(1, len(ids))) if ids else 0
            for i in ids[:n]:
                c.execute("UPDATE messages SET message_uid = ? WHERE id = ?", (hs._hex(self.sim.rng), i))
            return n
        n = self.sim.write(run)
        self.uidless = False
        return "backfill %d" % n

    def _resend(self, c: Any, sid: str, last: Any, tag: hs.Tag, answer_word: str) -> None:
        t = self.tick()
        lm = self.sim.lm("user", last["content"], parents=tag.carries | {tag.origin}, kind="resend")
        self.ins(c, sid, "user", last["content"], t, self.uid(), hs.Tag(lm, frozenset({lm}) | tag.carries))
        ans = "%s: %s answer" % (answer_word, self.marker())
        self.ins(c, sid, "assistant", ans, t + 1, self.uid(), self.new("assistant", ans))

    def op_cli_retry(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> str:
            rows = self.active(c, sid)
            users = [i for i, r in enumerate(rows) if r["role"] == "user"]
            if not users:
                return "skip"
            last = rows[users[-1]]
            for r in rows[users[-1]:]:
                c.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r["id"],))
            self._resend(c, sid, last, self.sim.tags[(sid, last["id"])], "Retried")
            return str(last["id"])
        return "cli_retry %s %s" % (sid, self.sim.write(run))

    def op_undo(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> str:
            rows = self.active(c, sid)
            users = [i for i, r in enumerate(rows) if r["role"] == "user"]
            if not users:
                return "skip"
            for r in rows[users[-1]:]:
                c.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r["id"],))
            return str(rows[users[-1]]["id"])
        return "undo %s %s" % (sid, self.sim.write(run))

    def op_gateway_retry(self) -> str:
        sid = self.rng.choice(self.sessions)

        def run(c: Any) -> str:
            rows = [dict(r) for r in self.active(c, sid)]
            users = [i for i, r in enumerate(rows) if r["role"] == "user"]
            if not users:
                return "skip"
            keep, resend = rows[:users[-1]], rows[users[-1]]
            tags = {r["id"]: self.sim.tags.pop((sid, r["id"])) for r in rows}
            c.execute("DELETE FROM messages WHERE session_id = ? AND active = 1", (sid,))
            for r in keep:
                self.ins(c, sid, r["role"], r["content"], r["timestamp"], r["message_uid"], tags[r["id"]],
                         call=r["tool_call_id"], absorbed=r["absorbed_message_uids"], tool_calls=r["tool_calls"])
            self._resend(c, sid, resend, tags[resend["id"]], "Again")
            return str(resend["id"])
        return "gw_retry %s %s" % (sid, self.sim.write(run))

    def op_delete(self) -> str | None:
        if len(self.sessions) < 2:
            return None
        sid = self.rng.choice(self.sessions)
        self.sim.delete_session(sid)
        self.sessions.remove(sid)
        return "delete %s" % sid


def generate(seed: int, root: str, words: list[int], env: dict[str, bool], log: list[str],
             on_event: Callable[[Event, "Old"], Any] | None = None, steps: int = 16) -> "Old":
    """rv3res_fuzz.scenario's draws: Event callbacks where it touched the Command Center. words: the number of 32-bit
    words each logged admin choice consumed. log: the run's own lines, checked one by one (Diverged on a mismatch;
    'need' when words ran out)."""
    rng = random.Random(seed)
    uidless = rng.random() < 0.35
    sim = hs.Sim(root, seed, uids="legacy" if uidless else "modern", uid_trigger=False)
    w = Old(sim, rng, uidless, env)
    lines = iter(log)
    pos = [0]
    used = [0]

    def expect(line: str | None, prefix_only: bool = False) -> str | None:
        want = next(lines, None)
        pos[0] += 1
        if line is not None and want is not None and (want.startswith(line) if prefix_only else want == line):
            return want
        raise Diverged("line %d: got %r, the run logged %r" % (pos[0], line, want))

    def peek() -> str:
        rest = log[pos[0]:]
        return rest[0] if rest else ""

    def admin_choice() -> None:
        if used[0] >= len(words):
            raise LookupError(used[0])
        for _ in range(words[used[0]]):
            rng.getrandbits(32)
        used[0] += 1

    sid = "s%03d_root" % seed
    w.add_session(sid, title="Probe %d" % seed)
    w.sessions.append(sid)
    for _ in range(3):
        expect(w.op_turn(sid))

    def do_import() -> None:
        expect("import run", prefix_only=True)
        if on_event:
            on_event(Event("import", log[pos[0] - 1]), w)
    do_import()
    for _ in range(steps):
        roll = rng.random()
        if roll < 0.18:
            do_import()
        elif roll < 0.30:
            nxt = peek()
            if nxt.startswith("WITHHOLD") or re.match(r"withhold \d+ refused", nxt):
                admin_choice()
                line = expect(nxt)
                m = re.match(r"WITHHOLD \d+ \((\S+) (\d+)\)", line or "")
                if m and on_event:
                    on_event(Event("withhold", line, m.group(1), int(m.group(2))), w)
        elif roll < 0.34:
            if peek().startswith("release "):
                admin_choice()
                line = expect(peek())
                if on_event:
                    on_event(Event("release", line), w)
        elif roll < 0.38:
            if peek().startswith("rollback "):
                line = expect(peek())
                if on_event and "refused" not in line:
                    on_event(Event("rollback", line), w)
        else:
            op = rng.choices(OPS, WEIGHTS)[0]
            line = getattr(w, "op_" + op)()
            if line is not None:
                expect(line)
                if on_event:
                    on_event(Event("hermes", line), w)
    do_import()
    do_import()
    if pos[0] != len(log):
        raise Diverged("the run logged %d lines, the replay %d" % (len(log), pos[0]))
    return w


def reconstruct(seed: int, log: list[str], root_factory: Callable[[], str], max_words: int = 12
                ) -> tuple[list[int], dict[str, bool]] | None:
    """Depth-first: the admin choices' word counts (and the env switches) under which every logged line matches."""
    for env in ({}, {"compressed_from": True}, {"uidless_prune": True},
                {"compressed_from": True, "uidless_prune": True}):
        stack: list[list[int]] = [[]]
        tried = 0
        while stack and tried < 4000:
            words = stack.pop()
            tried += 1
            try:
                generate(seed, root_factory(), words, env, log)
                return words, env
            except LookupError:
                for t in range(max_words, 0, -1):
                    stack.append(words + [t])
            except Diverged:
                continue
    return None


def load() -> dict[str, Any]:
    if not os.path.exists(REPLAYS):
        return {}
    with open(REPLAYS) as f:
        return json.load(f)
