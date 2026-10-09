import fs from "node:fs";
import path from "node:path";
import { WizardError, type StepOutcome, type WizardContext } from "../context.js";
import { commandExists, platformLabel, runCommand, type CommandResult } from "../system.js";

interface PreflightOverrides {
  platform?: NodeJS.Platform;
  commandAvailable?: (command: string) => boolean;
  run?: (ctx: WizardContext, command: string, args: string[]) => Promise<CommandResult>;
  /** Test seam for the absolute-path PowerShell fallback. */
  fileExists?: (filePath: string) => boolean;
  env?: NodeJS.ProcessEnv;
}

/**
 * The `where.exe` probe behind commandAvailable intermittently returns nonzero
 * on a busy machine (BUGS.md: Caleb — 4+ consecutive false "PowerShell was not
 * found", then a pass with no user change), which used to FATALLY block setup.
 * Windows PowerShell always lives at a fixed System32 path, so a deterministic
 * fs check is the reliable confirmation when the probe flaps.
 */
function resolveWindowsPowerShell(
  commandAvailable: (command: string) => boolean,
  fileExists: (filePath: string) => boolean,
  env: NodeJS.ProcessEnv,
): string | undefined {
  const viaProbe = ["powershell.exe", "pwsh"].find(commandAvailable);
  if (viaProbe !== undefined) return viaProbe;

  // The flake this targets is where.exe intermittently failing to find
  // powershell.exe (Windows PowerShell), which ALWAYS ships at a fixed System32
  // path already on PATH — a deterministic fs check confirms it. We do NOT fall
  // back to an off-PATH pwsh.exe: later steps launch `powershell.exe` by name,
  // so confirming a different executable would pass preflight but strand them.
  const systemRoot = env.SystemRoot ?? env.windir ?? "C:\\Windows";
  const candidates = [
    path.win32.join(systemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    path.win32.join(systemRoot, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe"),
  ];
  return candidates.find(fileExists);
}

export async function preflight(
  ctx: WizardContext,
  overrides: PreflightOverrides = {},
): Promise<StepOutcome> {
  const platform = overrides.platform ?? process.platform;
  const commandAvailable = overrides.commandAvailable
    ?? ((command: string) => commandExists(ctx, command, { platform }));
  const run = overrides.run
    ?? ((runCtx: WizardContext, command: string, args: string[]) => runCommand(runCtx, command, args, { stream: false }));
  if (ctx.dryRun) {
    return {
      status: "done",
      notes: [
        platform === "win32"
          ? `[dry-run] would check OS, Node ${process.version}, and PowerShell; the Hermes installer supplies its own PortableGit.`
          : ctx.packaged
            ? `[dry-run] would check OS and Node ${process.version}; the engine ships inside the app, so git and curl are not required.`
            : `[dry-run] would check OS, Node ${process.version}, run git --version, check curl, and inspect optional Homebrew.`,
      ],
    };
  }

  const notes: string[] = [];
  notes.push(`OS: ${platformLabel()}.`);

  const major = Number.parseInt(process.versions.node.split(".")[0] ?? "0", 10);
  if (major < 22) {
    throw new WizardError(
      `Node ${process.version} is too old.`,
      "Install Node 22 or newer, then rerun jarvis-setup.",
      { fatal: true },
    );
  }
  notes.push(`Node ${process.version} meets the >=22 requirement.`);

  if (platform === "win32") {
    const fileExists = overrides.fileExists ?? ((filePath: string) => fs.existsSync(filePath));
    const env = overrides.env ?? process.env;
    const powershell = resolveWindowsPowerShell(commandAvailable, fileExists, env);
    if (powershell === undefined) {
      // NON-FATAL by design: the probe flaps under load, and a Windows machine
      // truly without PowerShell is vanishingly rare, so a hard block here was
      // almost always a false negative that stranded setup (BUGS.md, Caleb).
      // Continue — a later step that genuinely needs PowerShell surfaces a real,
      // in-context error, and RETRY SETUP re-checks a probe that has settled.
      notes.push(
        "PowerShell could not be confirmed. This check can misfire on a busy machine, so setup will continue; if a later step needs PowerShell and it is genuinely missing, enable Windows PowerShell or install PowerShell 7 and run setup again.",
      );
      return { status: "done", notes };
    }
    const label = powershell === "powershell.exe" || powershell === "pwsh"
      ? powershell
      : `PowerShell (found at ${powershell})`;
    notes.push(`${label} is available; the Hermes installer will provision PortableGit and its own download tools.`);
    return { status: "done", notes };
  }

  // git and curl belong to the SOURCE install path only.
  //
  // A packaged app carries the pinned engine, an offline wheelhouse and its own
  // Python: `installVendoredEngine` runs the bundled interpreter and nothing
  // else, and a packaged build with a broken payload fails outright rather than
  // falling back to the network. Demanding git here was therefore a hard stop
  // for a Mac without Xcode command line tools — and it announced itself with
  // an "install developer tools" dialog, which is exactly the install-time
  // assembly this release exists to delete. Many non-technical Mac buyers have
  // no CLT and never needed one.
  //
  // A source checkout still needs both: that path clones the engine at the
  // pinned tag and runs the official installer script.
  if (ctx.packaged) {
    notes.push(
      "The JARVIS engine ships inside this application, so no developer tools are needed on this Mac.",
    );
  } else {
    const missingRequired = ["git", "curl"].filter((command) => !commandAvailable(command));
    if (missingRequired.length > 0) {
      throw new WizardError(
        `Missing required command${missingRequired.length === 1 ? "" : "s"}: ${missingRequired.join(", ")}.`,
        "This is a source checkout, which installs the engine from the network. "
        + "Install the missing command, then rerun jarvis-setup — or use the packaged JARVIS app, which needs neither.",
        { fatal: true },
      );
    }
    const gitVersion = await run(ctx, "git", ["--version"]);
    if (gitVersion.status !== 0) {
      throw new WizardError(
        "git was found, but it could not run.",
        platform === "darwin"
          ? "macOS just showed an 'install developer tools' dialog — click Install, wait for it to finish, then rerun JARVIS setup. "
            + "(The packaged JARVIS app does not need them at all.)"
          : "Finish installing or repairing git, confirm git --version works, then rerun JARVIS setup.",
        { fatal: true },
      );
    }
    notes.push(`${gitVersion.stdout.trim() || "git --version succeeded"}; curl is present.`);
  }

  // Lane C (v1.4.0): voice no longer needs Homebrew or a system audio library —
  // the speech engines and their models ship inside the application. Homebrew
  // is still worth noting for other optional tooling, but it is not a voice
  // prerequisite and must not be described as one.
  if (platform === "darwin") {
    notes.push(commandAvailable("brew")
      ? "Homebrew is present for optional tooling."
      : "Homebrew is not present; nothing JARVIS needs depends on it.");
  }

  if (!["darwin", "linux"].includes(platform)) {
    notes.push("This OS is not one of the usual Hermes targets; continuing, but service setup may need manual work.");
  }

  return { status: "done", notes };
}

