"""Round 3 fix A (groups-paths A, HIGH) against the vendored Hermes's own reading of lineage (rebuild R7).

hermes_lineage_driver.py writes a grid of shapes with Hermes's SessionDB (a parent ended in every way Hermes ends one,
with and without its own fork marker; a child whose fork marker names its parent, the parent's own copied marker, or
another conversation; with and without _compressed_from; children with no parent) and records Hermes's fork test,
SessionDB._is_explicit_fork_child_row(include_reset=True). app.brain.source.Backup then reads the same state.db.

- Hermes reads a child as no fork (an inherited marker, copied onto a rotation by compression): the Command Center
  never reads it as a down-only link (delegated or a markerless child); it links it both ways (continuation, branch,
  reset), or marks two parents (conflict: the group reads it as 'uncertain', held).
- Hermes reads a child as a fork (its marker names its parent, or it has no parent): the Command Center reads the
  kind that marker names.

HERMES_SRC names the vendored Hermes checkout; without it the test is skipped, and REQUIRE_HERMES_SRC=1 makes that a
failure. These checks show the listed shapes on this grid, not that no lineage is misread. Synthetic data only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.brain import source

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("HERMES_SRC", "")
DOWN_ONLY = {"delegated", ""}  # groups.DOWN_ONLY: 'delegated', and a markerless child ('' lineage, joins as 'child')
KIND = {"_branched_from": "branch", "_delegate_from": "delegated", "_reset_from": "reset"}


@pytest.fixture(scope="module")
def grid(tmp_path_factory) -> dict[str, Any]:
    if not SRC or not os.path.isdir(SRC):
        if os.environ.get("REQUIRE_HERMES_SRC") == "1":
            pytest.fail("HERMES_SRC is not set to the vendored Hermes source (REQUIRE_HERMES_SRC=1)")
        pytest.skip("HERMES_SRC not set: the lineage tests need the vendored Hermes source")
    out = tmp_path_factory.mktemp("hermes_lineage") / "grid.json"
    done = subprocess.run([sys.executable, "-I", os.path.join(HERE, "hermes_lineage_driver.py"), SRC, str(out)],
                          capture_output=True, text=True, timeout=600, cwd=str(out.parent))
    assert done.returncode == 0, done.stderr[-4000:]
    data = json.loads(out.read_text())
    assert "error" not in data, "the real Hermes function failed: %s\n%s" % (data["error"], data.get("trace", ""))
    b = source.Backup(data["db"], ZoneInfo("UTC"), {}, {})
    try:
        data["read"] = {s.id: s for s in b.sessions()}
    finally:
        b.close()
    return data


def _show(shape: dict[str, Any], s: source.SourceSession) -> str:
    return "%s: Hermes fork=%s, read %r (conflict %s)" % (
        json.dumps({k: shape[k] for k in ("parent_end", "parent_own", "marker", "target", "compressed")}),
        shape["hermes_fork"], s.lineage, getattr(s, "conflict", None))


def test_hermes_lineage_the_grid_covers_both_readings(grid):
    shapes = grid["shapes"]
    assert {sh["hermes_fork"] for sh in shapes} == {True, False}
    assert all(sh["child"] in grid["read"] for sh in shapes)
    # the shape of round 3 fix A is in the grid: a rotation (parent ended by a compression) that inherited its
    # parent's own _delegate_from naming an older conversation, which Hermes reads as no fork
    assert any(sh["parent_end"] == "compression" and sh["parent_own"] == {"_delegate_from": "G"}
               and sh["marker"] == "_delegate_from" and sh["target"] == "G" and not sh["hermes_fork"]
               for sh in shapes)


def test_hermes_lineage_a_child_hermes_reads_as_no_fork_is_never_a_down_only_link(grid):
    wrong = [_show(sh, grid["read"][sh["child"]]) for sh in grid["shapes"] if not sh["hermes_fork"]
             and grid["read"][sh["child"]].lineage in DOWN_ONLY and not getattr(grid["read"][sh["child"]],
                                                                                "conflict", False)]
    assert wrong == []


def test_hermes_lineage_an_inherited_delegate_marker_reads_as_a_continuation(grid):
    """The fix A shape itself: the parent's own _delegate_from copied onto its rotation is a continuation."""
    found = [grid["read"][sh["child"]] for sh in grid["shapes"]
             if sh["parent_end"] == "compression" and sh["parent_own"] == {"_delegate_from": "G"}
             and sh["marker"] == "_delegate_from" and sh["target"] == "G"]
    assert found and {s.lineage for s in found} == {"continuation"}
    assert not any(getattr(s, "conflict", False) for s in found)


def test_hermes_lineage_a_child_hermes_reads_as_a_fork_keeps_the_kind_its_marker_names(grid):
    wrong = [_show(sh, grid["read"][sh["child"]]) for sh in grid["shapes"] if sh["hermes_fork"]
             and grid["read"][sh["child"]].lineage != KIND[sh["marker"]]]
    assert wrong == []
