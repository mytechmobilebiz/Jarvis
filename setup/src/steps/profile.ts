import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { copyDirectoryContents, copyFileIfMissing, ensureDir, pathExists } from "../fs-utils.js";
import { migrateProfileTerminalCwd } from "../profile-migrate.js";
import { hermesInvocation, hermesVenvExecutablePath, runCommand } from "../system.js";

interface ProfileOverrides {
  run?: typeof runCommand;
  exists?: (filePath: string) => Promise<boolean>;
}

export async function profile(
  ctx: WizardContext,
  overrides: ProfileOverrides = {},
): Promise<StepOutcome> {
  const notes: string[] = [];
  const jarvisAgentDir = path.join(ctx.repoRoot, "agents", "jarvis");
  const expectedHermesExecutable = hermesVenvExecutablePath(ctx);
  const run = overrides.run ?? runCommand;
  const exists = overrides.exists ?? pathExists;
  const markersPresent = await Promise.all(
    ["SOUL.md", "config.yaml"].map((name) => exists(path.join(ctx.profileDir, name))),
  );

  if (markersPresent.every(Boolean)) {
    // Unsafe legacy routes are NOT cleaned here. provider-safety.ts is the one
    // authority: it parses provider AND model (so a deliberate Nous route
    // serving an anthropic/* slug survives), and it owns the whole lifecycle —
    // atomic write, gateway restart, Model invalidation. A second cleaner with
    // its own weaker rules erased working configurations.
    return {
      status: "done",
      notes: [`Profile already installed at ${ctx.profileDir}; use \`hermes profile update\` to upgrade it.`],
    };
  }

  const partialProfileExists = await exists(ctx.profileDir);
  const installArgs = ["profile", "install", jarvisAgentDir, "--name", "jarvis", "--alias", "-y"];
  if (partialProfileExists) installArgs.push("--force");

  const installInvocation = hermesInvocation(ctx, installArgs);
  const install = await run(
    ctx,
    installInvocation.command,
    installInvocation.args,
    { stream: true },
  );
  if (install.status !== 0) {
    // The failed step used to throw with zero captured detail — telemetry saw
    // `properties_json: {}`, so the fresh-zip-install failures (BUGS.md: Caleb,
    // Scott) were undiagnosable. The most likely cause is a zip-fallback install
    // whose Hermes venv was never created, so `hermes` here is the bare PATH
    // fallback that cannot run the profile. Capture the shape (anonymous,
    // closed-set) and point recovery at the Hermes step, which self-heals a
    // partial install, instead of the generic "confirm the file exists".
    const hermesVenvPresent = await exists(expectedHermesExecutable);
    return {
      status: "failed",
      notes: [
        hermesVenvPresent
          ? `The JARVIS Hermes profile did not install (exit ${install.status}). Fix: confirm ${expectedHermesExecutable} exists, then rerun JARVIS setup.`
          : `The JARVIS Hermes profile could not install because the Hermes engine is not fully set up yet (its program at ${expectedHermesExecutable} is missing; exit ${install.status}). Fix: run JARVIS setup again and let the Hermes step finish — it repairs a partial install — before the Profile step.`,
      ],
      props: {
        profile_exit_code: install.status,
        profile_forced: partialProfileExists,
        hermes_venv_present: hermesVenvPresent,
      },
    };
  }
  notes.push(
    partialProfileExists
      ? `Overwrote the partial profile directory at ${ctx.profileDir} with the jarvis Hermes profile.`
      : "Installed the jarvis Hermes profile from agents/jarvis.",
  );

  const knowledgeCount = await copyDirectoryContents(
    ctx,
    path.join(jarvisAgentDir, "knowledge"),
    path.join(ctx.profileDir, "knowledge"),
  );
  notes.push(`Copied ${knowledgeCount} Jarvis knowledge item${knowledgeCount === 1 ? "" : "s"} into the profile.`);

  const copiedOperations = await copyFileIfMissing(
    ctx,
    path.join(jarvisAgentDir, "operations.md"),
    path.join(ctx.profileDir, "operations.md"),
  );
  if (copiedOperations) notes.push("Seeded operations.md in the profile.");

  await ensureDir(ctx, path.join(ctx.profileDir, "workspace", "briefs"));
  await ensureDir(ctx, path.join(ctx.profileDir, "workspace", "reports"));
  await ensureDir(ctx, path.join(ctx.profileDir, "skills"));
  await ensureDir(ctx, path.join(ctx.profileDir, "cron"));
  await ensureDir(ctx, path.join(ctx.profileDir, "knowledge"));
  notes.push("Created workspace/briefs, workspace/reports, skills, cron, and knowledge directories.");

  // F4: the freshly installed seed carries terminal.cwd as a POSIX literal;
  // pin it to THIS machine's actual profile directory right away, so Windows
  // and custom-HERMES_HOME installs never launch a gateway with a working
  // folder that does not exist. Best-effort: on the default POSIX layout the
  // literal is already right, and the unconditional migration in runSetup
  // retries on every later run.
  try {
    const pinned = await migrateProfileTerminalCwd(ctx);
    if (pinned !== null) notes.push("Pinned the assistant's working folder to the profile.");
  } catch {
    notes.push("The working-folder pin could not be written yet; setup will retry it on the next run.");
  }

  return { status: "done", notes };
}

/** Read `model.default` out of the profile's config.yaml without a YAML parser. */
export function readModelDefault(configText: string): string | undefined {
  const lines = configText.split(/\r?\n/);
  const modelIndex = lines.findIndex((line) => /^model:\s*(\{\s*\})?\s*$/.test(line));
  if (modelIndex === -1) return undefined;
  for (let index = modelIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim() === "" || line.trimStart().startsWith("#")) continue;
    // Dedent ends the block.
    if (!/^\s/.test(line)) return undefined;
    const match = /^\s+default:\s*(.*)$/.exec(line);
    if (match === null) continue;
    const value = match[1].trim().replace(/^["']|["']$/g, "");
    return value === "" ? undefined : value;
  }
  return undefined;
}
