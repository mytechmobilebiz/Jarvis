export const meta = {
  name: 'stabilise-062-optionC-rebuild',
  description: 'Stabilise the rebuilt 0.6.2 option C: root-cause and fix the fresh-seed failure classes from R5 triage, close R7 gaps, then freeze and rerun all lanes and 900 fresh seeds',
  phases: [
    { title: 'Fix classes', detail: 'S1-S5 sequential on wip/brain-062-optionC' },
    { title: 'Freeze', detail: 'S6: freeze, full lanes, 900 fresh seeds in both arms, triage' },
  ],
}

const WT = '/home/user/Jarvis/.worktrees/brain-062C'
const RULES = `
CONTEXT. You are one step of STABILISING the rebuilt JARVIS Brain 0.6.2 "option C" (conversation-level, durable, private holding of withheld messages). The original option C code (rounds 1-3) was lost when the container was replaced; only snapshot i22 survived. Work in the git worktree ${WT} (branch wip/brain-062-optionC; product code under command-center/backend/app/brain, migrations, tests under command-center/backend/tests, dashboard under command-center/dashboard).

READ FIRST: ${WT}/command-center/docs/rebuild-062/REBUILD.md (plan, owner rules, decisions in force, pending decisions, steps) and the files in ${WT}/command-center/docs/rebuild-062/sources/ (the only record of option C: summary, round-2 and round-3 reports with guard names, test names, mutation-matrix rows, skeptic findings). Also read progress/*.md (R1-R7, and S*.md from earlier stabilisation steps), deviations.md, reconstruction-check.md, and progress/R5.md's triage with the per-seed results in progress/R5-seeds/results/. The rebuild R1-R7 is done (head e13deee or later). Round 3's final code had 8 of these 900 fresh seeds failing; the rebuild has 269. Your job is to root-cause and fix your class of failures in the product or the oracle (never by loosening a correct property; an oracle change needs a self-test proving real violations are still reported), within the decisions in force.

ENVIRONMENT. Run: eval "$(bash ${WT}/command-center/docs/rebuild-062/env.sh)" at the start and after any failure that looks like a restart (docker/db unreachable). It sets HERMES_SRC and PY. Run tests with $PY -m pytest from command-center/backend, -p no:randomly. Use your own database: export TEST_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/<yourlabel> after creating <yourlabel> and <yourlabel>_cr in cc-test-pg (docker exec cc-test-pg psql -U cc -d cc_test -c 'create database ...'); drop them when done. Property tests: HERMES_SRC set and REQUIRE_HERMES_SRC=1.

RULES (owner, non-negotiable):
- Privacy first, fail closed. Where the sources do not settle a rule, choose the fail-closed reading and append a RECON-<n> entry to command-center/docs/rebuild-062/deviations.md (next free number; say what the sources say, what you chose, why).
- Do NOT implement anything listed under "Pending owner decisions" in REBUILD.md.
- Never weaken an existing test, assertion or oracle property; adapt only toward more protection and log each adaptation as D-<n>.
- Every behaviour you add gets a regression test that fails on i22 (or on the previous step's commit) and passes now; name tests after the sources' names where the sources give them.
- No real JARVIS data, no backup imports, no external research or network beyond package installs and the already-cloned Hermes; never print .env values or credentials.
- Commit and push checkpoints often (at least after each coherent sub-part and at the end) ONLY to wip/brain-062-optionC: git add -A command-center && git commit -m "WIP 0.6.2 option C rebuild: <step>: <what> - UNREVIEWED, NOT A RELEASE, NO-GO" with these two trailer lines after a blank line:
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01KJrxiKBruFZHec96QYgGJP
  then git push origin wip/brain-062-optionC. Never force-push, never another branch, no PR, no release, no install, no deploy. On a network failure retry the push up to 4 times (2s,4s,8s,16s).
- Keep command-center/docs/rebuild-062/progress/<step>.md current (what is done, what is next, test commands and summary lines) so a restarted run can resume: if it already exists when you start, continue from it instead of redoing work.
- Before finishing, run the existing brain test files you touched plus tests/unit and the relevant integration files; report exact pytest summary lines. Pre-existing environment-dependent failures (test_gate x4, test_release_version, test_brain_ops package test) are never counted as passed.

RETURN: a concise report: what you built (files), RECON/D entries added, tests added (fail-before/pass-after evidence), exact run summary lines, commits pushed (hashes), anything left open. Do not claim tests prove absence of leaks.`
const COMMON = `
FOR EVERY CLASS: pick 3-6 representative seeds, reproduce each failure with the seed's exact arm (ORACLE_LIVE_HISTORY, ORACLE_SECOND_WITHHOLD as in progress/R5-seeds), trace it to code, fix the root cause, add a regression test (integration or unit) that fails before and passes after, then rerun every seed of the class in both arms (use progress/R5-seeds/lane.sh and runlist.sh; split into parallel lanes each with its own databases) and report before/after counts. Classify every seed you leave failing (product / oracle / pending owner shape from REBUILD.md) with the reason. Privacy-relevant failures (a withheld text or group row readable) come first. A fail-closed fix (holding more) is preferred over a fail-open one; never trade a leak for fewer failures. Write progress to command-center/docs/rebuild-062/progress/<step>.md.`

const STEPS = [
  { label: 'S1-restore-live', task: `STEP S1 (privacy first). Class B7-restore (12 of 36 restore seeds incl. 13825: pending group and carrier holds readable after restore + removals-only file + re-import) and class A5 (a) carrier release on the test copy not reaching live, (b) group rows held on the test copy readable on live (13010), (c) export refused after a complete review because uncertain rows wait (13075, 13159, 13355), which makes live import without the file and cascades into A1/B2/O1/O2/O8/O11. For (c), decide within O-D1 (uncertain rows are held and the export waits) what a complete review must offer so the export is not stuck (for example the reviewer can keep or type-release uncertain holds); record the reading as RECON.` },
  { label: 'S2-nib-second', task: `STEP S2. Class A3-nib-second (~107 seeds, fail-closed): after the second withhold, the page sweep holds W1's group rows as 'not_in_backup' with compared [] and decided_on ''; the next Settle never replays them (traced on 13450); for a conversation the backup no longer holds (13010) the text is gone. Two probes in R5 did not fix it (see progress/R5.md). Also the harness gap 13165 (Settle of the decision backup refused after the second withhold because a decision waits on another backup): decide whether the scenario or the product is wrong, per the decisions in force.` },
  { label: 'S3-safecopy-disappear', task: `STEP S3. Class B5 (~62 seeds: i22's safe-copy property fails; decide product vs oracle first, with evidence), class B1-disappeared (14 seeds: stored i22-family rows disappear; durability, privacy-adjacent) and B1-changed (7).` },
  { label: 'S4-evidence-rest', task: `STEP S4. Class A3-other (~28 seeds; 13075: an uncertain hold shows only 'uncertain' for a row identical to W, an evidence shortfall), B6 (9), over-hold A4/B4 (8), and the R2/R4/R5 open property failure test_rv3_bug_seed_replayed_exactly[1] (independent holds 34 and 35 refused: "this message is not held for review"). Over-holds are lower priority than evidence honesty; never fix an over-hold by holding less where the oracle demands a hold.` },
  { label: 'S5-gaps', task: `STEP S5. Close the R7 gaps in reconstruction-check.md that need no new product behaviour beyond the sources: the oracle reading import_records for rollback copies at rest (gd1:oracle_rollback_records_off) with its self-test; the 5 missing dec_json_off closing tests; a stand-in for the lost test_hermes_derived3_lines.py + hermes_lean_gen.py (N1/N4 on lines the vendored Hermes itself generates); a stand-in for the crafted-state test of c2:imp_stored_none_every_off; mutate and close the unmutated guards rel3:imp_carriers_group_skip, rel3:flip_any_anchor, gd1:p2_both_off; investigate the survivors r3:release_on_evidence_carrier_off and cr2:quick_no_quote_bypass (catch with a test, or show equivalence/compensation with a failing double). The oracle narrowings for 13327/13407/13364 are only to be rebuilt if the sources describe them; otherwise list them as not reconstructable. Do not take on round-4 items (orphan fork markers, decisions-file later withhold, session_search snippet, F2).` },
]

phase('Fix classes')
const reports = []
for (const s of STEPS) {
  const r = await agent(`${RULES}\n\n${COMMON}\n\n${s.task}`, { label: s.label, phase: 'Fix classes' })
  reports.push({ step: s.label, report: r })
  log(`${s.label} finished`)
}

phase('Freeze')
const freeze = await agent(`${RULES}\n\nSTEP S6 (freeze and regression). Make freeze F-R1: tag nothing on the remote; record the head commit and a manifest (find -type f under command-center excluding __pycache__, node_modules, .pytest_cache; LC_ALL=C sort; sha256sum) at command-center/docs/rebuild-062/freeze-FR1-MANIFEST.sha256 and its own sha256 in progress/S6.md. Then run on that commit: tests/unit; tests/integration in parallel shards with separate databases; tests/property with HERMES_SRC, REQUIRE_HERMES_SRC=1, ORACLE_SEEDS=0; the recorded seeds in both live-history arms; fresh seeds 13000-13899 in the second-withhold arm in both live-history arms (parallel lanes, own databases); dashboard Vitest and tsc. Triage every failing fresh seed into classes with cause and product/oracle/pending-owner verdict, and say which are privacy-relevant. Add newly reproducible failing seeds' numbers to nothing (do not change code in this step). Write progress/S6.md with every exact summary line and the triage table, commit and push.`, { label: 'S6-freeze', phase: 'Freeze' })

return { reports, freeze }
