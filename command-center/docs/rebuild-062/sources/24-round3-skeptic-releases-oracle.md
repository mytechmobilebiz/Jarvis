I found one new HIGH privacy problem in my lens: a withheld text becomes readable. 0.6.2 stays NO-GO. These runs cover the cases tried; they do not show that no leak remains. Under the owner's rules (privacy first, fail closed), new problem N1 means owner requirements 1, 2 and 4 are not held, nor B6/D3 ("none needs every text").

## Base and isolation
- I checked rvC3/f8d against its manifest (4ee4844c…) and confirmed integ/cand8 equals f8d (`diff -rq`), at the start and at the end.
- I worked only on my own tar copies under rvC3/sk/skC3-releases-oracle/ (an f8d copy, an f7b copy for the pre-existence checks, and one mutant copy at a time).
- My database labels all start with skc3ro. All of them are dropped (0 left), and all copies are deleted.
- I did not edit cand8 or any frozen tree, and I did not commit anything.
- One seed run was briefly started with ROOT=rvC3/f8d. I killed it and re-ran on my copy. Afterwards f8d still verifies against its manifest.

## New problems
**N1 (HIGH, N-A class, pre-existing): a hold's "none" stays when a decisions file brings a later withhold whose row this installation does not store.**
- **Root cause:**
  - `identity.mark_later_withhold` (the N-A fix) is called only from `review.withhold_message`.
  - The live import path (`importer.decisions_before_import`) puts the new withhold's stone directly.
  - The restore / removals-only path (`importer._apply_removal`) does too, when the withheld row is not here ("kept for later imports").
  - In both cases the existing pending holds keep "none" computed without the new withheld text, stay `one_click_eligible`, and are freed without RELEASE.
- **Shape:** the test copy withholds W1 (file f1), then W2 (file f2). JARVIS then deletes W2's conversation (`SessionDB.delete_session`). Row X in W1's conversation repeats W2 word for word.
- **S1 (live, file by file):** after f2, X is still `['none']`, one-click eligible. One-click (or an untyped per-row release) frees it, and the next import makes X readable with W2's words.
  - Test: `evidence/test_skc3_rel_adv.py::test_skc3_s1_live_second_file_brings_a_withhold_with_no_row_here_and_a_stale_none_stays[one_click|per_row_untyped]`
  - Failing line: 91, `assert not freed` (line 92, `assert not leaks`, would fail too).
  - `skc3ro-s1b` on f8d: "2 failed, 1 passed". The control (f2 alone gives `other_not_compared`) passes.
- **S2 (restore.ps1, a single installation):** restore point taken with X "none"; W2's conversation imported and withheld later, then deleted; DB restored and the removals-only file applied. X is still "none"; one-click frees it and X becomes readable with W2's words.
  - Test: `test_skc3_s2_restore_removals_only_brings_a_withhold_with_no_row_here_and_a_stale_none_stays[...]`, line 159 `assert not freed`.
  - `skc3ro-s2`: "2 failed, 3 deselected".
- **S3 (MEDIUM, same root, W2 in hand):** after f2, X still shows `['none']` and is one-click eligible (D3 / C3-evidence dishonest). One-click frees it. The N-B safety net re-holds X at the next import, so nothing leaks.
  - Line 195. `skc3ro-s3`: "1 failed, 5 deselected".
- **Pre-existing:** the same tests fail the same way on an f7b copy: `skc3ro-s1-f7b` "2 failed, 1 passed"; `skc3ro-s2-f7b` "2 failed, 3 deselected"; `skc3ro-s35-f7b` "2 failed, 9 passed, 5 deselected".
- **Fix direction (probe only, reverted):** `evidence/probe-S1-S2-fix.diff` calls `mark_later_withhold` and `reopen_decision_backup` in both paths when the file brings an admin's root withhold the database does not have yet.
  - With it: `skc3ro-s12-probe` "5 passed".
  - Lanes with it: g3 "236 passed, 6 warnings"; g9 + releases_r2 / release_trust / seeds_r3 / live_history "64 passed"; restore_releases / restore_c / decisions_v4 / releases_r2 / durability "42 passed".
  - Fresh seeds were not run with the probe.
- **The oracle never exercises this:** its second-withhold arm gives live one final decisions file and never deletes W2's conversation in between.

**N2 (MEDIUM, owner question, pre-existing):** live released X by one-click between the two files, with every text in hand at that point. f2 then brings W2, whose text live never holds. X stays readable with W2's words.
- The N-B re-check needs the later text in hand, and "none needs every text" covers holds, not releases.
- Test S5, line 254. `skc3ro-s5` "1 failed"; it also fails on f7b.

## Checked and held
- **Root-caused seeds, on my copy, both arms:** 13560, 13582, 13590 and 13500 each give "1 passed, 1 skipped" for h0 and h1.
  - I read the step-3 fixes: the `_w_text` "same message id" check, the 13500 seeding (consistent with O-lt1c) and the `rekeyed` oracle change.
- **Exemption trust (F1 fix):** the round-2 skeptic file gives "17 passed" on f8d, F1 included.
  - `Identities.released`, `exempt_covers`, `group_rows` and `_released_here` / `_release_covers` pair trust with coverage.
  - Still read without trust, and still unexercised (low impact as far as I can see): `archive._withheld_keys` (rollback copies at rest), `importer._readable_uids`, `later.load_family_readable`, the preview lists, and the "released as decided on this backup, which does not hold it" path. That last path ends a hold without a check, but the row stays without text and is listed again unless verified.
  - A file's `left_readable` exemption is trusted, as design-C says.
- **Direct API requests** (S4, "9 passed"): every untyped body gets 400 or 422 and the row stays held: empty, `confirmed`/`checked` flags, lowercase, zero-width, fullwidth, a list, two lines. The typed word with extra fields gives 200, as designed.
- **Simulator:** `skc3ro-conf` (Hermes conformance, lineage, derived3 lines and the five oracle self-test files, `REQUIRE_HERMES_SRC=1`) gives "86 passed". It is faithful where it is exercised; the gap is coverage (N1, N2).

## Weakened tests or properties
None found.
- No assert was removed from any test file between f7b and f8d; the assert counts are equal. scenario.py only passes the backup to `check_stored_unchanged`.
- Oracle changes:
  - span admission only widens what a hold may show;
  - `cc_redacted_of` accepts only stored texts whose readable characters appear, in order, in the released text;
  - `kept_carrier` only excuses holding more;
  - `rekeyed` matches the same archive row only when the backup gives it that uid;
  - the decode cap makes the check stricter.

## Mutation audit
- **Method:** I re-ran all 108 privacy-critical singles and the 7 doubles, each on its own copy (driver `evidence/skmut3.py`, results `evidence/skmut3-results.tsv`).
- **Result:** 108 FAIL and 7 PASS. The 7 that pass are the six f7 COMPENSATED singles plus `cr2:quick_no_snapshot`. All 7 doubles fail. No mutant crashed instead of failing an assertion.
- **Comparison:** my classification is identical to the consolidation's f8d rows on all 115.
- **COMPENSATED singles on broader test sets** (tests/unit plus their integration files): "9 failed, 840 passed", and 833 for `imp_keep_later_off`. The 9 are environment failures: the 5 environment-dependent tests (test_gate ×4, the test_brain_ops package test), and 4 test_release_version tests that fail because the mutant copies leave out `dashboard/`. No brain test fails, which matches COMPENSATED, and I found the compensating guards sound.
- `quick_no_snapshot` stays equivalent only through its invariant pins, as the matrix itself says.

## Owner decisions
- **O-D1: held as tested.** Every uncertain-link mutant is caught; uncertain links are never one-click.
- **O-CI: held as tested.** `ci_*` caught; `dec_nfkc` compensated by `fold`.
- **O-D4: held as tested.** The generic mutants are caught.
- **B6 / O-D3: NOT held** (N1 S1–S3).
- **Pending owner decisions** (groups-paths B and D, N3 / R5b, seeds 13426 and 13532): not tested here.

## Owner requirements
| Requirement | Verdict |
|---|---|
| 1. Decisions survive imports, restores and live installs | Not held (N1, live and restore) |
| 2. Copied and derived content protected | Not held (N1, N2) |
| 3. Carriers held individually | Held in my probes (N-B re-held X in S3) |
| 4. Releases explicit and limited | Not held (a one-click release of a row carrying W2 is offered and runs) |
| 5. Permissions and API | Held (S4) |

## Files
All in /tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rvC3/sk/skC3-releases-oracle/evidence/:
- test_skc3_rel_adv.py
- probe-S1-S2-fix.diff
- skmut3.py
- skmut3-results.tsv
- skmut3-run.out
- skc3ro-s1b.txt
- skc3ro-s2.txt
- skc3ro-s3.txt
- skc3ro-s4.txt
- skc3ro-s5.txt
- skc3ro-s1-f7b.txt
- skc3ro-s2-f7b.txt
- skc3ro-s35-f7b.txt
- skc3ro-s12-probe.txt

Progress file: /tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rvC3/skC3-releases-oracle-progress.md
Shell cwd was reset to /home/user/Jarvis