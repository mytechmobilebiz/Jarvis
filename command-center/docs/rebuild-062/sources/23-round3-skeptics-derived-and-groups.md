## Verdict
I found one new privacy leak in my lens (F1, MEDIUM-HIGH), so 0.6.2 stays NO-GO. I also found one new over-holding problem (F2, LOW) and three mutants that no test catches (M1). These tests show the shapes named here. They do not show that no other leak remains.
- I did not edit `integ/cand8`. At the end, `diff -rq` of cand8 against `rvC3/f8d` was empty (rc=0).
- No test or property was weakened.

S = `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad`, E = `S/rvC3/sk/skC3-derived-copies/evidence`.
- I worked on a tar copy of the final freeze f8d. It matched its manifest (4ee4844c…), and cand8 matched f8d.
- Progress file: `S/rvC3/skC3-derived-copies-progress.md`.

## New problems

**F1, MEDIUM-HIGH (privacy, new; not N3/R5b): Hermes's `session_search` search snippet keeps W readable outside every group.**
- **Hermes source:**
  - `tools/session_search_tool.py` `_discover`/`_hydrate_hit` returns, for its top hit, the FTS5 snippet. The snippet comes from `hermes_state_search.py:938`: `snippet(...,'>>>','<<<','...',40)`.
  - It also returns the hit message's content cut to 4000 characters plus "…" (`_shape_message`).
- **When it leaks:** the hit is a long message that quotes W after its 4000th character, such as a long compaction summary whose verbatim user section comes last.
  - Then the snippet is the row's only copy of W: W's own words with `>>>`/`<<<` around the search terms.
- **Why it is missed:** the marks break `contains`, and a highlight mark is not a cut mark. The product and the oracle both find no relation, so seeds cannot catch it.
- **What stays readable:** W's beginning before the first mark (56-85 characters for WA, WD and WN). For a long W (WL) it is a 160-180 character run.
- **Controls:** the same search with a short summary, and the same W in Chinese, are held.
- **Integration failure:** `E/test_skc3_derived_adv.py::test_skc3_a_session_search_snippet_of_w_with_highlight_marks_is_held_outside_every_group[apostrophe|umlaut|line_breaks|long]`, line 106, `assert set(seen.values()) <= {"held", "kept"}, seen`. Each gives `{'withhold': 'readable', 'settle': 'readable', 'later import': 'readable', 'live': 'readable'}`.
- **Unit failures** (`E/test_skc3_derived_unit.py`):
  - line 28, `assert I.carrier_relations([TEXTS[cid]], WS[...])` (product)
  - line 34, `assert o.carrier_relations(...)` (oracle)
- **Possible fix (not tried):** read `>>>`/`<<<` as masks in `_pieces`/`_REDACTION_MARK`, or strip them from each reading, in both the product and the oracle.

**F2, LOW (over-holding, new; O-D4 and owner requirement 3): the decode cap fires on ordinary Hermes text.**
- **Shape:** a `session_search` read of a conversation that itself held a read, five or six levels deep, of unrelated conversations. The text is about 3.4 KB and holds none of W's words.
- **What happens:** the product reaches `CANON_DEPTH` (64 steps). The open-literal reading of the cut, 2000-character inner content removes one quoted token per step.
- **Effect:** `canon_capped` is True, so the row is held as `not_checked` outside every group whenever any W is in hand.
- **Oracle mismatch:** the oracle is not capped on the same text and demands nothing. The owner's rule was "at the cap fail closed, the same in the oracle".
- **Integration failure:** `E/test_skc3_derived_adv.py::test_skc3_an_unrelated_nested_session_search_read_stays_readable[nest:U:5|nest:U2:5]`, line 142, `assert set(seen.values()) == {"readable"}, seen`, which gives `{'withhold': 'held', 'settle': 'held', 'later import': 'held'}`.
- **Unit failures** (`E/test_skc3_derived_unit.py`):
  - line 47, `assert I.canon_capped(...) == o.canon_capped(...)`
  - line 53, `assert I.carrier_relations([...], WS["WA"]) == set()`
- No leak here; it only holds too much. Any fix must keep failing closed when a cap is really reached.

**M1, mutation audit:** I ran 5 mutants that are not in the c3 matrix (`E/skc3_mut.py`).

| Mutant | Unit/property tests | 5 integration files | Leak it opens |
|---|---|---|---|
| `ign_hangul_fillers` | caught | not run | – |
| `summary_end_quote_only` | caught | not run | – |
| `unrepr_cut_keep` | 41 passed, survived | 79 passed, survived | `prev:WD:40-42`: a persisted-output preview cut inside W's `ß` escape (W's first 32 characters) |
| `open_literal_off` | 41 passed, survived | 79 passed, survived | the same `prev:WD:40-42` shape |
| `cut_per_line_off` | 41 passed, survived | 79 passed, survived | the inline fallback "…\n\n[Truncated: tool response was N chars…]" (`E/skc3_mut_probe.py`) |

- None of the three survivors is equivalent: each one opens a real W-prefix leak on Hermes's `tools/tool_result_storage.py` output. These are test gaps; f8d's product holds both shapes.
- On `prev:WD:40-42` the oracle also finds no relation (the product is stricter here).

## Owner requirements and decisions

| Item | Verdict |
|---|---|
| (1) Deleted or orphaned copies | Held as far as the candidate's suites test it (skc3dc-own below, including carriers_062). |
| (2) Derived content | **Not held for F1.** Held for every other Hermes shape I generated (214 texts, `E/eval-f8d.out`). |
| (3) Unrelated rows unchanged | **Not held for F2.** Held for 64 realistic unrelated Hermes texts and 4,000 random ones (0 held). |
| (4) Releases | Outside my lens. |
| O-D1 | Not tested here. |
| O-CI | Held: 0 misses over 3 W × 8 copy forms (`E/skc3_unicode_od4_probe.out`). |
| O-D4 | 16-character minimum respected. Over-holding: F2. |

The shapes behind requirement (2), all from the vendored Hermes:
- clarify summary with elision, one and two answers
- a FAILED suffix holding Python's `str(dict)` of W
- generic prune lines with list and dict values
- nested reads 1-8 levels deep
- large-result previews cut at 17 positions into W, including German and Chinese JSON with `ensure_ascii`

Cuts under 32 characters are residual R5 and were not counted.

The O-CI copy forms were JSON (ensure_ascii on and off), repr(), the vision_analyze summary line, upper case, NFKC, ignorables removed, and double JSON. The W texts used an emoji ZWJ sequence, VS16 and a soft hyphen, Turkish İ, and fullwidth letters.

## Pending owner decisions, not counted as new
- N3/R5b: the parts of F1's snippet after the first mark are middle pieces of W.
- I did not test groups-paths B/D or seeds 13426/13532.

## Derived3 open points rechecked
- W after the 20,000th JSON string, 1-3 levels deep, is held through the whole-text unescape (no leak).
- Hermes caps tool results at 100,000 characters (`tools/budget_config.py` `DEFAULT_RESULT_SIZE_CHARS`), so tool rows over 2,000,000 characters do not come from Hermes's tools.

## Adversarial cases built from the vendored Hermes source
- `E/skc3_hermes_gen.py`: FTS discovery through Hermes's `SessionDB` and `session_search`, the large-result preview, nested reads, clarify/FAILED/prune lines. The clock is deterministic.
- `E/skc3_hermes_unrelated_gen.py`: unrelated web, MCP and nested reads, and previews.
- `E/test_skc3_hermes_regen.py`: regenerates the fixture `E/skc3_hermes_texts.json` and compares it; it passes.

## Commands and summary lines
Each run used `ROOT=<copy> [HERMES_SRC=S/hermes-src/repo REQUIRE_HERMES_SRC=1] bash S/integ/fx/run.sh …`.

| Label | What ran | Result |
|---|---|---|
| skc3dc-unit | my unit file and the regen test | "12 failed, 3 passed in 13.18s" (2 controls and the regen pass) |
| skc3dc-int | my integration file | "6 failed, 2 passed in 29.69s" (2 controls pass) |
| skc3dc-own | 9 candidate derived-lens files | "167 passed in 230.66s" |
| skc3dc-m-* | the mutants | results above |

Logs are copied into E.

## Tests to bring into the candidate (cleaned harness, assertions unchanged)
- `E/test_skc3_derived_adv.py` → `tests/integration/`, with `skc3_hermes_texts.json`
- `E/test_skc3_derived_unit.py` → `tests/unit/`
- `E/test_skc3_hermes_regen.py` and `skc3_hermes_gen.py` → `tests/property/`
- For M1: new pins on `prev:WD:40-42` and on the inline-truncation shape.

## Cleanup
- I dropped my databases `cc_test_rd_fx{skc3u,skc3i,skc3o,skc3munreprcutkeep,skc3mcutperlineoff,skc3mignhangulfillers,skc3msummaryendquoteo,skc3mopenliteraloff}` and their `_cr` twins.
- 20 other `cc_test_rd_fxskc3*` databases belong to the other skeptics and were left alone.
- I deleted the scratch copy. Evidence (about 1 MB) is in E. Nothing was committed, stashed, reset or pushed.=========
## skC3-groups-paths: skeptic report on f8d (round 3)

**Verdict: refuted.** A withheld group's rows are still readable on one new path. When a conversation is orphaned (its parent row is gone from Hermes) and it carries a fork marker, groups.py turns copied-history matching (J8) off for it. Both ways in tested:

- an older Hermes's rotation that inherited `_delegate_from`;
- Hermes's own schema migration v16, which tags orphans `_delegate_from: '__orphaned__'`.

The fault fails the same way on f7b, so round 3 did not introduce it; round 3's fix for A does not cover it. 0.6.2 should stay NO-GO. These runs show only the listed properties on these scenarios; they do not prove that no leak remains.

I did not edit `integ/cand8` or any frozen tree. `sha256sum -c` against the f8d manifest returns rc 0 on both `integ/cand8` and `rvC3/f8d`. Nothing was committed, stashed, reset or pushed. I did not touch the 0.5.2 Knowledge Library work, kc-test-pg, or the deviation log (as a skeptic I only report).

S below means `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad`.

### Base
- I made a tar copy of `S/rvC3/f8d` at `S/rvC3/sk/skC3-groups-paths/f8d` (now deleted). `sha256sum -c --quiet S/rvC3/f8d-MANIFEST.sha256` returned rc 0 over 476 files.
- Every run used `ROOT=<copy> bash S/integ/fx/run.sh <label> <db> -rfE -p no:randomly <files>`. Logs are in `S/integ/fx/logs/<label>.txt`.

### Re-run of the earlier groups-paths skeptic tests
Run `skc3-gp-prev` covered `test_skc2_groups_paths_adv.py`, `test_skC_groups_paths_adv.py` and `test_skC_legacy_adv.py`: **"2 failed, 13 passed in 21.67s"**.
- **A now passes** in both forms (the pre-marker rotation and the marked-rotation control).
- **C, E, F (O-CI), G (O-D4) and the round-1 skC tests pass.**
- **B fails at `test_skc2_groups_paths_adv.py:144`** (`assert twins == []`). This is owner-pending; I do not count it as new.
- **D[legacy_reset] fails at line 201** (`assert await _readable(p) == []`). Also owner-pending, not counted as new.

The same three files plus the group files (groups, restore_c, known_failures_c, d1, d1_legacy, resolve_c, mutation_closure_c, groups_inherited, mutation_closure_r2, `unit/test_brain_source.py`, `unit/test_brain_source_markers.py`) ran as `skc3-gp-base-lanes`: **"2 failed, 178 passed in 205.12s"**. The only failures are B and D.

### New adversarial cases (from the vendored Hermes)
File: `S/rvC3/sk/skC3-groups-paths/evidence/test_skc3_groups_paths_adv.py`.
- On f8d (`skc3-gp-adv2`): **"6 failed, 7 passed in 20.81s"**.
- On a tar copy of f7b (`skc3-gp-adv-f7b`, manifest rc 0): **"6 failed, 7 passed in 19.99s"**, the same six.

**N1, HIGH (same class as round 2's A: rows of a withheld group are readable).** An orphaned rotation of a sub-agent with the inherited `_delegate_from`.
- **Shape:**
  - The data comes from an older Hermes with no uids: P has a sub-agent D (`_delegate_from: P`) that holds the private answer, and the Command Center imports backup b1.
  - Hermes then rotates D into D2. D2's model_config copies `_delegate_from: P` and has no `_compressed_from` (Hermes's own "inherited-delegate" shape, `tests/hermes_state/test_runtime_destructive_mutations.py::_delegate_topology`).
  - D then leaves Hermes and D2's parent becomes NULL. The simulator's `delete_session` models this. In the current Hermes it also happens through:
    - `delete_moved_session` (`sessions_repair_profiles`: "children left behind are detached");
    - session_recovery's `_reconcile`;
    - lost-and-found's dangling-parent repair.
  - The current `delete_session` would cascade D2 with D instead.
- **Root cause:**
  - `source.py:511`: with a NULL parent, any fork marker counts ("as in Hermes"). D2 is therefore read as `('delegated', P)`.
  - `groups._Graph.unanchored` (around line 377) returns False for NOT_COPYING lineage, and the marker-named parent P also anchors D2. J8 never runs for D2, although its real parent is gone.
  - No link D↔D2 was ever recorded, because D2 was first seen already orphaned.
- **Failing tests:**
  - `test_skc3_n1_..._is_one_conversation_with_its_parent[inherited_delegate-w_in_orphaned_rotation]`, line 135: `assert await _readable(other) == []`. D's 4 rows are readable, including the private answer; the group is {D2: withheld_here}.
  - `[inherited_delegate-w_in_stored_parent]`, line 135: W1 is in D, and D2's 5 rows (copies of D's head and tail, the summary, its own turn) are readable.
  - `test_skc3_n1b_live_with_extra_history_holds_the_stored_parent`, line 153: on live (earlier history b1, then b2 with the decisions file), D's rows are readable.
- **Controls pass:** the same orphan with `_compressed_from` (J2), and the same orphan with no marker (J8 joins it), in both directions.

**N2, MEDIUM (rows of a withheld group are readable).** Hermes schema data migration v16 (`hermes_state_schema.py`, `current_version < 16`, second statement).
- **What v16 does:** it writes `_delegate_from: '__orphaned__'` onto an orphan that has no title, at most 25 messages, a tool row, no children and no `_delegate_from`/`_branched_from`.
- **Shape:** an untitled, tool-using pre-marker rotation C2 whose parent C was deleted. The test runs v16's SQL verbatim. C2 is then read as 'delegated' (J8 off) under a parent that does not resolve.
- **Failing test:** `test_skc3_n2_...[w_in_orphaned_rotation-hermes_v16_orphan_tag]`, line 195. C's 6 rows are readable, and the archive holds C2 as `('delegated', '__orphaned__')`.
- **Passing:** the no-migration control. With W1 in the stored parent C, the test also passes in both variants: C2 is 'uncertain' and held.

**N3 (owner-pending class B/D, report only, not counted as new).** A pre-marker rotation C2 whose parent C was reopened (`reopen_session` clears end_reason) is a markerless child joined from the parent down only. C stays readable.
- **Failing:** `[markerless]` and `[hermes_v16_delegate_tag]`, both at line 222.
- **For the owner's B/D decision:** v16's first statement tags exactly such children `_delegate_from: <parent>` (`_ephemeral_child_sql`). A rule limited to "markerless" children would therefore miss them once Hermes has upgraded.

**Diagnostic probe** (scratch copy only, not a proposed patch): `evidence/probe-orphan-fork-marker-groups.diff`. In `groups.py`, a parent named only by a delegate reading no longer anchors J8, and the NOT_COPYING exclusion is kept for resets.
- A first version that also covered resets broke `test_brain_mutation_closure_c_062.py::test_a_reset_with_copied_looking_rows_never_joins_by_copied_history`: its how became 'copied_rows' instead of 'uncertain' (held either way). I narrowed it.
- **New tests + group selection** (`skc3-gp-probe2`): "3 failed, 190 passed in 218.05s". Only B, D and N3[markerless] remain; N1, N1b and N2 pass.
- **Lanes:** g3 "236 passed, 6 warnings", g12 "36 passed", g8 "20 passed", g13 "15 passed".
- **Remaining fix direction:** for an orphan (Hermes parent NULL), do not let a delegate marker or a delegate lineage switch off copied-history matching. Alternatively, treat such an orphan as uncertain.

### Owner requirements and decisions
| Item | Status |
|---|---|
| No group row or title readable (test copy, live, later backups) | **Not held:** N1 (test copy and live with extra history) and N2 |
| Hermes lineage markers (inherited vs fork, rotations, resets, sub-agents, branches, resumed parents) | Held for every non-orphan shape tested (A, the step-1 inherited files, `test_brain_source_markers`). **Not held** for an orphan with an inherited or v16 fork marker |
| Uncertain links held, never one-click, export waits | Held in the d1 suite, in N2 with W1 in C (C2 is uncertain), and in the mutation results |
| Unrelated conversations unchanged | Held: controls and g3 pass |
| O-D1 | Held as written; B, D and N3 are its pending parent side |
| O-CI | Held (F, the existing suite) |
| O-D4 | Held (G) |
| Rollback, restore, legacy reset | Not newly broken on these paths (the C, E and d1 restore tests pass); N1 is already open before any of them |

**Weakening:** I found no weakened test or property. The groups tests I ran keep their assertions, and skeptic A's case is carried into `test_brain_groups_inherited_062.py` with its assertions unchanged.

### Mutation audit (`evidence/mut3.py`, `evidence/mut3-results.tsv`)
One guard at a time on a tar copy of f8d (now deleted).

| Mutant | Result | Classification |
|---|---|---|
| `ga_src_fork_binding_off` | "1 failed" (`test_brain_source_markers[inherited_branch]`) | CAUGHT |
| `ga_src_copied_fallback_off` | "1 failed" | CAUGHT |
| `m18_j8_always_two` | "1 failed" | CAUGHT |
| `gd1_uncertain_off` | "1 failed" | CAUGHT |
| `sk3_archive_parent_edge_off` (new) | "1 failed, 17 passed" | CAUGHT |
| `sk3_src_orphan_any_marker_off` (new) | "1 failed, 156 passed" (`unit/test_brain_source.py::test_sessions_lineage_channels_and_time_order`) | CAUGHT (by a unit test only) |
| `sk3_graph_marker_edges_off` (new, groups.py:352, the backup's marker edges) | "181 passed in 198.81s" | **SURVIVED** |

- My four re-runs agree with the consolidation matrix's classification.
- **The surviving mutant, `sk3_graph_marker_edges_off`, is a LOW test gap.** I could not show that something compensates for it:
  - with `gd1:marker_links_off` (the importer's recorded marker links) added as well, the double mutant fails `test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins`;
  - but per the matrix that test already kills `gd1:marker_links_off` alone, so the double proves nothing.
- The edge is probably covered by the backup's parent edge and the recorded links. No test pins it alone.

### Cleanup
- **Scratch copies:** all four deleted (f8d, probe, f7b, mut).
- **Databases:** every `cc_test_rd_fxskc3*` database I created is dropped (count 0).
- **Evidence kept** in `S/rvC3/sk/skC3-groups-paths/evidence/`: `test_skc3_groups_paths_adv.py`, `probe-orphan-fork-marker-groups.diff`, `mut3.py`, `mut3-results.tsv`.
- **Progress file:** `S/rvC3/skC3-groups-paths-progress.md`.
Shell cwd was reset to /home/user/Jarvis