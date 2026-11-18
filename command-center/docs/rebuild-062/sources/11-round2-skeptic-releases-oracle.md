I found one new problem, F1 (MEDIUM): a gap in seed 6725's class that this round only half closed. I found no new HIGH leak in the release paths, and I could not refute the N-A and N-B fixes in any of the shapes I tried. Every CAUGHT, COMPENSATED and UNRESOLVED claim in the mutation matrix that I re-ran holds up. I found no weakened test or oracle property.

I worked only on my own tar copy of f7b (manifest verified). `integ/cand8` is unchanged: it still verifies against `rvC2/f7b-MANIFEST.sha256`.

## New problem F1 (MEDIUM): an unchecked release can borrow trust from another backup's typed release
- **Where:** `identity.exempt_covers` (identity.py, lines 2464–2466). It decides "trusted?" from any release of the row, and "covers this content?" from any release of the row. So an unchecked release decided on backup X can stand on a typed release decided on backup D.
  - `Identities.released` pairs trust with the covering release correctly.
  - Step 3 fixed only the marking side (`mark_exemptions`). Deviation 391 named exactly this outcome: a narrow fail-open of B6's enforcement of decisions files.
- **Effect:** a row that B6 requires to be held stays readable without its check, on every path that calls `exempt_covers`:
  - the `hold_group_rows` sweeps run by withhold, Settle, the decisions import, rollback and restore;
  - `hold_deleted_copies` and `hold_updated_copies`;
  - `hold_carriers`;
  - the Settle stale-time path.
- **Failing tests** (file `rvC2/sk/skC2-releases-oracle/evidence/test_skc2_rel_adv.py`):
  - `test_skc2_exempt_covers_never_takes_trust_from_another_backups_release`, line 146 `assert by_cov is False`. Here `Identities.released` returns False and `exempt_covers` returns True.
  - `test_skc2_an_unchecked_release_never_stands_on_another_backups_typed_release[typed_on_another_backup]`, line 193 `assert st == "held"`. The row stays readable; the control with the unchecked release alone is held.
- **Pre-existing:** both tests fail the same way on an f6 copy (`skc2-f6f1`: "2 failed, 1 passed, 14 deselected"). It is not this round's code, but it is the 6725 class the owner ordered fixed.
- **A minimal fix exists:** read trust only from the releases that cover the stored content (`evidence/f1-proposed-fix.diff`). With it, my file plus nine release/restore/rollback/Settle files gave "103 passed" (`skc2-fix`). That is not a full lane run.
- **Same pattern, not tested:** the local `exempt()` in `identity.group_rows` ignores trust altogether.

## Owner requirements (my lens only)
| # | Requirement | Verdict |
|---|---|---|
| 1 | Resets, branches, copies, imports, restores, live installs | Held on my new probes; not held overall because of F1 on the rollback and restore sweeps |
| 2 | Copied and derived content protected | Held on my probes |
| 3 | Carriers held individually | Not assessed beyond the controls |
| 4 | Releases explicit and limited | Held for N-A and N-B in every shape I tried; not held because of F1 |
| 5 | Problem 5 / 12150 / 11332 | Only `carried_via` (12150) checked: killed. Problem 5 and 11332 not assessed |

My probes for 1, 2 and 4:
- Restore plus the removals-only file after a later withhold: X comes back as `later_withhold`; the one-click release skips it and an untyped per-row release gets a 400.
- Live importing two decisions files in a row: X is held, including after a rollback of live's second import.
- A session_search row released for W1 is held for W2 at the withhold, at Settle and at the next import.
- A carrier of both W1 and W2, released with the typed word for one, is not freed even after JARVIS deletes the other withheld text's conversation.
- Five direct API bodies (file flags, lowercase, null, zero-width character, kind) all get a 400.

## Owner decisions
- **O-D1: held, as far as the mutants test it.** The uncertain-link mutants on the one-click, `eligible_now`, `release_unrelated`, `release_hold`, sweep and import checks are all caught. Five of them needed the consolidation's closing tests (MC2) to be caught.
- **O-CI: held, as tested.** The casefold, Turkish, NFKC and word-sequence mutants are caught.
- **O-D4: held, as tested.** `generic_in_evidence` and `generic_overbroad` are caught.

## Weakened tests or properties: none found in my lens
- The previous skeptic's 7 tests sit in `test_brain_releases_r2_062.py` with the same names and the same assert lines.
- The adapted tests (`settle_062`, `release_c_062`, `resolve_c_062`) only add protection.
- The oracle's narrowings for 13327, 13407 and 13364 each have a mutant that loosens them, and each such mutant is killed.

## Mutation audit
Each mutant ran on its own tar copy with `skc2m*` databases. All failures were assertion failures, not crashes.
- **Baseline:** every killing-test file, unmutated, gave "207 passed".
- **First pass:** 92 mutants (all 81 privacy-critical plus the 11 oracle ones), each with its driver's own tests: 76 caught, 16 passed.
- **The 16 that passed, re-run as the matrix specifies:**
  - **CAUGHT confirmed (8):** `j8_always_two`; `recorded_uncertain_off`, `repair_uncertain_legacy`, `uncertain_later_seeded`, `hgr_verify_uncertain_off` and `release_check_uncertain_off` (by MC2); `quick_keep_gt` and `quick_keep_marks` (by the whole UC2 file).
  - **COMPENSATED confirmed (6):** each single mutant passes broad tests and each double mutant fails.
    - `dec_nfkc_off`: double with `ci_nfkc_off` fails.
    - `quick_forms_one`: double with `quick_keep_gt` fails.
    - `imp_keep_later_off`: the single passes 58 tests including mine; the double with `imp_none_every_off` gives 2 failed.
    - `gone_deadrow`, `gone_key`, `gone_uid`: deadrow+uid fails, deadrow+key fails, all three fail.
    - Caveat: key+uid passes. So with the deleted-row lookup working, nothing tests the key and uid lookups. This matches the matrix's claim.
  - **UNRESOLVED confirmed (2):**
    - `quick_no_snapshot`: 43 and 31 passed.
    - `imp_stored_none_every_off`: only my two F1 tests fail, and they fail unmutated too. I found no input that reaches this guard: a withheld family's rows are never deleted by a later backup.
- **Not re-run:** the 9 mutants classed "other" (overreach, the withhold dialog preview, decision backup).

## Simulator
- The Hermes operations are faithful: conformance gave "47 passed" with `REQUIRE_HERMES_SRC=1`. `hermes_sim` changed only how the simulator's ground truth labels the markerless-child edge.
- The second-withhold arm has coverage gaps rather than unfaithful steps:
  - no Hermes operation, restore or rollback between the review and the second withhold;
  - live always gets one final decisions file, never two in a row;
  - it never picks a W2 shorter than 16 characters (13546's shape);
  - it never tries an untyped per-row release.
- My targeted tests cover the restore and two-file gaps, and both pass.

## Commands and summary lines
Each test run used `ROOT=<my copy> bash S/integ/fx/run.sh <label> <dblabel> -rfE ...`, with S the scratchpad.

| Label | What ran | Summary line |
|---|---|---|
| `skc2-base` | baseline, every killing-test file | 207 passed |
| `skc2-adv2` | first 5 adversarial tests | 2 failed (F1), 3 passed |
| `skc2-adv3` | live with two decisions files | 2 passed |
| `skc2-adv4` | restore with the removals-only file | 2 passed |
| `skc2-adv5` | carrier of two withheld texts | 2 passed |
| `skc2-adv6` | session_search row and direct API | 6 passed |
| `skc2-conf` | Hermes conformance | 47 passed |
| `skc2-f6f1` | F1 tests on an f6 copy | 2 failed, 1 passed |
| `skc2-fix` | proposed fix plus nine files | 103 passed |

Mutants ran through `tools/skmut.py` (labels `skc2m-*`). Two early probes of rollback copies were set up wrongly (the stored rows they targeted are never deleted, by design) and I removed them. Every scratch copy is deleted and every `cc_test_rd_fxskc2*` database is dropped (0 left).

Files are in `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rvC2/sk/skC2-releases-oracle/`:
- `evidence/test_skc2_rel_adv.py`
- `evidence/f1-proposed-fix.diff`
- `evidence/mutation-results.tsv`
- `evidence/mutation-pass2-3.tsv`
- `tools/skmut.py`

These tests show the listed shapes on the inputs tried; they do not show that no leak remains. 0.6.2 stays NO-GO.