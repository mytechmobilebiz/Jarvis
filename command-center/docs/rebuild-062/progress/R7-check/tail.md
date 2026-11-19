
## Items with no test, or not reconstructed
**Behaviour missing (not built in R7: this step adds no product or oracle behaviour):**
- **`gd1:oracle_rollback_records_off`** (cc_adapter.py:439, impact O) and its test
  `test_p2_the_oracle_names_the_conversation_of_a_copy_whose_row_was_deleted`: the rebuilt oracle never reads
  `import_records`, so it does not check rollback copies at rest (round-1 P2 case D) at all. The product side is
  tested (r2s2-02). Open for round 4 (RECON-50).
- **Seed 13500, "the withhold seeds from groups.seeds"** (r3s2-10): RECON-37 found no difference to build or test in
  the rebuilt withhold; there is no test for it.
- **Oracle narrowings 13327, 13407 and 13364** (r2s4-12): narrowings for round-2 seed scenarios that the rebuilt
  simulator does not reproduce. R5's own narrowings have self-tests and 22 killed oracle mutants instead.

**Tests or evidence lost and not rebuilt (the behaviour is covered by other tests above):**
- `tests/integration/test_brain_release_trust_062.py` (11 tests, round 3 step 1): stands in `test_brain_releases_r2_062.py`
  (`test_skc2_*`, `test_rel3_an_unchecked_file_release_frees_no_hold_and_a_trusted_one_does`).
- `tests/integration/test_brain_seeds_r3_062.py` (3) and `tests/integration/test_brain_mutation_closure_r3_062.py` (8,
  among them the crafted-state test of `c2:imp_stored_none_every_off`): the rebuilt importer has one "none every" guard
  (`identity.settled_relations`), tested by `test_none_every_none_needs_every_withheld_text_in_hand` and the 13465 test.
- `tests/property/test_hermes_derived3_lines.py` (6) and `hermes_lean_gen.py`: N1/N4 checked on lines that Hermes
  itself generates. Only hand-written Hermes-shaped lines are tested now (`test_n1_*`, `test_n4_*`, `test_m1_*`).
- `test_brain_resolve_c_062` (round-2 lane g15: the product's state list): no file of that name; the state codes are
  pinned by `unit/test_brain_hold_r2.py::test_the_new_hold_vias_and_the_state_are_known_codes` and
  `unit/test_brain_releases_r2_062.py::test_d3_states_that_were_not_compared_are_never_one_click`.
- Of the 7 closing tests for `dec_json_off`, 2 are in the tree.
- Not rerun: the large adversarial runs (15,928 filter comparisons, 11,565 random pieces, the derived skeptic's 214
  Hermes texts, 64 realistic and 4,000 random unrelated texts).
- The skeptics' adversarial files (`test_skC_*`, `test_skc2_*_adv.py`, `test_skc3_*`) were evidence, not suite files. The
  round-3 ones are for open or owner-pending items, so they are not fixed items.

**Matrix guards that the rebuild's mutation drivers (progress/R1..R5-mutation) never switched off.** Their tests fail on
i22 and pass on HEAD, but it is not shown that the rebuilt guard is the one the test catches:
- `gd1:eligible_now_uncertain_off`: switched off in R7 (`review.eligible_now` passing `uncertain=False`):
  `unit/test_brain_releases_r2_062.py::test_uncertain_is_never_one_click_eligible_and_never_eligible_now` "1 failed".
- `m18:dec_nfkc_off`: the rebuilt decode has no NFKC step of its own (NFKC is applied by `identity.fold` to every
  reading, `cr2:ci_nfkc_off`, CAUGHT in R3); nothing separate to switch off.
- `rel3:imp_mark_any_backup`, `rel3:hgr_mark_any_backup`: the rebuild checks the backup in `identity.mark_exemptions`
  only (`rel3:mark_any_backup`, CAUGHT in R4); its two callers pass the backup of the content they checked.
- `rel3:hc_group_skip`: `review.hold_carriers` has no group skip to switch on; R4's double
  `rel3:hgr_foreign_off+hc_carriers_off` is caught by the skC test.
- `rel3:imp_carriers_group_skip`, `rel3:flip_any_anchor`, `gd1:p2_both_off` (a double): not mutated (open).

**Different class in the rebuild than in the source matrix:**
- `cr2:quick_forms_one` and `cr2:quick_no_snapshot` were COMPENSATED in the source; R3's driver caught each on its own.
- `cr2:quick_no_quote_bypass` was CAUGHT in the source; it is COMPENSATED in the rebuild (R3).
- `rel3:hgr_foreign_off` was CAUGHT in the source and COMPENSATED in R4; it is caught on its own by the R7 test.
- `r3:release_on_evidence_carrier_off` (R3's own guard) still SURVIVED; it is not a source matrix row.

**Not in the sources:** the 83 matrix rows after line 230.

**Open test gaps from round 3, not fixed items:** `unrepr_cut_keep`, `open_literal_off`, `cut_per_line_off` (derived
skeptic) and `sk3_graph_marker_edges_off` (groups skeptic). R3's driver ran `sk3:cut_per_line_off` (COMPENSATED: the
double with `r3:truncated_mark_off` is caught); the others were not checked here.
