/**
 * One JARVIS per machine (review r1, finding 2).
 *
 * Two HUDs are not merely redundant: each spawns `gateway run --replace`, so
 * they take turns evicting each other's engine — restart loops, a port that
 * keeps moving, and a renderer holding credentials for a process that just
 * died. The rule is enforced before `app.whenReady()`, which is the only point
 * early enough to guarantee the loser never starts an engine at all.
 *
 * The decision lives here, apart from the Electron wiring, so it can be tested
 * without an app lifecycle.
 */
export interface InstanceDecision {
  /** True when this process must exit instead of initialising. */
  quit: boolean;
  /** True when this process owns the engine and the window. */
  primary: boolean;
  reason: "primary" | "secondary" | "dry-run";
}

export function decideInstanceRole(options: {
  hasLock: boolean;
  dryRun: boolean;
}): InstanceDecision {
  // A dry run touches nothing and starts no engine, so it must never be blocked
  // by (or steal) a real instance's lock.
  if (options.dryRun) return { quit: false, primary: true, reason: "dry-run" };
  if (options.hasLock) return { quit: false, primary: true, reason: "primary" };
  return { quit: true, primary: false, reason: "secondary" };
}
