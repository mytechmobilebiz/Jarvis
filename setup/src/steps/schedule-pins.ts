import { type WizardContext } from "../context.js";
import { enginePythonPath, runCommand } from "../system.js";
import { confirm, note, warn } from "../ui.js";

/**
 * MODEL-SWITCH-ORPHANS (P1, v1.5.2 — found live on the Justin Lipes call
 * 2026-08-17, JARVIS self-diagnosed it).
 *
 * Switching the global brain — an API key to a ChatGPT/Claude plan, the
 * migration v1.5.1's copy actively pushes — leaves the customer's SCHEDULED
 * jobs pointing at the setup they no longer have. They then fail every time
 * they are due, silently, until someone repairs them by hand. Sarah's job on
 * Lipes' machine failed every 30 minutes and nothing on screen ever said so.
 *
 * THERE ARE TWO WAYS A SCHEDULE BREAKS, and only one of them is what the
 * phrase "pinned to a dead model" suggests (verified in the pinned engine,
 * `cron/scheduler.py` and `cron/jobs.py`):
 *
 *  1. UNPINNED jobs — the common case, and the silent one. A job that names
 *     no provider/model follows global config at fire time, and `create_job`
 *     SNAPSHOTS what that resolved to at creation. `run_job` then fails
 *     CLOSED when the live resolution differs from the snapshot: it skips the
 *     run, makes no call, and raises "Skipped to prevent unintended spend"
 *     (the guard exists so a global switch cannot quietly start spending on a
 *     paid route). Deliberate on the engine's part — but after a switch the
 *     CUSTOMER made on purpose, at our prompting, the guard is firing on a
 *     drift that is not a surprise at all. Every unpinned job stops.
 *  2. PINNED jobs — a job naming a provider/model explicitly keeps naming it
 *     after the switch. The drift guard does not engage; the job simply runs
 *     against a route the customer may no longer have credentials for.
 *
 * WHAT THIS DOES, and why it is shaped this way:
 *
 *  - Unpinned jobs are RE-BASELINED, not pinned: only their snapshots move to
 *    the new resolution. They stay unpinned and keep following the global
 *    brain, which is the behaviour the customer already had and the shape the
 *    spec prefers. The guard is not weakened — it is told that THIS drift is
 *    the one the customer just approved on screen.
 *  - Pinned jobs are RE-POINTED at the new brain, and only ever with consent,
 *    listed by name first: a pin is a deliberate choice, and quietly
 *    overwriting one would be its own defect.
 *  - Nothing happens without the customer seeing the list and saying yes. A
 *    "no" is honest about the consequence rather than silently correct.
 *
 * WHY THE ENGINE'S OWN WRITER: `cron edit` has no --provider/--model flags,
 * so there is no CLI path to this. Hand-editing `jobs.json` would bypass the
 * cross-process advisory lock, the atomic temp-then-replace, the file-mode
 * hardening and the next-run recomputation that `cron.jobs.update_job` does —
 * on a file the running gateway's ticker is also reading. So the work happens
 * inside the engine's python through its own function, the same way
 * `applyProviderConfig` writes config.yaml through the engine's own loader.
 *
 * FAIL-OPEN, ALWAYS: this runs at the end of a Model step that has just
 * SUCCEEDED. A customer whose brain now works must never be told setup failed
 * because a schedule audit could not run. Every error path here degrades to
 * "say nothing, change nothing".
 */

/** One schedule that is no longer pointing at the brain the customer just chose. */
export interface OrphanedSchedule {
  id: string;
  /** The customer's own words for the job, or its first line of instruction. */
  name: string;
  /** "every 30 minutes", when the job records one. */
  when: string;
  /**
   * `follows-global`: unpinned, and the drift guard will SKIP it — certain.
   * `pinned`: names the old route explicitly and will keep doing so.
   */
  kind: "follows-global" | "pinned";
}

/**
 * Detect and repair, inside the engine.
 *
 * `report` changes nothing. `repair` re-computes the same decision from
 * scratch for the ids it is given (never trusting an id list to describe
 * what to write) and applies it through `update_job`.
 *
 * Both modes print ONE json object on stdout. Nothing else is printed, so a
 * chatty engine import cannot be mistaken for a result: the caller reads the
 * LAST line that parses as json.
 */
export const RECONCILE_SCHEDULES_PY = `
import json, sys

mode = sys.argv[1]
wanted = set(sys.argv[2:])

def emit(payload):
    sys.stdout.write("\\n" + json.dumps(payload) + "\\n")

try:
    from cron.jobs import (
        load_jobs,
        update_job,
        _compute_provider_model_snapshots,
        _normalize_job_optional_text,
    )
except Exception as exc:  # engine too old, or not importable — say so, change nothing
    emit({"ok": False, "error": "import: %s" % exc})
    raise SystemExit(0)


def plan(job):
    """What would have to change for this job to run on the CURRENT brain.

    Mirrors cron/scheduler.py's drift guard exactly: an axis matters only when
    it is UNPINNED and carries a snapshot, and the comparison is casefolded.
    """
    if bool(job.get("no_agent")):
        return None, None  # script jobs carry no inference axes at all
    pinned_provider = _normalize_job_optional_text(job.get("provider"))
    pinned_model = _normalize_job_optional_text(job.get("model"))
    # What resolution would pick RIGHT NOW for each axis, computed by the
    # engine's own function so this can never disagree with the ticker.
    current_provider, current_model = _compute_provider_model_snapshots(
        provider=None,
        model=None,
        base_url=job.get("base_url"),
        no_agent=job.get("no_agent"),
    )
    updates = {}
    kind = None
    snap_provider = (job.get("provider_snapshot") or "").strip().lower()
    snap_model = (job.get("model_snapshot") or "").strip().lower()

    if pinned_provider is None and snap_provider and current_provider and current_provider.strip().lower() != snap_provider:
        updates["provider_snapshot"] = current_provider
        kind = "follows-global"
    if pinned_model is None and snap_model and current_model and current_model.strip().lower() != snap_model:
        updates["model_snapshot"] = current_model
        kind = "follows-global"
    # A pinned axis is only reported when it actually names something ELSE.
    if pinned_provider is not None and current_provider and pinned_provider.strip().lower() != current_provider.strip().lower():
        updates["provider"] = current_provider
        kind = "pinned"
    if pinned_model is not None and current_model and pinned_model.strip().lower() != current_model.strip().lower():
        updates["model"] = current_model
        kind = "pinned"
    return (updates or None), kind


def describe(job):
    name = (job.get("name") or "").strip()
    if not name:
        prompt = (job.get("prompt") or "").strip().splitlines()
        name = (prompt[0] if prompt else "a scheduled job")[:70]
    when = (job.get("schedule_display") or "").strip()
    return name, when


try:
    jobs = load_jobs()
except Exception as exc:
    emit({"ok": False, "error": "load: %s" % exc})
    raise SystemExit(0)

found = []
repaired = []
failed = []
for job in jobs:
    job_id = job.get("id")
    if not job_id:
        continue
    if mode == "repair" and job_id not in wanted:
        continue
    try:
        updates, kind = plan(job)
    except Exception as exc:
        failed.append({"id": job_id, "error": str(exc)})
        continue
    if not updates:
        continue
    name, when = describe(job)
    found.append({"id": job_id, "name": name, "when": when, "kind": kind})
    if mode == "repair":
        try:
            if update_job(job_id, updates) is None:
                failed.append({"id": job_id, "error": "the job is no longer there"})
            else:
                repaired.append(job_id)
        except Exception as exc:
            failed.append({"id": job_id, "error": str(exc)})

emit({"ok": True, "found": found, "repaired": repaired, "failed": failed})
`;

interface ReconcileResult {
  ok: boolean;
  found?: OrphanedSchedule[];
  repaired?: string[];
  failed?: { id: string; error: string }[];
}

/** The LAST line of stdout that parses as a json object — see the script's docstring. */
export function parseReconcileOutput(stdout: string): ReconcileResult | undefined {
  const lines = stdout.split("\n").map((line) => line.trim()).filter((line) => line.startsWith("{"));
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    try {
      const parsed = JSON.parse(lines[i]) as unknown;
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as ReconcileResult;
    } catch {
      /* not this line */
    }
  }
  return undefined;
}

async function reconcile(
  ctx: WizardContext,
  run: typeof runCommand,
  mode: "report" | "repair",
  ids: readonly string[],
  platform?: NodeJS.Platform,
): Promise<ReconcileResult | undefined> {
  const python = enginePythonPath(ctx, platform);
  if (ctx.dryRun || python === undefined) return undefined;
  try {
    const result = await run(ctx, python, ["-c", RECONCILE_SCHEDULES_PY, mode, ...ids], {
      stream: false,
      // The profile home is what selects WHICH jobs.json: Hermes exports
      // HERMES_HOME=<root>/profiles/<name> for profile subprocesses, and the
      // cron store is anchored on it (cron/jobs.py, the per-profile isolation
      // boundary). Without this the audit would read the DEFAULT profile's
      // schedules and cheerfully repair somebody else's.
      env: { HERMES_HOME: ctx.profileDir },
    });
    if (result.status !== 0) return undefined;
    return parseReconcileOutput(result.stdout);
  } catch {
    return undefined;
  }
}

/** Plain-language list, one line per schedule. */
function describe(schedule: OrphanedSchedule): string {
  return schedule.when === "" ? `  • ${schedule.name}` : `  • ${schedule.name} — ${schedule.when}`;
}

/**
 * Run after a model route has been proven and settled.
 *
 * Returns the notes to fold into the step's ledger. It never throws and never
 * changes the step's outcome: the brain works either way, and this is about
 * what happens NEXT time a schedule is due.
 */
export async function reconcileSchedulesAfterSwitch(
  ctx: WizardContext,
  run: typeof runCommand,
  platform?: NodeJS.Platform,
): Promise<string[]> {
  const report = await reconcile(ctx, run, "report", [], platform);
  const found = report?.ok === true ? (report.found ?? []) : [];
  if (found.length === 0) return [];

  const notes: string[] = [];
  const many = found.length > 1;
  // NAMED before anything is asked — the same contract the verify step keeps.
  // "would stop" not "have stopped": nothing has been due yet.
  warn(
    ctx,
    `${many ? `${found.length} of your scheduled jobs are` : "One of your scheduled jobs is"} still set up for the way `
      + `Jarvis used to think, sir. ${many ? "They" : "It"} would quietly stop running.`,
  );
  for (const schedule of found) note(ctx, describe(schedule));

  const agreed = await confirm(ctx, `Shall I move ${many ? "them" : "it"} over to the new setup now?`, true);
  if (!agreed) {
    const line = `${found.length} scheduled job(s) still point at the old setup and will not run until they are changed: `
      + found.map((schedule) => schedule.name).join(", ")
      + ". Run setup again from JARVIS settings when you want them moved.";
    notes.push(line);
    return notes;
  }

  const result = await reconcile(ctx, run, "repair", found.map((schedule) => schedule.id), platform);
  const repaired = result?.ok === true ? (result.repaired ?? []) : [];
  const failed = result?.ok === true ? (result.failed ?? []) : [];

  if (repaired.length > 0) {
    const line = `Moved ${repaired.length} scheduled job(s) over to the new setup.`;
    note(ctx, line);
    notes.push(line);
  }
  // Honest about the remainder: anything neither repaired nor explained is
  // still broken, and saying "done" over it is the failure mode this whole
  // module exists to end.
  const unresolved = found.filter((schedule) => !repaired.includes(schedule.id));
  if (unresolved.length > 0) {
    const line = `${unresolved.length} scheduled job(s) could not be moved and will not run: `
      + unresolved.map((schedule) => schedule.name).join(", ")
      + ". Email jarvis.agent.tech@pm.me with your support code and I'll sort them out.";
    warn(ctx, line);
    notes.push(line);
    void failed;
  }
  return notes;
}
