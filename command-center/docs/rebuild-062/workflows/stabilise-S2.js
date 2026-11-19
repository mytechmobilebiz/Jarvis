export const meta = {
  name: 'stabilise2-062-optionC-rebuild',
  description: 'Stabilisation round 2 of the rebuilt 0.6.2 option C: P1 carrier regression, P3, planted-PIN failure, restore durability, oracle stored form, then freeze F-R2 and full regression',
  phases: [
    { title: 'Fix classes', detail: 'S7-S8 sequential on wip/brain-062-optionC' },
    { title: 'Freeze', detail: 'S9: freeze F-R2, full lanes, 900 fresh seeds in both arms' },
  ],
}

const WT = '/home/user/Jarvis/.worktrees/brain-062C'
const RULES = `
CONTEXT. You are one step of STABILISING the rebuilt JARVIS Brain 0.6.2 "option C" (conversation-level, durable, private holding of withheld messages). The original option C code (rounds 1-3) was lost when the container was replaced; only snapshot i22 survived. Work in the git worktree ${WT} (branch wip/brain-062-optionC; product code under command-center/backend/app/brain, migrations, tests under command-center/backend/tests, dashboard under command-center/dashboard).

READ FIRST: ${WT}/command-center/docs/rebuild-062/REBUILD.md (plan, owner rules, decisions in force, pending decisions, steps) and the files in ${WT}/command-center/docs/rebuild-062/sources/ (the only record of option C: summary, round-2 and round-3 reports with guard names, test names, mutation-matrix rows, skeptic findings). Also read progress/*.md (R1-R7, and S*.md from earlier stabilisation steps), deviations.md, reconstruction-check.md, and progress/S6.md's triage (freeze F-R1, 43d9698) with the per-seed results in progress/S6-seeds/results/. The rebuild R1-R7 is done (head e13deee or later). Round 3's final code had 8 of these 900 fresh seeds failing; the rebuild had 269 after R5 and 53 at freeze F-R1 (S6). Your job is to root-cause and fix your class of failures in the product or the oracle (never by loosening a correct property; an oracle change needs a self-test proving real violations are still reported), within the decisions in force.

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
  { label: 'S7-p1-carriers', task: `STEP S7 (privacy first). (1) P1 regression 13457 and 13756 (both arms) and F1 (13 fail-closed seeds 13138 13185 13340 13360 13364 13476 13539 13584 13621 13622 13846 13890 13893), same root, introduced by RECON-62's second part (commit 881c9c6, bisect in progress/S6.md): when W's own row is gone, W counts as not in this backup, so the per-row carrier check outside groups has nothing to compare and a later backup's read carrying W word for word stays readable. Design a fix that keeps RECON-62's protection (W's text is never taken from an unrelated kept row) and fails closed: a carrier of a withheld text must never become readable because that text is not in hand (for example compare against every candidate text a verified copy could be and hold on any match, or hold related-looking rows as not_checked/other_not_compared until the text is in hand). Do not implement the pending owner decision 13639 (recording W's absorbed set at withhold time). Regression tests from 13457's and 13409's shapes. (2) P3: 13315 and 13418 (O2: a group row readable with text the oracle's record of its release does not cover; pre-existing since e13deee): trace what the product's release covers versus the oracle's record; fix the product if it frees more than was released, else fix the oracle with a self-test. (3) The planted-PIN privacy test test_brain_import::test_the_full_import_of_the_planted_fixture failed twice under heavy load (PIN found once in archive_sessions, once in memory_items): reproduce under load (parallel shards, random hash seeds) with the failing field captured, root-cause, and fix whatever lets the PIN through; this is privacy-critical, never dismiss it as flaky without the captured cause.` },
  { label: 'S8-restore-oracle', task: `STEP S8. (1) F5 B7-restore: 13000 13325 13550 13775 13800 13825 (rows absent after the restore, a readable row's older text back, or held under another family text): root-cause durability first. (2) P4 13324 (oracle: A1/C1 judge a row the Command Center stores cut at 4,000 characters by the full backup row; D-49 taught only A3 the stored form): extend the stored form to A1/C1 with a self-test proving real leaks are still reported. (3) F3: the 23 live over-hold (A5) seeds, lowest priority: root-cause the stale family rows live keeps from earlier plain imports; fix only if it holds nothing less than the oracle demands. (4) F4 13159 h1 (empty rows): decide oracle vs product with evidence.` },
]

phase('Fix classes')
const reports = []
for (const s of STEPS) {
  const r = await agent(`${RULES}\n\n${COMMON}\n\n${s.task}`, { label: s.label, phase: 'Fix classes' })
  reports.push({ step: s.label, report: r })
  log(`${s.label} finished`)
}

phase('Freeze')
const freeze = await agent(`${RULES}\n\nSTEP S9 (freeze F-R2 and regression). Same method as S6 (read progress/S6.md and repeat it exactly, with freeze-FR2-MANIFEST.sha256 and progress/S9.md): unit, integration shards, property (ORACLE_SEEDS=0), recorded seeds both arms, fresh seeds 13000-13899 second-withhold arm both arms, dashboard Vitest and tsc; run the planted-PIN test 50 times under parallel load. Triage every failing fresh seed vs F-R1 (new, fixed, unchanged) with product/oracle/pending-owner verdicts and privacy relevance. No code change in this step. Commit and push.`, { label: 'S9-freeze', phase: 'Freeze' })
return { reports, freeze }
