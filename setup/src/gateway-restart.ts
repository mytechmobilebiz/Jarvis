import { type GatewayControl, type WizardContext } from "./context.js";
import { commandExists, hermesInvocation, runCommand, type CommandResult } from "./system.js";

/** Accepts both runCommand and the narrower per-step runner overrides. */
type GatewayRunner = (
  ctx: WizardContext,
  command: string,
  args: string[],
  options: { stream: boolean },
) => Promise<CommandResult>;

export interface GatewayRestartOverrides {
  platform?: NodeJS.Platform;
  powershell?: string;
}

/**
 * "Make the running engine pick up the credential we just wrote."
 *
 * v1.4.0 has TWO ways to be running, and this module is the one place that
 * knows both:
 *
 * 1. **Supervised** (`ctx.gateway` present — the HUD hosts this run, whether
 *    that is GUI setup or an app launch). The HUD owns the process and passes
 *    the key and port in the spawn environment, so "pick up the new
 *    credentials" is literally "restart the child", and the supervisor's own
 *    verified round-trip is what settles it.
 * 2. **Legacy / terminal** (no supervisor). Either a 1.3.x machine whose
 *    LOGON-started gateway is still alive — the case provider safety hits
 *    BEFORE the app ever supervises anything — or `pnpm wizard` in a source
 *    checkout. There is no process to ask, so the old status/process-scan
 *    forensics below stay: a gateway that started earlier read the profile
 *    `.env` once and is still serving what it read.
 *
 * Restarting the legacy way is NOT safe unconditionally: on POSIX with no
 * service installed, `hermes gateway restart` falls back to running the
 * gateway in the FOREGROUND of the CLI process, which would hang the wizard
 * forever. So that path probes first and only restarts a gateway it can
 * confirm is running under service management (any status="running" that is
 * not flagged as manual), or on Windows — whose restart backend is always
 * detached — one found by the process scan when status is unparseable.
 *
 * Neither path may silently no-op: every caller gets a real outcome, and
 * "could not restart" is reported as unsettled rather than swallowed.
 */

/**
 * What actually happened to the running gateway. Callers that are removing an
 * UNSAFE route need this rather than a sentence: a route cleared on disk is
 * still being served by a gateway that loaded it at startup, so "could not
 * restart" is a blocker to carry forward, not a note to print.
 */
export type GatewayRestartOutcome =
  | "restarted"
  | "not-running"
  | "manual-needs-reboot"
  | "unknown-needs-reboot"
  | "restart-failed"
  | "dry-run";

export interface GatewayRestartReport {
  outcome: GatewayRestartOutcome;
  note: string;
}

/** True when no process can still be serving the old configuration. */
export function gatewayIsSettled(outcome: GatewayRestartOutcome): boolean {
  return outcome === "restarted" || outcome === "not-running" || outcome === "dry-run";
}

export async function restartGatewayForCredentialChange(
  ctx: WizardContext,
  run: GatewayRunner = runCommand,
  overrides: GatewayRestartOverrides = {},
): Promise<string> {
  return (await restartGatewayForCredentialChangeDetailed(ctx, run, overrides)).note;
}

export async function restartGatewayForCredentialChangeDetailed(
  ctx: WizardContext,
  run: GatewayRunner = runCommand,
  overrides: GatewayRestartOverrides = {},
): Promise<GatewayRestartReport> {
  if (ctx.dryRun) {
    return { outcome: "dry-run", note: "[dry-run] would restart any running gateway to load the new credentials." };
  }
  // A supervisor object exists from the moment the HUD hosts the run, but it
  // only SPEAKS FOR a gateway once it has actually spawned one. Before that —
  // GUI setup on a 1.3.x machine, where the LOGON-started gateway is still
  // serving the route provider safety just removed — the live process is one
  // the supervisor has never owned, and asking it would answer a different
  // question ("is my child up?") than the one that matters. So the legacy
  // forensics below answer it, exactly as they did in 1.3.x.
  if (ctx.gateway !== undefined && supervisorOwnsEngine(ctx.gateway)) {
    return restartSupervisedGateway(ctx.gateway);
  }
  return restartLegacyGateway(ctx, run, overrides);
}

/**
 * Ownership, asked directly where the control surface can answer it.
 *
 * `connection() !== undefined` is the same answer today, but only because a
 * supervisor publishes a connection the moment it spawns. Keeping the fallback
 * means an older control surface still routes correctly; asking `owns()` first
 * means the two concepts can diverge later without silently sending a restart
 * down the legacy path (integration review r1, finding 6).
 */
function supervisorOwnsEngine(gateway: GatewayControl): boolean {
  return gateway.owns?.() ?? (gateway.connection() !== undefined);
}

/**
 * Provider safety's settle: STOP, never restart.
 *
 * The question safety asks is "can anything still be serving the route I just
 * removed?", and a confirmed stop answers it completely. Restarting would ask
 * the supervisor to SPAWN — which its own gate refuses while the debt safety
 * has just written still stands, leaving the old engine running and recording
 * a failure that made every later restart refuse too (review r3, P1: the gate
 * blocked its own remediation). Stopping is never gated, so this always works,
 * and the engine comes back on the next authorized start once the pass has
 * cleared the debt.
 */
export async function settleGatewayForSafety(
  ctx: WizardContext,
  run: GatewayRunner = runCommand,
  overrides: GatewayRestartOverrides = {},
): Promise<GatewayRestartReport> {
  if (ctx.dryRun) {
    return { outcome: "dry-run", note: "[dry-run] would stop any running engine before changing the provider." };
  }
  const gateway = ctx.gateway;
  if (gateway !== undefined && supervisorOwnsEngine(gateway) && gateway.stop !== undefined) {
    const stopped = await gateway.stop().catch(() => false);
    return stopped
      ? {
        outcome: "not-running",
        note: "Stopped the JARVIS engine so it can no longer use the setting I removed; "
          + "it starts again — with a provider you choose — when setup finishes.",
      }
      : {
        outcome: "restart-failed",
        note: "The JARVIS engine did not confirm that it stopped, so it may still be using the setting I removed. "
          + "Restart this computer, then open JARVIS again.",
      };
  }
  return restartLegacyGateway(ctx, run, overrides);
}

/**
 * The supervised case. The supervisor restarts its child with a fresh key and
 * verifies it through the port, so its answer IS the settled/not-settled
 * question — no status text to parse, no reboot advice to give.
 */
async function restartSupervisedGateway(gateway: GatewayControl): Promise<GatewayRestartReport> {
  const result = await gateway.restart();
  if (result.state === "ok") {
    return { outcome: "restarted", note: "Restarted the JARVIS engine so it picks up the new credentials." };
  }
  if (result.state === "blocked") {
    // The supervisor refused to SPAWN because lifecycle debt stands. Whether
    // that settles anything depends on what happened to the old process: a
    // restart stops before it asks, so `stopped` means nothing is serving the
    // old configuration; a plain refusal means the old engine is still up.
    return result.stopped === true
      ? {
        outcome: "not-running",
        note: result.detail
          ?? "JARVIS stopped its engine and will start it again once setup is finished.",
      }
      : {
        outcome: "restart-failed",
        note: result.detail ?? "JARVIS did not start its engine, so these credentials are not in use yet.",
      };
  }
  if (result.state === "unavailable") {
    // No engine installed yet: nothing is running, so nothing can still be
    // serving the configuration we just changed.
    return {
      outcome: "not-running",
      note: "The engine is not installed yet; it will start with these credentials once it is.",
    };
  }
  // crashed / unreachable / auth-rejected: the supervisor tried to stop the old
  // child but could not prove a healthy new one. Treating that as settled is
  // exactly the failure mode provider safety exists to catch, so it is not.
  return {
    outcome: "restart-failed",
    note: result.detail
      ?? "The JARVIS engine did not come back after the restart; the Start Engine step will report the details.",
  };
}

/** The 1.3.x-shaped world: a gateway nobody owns, identified from the outside. */
async function restartLegacyGateway(
  ctx: WizardContext,
  run: GatewayRunner,
  overrides: GatewayRestartOverrides,
): Promise<GatewayRestartReport> {
  const platform = overrides.platform ?? process.platform;
  // Through the single spawn authority: the site payload's engine is an
  // interpreter plus entry-point args, and the bare executable cannot run
  // hermes arguments (hermes-spawn-guard.test.ts).
  const statusInvocation = hermesInvocation(ctx, ["-p", "jarvis", "gateway", "status"], platform);
  const status = await run(ctx, statusInvocation.command, statusInvocation.args, { stream: false });
  const statusText = `${status.stdout}\n${status.stderr}`;
  const health = status.status === 0 ? parseGatewayStatus(statusText) : "unavailable";

  // Hermes prints "(Running manually, not as a system service)" ONLY from
  // the no-service status branch — it is the one marker that proves restart
  // would take the POSIX foreground fallback and hang the wizard. Windows
  // may still restart such a gateway: its backend is always detached.
  const manualNoService = /running manually, not as a system service/i.test(statusText);
  if (manualNoService && platform !== "win32") {
    return {
      outcome: "manual-needs-reboot",
      note: "The gateway is running outside service management; restart your computer (or sign out and back in) so it loads the new credentials.",
    };
  }

  // These shapes print only from the service-installed status branches (a
  // unit/plist/task exists), so `gateway restart` takes the service path on
  // every platform — it exits nonzero on failure, never the foreground
  // fallback. They also accompany a "service is stopped" line ("Gateway
  // process is running for this profile, but the service is not active"),
  // where the service line alone would falsely read as no gateway at all.
  const serviceBackedLiveProcess =
    /gateway process is running for this profile, but the service is not active/i.test(statusText)
    || /detached (?:fallback|gateway) process is running/i.test(statusText)
    || /gateway is running as a detached fallback process/i.test(statusText);

  const restartable = health === "running"
    || serviceBackedLiveProcess
    || (manualNoService && platform === "win32");

  if (!restartable) {
    if (health === "stopped") {
      return { outcome: "not-running", note: "No gateway is running yet; it will load these credentials when it starts." };
    }
    // An unparseable status does not mean no gateway — a stale one can serve
    // while status fails. The scan is read-only and safe everywhere, so use
    // it to report the truth; but only Windows may act on it with a restart,
    // while on POSIX a scan hit could be a manual no-service gateway whose
    // restart would hang the wizard.
    const found = await processScanFallback(ctx, platform, run, overrides.powershell);
    if (!found) {
      return { outcome: "not-running", note: "No running gateway was found; it will load these credentials when it starts." };
    }
    if (platform !== "win32") {
      return {
        outcome: "unknown-needs-reboot",
        note: "A gateway appears to be running but cannot be safely restarted from setup; restart your computer (or sign out and back in) so it loads the new credentials.",
      };
    }
  }

  const restartInvocation = hermesInvocation(ctx, ["-p", "jarvis", "gateway", "restart"], platform);
  const restart = await run(ctx, restartInvocation.command, restartInvocation.args, { stream: false });
  return restart.status === 0
    ? { outcome: "restarted", note: "Restarted the gateway so it picks up the new credentials." }
    : {
      outcome: "restart-failed",
      note: "The gateway could not be restarted; restart your computer (or sign out and back in) so it loads the new credentials.",
    };
}

export function parseGatewayStatus(output: string): "running" | "stopped" | "unavailable" {
  const plain = output.replace(/\u001b\[[0-9;]*m/g, "");
  if (/\bgateway process running\b/i.test(plain)) return "running";
  // macOS healthy service case: "✓ Gateway is supervised by launchd (PID n)".
  if (/\bgateway is supervised by launchd\b/i.test(plain)) return "running";
  if (/\bno gateway process detected\b/i.test(plain)) return "stopped";
  if (/\bgateway is running\b/i.test(plain)) return "running";
  if (/\bno gateway running\b/i.test(plain)) return "stopped";
  if (/\bgateway service is running\b/i.test(plain)) return "running";
  if (/\bgateway (?:service )?is (?:not running|stopped)\b/i.test(plain)) return "stopped";
  return "unavailable";
}

export async function processScanFallback(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: GatewayRunner,
  powershellOverride?: string,
): Promise<boolean> {
  // Our OWN engine and its watchdog carry exactly the command line this scan
  // matches on. They are excluded by PID and by ancestry, never by pattern: a
  // text rule that tried to tell them apart would be one refactor away from
  // either missing a real 1.3.x gateway or "finding" the child we started
  // ourselves (integration review r1, finding 6).
  const ours = new Set<number>([
    process.pid,
    ...(ctx.gateway?.ownedPids?.() ?? []).filter((pid) => Number.isInteger(pid) && pid > 0),
  ]);

  // Profile-scoped on purpose: the wizard's gateway always carries
  // "--profile jarvis" (or "-p jarvis" when hand-launched) on its command
  // line, and an unrelated Hermes profile's gateway must not authorize a
  // jarvis restart — on Windows that would START a jarvis gateway that was
  // never running.
  if (platform === "win32") {
    const powershell = powershellOverride
      ?? ["powershell.exe", "pwsh"].find((command) => commandExists(ctx, command, { platform }))
      ?? "powershell.exe";
    // Emits "<pid> <parentPid>" per candidate so ancestry can be walked here.
    const script = [
      "Get-CimInstance Win32_Process |",
      "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'hermes.*(--profile|-p) jarvis .*\\bgateway\\b' } |",
      "ForEach-Object { Write-Output ('{0} {1}' -f $_.ProcessId, $_.ParentProcessId) };",
    ].join(" ");
    const result = await run(ctx, powershell, ["-NoProfile", "-Command", script], { stream: false });
    const candidates = parsePidPairs(result.stdout);
    return candidates.some(([pid, parent]) => !ours.has(pid) && !ours.has(parent));
  }

  // The space after "jarvis" is the profile-token boundary: without it,
  // profiles like "jarvis-dev" or "jarvis2" would authorize a restart of a
  // jarvis gateway that was never running. The profile flag immediately
  // precedes "gateway" on every launch path (service cmd and hand-run CLI).
  const result = await run(ctx, "pgrep", ["-f", "hermes.*(--profile|-p) jarvis .*gateway"], { stream: false });
  if (result.status !== 0) return false;
  const found = result.stdout
    .split(/\r?\n/)
    .map((line) => Number.parseInt(line.trim(), 10))
    .filter((pid) => Number.isInteger(pid) && pid > 0);
  if (found.length === 0) return false;
  if (ours.size === 1) return found.some((pid) => !ours.has(pid));

  // One process table, not one call per candidate: an engine we launched is a
  // GRANDCHILD (watchdog in between), so identity alone is not enough.
  const table = await run(ctx, "ps", ["-o", "pid=,ppid=", "-ax"], { stream: false });
  const parents = new Map(parsePidPairs(table.stdout));
  return found.some((pid) => !ours.has(pid) && !hasOwnedAncestor(pid, parents, ours));
}

function parsePidPairs(output: string): [number, number][] {
  const pairs: [number, number][] = [];
  for (const line of output.split(/\r?\n/)) {
    const [rawPid, rawParent] = line.trim().split(/\s+/);
    const pid = Number.parseInt(rawPid ?? "", 10);
    const parent = Number.parseInt(rawParent ?? "", 10);
    if (Number.isInteger(pid) && pid > 0) pairs.push([pid, Number.isInteger(parent) ? parent : 0]);
  }
  return pairs;
}

/** Bounded walk: a cycle in a process table must not hang a credential write. */
function hasOwnedAncestor(pid: number, parents: Map<number, number>, ours: Set<number>): boolean {
  let current = parents.get(pid);
  for (let hop = 0; hop < 8 && current !== undefined && current > 1; hop += 1) {
    if (ours.has(current)) return true;
    current = parents.get(current);
  }
  return false;
}
