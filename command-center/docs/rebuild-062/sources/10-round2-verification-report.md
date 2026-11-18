# 0.6.2 Option C, fix round 2 (workflow wf_7663b952-3fe): verification report

**Date:** 2026-10-27.

**Verdict: NOT PASS. 0.6.2 stays NO-GO.**

The owner's PASS rule needs three conditions:
1. every privacy-critical mutation is CAUGHT or COMPENSATED with reproducible evidence;
2. no critical privacy failure remains;
3. the post-fix regression passes.

None of the three holds:
- 2 privacy-critical mutants are UNRESOLVED;
- 1 HIGH leak and several MEDIUM-HIGH leaks are open;
- 7 fresh seeds fail.

All 11 problems from round 1 are fixed and independently confirmed.

**Final candidate:** `integ/cand8` = f7b (`rvC2/f7b`). Manifest SHA-256 `2c6c2f8391ece313…`, 459 files. f7 (`fd2205125af9…`) holds the product; f7b adds test-only closures, so no product file differs between f7 and f7b.

**Agent reports:** `records/round2/00–06*.md`. Order: groups-d1, carriers, releases (resumed twice after container restarts), consolidation, then 3 skeptics.

## 1. Baseline and mutation testing
**Baseline:** f6 (`799b4d9f…`). It still verifies against its own manifest.

**Mutation closure matrix** (`conv/mutation-closure-matrix-f7.md`; 101 mutants, one guard at a time, each with its own copy and databases):

| Class | Count |
|---|---|
| CAUGHT | 93 |
| COMPENSATED | 6 (each double mutant shown failing) |
| UNRESOLVED | 2, both privacy-critical |

The two UNRESOLVED mutants:
- **`cr2:quick_no_snapshot`** (identity.py:1084). The derived-copies skeptic argues it is equivalent by construction: every snapshot reading contains `SNAPSHOT_LEAD`, which bypasses the quick filter first. Under the owner's rule it stays UNRESOLVED until a test or a proof is recorded in the matrix.
- **`c2:imp_stored_none_every_off`** (importer.py:3604). No input reaches this guard: group rows are held before a later backup deletes them. Two independent agents confirmed this.

**Independent audit:** the releases skeptic re-ran 92 mutants. It confirmed every CAUGHT, COMPENSATED and UNRESOLVED class it re-ran, and found no false classification.

## 2. Round-1 problems: status
| Round-1 item | Status (independently confirmed) |
|---|---|
| groups-paths P1 HIGH (markerless children A, B, C) | Fixed under O-D1; the skeptic's original tests pass |
| groups-paths P2 LOW (rollback copy at rest) | Fixed (case D passes) |
| derived-copies P2 HIGH (64-word prefilter) | Fixed: full stored-row scan; 15,928 adversarial filter comparisons, 0 differences |
| derived-copies P1 MEDIUM-HIGH (40-character prune line) | Fixed for W's beginning; a middle piece is still open (N3 below) |
| derived-copies P3 / O-D4 (16 characters, generic phrases) | Done under O-D4; 11,565 random pieces, none of W escaped |
| releases N-A HIGH (stale "none") | Fixed; the skeptic could not refute it |
| releases N-B HIGH (released row not re-held) | Fixed; the skeptic could not refute it |
| N-C / O-CI (case-sensitive matching) | Done; open edge N5 below |
| Seed 6725 (`mark_exemptions`) | Fixed on the marking side; the reading side is still open (F1 below) |
| `dec_json_off` gap | Now CAUGHT (7 tests) |
| Seed 13465 (new in this round: second-withhold leak) | Found and fixed |

## 3. Test gaps found, and how each ended
- **`test_brain_resolve_c_062` listed product states that were out of date.** Two new states had not been added. The file is now in a new lane, g15, and checks those states too.
- **20 mutants survived on f7 alone.** 10 new closing tests and the full runs of existing files close 12 of them. 6 are compensated. 2 are UNRESOLVED (section 1).
- **Four earlier mutation patches no longer matched f7's text.** They were rewritten for f7.
- **Coverage gaps in the second-withhold oracle arm, named by the skeptic.** It does no Hermes operation, restore or rollback between the review and the second withhold. It never imports two decisions files in a row. It never picks a W2 shorter than 16 characters. It never tries an untyped release. The skeptic's targeted tests cover the restore and two-file gaps, and both pass.

## 4. Changes in this round (f6 → f7b)
**Product:**
- `groups.py`, `review.py`, `importer.py`, `identity.py` and migration `0008` (R9 repair).

**Behaviour:**
- **Markerless children:** they join their parent's group, from the parent down only.
- **A new `uncertain` state:** a parent that does not resolve, or conflicting parents. Uncertain rows are held, never one-click, and the export waits.
- **Full stored-row scan:** the 64-longest-words prefilter is gone.
- **Case-insensitive matching (`fold`):** NFKC, casefold and Turkish i.
- **Generic list `generic-v1`** for O-D4.
- **Prune line:** Hermes's generic prune line counts as a cut.
- **Two new hold states, `later_withhold` and `other_not_compared`:** "none" now needs every withheld text in hand.
- **Released rows** are re-checked for later withholds.
- **`mark_exemptions`** now matches `decided_on` and kind.

**Oracle:**
- a second-withhold arm, with 14 recorded seeds;
- three narrowings (13327, 13407, 13364), each with a mutant that loosens it, and each such mutant is killed.

**Other:**
- **Dashboard and docs:** release notes, spec and the Windows guide.
- **Deviation log:** entries 392–436.

## 5. Post-fix regression (fresh, on f7 and f7b)
| Run | Result |
|---|---|
| Lanes g1–g5, g8–g15 | g1 206, g2 110, g3 236, g4 180, g5 68, g8 20, g9 13, g10 8, g11 17, g12 36, g13 15, g14 7 passed; g15 126 passed on f7b |
| Property (g7) | 129 passed, 1 skipped |
| Unit (g6) | 715 passed, **6 not run in their required environment** (`test_brain_ops` package, `test_gate` ×4, `test_release_version`). These are not counted as passed. |
| Dashboard | Vitest 257/257 passed; `tsc` clean |
| Recorded seeds (70 plus 14 second-withhold) | arm 0: 84/84; arm 1: 84/84 |
| Fresh seeds 13000–13299 | 300/300 |
| Fresh seeds 13300–13599 (new range) | **7 failed:** 13426, 13500, 13532, 13546, 13560, 13582, 13590 |

**All 7 failures fail the same way on f6's product,** so none was introduced by this round. Root causes, as far as they have been traced:
- **13546, privacy leak.** A short W1 (under 16 characters of its own words), quoted with Hermes's `> ` markers in a `session_search` result outside every group, is readable again after a rollback. The cause: `_wrapped_whole` never reads the text without its quote markers. A fix is proposed for the product and the oracle together.
- **13426 and 13532, B7.** A tool row released for W1 has a same-uid twin that is held and kept for W2. It is readable in the normal flow and held after a rollback. No withheld words are readable in either state. This needs an owner decision.
- **13500, B4 overreach after restore.** Fails closed (it holds more than it should).
- **13560, B1 durability.** Three stored rows of the withheld conversation disappear when live imports a later backup with its decisions file. Not yet root-caused.
- **13582, evidence mismatch.** The product records `part_of` where the oracle finds W absent. Not yet root-caused.
- **13590, B6 gate.** One-click releases ran while a withheld text was missing; no freed row was found related to it. Not yet root-caused.

**Regression result: FAIL.**

## 6. The three independent skeptic reviews (on f7b)
**groups-paths:**
- **A, HIGH (a group row is readable).** A sub-agent's rotation from an older Hermes inherits `_delegate_from: P` with no `_compressed_from`. Hermes reads it as a continuation, but `source.py` classifies it as `delegated`, so D's rows stay readable. The fix direction is tried in `probe-fixA-source.diff`: a marker counts as a fork only when it names the parent.
- **B, MEDIUM, owner question on O-D1.** An older CLI branch's parent keeps the identical originals readable.
- **D, MEDIUM, owner question.** A pre-marker gateway reset leaves P readable.

  B and D follow from the parent-to-child-only rule. The skeptic's fail-closed suggestion is to hold the parent of a markerless group child as `uncertain`.
- **Held:** C, E, F (O-CI), G (O-D4) and the controls. No weakened test was found. 5 of the 6 mutants it ran were caught; 1 was compensated.

**derived-copies:**
- **N1, MEDIUM-HIGH.** An apostrophe or a line break inside Hermes's quoted f-string summaries (`vision_analyze`, `web_search`, `search_files`, `delegate_task`) hides W. There are 40 such generated lines.
- **N2, MEDIUM-HIGH.** W three JSON levels deep (a `session_search` read of an MCP row) is not read. The product decodes 2 levels and the oracle 3, and no fixed depth is enough.
- **N3, MEDIUM, owner question.** A generic prune line whose value is a verbatim middle piece of W. This is close to R5b.
- **N4, LOW-MEDIUM.** The `execute_code` summary cuts a `repr()` escape in half.
- **N5, LOW.** `fold` does not remove invisible formatting characters such as the soft hyphen or zero-width characters.
- **Trial fix** (`trial-fix-N1-N2.diff`, diagnosis only): it closes N1 and N2, and g3 stays clean.
- **Held:** O-CI and O-D4 as built, and every other Hermes summary shape tested.

**releases-oracle:**
- **F1, MEDIUM.** `exempt_covers` takes "trusted?" and "covers this content?" from different releases. An unchecked release can therefore borrow trust from another backup's typed release. This is the 6725 class again, on the reading side; deviation 391 had predicted it. A minimal fix is in `f1-proposed-fix.diff` (103 passed). The local `exempt()` in `group_rows` ignores trust as well.
- **Held:** N-A and N-B could not be refuted in any shape tried. Restore with the removals-only file, two decisions files on live, `session_search` carriers, and five direct-API bodies (all 400) were all tested.
- **Also checked:** no weakened test or property was found, and the simulator still conforms to Hermes (47 passed).

## 7. Unresolved privacy, security and data-isolation failures
**Confirmed leaks:**
1. groups-paths **A, HIGH:** inherited delegate marker.
2. derived **N1, MEDIUM-HIGH:** quoted f-string summaries.
3. derived **N2, MEDIUM-HIGH:** nested JSON depth.
4. **Seed 13546, MEDIUM:** short quoted W after a rollback.
5. releases **F1, MEDIUM:** trust borrowed across backups.
6. derived **N4, LOW-MEDIUM:** `repr` escape cut.
7. derived **N5, LOW:** invisible formatting characters.

**Need an owner decision** (each leaves text readable today):
- B and D: the parent side of O-D1;
- N3 and R5b: middle pieces of W;
- 13426 and 13532: same-uid twins across two withholds.

**Not yet root-caused:** 13560 (durability), 13582, 13590.

**Unresolved mutants:** `quick_no_snapshot`, `imp_stored_none_every_off`.

**Remaining deployment blockers:**
- everything above;
- the 6 environment-dependent tests;
- run 12 (2,000 scenarios), packaging and the CRLF fix, upgrade tests, the Windows simulation and brain-check;
- the backup-import fallback stays unapplied (owner decision).

**Costs added in this round** (accepted for privacy, to be reported to the owner):
- the full stored scan takes about 50 seconds per 100,000 rows;
- "none needs every text": once JARVIS deletes a withheld conversation for good, its holds need a keep or a typed release before the export.

## 8. Commits, pushes, releases, installs, deployments
None. Checked with git:
- the main checkout is at 7e985d7;
- `brain-062` is at fb17528, with the candidate left uncommitted by design;
- `knowledge-center-dev` is at 7834d1c;
- `origin` refs are unchanged;
- the reflog has no entry since 2026-10-20.

Nothing was installed, deployed or published. The backup-import fallback stays unapplied. Two container restarts interrupted step 3; it was resumed from its own edits, with no duplicated deviation entries (412–427, each once).

These runs show the listed properties on the scenarios tested; they do not prove that no leak remains.
