import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { readEnvFile, removeEnv } from "../env-file.js";
import { migrateProfileTerminalCwd } from "../profile-migrate.js";
import { settleGatewayCleanupDebt } from "./hermes.js";
import { confirm } from "../ui.js";

/**
 * Start & verify — everything the old API Server (step 7) and Service (step 9)
 * steps did, minus the two things that generated support load: writing a
 * credential into a file a long-lived process had already read, and installing
 * a login task that resurrected a gateway nobody owned.
 *
 * The HUD supervises the gateway as its child and passes the key and port at
 * spawn, so this step's whole job is: clear the stale file-based credentials
 * that would OVERRIDE the spawn environment, then prove the engine answers.
 */
export const SUPERSEDED_ENV_KEYS = [
  "API_SERVER_KEY",
  "API_SERVER_PORT",
  "API_SERVER_HOST",
  "API_SERVER_ENABLED",
];

export async function gateway(ctx: WizardContext): Promise<StepOutcome> {
  const notes: string[] = [];
  const envPath = path.join(ctx.profileDir, ".env");

  // F4, last call before the engine spawns (review round 2, finding 2): the
  // run-start migration can be too early — on a machine mid-repair the engine
  // interpreter appears only during THIS run's Hermes step, and a retained
  // Profile step never re-runs. Idempotent and best-effort: a no-op when the
  // pin is already right, a note (never a failure) when it still cannot run.
  if (!ctx.dryRun) {
    try {
      const pinned = await migrateProfileTerminalCwd(ctx);
      if (pinned !== null) notes.push("Pinned the assistant's working folder to the profile before starting the engine.");
    } catch {
      notes.push("The working-folder pin could not be written; the engine starts with its previous working folder.");
    }

    // Cancellation during the pin (runCommand resolves 130 on abort, which
    // the migration surfaces as its ordinary error) must not fall through
    // into a gateway spawn the customer no longer wants (review round 17).
    if (ctx.signal?.aborted === true) {
      return { status: "failed", notes: [...notes, "Setup was cancelled before the engine started."] };
    }

    // Recorded installer-gateway cleanup debt BLOCKS the spawn (review round
    // 16): starting the supervised gateway next to a competing one would
    // have both consuming the same messaging credentials. The settle probe
    // retries removal of installer-created units and verifies the recorded
    // pids are gone; only proof clears the record.
    // The in-run carrier blocks even when the durable record could not be
    // written (review round 18: read-only profile, full disk).
    if (ctx.state.gatewayCleanupDebt === true) {
      return {
        status: "failed",
        notes: [
          ...notes,
          "A background Hermes gateway from the install attempt may still be running, so JARVIS did not start its own engine next to it.",
        ],
        nextActions: [
          "Restart this computer (or stop any running Hermes gateway), then run setup again.",
          "If it keeps failing, email jarvis.agent.tech@pm.me and a real person will help.",
        ],
      };
    }
    const debt = await settleGatewayCleanupDebt(ctx);
    if (debt === "cleared") {
      notes.push("Confirmed the background gateway from the earlier install attempt is gone.");
    } else if (debt === "blocked") {
      return {
        status: "failed",
        notes: [
          ...notes,
          "A background Hermes gateway from the install attempt may still be running, so JARVIS did not start its own engine next to it.",
        ],
        nextActions: [
          "Restart this computer (or stop any running Hermes gateway), then run setup again.",
          "If it keeps failing, email jarvis.agent.tech@pm.me and a real person will help.",
        ],
      };
    }
  }

  // Not hygiene — correctness. The engine loads the profile .env with
  // override=True (hermes_cli/env_loader.py:146-148), so a leftover
  // API_SERVER_KEY from a 1.3.x install BEATS the key the HUD passes at spawn
  // and the HUD gets a 401 against its own child.
  //
  // But this step does NOT get to rewrite a customer's profile behind their
  // back (review r1, finding 11). The migration step owns that change under its
  // own consent; if it was declined — or a stale line appeared some other way —
  // we ask here, and a "no" is respected: nothing is written, and the step says
  // honestly why the engine cannot start.
  const existing = await readEnvFile(envPath);
  const stale = SUPERSEDED_ENV_KEYS.filter((key) => existing[key] !== undefined);
  if (stale.length > 0 && !ctx.dryRun) {
    const allowed = await confirm(
      ctx,
      `This profile still has ${stale.length} saved gateway setting${stale.length === 1 ? "" : "s"} from an earlier JARVIS. They override the ones this app supplies, so chat will not work until they are cleared. Clear them now? (Your provider keys and everything else stay.)`,
      true,
    );
    if (!allowed) {
      return {
        status: "failed",
        notes: [
          ...notes,
          "The earlier install's saved gateway settings were left in place, so the JARVIS engine was not started — it would reject this app's credentials.",
        ],
        props: { gateway_supervised: false, gateway_state: "blocked", stale_env_keys_removed: 0 },
        nextActions: [
          `Run this step again and allow the change, or delete the ${SUPERSEDED_ENV_KEYS.join(" / ")} lines from the profile .env yourself.`,
        ],
      };
    }
    await removeEnv(ctx, envPath, SUPERSEDED_ENV_KEYS);
    notes.push(
      `Cleared ${stale.length} superseded gateway setting${stale.length === 1 ? "" : "s"} from the profile so JARVIS supplies them directly.`,
    );
  }

  if (ctx.dryRun) {
    return {
      status: "done",
      notes: [...notes, "[dry-run] would start the JARVIS engine and verify it through its port."],
    };
  }

  if (ctx.gateway === undefined) {
    // The terminal wizard has no supervisor. Say exactly that instead of
    // installing a service behind the user's back or claiming a green check.
    return {
      status: "skipped",
      notes: [
        ...notes,
        "The JARVIS app starts and supervises the engine itself; run the app to start it. This terminal run did not start an engine.",
        "For a terminal-only session, run: hermes -p jarvis gateway run",
      ],
      props: { gateway_supervised: false, gateway_state: "unavailable" },
    };
  }

  const result = await ctx.gateway.ensureRunning();
  const props = {
    gateway_supervised: true,
    gateway_state: result.state,
    stale_env_keys_removed: stale.length,
  };

  if (result.state === "ok") {
    ctx.state.gatewayIntegrityVerified = true;
    notes.push(
      `The JARVIS engine is running and answered on port ${result.connection?.port ?? 0} with this session's credentials.`,
    );
    return { status: "done", notes, props };
  }

  notes.push(result.detail ?? "The JARVIS engine did not start.");
  return {
    status: "failed",
    notes,
    props,
    nextActions: nextActionsFor(result.state),
  };
}

function nextActionsFor(state: string): string[] {
  if (state === "blocked") {
    // The supervisor refused: provider-safety or migration debt stands. The
    // detail already says which, so these are the two ways out.
    return [
      "Run the steps above that did not finish — the Earlier Install and Model steps are the two that clear this.",
      "If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    ];
  }
  if (state === "unavailable") {
    return [
      "Run the Hermes step again so the engine is installed, then run this step.",
      "If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    ];
  }
  if (state === "auth-rejected") {
    return [
      `Open the profile .env and delete any ${SUPERSEDED_ENV_KEYS.join(" / ")} lines, then run this step again.`,
      "If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    ];
  }
  return [
    "Run this step again — the engine can need a moment on a slow computer.",
    "If it keeps failing, restart this computer and run setup once more.",
    "Still failing? Use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
  ];
}
