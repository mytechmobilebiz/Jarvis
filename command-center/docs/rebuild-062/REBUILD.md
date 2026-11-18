# 0.6.2 option C rebuild: plan and rules

Status: UNREVIEWED, NOT A RELEASE, NO-GO.

## Why this exists
On 2026-11-18 the cloud container was replaced. All unpushed work was lost:
- the option C code (rounds 1–3, final freeze f8d);
- the deviation log (entries up to 476);
- the vendored Hermes copy;
- the skeptics' evidence.

The owner chose to rebuild option C from the last pushed snapshot, i22 (`origin/wip/brain-062-unreviewed`, 28e9c3d), and then run fix round 4. The owner approved pushing work-in-progress to `wip/` branches.

The only record of option C is in `sources/`, taken from the surviving session log:
- `00-session-summary.md`: concepts, owner rules and decisions, file and test names.
- `10-round2-verification-report.md`, `11-round2-skeptic-releases-oracle.md`.
- `20-…` to `24-…`: round-3 step, consolidation and skeptic reports, including the mutation matrix rows with guard names and killing-test names.
- `30-round3-verification-report.md`, `31-round3-summary-to-owner.md`.

The code is a reconstruction of behaviour, not a replay. Where the sources do not settle a rule, choose the fail-closed reading, and record it in `deviations.md` as `RECON-<n>` for owner review.

## Environment
Run `bash command-center/docs/rebuild-062/env.sh` after any container restart. It starts:
- Docker;
- `cc-test-pg` (127.0.0.1:55432, test-only credentials);
- the venv at `/home/user/rebuild/venv`;
- Hermes at commit `b1a6350602383fdbed9b0bd0eb15d86eaa335da8`, the last NousResearch/hermes-agent commit on 2026-11-10.

Notes:
- The i22 conformance test passes 24/24 on that commit.
- Upstream Hermes removed its old session store on 2026-11-17 (`be3d6affc8`). That is out of scope here; it is reported to the owner.
- Use `HERMES_SRC` and `REQUIRE_HERMES_SRC=1` for property tests.
- Give each parallel test run its own database name (`TEST_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/<own>`, plus `<own>_cr` created first). Also set `TEST_CREDIT_REPAIR_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/<own>_cr`; otherwise conftest uses the shared `cc_test_cr` and parallel runs collide. Drop them afterwards.
- Do not end your turn while test runs are still going in the background: wait for them, or run them in the foreground, and only then report. A report that says "waiting for notifications" is lost.
- Containers restart often. Commit and push at every checkpoint, and resume from `progress/<step>.md`.

## Owner rules (unchanged)
- **Privacy first, fail closed.** Never claim that tests prove no leak remains.
- **Pushing:** WIP commits and pushes go only to `wip/brain-062-optionC`. Message format: `WIP 0.6.2 option C rebuild: <step> - UNREVIEWED, NOT A RELEASE, NO-GO`. Never force-push, never another branch, no PR, no release, no install, no deploy.
- **Nothing outward:** no real JARVIS data, no JARVIS Brain backup imports, no external research, no production systems. Never print `.env` values or credentials.
- **Do not weaken** any existing test, assertion or oracle property. Adapt only toward more protection, and log each adaptation.
- **The backup-import fallback** stays unapplied.

## Owner decisions in force
- **Option C:** conversation-level holding of withheld messages; durable and private.
- **Groups, from a withheld message (J1–J9 closure):**
  - lineage: compression continuation (`_compressed_from`, parent ending `compression`), branch, reset, delegate, rotation;
  - copied-history matching (J8) for conversations with no anchoring parent.
- **O-D1:**
  - a markerless child joins its parent's group, from the parent down only;
  - a parent that does not resolve, or conflicting parents, gives the `uncertain` state;
  - uncertain rows are held, never one-click, and the export waits.
- **Round-3 A:** a fork marker (`_branched_from`, `_delegate_from`, `_reset_from`) counts only when it names the session's own parent.
  - With a NULL parent, any marker counts (as in Hermes).
  - A copied fork marker after the parent was reopened reads as `continuation`.
  - A marker naming another conversation keeps its kind.
- **O-CI:** every comparison uses `fold`: NFKC, casefold, Turkish dotted/dotless i, and removal of default-ignorable code points.
- **O-D4:** list `generic-v1` and a 16-character minimum. A generic piece is neither demanded nor allowed as a carrier; anything longer is held.
- **D3 / B6:**
  - hold evidence states include `none`, `cut`, `part_of`, `contains`, `not_in_backup`, `not_checked`, `later_withhold`, `other_not_compared` and `uncertain`;
  - `none` requires every withheld text in hand;
  - one-click release only for exactly `none`, user/assistant rows, and a read form;
  - per-row release needs the typed word on the server (untyped gets 400/422);
  - a release's trust must come from a release that covers the stored content (6725 / F1 class).
- **Decision A (i22, entry 292):** fail-closed W text.
- **lt1_c (i) and the D3 conditional:** the details are not in the sources. Keep i22's behaviour and the round reports' descriptions (for example "13500 seeding consistent with O-lt1c"). Record any reading you choose.

## Pending owner decisions (do NOT implement; keep shapes documented)
- groups-paths B, D and N3 (the parent side of O-D1, including Hermes v16 tags);
- N3 / R5b (middle pieces of W);
- 13426, 13532 and 13754 (same-uid twins);
- releases N2 (a release made before a later withhold);
- 13639 (record W's absorbed set at withhold time);
- 13621 (a lineage no backup held);
- a reopened parent and its copied-marker child held both ways;
- costs (full scan, "none needs every text").

## Steps
| Step | Content |
|---|---|
| R1 groups | schema (`withheld_sessions`, `hidden_titles`, link kinds, title trigger), `groups.py` closure J1–J9 with O-D1 and uncertain, `source.py` markers (round-3 A), wiring into withhold / Settle / live / later / restore / legacy, R9 repair, export gate |
| R2 hold | central sweep `hold_group_rows`, rollback / deleted / updated copies, startup repair, counters, restore and legacy paths |
| R3 derived | canonical iterative decode (caps: 64 steps, depth 32, size budget; capped means `not_checked`, the same in the oracle), `row_relations`, cut / part_of / contains over every field, quote spans, text without quote markers (≥16 chars), repr cut, prune line (W's first 40 chars), fold, generic-v1, full stored-row scan (no prefilter), titles and memory items, carriers outside groups held per row, withhold preview |
| R4 releases | `one_click_eligible`, typed per-row release, B6, D3 states, `later_withhold` / `other_not_compared`, N-A (`mark_later_withhold`), N-B (re-hold released rows), trust pairing (`releases_covering_stored`, `exempt_covers`, `group_rows` exempt), C7, `carried_via`, Settle, decisions-file import, restore with the removals-only file, `_w_text` same-message-id, seeding from `groups.seeds` |
| R5 oracle | option C oracle O1–O11, simulator gaps, live-history and second-withhold arms, `kept_carrier`, `rekeyed`, `quote_spans` / `span_relations`, `cc_redacted_of`, `fwords`, decode cap; re-establish recorded seeds |
| R6 dashboard and docs | group view, uncertain and later-withhold display, per-row release, counts; spec, release notes, Windows guide |
| R7 check | every round 1–3 item in the sources has a regression test that fails on i22 and passes now |

Then fix round 4, consolidation, and three skeptics.

Each step:
- writes `progress/<step>.md` as it goes;
- appends its `RECON-` and deviation entries to `deviations.md`;
- commits and pushes at each checkpoint.
