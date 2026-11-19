"""Crafted states for the oracle's self-tests (rebuild R5): a simulator world with exact rows, and the Command Center
rows, stones and holds as the oracle reads them, without a database. Synthetic data only."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from . import hermes_sim as hs
from . import oracle as o


@dataclass
class World:
    sim: hs.Sim
    rows: dict[str, list[int]] = field(default_factory=dict)  # sid -> row ids in order

    def session(self, *, parent: str | None = None, cfg: dict | None = None, kind: str | None = None,
                source: str = "cli") -> str:
        sid = self.sim.new_session(source=source, parent=parent, model_config=cfg)
        if parent and kind:
            self.sim.links[sid] = (parent, kind)
        self.rows[sid] = []
        return sid

    def row(self, sid: str, role: str, text: str, *, carries: set[int] | None = None, origin: int | None = None,
            uid: str | None = None, calls: str | None = None, call: str | None = None) -> tuple[int, int]:
        """A row with exact ground truth: its own logical message (or `origin`) carrying `carries` too."""
        lm = origin if origin is not None else self.sim.lm(role, text)
        tag = hs.Tag(lm, frozenset({lm} | (carries or set())))
        holder: dict[str, int] = {}

        def run(c: Any) -> None:
            holder["id"] = self.sim.insert(c, sid, role, text, self.sim.tick(), uid or hs._hex(self.sim.rng), tag,
                                           tool_calls=calls, call=call)
        self.sim.write(run)
        self.rows[sid].append(holder["id"])
        return holder["id"], lm


def world(tmp: Any, seed: int = 1) -> World:
    return World(hs.Sim(str(tmp / "jarvis"), seed))


def cc_rows(snap: hs.Snapshot, *, withheld: set[tuple[str, int]] = frozenset(),
            content: dict[tuple[str, int], str] | None = None, titles: dict[str, str] | None = None) -> list[dict]:
    """The Command Center's stored rows of a backup as cc_adapter.rows() returns them (held rows blank)."""
    out = []
    for n, ((sid, rid), v) in enumerate(sorted(snap.rows.items(), key=lambda x: x[0][1]), start=1):
        held = (sid, rid) in withheld
        text = (content or {}).get((sid, rid), v.content)
        out.append({"id": n, "sid": sid, "source_key": "jarvis:message:%s:%d" % (sid, rid),
                    "source_message_id": str(rid), "role": v.role, "at": v.ts, "source_uid": v.uid or "",
                    "content": "" if held else text, "withheld": held, "tool_calls": v.calls or None,
                    "flags": {}, "session_title": (titles or {}).get(sid, ""), "session_flags": {}})
    return out


def state(rows: list[dict], stones: list[dict] | None = None) -> dict[str, Any]:
    stones = stones or []
    return {"rows": rows, "stones": stones, "stone_by_key": {s["source_key"]: s for s in stones}}


def stone(row: dict, *, review: str = "pending", via: str = "", anchor: str = "", relations: list | None = None,
          decided_on: str = "") -> dict:
    return {"source_key": row["source_key"], "source_session_id": row["sid"], "source_row_id": row["source_message_id"],
            "role": row["role"], "source_at": row["at"], "message_uid": row.get("source_uid") or None,
            "review": review, "via": via, "anchor_key": anchor, "relations": relations or [],
            "decided_on": decided_on, "reason": "withheld by an admin"}


def run_of(w: World, snap: hs.Snapshot, *imported: hs.Snapshot) -> o.Run:
    run = o.Run(1, w.sim, "", "", None, random.Random(1))  # type: ignore[arg-type]
    run.imported = list(imported) or [snap]
    return run


def withhold(run: o.Run, rows: list[dict], sid: str, rid: int) -> o.Withhold:
    r = next(x for x in rows if x["sid"] == sid and x["source_message_id"] == str(rid))
    v = run.sim.versions_of(sid, rid)[-1]
    w = o.Withhold(r["id"], r["source_key"], sid, rid, v.content, o.message_lms(run.sim, v.tag), v.tag.origin,
                   v.role, v.ts, v.uid or "", tuple(v.absorbed))
    run.withholds.append(w)
    return w


def hold(row: dict, relations: list[str], **kw: Any) -> dict:
    out = {"message": row["id"], "relations": relations, "role": row["role"], "form": "plain", "one_click": False,
           "uncertain": False, "later_withhold": "later_withhold" in relations, "confirm_required": True}
    out.update(kw)
    return out


def groups(members: set[str] = frozenset(), uncertain: set[str] = frozenset()) -> dict[str, Any]:
    return {"members": set(members) | set(uncertain), "uncertain": set(uncertain), "hidden_titles": set()}
