00-grpA-trust.md
01-derived3.md
02-seeds-mut3.md
03-consolidate.md
04-skeptic_derived-copies.md
05-skeptic_groups-paths.md
06-skeptic_releases-oracle.md
consC3 (round 3 consolidation) is done. The final freeze is **f8d**. 0.6.2 stays **NO-GO**: these runs do not show that no leak remains.

No product file changed in this consolidation. All the product code is steps 1-3's. My changes are tests, two oracle changes and one spec sentence. Eight fresh seeds still fail on f8d. Two are owner-pending; one (13639) is a privacy gap that step 3's correct fix exposed and that needs a design change, so it is not fixed. Details follow.

This was a resumed run. A container restart at about 2026-11-11 00:04Z killed the recorded-seed run on f8d. I re-checked f8d against its manifest and `integ/cand8` with `diff -rq`, dropped my 146 `cc_test_rd_fxc3*` databases (1.4 GB), and resumed seed by seed.

## 1. Freezes
All files are under `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/`.

| Freeze | Files | Manifest sha256 | What it adds |
|---|---|---|---|
| `rvC3/f8` | 473 | 3c429cb43e65f86a8d89fed7c488170b51bf416240000f45e5f3a9bba469fad1 | cand8 = f7b + steps 1-3 |
| `rvC3/f8b` | 474 | bd217e1d54ebdb8322575001b27cd1d868c3a0e642de598d9a34df2ec7bd39f6 | first closing test |
| `rvC3/f8c` | 474 | ba5289f1932b01e490bde39ee9f3a6dadad9f5517480967e56370c152698de72 | closing test extended |
| `rvC3/f8d` | 476 | 4ee4844cd85e59e9600a75c031d3c2d590b3cb349402057264199b6479ec21ce | oracle changes and self-tests (final) |

- The manifest recipe is f7b's: `find -type f` without `__pycache__`, `node_modules` and `.pytest_cache`, then `LC_ALL=C sort`, then `sha256sum`. It reproduces f7b's manifest.
- Diffs: `rvC3/f8-vs-f7b.diff`, `f8b-vs-f7b.diff`, `f8b-vs-f8.diff`, `f8c-vs-f7b.diff`, `f8c-vs-f8.diff`, `f8c-vs-f8b.diff`, `f8d-vs-f7b.diff`, `f8d-vs-f8.diff`, `f8d-vs-f8c.diff`.
- `f8d-vs-f7b.diff` covers 26 files: 9 changed, 17 new, none removed.
  - Changed: identity.py, importer.py, review.py, source.py, oracle.py, scenario.py, test_oracle_properties.py, JARVIS_BRAIN_SPEC.md, RELEASE_NOTES_0.6.2.md.
- No product file differs between f8 and f8d.

## 2. Files I changed in integ/cand8 (= f8d − f8)
- **`backend/tests/unit/test_brain_mutation_closure_c3_062.py`** (new; 5 functions, 6 items):
  - JSON nested past the parser is capped and fails closed, in the product and in the oracle. This closes `c3:recursion_open` and `c3:oracle_recursion_open`.
  - A piece of W quoted on one line of several is read on its line. This closes `c3:spans_per_line_off`, which survived on f8b ("54 passed").
  - Controls for both.
- **`backend/tests/property/oracle.py`, ORACLE CHANGE 1** (seeds 13154, 13774; deviation 470):
  - New `quote_spans` (written from the spec text) and `span_relations`.
  - `check_evidence` admits a relation of a span reading only on the side of what a hold *may* show: `allowed = computed | span_relations(...)`, used only in `rep_text <= allowed`. Nothing new is demanded.
  - Justification: the product's evidence 'cut'/'part_of' comes from the N1 whole-text span, which is a contract reading (spec "Hermes's f-string summaries"). The oracle had no span readings.
- **`oracle.py`, ORACLE CHANGE 2** (seeds 13763, 13863, 13879; deviation 471):
  - New `cc_redacted_of`, used by `released_ok`. It accepts a stored released row that equals the released text with only the Command Center's own marks (`[REDACTED:<kind>]`, `[email]`, `[phone]`) in place of spans.
  - Justification: this was a pre-existing oracle defect. All three seeds fail identically on an f7b copy.
- **`test_oracle_properties.py`:** `ROUND3_SEEDS` += 13154 13774 13763 13863 13879, with a comment.
- **`backend/tests/property/test_oracle_spans_c3.py`** (new, 6 tests) and **`test_oracle_released_c3.py`** (new, 4 tests): self-tests proving real violations are still reported.
  - On f8c's oracle the regressions fail (AssertionError or AttributeError); the violation tests pass on both trees.
- **`docs/JARVIS_BRAIN_SPEC.md`:** one sentence on the oracle's span admissibility.
- **Deviation log** `design-062-deviations.md`: entries 465-476 under "2026-11-10, round 3 consolidation (integrator, label consC3)".
- Progress file: `rvC3/consC3-progress.md`.

No existing assertion was weakened. The oracle changes only widen what a hold may show or how a released row may match, each with self-tests.

## 3. Regression runs
Logs are in `integ/fx/logs/<label>.txt`. Summary lines are in `integ/fx/summary.txt`.

**Lanes** on f8d (`bash rvC2/c2lanes.sh rvC3/f8d c3f8d c3f8d g1 g9 g11 g13 g14 g15 -- g4 g2 g8 g10 g12`; g3, g5, g6, g7 and new via `rvC3/c3d-queue.sh`):

| Label | Result |
|---|---|
| c3f8d-g1 | 206 passed |
| c3f8d-g2 | 110 passed |
| c3f8d-g3 | 236 passed, 6 warnings |
| c3f8d-g4 | 180 passed |
| c3f8d-g5 | 68 passed, 56 warnings |
| c3f8d-g6 (tests/unit) | 6 failed, 783 passed |
| c3f8d-g7 (tests/property) | 168 passed, 1 skipped |
| c3f8d-g8 | 20 passed |
| c3f8d-g9 | 13 passed |
| c3f8d-g10 | 8 passed |
| c3f8d-g11 | 17 passed |
| c3f8d-g12 | 36 passed |
| c3f8d-g13 | 15 passed |
| c3f8d-g14 | 7 passed |
| c3f8d-g15 | 116 passed |
| c3f8d-new (the 5 new round-3 integration files incl. mutation_closure_r2) | 51 passed |

- **g7** ran with `HERMES_SRC=…/hermes-src/repo REQUIRE_HERMES_SRC=1 ORACLE_SEEDS=0`. The 1 skip is `test_oracle`'s empty parameter list at ORACLE_SEEDS=0.
- **g6, the six environment-dependent tests:** these were NOT run in their required environment and are not counted as passed. They need a built package and an unprivileged user.
  - `test_brain_ops.py::test_the_package_ships_the_operations_files_and_not_the_sandbox_check`
  - `test_gate.py::test_gate_refuses_a_config_it_could_have_changed`
  - `test_gate.py::test_hello_as_an_unprivileged_user`
  - `test_gate.py::test_wp_cli_gets_an_environment_the_site_cannot_influence`
  - `test_gate.py::test_the_wrapper_passes_a_stop_request_on_to_wp_cli`
  - `test_release_version.py::test_the_built_package_carries_this_builds_timestamp_on_every_file`
- **Earlier freezes:** c3f8-g* on f8 and c3f8b-g6 "6 failed, 780 passed" on f8b; c3f8c-g6 "6 failed, 783 passed" on f8c. These are the same six tests.

**Non-brain integration,** `c3f8b-other` (no non-brain file differs from f7b): "1 failed, 186 passed".
- The failure is `test_approvals_050.py::test_every_ad_finding_says_it_is_sample_data` (`assert ([])`).
- It is date-driven: `MockAds.insights` is seeded by the date and gives 0 findings on 2026-10-27 and 2026-11-10 (`rvC3/c3probe/ads_dates.py`).
- On a tar copy of f7b on the same date (`c3f7b-ads`): "1 failed". It is not round-3 code and is not counted as passed (deviation 468, 475).

**Dashboard:** `diff -rq rvC2/f7b/dashboard rvC3/f8d/dashboard` shows nothing. It is byte-identical, so no Vitest or tsc run was needed.

**Recorded seeds,** both arms on f8d.
- Command: `bash rvC/verify/seeds.sh rvC3/f8d c3f8d-seeds rvC3/c3-seeds94.txt`, finished after the restart by `rvC3/c3f-queue.sh`, which appends only missing seeds.
- The seed list is c2-seeds84 + 13500 13560 13582 13590 13546 + 13154 13774 13763 13863 13879.
- Arm 0: 94/94. Arm 1: 94/94. Each "1 passed, 1 skipped" (`rvC/verify/c3f8d-seeds-h0.out`, `-h1.out`).
- Earlier, on f8b: `c3f8b-seeds` (89 seeds), arm 0 89/89, arm 1 89/89.

**Fresh seeds 13000-13899,** second-withhold arm, 6 lanes of 150, on f8b and re-run on f8d because the oracle changed. Command: `bash rvC2/rel3wfresh.sh <tree> <prefix> <b1> <b2> 150`.

| Base | f8b (c3f8b-fr-*) | f8d (c3f8d-fr-*) |
|---|---|---|
| 13000 | 150 passed, 1 skipped | 150 passed, 1 skipped |
| 13150 | 1 failed, 149 passed, 1 skipped (13154) | 150 passed, 1 skipped |
| 13300 | 1 failed, 149 passed, 1 skipped (13426) | 1 failed, 149 passed, 1 skipped (13426) |
| 13450 | 1 failed, 149 passed, 1 skipped (13532) | 1 failed, 149 passed, 1 skipped (13532) |
| 13600 | 4 failed, 146 passed, 1 skipped (13621 13639 13691 13704) | 4 failed, 146 passed, 1 skipped (13621 13639 13691 13704) |
| 13750 | 6 failed, 144 passed, 1 skipped (13754 13763 13774 13825 13863 13879) | 2 failed, 148 passed, 1 skipped (13754 13825) |

- On f8d every failing seed's problem list is exactly the f8b list, and also matches the drawn-arm `c3f8d-d<seed>` runs. Nothing that passed on f8b fails on f8d.
- The five oracle seeds pass in both arms on f8d (`c3f8d-s<seed>-h0/h1`, "1 passed, 1 skipped").

**The 7 round-2 failures of 13000-13599:**
- 13500, 13560, 13582 and 13590 (step 3) and 13546 (step 2) pass.
- 13426 and 13532 are pending owner decision. Their problem lists are identical to step 3's `sm3-p-13426-h0` and `sm3-p-13532-h1`.

**Root causes of the other failures**

| Seed | Class | Cause | Action |
|---|---|---|---|
| 13154, 13774 (A3/C3-evidence) | oracle gap, introduced with round-3 N1 spans; passed on f7 | product reads 'cut'/'part_of' from the whole-text double-quote span; the oracle had no span reading | fixed by oracle change 1 (deviation 470) |
| 13763, 13863, 13879 (B2) | oracle defect, older than round 3 (identical on f7b) | product stores the released row with its own redaction; `record_release` kept the raw text; Hermes pruned the row | fixed by oracle change 2 (deviation 471) |
| 13639 (B9 + C3) | product gap, privacy, NOT fixed | see below | owner point |
| 13704 | same family as 13639 as far as seen; identical on f7b; not debugged | W2's conversation deleted; product `not_in_backup`, oracle finds W2 | none |
| 13621 (A1 ×12, A4, A6) | identical on f7b | lineage runs through a gateway branch created and deleted between two backups; no backup ever held it | owner point (is such a link in contract?) |
| 13691 (A3) | identical on f7b; fails closed | 'part_of' on a compaction where Hermes masked a secret in W | none |
| 13754 (B7) | identical on f7b; same shape as owner-pending 13426/13532; not debugged | rollback + re-import: a 'same message id' row goes readable → pending | none |
| 13825 (B7) | identical on f7b; possible durability gap; not debugged | after restore/removals/re-import, 3 'new in a later backup' pending rows become readable; no B2/B9 line names them | none |

13639 in detail:
- W is a fold row, and its conversation is deleted later. The only copy left is a CLI-branch copy with the same uid.
- The product reads W's absorbed set only from W's own row (deviation 346), so the copy counts as "absorbs more". W is then not in hand: the holds say `not_in_backup` (fail-closed), but carriers outside the group are not compared.
- So 'Thanks, that is all.', its title and a memory item stay readable: 18 B9 lines, which is residual R2 as the product sees it.
- On f7b this seed looked better only by accident: it took a 'same message id' piece of W as W's text, which is the defect step 3 removed for 13582/13590.
- A fix needs W's absorbed set recorded at withhold time. That is a design change, so it is an owner point (deviation 472).

**No product defect in this round's code was found that I could fix within the contract,** so there was no product edit and no product regression test from me. The f8b/f8c/f8d re-freezes are test- and oracle-only. After each re-freeze I re-ran the affected checks: g6/g7/g3/g5/new/seeds on f8d, then every lane, all mutants and all fresh seeds again on f8d.

## 4. Mutation closure matrix
The full file is `conv/mutation-closure-matrix-f8.md`. Writer: `rvC3/c3matrix.py`, which asserts that no killing test is among its lane's failures on f8d. Driver: `rvC3/c3mut.py --src rvC3/f8d`. Results: `rvC3/c3mut-results.tsv`, tag f8d.

- **UNRESOLVED: none.** All 158 rows ran on f8d: **151 CAUGHT, 7 COMPENSATED, 0 UNRESOLVED.** Every privacy-critical mutant is CAUGHT or COMPENSATED.
- The 158 rows are:
  - 101 f7 mutants (4 re-expressed: `cr2:ci_casefold_off`, `cr2:ci_nfkc_off`, `cr2:oracle_fold_off`, `c2:ci_cutmarks_off`)
  - 7 doubles
  - 32 from steps 1-3
  - 18 new `c3:` rows
- `c3mut.py dry`: "158 mutants and doubles, 0 problems". No INVALID runs.
- An earlier full pass on f8b gave 143 FAIL, 8 PASS: the same 7, plus `c3:spans_per_line_off`, which the f8c closing test now catches.
- **Caveats listed at the top of the matrix:**
  - `cr2:quick_no_snapshot` is COMPENSATED only by an invariant. Its doubles fail only the invariant pins. A probe (`rvC3/c3probe/sp/qprobe.py`) found no behavioural effect even with the invariant broken (710 carriers, 0 skipped).
  - `c2:imp_stored_none_every_off` is CAUGHT only through a crafted-state unit test.

Columns: id | guard file:line (f8d) | impact P=privacy-critical, O=other | killing test (I/U/P = tests/integration, unit, property; first one) | result on the mutant (on f8d) | result on f8 (unmutated, f8d lane) | class / compensating double.

```
m18:trig_release_any_group | 0008_brain_withhold_identity.sql:480 | P | I/test_brain_mutation_closure_c_062.py::test_trig_release_any_group_a_release_under_one_group_never_frees_a_row_of_two | FAIL 1 failed (c3m-m18trigreleaseanygroup) | c3f8d-g15 116 passed | CAUGHT
m18:trig_release_not_exact | 0008_brain_withhold_identity.sql:483 | P | I/test_brain_mutation_closure_c_062.py::test_trig_release_not_exact_a_release_never_frees_another_message_at_its_row_id | FAIL 1 failed (c3m-m18trigreleasenotexact) | c3f8d-g15 116 passed | CAUGHT
m18:r9_off | 0008_brain_withhold_identity.sql:710 | P | I/test_brain_mutation_closure_c_062.py::test_r9_the_repair_blanks_a_rollback_copy_of_a_group_row_written_without_the_sweep | FAIL 1 failed (c3m-m18r9off) | c3f8d-g15 116 passed | CAUGHT
m18:title_trig_groups | 0008_brain_withhold_identity.sql:387 | P | I/test_brain_mutation_closure_c_062.py::test_title_trigger_hides_the_title_of_a_group_conversation_by_membership_alone | FAIL 1 failed (c3m-m18titletriggroups) | c3f8d-g15 116 passed | CAUGHT
m18:seed_pending_too | groups.py:197 | O | I/test_brain_mutation_closure_c_062.py::test_a_pending_hold_never_seeds_a_group | FAIL 1 failed (c3m-m18seedpendingtoo) | c3f8d-g15 116 passed | CAUGHT
m18:j8_always_two | groups.py:393 | P | I/test_brain_mutation_closure_r2_062.py::test_an_orphaned_branch_with_one_copied_row_joins_its_group_by_provenance | FAIL 1 failed (c3m-m18j8alwaystwo) | c3f8d-new 51 passed | CAUGHT
m18:j8_reset_copies | groups.py:377 | O | I/test_brain_mutation_closure_c_062.py::test_a_reset_with_copied_looking_rows_never_joins_by_copied_history | FAIL 1 failed (c3m-m18j8resetcopies) | c3f8d-g15 116 passed | CAUGHT
m18:v_f8_rollback_scope | importer.py:4739 | P | I/test_brain_mutation_closure_c_062.py::test_a_rolled_back_carrier_changed_in_place_comes_back_held_on_its_own | FAIL 1 failed (c3m-m18vf8rollbackscope) | c3f8d-g15 116 passed | CAUGHT
m18:oc_role | review.py:1715 | P | U/test_brain_mutation_closure_c_062.py::test_oc_role_only_user_and_assistant_rows_are_one_click_eligible[system] | FAIL 3 failed (c3m-m18ocrole) | c3f8d-g6 6 failed, 783 passed | CAUGHT
m18:oc_none_in | review.py:1718 | P | U/test_brain_mutation_closure_c_062.py::test_oc_none_in_only_exactly_none_is_one_click_eligible[relations0-related] | FAIL 4 failed (c3m-m18ocnonein) | c3f8d-g6 6 failed, 783 passed | CAUGHT
m18:dec_json_off | identity.py:891 | P | U/test_brain_carriers_r2_062.py::test_skc_a_json_value_only_json_loads_reads_back_is_a_carrier_and_evidence | FAIL 1 failed, 2 passed (c3m-m18decjsonoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
m18:dec_escape_off | identity.py:925 | P | U/test_brain_mutation_closure_c_062.py::test_dec_escape_a_cut_json_text_carries_w_through_its_escapes_only | FAIL 1 failed (c3m-m18decescapeoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
m18:dec_repr_off | identity.py:934 | P | U/test_brain_mutation_closure_c_062.py::test_dec_repr_a_repr_literal_only_unrepr_reads_back_carries_w[...] | FAIL 2 failed (c3m-m18decreproff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
m18:dec_nfkc_off | identity.py:971 | P | (none on single) | PASS 1 passed (c3m-m18decnfkcoff) | c3f8d-g6 6 failed, 783 passed | COMPENSATED ; double: m18:dec_nfkc_off+cr2:ci_nfkc_off FAILS 1 failed
m18:oc_plain | review.py:1713 | P | I/test_brain_later_062.py::test_the_one_click_release_skips_a_row_whose_form_was_never_read | FAIL 1 failed (c3m-m18ocplain) | c3f8d-g3 236 passed, 6 warnings | CAUGHT
m18:oc_tool_row | review.py:1700 | P | I/test_brain_later_062.py::test_the_one_click_release_skips_a_hermes_summary_or_wrapped_text_and_releases_the_plain_row[nesting_mask] | FAIL 2 failed, 19 passed, 6 warnings (c3m-m18octoolrow) | c3f8d-g3 236 passed, 6 warnings | CAUGHT
m18:hgr_updated_copies_off | review.py:988 | P | I/test_brain_rollback_updates_062.py::test_a_rollback_never_restores_a_pruned_tool_row_that_echoed_the_withheld_text[legacy] | FAIL 1 failed, 1 passed (c3m-m18hgrupdatedcopiesoff) | c3f8d-g9 13 passed | CAUGHT
m18:carried_via | importer.py:6339 | P | I/test_brain_oracle_s3_062.py::test_a_kept_hold_keeps_the_rule_that_held_it_through_a_restore[held_before_the_point] | FAIL 1 failed, 1 passed (c3m-m18carriedvia) | c3f8d-g8 20 passed | CAUGHT
gd1:child_edge_off | groups.py:315, groups.py:347 | P | I/test_brain_groups_d1_062.py::test_skc_a_markerless_legacy_gateway_reset_joins_the_group_and_is_held | FAIL 2 failed (c3m-gd1childedgeoff) | c3f8d-g15 116 passed | CAUGHT
gd1:child_both_ways | groups.py:80 | O | I/test_brain_groups_d1_062.py::test_d1_a_markerless_child_joins_from_the_parent_down_only | FAIL 1 failed (c3m-gd1childbothways) | c3f8d-g15 116 passed | CAUGHT
gd1:uncertain_off | groups.py:603 | P | I/test_brain_groups_d1_062.py::test_d1_child_and_uncertain_members_survive_a_restore_and_the_reimport | FAIL 2 failed (c3m-gd1uncertainoff) | c3f8d-g15 116 passed | CAUGHT
gd1:uncertain_no_spread | groups.py:630 | P | I/test_brain_groups_d1_062.py::test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins | FAIL 2 failed (c3m-gd1uncertainnospread) | c3f8d-g15 116 passed | CAUGHT
gd1:link_parents_off | groups.py:322 | P | I/test_brain_groups_d1_062.py::test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins | FAIL 1 failed (c3m-gd1linkparentsoff) | c3f8d-g15 116 passed | CAUGHT
gd1:marker_links_off | importer.py:1197 | P | I/test_brain_groups_d1_062.py::test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins | FAIL 1 failed (c3m-gd1markerlinksoff) | c3f8d-g15 116 passed | CAUGHT
gd1:one_click_uncertain_off | review.py:1695 | P | I/test_brain_groups_d1_062.py::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits | FAIL 4 failed (c3m-gd1oneclickuncertainoff) | c3f8d-g15 116 passed | CAUGHT
gd1:eligible_now_uncertain_off | review.py:1748 | P | I/test_brain_groups_d1_062.py::test_uncertain_is_never_one_click_eligible_and_never_eligible_now | FAIL 1 failed (c3m-gd1eligiblenowuncertainoff) | c3f8d-g15 116 passed | CAUGHT
gd1:release_unrelated_uncertain_off | review.py:1790 | P | I/test_brain_groups_d1_062.py::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits | FAIL 1 failed (c3m-gd1releaseunrelateduncerta) | c3f8d-g15 116 passed | CAUGHT
gd1:holds_display_off | review.py:1528 | P | I/test_brain_groups_d1_062.py::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits | FAIL 4 failed (c3m-gd1holdsdisplayoff) | c3f8d-g15 116 passed | CAUGHT
gd1:export_gate_off | 0008_brain_withhold_identity.sql:953 | P | I/test_brain_groups_d1_062.py::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits | FAIL 1 failed (c3m-gd1exportgateoff) | c3f8d-g15 116 passed | CAUGHT
gd1:definite_off | importer.py:3924, review.py:444 | O | I/test_brain_groups_d1_062.py::test_uncertain_members_never_move_the_decision_to_another_backup | FAIL 1 failed (c3m-gd1definiteoff) | c3f8d-g15 116 passed | CAUGHT
gd1:r9_copy_session_off | 0008_brain_withhold_identity.sql:724, :730, :736 | P | I/test_brain_groups_d1_062.py::test_p2_the_startup_repair_blanks_a_copy_of_a_changed_then_deleted_group_row | FAIL 1 failed (c3m-gd1r9copysessionoff) | c3f8d-g15 116 passed | CAUGHT
gd1:updated_copies_gone_off | review.py:742 | P | I/test_brain_groups_d1_062.py::test_skc_d_the_older_text_of_a_group_row_changed_then_deleted_keeps_no_text_in_any_rollback_copy | FAIL 1 failed (c3m-gd1updatedcopiesgoneoff) | c3f8d-g15 116 passed | CAUGHT
gd1:p2_both_off | 0008_brain_withhold_identity.sql:724, :730, :736, review.py:742 | P | I/test_brain_groups_d1_062.py::test_skc_d_the_older_text_of_a_group_row_changed_then_deleted_keeps_no_text_in_any_rollback_copy | FAIL 2 failed (c3m-gd1p2bothoff) | c3f8d-g15 116 passed | CAUGHT
gd1:oracle_rollback_records_off | cc_adapter.py:439 | O | I/test_brain_groups_d1_062.py::test_p2_the_oracle_names_the_conversation_of_a_copy_whose_row_was_deleted | FAIL 1 failed (c3m-gd1oraclerollbackrecordsof) | c3f8d-g15 116 passed | CAUGHT
cr2:ci_casefold_off | identity.py:416 | P | U/test_brain_carriers_r2_062.py::test_oci_every_relation_ignores_case_and_normalisation[en-all caps] | FAIL 12 failed, 6 passed (c3m-cr2cicasefoldoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:ci_turkish_off | identity.py:417 | P | U/test_brain_carriers_r2_062.py::test_oci_every_relation_ignores_case_and_normalisation[tr-all caps] | FAIL 2 failed, 17 passed (c3m-cr2citurkishoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:ci_nfkc_off | identity.py:416 | P | U/test_brain_carriers_r2_062.py::test_oci_every_relation_ignores_case_and_normalisation[en-fullwidth] | FAIL 5 failed, 13 passed (c3m-cr2cinfkcoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:ci_wordseq_off | identity.py:1430 | P | U/test_brain_carriers_r2_062.py::test_oci_a_short_withheld_text_in_another_case_is_ambiguous_not_none | FAIL 1 failed (c3m-cr2ciwordseqoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:generic_off | identity.py:462 | O | U/test_brain_carriers_r2_062.py::test_skc_a_common_url_value_in_an_unrelated_search_result_is_no_carrier | FAIL 2 failed (c3m-cr2genericoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:generic_overbroad | identity.py:462 | P | U/test_brain_carriers_r2_062.py::test_od4_greeting_and_url_cases_hold_nothing_and_anything_more_is_held | FAIL 2 failed (c3m-cr2genericoverbroad) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:prune_off | identity.py:1065 | P | U/test_brain_carriers_r2_062.py::test_p1_hermes_generic_prune_line_keeping_ws_first_40_characters_is_a_cut[image_generate-args0] | FAIL 2 failed, 4 passed (c3m-cr2pruneoff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:scan_longest64 | review.py:1197 | P | I/test_brain_carriers_r2_062.py::test_skc_a_native_partial_reply_quote_of_a_long_w_is_held_wherever_it_is_found[stored_before_the_withhold] | FAIL 2 failed, 1 passed (c3m-cr2scanlongest64) | c3f8d-g15 116 passed | CAUGHT
cr2:titles_longest64 | review.py:1252 | P | I/test_brain_carriers_r2_062.py::test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too | FAIL 1 failed (c3m-cr2titleslongest64) | c3f8d-g15 116 passed | CAUGHT
cr2:items_longest64 | review.py:1303 | P | I/test_brain_carriers_r2_062.py::test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too | FAIL 1 failed (c3m-cr2itemslongest64) | c3f8d-g15 116 passed | CAUGHT
cr2:quick_forms_one | identity.py:1186 | P | (none on single) | PASS 43 passed (c3m-cr2quickformsone) | c3f8d-g6 6 failed, 783 passed | COMPENSATED ; double: cr2:quick_forms_one+cr2:quick_keep_gt FAILS 2 failed, 41 passed
cr2:quick_keep_gt | identity.py:1168 | P | U/test_brain_carriers_r2_062.py::test_p2_the_skeleton_drops_every_whitespace_and_combining_character | FAIL 1 failed (c3m-cr2quickkeepgt) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:quick_keep_marks | identity.py:1171 | P | U/test_brain_carriers_r2_062.py::test_p2_the_skeleton_drops_every_whitespace_and_combining_character | FAIL 1 failed (c3m-cr2quickkeepmarks) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:quick_q8 | identity.py:1199 | P | U/test_brain_carriers_r2_062.py::test_p2_the_quick_filter_never_skips_a_row_the_comparison_relates | FAIL 1 failed (c3m-cr2quickq8) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:quick_no_decode | identity.py:1221 | P | U/test_brain_carriers_r2_062.py::test_p2_the_quick_filter_never_skips_a_row_the_comparison_relates | FAIL 1 failed (c3m-cr2quicknodecode) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:quick_no_snapshot | identity.py:1222 | P | (none on single) | PASS 49 passed (c3m-cr2quicknosnapshot) | c3f8d-g6 6 failed, 783 passed | COMPENSATED (invariant) ; double: cr2:quick_no_snapshot+sm3:snapshot_re_i FAILS 2 failed, 6 passed; cr2:quick_no_snapshot+sm3:quote_shapes_no_lead FAILS 2 failed, 6 passed
cr2:quick_no_quote_bypass | identity.py:1224 | P | U/test_brain_carriers_r2_062.py::test_p2_a_reply_quote_of_sixteen_characters_as_written_is_never_skipped_by_the_quick_filter | FAIL 1 failed (c3m-cr2quicknoquotebypass) | c3f8d-g6 6 failed, 783 passed | CAUGHT
cr2:preview_narrow | identity.py:2399 | O | I/test_brain_carriers_r2_062.py::test_oci_the_withhold_preview_reads_another_case | FAIL 1 failed (c3m-cr2previewnarrow) | c3f8d-g15 116 passed | CAUGHT
cr2:preview_items_case | identity.py:2800 | O | I/test_brain_carriers_r2_062.py::test_oci_the_withhold_preview_reads_another_case | FAIL 1 failed (c3m-cr2previewitemscase) | c3f8d-g15 116 passed | CAUGHT
cr2:preview_title_case | identity.py:2507 | O | I/test_brain_carriers_r2_062.py::test_oci_the_withhold_preview_reads_another_case | FAIL 1 failed (c3m-cr2previewtitlecase) | c3f8d-g15 116 passed | CAUGHT
cr2:oracle_fold_off | oracle.py:90 | O | P/test_oracle_carriers_r2.py::test_oci_the_oracle_demands_a_carrier_in_another_case_or_form | FAIL 2 failed (c3m-cr2oraclefoldoff) | c3f8d-g7 168 passed, 1 skipped | CAUGHT
cr2:oracle_generic_off | oracle.py:127 | O | P/test_oracle_carriers_r2.py::test_od4_the_oracle_neither_demands_nor_allows_a_generic_piece_and_demands_anything_more | FAIL 1 failed (c3m-cr2oraclegenericoff) | c3f8d-g7 168 passed, 1 skipped | CAUGHT
cr2:oracle_prune_off | oracle.py:1792 | O | P/test_oracle_carriers_r2.py::test_p1_the_oracle_demands_ws_first_40_characters_in_the_generic_prune_line | FAIL 2 failed (c3m-cr2oraclepruneoff) | c3f8d-g7 168 passed, 1 skipped | CAUGHT
rel3:mark_off | review.py:404 | P | I/test_brain_releases_r2_062.py::test_rel3_a_later_withhold_makes_none_stale_at_once_and_the_export_waits | FAIL 2 failed (c3m-rel3markoff) | c3f8d-g15 116 passed | CAUGHT
rel3:reopen_off | review.py:405 | P | I/test_brain_releases_r2_062.py::test_rel3_settle_of_a_newer_backup_finishes_the_earlier_decision_again | FAIL 1 failed (c3m-rel3reopenoff) | c3f8d-g15 116 passed | CAUGHT
rel3:keep_later_off | identity.py:1982 | P | U/test_brain_releases_r2_062.py::test_keep_later_keeps_the_mark_until_every_text_was_in_hand | FAIL 1 failed (c3m-rel3keeplateroff) | c3f8d-g6 6 failed, 783 passed | CAUGHT
rel3:all_in_hand_true | importer.py:947 | P | I/test_brain_releases_r2_062.py::test_rel3_a_settle_without_the_later_text_keeps_the_evidence_stale | FAIL 1 failed (c3m-rel3allinhandtrue) | c3f8d-g15 116 passed | CAUGHT
rel3:all_in_hand_false | importer.py:947 | O | I/test_brain_releases_r2_062.py::test_rel3_settle_with_every_text_in_hand_computes_it_again | FAIL 1 failed (c3m-rel3allinhandfalse) | c3f8d-g15 116 passed | CAUGHT
rel3:eligible_own_only | review.py:1755 | P | U/test_brain_releases_r2_062.py::test_eligible_now_compares_every_withheld_text_in_hand | FAIL 1 failed (c3m-rel3eligibleownonly) | c3f8d-g6 6 failed, 783 passed | CAUGHT
rel3:hc_group_skip | review.py:1198 | P | I/test_brain_releases_r2_062.py::test_skC_a_row_released_for_w1_that_carries_w2_is_held_when_w2_is_withheld | FAIL 3 failed (c3m-rel3hcgroupskip) | c3f8d-g15 116 passed | CAUGHT
rel3:hgr_foreign_off | review.py:1033 | P | I/test_brain_releases_r2_062.py::test_rel3_hold_group_rows_compares_a_released_row_with_other_withheld_texts | FAIL 1 failed (c3m-rel3hgrforeignoff) | c3f8d-g15 116 passed | CAUGHT
rel3:imp_carriers_group_skip | importer.py:1922 | P | I/test_brain_releases_r2_062.py::test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import | FAIL 1 failed (c3m-rel3impcarriersgroupskip) | c3f8d-g15 116 passed | CAUGHT
rel3:flip_any_anchor | review.py:951 | P | I/test_brain_releases_r2_062.py::test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import | FAIL 1 failed (c3m-rel3flipanyanchor) | c3f8d-g15 116 passed | CAUGHT
rel3:mark_any_backup | identity.py:2666 | P | I/test_brain_releases_r2_062.py::test_rel3_6725_a_typed_release_never_makes_another_backups_release_trusted | FAIL 1 failed (c3m-rel3markanybackup) | c3f8d-g15 116 passed | CAUGHT
rel3:imp_mark_any_backup | importer.py:3297 | P | I/test_brain_releases_r2_062.py::test_rel3_6725_a_check_of_one_backups_content_marks_only_that_backups_release | FAIL 1 failed (c3m-rel3impmarkanybackup) | c3f8d-g15 116 passed | CAUGHT
rel3:hgr_mark_any_backup | review.py:1023 | P | I/test_brain_releases_r2_062.py::test_rel3_6725_a_check_of_one_backups_content_marks_only_that_backups_release | FAIL 1 failed (c3m-rel3hgrmarkanybackup) | c3f8d-g15 116 passed | CAUGHT
rel3:oracle_nb_off | oracle.py:2103 | O | P/test_oracle_releases_r2.py::test_nb_a_released_row_of_a_withheld_conversation_carrying_another_withheld_text_is_a_leak | FAIL 1 failed (c3m-rel3oraclenboff) | c3f8d-g7 168 passed, 1 skipped | CAUGHT
rel3:oracle_na_own_only | oracle.py:1026 | O | P/test_oracle_releases_r2.py::test_na_none_must_hold_against_every_withheld_text_in_hand | FAIL 1 failed (c3m-rel3oraclenaownonly) | c3f8d-g7 168 passed, 1 skipped | CAUGHT
rel3:oracle_later_unread | oracle.py:316 | O | P/test_oracle_releases_r2.py::test_later_withhold_is_an_undecided_state_for_the_oracle | FAIL 2 failed (c3m-rel3oraclelaterunread) | c3f8d-g7 168 passed, 1 skipped | CAUGHT
rel3:none_every_off | identity.py:2000 | P | I/test_brain_releases_r2_062.py::test_rel3_13465_a_later_row_identical_to_a_withheld_text_not_in_hand_is_never_none | FAIL 2 failed (c3m-rel3noneeveryoff) | c3f8d-g15 116 passed | CAUGHT
rel3:imp_none_every_off | importer.py:2856 | P | I/test_brain_releases_r2_062.py::test_rel3_13465_a_later_row_identical_to_a_withheld_text_not_in_hand_is_never_none | FAIL 1 failed (c3m-rel3impnoneeveryoff) | c3f8d-g15 116 passed | CAUGHT
rel3: