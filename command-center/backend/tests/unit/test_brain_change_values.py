"""Every memory_item_versions.change value the code writes is one the migrations allow (0.6.0's withhold wrote
'quarantined', which the CHECK of 0006_brain.sql refused, so withholding a message with a promoted copy failed); and
a database error is described by its classes and SQLSTATE, never by its text (PostgreSQL's DETAIL holds the row)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
from sqlalchemy.exc import DataError, IntegrityError

from app import db
from app.brain import store

APP = Path(__file__).resolve().parents[2] / "app"
CHECK = re.compile(r"CHECK\s*\(\s*change\s+IN\s*\(([^)]*)\)", re.I | re.S)
WRITTEN = (re.compile(r"_snapshot\(\s*[\w.]+\s*,\s*\"([a-z_]+)\""), re.compile(r"\bchange=\"([a-z_]+)\""),
           re.compile(r"\bchange: str = \"([a-z_]+)\""))


def allowed_by(path: Path) -> list[str]:
    found = CHECK.findall(path.read_text(encoding="utf-8"))
    return re.findall(r"'([^']*)'", found[-1]) if found else []


def latest_check() -> set[str]:
    values: list[str] = []
    for path in db.migration_files():  # in the order the runner applies them: the last CHECK is the one in force
        values = allowed_by(path) or values
    return set(values)


def test_every_version_change_the_code_writes_is_allowed_by_the_migrations():
    written = {change for _, change in store.REVIEW.values()}
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in WRITTEN:
            written.update(pattern.findall(text))
    assert {"created", "imported", "edited", "redacted", "reverted", "quarantined", "approved"} <= written
    allowed = latest_check()
    assert written <= allowed, sorted(written - allowed)
    # 0007 only adds values: a 0.6.0 row, and 0.6.0's code, still fit.
    assert set(allowed_by(db.MIGRATIONS_DIR / "0006_brain.sql")) < allowed


class _Refused(Exception):
    sqlstate = "23514"


async def test_database_errors_are_described_without_row_values(caplog):
    from app.main import create_app

    marker = "ROWVALUE" + "MARKER7731"
    detail = "new row violates check constraint\nDETAIL:  Failing row contains (%s)." % marker
    refused = IntegrityError("INSERT INTO memory_item_versions (title) VALUES ($1)", {}, _Refused(detail))
    assert marker in str(refused)  # what must never be logged or answered
    described = db.describe_error(refused)
    assert described == "IntegrityError (_Refused, SQLSTATE 23514)"

    app = create_app(run_bootstrap=False)

    async def conflict() -> None:
        raise refused

    async def broken() -> None:
        raise DataError("UPDATE archive_messages SET content = $1", {}, Exception("invalid input: %s" % marker))

    app.add_api_route("/api/test/conflict", conflict)
    app.add_api_route("/api/test/broken", broken)
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cc.test") as client:
        r = await client.get("/api/test/conflict", params={"q": marker})
        assert r.status_code == 409 and r.json() == {"detail": "the database refused this change; nothing was saved"}
        r = await client.get("/api/test/broken")
        assert r.status_code == 500 and marker not in r.text
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))  # not the test client's
    assert marker not in logged
    assert "GET /api/test/conflict: IntegrityError (_Refused, SQLSTATE 23514)" in logged
    assert "GET /api/test/broken: DataError (Exception, SQLSTATE unknown)" in logged
