#!/usr/bin/env node
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WizardError, createContext } from "./context.js";
import { installApp } from "./steps/install-app.js";
import { TerminalIO, errorLine, printOutcome, stepHeading } from "./terminal-io.js";

async function main(): Promise<void> {
  const args = new Set(process.argv.slice(2));
  const dryRun = args.has("--dry-run");
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const ctx = createContext({ dryRun, io: new TerminalIO(), repoRoot });

  stepHeading(1, 1, "Install App");
  if (dryRun) {
    ctx.io.warn("Dry run: nothing is built and nothing is installed.");
  }

  const outcome = await installApp(ctx);
  printOutcome(ctx.io, outcome);
  process.exitCode = outcome.status === "failed" ? 1 : 0;
}

main().catch((error: unknown) => {
  if (error instanceof WizardError) {
    errorLine(`${error.message} Fix: ${error.hint}`);
  } else {
    errorLine(error instanceof Error ? error.message : String(error));
  }
  process.exitCode = 1;
});
