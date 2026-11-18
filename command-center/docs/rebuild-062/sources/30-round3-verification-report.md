# 0.6.2 Option C, fix round 3 (workflow wf_3fd1eaeb-b75): verification report

**Date:** 2026-11-11.

**Verdict: NOT PASS. 0.6.2 stays NO-GO.**

The owner's PASS rule needs three conditions:
1. every privacy-critical mutation is CAUGHT or COMPENSATED with reproducible evidence;
2. no critical privacy failure remains;
3. the post-fix regression passes.

| Condition | Result |
|---|---|
| 1. Mutations | **Holds for the matrix:** 158 rows, 151 CAUGHT, 7 COMPENSATED, 0 UNRESOLVED. **Does not hold overall:** the skeptics ran 3 privacy-relevant mutants outside the matrix that no test catches (section 1). |
| 2. No critical privacy failure | **Does not hold:** 2 new HIGH leaks, 1 MEDIUM-HIGH and 1 MEDIUM, plus seed 13639 (section 7). |
| 3. Post-fix regression | **Does not hold:** 8 of the 900 fresh seeds fail. 7 of them fail the same way on f7b; 13639 is a gap that this round's correct fix exposed. |

**What the round achieved:**
- All 7 confirmed round-2 leaks are fixed and independently confirmed: A, F1, 13546, N1, N2, N4 and N5.
- The 4 un-root-caused seeds pass: 13500, 13560, 13582 and 13590.
- Both UNRESOLVED mutants are closed.

None of the new leaks was introduced by round 3. Each new one fails the same way on an f7b copy. The skeptics found them by building cases from the vendored Hermes source that the simulator never generates.

**Final candidate:** `integ/cand8` = f8d (`rvC3/f8d`). Manifest SHA-256 `4ee4844cd85e59e9600a75c031d3c2d590b3cb349402057264199b6479ec21ce`, 476 files. f8d verifies against its manifest, and cand8 equals f8d (`diff -rq` is empty). No product file differs between f8 and f8d: f8b, f8c and f8d add only tests and oracle changes.

**Agent reports:** `records/round3/00–06*.md`. Order: grpA-trust, derived3, seeds-mut3, consolidation (resumed after container restarts and a spend-limit stop), then 3 skeptics.

## 1. Baseline and mutation testing
**Baseline:** f7b (`2c6c2f83…`). It still verifies. The diff from f7b to f8d covers 26 files: 9 changed and 17 new.

**Mutation closure matrix** (`conv/mutation-closure-matrix-f8.md`; one guard at a time, each on its own copy and databases, all on f8d):

| Class | Count |
|---|---|
| CAUGHT | 151 |
| COMPENSATED | 7 (each with a failing double mutant) |
| UNRESOLVED | 0 |

The 158 rows are: the 101 f7 mutants, 7 doubles, 32 mutants from steps 1–3 and 18 new `c3:` mutants. The driver's dry run found 0 problems and there were no invalid runs.

**Caveats, as stated in the matrix:**
- `cr2:quick_no_snapshot` is COMPENSATED only through an invariant. Its doubles fail only the tests that pin the invariant. A probe found no effect on behaviour even with the invariant broken (710 carriers, 0 skipped).
- `c2:imp_stored_none_every_off` is CAUGHT only by a unit test that builds a crafted state.

**Independent audit (releases skeptic):** it re-ran all 108 privacy-critical singles and the 7 doubles. Its classification matches the matrix on all 115.

**Survivors found outside the matrix:**
- **derived-copies skeptic:** 3 mutants survived, `unrepr_cut_keep`, `open_literal_off` and `cut_per_line_off`. Each passed 41 unit and property tests and 79 integration tests. None is equivalent: each opens a real leak of W's beginning on output from Hermes's `tool_result_storage.py`. f8d's product holds both shapes, so these are test gaps, not leaks.
- **groups-paths skeptic:** `sk3_graph_marker_edges_off` (groups.py:352) passed 181 tests. LOW. The guard is probably also covered by other edges, but no test pins it on its own.

## 2. Round-2 problems: status
| Round-2 item | Status (independently confirmed) |
|---|---|
| groups-paths A, HIGH (inherited delegate marker) | Fixed: a fork marker counts only when it names the session's own parent. The skeptic's test passes in both forms. |
| releases F1, MEDIUM (trust borrowed across backups) | Fixed: `exempt_covers` reads trust only from releases that cover the stored content. `group_rows` now loads trust. The round-2 skeptic file gives 17 passed. |
| Seed 13546, MEDIUM (short quoted W after a rollback) | Fixed: W is also read with the quote markers removed, in the product and the oracle. |
| derived N1, MEDIUM-HIGH (quoted f-string summaries) | Fixed with quote spans |
| derived N2, MEDIUM-HIGH (nested JSON depth) | Fixed: iterative decode with caps of 64 steps and depth 32 and a size budget; a capped row fails closed as `not_checked`. One over-hold remains, F2 below. |
| derived N4, LOW-MEDIUM (repr escape cut) | Fixed |
| derived N5, LOW (invisible characters) | Fixed: `fold` removes the 4,174 default-ignorable code points |
| 13500, 13560, 13582, 13590 | Root-caused and fixed: `_w_text` "same message id", seeding from `groups.seeds`, oracle `rekeyed`. All 4 pass in both arms (also confirmed by the releases skeptic). |
| `quick_no_snapshot`, `imp_stored_none_every_off` | No longer UNRESOLVED (caveats in section 1) |
| 13426, 13532, groups B and D, N3 / R5b | Still pending owner decisions. Unchanged and not counted as new. |

## 3. Test gaps found, and how each ended
- **`c3:spans_per_line_off` survived on f8b** (54 passed). A closing test was added in f8c, and it is now CAUGHT.
- **The oracle had no reading of quote spans.** It flagged correct product evidence on 13154 and 13774. Oracle change 1 widens only what a hold may show; deviation 470.
- **The oracle compared released rows with the raw text.** It flagged 13763, 13863 and 13879, which fail identically on f7b. Oracle change 2 (`cc_redacted_of`); deviation 471.
- Each oracle change has its own self-tests (6 and 4) proving real violations are still reported. The skeptics found no weakened test or property: assert counts are equal from f7b to f8d.
- **Gaps that stay open:**
  - The oracle does not demand N1 spans or the N4 escape cut.
  - The simulator never writes an inherited fork marker, never deletes W2's conversation between two decisions files, and never generates `session_search` highlight snippets.
  - So the seeds do not exercise fix A, N1 or N4. All three new leaks sit in these blind spots.
- **The skeptics' tests are kept as evidence and not yet brought into the suite:**
  - `rvC3/sk/skC3-derived-copies/evidence/test_skc3_derived_adv.py` and `test_skc3_derived_unit.py`
  - `rvC3/sk/skC3-groups-paths/evidence/test_skc3_groups_paths_adv.py`
  - `rvC3/sk/skC3-releases-oracle/evidence/test_skc3_rel_adv.py`

## 4. Changes in this round (f7b → f8d)
**Product:** `source.py`, `identity.py`, `importer.py` and `review.py`.
- **Lineage:** `FORK_MARKERS` and `_markers()`.
- **Trust pairing:** `releases_covering_stored`.
- **Derived copies:** text read without quote markers, quote spans, the capped iterative decode, the repr cut and removal of invisible characters.
- **Seeds:** `_w_text` and seeding from the withhold.

**Cost:** about 1.12 ms per stored row, up 43%. The full scan is now about 112 s per 100,000 rows.

**Oracle:**
- `fwords`, `kept_carrier`, `rekeyed`, `quote_spans` / `span_relations`, `cc_redacted_of` and the decode cap.
- 10 new recorded seeds, for 94 in all.

**Tests:** 17 new test and generator files.

**Docs:** `JARVIS_BRAIN_SPEC.md` and `RELEASE_NOTES_0.6.2.md`.

**Deviation log:** entries 437–476.

## 5. Post-fix regression (fresh, on f8d)
| Run | Result |
|---|---|
| Lanes g1–g5, g8–g15 | g1 206, g2 110, g3 236, g4 180, g5 68, g8 20, g9 13, g10 8, g11 17, g12 36, g13 15, g14 7, g15 116 passed; the new round-3 integration files 51 passed |
| Property (g7, real Hermes source required) | 168 passed, 1 skipped (an empty parameter list) |
| Unit (g6) | 783 passed, **6 not run in their required environment** (`test_brain_ops` package, `test_gate` ×4, `test_release_version`). These are not counted as passed. |
| Non-brain integration | 186 passed, 1 failed. The failure is `test_approvals_050` ads, which depends on the date and fails identically on f7b. Not counted as passed. |
| Dashboard | Byte-identical to f7b (Vitest 257/257 and `tsc` clean on f7b) |
| Recorded seeds (94) | arm 0: 94/94; arm 1: 94/94 |
| Fresh seeds 13000–13899 (second-withhold arm, 900 seeds) | **8 failed:** 13426, 13532, 13621, 13639, 13691, 13704, 13754, 13825 |

| Seed | Status | Cause |
|---|---|---|
| 13426, 13532 (B7) | identical on f7b; owner-pending | same-uid twin across two withholds |
| 13639 (B9, C3), **privacy** | exposed by step 3's correct fix | W's conversation is deleted and only a same-uid copy remains. W's absorbed set is read only from W's own row, so W is not in hand. Carriers outside the group stay readable: a short reply, its title and a memory item. Needs W's absorbed set recorded at withhold time, which is a design change (deviation 472). |
| 13704 | identical on f7b | probably the same family as 13639; not debugged |
| 13621 (A1, A4, A6) | identical on f7b | a lineage through a gateway branch that no backup ever held. Owner question: is such a link in the contract? (deviation 473) |
| 13691 (A3) | identical on f7b | fails closed (holds more than it should) |
| 13754 (B7) | identical on f7b | the same shape as 13426 and 13532 |
| 13825 (B7) | identical on f7b | possible durability gap: after restore, the removals-only file and a re-import, 3 pending rows become readable. Not debugged. |

**Regression result: FAIL.**

## 6. The three independent skeptic reviews (on f8d)
**groups-paths: refuted.**
- **N1, HIGH (group rows readable).** An orphaned sub-agent rotation that inherited `_delegate_from` from an older Hermes; its parent is gone from Hermes.
  - The cause is in `source.py:511`: with a NULL parent any fork marker counts, so the rotation is read as delegated.
  - Copied-history matching (J8) then never runs for it.
  - The withheld group's rows stay readable on the test copy and on live.
- **N2, MEDIUM.** Hermes's own schema migration v16 tags orphans `_delegate_from: '__orphaned__'`, with the same effect.
- Both are identical on f7b; round 3's fix for A does not cover orphans. A diagnostic probe (`probe-orphan-fork-marker-groups.diff`) closes N1 and N2 and keeps g3, g8, g12 and g13 green.
- **N3 is the pending B/D class.** For the B/D decision: v16 tags such children `_delegate_from: <parent>`, so a rule that only looks at markerless children would miss them after Hermes upgrades.
- **Held:**
  - A, C, E, F (O-CI), G (O-D4) and every non-orphan lineage shape;
  - uncertain links are held, never one-click, and the export waits;
  - unrelated conversations are unchanged.

**derived-copies:**
- **F1, MEDIUM-HIGH (readable outside every group).**
  - The shape: Hermes's `session_search` returns an FTS5 snippet with `>>>`/`<<<` around the search terms, and the hit message's content cut at 4,000 characters.
  - When W sits past character 4,000 of a long message, the snippet is the only copy, and the marks break matching in the product and the oracle alike.
  - Readable: 56–85 characters of W's beginning; for a long W, a run of 160–180 characters.
  - Fix direction (not tried): read the highlight marks as masks.
- **F2, LOW (over-hold).** An ordinary nested `session_search` read five or six levels deep reaches the 64-step decode cap and is held as `not_checked`. The oracle is not capped there, which breaks the owner's "same in the oracle" rule.
- **Held:** 214 generated Hermes texts, 64 realistic unrelated texts and 4,000 random ones (0 held), and O-CI and O-D4.
- **Decode limits rechecked:** a W after the 20,000th JSON string is still held. Hermes caps tool results at 100,000 characters, so the 2,000,000-character limit is not reached by Hermes's own tools.

**releases-oracle:**
- **N1, HIGH (a withheld text becomes readable).** `mark_later_withhold` (the round-2 N-A fix) is called only from `withhold_message`. Two paths put a later withhold in place without it:
  - the live import of a decisions file;
  - restore with the removals-only file, when the withheld row is not stored here.

  Existing holds then keep a stale "none", stay one-click eligible, and are freed without RELEASE. X becomes readable with W2's words.
  - S1: live, file by file;
  - S2: `restore.ps1`;
  - S3, MEDIUM, with W2 in hand: the stale "none" is shown, and the N-B check re-holds X at the next import.

  Identical on f7b. A probe (`probe-S1-S2-fix.diff`) passes 5 tests and keeps the lanes green; fresh seeds were not run with it.
- **N2, MEDIUM, owner question.** A row released by one-click while every text was in hand stays readable after a later file brings a W2 that live never holds. "None needs every text" covers holds, not releases.
- **Held:**
  - the F1 trust fix;
  - the four root-caused seeds;
  - direct API bodies: every untyped body gets 400 or 422;
  - simulator conformance: 86 passed;
  - no weakened tests.
- **Not held:** B6 / O-D3.

## 7. Unresolved privacy, security and data-isolation failures
**Confirmed leaks (all fail the same way on f7b):**
1. groups-paths **N1, HIGH:** an orphaned rotation with an inherited delegate marker.
2. releases **N1, HIGH:** a stale "none" after a decisions-file or restore withhold (S1, S2; S3 MEDIUM).
3. derived **F1, MEDIUM-HIGH:** the `session_search` highlight snippet.
4. groups-paths **N2, MEDIUM:** Hermes v16's orphan tag.
5. **Seed 13639, privacy:** W not in hand after its conversation is deleted. Needs a design change. 13704 is probably the same family.

**Lower:**
- derived F2, LOW (over-hold at the decode cap);
- 4 test gaps (3 derived mutants, 1 groups mutant);
- 13825, a possible durability gap, not debugged.

**Need an owner decision:**
- groups B, D and N3 (the parent side of O-D1, now including Hermes v16's tags);
- N3 / R5b (middle pieces of W);
- 13426, 13532 and 13754 (same-uid twins);
- releases N2 (a release made before a later withhold);
- 13639's design: record W's absorbed set at withhold time;
- 13621: a lineage that no backup ever held;
- a reopened parent and its copied-marker child held both ways;
- the unproven "none" risk on the `withhold_message` copy path;
- costs: the full scan at about 112 s per 100,000 rows (+43% this round), and "none needs every text".

**Also open (entry 446):** the release-without-trust pattern in `archive._withheld_keys`, `later.load_family_readable`, the importer's `_readable_uids` and the preview lists. Low impact as far as the skeptic could see; each needs a regression test first.

**Remaining deployment blockers:**
- everything above;
- the 6 environment-dependent tests;
- run 12 (2,000 scenarios), packaging and the CRLF fix, upgrade tests, the Windows simulation and brain-check;
- the backup-import fallback stays unapplied (owner decision).

## 8. Commits, pushes, releases, installs, deployments
None. Checked with git:
- the main checkout is at 7e985d7, with the same 57 uncommitted files as before;
- `brain-062` is at fb17528, with the candidate left uncommitted by design;
- `knowledge-center-dev` is at 7834d1c;
- the `origin` refs are unchanged;
- the reflog has no entry since 2026-10-20.

Nothing was installed, deployed or published. The owner's running Command Center containers were not touched. The 0.5.2 package and its evidence were not touched. Every round-3 test database was dropped.

**Disk:** 3.6 GB free. **Cleanup candidates, not deleted:**
- the earlier freezes `rvC3/f8`, `f8b` and `f8c` (about 68 MB; f8d is final);
- the round-1 and round-2 trees `rv2`–`rv5` (about 2.8 GB);
- the image `kc052-sbom/backend:pkg` (369 MB).

These runs show the listed properties on the scenarios tested; they do not prove that no leak remains.
