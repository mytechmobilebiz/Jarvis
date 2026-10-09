---
name: coding-lane
description: "Tom's one-task-at-a-time build lane: lock, worktree, headless coding agent, tests, PR, and #dev status."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [dev, coding, worktree, tests, pull-request, tom]
    related_skills: [plan-before-code, post-as]
---

# Coding Lane

Use this only after the `plan-before-code` skill has posted the plan. The plan and originating task are the working contract.

## Hard rules

- One task at a time.
- Never push to `main` or `master`.
- Never merge.
- Never force-push.
- Work on branch `tom/<taskid>`.
- Stop at 45 minutes wall-clock time and report status.
- Do not loop unattended on paid APIs. Two coding-agent attempts is the ceiling.

## Lock

1. Derive a short task id from the thread, ticket, or owner wording: lowercase letters, numbers, and hyphens only.
2. Ensure `workspace/` and `workspace/reports/` exist, then check `workspace/tom.lock`.
3. If an active lock exists and its timestamp is younger than 2 hours, queue the task in `workspace/reports/tom-YYYY-MM-DD.md`, post in `#dev` as Tom that it is queued behind `<taskid>`, and stop.
4. If the lock is older than 2 hours, note the stale lock in the daily report, replace it, and continue.
5. Create `workspace/tom.lock` containing:

```yaml
task_id: <taskid>
started_at: <ISO-8601 timestamp>
source: <thread, ticket, or brief label>
```

## Build lane

1. Read the target row in `knowledge/tom/repos.md`.
2. `cd` into the repo path from that row.
3. Create a worktree on a new branch:

```bash
git worktree add ../<repo>-tom-<taskid> -b tom/<taskid> <main-branch>
cd ../<repo>-tom-<taskid>
```

4. Run the headless coding agent inside the worktree. Prefer Claude:

```bash
claude -p "<the posted plan + originating task, verbatim>" --permission-mode acceptEdits
```

If `claude` is unavailable, use Codex:

```bash
codex exec --skip-git-repo-check -s workspace-write "<the posted plan + originating task, verbatim>"
```

5. Run the repo's test command from `knowledge/tom/repos.md`.
6. If tests fail, make one more coding-agent attempt using the same plan plus the failing output. Run the tests again.

## Green path

When tests pass:

1. Check the diff for unrelated changes.
2. Push only `tom/<taskid>`.
3. Create a PR with `gh pr create`. Title and body must be plain English:
   - Symptom: what the user saw.
   - Cause: what was wrong.
   - Fix: what changed.
   - Proof: exact test command and result.
4. Post the PR link and a 3-sentence summary in `#dev` as Tom via `post-as`.
5. Write the PR link, test proof, and summary to `workspace/reports/tom-YYYY-MM-DD.md`.
6. Release the lock by removing only `workspace/tom.lock`. Remove no branches, worktrees, logs, or generated artifacts.

## Red path

If tests are still red after 2 attempts, or the 45-minute ceiling arrives:

1. Stop changing code.
2. Post an honest status in `#dev` as Tom: what was tried, the failing command, the shortest useful failing output, and the next recommended step.
3. Write the same status to `workspace/reports/tom-YYYY-MM-DD.md`.
4. Release the lock by removing only `workspace/tom.lock`. Leave the worktree intact for inspection.
