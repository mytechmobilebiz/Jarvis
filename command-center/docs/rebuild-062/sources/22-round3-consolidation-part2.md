The g6 lane's "6 failed" are the six environment tests, never a killing test; the writer asserts that. Where a row shows only a lane label, its summary is as in section 3.

**Guards that only unit, lines or self-tests catch.** The oracle does not demand N1 spans or the N4 repr-escape cut, and the simulator never writes an inherited fork marker. So for those guards the seeds do not exercise step 1's fix (a), N1 or N4.

## 5. Open owner points
1. **Still pending, not implemented, skeptic tests kept out of the suite** (kept in evidence):
   - groups-paths B and D (parent side of O-D1).
   - Derived N3 / R5b: the 20 grid lines stay readable.
   - Seeds 13426 and 13532: identical problems on f8b and f8d, and identical to step 3's record.
2. **13639 (privacy) and 13704 (same family as far as seen).** W's absorbed set is known only from W's own backup row (deviation 346). When W's conversation is deleted and only a same-uid copy remains, W is not in hand, and carriers outside the group stay readable (R2 as the product sees it). The fix needs W's absorbed set recorded at withhold time, which is a design change (deviation 472).
3. **13621:** a lineage through a gateway branch that no backup ever held. Is such a link in contract? (deviation 473)
4. **13754, 13825 (B7), 13691 (A3, fails closed):** pre-existing, not changed. 13825 may be a durability gap; it was not debugged to code level.
5. **Carried over from steps 1-3:**
   - Entry 446: the release-without-trust pattern in `archive._withheld_keys`, `later.load_family_readable`, the importer's `_readable_uids`, and the preview `matching_rows`/`form_rows`. Also the reopened parent held both ways.
   - Entry 456: the 20,000-JSON-string and 2,000,000-character decode limits for carriers; scan cost +43%.
   - Entry 464: copies of a kept carrier, and the unproven 'none' risk on the 13500 path.
6. **`test_approvals_050` ads test:** date-dependent, outside the brain. Its owner should pin the date.

## 6. Final state of integ/cand8
- **Equal to f8d.** `diff -rq -x __pycache__ -x node_modules -x .pytest_cache integ/cand8 rvC3/f8d` is empty, and `sha256sum -c --quiet rvC3/f8d-MANIFEST.sha256` passes inside cand8.
- **Against f7b's manifest** it fails on exactly the 9 files listed in section 1, plus the 17 new test and generator files (no file removed).
- **Housekeeping:**
  - All `cc_test_rd_fxc3*` databases are dropped (count 0). No scratch tree copies remain; `rvC3/c3mut` is removed.
  - I kept the freezes f8, f8b, f8c and f8d, and the debug evidence in `rvC3/c3probe`.
  - Nothing was committed, stashed, reset or pushed. No other tree was edited, and the Knowledge Library work was not touched.

Key paths (under `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/`):
- `conv/mutation-closure-matrix-f8.md`
- `rvC3/f8d`, `rvC3/f8d-MANIFEST.sha256`
- `rvC3/f8d-vs-f7b.diff`, `rvC3/f8d-vs-f8.diff`
- `rvC3/consC3-progress.md`
- `rvC3/c3mut.py`, `rvC3/c3matrix.py`, `rvC3/c3mut-results.tsv`, `rvC3/c3matrix-compact.txt`
- `rvC3/c3-seeds94.txt`
- `rvC3/c3f-queue.sh`, `rvC3/c3g-queue.sh`, `rvC3/c3h-queue.sh`
- `rvC/verify/c3f8d-seeds-h0.out`, `rvC/verify/c3f8d-seeds-h1.out`
- `design-062-deviations.md` (entries 465-476)
- `integ/fx/summary.txt`
Shell cwd was reset to /home/user/Jarvis