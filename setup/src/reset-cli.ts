#!/usr/bin/env node
/**
 * jarvis-reset — remove this computer's JARVIS leftovers, so setup can start
 * from zero.
 *
 * The tool support has been missing. When a customer's install is wedged by an
 * earlier version — an auto-start entry that keeps resurrecting a 1.3.x
 * gateway, a half-built engine folder, a saved gateway password that no longer
 * matches anything — the answer has been a sequence of manual steps over email.
 * This is that sequence, in one command, using exactly the same code the wizard
 * runs (purge.ts), so support and setup can never drift apart.
 *
 * It removes OUR machinery and OUR regenerable secrets. It does not touch the
 * customer's provider keys, Slack tokens, history or settings — see purge.ts
 * for the three classes. Anything it cannot prove is ours (a second Hermes
 * install's scheduled task, a Startup script pointing somewhere else) is named
 * and left alone.
 *
 *   jarvis-reset            # do it, printing what changed
 *   jarvis-reset --dry-run  # say what it WOULD do, change nothing
 */
import path from "node:path";
import { createContext } from "./context.js";
import { resolveHermesRoot } from "./hermes-root.js";
import { purgeLegacyFootprint } from "./purge.js";

async function main(): Promise<number> {
  const argv = process.argv.slice(2);
  // A STRICT whitelist, because the failure mode is deleting things. Anything
  // unrecognised — `--dryrun`, `--dry_run`, a stray path — used to be ignored,
  // which meant a customer who typed it slightly wrong got the destructive run
  // they were trying to avoid.
  const KNOWN = new Set(["--dry-run", "-n", "--help", "-h"]);
  const unknown = argv.filter((argument) => !KNOWN.has(argument));
  if (unknown.length > 0) {
    console.error(`jarvis-reset: unrecognised option ${unknown.join(", ")}`);
    console.error("Nothing was changed. Run `jarvis-reset --help` to see what it accepts.");
    return 2;
  }
  const dryRun = argv.includes("--dry-run") || argv.includes("-n");
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("usage: jarvis-reset [--dry-run]\n\n"
      + "Removes this computer's JARVIS leftovers (auto-start entries, old engine\n"
      + "folders, saved gateway passwords) so setup can start from zero.\n"
      + "Your provider keys, Slack tokens, history and settings are NOT touched.");
    return 0;
  }

  // A console-only IO: this tool asks nothing. Support hands it to a customer
  // who is already stuck, and a prompt they cannot answer would be one more
  // thing standing between them and a working install.
  const io = {
    inheritsStdio: true,
    confirm: async () => true,
    select: async (_message: string, _choices: unknown, defaultKey: string) => defaultKey,
    promptText: async (_message: string, defaultValue: string) => defaultValue,
    promptSecret: async () => "",
    showDeviceCode: () => {},
    warn: (message: string) => console.error(`  ! ${message}`),
    note: (message: string) => console.log(`  ${message}`),
  };

  const ctx = createContext({
    dryRun,
    packaged: false,
    io: io as never,
    repoRoot: path.resolve(process.cwd()),
  });

  // A RELATIVE HERMES_HOME would resolve against whatever directory this was
  // started from, so `HERMES_HOME=.hermes jarvis-reset` run from Downloads
  // would purge `Downloads/.hermes` while the app used another tree entirely.
  // For a destructive command that is not a warning, it is a refusal.
  const configured = process.env.HERMES_HOME;
  if (configured !== undefined && configured !== "" && !path.isAbsolute(configured)) {
    console.error(`jarvis-reset: HERMES_HOME is a relative path (${configured}), so it is ambiguous here.`);
    console.error("Nothing was changed. Set it to an absolute path and run this again.");
    return 2;
  }

  console.log(`JARVIS reset — ${resolveHermesRoot()}`);
  if (dryRun) {
    console.log("  (dry run: nothing will be changed)\n");
    console.log("  would remove: auto-start entries and running engines belonging to this install,");
    console.log("                earlier engine folders and their Python environments,");
    console.log("                the saved gateway password and port,");
    console.log("                JARVIS's own state files (they are rebuilt by setup).");
    console.log("  would keep:   your provider keys, Slack tokens, history, settings —");
    console.log("                and anything on this computer that belongs to something else.");
    return 0;
  }

  const report = await purgeLegacyFootprint(ctx);

  for (const line of report.removed) console.log(`  removed ${line}`);
  for (const line of report.quarantined) console.log(`  set aside ${line}`);
  for (const line of report.foreign) console.log(`  left alone: ${line}`);
  for (const line of report.stubborn) console.error(`  COULD NOT REMOVE ${line}`);

  console.log("");
  if (report.removed.length === 0 && report.quarantined.length === 0) {
    console.log("Nothing to clean up — this computer has no JARVIS leftovers.");
  } else {
    console.log(`Cleaned up ${report.removed.length + report.quarantined.length} item(s). Run JARVIS setup to install fresh.`);
  }
  if (report.stubborn.length > 0) {
    console.error(
      report.elevationLikely
        ? "\nSome items need administrator rights. Open a terminal as administrator and run this again."
        : "\nSome items could not be removed. Restart this computer and run this again.",
    );
    // NOT a failure exit: setup will still work, and a nonzero code here would
    // make support think the tool broke when it did most of its job.
  }
  console.log("Your keys, settings and history were left untouched.");
  return 0;
}

main().then(
  (code) => process.exit(code),
  (error: unknown) => {
    console.error(`jarvis-reset could not finish: ${error instanceof Error ? error.message : String(error)}`);
    console.error("Nothing was left half-removed — rerun setup and it will clean up what it can.");
    process.exit(1);
  },
);
