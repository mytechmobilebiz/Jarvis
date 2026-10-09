import { type StepOutcome, type WizardContext } from "../context.js";
import type { LegacyDeps } from "../legacy-cleanup.js";
import { liftLegacyProviderKeys } from "../legacy-key-carry.js";
import { writeMigrationMarker } from "../migration-marker.js";
import { purgeLegacyFootprint, type PurgeDeps, type PurgeReport } from "../purge.js";
import { appendSetupLog } from "../fs-utils.js";
import { warn } from "../ui.js";
import { redactSecrets } from "../redact.js";

/**
 * First run of 1.4.0 over an earlier install — and it CLEARS THE GROUND rather
 * than inspecting it.
 *
 * The version of this step that shipped first asked a different question: "can
 * I prove this computer is clean?" When it could not — a `schtasks` query that
 * failed, one Startup script it could not attribute, a process it could not
 * trace — it FAILED, wrote no migration marker, and the gateway's authorization
 * gate then refused to start the engine. The result was a customer with a
 * broken earlier install ending up with a JARVIS that would not run, which is
 * exactly backwards: a broken earlier install is the situation this version
 * exists to fix.
 *
 * So this step now removes our own leftovers unconditionally and CONTINUES. It
 * has no failure outcome. What it could not remove is reported; what belongs to
 * someone else is named and left alone; neither stops setup, and neither stops
 * the engine — see purge.ts for the three classes and why customer data is
 * never in the deletable set.
 */
export async function migrate(
  ctx: WizardContext,
  deps: LegacyDeps & PurgeDeps & {
    purge?: typeof purgeLegacyFootprint;
    writeMarker?: typeof writeMigrationMarker;
    lift?: typeof liftLegacyProviderKeys;
  } = {},
): Promise<StepOutcome> {
  const purge = deps.purge ?? purgeLegacyFootprint;
  const writeMarker = deps.writeMarker ?? writeMigrationMarker;

  if (ctx.dryRun) {
    return {
      status: "done",
      notes: ["[dry-run] would remove an earlier JARVIS install's auto-start entries, engine folder and saved gateway password."],
    };
  }

  // NO consent prompt. Peter's ruling (2026-08-14): "If we need to clean it,
  // we clean it quickly — the previous broken setup shouldn't be a problem."
  // The safety is structural, not conversational: only provably-ours machinery
  // (classes A/B) is removed, realpath containment holds, customer data
  // (class C) is never deleted — quarantined at most. This is what every
  // installer's upgrade does; asking first was the friction, not the safety.
  //
  // Even the purge's own rejection cannot fail this step: runStep turns a
  // thrown error into status "failed", and a failed step is what this whole
  // change exists to abolish.
  // BEFORE the wipe: lift the previous install's provider key into the
  // profile, where the Model step's saved-key auto-verify takes over ("JARVIS
  // never asks for a key he already has" — Peter, re-cut #5). Shape-gated,
  // profile-wins, one value not artifacts, and the carried key still faces
  // the full verify — see legacy-key-carry.ts. Best-effort like everything
  // else in this step: a failed lift must never block the cleanup.
  const lift = deps.lift ?? liftLegacyProviderKeys;
  const carry = await lift(ctx).catch(() => ({ carried: [], refused: [] }));

  const report = await purge(ctx, deps).catch((error: unknown) => ({
    removed: [],
    stubborn: [`the cleanup could not run (${error instanceof Error ? error.message : String(error)})`],
    foreign: [],
    quarantined: [],
    elevationLikely: false,
  }));

  // ONE plain line for the customer; the itemized mechanics (paths, plists,
  // env keys) go to the setup log where support can read them (Peter's copy
  // rule, 2026-08-14: no technical detail in the voice log).
  const detail = [
    // Key NAMES only, never values (and the whole block passes redactSecrets).
    ...carry.carried.map((line) => `carried into the profile: ${line}`),
    ...carry.refused.map((line) => `not carried: ${line}`),
    ...report.removed.map((line) => `removed: ${line}`),
    ...report.quarantined.map((line) => `set aside: ${line}`),
    ...report.foreign.map((line) => `left alone: ${line}`),
    ...report.stubborn.map((line) => `could not remove: ${line}`),
  ];
  if (detail.length > 0) await appendSetupLog(ctx, redactSecrets(`earlier-install cleanup:\n${detail.join("\n")}`));
  if (report.removed.length > 0 || report.quarantined.length > 0) {
    ctx.io.note("Cleaned up what the earlier JARVIS left behind. Your keys, settings and history are untouched.");
  }
  if (report.stubborn.length > 0) {
    warn(ctx, "A couple of things from the earlier install would not budge — setup is continuing anyway.");
  }

  // The marker is a RECORD that this ran, not a permission to start. Nothing
  // reads it as a gate any more (gateway-authorization.ts), so writing it here
  // — even when something was stubborn — cannot strand a customer.
  // Best-effort by design: the marker no longer authorises anything, so an
  // unwritable profile or a full disk — the broken machines this targets —
  // must not turn into a failure the customer cannot clear.
  await writeMarker(ctx, { cleaned: report.removed.length > 0 }).catch(() => {
    report.stubborn.push("the record of this cleanup could not be saved");
  });

  const notes = summarize(report);
  return {
    status: report.removed.length === 0 && report.quarantined.length === 0 ? "skipped" : "done",
    notes,
    props: {
      purged: report.removed.length,
      stubborn: report.stubborn.length,
      foreign: report.foreign.length,
      quarantined: report.quarantined.length,
      elevation_likely: report.elevationLikely,
      keys_carried: carry.carried.length,
      keys_refused_carry: carry.refused.length,
    },
    ...(report.stubborn.length > 0
      ? {
        nextActions: [
          report.elevationLikely
            ? 'If JARVIS misbehaves, close it, right-click it, choose "Run as administrator" and run setup again.'
            : "If JARVIS misbehaves, restart this computer and run setup again — that clears anything still running.",
        ],
      }
      : {}),
  };
}

/** The step ledger: plain, counted, no paths — the mechanics are in the setup log. */
function summarize(report: PurgeReport): string[] {
  const notes: string[] = [];
  if (report.removed.length === 0 && report.quarantined.length === 0) {
    notes.push("Nothing was left to clean up from an earlier JARVIS.");
  } else {
    notes.push("Cleaned up what the earlier JARVIS left behind.");
  }
  if (report.stubborn.length > 0) {
    notes.push("A couple of things would not budge; JARVIS will start anyway, and the details are in the log.");
  }
  notes.push("Your keys, settings and history were left untouched.");
  return notes;
}
