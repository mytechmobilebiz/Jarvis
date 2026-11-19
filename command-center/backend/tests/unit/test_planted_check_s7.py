"""Stabilisation S7 (D-53): the planted-value search of test_brain_import::test_the_full_import_of_the_planted_fixture.

That privacy test failed twice under load in S6 (the planted 6-digit PIN "found" once in archive_sessions, once in
memory_items). Every planted-fixture run's dump of those tables differs from another run's only in the times the
import writes, and a time's microsecond part is six digits: a time whose microseconds equal the PIN's digits made the
old text search report the PIN (shown on a crafted row in S7). The search now reads every key and value of every row
and leaves out only a whole value that is a clock time. These tests (no database) show that a planted value anywhere
else is still found. Synthetic fixture values only."""

from __future__ import annotations

import json

from tests import brain_jarvis as bj

VALUES = bj.never_stored()
PIN = VALUES["brain_pin"]


def found(row) -> set[str]:
    return bj.planted_in_json(row, VALUES)


def test_a_time_whose_microseconds_are_the_pin_digits_is_not_the_pin():
    for at in ("2026-11-19T21:48:03.%s+00:00" % PIN, "2026-11-19 21:48:03.%s" % PIN, "2026-11-19T21:48:03.%sZ" % PIN):
        assert found({"id": 1, "created_at": at, "flags": {"withheld_by_admin": {"at": at}}}) == set(), at


def test_the_pin_anywhere_else_is_found():
    rows = [
        {"content": "your PIN is %s" % PIN},  # a text value
        {"flags": {"note": "PIN %s" % PIN}},  # inside a JSON object
        {"labels": ["x", "code %s" % PIN]},  # inside a list
        {"flags": {PIN: True}},  # a key
        {"count": int(PIN)},  # a number
        {"report": json.dumps({"text": "your PIN is %s" % PIN})},  # JSON text inside a value
        {"summary": "sent at 2026-11-19T21:48:03.%s+00:00 to the team" % PIN},  # a time inside text is text
        {"created_at": "2026-11-19T21:48:03.123456+00:00", "title": PIN},  # beside a time
    ]
    for row in rows:
        assert "brain_pin" in found(row), row


def test_every_other_planted_value_is_still_found_in_text():
    for name, value in VALUES.items():
        assert name in found({"content": "before %s after" % value}), name
