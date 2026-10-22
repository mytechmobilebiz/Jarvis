"""Message keys are a contract (0.6.2 plan 2.3): a decision is replayed on the live installation by the keys and
JARVIS rows the test copy derived from the same backup. A vendored state.db (tests/fixtures/golden_state.db, made by
tests/fixtures/make_golden_state.py, synthetic) and the keys, lineage and markers it must give
(tests/fixtures/golden_keys.json). A change to key derivation fails here first; change the contract on purpose only.
No database."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
sys.path.insert(0, str(FIXTURES))

import make_golden_state  # noqa: E402

GOLDEN = json.loads((FIXTURES / "golden_keys.json").read_text(encoding="utf-8"))


def test_the_vendored_state_db_gives_the_golden_keys(tmp_path):
    db = tmp_path / "state.db"
    shutil.copyfile(FIXTURES / "golden_state.db", db)  # the backup is opened read-only; a copy keeps the fixture
    assert make_golden_state.derive(str(db)) == GOLDEN


def test_the_generator_still_makes_that_backup(tmp_path):
    db = tmp_path / "state.db"
    make_golden_state.build(str(db))
    assert make_golden_state.derive(str(db)) == GOLDEN


@pytest.mark.parametrize("session, lineage, parent", [
    ("rot", "continuation", "root-deleted"),  # _compressed_from, the parent deleted (parent_session_id NULL)
    ("gwb", "branch", "root"),
    ("reset", "reset", "root"),               # a gateway reset copies no rows: never a family link
    ("deleg", "delegated", "root"),
    ("cont", "continuation", "root"),        # an older Hermes: the parent's end_reason only
])
def test_lineage_and_markers(session, lineage, parent):
    got = GOLDEN["sessions"][session]
    assert (got["lineage"], got["parent"]) == (lineage, parent)


def test_key_rules_the_golden_backup_pins():
    by_row = {(m["session"], m["row"]): m for m in GOLDEN["messages"]}
    # a uid used by several rows: the row with the smallest id keeps the uid key, the others a row-form key
    assert by_row[("root", "2")]["key"].startswith("jarvis:message:uid:")
    assert by_row[("root", "7")]["key"] == "jarvis:message:root:7"
    assert by_row[("rot", "10")]["key"] == "jarvis:message:rot:10"  # a rotation's copy keeps its uid and time
    assert by_row[("rot", "10")]["at"] == by_row[("root", "1")]["at"]
    # no uid (NULL or ''): a row-form key; a session id with colons is split at the last one
    assert by_row[("root", "5")]["key"] == "jarvis:message:root:5"
    assert by_row[("root", "6")]["key"] == "jarvis:message:root:6"
    assert by_row[("slack:C123:thread:9", "17")]["key"] == "jarvis:message:slack:C123:thread:9:17"
    # a millisecond time is read as such, and the backup's newest time compares times, not raw numbers
    assert by_row[("root", "9")]["at"] == "2026-07-01T00:00:08+00:00"
    assert GOLDEN["newest_at"] == "2026-07-01T00:11:40+00:00"
    assert by_row[("root", "8")]["absorbed"] == ["60200000000000000000000000000008", "60200000000000000000000000000009"]
