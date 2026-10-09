#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createContext, isSetupCancelled } from "./context.js";
import { buildLastRunRecord, loadSupportCode, writeLastRun } from "./diagnostics.js";
import { runSetup } from "./engine.js";
import { exitCodeFor } from "./exit-code.js";
import { resolveVendorRoot } from "./vendor.js";
import { jarvisConfigDir, telemetryEnabled, withTelemetry } from "./telemetry.js";
import { TerminalIO, banner, errorLine, printOutcome, stepHeading } from "./terminal-io.js";

function packageVersion(thisFile: string): string {
  try {
    const raw = fs.readFileSync(path.resolve(path.dirname(thisFile), "../package.json"), "utf8");
    return String(JSON.parse(raw).version ?? "unknown");
  } catch {
    return "unknown";
  }
}

async function main(): Promise<void> {
  const args = new Set(process.argv.slice(2));
  const dryRun = args.has("--dry-run");
  const thisFile = fileURLToPath(import.meta.url);
  const repoRoot = path.resolve(path.dirname(thisFile), "../..");
  // Ctrl+C must take the engine's children with it AND end the run.
  //
  // Installing these listeners removes Node's default-fatal behaviour, so the
  // first version aborted the login and then carried on into the next step —
  // still changing the customer's machine after they asked it to stop. The
  // abort now reaches both the children (via ctx.signal) and runSetup's loop,
  // and the process exits 130 once cleanup has finished. A second interrupt
  // means "now" and exits immediately.
  const aborter = new AbortController();
  let interrupted = false;
  const interrupt = (): void => {
    if (interrupted) {
      process.exit(130);
    }
    interrupted = true;
    errorLine("Stopping setup… finishing the current step cleanly. Press Ctrl+C again to quit immediately.");
    aborter.abort();
  };
  process.on("SIGINT", interrupt);
  process.on("SIGTERM", interrupt);
  // A registered signal listener is a live libuv handle: leaving it installed
  // keeps the event loop — and the process — alive after the run has finished.
  const releaseSignals = (): void => {
    process.off("SIGINT", interrupt);
    process.off("SIGTERM", interrupt);
  };
  // Ctrl+C at a raw-mode prompt never raises SIGINT — the terminal hands it to
  // us as data — so the IO gets the same interrupt hook the signal handler uses.
  const io = new TerminalIO({ onCancel: interrupt });
  // A checkout has no payload, so this is undefined and the network install
  // path stays the developer/Linux default. With one built (or
  // JARVIS_ENGINE_VENDOR set) the terminal wizard exercises the SAME offline
  // install the packaged app uses.
  const vendorRoot = resolveVendorRoot({ packaged: false, repoRoot, env: process.env });
  // No `gateway` here on purpose: the terminal wizard has no supervisor, so the
  // Start Engine step reports that honestly and credential restarts take the
  // legacy (process-scan) path in gateway-restart.ts.
  const appVersion = packageVersion(thisFile);
  const ctx = createContext({
    dryRun,
    version: appVersion,
    io,
    repoRoot,
    signal: aborter.signal,
    ...(vendorRoot === undefined ? {} : { vendorRoot }),
  });

  banner();
  if (dryRun) {
    ctx.io.warn("Dry run: no commands that change the system, no files written, no browser opened, no network calls made.");
  }

  const telemetry = withTelemetry(ctx, {
    stepStarted: ({ index, total, title }) => {
      stepHeading(index + 1, total, title);
    },
    stepFinished: ({ outcome }) => {
      printOutcome(ctx.io, outcome);
    },
  }, { appVersion });
  let result;
  try {
    result = await runSetup(ctx, telemetry.hooks, undefined, undefined, { slackOnly: args.has("--slack") });
  } finally {
    releaseSignals();
    // Also on the interrupt path: whatever was recorded is sent or queued.
    await telemetry.flush();
  }

  if (!ctx.dryRun) {
    await writeLastRun(jarvisConfigDir(), buildLastRunRecord({
      summary: result.summary,
      failed: result.failed,
      appVersion,
      packaged: ctx.packaged,
    }));
  }
  if (result.failed) {
    // Only advertise the support code when telemetry will actually carry events
    // for it — an opted-out run has nothing on the server to correlate against.
    const code = (await telemetryEnabled(ctx))
      ? await loadSupportCode(jarvisConfigDir(), { mint: true })
      : undefined;
    ctx.io.note(
      code === undefined
        ? "Stuck? Email jarvis.agent.tech@pm.me — a real person reads and answers."
        : `Stuck? Email jarvis.agent.tech@pm.me — a real person reads and answers. Mention support code ${code} so we can see exactly where setup stopped.`,
    );
  }

  if (result.cancelled === true) {
    ctx.io.warn("Setup was stopped before it finished. Nothing already completed was undone — run it again when you are ready.");
  }
  process.exitCode = exitCodeFor(result);
}

main().catch((error: unknown) => {
  if (isSetupCancelled(error)) {
    // Cancelling is not failing: the same status an OS interrupt produces.
    errorLine("Setup was stopped. Nothing already completed was undone.");
    process.exitCode = 130;
    return;
  }
  const message = error instanceof Error ? error.message : String(error);
  errorLine(message);
  process.exitCode = 1;
});
