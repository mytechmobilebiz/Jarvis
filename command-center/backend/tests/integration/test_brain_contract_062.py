"""JARVIS Brain 0.6.2: the contract's named regressions (plan 5.3) and the determinism suite (plan 5.2).

Written by the independent oracle team from RECOMMENDATION.md sections 3 and 5 only (not from the rebuilt engine). One
test per row of the 5.3 table, named after the findings it covers, asserting the outcome the table states under the
contract C1-C6. The conversations are built with tests/property/hermes_sim.py, whose every operation is checked
against the vendored Hermes function (tests/property/test_hermes_conformance.py). The Command Center is driven only
through the entry points the plan names (tests/property/cc_adapter.py). Synthetic data only.

Vocabulary: 'certain' = withheld for good and not listed; 'held' = withheld and listed for review; 'kept' = an admin
kept a hold; 'readable' = stored with its text; 'absent' = not stored."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text

from app import db
from app.brain import importer as imp
from app.db import session_scope
from app.security import CSRF_HEADER, CSRF_VALUE

from ..property import cc_adapter as cc
from ..property import hermes_sim as hs
from ..property import oracle as o

pytestmark = pytest.mark.usefixtures("clean_db")

OWN_DB = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1]
REQUIRED = os.environ.get("REQUIRE_DOCKER_TESTS") == "1"
ADMIN = cc.ADMIN_ACTOR
W1 = "The new vendor contract with Northwind is worth forty thousand dollars a year."
W2 = "Please move the bonus pool to the second account before Friday."
OTHER = "Please double check the landing page copy when you have a moment."


# --- the world ------------------------------------------------------------------------------------------------------


class World:
    def __init__(self, tmp: Path, client: Any, **sim_kw: Any) -> None:
        kw = {"uids": "modern", "donor": True, "compressed_marker": True, "uid_trigger": True}
        kw.update(sim_kw)
        self.tmp = tmp
        self.sim = hs.Sim(str(tmp / "jarvis"), 62, **kw)
        self.profile, self.content = str(tmp / "profile"), str(tmp / "content")
        os.makedirs(self.profile, exist_ok=True)
        os.makedirs(self.content, exist_ok=True)
        self.api = cc.Api(client)
        self.n = 0

    # Hermes side
    def session(self, source: str = "cli", **kw: Any) -> str:
        return self.sim.new_session(source=source, **kw)

    def add(self, sid: str, role: str, content: str, *, ts: float | None = None, uid: Any = "new",
            call: str | None = None, active: int = 1, compacted: int = 0) -> int:
        """One row as SessionDB.append_message writes it (a new logical message)."""
        s = self.sim
        lm = s.lm(role, content)

        def run(c: Any) -> int:
            t = s.tick(10.0) if ts is None else ts
            s.clock = max(s.clock, t)
            u = s.new_uid() if uid == "new" else uid
            tool_calls = None
            if role == "assistant" and call and not content:
                tool_calls = json.dumps([{"id": call, "type": "function", "function": {"name": "web_fetch",
                                                                                         "arguments": "{}"}}])
            rid = s.insert(c, sid, role, content, t, u, hs.Tag(lm, frozenset({lm}) if content else frozenset()),
                           call=None if role == "assistant" else call, tool_calls=tool_calls, active=active,
                           compacted=compacted)
            if role == "user" and not s.session(c, sid)["title"]:
                s.set_title(c, sid, hs.derive_title(content), lm)
            return rid
        return s.write(run)

    def turn(self, sid: str, user: str, answer: str = "Noted, I will take care of it.") -> tuple[int, int]:
        return self.add(sid, "user", user), self.add(sid, "assistant", answer)

    def repeat(self, sid: str, of_row_sid: str, of_row: int, *, ts: float | None = None) -> int:
        """An independent message with the very words of another row (a new logical message, no parent)."""
        c = self.sim.con()
        try:
            r = c.execute("SELECT * FROM messages WHERE id = ?", (of_row,)).fetchone()
        finally:
            c.close()
        return self.add(sid, r["role"], r["content"], ts=ts)

    def snap(self, label: str) -> hs.Snapshot:
        self.n += 1
        return self.sim.backup("%s%d" % (label, self.n))

    # Command Center side
    async def imp(self, snap: hs.Snapshot, **kw: Any) -> dict[str, Any]:
        return await cc.import_backup(snap.path, self.profile, self.content, **kw)

    def settle(self, snap: hs.Snapshot) -> str:
        try:
            return cc.settle(snap.path, self.profile, self.content)
        except cc.Binding:
            if cc.LENIENT:  # while the rebuild is in progress: go on and see what the rest of the test finds
                return ""
            raise

    async def stored(self, sid: str, row: int) -> list[dict[str, Any]]:
        return [r for r in await cc.rows() if r["sid"] == sid and str(r.get("source_message_id")) == str(row)]

    async def withhold(self, sid: str, row: int, **body: Any) -> dict[str, Any]:
        [r] = (await self.stored(sid, row))[-1:]
        resp = await self.api.call("POST", "/api/brain/messages/%d/withhold" % r["id"], json=body or {})
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def holds(self) -> list[dict[str, Any]]:
        return await self.api.holds()

    async def hold_of(self, sid: str, row: int) -> dict[str, Any] | None:
        ids = {r["id"] for r in await self.stored(sid, row)}
        for h in await self.holds():
            if h.get("message") in ids:
                return h
        return None

    async def status(self, sid: str, row: int) -> str:
        rows = await self.stored(sid, row)
        if not rows:
            return "absent"
        r = rows[-1]
        if not r.get("withheld"):
            return "readable"
        if await self.hold_of(sid, row) is not None:
            return "held"
        stones = {s["source_key"]: s for s in await cc.stones()}
        stone = stones.get(r["source_key"])
        if stone is not None and stone.get("review") == "kept":
            return "kept"
        return "certain"

    async def relations(self, sid: str, row: int) -> set[str]:
        h = await self.hold_of(sid, row)
        assert h is not None, "(%s, %s) is not held" % (sid, row)
        return o.reported_relations(h)[0]

    async def readable_with(self, words: str) -> list[dict[str, Any]]:
        return [r for r in await cc.rows() if not r.get("withheld") and words in (r.get("content") or "")]

    async def keep(self, sid: str, row: int) -> None:
        h = await self.hold_of(sid, row)
        assert h is not None
        r = await self.api.keep(h["message"])
        assert r.status_code == 200, r.text

    async def release(self, sid: str, row: int) -> None:
        h = await self.hold_of(sid, row)
        assert h is not None
        r = await self.api.release(h["message"])
        assert r.status_code == 200, r.text

    async def review_all(self, independent: set[tuple[str, int]] = frozenset()) -> None:
        rows = {r["id"]: r for r in await cc.rows()}
        for h in await self.holds():
            r = rows[h["message"]]
            key = (r["sid"], int(r["source_message_id"]))
            resp = await (self.api.release(h["message"]) if key in independent else self.api.keep(h["message"]))
            assert resp.status_code == 200, resp.text

    async def export(self, name: str = "decisions.json", **kw: Any) -> str:
        out = str(self.tmp / name)
        await cc.export_decisions(out, **kw)
        return out

    async def live(self, snap: hs.Snapshot, decisions: str | None) -> None:
        """The live installation: nothing stored, nothing decided; the same backup imported with the decisions file."""
        await cc.wipe()
        await self.imp(snap, decisions=decisions)


@pytest.fixture
async def client(app_client):
    app_client.headers[CSRF_HEADER] = CSRF_VALUE
    return app_client


@pytest.fixture
def world(tmp_path, client):
    def make(**kw: Any) -> World:
        return World(tmp_path, client, **kw)
    return make


async def state_rows() -> dict[str, tuple]:
    st = await o.state()
    return {r["source_key"]: o.row_state(r, st) for r in st["rows"]}


def assert_same_state(a: dict[str, tuple], b: dict[str, tuple], keys: set[str] | None = None) -> None:
    keys = set(a) | set(b) if keys is None else keys
    diff = {k: (a.get(k), b.get(k)) for k in sorted(keys) if a.get(k) != b.get(k)}
    assert not diff, "rows differ (test copy -> live): %s" % json.dumps(diff, default=str)[:3000]


def withheld_states() -> tuple[str, ...]:
    return ("certain", "held", "kept")


# --- docker: restore.ps1's database half ---------------------------------------------------------------------------


def _docker_ok() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "exec", "cc-test-pg", "true"],
                                                           capture_output=True).returncode == 0


@pytest.fixture
def restore_point(migrated):
    if not _docker_ok():
        (pytest.fail if REQUIRED else pytest.skip)("needs docker and the cc-test-pg container for pg_dump/pg_restore")

    class Point:
        path = "/tmp/%s_contract062.dump" % OWN_DB

        def _pg(self, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["docker", "exec", "cc-test-pg", *args], capture_output=True, text=True)

        def take(self) -> None:
            done = self._pg("pg_dump", "-U", "cc", "-Fc", "-f", self.path, OWN_DB)
            assert done.returncode == 0, done.stderr

        async def restore(self) -> None:
            await migrated.dispose()
            done = self._pg("pg_restore", "-U", "cc", "-d", OWN_DB, "--clean", "--if-exists", "--single-transaction",
                            "--exit-on-error", self.path)
            await migrated.dispose()
            assert done.returncode == 0, done.stderr
            await db.run_migrations()

    p = Point()
    yield p
    p._pg("rm", "-f", p.path)


# =====================================================================================================================
# 5.3: one named regression per high finding and per round-3 claim
# =====================================================================================================================


@pytest.mark.parametrize("order", ["branch_before_first_import", "branch_after_first_import"])
async def test_R1_R15_gateway_branch_copy_stored_before_the_withhold(world, order):
    """Settle: certain by rule 6, or a listed hold. Identical on live by replay. Import order is irrelevant."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    if order == "branch_after_first_import":
        await w.imp(w.snap("d0"))
    c = w.sim.gateway_branch(p)
    d = w.snap("d")
    await w.imp(d)
    crow = [v.row for (sid, _), v in d.rows.items() if sid == c and v.content == W1][0]
    assert await w.status(c, crow) == "readable"  # stored before the withhold
    await w.withhold(p, wrow)
    w.settle(d)
    assert await w.status(c, crow) in ("certain", "held")
    if await w.status(c, crow) == "held":
        assert {"identical", "same_time"} & await w.relations(c, crow)
    await w.review_all()
    tc = await state_rows()
    out = await w.export()
    await w.live(d, out)
    assert await w.status(c, crow) in withheld_states()
    live = await state_rows()
    assert_same_state(tc, live, set(live))
    assert await w.readable_with(W1) == []


@pytest.mark.parametrize("uid", [True, False])
async def test_R2_fold_stored_before_the_withhold(world, uid):
    """Settle: certain by absorbed uid. Without a uid: a hold, 'contains'."""
    w = world(uids="modern" if uid else "legacy")
    p = w.session("cli")
    a = w.add(p, "user", "Please look again at the backup schedule and flag anything odd.")
    b = w.add(p, "user", W1)  # no reply in between: Hermes folds the two turns
    w.add(p, "assistant", "Noted both of them.")
    await w.imp(w.snap("d0"))
    composite = w.sim.fold(p, pair=(a, b))
    assert composite is not None
    d = w.snap("d")
    await w.imp(d)
    assert await w.status(p, composite) == "readable"  # stored before the withhold
    await w.withhold(p, b)  # the withheld message's own (archived) row
    w.settle(d)
    if uid:
        assert await w.status(p, composite) == "certain"
    else:
        assert await w.status(p, composite) == "held"
        assert "contains" in await w.relations(p, composite)
    assert await w.readable_with(W1) == []


@pytest.mark.parametrize("how", ["cli", "gateway"])
@pytest.mark.parametrize("when", ["settle", "later_backup"])
async def test_R4_retry_resend(world, how, when):
    """Settle: a hold, 'identical'. Later backup: held. With no original left: 'not in this backup', and no one-click
    release."""
    w = world()
    p = w.session("cli" if how == "cli" else "telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow, _ = w.turn(p, W2, "I will do that today.")
    if when == "settle":  # the withhold is taken on the backup before the /retry; Settle runs on the newest one
        d0 = w.snap("d0")
        await w.imp(d0)
        await w.withhold(p, wrow)
        out = w.sim.cli_retry(p) if how == "cli" else w.sim.gateway_retry(p)
        d = w.snap("d")
        await w.imp(d)
        w.settle(d)
        assert await w.status(p, out["user"]) == "held"
        rel = await w.relations(p, out["user"])
        assert ("identical" in rel) if how == "cli" else ({"identical", "not_in_backup"} & rel)
    else:
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, wrow)
        w.settle(d)
        await w.review_all()
        out = w.sim.cli_retry(p) if how == "cli" else w.sim.gateway_retry(p)
        x = w.snap("x")
        await w.imp(x)
        assert await w.status(p, out["user"]) == "held"
        rel = await w.relations(p, out["user"])
        if how == "gateway":  # the original is gone from the backup
            assert "not_in_backup" in rel, rel
            [stored] = (await w.stored(p, out["user"]))[-1:]
            resp = await w.api.release_unrelated(stored["session_id"])
            if resp is not None:
                assert resp.status_code >= 400 or await w.status(p, out["user"]) == "held"
        else:
            assert "identical" in rel, rel
    assert await w.readable_with(W2) == []


@pytest.mark.parametrize("kind", ["rotate", "cli_branch", "compact_tail", "legacy_rotate"])
async def test_R9_R10_S10_copy_of_another_readable_message_is_never_certain(world, kind):
    """Never certain: uid conflict, not unique, or no uid. Listed only when its text equals W."""
    w = world(uids="legacy" if kind == "legacy_rotate" else "modern")
    p = w.session("cli")
    wrow, _ = w.turn(p, W2, "Understood.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    indep, _ = w.turn(p, W2, "Understood, again.")  # an independent message with the same words
    if kind in ("rotate", "legacy_rotate"):
        child = w.sim.rotate(p, 0, 2)
    elif kind == "cli_branch":
        child = w.sim.cli_branch(p)
    else:
        w.sim.compact(p, 0, 2)
        child = p
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    lm_indep = w.sim.tags[(p, indep)].origin
    copies = [v.row for (sid, _), v in d.rows.items() if sid == child and v.tag.origin == lm_indep]
    assert copies, "the copy of the independent message is in the backup"
    for row in copies:  # a rewound copy is never stored: absent is fine; certain never
        assert await w.status(child, row) in ("readable", "held", "absent"), row
    # the independent original: readable or listed (absent when Hermes rewound it, as the importer never stores a
    # rewound row); never withheld for good
    assert await w.status(p, indep) in ("readable", "held", "absent")


@pytest.mark.parametrize("event", ["compact", "renumber", "backfill"])
async def test_R3_R11_R12_R13_S5_a_release_survives_compaction_backfill_and_renumbering(world, event):
    """The released row is immutable. Its later copies are safe copies or not certain (the uid is shared with a
    released row), so they stay readable."""
    w = world(uids="legacy" if event == "backfill" else "modern", uid_trigger=event != "backfill")
    p = w.session("telegram" if event == "renumber" else "cli")
    w.add(p, "user", "Please fetch the pricing page.")
    w.add(p, "assistant", "", call="call_aa01")
    tool = w.add(p, "tool", W1, call="call_aa01")
    w.add(p, "assistant", "Fetched; the numbers are in.")
    w.add(p, "user", "Thanks, now archive it please.")
    twin = w.sim.prune(p, tool)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, twin)  # the full output
    w.settle(d)
    assert await w.status(p, tool) == "held"  # the stub: same message, other text
    await w.release(p, tool)
    assert await w.hold_of(p, tool) is None
    await w.imp(d)  # the release covers the exact row: the next import of a backup holding it brings its text back
    assert await w.status(p, tool) == "readable"
    released = (await w.stored(p, tool))[-1]
    if event == "compact":
        w.sim.compact(p, 2, 3)
    elif event == "renumber":
        w.sim.gateway_retry(p)
    else:
        w.sim.upgrade()
        w.sim.backfill(chunk=50)
    x = w.snap("x")
    await w.imp(x)
    after = [r for r in await cc.rows() if r["source_key"] == released["source_key"]]
    assert after and not after[0]["withheld"] and after[0]["content"] == released["content"]  # immutable
    stub_lm = w.sim.tags[(p, tool)].origin if (p, tool) in w.sim.tags else None
    for (sid, row), v in x.rows.items():
        if v.tag.origin == stub_lm and v.content == hs.STUB and v.active:
            got = await w.status(sid, row)
            if got == "absent" and sid == p:
                # plan 5.3 SAFE COPY: in the same session a safe copy is recorded as an alias of the readable stored
                # row (same role, call, time and text), not stored twice (core-3 deviation 200; integrator 243)
                alias = [r for r in await cc.rows() if r["sid"] == p and not r.get("withheld")
                         and r.get("role") == v.role and (r.get("tool_call_id") or "") == (v.call or "")
                         and abs((o.epoch(r.get("at")) or 0.0) - v.ts) < 2e-3 and r.get("content") == v.content]
                assert alias, (sid, row)
                continue
            assert got == "readable", (sid, row)
    assert await w.readable_with(W1) == []


@pytest.mark.parametrize("live_backup", ["same", "different"])
async def test_R5_S2_rv3_res_3_test_copy_decisions_replayed_word_for_word(world, live_backup):
    """Replay word for word. A different backup on live means a fail-safe hold."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    w.turn(p, "Please send over the newsletter draft.", W1)  # an independent answer with the same words
    w.sim.gateway_branch(p)
    w.sim.compact(p, 1, 2)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    tc = await state_rows()
    out = await w.export()
    if live_backup == "same":
        await w.live(d, out)
        live = await state_rows()
        assert_same_state(tc, live, set(live))
        assert set(tc) >= set(live)
    else:
        new_user, _ = w.turn(p, "Please prepare the content calendar for the team meeting.")
        d2 = w.snap("d2")
        await w.live(d2, out)
        assert await w.status(p, new_user) == "held"  # fail-safe: a later backup for that family
    w_origin = d.rows[(p, wrow)].tag.origin
    for r in await w.readable_with(W1):  # only the independent answer (released by the review) may be readable
        assert not any(w.sim.derives(v.tag, {w_origin}) for v in o.versions_for(w.sim, r)), r["id"]


@pytest.mark.parametrize("case", ["S1a_branch_of_branch_middle_deleted", "rv3-res-0_renumbered_then_branched",
                                  "rv3-fb-2_orphaned_rotation"])
async def test_S1_rv3_res_0_rv3_fb_2_copies_with_broken_links(world, case):
    """Later backup: the copy is held, 'same time' (and 'identical' when the text is in X)."""
    legacy = case.startswith("rv3-fb-2")
    w = world(uids="legacy" if legacy else "modern", compressed_marker=not legacy)
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    w.turn(p, "Thanks, that is all for today.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    if case.startswith("S1a"):
        b1 = w.sim.gateway_branch(p)
        b2 = w.sim.gateway_branch(b1)
        w.sim.delete_session(b1)
        target = b2
    elif case.startswith("rv3-res-0"):
        w.sim.gateway_retry(p)  # M keeps its uid and time under a new row id
        target = w.sim.gateway_branch(p)
    else:
        target = w.sim.rotate(p, 0, 3)
        w.sim.delete_session(p)
    x = w.snap("x")
    await w.imp(x)
    copies = [(sid, row) for (sid, row), v in x.rows.items() if sid == target and v.content == W1]
    assert copies
    for sid, row in copies:
        st = await w.status(sid, row)
        assert st in ("held", "certain"), (sid, row, st)
        if st == "held":
            rel = await w.relations(sid, row)
            assert "same_time" in rel or "identical" in rel, rel
    assert await w.readable_with(W1) == []


async def test_S6_S8_S12_R14_R26_S22_rv3_res_1_rv3_fb_0_rv3_ov_1_legacy_data_is_locked_then_reset(tmp_path, settings):
    """0.6.x data: the legacy lock refuses, then the reset path runs. No text-only rule exists any more."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "release-0.6.1-backend-app.tar.gz"
    if not fixture.is_file():
        (pytest.fail if REQUIRED else pytest.skip)("the vendored 0.6.1 release is missing")
    with tarfile.open(fixture) as tar:
        tar.extractall(tmp_path / "rel", filter="data")
    old = tmp_path / "rel" / "command-center" / "backend"
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
    scratch = OWN_DB + "_legacy"
    admin = await asyncpg.connect(base + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % scratch)
        await admin.execute("CREATE DATABASE %s" % scratch)
    finally:
        await admin.close()
    url = base + "/" + scratch
    env = {**os.environ, "DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1)}
    try:
        done = subprocess.run([os.sys.executable, "-m", "app.cli", "migrate"], cwd=str(old), env=env,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        # S6's shape: 0.6.1 paired an independent later answer with an archived identical one by text alone
        sim = hs.Sim(str(tmp_path / "jarvis"), 61, uids="legacy")
        p = sim.new_session(source="cli")
        lm = sim.lm("user", "Please summarise the vendor situation.")
        sim.write(lambda c: sim.insert(c, p, "user", "Please summarise the vendor situation.", sim.tick(),
                                       None, hs.Tag(lm, frozenset({lm}))))
        lm2 = sim.lm("assistant", W2)
        sim.write(lambda c: sim.insert(c, p, "assistant", W2, sim.tick(), None, hs.Tag(lm2, frozenset({lm2}))))
        lm3 = sim.lm("user", "Thanks.")
        sim.write(lambda c: sim.insert(c, p, "user", "Thanks.", sim.tick(), None, hs.Tag(lm3, frozenset({lm3}))))
        lm4 = sim.lm("assistant", W2)  # independent, same words
        sim.write(lambda c: sim.insert(c, p, "assistant", W2, sim.tick(), None, hs.Tag(lm4, frozenset({lm4}))))
        profile, content = tmp_path / "profile", tmp_path / "content"
        profile.mkdir()
        content.mkdir()
        snap1 = sim.backup("d061")
        script = old.parent / "legacy_run.py"
        script.write_text(LEGACY_SCRIPT)
        oenv = {**env, "PYTHONPATH": str(old)}

        def legacy(*args: str) -> None:
            done = subprocess.run([os.sys.executable, str(script), *args], cwd=str(old), env=oenv,
                                  capture_output=True, text=True, timeout=300)
            assert done.returncode == 0, done.stdout + done.stderr

        legacy("run", snap1.path, str(profile), str(content))
        legacy("withhold", W2)
        sim.compact(p, 0, 2)
        snap2 = sim.backup("d061b")
        legacy("run", snap2.path, str(profile), str(content))
        # 0.6.2 migrates the database
        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine(env["DATABASE_URL"])
        try:
            await db.run_migrations(engine)
            async with engine.connect() as conn:
                locked = await conn.scalar(text(
                    "SELECT count(*) FROM brain_maintenance WHERE name = 'legacy_brain_data'"))
        finally:
            await engine.dispose()
        assert locked == 1, "0.6.1 Brain data must set the legacy lock (plan 1.7, 8.1)"
        new = Path(__file__).resolve().parents[2]
        nenv = {**env, "PYTHONPATH": str(new)}
        done = subprocess.run([os.sys.executable, "-m", "app.cli", "brain-import", "run", "--db", snap2.path,
                               "--profile", str(profile), "--content", str(content), "--all"], cwd=str(new), env=nenv,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 2 and "legacy-reset" in (done.stdout + done.stderr), done.stdout + done.stderr
        export = tmp_path / "legacy-decisions.json"
        done = subprocess.run([os.sys.executable, "-m", "app.cli", "brain-import", "legacy-reset", "--export",
                               str(export), "--yes"], cwd=str(new), env=nenv, capture_output=True, text=True,
                              timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        assert json.loads(export.read_text()).get("version") == 5
        conn = await asyncpg.connect(url)
        try:
            assert await conn.fetchval("SELECT count(*) FROM brain_maintenance WHERE name = 'legacy_brain_data'") == 0
            assert await conn.fetchval("SELECT count(*) FROM import_runs WHERE status <> 'rolled_back'") == 0
        finally:
            await conn.close()
        done = subprocess.run([os.sys.executable, "-m", "app.cli", "brain-import", "run", "--db", snap2.path,
                               "--profile", str(profile), "--content", str(content), "--all"], cwd=str(new),
                              env=nenv, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        done = subprocess.run([os.sys.executable, "-m", "app.cli", "brain-import", "settle", "--backup", snap2.path],
                              cwd=str(new), env=nenv, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        conn = await asyncpg.connect(url)
        try:  # the independent answer (lm4's rows) is never withheld for good: readable, or listed
            certain = await conn.fetch(
                "SELECT m.source_message_id, s.source_session_id FROM archive_messages m JOIN archive_sessions s ON "
                "s.id = m.session_id LEFT JOIN brain_tombstones t ON t.source_key = m.source_key WHERE m.withheld AND "
                "COALESCE(t.review, '') <> 'pending'")
        finally:
            await conn.close()
        for r in certain:
            vs = sim.versions_of(r["source_session_id"], int(r["source_message_id"]))
            assert not any(v.tag.origin == lm4 for v in vs), "the independent answer is withheld for good"
    finally:
        admin = await asyncpg.connect(base + "/postgres")
        try:
            await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % scratch)
        finally:
            await admin.close()


LEGACY_SCRIPT = '''
import asyncio, sys
from sqlalchemy import select


async def main():
    from app.brain import importer as imp, review
    from app.db import dispose_engine, session_scope
    from app.models import ArchiveMessage
    if sys.argv[1] == "run":
        await imp.run(imp.SourcePaths(db=sys.argv[2], profile=sys.argv[3], content=sys.argv[4]),
                      imp.Selection(all=True))
    elif sys.argv[1] == "withhold":  # what 0.6.1's POST /api/brain/messages/{id}/withhold did
        async with session_scope() as s:
            m = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content.contains(sys.argv[2]))
                               .order_by(ArchiveMessage.id).limit(1))
            await review.withhold_message(s, m, actor="admin@test.local")
    await dispose_engine()

asyncio.run(main())
'''


async def test_S14_withhold_in_a_gateway_branch_child_reaches_the_unique_same_time_origin(world):
    """Settle: the parent's unique same-time origin is certain."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    orig = w.add(p, "assistant", W1)
    c = w.sim.gateway_branch(p)
    d = w.snap("d")
    await w.imp(d)
    [copy] = [row for (sid, row), v in d.rows.items() if sid == c and v.content == W1]
    await w.withhold(c, copy)
    w.settle(d)
    assert await w.status(p, orig) == "certain"
    assert await w.readable_with(W1) == []


async def test_rv3_res_2_cli_retry_of_a_held_copy_is_held(world):
    """Later backup: the re-send is held."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow, _ = w.turn(p, W2, "Understood.")
    c = w.sim.gateway_branch(p)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    out = w.sim.cli_retry(c)
    x = w.snap("x")
    await w.imp(x)
    assert await w.status(c, out["user"]) == "held"
    assert await w.readable_with(W2) == []


async def test_rv3_res_4_orphaned_rotation_stays_in_the_family(world):
    """_compressed_from and the sticky link put it in the family, and its copy is held."""
    w = world(uids="legacy", compressed_marker=True)
    p = w.session("cli")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    w.turn(p, "Thanks, that is all for today.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    child = w.sim.rotate(p, 0, 3)
    w.sim.delete_session(p)
    x = w.snap("x")
    await w.imp(x)
    copies = [(sid, row) for (sid, row), v in x.rows.items() if sid == child and v.content == W1]
    assert copies
    for sid, row in copies:
        assert await w.status(sid, row) in ("held", "certain")
    assert await w.readable_with(W1) == []


async def test_rv3_res_5_rv3_fb_1_legacy_prune_twin_is_held_with_honest_evidence(world):
    """Later backup: the twin is held, 'same time' and 'same tool call'. Its evidence is never 'differs' while W is
    in memory."""
    w = world(uids="legacy", uid_trigger=False)
    p = w.session("cli")
    w.add(p, "user", "Please fetch the pricing page.")
    w.add(p, "assistant", "", call="call_bb02")
    tool = w.add(p, "tool", W1, call="call_bb02")
    w.add(p, "assistant", "Fetched; the numbers are in.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, tool)
    w.settle(d)
    await w.review_all()
    w.sim.upgrade()  # a Hermes with message_uid but not yet the insert trigger
    twin = w.sim.prune(p, tool)
    w.sim.backfill(chunk=100)  # stub and twin get different random uids
    x = w.snap("x")
    await w.imp(x)
    st = await w.status(p, twin)
    assert st in ("held", "certain"), st
    if st == "held":
        rel = await w.relations(p, twin)
        assert {"same_time", "same_call", "identical"} & rel and "differs" not in rel, rel
    assert await w.readable_with(W1) == []


async def test_rv3_res_6_branch_copy_of_a_fold_survivor_stays_readable(world):
    """Readable: fold stones are not uid sources."""
    w = world()
    p = w.session("cli")
    a = w.add(p, "user", "Please look again at the backup schedule and flag anything odd.")
    b = w.add(p, "user", W2)
    w.add(p, "assistant", "Noted both of them.")
    c = w.sim.cli_branch(p)
    w.sim.fold(p, pair=(a, b))
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, b)  # B's own (archived) row in the parent
    w.settle(d)
    a_lm = w.sim.tags[(p, a)].origin
    [a_copy] = [row for (sid, row), v in d.rows.items() if sid == c and v.tag.origin == a_lm]
    assert await w.status(c, a_copy) == "readable"
    b_lm = w.sim.tags[(p, b)].origin
    [b_copy] = [row for (sid, row), v in d.rows.items() if sid == c and v.tag.origin == b_lm]
    assert await w.status(c, b_copy) in withheld_states()


async def test_rv3_res_7_uidless_renumber_duplicate_is_a_listed_hold(world):
    """Settle: the stale row at W's role and time is a listed hold, never offered as 'identical, leave readable'."""
    w = world(uids="legacy")
    p = w.session("cli")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    w.turn(p, "Thanks, that is all for today.")
    await w.imp(w.snap("d0"))
    w.sim.replace(p, drop_last_turn=False)  # destructive: every row again, new ids, no uids
    d = w.snap("d")
    await w.imp(d)
    [now] = [row for (sid, row), v in d.rows.items() if sid == p and v.content == W1]
    assert now != wrow
    await w.withhold(p, now)
    w.settle(d)
    assert await w.status(p, wrow) in ("held", "certain")
    assert await w.readable_with(W1) == []


@pytest.mark.parametrize("when", ["settle", "later_backup"])
async def test_rv3_fb_3_legacy_fold_is_a_hold_contains(world, when):
    """Settle: a hold, 'contains'. Later backup: held, 'contains'."""
    w = world(uids="legacy")
    p = w.session("cli")
    a = w.add(p, "user", "Please look again at the backup schedule and flag anything odd.")
    b = w.add(p, "user", W1)
    w.add(p, "assistant", "Noted both of them.")
    if when == "settle":
        await w.imp(w.snap("d0"))
        comp = w.sim.fold(p, pair=(a, b))
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, b)
        w.settle(d)
    else:
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, b)
        w.settle(d)
        await w.review_all()
        comp = w.sim.fold(p, pair=(a, b))
        await w.imp(w.snap("x"))
    assert await w.status(p, comp) == "held"
    assert "contains" in await w.relations(p, comp)
    assert await w.readable_with(W1) == []


@pytest.mark.parametrize("order", ["rotation_then_withhold", "withhold_then_rotation"])
async def test_rv3_fb_4_rv3_ov_2_donor_timestamp_copy_is_not_certain(world, order):
    """Not certain, because the parent has two rows with that text. Listed. W' stays readable, and the admin's 'left
    readable' choice stands."""
    w = world(uids="legacy", donor=True)
    p = w.session("cli")
    r1, _ = w.turn(p, W2, "Understood.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    r5, _ = w.turn(p, W2, "Understood, again.")  # W': independent, same words
    if order == "rotation_then_withhold":
        child = w.sim.rotate(p, 0, 2)  # carries r5 only: its copy gets r1's time (first match)
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, r1)
        w.settle(d)
    else:
        d = w.snap("d")
        await w.imp(d)
        [r5_stored] = (await w.stored(p, r5))[-1:]
        await w.withhold(p, r1, leave_readable=[r5_stored["id"]], include_identical=False)
        w.settle(d)
        await w.review_all(independent={(p, r5)})
        assert await w.status(p, r5) == "readable"  # the admin's choice
        child = w.sim.rotate(p, 0, 2)
        await w.imp(w.snap("x"))
    r5_lm = w.sim.tags[(p, r5)].origin
    copies = [(sid, row) for (sid, row), v in w.sim.snapshots[-1].rows.items() if sid == child
              and v.tag.origin == r5_lm]
    assert copies
    for sid, row in copies:
        assert await w.status(sid, row) in ("held", "readable"), (sid, row)
    assert await w.status(p, r5) in ("readable", "held")


@pytest.mark.parametrize("case", ["prune_twin_identical", "restated"])
async def test_rv3_fb_5_R7_evidence_is_computed(world, case):
    """The evidence is computed: 'identical' or 'restated'."""
    w = world()
    p = w.session("cli")
    if case == "prune_twin_identical":
        w.add(p, "user", "Please fetch the pricing page.")
        w.add(p, "assistant", "", call="call_cc03")
        tool = w.add(p, "tool", W1, call="call_cc03")
        w.add(p, "assistant", "Fetched; the numbers are in.")
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, tool)
        w.settle(d)
        await w.review_all()
        c = w.sim.cli_branch(p)
        [copy] = [row for (sid, row), v in w.sim.backup("probe").rows.items() if sid == c and v.content == W1]
        twin = w.sim.prune(c, copy)
        w.sim.delete_session(p)
        await w.imp(w.snap("x"))
        st = await w.status(c, twin)
        assert st in ("held", "certain")
        if st == "held":
            rel = await w.relations(c, twin)
            assert "identical" in rel and "differs" not in rel, rel
    else:
        w.turn(p, "Please summarise the vendor situation for me.")
        u = w.add(p, "user", W2)  # the in-flight request (short, withheld)
        w.add(p, "assistant", "", call="call_cc04")
        w.add(p, "tool", "Search results for the bonus pool: the first entries match.", call="call_cc04")
        w.sim.compact(p, 0, 1, restate="row")
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, u)
        w.settle(d)
        replays = [row for (sid, row), v in d.rows.items() if sid == p and v.content.startswith(hs.REPLAY_HEADER)]
        assert replays
        for row in replays:
            st = await w.status(p, row)
            assert st in ("held", "certain")
            if st == "held":
                rel = await w.relations(p, row)
                assert "restated" in rel and "differs" not in rel, rel
    assert await w.readable_with(W1 if case == "prune_twin_identical" else W2) == [] or case == "restated"


@pytest.mark.parametrize("move", ["rotation_parent_deleted", "gateway_branch"])
async def test_rv3_fb_6_R6_R17_S3_title_moves_stay_hidden(world, move):
    """Current fixes, plus: a new family session's title is hidden until released."""
    w = world()
    p = w.session("telegram")
    first = w.add(p, "user", W2)  # Hermes's derived title: its first 48 characters
    w.add(p, "assistant", "Understood, I will do it.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, first, hide_title=True)
    w.settle(d)
    await w.review_all()
    if move == "rotation_parent_deleted":
        child = w.sim.rotate(p, 0, 2)
        w.sim.delete_session(p)
    else:
        child = w.sim.gateway_branch(p)
    x = w.snap("x")
    await w.imp(x)
    title = hs.derive_title(W2)
    assert x.sessions[child]["title"] and title[:20] in x.sessions[child]["title"]
    stored = [s for s in await cc.sessions() if s.get("source_session_id") == child]
    assert stored and title[:20] not in (stored[0].get("title") or "")
    r = await w.api.call("GET", "/api/brain/sessions", params={"limit": 200})
    assert title[:20] not in r.text


async def test_rv3_fb_7_S7_restatement_under_its_own_uid_is_listed_not_certain(world):
    """Settle: listed with 'restated; own message id', pre-selected for release. No false evidence."""
    w = world()
    p = w.session("cli")
    wrow, _ = w.turn(p, W2, "Understood.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    later = w.add(p, "user", W2)  # an independent later request with the same words: the in-flight one
    w.add(p, "assistant", "", call="call_dd05")
    w.add(p, "tool", "Search results for the bonus pool: the first entries match.", call="call_dd05")
    w.sim.compact(p, 0, 1, restate="row")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    later_lm = w.sim.tags.get((p, later)) or d.rows[(p, later)].tag
    replays = [row for (sid, row), v in d.rows.items() if sid == p and v.tag.origin == later_lm.origin
               and v.content.startswith(hs.REPLAY_HEADER)]
    assert replays
    for row in replays:
        assert await w.status(p, row) in ("readable", "held")
        if await w.status(p, row) == "held":
            rel = await w.relations(p, row)
            assert "restated" in rel and not ({"differs"} & rel), rel


async def test_rv3_ov_0_row_id_reuse_after_a_snapshot_restore_is_never_certain(world):
    """The trigger and exact identity reject the match (uid or time differs). In a family it is held and listed;
    elsewhere it is readable."""
    w = world()
    p = w.session("cli")
    w.turn(p, "Please summarise the vendor situation for me.")
    cp = w.sim.checkpoint()
    wrow = w.add(p, "assistant", W1)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    w.sim.restore_checkpoint(cp)  # hermes backup import: sqlite_sequence comes back
    reused = w.add(p, "assistant", "Done, the content calendar is ready for the team.")
    assert reused == wrow  # the same JARVIS row id, another message
    x = w.snap("x")
    await w.imp(x)
    st = await w.status(p, reused)
    assert st in ("held", "readable") and st != "certain", st
    if st == "held":
        assert await w.hold_of(p, reused) is not None
    assert await w.readable_with(W1) == []


async def test_rv3_ov_3_same_second_gateway_copy_of_a_readable_message_is_a_safe_copy(world):
    """Later backup: the copy of the readable message is a safe copy and readable. The time rules are gone."""
    w = world()
    p = w.session("whatsapp")
    t = float(int(w.sim.tick(100.0)))
    wrow = w.add(p, "user", W2, ts=t)
    other = w.add(p, "user", OTHER, ts=t)  # another message in the same whole second
    w.add(p, "assistant", "Both noted.", ts=t + 3)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all(independent={(p, other)})
    await w.imp(d)  # a released row's text comes back at the next import of a backup holding it (owner Q6; 240)
    assert await w.status(p, other) == "readable"
    c = w.sim.gateway_branch(p)
    x = w.snap("x")
    await w.imp(x)
    copies = [row for (sid, row), v in x.rows.items() if sid == c and v.content == OTHER]
    assert copies
    for row in copies:
        assert await w.status(c, row) == "readable"


@pytest.mark.parametrize("words", ["same_as_withheld", "other"])
async def test_rv3_ov_4_queued_message_before_the_child_start(world, words):
    """Not certain, because its time differs. Listed only when its text equals W."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    child = w.sim.rotate(p, 0, 2)
    started = w.sim.backup("probe").sessions[child]["started_at"]
    q = w.add(child, "user", W1 if words == "same_as_withheld" else OTHER, ts=float(int(started)) - 1.0)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    st = await w.status(child, q)
    assert st != "certain", st
    assert st == ("held" if words == "same_as_withheld" else "readable"), st


@pytest.mark.parametrize("where", ["settle", "later_backup", "outside_the_family"])
@pytest.mark.parametrize("quote", ["full", "partial"])
async def test_N1_reply_pointer_quoting_the_withheld_message(world, where, quote):
    """Settle: a hold, 'part of (reply quote)'. Later backup: held with that code. Outside a family: owner Q8 (a),
    held for review."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    q = W1 if quote == "full" else W1[4:40]
    reply_text = hs.reply_pointer(q, "Is that the yearly figure?")
    if where == "settle":
        r = w.add(p, "user", reply_text)
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, wrow)
        w.settle(d)
        sid = p
    else:
        d = w.snap("d")
        await w.imp(d)
        await w.withhold(p, wrow)
        w.settle(d)
        await w.review_all()
        sid = p if where == "later_backup" else w.session("telegram")
        r = w.add(sid, "user", reply_text)
        await w.imp(w.snap("x"))
    assert await w.status(sid, r) == "held"
    assert "part_of" in await w.relations(sid, r) or (quote == "full" and "contains" in await w.relations(sid, r))


# --- rv3-dur-0, -2, -3, -5, -6: the targeted fixes (1.1, 1.7, 7.2, 7.3, 7.4) -----------------------------------------


async def test_rv3_dur_0_a_pending_restore_refuses_before_any_migration(monkeypatch, settings):
    """7.3: bootstrap calls refuse_while_a_restore_is_pending() before run_migrations()."""
    from app import bootstrap as boot
    from app import restore_carry

    async with session_scope() as s:
        await s.execute(text(
            "INSERT INTO brain_maintenance (name, detail) VALUES ('restore_reapply_pending', jsonb_build_object("
            "'access', true, 'removals', true, 'access_file', 'access-before-restore-1.json', 'removals_file', "
            "'removals-before-restore-1.json', 'backup', 'old', 'at', now())) ON CONFLICT (name) DO UPDATE SET "
            "detail = EXCLUDED.detail"))
    migrated = []

    async def spy(*a: Any, **k: Any) -> list[str]:
        migrated.append(True)
        return []
    monkeypatch.setattr(boot, "run_migrations", spy)
    with pytest.raises(restore_carry.RestorePending):
        await boot.bootstrap(settings)
    assert migrated == [], "a refused start must not migrate the restored database first"


async def test_rv3_dur_2_an_older_importer_cannot_write_rows_on_0_6_2(world):
    """1.7: the row-level refusal brain_refuse_old_rows: rows of a run by an importer older than 1.1.0 are refused."""
    async with session_scope() as s:
        cols = {r[0] for r in (await s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'import_runs'"))).all()}
        assert "importer_version" in cols
        # the 0.6.1 run started before 0008 was installed: its run row predates every refusal (triggers off for it)
        await s.execute(text("SET LOCAL session_replication_role = replica"))
        run_id = await s.scalar(text(
            "INSERT INTO import_runs (status, importer_version) VALUES ('running', '1.0.4') RETURNING id"))
        await s.execute(text("SET LOCAL session_replication_role = origin"))
        sess = await s.scalar(text(
            "INSERT INTO archive_sessions (source_key, source_session_id) VALUES ('jarvis:session:old', 'old') "
            "RETURNING id"))
        await s.commit()
    with pytest.raises(Exception):
        async with session_scope() as s:
            await s.execute(text(
                "INSERT INTO archive_messages (session_id, source_key, source_message_id, position, role, content, "
                "import_run_id) VALUES (:s, 'jarvis:message:old:1', '1', 0, 'assistant', 'text', :r)"),
                {"s": sess, "r": run_id})
            await s.commit()


async def test_rv3_dur_3_a_version_redaction_survives_a_restore(client, restore_point):
    """7.2: --removals-only carries the version redactions; a restore of an older backup puts them back."""
    from app.brain import store

    async with session_scope() as s:
        item = await store.create_item(s, kind="business", title="Vendor terms", body="First body " + W2,
                                       actor="seed@test.local")
        await store.edit_item(s, item, body="Second body, nothing sensitive.", actor="seed@test.local")
        item_id = item.id
    restore_point.take()
    api = cc.Api(client)
    r = await api.call("POST", "/api/brain/items/%d/versions/1/redact" % item_id, json={})
    assert r.status_code == 200, r.text
    out = "/tmp/%s_dur3_removals.json" % OWN_DB  # noqa: S108 (this run's own file)
    await cc.export_decisions(out, removals_only=True)
    await restore_point.restore()
    await cc.apply_removals_only(out)
    async with session_scope() as s:
        found = await s.scalar(text("SELECT count(*) FROM memory_item_versions WHERE item_id = :i AND "
                                    "(to_jsonb(memory_item_versions)::text) LIKE :w"), {"i": item_id,
                                                                                         "w": "%bonus pool%"})
    os.remove(out)
    assert found == 0, "the redacted version's text came back with the restore"


@pytest.mark.parametrize("mark", ["absent", "present"])
def test_rv3_dur_5_a_stale_pending_marker_is_checked_against_the_database_mark(tmp_path, mark):
    """7.4: restore.ps1 -FinishPending reads the database's restore mark before acting on restore-pending.json. With
    no mark in the database (the earlier restore finished; the marker is stale) the old access and removals files are
    not applied again; with the mark they are (the control)."""
    from ..unit import test_restore_ps1 as rp

    detail = ('{"access": true, "removals": true, "access_file": "access-before-restore-1.json", '
              '"removals_file": "removals-before-restore-1.json", "backup": "old", "at": "now"}')
    answer = '""' if mark == "absent" else "'%s'" % detail
    stub = rp.STUB.replace(
        "  if ($line -match \"version = '0006_brain'\") { \"1\"; return }",
        "  if ($line -match 'restore_reapply_pending') { %s; return }\n"
        "  if ($line -match \"version = '0006_brain'\") { \"1\"; return }" % answer)
    stub = stub.replace('& (Join-Path $Dir "scripts/restore.ps1") -BackupFolder (Join-Path $Dir "backups/old") -Yes '
                        '@flags', '& (Join-Path $Dir "scripts/restore.ps1") -FinishPending -Yes')
    assert stub.count("restore_reapply_pending") == 1 and "-FinishPending -Yes" in stub
    saved = rp.STUB
    rp.STUB = stub
    try:
        out, calls, code = rp.run_restore(tmp_path, "ok", pending=True)
    finally:
        rp.STUB = saved
    applied = [c for c in calls if "before-restore-1.json" in c]  # handed to the helper to be applied
    if mark == "absent":
        assert applied == [], "a stale marker's files were applied again: %s\n%s" % (applied, out[-1500:])
    else:
        assert applied, "the control: with the database's mark the saved files are applied\n%s" % out[-1500:]


async def test_rv3_dur_6_kept_by_survives_a_restore(world, restore_point):
    """1.1: kept_by and kept_at on the stone; a hold kept after the backup, on a row imported after it, comes back
    kept, with who and when."""
    w = world()
    p = w.session("cli")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    restore_point.take()
    new_user, _ = w.turn(p, "Please prepare the content calendar for the team meeting.")
    x = w.snap("x")
    await w.imp(x)
    assert await w.status(p, new_user) == "held"
    await w.keep(p, new_user)
    [row] = (await w.stored(p, new_user))[-1:]
    stone = {s["source_key"]: s for s in await cc.stones()}[row["source_key"]]
    assert stone.get("kept_by") == ADMIN and stone.get("kept_at")
    out = str(w.tmp / "removals.json")
    await cc.export_decisions(out, removals_only=True)
    await restore_point.restore()
    await cc.apply_removals_only(out)
    await w.imp(x)
    [row] = (await w.stored(p, new_user))[-1:]
    stone = {s["source_key"]: s for s in await cc.stones()}[row["source_key"]]
    assert row["withheld"] and stone.get("review") == "kept" and stone.get("kept_by") == ADMIN
    assert ((row.get("flags") or {}).get("withheld_by_admin") or {}).get("review") == "kept"


# =====================================================================================================================
# 5.2: determinism, test copy against live: (key, withheld, review, via, content hash) for every row
# =====================================================================================================================


def _family_world(w: World) -> tuple[str, int]:
    """A conversation family with every kind of copy: a gateway branch, a CLI branch, a rotation, a fold, a prune, an
    in-place compaction, an independent repeat, a reply quote; and an unrelated conversation."""
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    w.turn(p, "Please send over the newsletter draft.", W1)  # independent, same words
    w.add(p, "user", hs.reply_pointer(W1[:30], "Is that per year?"))
    w.add(p, "assistant", "", call="call_ee06")
    tool = w.add(p, "tool", "Fetched page for the vendor review: " + W1, call="call_ee06")
    w.add(p, "assistant", "That is per year.")
    w.sim.prune(p, tool)
    w.sim.gateway_branch(p)
    c2 = w.sim.cli_branch(p)
    w.sim.compact(c2, 1, 2)
    w.sim.rotate(p, 0, 3)
    o_sid = w.session("cli")
    w.turn(o_sid, "Please prepare the content calendar for the team meeting.", W1)  # unrelated, same words
    return p, wrow


async def _test_copy(w: World, d: hs.Snapshot, p: str, wrow: int, *, sample: int | None = None
                     ) -> tuple[dict[str, tuple], str]:
    if sample is not None:
        await w.imp(d, selection=imp.Selection(sample=sample))
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    w_origin = w.sim.tags[(p, wrow)].origin
    rows = {r["id"]: r for r in await cc.rows()}
    independent = set()
    for h in await w.holds():
        r = rows[h["message"]]
        vs = o.versions_for(w.sim, r)
        if vs and not any(w.sim.derives(v.tag, {w_origin}) for v in vs):
            independent.add((r["sid"], int(r["source_message_id"])))
    await w.review_all(independent)
    await w.imp(d)  # the documented next import brings the released rows' text back (owner Q6; deviation 240)
    return await state_rows(), await w.export()


@pytest.mark.parametrize("path", ["plain", "rollback_then_reimport", "sample_then_full_on_the_test_copy",
                                  "live_in_two_stages"])
async def test_determinism_test_copy_against_live(world, path):
    w = world()
    p, wrow = _family_world(w)
    d = w.snap("d")
    tc, out = await _test_copy(w, d, p, wrow, sample=1 if path == "sample_then_full_on_the_test_copy" else None)
    await cc.wipe()
    if path == "live_in_two_stages":
        await w.imp(d, decisions=out, selection=imp.Selection(sample=1))
        await w.imp(d)
    else:
        first = await w.imp(d, decisions=out)
        if path == "rollback_then_reimport":
            await cc.rollback(first["run_id"])
            await w.imp(d)
    live = await state_rows()
    assert_same_state(tc, live, set(live) | {k for k in tc if k in live})
    assert set(live) >= {k for k in tc if k in live}


async def test_determinism_after_restore_ps1_with_a_reimport(world, restore_point):
    w = world()
    p, wrow = _family_world(w)
    d = w.snap("d")
    tc, out = await _test_copy(w, d, p, wrow)
    await cc.wipe()
    restore_point.take()  # the live installation's backup before its import
    await w.imp(d, decisions=out)
    removals = str(w.tmp / "removals.json")
    await cc.export_decisions(removals, removals_only=True)
    await restore_point.restore()
    await cc.apply_removals_only(removals)
    await w.imp(d)
    live = await state_rows()
    assert_same_state(tc, live, set(live))
