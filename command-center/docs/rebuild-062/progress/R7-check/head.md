# 0.6.2 option C rebuild: reconstruction check (R7)

Status: UNREVIEWED, NOT A RELEASE, NO-GO. These runs show the listed tests failing on i22 and passing on the rebuilt
tree for the inputs they use; they do not show that no leak remains.

## What was checked
- **Items:** every fixed item the sources list in the round-2 report (sections 2 and 4), the round-3 report
  (sections 2 and 4), the round-3 consolidation (closing tests, oracle changes 1 and 2, seeds), and every row of the
  round-3 mutation closure matrix that survives in `sources/21-round3-consolidation-part1.md` (lines 156-230: 75 rows,
  all CAUGHT or COMPENSATED). The other 83 of the matrix's 158 rows were cut off in the surviving log and are not in
  the sources, so they cannot be checked here.
- **Trees:**
  - **i22**: a git worktree of `origin/wip/brain-062-unreviewed` at 28e9c3d (`/home/user/rebuild/r7-i22`, removed
    afterwards) with the rebuilt `tests/` copied over it: i22's product, migrations and app, the rebuilt tests and
    test helpers.
  - **i22 oracle**: the oracle self-tests (`tests/property/test_oracle_*.py` except `test_oracle_properties.py`) ran on
    a copy of i22 with its own `tests/property` helpers and only the self-test files (and `selftest_kit.py`) added, so
    the oracle under test is i22's.
  - **HEAD**: the rebuilt branch `wip/brain-062-optionC` (code at d93369a plus the R7 tests; no product file changed in
    R7).
- **How:** `progress/R7-check/check.py` (items in `progress/R7-check/items.py`) runs one pytest per test file and tree
  with `-p no:randomly`, own databases, `HERMES_SRC` and `REQUIRE_HERMES_SRC=1`, and reads each test's outcome from
  JUnit XML. An item counts as checked when every test named for it, controls aside, fails on i22 and every one passes
  on HEAD. Raw results: `progress/R7-check/results-head.json`, `results-i22.json`, `results-i22-oracle.json`.
- **"fails at import":** the test file cannot be imported on i22 because the name it needs does not exist there (for
  example `app.brain.groups`, `identity.LATER_WITHHOLD`). On i22 that behaviour does not exist at all. The test does not
  reach its assertions there, so this is weaker evidence than an assertion failure. Of the 116 test functions named,
  38 fail at import on i22; 78 run there, and every case of 77 of them fails (the 78th is `test_hermes_lineage.py`,
  3 of 4 failing). On HEAD all 116 functions (217 test cases with their parameters) pass.

## Result
- **119 items and matrix rows have tests that fail on i22 and pass on HEAD.** No named test fails on HEAD. On i22 the
  only passing case is the one `test_hermes_lineage.py` check above; the five marked controls fail there too, because
  their files need names i22 lacks (they are not counted either way).
- **4 are checked by a lane, not per test:** the recorded seeds (property lane, HEAD only; the round-2/3 seed numbers do
  not reproduce, R5) and the dashboard (Vitest, run on both trees).
- **3 have no test**, listed below with the other gaps.

## Tests added in R7 (each fails on i22 and passes on HEAD; mutant evidence on a scratch copy)
| test | stands for | i22 | HEAD | mutant |
|---|---|---|---|---|
| `integration/test_brain_groups_d1_062.py::test_skc_a_markerless_legacy_gateway_reset_joins_the_group_and_is_held` | round-1 groups-paths P1 case A, `gd1:child_edge_off` (its round-2 name) | fails at import (no `groups`) | passes | `gd1:child_edge_off`: "1 failed" |
| `integration/test_brain_releases_r2_062.py::test_rel3_hold_group_rows_compares_a_released_row_with_other_withheld_texts` | `rel3:hgr_foreign_off` (its round-3 name) | fails (assertion) | passes | `rel3:hgr_foreign_off` alone: "1 failed, 1 passed" (the skC test passes under it, as in R4; the row was COMPENSATED in R4, it is now caught on its own) |
| `property/test_hermes_lineage.py` (4 tests) + `hermes_lineage_driver.py` | round-3 fix A against Hermes's own `_is_explicit_fork_child_row` on a 366-shape grid (the round-3 file was lost) | "3 failed, 1 passed" (the passing one is the "fork keeps its kind" check, which i22 also meets) | "4 passed" | fix A reverted to presence-only markers: "2 failed, 2 passed" |

Shapes chosen for these tests: RECON-50. No existing test or assertion was changed (no D- entry).
