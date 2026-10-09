import fs from "node:fs/promises";
import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { confirm } from "../ui.js";
import {
  appendSetupLog,
  copyDirectoryContents,
  copyDirectoryContentsIfMissing,
  ensureDir,
  pathExists,
  readJsonIfExists,
  readTextIfExists,
  writeTextIfChanged,
} from "../fs-utils.js";
import { hermesInvocation, runCommand } from "../system.js";
import { redactSecrets } from "../redact.js";

interface Role {
  id: string;
  title: string;
}

interface CronJobsFile {
  jobs?: RoleCronJob[];
}

export interface RoleCronJob {
  id?: string;
  name?: string;
  prompt?: string;
  skills?: string[];
  skill?: string;
  deliver?: string;
  /**
   * What this schedule DOES for the customer, in plain words with the time in
   * it — the sentence the wizard's enable question is built from. Internal
   * names like "write-hooks" never reach a customer (Peter, 2026-08-14:
   * "people don't know what hooks are"); they stay in logs and config.
   */
  description?: string;
  /** Human-readable time, e.g. "daily at 08:00"; mirrors schedule.display. */
  schedule_display?: string;
  schedule?: {
    expr?: string;
    display?: string;
  };
}

interface CronMarker {
  jobs: string[];
}

const ROLES: Role[] = [
  { id: "sarah", title: "Sarah" },
  { id: "tom", title: "Tom" },
  { id: "bobby", title: "Bobby" },
  { id: "scout", title: "Scout" },
  { id: "ava", title: "Ava" },
];

export async function composeRoles(ctx: WizardContext): Promise<StepOutcome> {
  const notes: string[] = [];
  await ensureDir(ctx, path.join(ctx.profileDir, "skills"));
  await ensureDir(ctx, path.join(ctx.profileDir, "knowledge"));
  await ensureDir(ctx, path.join(ctx.profileDir, "cron"));

  const demoTarget = path.join(ctx.profileDir, "demo");
  if (await pathExists(demoTarget)) {
    notes.push("The example content was already in place.");
  } else if (ctx.dryRun) {
    notes.push(`[dry-run] would copy demo fixtures to ${demoTarget}.`);
  } else {
    await fs.cp(path.join(ctx.repoRoot, "demo"), demoTarget, {
      preserveTimestamps: true,
      recursive: true,
    });
    notes.push("Added some example content so the office does not start empty.");
  }

  let operations = (await readTextIfExists(path.join(ctx.profileDir, "operations.md"))) ?? "# Operations manual\n";
  const markerPath = path.join(ctx.profileDir, "cron", "jarvis-setup-registered.json");
  const marker = await readJsonIfExists<CronMarker>(markerPath, { jobs: [] });
  const registered = new Set(marker.jobs);
  /** Internal job names that failed to register — counted into diagnostics. */
  const cronFailures: string[] = [];

  // Jarvis is not a role here: only force-copy its skills. They are ours, and
  // this step is their only update channel on existing installs.
  const jarvisSkillsCopied = await copyDirectoryContents(
    ctx,
    path.join(ctx.repoRoot, "agents", "jarvis", "skills"),
    path.join(ctx.profileDir, "skills"),
  );
  notes.push(`Jarvis: brought ${jarvisSkillsCopied === 1 ? "a skill" : `${jarvisSkillsCopied} skills`} into the office.`);

  for (const role of ROLES) {
    const roleDir = path.join(ctx.repoRoot, "agents", role.id);
    const skillsCopied = await copyDirectoryContents(
      ctx,
      path.join(roleDir, "skills"),
      path.join(ctx.profileDir, "skills"),
    );
    notes.push(`${role.title}: brought ${skillsCopied === 1 ? "a skill" : `${skillsCopied} skills`} into the office.`);

    const knowledgeCopied = await copyDirectoryContentsIfMissing(
      ctx,
      path.join(roleDir, "knowledge"),
      path.join(ctx.profileDir, "knowledge", role.id),
    );
    notes.push(knowledgeCopied === 0
      ? `${role.title}: already had everything on file.`
      : `${role.title}: brought ${knowledgeCopied === 1 ? "a knowledge file" : `${knowledgeCopied} knowledge files`} along.`);

    if (role.id === "tom") {
      const reposPath = path.join(ctx.profileDir, "knowledge", "tom", "repos.md");
      const repos = await readTextIfExists(reposPath);
      if (repos !== undefined) {
        const reposChanged = await writeTextIfChanged(
          ctx,
          reposPath,
          repos.replaceAll("{{JARVIS_REPO_ROOT}}", ctx.repoRoot),
          "Tom repository paths",
        );
        notes.push(reposChanged ? "Tom: noted where your code lives." : "Tom: already knew where your code lives.");
      }
    }

    const soul = await fs.readFile(path.join(roleDir, "SOUL.md"), "utf8");
    operations = mergeRoleIntoOperations(operations, role.id, role.title, extractPersonaSection(soul));

    const jobNotes = await registerRoleJobs(ctx, role, registered, markerPath, cronFailures);
    notes.push(...jobNotes);
  }

  const operationsChanged = await writeTextIfChanged(
    ctx,
    path.join(ctx.profileDir, "operations.md"),
    operations,
    "operations role merge",
  );
  notes.push(operationsChanged ? "Added everyone to your operations manual." : "Everyone was already in your operations manual.");

  await persistCronMarker(ctx, markerPath, registered);

  // A schedule that failed to register FAILS this step (degraded: red in the
  // ledger, but it does not fail the whole run) — "done" would retain on the
  // next resume and quietly break the promise that setup will retry it (Codex
  // review 2026-08-14, finding 4). Degraded keeps progress.completed true, so
  // the customer is not walled off from a working JARVIS by one schedule.
  const failed = cronFailures.length > 0;
  return {
    status: failed ? "failed" : "done",
    ...(failed ? { degraded: true } : {}),
    notes,
    // Counted, never named, so the anonymous diagnostics can say "a schedule
    // failed on this machine" without carrying content.
    ...(failed ? { props: { cron_create_failed: cronFailures.length } } : {}),
  };
}

export function extractPersonaSection(soul: string): string {
  const withoutTitle = soul.replace(/^# .*(?:\r?\n)+/, "");
  const [persona = ""] = withoutTitle.split(/\r?\n##\s+/);
  return persona.trim();
}

export function mergeRoleIntoOperations(
  existing: string,
  roleId: string,
  roleTitle: string,
  persona: string,
): string {
  const start = `<!-- jarvis-setup:role:${roleId}:start -->`;
  const end = `<!-- jarvis-setup:role:${roleId}:end -->`;
  const block = `${start}\n### ${roleTitle}\n\n${persona.trim()}\n${end}`;
  const markerPattern = new RegExp(
    `\\n*${escapeRegExp(start)}[\\s\\S]*?${escapeRegExp(end)}\\n*`,
    "g",
  );
  const withoutExistingBlock = existing.trimEnd().replace(markerPattern, "\n\n").replace(/\n{3,}/g, "\n\n").trimEnd();
  const rolesHeader = /^## Roles\s*$/m.exec(withoutExistingBlock);

  if (rolesHeader === null) {
    return `${withoutExistingBlock}\n\n## Roles\n\n${block}\n`;
  }

  const headerEnd = rolesHeader.index + rolesHeader[0].length;
  const afterHeader = withoutExistingBlock.slice(headerEnd);
  const nextSection = /\n##\s+/.exec(afterHeader);
  const insertAt = nextSection === null ? withoutExistingBlock.length : headerEnd + nextSection.index;
  const before = withoutExistingBlock.slice(0, insertAt).trimEnd();
  const after = withoutExistingBlock.slice(insertAt).trimStart();

  return `${before}\n\n${block}${after === "" ? "" : `\n\n${after}`}\n`;
}

/**
 * What one schedule does, in the customer's language. The plain sentence comes
 * from jobs.json's `description` — written next to the schedule it describes,
 * so the prompt can never promise work the profile does not actually carry.
 * The fallback humanizes the internal name (dashes to spaces) rather than ever
 * printing a raw slug at a customer.
 */
export function describeJob(roleTitle: string, job: RoleCronJob): string {
  const description = (job.description ?? "").trim();
  if (description !== "") return description;
  const name = (job.name ?? job.id ?? "").replaceAll("-", " ").trim();
  const when = job.schedule_display ?? job.schedule?.display ?? "";
  const what = name === "" ? "some scheduled work" : name;
  return when === "" ? `${roleTitle} handles ${what} for you` : `${roleTitle} handles ${what} for you, ${when}`;
}

/**
 * The schedule question: what the staff member will DO, plainly, then the
 * honest cost line. No internal job names, no terminal commands (Peter,
 * 2026-08-14) — and no promise of an off-switch that does not exist: declining
 * points to RUN SETUP AGAIN as the existing way to switch it on later.
 */
export function scheduleQuestion(roleTitle: string, jobs: RoleCronJob[]): string {
  const described = jobs.map((job) => describeJob(roleTitle, job)).filter((entry) => entry !== "");
  const what = described.length === 0 ? `${roleTitle} has scheduled work ready` : described.join(" ");
  return `${what}. Each time it runs, it uses your plan's allowance. Shall I switch this schedule on? `
    + "If not now, you can switch it on later with RUN SETUP AGAIN.";
}

async function registerRoleJobs(
  ctx: WizardContext,
  role: Role,
  registered: Set<string>,
  markerPath: string,
  failures: string[],
): Promise<string[]> {
  const notes: string[] = [];
  const jobsPath = path.join(ctx.repoRoot, "agents", role.id, "cron", "jobs.json");
  if (!(await pathExists(jobsPath))) {
    notes.push(`${role.title}: nothing scheduled for this role.`);
    return notes;
  }

  const jobsFile = await readJsonIfExists<CronJobsFile>(jobsPath, { jobs: [] });
  const jobs = jobsFile.jobs ?? [];
  if (jobs.length === 0) {
    notes.push(`${role.title}: nothing scheduled for this role.`);
    return notes;
  }

  // Every shipped schedule for this role is already registered — there is
  // nothing to ask about, and this step now runs on EVERY setup pass.
  if (jobs.every((job) => registered.has(job.id ?? `${role.id}:${job.name ?? `${role.id}-job`}`))) {
    notes.push(`${role.title}: this schedule was already set up.`);
    return notes;
  }

  // Automated work starts only when the owner opts in; every run spends their
  // plan allowance, and setup remains the existing switch for turning it on.
  const enableSchedule = await confirm(ctx, scheduleQuestion(role.title, jobs), false);
  if (!enableSchedule) {
    notes.push(`${role.title}: schedule left disabled; it can be enabled later.`);
    return notes;
  }

  // Reconcile against the engine BEFORE creating anything: cron create
  // persists the job before our marker records it, so an interrupt in that
  // gap would create a DUPLICATE paid schedule on resume (Codex review
  // 2026-08-14, finding 6). A job the engine already has under our name is
  // adopted, never re-created. A list that cannot run leaves the set empty —
  // creation then behaves exactly as before.
  const listInvocation = hermesInvocation(ctx, ["-p", "jarvis", "cron", "list", "--all"]);
  const listing = await runCommand(ctx, listInvocation.command, listInvocation.args, { stream: false })
    .catch(() => ({ status: 1, stdout: "", stderr: "" }));
  // Whole-token match, never a substring: `morning-brief-old` in the listing
  // must not adopt `morning-brief` (verification pass 2026-08-14, F6). Job
  // names are slugs, so a token boundary is any non-slug character.
  const engineHas = (name: string): boolean =>
    listing.status === 0
    && name !== ""
    && new RegExp(`(^|[^A-Za-z0-9_-])${escapeRegExp(name)}([^A-Za-z0-9_-]|$)`, "m").test(listing.stdout);

  for (const job of jobs) {
    const jobName = job.name ?? job.id ?? `${role.id}-job`;
    const jobId = job.id ?? `${role.id}:${jobName}`;
    if (registered.has(jobId)) {
      notes.push(`${role.title}: this schedule was already set up.`);
      continue;
    }
    if (engineHas(jobName)) {
      registered.add(jobId);
      await persistCronMarker(ctx, markerPath, registered);
      notes.push(`${role.title}: this schedule was already set up.`);
      continue;
    }

    const expr = job.schedule?.expr;
    const prompt = job.prompt;
    const skill = job.skills?.[0] ?? job.skill;
    if (expr === undefined || prompt === undefined || skill === undefined) {
      notes.push(`${role.title}: one schedule in this build is incomplete, so I left it out — it's noted in the log.`);
      await appendSetupLog(ctx, `cron job ${jobName} (${role.id}) is incomplete: expr/prompt/skill missing in jobs.json`);
      continue;
    }

    // Through the single spawn authority: the site payload's engine is a bare
    // interpreter plus entry-point args, and spawning the executable alone
    // fed `-p` to Python itself — the schedule never existed and the customer
    // was shown a Python usage error (P1, Peter's walkthrough 2026-08-14).
    // stream: false — raw command output is never customer-visible; the
    // technical detail goes to the setup log below.
    const invocation = hermesInvocation(ctx, [
      "-p",
      "jarvis",
      "cron",
      "create",
      expr,
      prompt,
      "--name",
      jobName,
      "--skill",
      skill,
      "--deliver",
      job.deliver ?? "slack",
    ]);
    const result = await runCommand(ctx, invocation.command, invocation.args, { stream: false });

    if (result.status === 0) {
      registered.add(jobId);
      await persistCronMarker(ctx, markerPath, registered);
      notes.push(`${role.title}: schedule is set — ${describeJob(role.title, job)}.`);
    } else {
      // The customer hears it plainly; support gets the mechanics — but never
      // the job PROMPT or raw output verbatim: the log is a file support asks
      // customers to send, so it carries the job name, the exit code and a
      // redacted tail only (Codex review 2026-08-14, finding 8).
      failures.push(jobName);
      notes.push(`${role.title}: I couldn't set up this schedule — it's noted in the log, and running setup again will retry it.`);
      const tail = redactSecrets((result.stderr || result.stdout).trim()).slice(0, 400);
      await appendSetupLog(ctx, `cron create failed for ${jobName} (exit ${result.status}): ${tail}`);
    }
  }

  return notes;
}

async function persistCronMarker(ctx: WizardContext, markerPath: string, registered: Set<string>): Promise<void> {
  if (ctx.dryRun) {
    return;
  }

  await fs.writeFile(markerPath, JSON.stringify({ jobs: [...registered].sort() }, null, 2) + "\n", "utf8");
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
