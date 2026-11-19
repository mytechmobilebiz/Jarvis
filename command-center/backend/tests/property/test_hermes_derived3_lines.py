"""Round 3 derived N1 / N4 on the lines the vendored Hermes itself writes (rebuild S5: a stand-in for the round-3
test_hermes_derived3_lines.py (6 tests) and hermes_lean_gen.py, lost with the container; their contents are not in
the sources, so these are rebuilt from the round-3 descriptions of N1 and N4, plus one test of Hermes's persisted-output
previews, the derived skeptic's survivor shapes).

hermes_lean_gen.py calls Hermes's agent.context_compressor._summarize_tool_result on tool calls whose argument is a
withheld-looking text W (apostrophes, double quotes, line breaks, non-ASCII, backslashes, a long text): the quoted
f-string summaries (N1), the code and command previews and the generic summary that cuts str(value) at 40 characters
(a repr cut inside an escape: N4). The Command Center (app.brain.identity) must read every such line of W as a
carrier and a group row holding one as never 'none', show every relation the oracle demands of it, and hold nothing
for the same summaries of unrelated arguments.

HERMES_SRC names the vendored Hermes checkout; without it the tests are skipped, and REQUIRE_HERMES_SRC=1 makes that
a failure. These checks show the listed shapes, not that no derived form escapes. Synthetic data only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest

from app.brain import identity as I

from . import oracle as o

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("HERMES_SRC", "")
QUOTED = ("vision_question", "web_search_query", "delegate_goal", "search_pattern")
PREVIEWS = ("terminal_echo", "code_repr", "code_ascii")
GENERIC = ("generic_text", "generic_dict", "generic_list", "generic_failed")
PERSIST = ("persist_json_escape", "persist_repr_escape")  # every W; persist_line_cut: a W with a line break only
ALL = QUOTED + PREVIEWS + GENERIC + PERSIST
TEXT = set(I.TEXT_RELATIONS)


@pytest.fixture(scope="module")
def gen(tmp_path_factory) -> dict[str, Any]:
    if not SRC or not os.path.isdir(SRC):
        if os.environ.get("REQUIRE_HERMES_SRC") == "1":
            pytest.fail("HERMES_SRC is not set to the vendored Hermes source (REQUIRE_HERMES_SRC=1)")
        pytest.skip("HERMES_SRC not set: these tests need the vendored Hermes source")
    out = tmp_path_factory.mktemp("hermes_lean") / "lines.json"
    done = subprocess.run([sys.executable, "-I", os.path.join(HERE, "hermes_lean_gen.py"), SRC, str(out)],
                          capture_output=True, text=True, timeout=600, cwd=str(out.parent))
    assert done.returncode == 0, done.stderr[-4000:]
    data = json.loads(out.read_text())
    assert "error" not in data, "the real Hermes function failed: %s\n%s" % (data["error"], data.get("trace", ""))
    shapes = {r["shape"] for r in data["lines"]}
    assert set(ALL) | {"persist_line_cut"} <= shapes, shapes
    return data


def of_w(gen: dict[str, Any], shapes: tuple[str, ...]) -> list[dict[str, Any]]:
    found = [r for r in gen["lines"] if r["kind"] == "w" and r["shape"] in shapes]
    assert len(found) == len(gen["ws"]) * len(shapes)
    return found


def show(r: dict[str, Any], got: Any) -> str:
    return "%s/%s: %r -> %s" % (r["w"], r["shape"], r["line"][:120], got)


def test_n1_every_quoted_summary_hermes_writes_of_w_is_a_carrier(gen):
    """N1: vision_analyze ('question'[:50], quoted across apostrophes and line breaks), web_search query='...',
    delegate_task '...' (cut with '...') and search_files '...': each line is a carrier of W."""
    for r in of_w(gen, QUOTED):
        got = I.carrier_relations([r["line"]], r["text"])
        assert got & TEXT, show(r, got)


def test_n4_every_code_and_command_preview_hermes_writes_of_w_is_a_cut(gen):
    """N4: execute_code's `code[:60]...` of a repr or an ascii() literal (cut inside or next to an escape) and the
    terminal's `command[:77]...` of a JSON-quoted echo: each reads as a cut of W."""
    for r in of_w(gen, PREVIEWS):
        got = I.carrier_relations([r["line"]], r["text"])
        assert I.CUT in got, show(r, got)


def test_n4_the_generic_summary_cutting_str_of_an_argument_is_a_cut(gen):
    """N4: the summary of any other tool cuts str(value) at 40 characters: W itself, or the repr of a dict or list
    holding W (its escapes, a doubled backslash, a quote), cut there; with a FAILED suffix too."""
    for r in of_w(gen, GENERIC):
        got = I.carrier_relations([r["line"]], r["text"])
        assert I.CUT in got, show(r, got)


def test_hermes_lines_a_persisted_output_preview_cut_inside_w_is_a_cut(gen):
    """Beyond the six round-3 tests (rebuild S5): the <persisted-output> preview Hermes's tool_result_storage writes for
    an oversized result, cut at its preview size inside one of W's escapes (a one-line ensure_ascii JSON or ascii()
    repr result) or at the line break inside W (a multi-line result: the derived skeptic's cut_per_line shape): a cut
    of W, never readable as 'none'."""
    found = of_w(gen, PERSIST) + [r for r in gen["lines"] if r["kind"] == "w" and r["shape"] == "persist_line_cut"]
    assert sum(1 for r in found if r["shape"] == "persist_line_cut") == sum(1 for w in gen["ws"].values() if "\n" in w)
    for r in found:
        got = I.carrier_relations([r["line"]], r["text"])
        assert I.CUT in got, show(r, got)
        assert I.NO_RELATION not in I.group_relations([r["line"]], r["text"], all_in_hand=True), show(r, got)


def test_hermes_lines_the_product_shows_every_relation_the_oracle_demands(gen):
    """The oracle's demanded relations of each line (outside every group and in a group) are among the product's
    (the oracle only admits a quote span's relation, never demands it: round-3 oracle change 1)."""
    for r in of_w(gen, ALL):
        line, w = r["line"], r["text"]
        dem = o.relations_c([line], w, group=False, carrier=True)[0] - {"none"}
        got = I.carrier_relations([line], w)
        assert dem <= got, show(r, (sorted(dem), sorted(got)))
        dem_g = o.relations_c([line], w, group=True)[0] - {"none"}
        got_g = set(I.group_relations([line], w, all_in_hand=True))
        assert dem_g <= got_g, show(r, (sorted(dem_g), sorted(got_g)))


def test_hermes_lines_a_group_row_holding_a_hermes_line_of_w_is_never_none(gen):
    """A row of a withheld conversation group holding one of these lines is never 'none' (never one click), whether W
    is the group's own withheld text or another one in hand."""
    other = "Bitte buche den Zug nach Hamburg für Dienstag früh."
    for r in of_w(gen, ALL):
        for got in (I.group_relations([r["line"]], r["text"], all_in_hand=True),
                    I.group_relations([r["line"]], other, [r["text"]], all_in_hand=True)):
            assert I.NO_RELATION not in got and set(got) & TEXT, show(r, got)


def test_hermes_lines_control_summaries_of_unrelated_arguments_hold_nothing(gen):
    """The same summaries of unrelated arguments relate to no W: no carrier, nothing the oracle demands, and 'none'
    in a group with every withheld text in hand."""
    controls = [r for r in gen["lines"] if r["kind"] == "control"]
    assert len(controls) == len(gen["controls"]) * len(ALL) + sum(1 for c in gen["controls"].values() if "\n" in c)
    for r in controls:
        for w in gen["ws"].values():
            got = I.carrier_relations([r["line"]], w)
            assert not got, show(r, got)
            assert not (o.relations_c([r["line"]], w, group=False, carrier=True)[0] - {"none"}), show(r, w[:20])
            assert I.group_relations([r["line"]], w, all_in_hand=True) == (I.NO_RELATION,), show(r, w[:20])
