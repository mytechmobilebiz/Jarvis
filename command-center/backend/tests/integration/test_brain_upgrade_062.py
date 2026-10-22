"""Upgrading to 0.6.2 (spec 3.3, 19.3): from 0.5.0, 0.6.0 and 0.6.1, with and without data, withholds and tombstones;
and the leak states the real 0.6.1 importer leaves, closed by the migration before the first request.

The legacy_061 fixture runs the exact 0.6.1 importer (the release commit d4f36746: its backend/app, vendored in
tests/fixtures and checked against a pinned SHA-256, or extracted with `git archive`, read only) in a process of its
own against a scratch database: it imports a synthetic JARVIS backup, withholds messages as 0.6.1's API did, then
imports a later backup in which Hermes compacted, edited and backfilled uids. That database holds every leak state the
confirmed findings describe (A-tail, B, C1), made by the code that made them. 0.6.2 then migrates it, runs its start-up
step and imports again. The vendored copy runs these tests outside a git checkout too (a copied tree, a shallow clone);
with neither, they fail under REQUIRE_DOCKER_TESTS=1 (the release run) instead of skipping. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import asyncpg
import pytest

from app import db

from .. import brain_jarvis as bj
from .test_brain_identity_migration import FX_061, FX_EXTRA, WITHHELD_BY_REPAIR, _base, _state
from .test_brain_schema import _migrate

BACKEND = Path(__file__).resolve().parents[2]
RELEASE_061 = "d4f36746"
# `git archive --format=tar d4f36746 command-center/backend/app | gzip -9n`: the 0.6.1 release's backend/app, so the
# upgrade tests run without the repository (review 1 R36). Pinned: a changed copy is a failure, never a skip.
VENDORED_061 = Path(__file__).resolve().parents[1] / "fixtures" / "release-0.6.1-backend-app.tar.gz"
VENDORED_061_SHA256 = "c3eee2bbd7cbd2df921b347856ab6af3f48633b444411dabee3561b66612be39"
SCRATCH = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1] + "_upgrade"
T = 1795000000.0
MARKS = {"tail": "zqupgtail61", "repeat": "zqupgrepeat61", "uid": "zqupguid61"}


def secret(mark: str) -> str:
    return "The new vendor contract with %s is worth forty thousand dollars a year." % mark


@pytest.fixture
async def scratch(settings):
    admin = await asyncpg.connect(_base(settings) + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % SCRATCH)
        await admin.execute("CREATE DATABASE %s" % SCRATCH)
    finally:
        await admin.close()
    try:
        yield _base(settings) + "/" + SCRATCH
    finally:
        admin = await asyncpg.connect(_base(settings) + "/postgres")
        try:
            await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % SCRATCH)
        finally:
            await admin.close()


async def _install(url: str, before: str, *fixtures: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, "
                           "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in db.migration_files():
            if path.stem < before:
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
        for sql in fixtures:
            async with conn.transaction():
                await conn.execute(sql)
    finally:
        await conn.close()


@pytest.mark.parametrize("release, before, data", [
    ("0.5.0", "0006", False), ("0.6.0", "0007", False), ("0.6.0", "0007", True), ("0.6.1", "0008", False),
    ("0.6.1", "0008", True)])
async def test_upgrade_from_0_5_0_0_6_0_0_6_1_with_and_without_data_withholds_and_tombstones(scratch, release, before,
                                                                                            data):
    await _install(scratch, before, *((FX_061, FX_EXTRA) if data else ()))
    expected = [p.stem for p in db.migration_files() if p.stem >= before]
    assert await _migrate(scratch) == expected and expected[-1] == "0008_brain_withhold_identity"
    assert await _migrate(scratch) == []
    conn = await asyncpg.connect(scratch)
    try:
        assert await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE tgname = 'archive_messages_keep_withheld'")
        pending = json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))
        assert pending["repair_done"] and pending["readable_by_key"] == pending["readable_by_jarvis_row"] == 0
        if data:  # 0.6.x Brain data: marked legacy (0.6.2 plan 1.7), never re-derived; every tombstone kept
            state = await _state(conn)
            assert state["withheld_by_repair"] == WITHHELD_BY_REPAIR
            assert pending["legacy_lock"] is True
            reasons = dict(await conn.fetch("SELECT source_key, reason FROM brain_tombstones"))
            assert reasons["jarvis:file:profile/memories/MEMORY.md#entry-0123456789abcdef"] == "purged by an admin"
            assert {k for k, r in reasons.items() if r == "withheld by an admin"} >= {
                "jarvis:message:S1:101", "jarvis:message:uid:uD", "jarvis:message:S1:130", "jarvis:message:S1:140",
                "jarvis:message:S2:165", "jarvis:message:uid:uY", "jarvis:message:uid:uQ", "jarvis:message:S7:500"}
            assert await conn.fetchval("SELECT count(*) FROM audit_log WHERE action = "
                                       "'brain.withhold_identity_repaired'") == 1
        else:
            assert await conn.fetchval("SELECT count(*) FROM audit_log") == 0
            assert pending["legacy_lock"] is False
    finally:
        await conn.close()


# --- The real 0.6.1 importer's leak states ---------------------------------------------------------------------

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


def _missing(reason: str) -> None:
    """A prerequisite the release run must have: a failure under REQUIRE_DOCKER_TESTS=1, a skip otherwise."""
    (pytest.fail if os.environ.get("REQUIRE_DOCKER_TESTS") == "1" else pytest.skip)(reason)


def _vendored_061_ok() -> bool:
    if not VENDORED_061.is_file():
        return False
    found = hashlib.sha256(VENDORED_061.read_bytes()).hexdigest()
    if found != VENDORED_061_SHA256:
        pytest.fail("the vendored 0.6.1 release (%s) was changed: sha256 %s" % (VENDORED_061.name, found))
    return True


def _release_061(dest: Path) -> Path:
    """The 0.6.1 backend (its app package), read-only: the vendored copy of the release commit when it is there (its
    SHA-256 checked), else extracted from the release commit with git archive (no checkout, no change to the
    repository). With neither, a failure under REQUIRE_DOCKER_TESTS=1 and a skip otherwise (review 1 R36)."""
    dest.mkdir(parents=True, exist_ok=True)
    if _vendored_061_ok():
        with tarfile.open(VENDORED_061) as tar:
            tar.extractall(dest / "tree", filter="data")
        return dest / "tree" / "command-center" / "backend"
    git = shutil.which("git")
    if not git:
        _missing("the 0.6.1 release: no vendored copy (%s) and no git to extract it" % VENDORED_061.name)
    top = subprocess.run([git, "-C", str(BACKEND), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        _missing("the 0.6.1 release: no vendored copy (%s) and not a git checkout" % VENDORED_061.name)
    prefix = os.path.relpath(BACKEND, top.stdout.strip())
    archive = dest / "release.tar"
    with open(archive, "wb") as f:
        done = subprocess.run([git, "-C", top.stdout.strip(), "archive", RELEASE_061, prefix], stdout=f,
                              stderr=subprocess.PIPE)
    if done.returncode != 0:
        _missing("the 0.6.1 release: no vendored copy (%s) and commit %s is not in this repository"
                 % (VENDORED_061.name, RELEASE_061))
    with tarfile.open(archive) as tar:
        tar.extractall(dest / "tree", filter="data")
    return dest / "tree" / prefix


def test_the_vendored_0_6_1_release_is_the_release_commit():
    """Review 1 R36: the vendored copy is there and unchanged (its pinned SHA-256, everywhere) and, where the
    repository holds the release commit, file for file the commit's backend/app."""
    assert _vendored_061_ok(), "the vendored 0.6.1 release is missing: %s" % VENDORED_061
    git = shutil.which("git")
    top = subprocess.run([git, "-C", str(BACKEND), "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True) if git else None
    if top is None or top.returncode != 0:
        return  # a copied tree: the pin above is the check (it was taken from the commit's git archive)
    prefix = os.path.relpath(BACKEND / "app", top.stdout.strip())
    done = subprocess.run([git, "-C", top.stdout.strip(), "archive", "--format=tar", RELEASE_061, prefix],
                          capture_output=True)
    if done.returncode != 0:
        return  # a shallow clone without the release commit: the pin above is the check
    import gzip
    import io
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as a, tarfile.open(fileobj=io.BytesIO(
            gzip.decompress(VENDORED_061.read_bytes()))) as b:
        def files(t: tarfile.TarFile) -> dict[str, bytes]:
            out = {}
            for m in t.getmembers():
                if m.isfile():
                    f = t.extractfile(m)
                    assert f is not None
                    out[m.name.split("/backend/app/", 1)[-1]] = f.read()
            return out
        assert files(a) == files(b)


def _legacy(backend: Path, url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1),
           "PYTHONPATH": str(backend)}
    script = backend.parent / "legacy_061.py"
    script.write_text(LEGACY_SCRIPT)
    done = subprocess.run([sys.executable, str(script), *args], cwd=str(backend), env=env, capture_output=True,
                          text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr


def _backup(fx: dict[str, str]) -> dict[str, list[int]]:
    db_ = fx["db"]
    ids = {}
    bj.add_session(db_, "up_tail", started=T)
    ids["tail"] = [bj.add_row(db_, "up_tail", role, content, T + n + 1) for n, (role, content) in enumerate([
        ("user", "Please summarise the vendor situation."), ("assistant", "Sure, here it is."),
        ("user", "Give me the headline number."), ("assistant", secret(MARKS["tail"])),
        ("user", "And the other vendor?"), ("assistant", "No change there."),
        ("user", "Thanks."), ("assistant", "You are welcome.")])]
    bj.add_session(db_, "up_repeat", started=T + 10)
    ids["repeat"] = [bj.add_row(db_, "up_repeat", role, content, T + 10 + n + 1) for n, (role, content) in enumerate([
        ("user", "Can you check the contract?"), ("assistant", "Yes."), ("user", secret(MARKS["repeat"])),
        ("user", secret(MARKS["repeat"])), ("assistant", "Noted.")])]
    bj.add_session(db_, "up_uid", started=T + 20)
    ids["uid"] = [bj.add_row(db_, "up_uid", role, content, T + 20 + n + 1, uid=False) for n, (role, content) in
                  enumerate([("user", "What is it worth?"), ("assistant", secret(MARKS["uid"])), ("user", "Ok.")])]
    return ids


def _later(fx: dict[str, str], ids: dict[str, list[int]]) -> None:
    """The later backup: one in-place compaction (A-tail), the original of a repeat edited (B), uids backfilled
    (C1)."""
    bj.hermes_compact(fx["db"], "up_tail", head_n=1, tail_n=6, ts=T + 100)
    bj.change(fx["db"], "UPDATE messages SET content = ? WHERE id = ?", secret(MARKS["repeat"]) + " (edited)",
              ids["repeat"][2])
    bj.backfill_uids(fx["db"], "up_uid")


@pytest.fixture
async def legacy_061(scratch, tmp_path):
    """A database the real 0.6.1 left with the A-tail, B and C1 leaks (and a promoted copy, a quote and a session
    summary of the A-tail copy's text, as 0.6.1 readers made them)."""
    backend = _release_061(tmp_path / "release")
    env = {**os.environ, "DATABASE_URL": scratch.replace("postgresql://", "postgresql+asyncpg://", 1)}
    done = subprocess.run([sys.executable, "-m", "app.cli", "migrate"], cwd=str(backend), env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    fx = bj.mini(tmp_path / "fx")
    ids = _backup(fx)
    _legacy(backend, scratch, "run", fx["db"], fx["profile"], fx["content"])
    for mark in MARKS.values():
        _legacy(backend, scratch, "withhold", mark)
    _later(fx, ids)
    _legacy(backend, scratch, "run", fx["db"], fx["profile"], fx["content"])
    conn = await asyncpg.connect(scratch)
    try:
        leaked = {mark: await conn.fetchval("SELECT count(*) FROM archive_messages WHERE NOT withheld AND content "
                                            "LIKE '%' || $1 || '%'", mark) for mark in MARKS.values()}
        assert all(leaked.values()), leaked  # 0.6.1's own leaks: the precondition of this test
        copy = await conn.fetchrow("SELECT id, session_id, source_key, position FROM archive_messages WHERE NOT "
                                   "withheld AND content LIKE '%' || $1 || '%' LIMIT 1", MARKS["tail"])
        sid = await conn.fetchval("SELECT source_session_id FROM archive_sessions WHERE id = $1", copy["session_id"])
        item = await conn.fetchval(
            "INSERT INTO memory_items (kind, title, body, status, origin, flags, source_ref, content_hash) VALUES "
            "('project', 'Copy of the contract', $1, 'approved', 'promoted', $2::jsonb, $3, 'h') RETURNING id",
            secret(MARKS["tail"]), json.dumps({"message_id": copy["id"], "archive_session_id": copy["session_id"],
                                               "message_source_key": copy["source_key"]}),
            "JARVIS session %s · message %d" % (sid, copy["position"]))
        task = await conn.fetchval(
            "INSERT INTO tasks (agent, capability, params, status, risk, result) VALUES ('supervisor', 'recall', "
            "'{}', 'succeeded', 'read', $1::jsonb) RETURNING id",
            json.dumps({"summary": "history", "data": {"local_only": True, "answer": [
                "### Decisions", '- "%s" (source: up_tail · message 3)' % secret(MARKS["tail"])],
                "sources": [None, {"message": copy["id"]}]}}))
        summaries = await conn.fetchval("SELECT count(*) FROM archive_sessions WHERE summary LIKE '%zqupg%'")
    finally:
        await conn.close()
    return {"url": scratch, "fx": fx, "backend": backend, "item": item, "task": task, "summaries": summaries}


async def test_real_0_6_1_importer_data_is_marked_legacy_by_the_migration(legacy_061):
    """0.6.2 plan 1.7: what the real 0.6.1 importer left (two runs by importer 1.0.1, three withholds) is legacy Brain
    data: the migration marks it (the legacy lock hides it and refuses imports until the legacy reset re-imports it
    from the JARVIS backup, plan 8) instead of re-deriving 0.6.1's leaks by text; it keeps no plain fingerprint of a
    withheld row, and the rules that need no re-derivation (key, exact JARVIS row) leave nothing readable."""
    assert await _migrate(legacy_061["url"]) == ["0008_brain_withhold_identity"]
    conn = await asyncpg.connect(legacy_061["url"])
    try:  # the migration alone: before any process serves a request
        legacy = json.loads(await conn.fetchval(
            "SELECT detail FROM brain_maintenance WHERE name = 'legacy_brain_data'"))
        assert legacy["runs_by_an_older_importer"] == 2 and legacy["withholds_before_0008"] == len(MARKS)
        assert await conn.fetchval("SELECT count(*) FROM archive_messages WHERE withheld AND source_fp NOT LIKE "
                                   "'w1:%' AND source_fp <> ''") == 0
        pending = json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))
        assert pending["readable_by_key"] == pending["readable_by_jarvis_row"] == 0 and pending["repair_done"]
        assert pending["legacy_lock"] is True
    finally:
        await conn.close()
