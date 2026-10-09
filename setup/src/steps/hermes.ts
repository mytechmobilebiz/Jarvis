import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { pathExists, readTextIfExists } from "../fs-utils.js";
import {
  bundledUvPath,
  commandExists,
  confirmManualRun,
  formatCommand,
  handoffHermesEnvironment,
  hermesInvocation,
  hermesVenvExecutablePath,
  managedGitPath,
  runCommand,
  runShell,
  type CommandResult,
} from "../system.js";
import { enumerateGatewayPids } from "../legacy-cleanup.js";
import { warn } from "../ui.js";
import { readVendorManifest } from "../vendor.js";
import { installVendoredEngine, vendoredFailureOutcome } from "../vendored-engine.js";

export const HERMES_VERSION = "v2026.7.1";
export const WINDOWS_INSTALLER_URL = `https://raw.githubusercontent.com/NousResearch/hermes-agent/${HERMES_VERSION}/scripts/install.ps1`;
/**
 * F1 (ZIP-TRIAGE-260814): the official installer runs NON-INTERACTIVELY with
 * its own setup wizard suppressed. Without the flags, upstream's `main()` ran
 * its interactive setup wizard and gateway stages inside OUR wizard whenever a
 * tty existed — a double-wizard, where a Ctrl-C in theirs failed our step —
 * and any interactive stall left a repo-without-venv half-install behind.
 * Both flags are supported by the pinned tag's install.sh (--skip-setup skips
 * the wizard stage, --non-interactive skips every stage that needs input).
 */
const UNIX_INSTALL_SCRIPT =
  "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup --non-interactive";

/**
 * A transient network hiccup on the very first fetch used to leave a buyer on an
 * unknown Hermes build for good, so the pin is retried before it is believed.
 */
export const PIN_ATTEMPTS = 3;
const PIN_RETRY_DELAY_MS = 1500;

/**
 * Why the pin could not be applied. These strings go into onboarding telemetry
 * verbatim, so they must stay a closed set of machine-readable codes — never
 * git's stderr, which carries the buyer's home-directory path.
 */
export type PinFailure =
  | "agent-dir-missing"
  | "not-a-git-checkout"
  | "fetch-failed"
  | "checkout-failed"
  | "git-unavailable"
  | "fetch-transport"
  | "fetch-repository"
  | "checkout-conflict";

export interface PinOutcome {
  /** True ONLY when the tree is provably at the supported tag. */
  pinned: boolean;
  /**
   * True when Hermes lives outside the folder JARVIS manages, so the pin is
   * unknowable AND unfixable by rerunning. Distinct from `pinned` on purpose:
   * setup continues, but nothing here claims the release was verified.
   */
  unmanaged: boolean;
  notes: string[];
  /** How many fetch+checkout rounds were spent. */
  attempts: number;
  failure?: PinFailure;
  /** Release stamp read back from `hermes --version`, e.g. "2026.7.1". */
  installedVersion?: string;
  /** True when fetch/checkout failed but HEAD was already the tag. */
  verifiedByTag: boolean;
  /** Git's numeric status only; stderr is deliberately never retained. */
  gitExitCode?: number;
  /** A failed checkout also had no HEAD, so the official installer must self-heal it. */
  repairRequired?: boolean;
}

interface HermesOverrides {
  /** Injected in tests; production reads the manifest from ctx.vendorRoot. */
  readManifest?: typeof readVendorManifest;
  installVendored?: typeof installVendoredEngine;
  platform?: NodeJS.Platform;
  commandAvailable?: (command: string) => boolean;
  run?: HermesRunner;
  runShell?: typeof runShell;
  exists?: (filePath: string) => Promise<boolean>;
  delay?: (ms: number) => Promise<void>;
  handoff?: typeof handoffHermesEnvironment;
  /** Injected in tests; production lists real service directories. */
  listDir?: (dir: string) => Promise<string[]>;
  homeDir?: () => string;
}

type HermesRunner = typeof runCommand;

/**
 * Durable installer-gateway cleanup debt (review round 16): a warning alone
 * dies with the run — this run's Start Engine step and every later run must
 * keep refusing to start the supervised gateway until the competing one is
 * provably gone. The record is what a later probe can verify: the surviving
 * pids and the installer-created unit files (which remain OURS to remove on
 * retry; a customer's pre-existing unit is never recorded here).
 */
export const GATEWAY_DEBT_FILE = "gateway-cleanup-debt.json";

/** What `systemctl is-enabled` said about a unit BEFORE the installer ran. */
export type EnabledBaseline = "enabled" | "disabled" | "unknown";

interface ReactivatedUnitRecord {
  unit: string;
  /** Legacy boolean kept for older debt records; `baseline` is authoritative. */
  wasEnabled: boolean;
  /** Three-state (integration review r2 P1): a FAILED baseline query must
   * never read as "disabled" — that would let settle disable a unit the
   * customer had enabled all along. */
  baseline?: EnabledBaseline;
}

interface GatewayDebtRecord {
  pids: number[];
  units: string[];
  /** Customer units the installer may have re-enabled — never removed by us;
   * settle requires them inactive (and back at their baseline enabled
   * state) again. */
  reactivatedUnits?: ReactivatedUnitRecord[];
}

function gatewayDebtPath(ctx: WizardContext): string {
  return path.join(ctx.profileDir, GATEWAY_DEBT_FILE);
}

/**
 * Settle recorded cleanup debt, or report why the gateway may not start.
 *  - "none": no debt recorded.
 *  - "cleared": every recorded pid is gone (or, for a blind-scan record, one
 *    conclusive scan shows no gateway at all) and every recorded
 *    installer-created unit could be removed; the record is deleted.
 *  - "blocked": the competing gateway may still exist — the caller must not
 *    start the supervised gateway.
 */
export async function settleGatewayCleanupDebt(
  ctx: WizardContext,
  overrides: { run?: HermesRunner; platform?: NodeJS.Platform } = {},
): Promise<"none" | "cleared" | "blocked"> {
  if (ctx.dryRun) return "none";
  const platform = overrides.platform ?? process.platform;
  if (platform === "win32") return "none";
  const run = overrides.run ?? runCommand;
  const debtPath = gatewayDebtPath(ctx);
  let debt: GatewayDebtRecord;
  try {
    const text = await readTextIfExists(debtPath);
    if (text === undefined) return "none";
    const parsed = JSON.parse(text) as Partial<GatewayDebtRecord>;
    debt = {
      pids: Array.isArray(parsed.pids) ? parsed.pids.filter((pid): pid is number => Number.isInteger(pid)) : [],
      units: Array.isArray(parsed.units) ? parsed.units.filter((unit): unit is string => typeof unit === "string") : [],
      reactivatedUnits: Array.isArray(parsed.reactivatedUnits)
        ? parsed.reactivatedUnits.flatMap((entry): ReactivatedUnitRecord[] => {
          // Legacy string entries predate the enabled baseline. UNKNOWN, not
          // disabled: their baseline was never actually observed (r2 P1).
          if (typeof entry === "string") return [{ unit: entry, wasEnabled: false, baseline: "unknown" }];
          if (typeof entry === "object" && entry !== null && typeof (entry as { unit?: unknown }).unit === "string") {
            const wasEnabled = (entry as { wasEnabled?: unknown }).wasEnabled === true;
            const rawBaseline = (entry as { baseline?: unknown }).baseline;
            const baseline: EnabledBaseline = rawBaseline === "enabled" || rawBaseline === "disabled" || rawBaseline === "unknown"
              ? rawBaseline
              // Records written before the three-state fix sampled is-enabled
              // POST-install with a bare boolean; trust "enabled" (never
              // disable) and downgrade "false" to unknown rather than let a
              // possibly-failed query authorize a disable.
              : (wasEnabled ? "enabled" : "unknown");
            return [{ unit: (entry as { unit: string }).unit, wasEnabled, baseline }];
          }
          return [];
        })
        : [],
    };
  } catch {
    return "blocked"; // unreadable debt is still debt
  }
  // Installer-created units are ours to remove — retry the removal now.
  if (debt.units.length > 0) {
    const stop = hermesInvocation(ctx, ["gateway", "stop"], platform);
    await runSafely(ctx, stop.command, stop.args, run);
    const uninstall = hermesInvocation(ctx, ["gateway", "uninstall"], platform);
    await runSafely(ctx, uninstall.command, uninstall.args, run);
    for (const unit of debt.units) {
      if (await pathExists(unit)) return "blocked";
    }
  }
  // A customer unit the installer may have re-enabled is never removed by
  // us — it must be INACTIVE again, and back at its baseline enabled state,
  // before the supervised gateway starts next to it (rounds 17-18). An
  // installer-ENABLED unit (baseline disabled) is disabled again — undoing
  // the installer's change, not the customer's — and only systemd's
  // explicit 'inactive'/'failed' answer settles; a query failure blocks.
  for (const entry of debt.reactivatedUnits ?? []) {
    const name = entry.unit.split("/").pop() ?? entry.unit;
    const userScoped = !entry.unit.startsWith("/etc/");
    const scope = userScoped ? ["--user"] : [];
    const baseline = entry.baseline ?? (entry.wasEnabled ? "enabled" : "unknown");
    if (baseline === "disabled") {
      // Conclusively disabled before the installer: an enablement now is the
      // installer's change, and undoing it restores the customer's state.
      const enabled = await runSafely(ctx, "systemctl", [...scope, "is-enabled", "--quiet", name], run);
      if (enabled.status === 0) {
        await runSafely(ctx, "systemctl", [...scope, "disable", name], run);
        const still = await runSafely(ctx, "systemctl", [...scope, "is-enabled", "--quiet", name], run);
        if (still.status === 0) return "blocked";
      }
    } else if (baseline === "unknown") {
      // The baseline query FAILED, so nothing may be disabled on a guess
      // (r2 P1: a customer's own enablement is not ours to undo). But the
      // debt cannot settle while the unit IS enabled either — that may be
      // the installer's change surviving. Only a conclusive "not enabled"
      // answer lets this entry pass.
      const enabled = await runSafely(ctx, "systemctl", [...scope, "is-enabled", name], run);
      if (enabled.status === 0) return "blocked";
      const state = enabled.stdout.trim();
      // "not-found" included (review r3 P1): a unit the customer has since
      // REMOVED cannot restart anything — blocking on it forever would keep
      // Start Engine dark on a machine that is actually clean.
      const conclusiveNotEnabled = ["disabled", "masked", "masked-runtime", "static", "indirect", "linked", "linked-runtime", "not-found"].includes(state);
      if (!conclusiveNotEnabled) return "blocked"; // a failed QUERY is not proof
    }
    const active = await runSafely(ctx, "systemctl", [...scope, "is-active", name], run);
    const answer = active.stdout.trim();
    if (active.status === 0) return "blocked";
    if (answer !== "inactive" && answer !== "failed") return "blocked"; // a failed QUERY is not proof
  }
  try {
    const scan = await enumerateGatewayPids(ctx, platform, run, {});
    if (!scan.ok) return "blocked";
    if (debt.pids.length > 0) {
      if (debt.pids.some((pid) => scan.pids.includes(pid))) return "blocked";
    } else if (debt.units.length === 0 && (debt.reactivatedUnits ?? []).length === 0 && scan.pids.length > 0) {
      // A blind-scan record clears only on a conclusive EMPTY scan.
      return "blocked";
    }
  } catch {
    return "blocked";
  }
  try {
    await fs.rm(debtPath, { force: true });
  } catch {
    // A record we cannot delete keeps blocking the next run — safe side.
    return "blocked";
  }
  return "cleared";
}

export async function hermes(
  ctx: WizardContext,
  overrides: HermesOverrides = {},
): Promise<StepOutcome> {
  // The abort can arrive between runSetup's per-step check and this step's
  // first filesystem-changing action — an aborted retention probe, for one
  // (review round 18). Nothing here may rename or copy an engine after the
  // customer closed setup.
  const setupAborted = (): boolean => ctx.signal?.aborted === true;
  if (setupAborted()) {
    return { status: "failed", notes: ["Setup was cancelled."] };
  }
  const platform = overrides.platform ?? process.platform;
  const commandAvailable = overrides.commandAvailable
    ?? ((command: string) => commandExists(ctx, command, { platform }));
  const run = overrides.run ?? runCommand;
  const shell = overrides.runShell ?? runShell;
  const exists = overrides.exists ?? pathExists;
  const delay = overrides.delay
    ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const handoff = overrides.handoff ?? handoffHermesEnvironment;
  const notes: string[] = [];
  let installerRuns = 0;

  // v1.4.0 appliance path: a packaged build carries the pinned engine, an
  // offline wheelhouse and its own Python, so this step never touches git, the
  // official installer script, or pypi. A source checkout has no payload and
  // falls through to the network install below, which developers and Linux
  // still use.
  const readManifest = overrides.readManifest ?? readVendorManifest;
  let manifest;
  try {
    manifest = await readManifest(ctx.vendorRoot, { requireTrust: ctx.packaged });
  } catch (error) {
    // An invalid manifest in a packaged app is a broken download, not an
    // invitation to go and fetch an engine from the internet.
    if (ctx.packaged) return packagedPayloadFailure(error instanceof Error ? error.message : String(error));
    manifest = undefined;
  }
  if (manifest === undefined && ctx.packaged) {
    // THE offline guarantee (review r1, finding 4). Before this, a damaged
    // installer silently fell through to the official network installer —
    // violating the promise the whole lane exists to keep, on exactly the
    // machines least able to survive it, and reporting the wrong cause.
    return packagedPayloadFailure("The bundled JARVIS engine is missing from this installation.");
  }
  if (manifest !== undefined) {
    const install = overrides.installVendored ?? installVendoredEngine;
    const result = await install(ctx, ctx.vendorRoot as string, manifest, {
      platform,
      ...(overrides.run === undefined ? {} : { run: overrides.run }),
      ...(overrides.exists === undefined ? {} : { exists: overrides.exists }),
    });
    const props: Record<string, unknown> = {
      engine_vendored: true,
      engine_action: result.action,
      hermes_version: manifest.engineVersion,
    };
    if (result.failure !== undefined) return vendoredFailureOutcome(result, props);
    return {
      status: "done",
      notes: result.notes,
      props: { ...props, hermes_pinned: true, hermes_venv_present: !ctx.dryRun },
    };
  }

  // Where the engine registers a background gateway service. Snapshotting
  // these BEFORE the installer runs is what lets us tell "the installer just
  // created this" from "the customer already ran a Hermes gateway service
  // here" (review round 2, finding 1) — only the former may be removed.
  const home = (overrides.homeDir ?? (() => os.homedir()))();
  // The seam THROWS on failure — the snapshot below is what decides that
  // only genuine absence (ENOENT/ENOTDIR) reads as an authoritative empty
  // directory, and anything else makes the scan inconclusive.
  const listDir = overrides.listDir ?? ((dir: string) => fs.readdir(dir));
  const gatewayUnitDirs: Array<{ dir: string; isUnit: (name: string) => boolean }> = [
    {
      dir: path.posix.join(home, ".config", "systemd", "user"),
      isUnit: (name) => name.startsWith("hermes-gateway") && name.endsWith(".service"),
    },
    {
      dir: "/etc/systemd/system",
      isUnit: (name) => name.startsWith("hermes-gateway") && name.endsWith(".service"),
    },
    {
      dir: path.posix.join(home, "Library", "LaunchAgents"),
      isUnit: (name) => name.startsWith("ai.hermes.gateway") && name.endsWith(".plist"),
    },
  ];
  // Like the pid snapshot below, the unit snapshot carries its own
  // uncertainty (review round 5, finding 1): a baseline readdir that failed
  // with EACCES must not read as "no units existed", or a pre-existing
  // customer service would be classified as installer-created and removed.
  const snapshotGatewayUnits = async (): Promise<{ units: Set<string>; ok: boolean }> => {
    const units = new Set<string>();
    let ok = true;
    for (const { dir, isUnit } of gatewayUnitDirs) {
      try {
        for (const name of await listDir(dir)) {
          if (isUnit(name)) units.add(path.posix.join(dir, name));
        }
      } catch (error) {
        const code = (error as NodeJS.ErrnoException)?.code;
        if (code !== "ENOENT" && code !== "ENOTDIR") ok = false;
      }
    }
    return { units, ok };
  };
  // Unit files are only half the story (review round 3, finding 1): without
  // systemd — macOS launchd aside, Termux, plain unix — the installer starts
  // the gateway as a nohup'd BACKGROUND PROCESS and writes no unit at all.
  // Attribution is temporal: a gateway pid that appears during the installer
  // run was started by it (the default-profile gateway names no profile dir,
  // so textual ownership can never claim it).
  // The scan's own uncertainty is carried, not flattened (review round 4,
  // finding 2): a failed BASELINE scan reads as an authoritative empty set,
  // which would classify a pre-existing gateway as installer-created and
  // stop it. No conclusive baseline → no pid-based cleanup.
  const snapshotGatewayPids = async (): Promise<{ pids: Set<number>; ok: boolean }> => {
    try {
      const scan = await enumerateGatewayPids(ctx, platform, run, {});
      return { pids: new Set(scan.pids), ok: scan.ok };
    } catch {
      return { pids: new Set<number>(), ok: false };
    }
  };

  let installerFailed = false;
  // True while a gateway the installer started may still be alive — durable
  // cleanup debt that must keep the step from ending "done" (review round 5,
  // finding 2): a completed setup never re-runs the purge on normal
  // launches, so a mere warning would leave the competing gateway running
  // indefinitely. PERSISTED (round 16): the Start Engine step refuses to
  // spawn while the record exists, this run and every later one, until
  // settleGatewayCleanupDebt proves the competitor gone.
  let gatewayCleanupDebt = false;
  const recordGatewayDebt = async (detail: GatewayDebtRecord): Promise<void> => {
    gatewayCleanupDebt = true;
    // The in-run carrier: even when the WRITE below fails (read-only
    // profile, full disk), this run's Start Engine step still refuses to
    // spawn (review round 18). (Guarded: production contexts always carry
    // state; minimal test fixtures may not.)
    if (ctx.state !== undefined) ctx.state.gatewayCleanupDebt = true;
    try {
      await fs.mkdir(ctx.profileDir, { recursive: true });
      await fs.writeFile(gatewayDebtPath(ctx), JSON.stringify(detail), "utf8");
    } catch {
      // The context flag blocks this run; the next run's manual-path
      // cleanup re-detects what it can.
    }
  };
  const clearGatewayDebtRecord = async (): Promise<void> => {
    try {
      await fs.rm(gatewayDebtPath(ctx), { force: true });
    } catch {
      // Best-effort; settleGatewayCleanupDebt re-verifies anyway.
    }
  };

  // The exact trigger list the pinned install.sh's maybe_start_gateway
  // checks in the root $HERMES_HOME/.env before offering to start a gateway.
  const rootEnvHasMessagingToken = async (): Promise<boolean> => {
    try {
      const text = await readTextIfExists(path.join(ctx.hermesRoot, ".env"));
      if (text === undefined) return false;
      for (const key of ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "WHATSAPP_ENABLED"]) {
        const match = new RegExp(`^${key}=(.*)$`, "m").exec(text);
        const value = match?.[1]?.trim() ?? "";
        if (value !== "" && value !== "your-token-here") return true;
      }
      return false;
    } catch {
      // An unreadable root .env cannot rule the trigger out.
      return true;
    }
  };

  // The LIVE installer's maybe_start_gateway stage can install and start a
  // background gateway on unix — as a service unit, or as a nohup'd process
  // where systemd is absent — because its non-interactive prompt defaults to
  // YES whenever the root $HERMES_HOME/.env carries a messaging token and a
  // tty exists (F1 review rounds 1–3). That is exactly the machinery the
  // Earlier Install purge removes, and it would fight the HUD-supervised
  // gateway. Undo it — but ONLY what this installer run provably created
  // (both snapshot ends conclusive), and claim only what a re-scan confirms.
  // Which of these unit files were ACTIVE services at baseline? Asked with
  // systemd's own answer, PER UNIT, at the right bus (rounds 16-17): one
  // profile's running service must not vouch for another profile's
  // inactive unit.
  const activeUnitsAtBaseline = async (units: Set<string>): Promise<Set<string>> => {
    const active = new Set<string>();
    for (const unit of units) {
      if (!unit.endsWith(".service")) continue;
      const name = unit.split("/").pop() ?? unit;
      const args = unit.startsWith("/etc/")
        ? ["is-active", "--quiet", name]
        : ["--user", "is-active", "--quiet", name];
      const result = await runSafely(ctx, "systemctl", args, run);
      if (result.status === 0) active.add(unit);
    }
    return active;
  };
  /**
   * The ENABLED state of pre-existing service units before the installer ran
   * — three-state (r2 P1): a failed query is UNKNOWN, never "disabled",
   * because "disabled at baseline" is what later authorizes settle to run
   * `systemctl disable` on the unit.
   */
  const enabledUnitsAtBaseline = async (units: Set<string>): Promise<Map<string, EnabledBaseline>> => {
    const states = new Map<string, EnabledBaseline>();
    for (const unit of units) {
      if (!unit.endsWith(".service")) continue;
      const name = unit.split("/").pop() ?? unit;
      const args = unit.startsWith("/etc/")
        ? ["is-enabled", name]
        : ["--user", "is-enabled", name];
      const result = await runSafely(ctx, "systemctl", args, run);
      if (result.status === 0) {
        states.set(unit, "enabled");
        continue;
      }
      const state = result.stdout.trim();
      states.set(
        unit,
        ["disabled", "masked", "masked-runtime", "static", "indirect", "linked", "linked-runtime", "not-found"].includes(state)
          ? "disabled"
          : "unknown",
      );
    }
    return states;
  };

  const cleanupInstallerGateway = async (
    preUnits: { units: Set<string>; ok: boolean },
    prePids: { pids: Set<number>; ok: boolean },
    preActiveUnits: Set<string>,
    preEnabledStates: Map<string, EnabledBaseline>,
  ): Promise<void> => {
    const postUnits = await snapshotGatewayUnits();
    const unitScanConclusive = preUnits.ok && postUnits.ok;
    const createdUnits = unitScanConclusive
      ? [...postUnits.units].filter((unit) => !preUnits.units.has(unit))
      : [];
    const postPids = await snapshotGatewayPids();
    const rawNewPids = prePids.ok && postPids.ok
      ? [...postPids.pids].filter((pid) => !prePids.pids.has(pid))
      : [];
    // Attribution of a pid delta, by what the baseline can account for
    // (review rounds 10–11):
    //  - Pre-existing SYSTEMD units: suppress. A customer's service that
    //    restarts mid-install shows a fresh pid that is not the installer's,
    //    and on a systemd machine the installer's gateway stage registers a
    //    unit (visible in createdUnits), never a bare nohup process.
    //  - Pre-existing LAUNCHD plists only: upstream's installer checks for
    //    systemctl, not for plists, so on macOS it still takes the nohup
    //    branch — a new pid is genuinely AMBIGUOUS (installer nohup vs the
    //    customer's KeepAlive service restarting). Touch nothing; keep the
    //    debt honestly.
    // A .service FILE only accounts for restarts when systemd can actually
    // run it (review round 12): without systemctl the installer nohups and
    // the stale unit file is an inert relic, so attribution proceeds.
    // And a pre-existing unit only accounts for a NEW pid when its service
    // was RUNNING at baseline (review round 13): an INACTIVE unit that gains
    // a pid mid-install was activated by the installer's `gateway start` —
    // that pid is installer-caused, and stopping it leaves the customer's
    // unit installed and in its baseline (inactive) state.
    const runningAtBaseline = prePids.ok && prePids.pids.size > 0;
    // "Some gateway pid existed" does not prove THIS unit was running —
    // several profiles can coexist (rounds 16-17). A fresh pid is explained
    // as a restart only when EVERY pre-existing service unit was active at
    // baseline; one inactive unit among them may be the one the installer
    // just activated, so suppression is off and the activation-debt rule
    // below takes over.
    const preServiceUnits = [...preUnits.units].filter((unit) => unit.endsWith(".service"));
    const preSystemd = preUnits.ok
      && commandAvailable("systemctl")
      && preServiceUnits.length > 0
      && preServiceUnits.every((unit) => preActiveUnits.has(unit));
    const preLaunchdOnly = preUnits.ok && !preSystemd && [...preUnits.units].some((unit) => unit.endsWith(".plist"));
    const ambiguousUnderLaunchd = preLaunchdOnly && runningAtBaseline && rawNewPids.length > 0;
    const newPids = preUnits.ok && !preSystemd && !ambiguousUnderLaunchd ? rawNewPids : [];
    if (ambiguousUnderLaunchd) {
      await recordGatewayDebt({ pids: rawNewPids, units: [] });
      const warning =
        "A Hermes gateway appeared while the official installer ran, but JARVIS could not tell whether "
        + "the installer or your own Hermes service started it — nothing was touched.";
      warn(ctx, warning);
      notes.push(warning);
      if (createdUnits.length === 0) return;
    }
    if (createdUnits.length === 0 && newPids.length === 0) {
      if (unitScanConclusive && prePids.ok && postPids.ok) return;
      // Nothing observed, but the scans could not establish absence (no
      // pgrep, unreadable unit dirs). Mirror the installer's OWN trigger
      // gate: its gateway stage only fires when the root $HERMES_HOME/.env
      // carries a messaging token — no token, no gateway, conclusively
      // (review round 7, finding 1). With a token and blind scans, the
      // debt stands: an unobserved gateway may be processing messages.
      if (!(await rootEnvHasMessagingToken())) return;
      await recordGatewayDebt({ pids: [], units: [] });
      const warning =
        "This computer could not be checked for a background gateway the official installer may have started.";
      warn(ctx, warning);
      notes.push(warning);
      return;
    }
    const stop = hermesInvocation(ctx, ["gateway", "stop"], platform);
    const stopped = await runSafely(ctx, stop.command, stop.args, run);
    let uninstalled = true;
    if (createdUnits.length > 0) {
      const uninstall = hermesInvocation(ctx, ["gateway", "uninstall"], platform);
      uninstalled = (await runSafely(ctx, uninstall.command, uninstall.args, run)).status === 0;
    }
    const verifyPids = newPids.length > 0 ? await snapshotGatewayPids() : { pids: new Set<number>(), ok: true };
    const pidSurvivors = verifyPids.ok ? newPids.filter((pid) => verifyPids.pids.has(pid)) : newPids;
    // An installer 'gateway install' against a PRE-EXISTING unit rewrites and
    // enables that unit in place — nothing shows in createdUnits, and we can
    // stop the process but cannot restore the unit's prior content or
    // enabled state (review round 14). Debt, honestly worded: the retry (and
    // the customer's restart) is the recovery, never a silent success claim.
    const reactivationCandidates = [...preUnits.units].filter(
      (unit) => unit.endsWith(".service") && !preActiveUnits.has(unit),
    );
    // Baseline ENABLED state per candidate (round 18): 'gateway stop' makes
    // a unit inactive but not disabled — a unit the installer ENABLED must
    // be disabled again before the debt may clear, and one that was already
    // enabled at baseline is the customer's own arrangement. The baseline is
    // the PRE-INSTALL sample (round 19): reading is-enabled here, after the
    // installer ran, would record an installer-made enablement as the
    // customer's own and never undo it — the unit would revive the gateway
    // at the next login with the debt marked settled.
    const reactivationBaseline: ReactivatedUnitRecord[] =
      reactivationCandidates.map((unit) => {
        const baseline = preEnabledStates.get(unit) ?? "unknown";
        return { unit, wasEnabled: baseline === "enabled", baseline };
      });
    const activatedPreexistingUnit = newPids.length > 0
      && preUnits.ok
      // Only systemd can have rewritten/enabled/started that unit; without
      // systemctl the file is an inert relic and the pid was a plain nohup.
      && commandAvailable("systemctl")
      && reactivationCandidates.length > 0;
    const verifyUnits = createdUnits.length > 0 ? await snapshotGatewayUnits() : { units: new Set<string>(), ok: true };
    const unitSurvivors = verifyUnits.ok
      ? createdUnits.filter((unit) => verifyUnits.units.has(unit))
      : createdUnits;
    // Success requires EVERY certainty (review round 6, finding 2): a new
    // gateway process seen while the unit scan was inconclusive may have an
    // enabled unit we could not observe — that restarts at the next login,
    // so mixed certainty keeps the debt.
    const fullyObserved = unitScanConclusive || newPids.length === 0;
    if (
      stopped.status === 0 && uninstalled && pidSurvivors.length === 0
      && unitSurvivors.length === 0 && fullyObserved && !activatedPreexistingUnit
    ) {
      notes.push("Stopped the background gateway the official installer started; JARVIS supervises its own engine.");
      await clearGatewayDebtRecord();
    } else {
      await recordGatewayDebt({
        pids: pidSurvivors.length > 0 ? pidSurvivors : newPids,
        units: unitSurvivors,
        // A pre-existing unit the installer may have re-enabled cannot be
        // removed by us — settle requires it INACTIVE again before the
        // supervised gateway may start (round 17): a stopped pid alone
        // proves nothing once the unit can revive it at the next login.
        reactivatedUnits: activatedPreexistingUnit ? reactivationBaseline : [],
      });
      const warning = "The official installer started a background gateway that could not be fully removed.";
      warn(ctx, warning);
      notes.push(warning);
    }
  };

  const installOfficialHermes = async (): Promise<void> => {
    if (installerRuns >= 1) return;
    installerRuns += 1;
    const snapshot = platform !== "win32" && !ctx.dryRun;
    const preUnits = snapshot ? await snapshotGatewayUnits() : { units: new Set<string>(), ok: false };
    const prePids = snapshot ? await snapshotGatewayPids() : { pids: new Set<number>(), ok: false };
    const preActiveUnits = snapshot && preUnits.ok ? await activeUnitsAtBaseline(preUnits.units) : new Set<string>();
    // Sampled BEFORE the installer for the same reason preActiveUnits is
    // (round 19): only the pre-install answer distinguishes "the customer had
    // this enabled" from "the installer just enabled it".
    const preEnabledStates = snapshot && preUnits.ok
      ? await enabledUnitsAtBaseline(preUnits.units)
      : new Map<string, EnabledBaseline>();
    const invocation = platform === "win32"
      ? windowsInstallerInvocation(
          ctx,
          ["powershell.exe", "pwsh"].find(commandAvailable) ?? "powershell.exe",
        )
      : { command: "bash", args: ["-lc", UNIX_INSTALL_SCRIPT], display: UNIX_INSTALL_SCRIPT };
    const install = platform === "win32"
      ? await run(ctx, invocation.command, invocation.args, { stream: true })
      // detachTty: the installer runs with NO controlling terminal, so its
      // own "(: </dev/tty)" gate skips the gateway stage deterministically
      // ("Gateway setup skipped (no terminal available)") — prevention, not
      // attribution, closes the auto-install path (review round 14). Output
      // still streams through the inherited fds. The snapshot machinery
      // below stays armed for the MANUAL command, which the customer runs
      // in their own real terminal.
      : await shell(ctx, UNIX_INSTALL_SCRIPT, { stream: true, detachTty: true });
    // A CANCELLED installer (runCommand resolves 130 on abort) is not a
    // failed one (round 17): no cleanup diff, no debt, no manual prompt, no
    // repair — the run is ending and the caller returns immediately.
    if (setupAborted()) {
      installerFailed = true;
      return;
    }
    if (install.status !== 0) {
      if (!(await confirmManualRun(ctx, invocation.display))) {
        // NOT an immediate failure (F1 review round 1): the failed run — or
        // an earlier one — may have left a usable checkout behind, and the
        // pin + wizard-side venv rebuild below are precisely the repair for
        // that half-install shape. The step fails with the installer's
        // message only when that direct repair cannot produce a working
        // engine either. NO attribution cleanup here (round 18): the
        // detached auto attempt provably could not start a gateway, so a
        // diff would only misattribute concurrent customer activity.
        installerFailed = true;
        notes.push("The official Hermes installer did not complete; trying to repair the existing engine files directly.");
        return;
      }
      notes.push("Hermes was installed in a terminal, as you confirmed.");
      // ONLY the manual path gets the attribution cleanup (round 18): the
      // customer's own terminal HAS a controlling tty, so the installer's
      // gateway stage can fire there — while the detached auto path cannot,
      // making attribution there pure false-positive surface.
      if (snapshot) await cleanupInstallerGateway(preUnits, prePids, preActiveUnits, preEnabledStates);
    } else {
      notes.push(
        platform === "win32"
          ? `Hermes ${HERMES_VERSION} installed from the official Windows installer.`
          : "Hermes installed from the official installer.",
      );
    }
  };

  // A done outcome is never issued over live cleanup debt: failing the step
  // is what guarantees a retry (the purge and this cleanup both run again).
  const gatewayDebtOutcome = (props: Record<string, unknown>): StepOutcome | undefined => {
    if (!gatewayCleanupDebt) return undefined;
    return {
      status: "failed",
      notes,
      props: { ...props, gateway_cleanup_failed: true },
      nextActions: [
        "Restart this computer (or stop any running Hermes gateway), then run setup again.",
        "If it keeps failing, email jarvis.agent.tech@pm.me and a real person will help.",
      ],
    };
  };

  const healthy = await hermesInstallIsHealthy(ctx, platform, run, exists);
  if (healthy) {
    notes.push("Hermes is already installed.");
  } else {
    notes.push("The existing Hermes files did not form a runnable, versioned install; the official installer will repair them.");
    await installOfficialHermes();
    if (setupAborted()) {
      return { status: "failed", notes: [...notes, "Setup was cancelled."] };
    }
  }

  if (!ctx.dryRun) handoff(ctx, { platform });

  let pin = await pinHermes(ctx, platform, run, exists, delay);
  if (pin.repairRequired === true && installerRuns === 0) {
    notes.push("The Hermes checkout has no valid HEAD; rerunning the official installer once so it can move the broken checkout aside.");
    await installOfficialHermes();
    if (!ctx.dryRun) handoff(ctx, { platform });
    pin = await pinHermes(ctx, platform, run, exists, delay);
  }
  notes.push(...pin.notes);

  // Only closed-set, non-identifying values: see PinFailure. hermes_pinned is
  // strictly "provably at the tag", so an unmanaged install reports false and
  // is separated out — the whole point of these fields is telling the two
  // apart without a support round-trip.
  const props: Record<string, unknown> = {
    hermes_pinned: pin.pinned,
    pin_attempts: pin.attempts,
    ...(installerFailed ? { installer_failed: true } : {}),
    ...(gatewayCleanupDebt ? { gateway_cleanup_failed: true } : {}),
    ...(pin.unmanaged ? { hermes_unmanaged: true } : {}),
    ...(pin.verifiedByTag ? { pin_verified_by_tag: true } : {}),
    ...(pin.failure === undefined ? {} : { pin_failure: pin.failure }),
    ...(pin.installedVersion === undefined ? {} : { hermes_version: pin.installedVersion }),
    ...(pin.gitExitCode === undefined ? {} : { git_exit_code: pin.gitExitCode }),
  };

  // NOTE (review round 2, finding 4): an installer failure does NOT
  // short-circuit here. The existing !pin.pinned handling below already asks
  // the only question that matters — is there a functional engine? — and a
  // runnable unmanaged/unverified engine after a late-stage installer
  // failure is still a usable machine. installer_failed rides the props
  // either way, and the failed outcome's remedy is the same "run setup
  // again" the thrown error used to carry.
  if (!pin.pinned) {
    const functional = await hermesEngineIsFunctional(ctx, platform, run);
    if (functional) {
      if (!pin.unmanaged) {
        const warning =
          "Hermes is running an unverified engine build — JARVIS will keep trying to pin on future setup runs.";
        warn(ctx, warning);
        notes.push(warning);
      }
      return gatewayDebtOutcome(props) ?? { status: "done", notes, props };
    }

    return {
      status: "failed",
      notes,
      props,
      nextActions: [
        "Run setup again so the official Hermes installer can make one repair attempt.",
        "If it keeps failing, email jarvis.agent.tech@pm.me and a real person will help.",
      ],
    };
  }

  // The pin is real, but a pin is not a runnable engine: gate "done" on the
  // venv's programs actually existing and running, so a silent `uv sync`
  // failure can't leave the buyer stuck at a green step with no `hermes`
  // command (Scott Reed's confirmed root cause, 2026-08-11).
  if (!ctx.dryRun) {
    if (!(await hermesVenvIsHealthy(ctx, platform, run, exists))) {
      const message =
        "Hermes was set to the supported version, but its programs did not finish building — "
        + "this usually means the connection to pypi.org or files.pythonhosted.org was blocked or dropped.";
      warn(ctx, message);
      notes.push(message);
      return {
        status: "failed",
        notes,
        props: { ...props, hermes_venv_present: false },
        nextActions: [
          "Check this computer's internet or proxy — the engine also downloads from pypi.org and files.pythonhosted.org, not just GitHub.",
          "Then run setup again so JARVIS can finish building the engine's programs.",
          "If it keeps failing, email jarvis.agent.tech@pm.me and a real person will help.",
        ],
      };
    }
    props.hermes_venv_present = true;
  }

  return gatewayDebtOutcome(props) ?? { status: "done", notes, props };
}

/**
 * A packaged install with no usable payload fails HERE, before any
 * network-capable command runs. Never "fall back to downloading it": the
 * customer bought an appliance, their network may be filtered, and pretending
 * otherwise is what produced the August support load.
 */
function packagedPayloadFailure(reason: string): StepOutcome {
  return {
    status: "failed",
    notes: [reason, "JARVIS did not try to download an engine, because this build is meant to carry one."],
    props: { engine_vendored: false, engine_failure: "payload-missing", hermes_venv_present: false },
    nextActions: [
      "Download JARVIS again from the link in your purchase email and reinstall it — this copy is incomplete.",
      "If the new download does the same, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    ],
  };
}

export function windowsInstallerInvocation(
  ctx: WizardContext,
  powershell = "powershell.exe",
): { command: string; args: string[]; display: string } {
  const script = [
    `$installer = (Invoke-WebRequest -UseBasicParsing -Uri ${powerShellLiteral(WINDOWS_INSTALLER_URL)}).Content;`,
    "& ([ScriptBlock]::Create([string]$installer))",
    `-Tag ${powerShellLiteral(HERMES_VERSION)}`,
    "-NonInteractive",
    `-HermesHome ${powerShellLiteral(ctx.hermesRoot)}`,
    `-InstallDir ${powerShellLiteral(ctx.hermesAgentDir)}`,
  ].join(" ");
  const args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script];
  return { command: powershell, args, display: formatCommand(powershell, args) };
}

function powerShellLiteral(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

/**
 * `hermes --version` opens with e.g.
 *   Hermes Agent v0.18.0 (2026.7.1) · upstream c9a150d6
 * The parenthesised release stamp is the tag without its leading "v", which is
 * the only field that identifies which supported release is actually installed.
 */
export function parseHermesVersion(output: string): string | undefined {
  return /\(([0-9]{4}(?:\.[0-9]+)+)\)/.exec(output)?.[1];
}

export function expectedReleaseStamp(): string {
  return HERMES_VERSION.replace(/^v/, "");
}

async function hermesInstallIsHealthy(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: HermesRunner,
  exists: (filePath: string) => Promise<boolean>,
): Promise<boolean> {
  if (ctx.dryRun) return false;

  if (!(await exists(ctx.hermesAgentDir))) {
    const invocation = hermesInvocation(ctx, ["--version"], platform);
    const version = await runSafely(ctx, invocation.command, invocation.args, run);
    return version.status === 0;
  }

  const executable = hermesVenvExecutablePath(ctx, platform);
  if (!(await exists(executable))) return false;

  const version = await runSafely(ctx, executable, ["--version"], run);
  if (version.status !== 0) return false;

  const managedGit = managedGitPath(ctx, platform);
  const git = platform === "win32" && await exists(managedGit) ? managedGit : "git";
  const gitVersion = await runSafely(ctx, git, ["--version"], run);
  if (gitVersion.status !== 0) return false;

  const head = await runSafely(
    ctx,
    git,
    ["-C", ctx.hermesAgentDir, "rev-parse", "--verify", "HEAD"],
    run,
  );
  return head.status === 0;
}

/**
 * A pinned checkout is not a usable engine. `uv sync` builds the venv that
 * actually holds the `hermes` program, and on a filtered or dropped network it
 * can fail while the git pin (which only needs GitHub) still succeeds — leaving
 * the tree at the right tag but `<hermesAgentDir>/venv/Scripts/hermes.exe`
 * absent, so `hermes` is command-not-found forever (Scott Reed, 2026-08-11).
 * The venv's own `hermes --version` is the only proof its programs were built.
 */
async function hermesVenvIsHealthy(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: HermesRunner,
  exists: (filePath: string) => Promise<boolean>,
): Promise<boolean> {
  const executable = hermesVenvExecutablePath(ctx, platform);
  if (!(await exists(executable))) return false;
  const version = await runSafely(ctx, executable, ["--version"], run);
  return version.status === 0;
}

async function hermesEngineIsFunctional(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: HermesRunner,
): Promise<boolean> {
  const versionInvocation = hermesInvocation(ctx, ["--version"], platform);
  const version = await runSafely(ctx, versionInvocation.command, versionInvocation.args, run);
  if (version.status !== 0 || parseHermesVersion(`${version.stdout}\n${version.stderr}`) === undefined) {
    return false;
  }
  const helpInvocation = hermesInvocation(ctx, ["--help"], platform);
  const trivial = await runSafely(ctx, helpInvocation.command, helpInvocation.args, run);
  return trivial.status === 0;
}

async function runSafely(
  ctx: WizardContext,
  command: string,
  args: string[],
  run: HermesRunner,
): Promise<CommandResult> {
  try {
    return await run(ctx, command, args, { stream: false });
  } catch {
    return { status: 127, stdout: "", stderr: "" };
  }
}

export async function pinHermes(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: HermesRunner = runCommand,
  exists: (filePath: string) => Promise<boolean> = pathExists,
  delay: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
): Promise<PinOutcome> {
  const notes: string[] = [];
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const managedGit = managedGitPath(ctx, platform);
  const git = platform === "win32" && await exists(managedGit) ? managedGit : "git";
  const tagRef = `refs/tags/${HERMES_VERSION}`;
  const fetchArgs = [
    "-c",
    "windows.appendAtomically=false",
    "-C",
    ctx.hermesAgentDir,
    "fetch",
    "--depth",
    "1",
    "--force",
    "origin",
    `+${tagRef}:${tagRef}`,
  ];
  const checkoutArgs = ["-C", ctx.hermesAgentDir, "checkout", "--force", "--detach", tagRef];

  if (ctx.dryRun) {
    await run(ctx, git, fetchArgs, { stream: false });
    await run(ctx, git, checkoutArgs, { stream: false });
    notes.push(`[dry-run] would pin Hermes to ${HERMES_VERSION}, retrying up to ${PIN_ATTEMPTS} times.`);
    return { pinned: true, unmanaged: false, notes, attempts: 1, verifiedByTag: false };
  }

  const giveUp = async (
    failure: PinFailure,
    reason: string,
    attempts: number,
    gitExitCode?: number,
    repairRequired = false,
  ): Promise<PinOutcome> => {
    // The version probe is a DIAGNOSTIC, never proof: the release stamp in the
    // banner survives every commit made after the tag, so a tree sitting on
    // main reports the supported release while being nothing of the sort. Only
    // git can answer "is HEAD the tag", and it is asked separately below.
    const installedVersion = await readInstalledVersion(ctx, platform, run);

    // An install outside the folder JARVIS manages (a global or Homebrew
    // hermes, or Linux's /usr/local/lib/hermes-agent) cannot be pinned by us
    // and cannot be repaired by rerunning either — failing the step would
    // strand a working machine in a setup loop it can never leave. Say so
    // loudly, record it, and let setup continue.
    if (failure === "agent-dir-missing" || failure === "not-a-git-checkout") {
      const unmanaged =
        `${reason} JARVIS cannot confirm which Hermes release is installed, so it cannot guarantee the supported one`
        + `${installedVersion === undefined ? "" : ` (it reports ${installedVersion})`}.`;
      warn(ctx, unmanaged);
      notes.push(unmanaged);
      return {
        pinned: false,
        unmanaged: true,
        notes,
        attempts,
        failure,
        installedVersion,
        verifiedByTag: false,
        ...(gitExitCode === undefined ? {} : { gitExitCode }),
      };
    }

    const message = `JARVIS could not verify Hermes at ${HERMES_VERSION}. ${reason}`;
    warn(ctx, message);
    notes.push(message);
    return {
      pinned: false,
      unmanaged: false,
      notes,
      attempts,
      failure,
      installedVersion,
      verifiedByTag: false,
      ...(gitExitCode === undefined ? {} : { gitExitCode }),
      ...(repairRequired ? { repairRequired: true } : {}),
    };
  };

  if (!(await exists(ctx.hermesAgentDir))) {
    return giveUp("agent-dir-missing", "Hermes is installed somewhere JARVIS does not manage.", 0);
  }

  if (!(await exists(pathApi.join(ctx.hermesAgentDir, ".git")))) {
    return giveUp("not-a-git-checkout", "The Hermes program folder is not a versioned checkout.", 0);
  }

  // This is deliberately the first git operation. A machine already at the
  // supported tag completes offline and never touches its remote.
  if (await headIsPinned(ctx, git, run)) {
    const installedVersion = await readInstalledVersion(ctx, platform, run);
    if (installedVersion === expectedReleaseStamp()) {
      notes.push(`Hermes is already pinned to ${HERMES_VERSION}; no download was needed.`);
      return {
        pinned: true,
        unmanaged: false,
        notes,
        attempts: 0,
        installedVersion,
        verifiedByTag: true,
      };
    }
    notes.push(
      `The checkout is at the supported release but the runtime reports ${installedVersion ?? "an unknown version"}; resynchronizing.`,
    );
  }

  const gitVersion = await runSafely(ctx, git, ["--version"], run);
  if (gitVersion.status !== 0) {
    return giveUp(
      "git-unavailable",
      platform === "win32"
        ? "Git for Windows is not installed. Install it from git-scm.com/download/win with the default options, restart your computer, then open JARVIS again."
        : "Git is not installed. Open Terminal, run xcode-select --install, finish the dialog, then open JARVIS again.",
      0,
      gitVersion.status,
    );
  }

  let failure: PinFailure = "fetch-transport";
  let gitExitCode: number | undefined;
  let repairRequired = false;
  let attempts = 0;
  let pinned = false;
  for (let attempt = 1; attempt <= PIN_ATTEMPTS; attempt += 1) {
    attempts = attempt;
    const fetched = await runSafely(ctx, git, fetchArgs, run);
    if (fetched.status !== 0) {
      gitExitCode = fetched.status;
      failure = classifyFetchFailure(fetched);
    } else {
      const checkout = await runSafely(ctx, git, checkoutArgs, run);
      if (checkout.status !== 0) {
        gitExitCode = checkout.status;
        failure = checkout.status === 127 ? "git-unavailable" : "checkout-conflict";

        const head = await runSafely(
          ctx,
          git,
          ["-C", ctx.hermesAgentDir, "rev-parse", "--verify", "HEAD"],
          run,
        );
        if (head.status !== 0) {
          if (head.status === 127) {
            failure = "git-unavailable";
            gitExitCode = head.status;
          } else {
            repairRequired = true;
          }
          break;
        }
      } else if (await headIsPinned(ctx, git, run)) {
        pinned = true;
        break;
      } else {
        failure = "checkout-conflict";
        const described = await runSafely(
          ctx,
          git,
          ["-C", ctx.hermesAgentDir, "describe", "--tags", "--exact-match", "HEAD"],
          run,
        );
        gitExitCode = described.status;
      }
    }

    if (attempt < PIN_ATTEMPTS && failure !== "git-unavailable") {
      notes.push(`Setting Hermes to ${HERMES_VERSION} did not take on attempt ${attempt}; retrying.`);
      await delay(PIN_RETRY_DELAY_MS);
    } else if (failure === "git-unavailable") {
      break;
    }
  }

  if (!pinned) {
    return giveUp(
      failure,
      pinFailureCopy(failure, gitExitCode, attempts, repairRequired),
      attempts,
      gitExitCode,
      repairRequired,
    );
  }

  notes.push(`Hermes pinned to ${HERMES_VERSION}.`);

  const uvPath = bundledUvPath(ctx, platform);
  const venvPath = pathApi.join(ctx.hermesAgentDir, "venv");
  if ((await exists(uvPath)) && (await exists(pathApi.join(ctx.hermesAgentDir, "pyproject.toml")))) {
    // F1 (ZIP-TRIAGE-260814): the wizard can now rebuild a MISSING venv
    // itself instead of depending on upstream's installer to have finished
    // its venv stage. A failure anywhere inside the live 3.4k-line installer
    // after clone_repo used to leave repo-without-venv, and nothing on the
    // source path could repair it. `uv venv venv --python 3.11` mirrors the
    // pinned tag's own setup_venv exactly; `uv sync` then populates it. A
    // failed create is not fatal — uv sync creates the project environment
    // itself when absent, so the sync below still gets its chance.
    if (!(await exists(hermesVenvExecutablePath(ctx, platform)))) {
      const create = await run(ctx, uvPath, ["venv", "venv", "--python", "3.11"], {
        cwd: ctx.hermesAgentDir,
        stream: true,
      });
      notes.push(
        create.status === 0
          ? "Rebuilt the engine's missing Python environment."
          : "The engine's Python environment could not be pre-created; letting the dependency sync build it.",
      );
    }
    // Pin UV_PYTHON to the venv's own interpreter when it exists — upstream's
    // installer does the same, because an inherited UV_PYTHON (e.g. =3.14 in
    // the customer's shell) makes `uv sync` silently DELETE this venv and
    // recreate it at that version, failing on source-only transitive builds.
    const venvPython = pathApi.join(
      venvPath,
      ...(platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"]),
    );
    const sync = await run(ctx, uvPath, ["sync", "--extra", "all", "--locked", "--inexact"], {
      cwd: ctx.hermesAgentDir,
      env: {
        VIRTUAL_ENV: venvPath,
        UV_PROJECT_ENVIRONMENT: venvPath,
        // ALWAYS pinned (review round 10, finding 3): with no venv
        // interpreter to point at — the failed-create fallback — an
        // inherited UV_PYTHON (e.g. =3.14 in the customer's shell) would
        // otherwise steer the sync's own env creation to an unsupported
        // Python; the engine's supported version constrains it instead.
        UV_PYTHON: (await exists(venvPython)) ? venvPython : "3.11",
      },
      stream: true,
    });
    if (sync.status === 0) {
      notes.push("Hermes virtual environment synced after pin.");
    } else {
      notes.push("The engine install did not fully finish; if anything misbehaves, run setup again and it will repair itself.");
    }
  } else {
    notes.push("Hermes virtual environment sync was not needed.");
  }

  return {
    pinned: true,
    unmanaged: false,
    notes,
    attempts,
    installedVersion: await readInstalledVersion(ctx, platform, run),
    verifiedByTag: false,
  };
}

function classifyFetchFailure(result: CommandResult): PinFailure {
  if (result.status === 127) return "git-unavailable";
  const stderr = result.stderr.toLowerCase();
  if (
    /could not resolve host|failed to connect|connection (?:timed out|reset)|network is unreachable|tls|ssl|early eof|unable to access/.test(
      stderr,
    )
  ) {
    return "fetch-transport";
  }
  return result.status === 128 ? "fetch-repository" : "fetch-transport";
}

function pinFailureCopy(
  failure: PinFailure,
  gitExitCode: number | undefined,
  attempts: number,
  repairRequired: boolean,
): string {
  const exit = gitExitCode === undefined ? "" : ` (git exit ${gitExitCode})`;
  switch (failure) {
    case "git-unavailable":
      return `Git could not run${exit}; run setup again so the official installer can restore its managed Git tools.`;
    case "fetch-transport":
      return `The Hermes release download could not be reached after ${attempts} attempt${attempts === 1 ? "" : "s"}${exit}. Check this computer's internet or proxy connection, then run setup again.`;
    case "fetch-repository":
      return `The Hermes checkout's origin or tag records could not provide the supported release after ${attempts} attempt${attempts === 1 ? "" : "s"}${exit}. Run setup again; if it repeats, email support.`;
    case "checkout-conflict":
      return repairRequired
        ? `The Hermes checkout has no valid HEAD${exit}; the official installer must repair it.`
        : `Hermes could not switch its checkout to the supported release after ${attempts} attempt${attempts === 1 ? "" : "s"}${exit}. Close programs using Hermes files, then run setup again.`;
    default:
      return `Hermes could not be pinned after ${attempts} attempt${attempts === 1 ? "" : "s"}${exit}. Run setup again.`;
  }
}

/**
 * The only trustworthy answer to "is this tree the supported release".
 * `--exact-match` succeeds solely when HEAD is the annotated tag itself, so a
 * checkout that merely carries the tag's release date does not pass. Works
 * offline once the tag is local, which is exactly the already-pinned machine
 * whose later fetch fails on a dead network.
 */
async function headIsPinned(ctx: WizardContext, git: string, run: HermesRunner): Promise<boolean> {
  try {
    const described = await run(
      ctx,
      git,
      ["-C", ctx.hermesAgentDir, "describe", "--tags", "--exact-match", "HEAD"],
      { stream: false },
    );
    return described.status === 0 && described.stdout.trim() === HERMES_VERSION;
  } catch {
    return false;
  }
}

async function readInstalledVersion(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: HermesRunner,
): Promise<string | undefined> {
  try {
    const probeInvocation = hermesInvocation(ctx, ["--version"], platform);
    const probe = await run(ctx, probeInvocation.command, probeInvocation.args, { stream: false });
    if (probe.status !== 0) return undefined;
    return parseHermesVersion(`${probe.stdout}\n${probe.stderr}`);
  } catch {
    // A missing or unrunnable hermes is itself the failure being reported.
    return undefined;
  }
}

