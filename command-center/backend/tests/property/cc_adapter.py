"""How the oracle drives the Command Center: every entry point the 0.6.2 plan names, bound in one place.

The oracle team wrote these bindings from the plan's contract (RECOMMENDATION.md sections 3-5) and from the interfaces
the existing tests already use (importer.run, rollback_run, decisions_export, decisions_apply, the withhold and holds
API), not from the rebuilt implementation. Where the plan names an interface without its exact shape, the binding
looks for it the way an operator would find it:

- Settle (step 4.1): ``brain-import settle --backup <file>`` through the real CLI in a subprocess (``python -m app.cli``
  with this test's DATABASE_URL), with ``--yes`` added when the CLI asks for it.
- Import with a decisions file (step 3.3): ``importer.run(..., decisions=<file>)`` when run takes it, else
  ``brain-import run ... --decisions <file>``.
- "Release this conversation's rows with no relation" and "Stop importing this conversation" (step 9.3): the API
  route whose path names it, found in the application's route table.
- A typed confirmation (step 4.6): when a release answers 4xx asking for one, the body the answer names.

A missing entry point raises Binding (the test fails with BINDING: ..., never silently passes). ORACLE_LENIENT=1 lets
the harness record the missing step and go on checking what it can (for use while the rebuild is in progress)."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from typing import Any

from sqlalchemy import text

from app.brain import importer as imp
from app.db import session_scope

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LENIENT = os.environ.get("ORACLE_LENIENT") == "1"
ADMIN_ACTOR = os.environ.get("ADMIN_EMAIL", "admin@test.local")
WITHHELD_BY_ADMIN = "withheld by an admin"


class Binding(Exception):
    """The Command Center has no entry point the plan names (or it refused in a way the plan does not allow)."""


def cli(*args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = BACKEND + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "app.cli", "brain-import", *args], capture_output=True, text=True,
                          timeout=timeout, cwd=BACKEND, env=env)


def _usage_error(done: subprocess.CompletedProcess) -> bool:
    err = (done.stderr or "") + (done.stdout or "")
    return done.returncode == 2 and bool(re.search(r"usage:|unrecognized arguments|invalid choice|required:", err))


def paths(db: str, profile: str, content: str) -> Any:
    return imp.SourcePaths(db=db, profile=profile, content=content)


ALL = imp.Selection(all=True)


async def import_backup(db: str, profile: str, content: str, *, decisions: str | None = None,
                        selection: Any = None) -> dict[str, Any]:
    sel = selection or ALL
    if decisions is None:
        return await imp.run(paths(db, profile, content), sel)
    if "decisions" in inspect.signature(imp.run).parameters:  # decisions that needed an admin still need one
        return await imp.run(paths(db, profile, content), sel, decisions=decisions, actor=ADMIN_ACTOR)
    done = cli("run", "--db", db, "--profile", profile, "--content", content, "--all", "--decisions", decisions,
               "--actor", ADMIN_ACTOR)
    if _usage_error(done):
        raise Binding("import with a decisions file (plan 3.3: jarvis-import -Mode Import -Decisions): neither "
                      "importer.run(decisions=...) nor 'brain-import run --decisions' exists: %s" % done.stderr[-400:])
    if done.returncode != 0:
        raise Binding("brain-import run --decisions failed (exit %d): %s" % (done.returncode,
                                                                            (done.stderr or done.stdout)[-800:]))
    async with session_scope() as s:
        run_id = await s.scalar(text("SELECT max(id) FROM import_runs"))
    return {"run_id": run_id, "cli": done.stdout[-400:]}


def settle(backup: str, profile: str, content: str) -> str:
    """brain-import settle --backup <file> (plan 4.1)."""
    attempts = (["settle", "--backup", backup], ["settle", "--backup", backup, "--yes"],
                ["settle", "--backup", backup, "--profile", profile, "--content", content, "--yes"])
    last = None
    for args in attempts:
        done = cli(*args)
        last = done
        if done.returncode == 0:
            return done.stdout
        if not _usage_error(done):
            break
    raise Binding("settle (plan 4.1: brain-import settle --backup <file>) failed or does not exist (exit %s): %s"
                  % (last.returncode if last else "?", ((last.stderr or "") + (last.stdout or ""))[-800:]))


async def export_decisions(out: str, *, removals_only: bool = False) -> dict[str, Any]:
    return await imp.decisions_export(out, removals_only=removals_only)


async def apply_removals_only(path: str) -> dict[str, Any]:
    return await imp.decisions_apply(path, None, actor=None, apply=True, removals_only=True)


async def rollback(run_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        out = await imp.rollback_run(s, run_id, ADMIN_ACTOR)
    return out


async def wipe() -> None:
    """Another installation in the same test database: every Brain table emptied (users and migrations kept)."""
    async with session_scope() as s:
        tables = [r[0] for r in (await s.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND (tablename LIKE 'archive\\_%' OR "
            "tablename LIKE 'import\\_%' OR tablename LIKE 'brain\\_%' OR tablename LIKE 'memory\\_%' OR "
            "tablename = 'workflow_drafts')"))).all()]
        await s.execute(text("TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(tables)))
        await s.commit()


# --- the API (as the admin) ---------------------------------------------------------------------------------------


class Api:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.token: str | None = None

    async def login(self) -> None:
        from app.config import get_settings
        from app.security import CSRF_HEADER, CSRF_VALUE

        from ..conftest import ADMIN

        self.client.cookies.clear()
        r = await self.client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
        assert r.status_code == 200, r.text
        self.token = r.cookies.get(get_settings().session_cookie_name)
        self.client.headers[CSRF_HEADER] = CSRF_VALUE

    async def call(self, method: str, url: str, **kw: Any) -> Any:
        from app.config import get_settings

        if self.token is None:
            await self.login()
        self.client.cookies.clear()
        self.client.cookies.set(get_settings().session_cookie_name, self.token)
        r = await self.client.request(method, url, **kw)
        if r.status_code == 401:  # a restore took the sign-in away
            await self.login()
            self.client.cookies.set(get_settings().session_cookie_name, self.token)
            r = await self.client.request(method, url, **kw)
        return r

    async def withhold(self, message_id: int, **body: Any) -> Any:
        return await self.call("POST", "/api/brain/messages/%d/withhold" % message_id, json=body)

    async def holds(self) -> list[dict[str, Any]]:
        r = await self.call("GET", "/api/brain/holds")
        assert r.status_code == 200, r.text
        data = r.json()
        found = list(data.get("holds", []))
        for group in ("later", "new_in_a_later_backup", "found_when_finishing", "settle_holds"):
            for h in data.get(group, []) or []:
                if isinstance(h, dict) and h not in found:
                    found.append(h)
        return found

    async def keep(self, message_id: int) -> Any:
        return await self.call("POST", "/api/brain/holds/%d/keep" % message_id)

    async def release(self, message_id: int) -> Any:
        """A release, with the typed confirmation the answer asks for when the hold's relation needs one (4.6)."""
        url = "/api/brain/holds/%d/release" % message_id
        r = await self.call("POST", url)
        if r.status_code < 400:
            return r
        tried = [r]
        for body in confirmation_bodies(r.text):
            r = await self.call("POST", url, json=body)
            tried.append(r)
            if r.status_code < 400:
                return r
        return r

    def route(self, *words: str) -> str | None:
        app = getattr(getattr(self.client, "_transport", None), "app", None)
        for rt in getattr(app, "routes", []) or []:
            path = getattr(rt, "path", "")
            if "POST" in (getattr(rt, "methods", None) or set()) and all(w in path for w in words):
                return path
        # FastAPI 0.143 keeps included routers as _IncludedRouter objects with no path of their own: app.routes alone
        # never lists the API's routes, so the schema's paths are searched too (integrator, deviation 254: without
        # this B6 was never exercised)
        try:
            paths = app.openapi().get("paths", {}) if app is not None else {}
        except Exception:
            paths = {}
        for path, ops in paths.items():
            if "post" in ops and all(w in path for w in words):
                return path
        return None

    async def release_unrelated(self, session_id: int) -> Any | None:
        """'Release this conversation's rows with no relation' (9.3), or None when the API has no such route."""
        path = self.route("unrelated") or self.route("no-relation") or self.route("no_relation")
        if path is None:
            return None
        url = re.sub(r"\{[^}]+\}", str(session_id), path)
        r = await self.call("POST", url, json={"session": session_id, "session_id": session_id})
        if r.status_code >= 400 and r.status_code != 404:
            for body in confirmation_bodies(r.text):
                r2 = await self.call("POST", url, json={"session": session_id, "session_id": session_id, **body})
                if r2.status_code < 400:
                    return r2
        return r


def confirmation_bodies(answer: str) -> list[dict[str, Any]]:
    """The typed confirmation an answer asks for: a quoted word it names, under the field names it mentions."""
    words = re.findall(r"[\"'`“]([^\"'`”]{2,80})[\"'`”]", answer or "")
    fields = [f for f in re.findall(r"\b(confirm\w*|typed\w*)\b", answer or "", re.I)] or ["confirm", "confirmation",
                                                                                          "typed"]
    out: list[dict[str, Any]] = []
    for w in words + ["RELEASE", "release"]:
        out.append({f: w for f in dict.fromkeys(fields)})
    out.append({f: True for f in dict.fromkeys(fields)})
    return out


# --- what the Command Center stored -------------------------------------------------------------------------------


async def rows() -> list[dict[str, Any]]:
    async with session_scope() as s:
        found = (await s.execute(text(
            "SELECT to_jsonb(m) - 'search', s.source_session_id, s.title, s.flags FROM archive_messages m "
            "JOIN archive_sessions s ON s.id = m.session_id ORDER BY m.id"))).all()
    out = []
    for m, sid, title, sflags in found:
        m = dict(m)
        m["sid"] = sid
        m["session_title"] = title
        m["session_flags"] = sflags or {}
        out.append(m)
    return out


async def sessions() -> list[dict[str, Any]]:
    async with session_scope() as s:
        found = (await s.execute(text("SELECT to_jsonb(x) - 'search' FROM archive_sessions x ORDER BY id"))).scalars()
        return [dict(r) for r in found]


async def stones() -> list[dict[str, Any]]:
    async with session_scope() as s:
        found = (await s.execute(text("SELECT to_jsonb(t) FROM brain_tombstones t WHERE reason = :r ORDER BY 1"),
                                 {"r": WITHHELD_BY_ADMIN})).scalars()
        return [dict(r) for r in found]


async def table_exists(name: str) -> bool:
    async with session_scope() as s:
        return bool(await s.scalar(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": "public." + name}))


# --- C1 'at any moment': a test-only trigger records every version of every row an import writes -----------------

WATCH = """
CREATE TABLE IF NOT EXISTS oracle_watch (seq BIGSERIAL PRIMARY KEY, txid BIGINT, message_id BIGINT, session_id BIGINT,
    source_message_id TEXT, role TEXT, at TIMESTAMPTZ, source_uid TEXT, withheld BOOLEAN, content TEXT);
CREATE OR REPLACE FUNCTION oracle_watch_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO oracle_watch (txid, message_id, session_id, source_message_id, role, at, source_uid, withheld, content)
  VALUES (txid_current(), NEW.id, NEW.session_id, NEW.source_message_id, NEW.role, NEW.at,
          COALESCE(to_jsonb(NEW)->>'source_uid', ''), NEW.withheld, NEW.content);
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS oracle_watch_trg ON archive_messages;
CREATE TRIGGER oracle_watch_trg AFTER INSERT OR UPDATE ON archive_messages FOR EACH ROW
    EXECUTE FUNCTION oracle_watch_fn();
"""


async def watch_start() -> None:
    async with session_scope() as s:
        for stmt in _statements(WATCH):
            await s.execute(text(stmt))
        await s.execute(text("TRUNCATE oracle_watch"))
        await s.commit()


async def watch_stop() -> list[dict[str, Any]]:
    """Every row version that was the LAST one its transaction wrote (what a reader could see after the commit)."""
    async with session_scope() as s:
        found = (await s.execute(text(
            "SELECT DISTINCT ON (w.txid, w.message_id) w.message_id, s.source_session_id, w.source_message_id, w.role, "
            "w.at, w.source_uid, w.withheld, w.content FROM oracle_watch w LEFT JOIN archive_sessions s "
            "ON s.id = w.session_id ORDER BY w.txid, w.message_id, w.seq DESC"))).all()
        await s.execute(text("DROP TRIGGER IF EXISTS oracle_watch_trg ON archive_messages"))
        await s.execute(text("DROP TABLE IF EXISTS oracle_watch"))
        await s.execute(text("DROP FUNCTION IF EXISTS oracle_watch_fn()"))
        await s.commit()
    return [{"id": r[0], "sid": r[1], "source_message_id": r[2], "role": r[3],
             "at": r[4].isoformat() if r[4] is not None else None, "source_uid": r[5], "withheld": r[6],
             "content": r[7]} for r in found]


def _statements(sql: str) -> list[str]:
    out, buf, in_body = [], [], False
    for line in sql.strip().splitlines():
        buf.append(line)
        if "$$" in line:
            in_body = not in_body if line.count("$$") == 1 else in_body
        if line.rstrip().endswith(";") and not in_body:
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


def jdump(x: Any) -> str:
    return json.dumps(x, default=str, sort_keys=True)
