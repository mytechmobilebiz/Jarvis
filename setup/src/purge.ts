import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";
import type { WizardContext } from "./context.js";
import {
  cleanupLegacyInstall,
  detectLegacyInstall,
  isSameOrInside,
  type LegacyArtifacts,
  type LegacyDeps,
  type LegacyRegistration,
} from "./legacy-cleanup.js";
import { SETUP_STATE_VERSION, setupStateFilePath } from "./setup-state.js";
import { SUPERSEDED_ENV_KEYS } from "./steps/gateway.js";
import { checkProvenance } from "./engine-pin.js";
import { readVendorManifest, readVendorStamp } from "./vendor.js";

/**
 * Make this computer ready, instead of certifying that it already is.
 *
 * The migration this replaces asked "can I PROVE this machine is clean?" and
 * refused when it could not — and refusing meant the engine never started. A
 * customer whose Windows task table could not be queried, or who had one
 * Startup `.vbs` we could not attribute, ended up with a JARVIS that would not
 * run, on a machine where nothing of ours had even been found. That is
 * backwards: a broken earlier install is the exact situation the new version
 * exists to fix, so leftovers must be REMOVED, not treated as an obstacle.
 *
 * Three classes, three rules, and the class decides everything:
 *
 *  A. OUR MACHINERY — the old engine tree, its venv, its auto-start entries,
 *     its running gateways, our own state files. Deleted unconditionally, and
 *     ambiguity resolves to DELETE. Nothing here is worth keeping and nothing
 *     here belongs to anyone else.
 *  B. REGENERABLE SECRETS — the gateway key and port the old version pinned in
 *     the profile `.env`. Deleted unconditionally: v1.4.0 mints both at every
 *     spawn, so a stale pair is pure risk and zero value.
 *  C. CUSTOMER-AUTHORED DATA — provider API keys, Slack tokens, history,
 *     settings. KEPT. If a file in this class is unreadable and would block
 *     setup, it is moved aside with a timestamp and setup continues from zero.
 *     Deleting it would turn a repair into a re-onboarding, and that is a worse
 *     outcome than any leftover.
 *
 * OUTSIDE OUR DIRECTORIES NOTHING IS EVER DELETED. A scheduled task or a
 * Startup script that names some other Hermes root stays exactly where it is —
 * we cannot prove it is ours, and destroying a second install is a far worse
 * failure than leaving it. What changed is the CONSEQUENCE: it is now a note in
 * the summary, never a reason to stop. It cannot hurt us either, and the reason
 * is structural — the supervised gateway binds a free port and mints its own
 * key, so a foreign gateway has nothing of ours to squat.
 *
 * This function does not throw and has no failure outcome. Whatever it could
 * not do, it reports; setup proceeds regardless.
 */

export interface PurgeReport {
  /** Class A/B things we removed, in customer-facing words. */
  removed: string[];
  /** Ours, but it resisted removal (a lock, a permission). Setup continues. */
  stubborn: string[];
  /** Not ours: named, untouched, and harmless. */
  foreign: string[];
  /** Class C files moved aside rather than deleted. */
  quarantined: string[];
  /** True when a stubborn item looked like it needed administrator rights. */
  elevationLikely: boolean;
}

export interface PurgeDeps extends LegacyDeps {
  detect?: typeof detectLegacyInstall;
  cleanup?: typeof cleanupLegacyInstall;
  removeDir?: (target: string) => Promise<void>;
  /** Distinguishes a symlink from what it points at; see containment below. */
  lstat?: (target: string) => Promise<{ isSymbolicLink: boolean; isDirectory: boolean; isFile: boolean }>;
  realpath?: (target: string) => Promise<string>;
  rename?: (from: string, to: string) => Promise<void>;
  listDirEntries?: (dir: string) => Promise<string[]>;
  readTextFile?: (file: string) => Promise<string | undefined>;
  writeTextFile?: (file: string, contents: string) => Promise<void>;
  exists?: (target: string) => Promise<boolean>;
  now?: () => Date;
  /** Injected in tests; production reads the manifest from ctx.vendorRoot. */
  readManifest?: typeof readVendorManifest;
  /** Injected in tests; production reads the stamp from the engine dir. */
  readStamp?: typeof readVendorStamp;
}

/**
 * Which parts of the engine tree belong to the CURRENT install — and are
 * therefore never "an earlier install", whatever presence alone suggests.
 *
 * The v1.4.0 classification keyed on presence: `venv` and `.git` under
 * hermes-agent read as 1.3.x leftovers, `.jarvis-engine.json` as an old stamp.
 * On the SOURCE path a healthy install IS a git checkout with a venv —
 * indistinguishable by those keys — so every `bash start.sh` rerun destroyed
 * the working engine it had just built (Kevin Dunn, ZIP-TRIAGE-260814, F2).
 * On desktop the same misclassification deleted the CURRENT vendored install's
 * stamp (and, on Windows, its wheelhouse venv) on every rerun, forcing a
 * pointless full engine reinstall and keeping the Earlier Install step alive
 * forever.
 *
 * The rule, decided by install shape — ONE decision shared by the purge and
 * the probe, so they cannot disagree:
 *
 *  - No vendor payload (source/dev path): the engine tree's venv and .git are
 *    current-by-definition. 1.3.x and 1.4.0+ source layouts are identical, so
 *    there is nothing "earlier" there that reinstalling would not need again.
 *    A stamp is still deletable — source installs never write one, so one
 *    lying around came from an earlier desktop install.
 *  - Vendor payload present (desktop): the stamp — and the venv it may govern
 *    (the Windows wheelhouse build) — is current exactly when its engineTag
 *    matches the shipped payload's. A `.git` under a vendored tree is never
 *    current: the vendored installer copies a snapshot, not a checkout.
 *  - PACKAGED app whose manifest is missing or fails the trust checks
 *    (review round 4, finding 1): NO exemptions. A packaged install may only
 *    keep an engine its own trusted payload vouches for — preserving the
 *    tree here would let a later step run an old, unverified engine while
 *    the Hermes step is busy refusing the broken payload. Deleting is safe
 *    on desktop by construction: an intact payload restores everything, and
 *    a broken payload must leave the machine engine-less, not engine-stale.
 *    Only the UNPACKAGED path (source/dev, where no manifest is expected)
 *    treats a missing manifest as "the checkout is the engine".
 */
interface EngineExemptions {
  venv: boolean;
  git: boolean;
  stamp: boolean;
}

async function currentEngineExemptions(
  ctx: WizardContext,
  deps: PurgeDeps,
): Promise<EngineExemptions> {
  const readManifest = deps.readManifest ?? readVendorManifest;
  const readStamp = deps.readStamp ?? readVendorStamp;
  const packaged = ctx.packaged === true;
  let manifest: Awaited<ReturnType<typeof readVendorManifest>>;
  try {
    // The SAME trust bar the Hermes step applies: a packaged app never
    // believes an untrusted manifest, here or there.
    manifest = await readManifest(ctx.vendorRoot, { requireTrust: packaged });
  } catch {
    return packaged
      ? { venv: false, git: false, stamp: false }
      : { venv: true, git: true, stamp: true };
  }
  if (manifest === undefined) {
    return packaged
      ? { venv: false, git: false, stamp: false }
      : { venv: true, git: true, stamp: false };
  }
  // The SAME bar the vendored installer applies before reusing a tree
  // (review round 9): provenance against the build's engine pin on the
  // packaged path, then tag AND payload digest — same tag is not same build.
  if (packaged && checkProvenance(manifest.source) !== undefined) {
    return { venv: false, git: false, stamp: false };
  }
  const stamp = await guard(() => readStamp(ctx.hermesAgentDir), undefined);
  const current = stamp !== undefined
    && stamp.engineTag === manifest.engineTag
    && (manifest.digests === undefined || stamp.payloadDigest === manifest.digests.sha256);
  // A venv is only CURRENT machinery on payloads that BUILD one (the Windows
  // wheelhouse). A site payload (macOS) installs its libraries into the
  // bundled interpreter's own site-packages and never creates
  // hermes-agent/venv — so on a site-payload machine, any venv found there is
  // an earlier network install's leftover and must stay purgeable (review
  // round 19): exempting it would leave 1.3.x's python environment resident
  // forever on every upgraded mac.
  const venvIsCurrentMachinery = current && manifest.site === undefined;
  return { venv: venvIsCurrentMachinery, git: false, stamp: current };
}

/**
 * Class B, and deliberately the SAME list the gateway step uses — one
 * definition, so a key can never be superseded in one place and preserved in
 * the other.
 */
const REGENERABLE_ENV_KEYS = SUPERSEDED_ENV_KEYS;

export async function purgeLegacyFootprint(
  ctx: WizardContext,
  deps: PurgeDeps = {},
): Promise<PurgeReport> {
  const platform = deps.platform ?? process.platform;
  const detect = deps.detect ?? detectLegacyInstall;
  const cleanup = deps.cleanup ?? cleanupLegacyInstall;
  const removeDir = deps.removeDir ?? ((target: string) => fs.rm(target, { recursive: true, force: true }));
  const rename = deps.rename ?? ((from: string, to: string) => fs.rename(from, to));
  const listDirEntries = deps.listDirEntries ?? (async (dir: string) => {
    try {
      return await fs.readdir(dir);
    } catch {
      return [];
    }
  });
  const readTextFile = deps.readTextFile ?? (async (file: string) => {
    try {
      return await fs.readFile(file, "utf8");
    } catch {
      return undefined;
    }
  });
  const writeTextFile = deps.writeTextFile ?? ((file: string, contents: string) => fs.writeFile(file, contents, "utf8"));
  const existsRaw = deps.exists ?? (async (target: string) => {
    try {
      await fs.stat(target);
      return true;
    } catch {
      return false;
    }
  });
  // guard() wraps the CALL, so a seam that throws before returning a promise
  // is caught too — `existsRaw(target).catch(...)` never saw those.
  const exists = (target: string): Promise<boolean> => guard(() => existsRaw(target), false);
  const lstat = deps.lstat ?? (async (target: string) => {
    const info = await fs.lstat(target);
    return { isSymbolicLink: info.isSymbolicLink(), isDirectory: info.isDirectory(), isFile: info.isFile() };
  });
  const realpath = deps.realpath ?? ((target: string) => fs.realpath(target));
  const nowRaw = deps.now ?? (() => new Date());
  // Even the clock is a seam, and a seam that throws must not take the purge
  // with it — the timestamp only names a quarantine file.
  const now = (): Date => {
    try {
      const value: unknown = nowRaw();
      // A seam is allowed to be wrong in any way at all. If it hands back a
      // promise (or anything that is not a usable Date), take the epoch and
      // swallow the rejection rather than letting it surface as an unhandled
      // one — the timestamp only names a quarantine file.
      if (typeof (value as { then?: unknown })?.then === "function") {
        void (value as Promise<unknown>).catch(() => {});
        return new Date(0);
      }
      const date = value as Date;
      return date instanceof Date && !Number.isNaN(date.getTime()) ? date : new Date(0);
    } catch {
      return new Date(0);
    }
  };
  // Filesystem work uses the HOST path module, because these are real paths on
  // the machine we are running on. `platform` decides how REGISTRATIONS are
  // read (a Windows Startup folder, a launchd plist), not how a directory on
  // this disk is spelled — and mixing the two silently pointed every file
  // operation at a path that could not exist.
  const pathApi = path;

  const report: PurgeReport = {
    removed: [],
    stubborn: [],
    foreign: [],
    quarantined: [],
    elevationLikely: false,
  };

  // CONTAINMENT, before anything is deleted.
  //
  // Every path below is built from ctx by string joining, and a string prefix
  // is not a containment proof: if `<root>/hermes-agent` is a symlink to a
  // customer's own checkout, "delete <hermesAgentDir>/venv" deletes THEIR venv.
  // Resolving the real paths first, and refusing when a base is a link, is what
  // makes "we only delete inside our own footprint" true rather than intended.
  const realRoot = await guard(() => realpath(ctx.hermesRoot), undefined);
  for (const [base, label] of [
    [ctx.hermesAgentDir, "engine folder"],
    [ctx.profileDir, "profile folder"],
  ] as const) {
    const link = await guard(() => lstat(base), undefined);
    if (link?.isSymbolicLink === true) {
      report.stubborn.push(
        `the ${label} is a shortcut to somewhere else, so nothing inside it was touched`,
      );
      return report;
    }
  }

  // 1. Registrations and running gateways. Detection is unchanged and still
  //    proves ownership — but a scan that could not finish no longer stops
  //    anything: it means we remove what we DID find and say what we missed.
  const found = await guard(() => detect(ctx, deps), undefined);
  if (found === undefined) {
    report.stubborn.push("this computer could not be checked for an earlier JARVIS auto-start entry");
  }
  // A detector that answers with the wrong SHAPE is the same class of problem
  // as one that throws — `{}` would crash on `.registrations.filter`.
  const artifacts = normalizeArtifacts(found);

  if (artifacts !== undefined) {
    // Ambiguity inside OUR OWN directories resolves to delete: a Startup script
    // or launch agent that lives in, or points into, this install's hermes root
    // is ours by construction, whatever its text says.
    for (const entry of artifacts.registrations) {
      if (entry.ownership !== "unresolved") continue;
      if (await registrationIsProvablyOurs(ctx, entry, platform, realRoot, realpath)) {
        entry.ownership = "owned";
      }
    }

    const result = await guard(() => cleanup(ctx, artifacts, deps), undefined);
    if (result === undefined) {
      report.stubborn.push("the earlier install's auto-start entries could not be removed");
    } else {
      report.elevationLikely = result.elevationLikely;
      for (const entry of result.removed) report.removed.push(describeRegistration(entry));
      for (const entry of result.failed) report.stubborn.push(describeRegistration(entry));
      if (result.killedPids.length > 0) {
        report.removed.push(`stopped ${result.killedPids.length} old JARVIS engine process(es)`);
      }
      if (result.survivingPids.length > 0) {
        report.stubborn.push(`${result.survivingPids.length} old engine process(es) would not stop`);
      }
      if (result.skippedPids.length > 0) {
        // They stopped matching us between the scan and the kill — a recycled
        // PID belongs to somebody else's program now, so it is left alone AND
        // said out loud rather than vanishing from the report.
        report.foreign.push(
          `${result.skippedPids.length} process(es) stopped matching this install before they could be stopped, so they were left running`,
        );
      }
    }

    for (const entry of artifacts.registrations) {
      // "foreign" is proven to be someone else's; "unresolved" is the one we
      // could not read at all — and outside our own directories those get the
      // same treatment, because the alternative is deleting on a guess. Both
      // are reported; neither stops anything.
      if (entry.ownership === "foreign") {
        report.foreign.push(`${describeRegistration(entry)} — it belongs to another Hermes install, so it was left alone`);
      } else if (entry.ownership === "unresolved") {
        report.foreign.push(`${describeRegistration(entry)} — JARVIS could not confirm it belongs to this install, so it was left alone`);
      }
    }
    if (artifacts.unresolvedPids.length > 0) {
      report.foreign.push(
        `${artifacts.unresolvedPids.length} Hermes-looking process(es) could not be traced to this install, so they were left running`,
      );
    }
    if (!artifacts.scanOk) {
      report.foreign.push("some of this computer's auto-start entries could not be listed");
    }
  }

  // 2. Class A on disk: the old engine trees. `hermes-agent.replaced-*` are the
  //    installer's own leftovers and can be hundreds of MB each.
  const engineParent = pathApi.dirname(ctx.hermesAgentDir);
  const engineName = pathApi.basename(ctx.hermesAgentDir);
  // Every step from here on is wrapped: this function's contract is that it
  // cannot fail, and a listing that throws is exactly the kind of machine it
  // exists to rescue.
  const listed = await guard(() => listDirEntries(engineParent), undefined);
  if (listed === undefined) report.stubborn.push("the engine folder could not be listed");
  const siblings = Array.isArray(listed) ? listed.filter((entry) => typeof entry === "string") : [];
  for (const entry of siblings) {
    // The EXACT shape the installer writes (stampSuffix: an ISO timestamp with
    // `:` and `.` turned into `-`). A prefix match would have deleted a
    // customer's own `hermes-agent.replaced-my-work` and everything under it —
    // the whole point of class A is that it contains only things WE created.
    if (!BACKUP_DIRECTORY.test(entry) || !entry.startsWith(`${engineName}.replaced-`)) continue;
    const target = pathApi.join(engineParent, entry);
    try {
      await guard(() => removeDir(target), undefined);
      if (await exists(target)) throw new Error("still there");
      report.removed.push(`an earlier engine folder (${entry})`);
    } catch {
      report.stubborn.push(`an earlier engine folder could not be deleted (${entry})`);
    }
  }
  // The 1.3.x venv and its git checkout, if the engine folder still holds them
  // — and ONLY when they are not the CURRENT install's own machinery (F2,
  // ZIP-TRIAGE-260814: presence alone also matched a healthy source checkout
  // and the Windows wheelhouse venv; see currentEngineExemptions).
  const exempt = await guard(
    () => currentEngineExemptions(ctx, deps),
    { venv: true, git: true, stamp: true },
  );
  for (const [relative, label] of [
    ["venv", "the earlier engine's Python environment"],
    [".git", "the earlier engine's source checkout"],
  ] as const) {
    if (relative === "venv" && exempt.venv) continue;
    if (relative === ".git" && exempt.git) continue;
    const target = pathApi.join(ctx.hermesAgentDir, relative);
    if (!(await exists(target))) continue;
    try {
      await removeDir(target);
      report.removed.push(label);
    } catch {
      report.stubborn.push(`${label} could not be deleted`);
    }
  }

  // 3. Class A state files. Every one of these is rebuilt by the run that
  //    follows, so deleting them cannot lose anything a customer typed.
  for (const [target, label] of [
    // The HUD writes this (hud/src/local-api.ts); the wizard only has to know
    // that a stale one must not outlive the engine it described.
    [pathApi.join(ctx.profileDir, "local-api.json"), "the recorded local API address"],
    ...(exempt.stamp
      ? []
      : [[pathApi.join(ctx.hermesAgentDir, ".jarvis-engine.json"), "the earlier engine's install stamp"] as const]),
  ] as const) {
    if (!(await exists(target))) continue;
    try {
      await removeDir(target);
      report.removed.push(label);
    } catch {
      report.stubborn.push(`${label} could not be deleted`);
    }
  }

  // 4. Class B: credentials the old version pinned, which this one mints per
  //    spawn. Rewriting the file preserves every OTHER line — the customer's
  //    provider keys live here too, and they are class C.
  const envPath = pathApi.join(ctx.profileDir, ".env");
  await rewriteEnv(envPath, report, { readTextFile, writeTextFile, rename, lstat, removeDir });

  // 5. Class C, and the only place quarantine applies: setup state we cannot
  //    parse. Treating it as "start over" is what lets a broken machine finish
  //    setup at all; moving it aside instead of deleting it keeps the evidence
  //    for support without keeping the obstacle.
  const statePath = setupStateFilePath(ctx.profileDir);
  const stateText = await guard(() => readTextFile(statePath), undefined);
  if (stateText !== undefined && !isUsableSetupState(stateText)) {
    // Collision-proof, and NEVER a deletion. An earlier quarantine is evidence
    // too; overwriting it, or deleting the original when the rename fails,
    // would destroy the very thing this promises to keep.
    let aside = `${statePath}.quarantined-${stamp(now())}`;
    for (let attempt = 1; await guard(() => exists(aside), false); attempt += 1) {
      aside = `${statePath}.quarantined-${stamp(now())}-${attempt}`;
    }
    try {
      await Promise.resolve(rename(statePath, aside));
      report.quarantined.push(`unreadable setup state (kept at ${aside})`);
    } catch {
      report.stubborn.push("unreadable setup state could not be moved aside (it was left exactly as it is)");
    }
  }

  return report;
}

/** A detector's answer, made safe to use whatever it actually returned. */
function normalizeArtifacts(found: unknown): LegacyArtifacts | undefined {
  if (typeof found !== "object" || found === null) return undefined;
  const raw = found as Partial<LegacyArtifacts>;
  const registrations = Array.isArray(raw.registrations)
    ? raw.registrations.filter((entry): entry is LegacyRegistration =>
      typeof entry === "object" && entry !== null && typeof (entry as LegacyRegistration).id === "string")
    : [];
  return {
    scanOk: raw.scanOk !== false,
    scanFailures: Array.isArray(raw.scanFailures) ? raw.scanFailures : [],
    registrations,
    gatewayPids: Array.isArray(raw.gatewayPids) ? raw.gatewayPids : [],
    unresolvedPids: Array.isArray(raw.unresolvedPids) ? raw.unresolvedPids : [],
    gitCheckout: raw.gitCheckout === true,
    staleEnvKeys: Array.isArray(raw.staleEnvKeys) ? raw.staleEnvKeys : [],
  };
}

/**
 * Every injected seam goes through this.
 *
 * A dependency that throws SYNCHRONOUSLY escapes `promise.catch()`, because the
 * throw happens before a promise exists — which is how "this function cannot
 * fail" was still a claim rather than a fact. Wrapping the CALL, not the
 * result, closes that and the malformed-return case in one place.
 */
async function guard<T>(call: () => Promise<T> | T, fallback: T): Promise<T> {
  try {
    return await call();
  } catch {
    return fallback;
  }
}

/** Exactly what the installer names a backup: `<name>.replaced-<stampSuffix>`. */
const BACKUP_DIRECTORY = /\.replaced-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}$/;

/**
 * Rewrites the profile `.env` to drop class-B credentials, and does it the way
 * a file holding somebody's API keys deserves.
 *
 *  - LINE ENDINGS SURVIVE. Splitting on /\r?\n/ and joining with "\n" quietly
 *    rewrote every line of a Windows profile; each delimiter is now carried
 *    through with the line it followed.
 *  - THE WRITE IS ATOMIC. `writeFile` truncates first, so a crash or a full
 *    disk mid-write left a profile with its provider keys half-gone. A
 *    same-directory temporary file plus a rename means the customer either has
 *    the old file or the new one.
 *  - A SYMLINK IS NEVER FOLLOWED. `.env` pointing elsewhere would have made us
 *    rewrite a file outside the profile entirely.
 *  - The match is the parser's, not a guess: `KEY = value` counts, because
 *    readEnvFile trims before comparing, and leaving it would have kept a
 *    credential we told the customer we had cleared.
 */
async function rewriteEnv(
  envPath: string,
  report: PurgeReport,
  io: {
    readTextFile: (file: string) => Promise<string | undefined>;
    writeTextFile: (file: string, contents: string) => Promise<void>;
    rename: (from: string, to: string) => Promise<void>;
    lstat: (target: string) => Promise<{ isSymbolicLink: boolean; isDirectory: boolean; isFile: boolean }>;
    removeDir: (target: string) => Promise<void>;
  },
): Promise<void> {
  const info = await guard(() => io.lstat(envPath), undefined);
  if (info === undefined) return; // no .env at all: nothing to clear
  if (info.isSymbolicLink || !info.isFile) {
    report.stubborn.push("the profile's .env is not a regular file, so its saved gateway password was left alone");
    return;
  }
  const text = await guard(() => io.readTextFile(envPath), undefined);
  if (text === undefined) {
    report.stubborn.push("the profile's .env could not be read");
    return;
  }

  // Split into (content, delimiter) pairs so the file can be rebuilt exactly.
  const parts = text.match(/[^\r\n]*(?:\r\n|\r|\n|$)/g) ?? [];
  const kept: string[] = [];
  let dropped = 0;
  for (const part of parts) {
    const content = part.replace(/(?:\r\n|\r|\n)$/, "");
    if (content === "" && part === "") continue;
    const key = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/.exec(content)?.[1];
    if (key !== undefined && REGENERABLE_ENV_KEYS.includes(key)) {
      dropped += 1;
      continue;
    }
    kept.push(part);
  }
  if (dropped === 0) return;

  const temporary = `${envPath}.jarvis-tmp-${process.pid}`;
  try {
    await Promise.resolve(io.writeTextFile(temporary, kept.join("")));
    await Promise.resolve(io.rename(temporary, envPath));
    report.removed.push(`${dropped} saved gateway credential line${dropped === 1 ? "" : "s"}`);
  } catch {
    report.stubborn.push("the earlier gateway password could not be cleared");
    await guard(() => io.removeDir(temporary), undefined);
  }
}

/**
 * Is this setup state one the readers can actually USE?
 *
 * "Parses as JSON" was too weak: `{}` and `{"version":99}` parse perfectly and
 * are still rejected by the strict reader downstream, so they were left in
 * place to block a machine that this step is supposed to unblock. Mirror what
 * the reader demands instead.
 */
function isUsableSetupState(text: string): boolean {
  try {
    const parsed = JSON.parse(text) as { version?: unknown; steps?: unknown };
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return false;
    return parsed.version === SETUP_STATE_VERSION
      && typeof parsed.steps === "object"
      && parsed.steps !== null
      && !Array.isArray(parsed.steps);
  } catch {
    return false;
  }
}

/**
 * May we delete a registration we could NOT attribute by reading it?
 *
 * Only when its real path is inside our real hermes root — and the two words
 * doing the work are "real" and "our".
 *
 *  - REAL: both sides are resolved with realpath first. A lexical check says
 *    `<root>/link/foreign.vbs` is inside the root even when `link` points at
 *    someone else's directory, which would delete a stranger's autostart entry
 *    on the strength of a string.
 *  - OUR: HERMES_HOME can legitimately name a broad directory — a customer's
 *    home, in the worst case. An unreadable launch agent under
 *    `~/Library/LaunchAgents` is then "inside our root" and would be deleted.
 *    So the root must be a directory we plausibly own, never the home
 *    directory or its parent.
 *
 * Everything else stays unresolved: reported, untouched, and harmless.
 */
async function registrationIsProvablyOurs(
  ctx: WizardContext,
  entry: LegacyRegistration,
  platform: NodeJS.Platform,
  realRoot: string | undefined,
  realpath: (target: string) => Promise<string>,
): Promise<boolean> {
  if (entry.kind === "scheduled-task") return false; // a task name is not a path
  if (realRoot === undefined) return false;
  const home = os.homedir();
  if (isSameOrInside(realRoot, home, platform) || realRoot === path.dirname(home)) return false;
  const realEntry = await guard(() => realpath(entry.id), undefined);
  if (realEntry === undefined) return false;
  return isSameOrInside(realRoot, realEntry, platform);
}

function describeRegistration(entry: LegacyRegistration): string {
  if (entry.kind === "scheduled-task") return `the scheduled task ${entry.id}`;
  if (entry.kind === "launch-agent") return `the startup item ${entry.id}`;
  return `the startup script ${entry.id}`;
}

function isReadableJson(text: string): boolean {
  try {
    const parsed: unknown = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null;
  } catch {
    return false;
  }
}

function stamp(date: Date): string {
  return date.toISOString().replace(/[:.]/g, "-").replace(/Z$/, "");
}

/** What a read-only look at the machine found — decides whether the Earlier Install step exists at all. */
export interface LegacyFootprintProbe {
  /** True when the purge would actually remove something (class A or B). */
  actionable: boolean;
  /**
   * False when any part of the machine could not be inspected. An
   * uninspectable machine is the one most likely to need cleaning, so the
   * caller SHOWS the step on inconclusive — the purge itself cannot fail, so
   * showing it costs seconds while hiding it could strand leftovers (Peter's
   * conditional-step ruling, 2026-08-14).
   */
  conclusive: boolean;
}

/**
 * The purge's OWN inventory, read-only: the same detector, the same class
 * lists, the same paths — just without removing anything. It exists so the
 * wizard can leave the Earlier Install step out of the sequence entirely on a
 * machine with nothing to clean (rather than showing a step that has nothing
 * to do), without ever inventing a second detector that could disagree with
 * what the purge actually removes.
 *
 * Class C (customer data — including an unreadable setup state, which the
 * wizard heals by rewriting it) deliberately does NOT count: there is nothing
 * to clean about it, only things to keep.
 */
export async function probeLegacyFootprint(
  ctx: WizardContext,
  deps: PurgeDeps = {},
): Promise<LegacyFootprintProbe> {
  const platform = deps.platform ?? process.platform;
  const detect = deps.detect ?? detectLegacyInstall;
  const lstat = deps.lstat ?? (async (target: string) => {
    const info = await fs.lstat(target);
    return { isSymbolicLink: info.isSymbolicLink(), isDirectory: info.isDirectory(), isFile: info.isFile() };
  });
  const realpath = deps.realpath ?? ((target: string) => fs.realpath(target));
  const pathApi = path;

  let conclusive = true;
  const isAbsence = (error: unknown): boolean => {
    const code = (error as NodeJS.ErrnoException)?.code;
    return code === "ENOENT" || code === "ENOTDIR";
  };
  // ONLY genuine absence reads as absence. An EACCES/EIO answer used to
  // flatten into "not there", which let this probe certify a machine clean —
  // and write the migration marker — on the exact machines it could not
  // actually inspect (Codex review 2026-08-14, finding 5). Anything that is
  // not provable absence makes the probe INCONCLUSIVE, and inconclusive shows
  // the step. Injected test seams stay authoritative as booleans.
  const exists = async (target: string): Promise<boolean> => {
    if (deps.exists !== undefined) return guard(() => deps.exists!(target), false);
    try {
      await fs.stat(target);
      return true;
    } catch (error) {
      if (!isAbsence(error)) conclusive = false;
      return false;
    }
  };
  const listDirEntries = async (dir: string): Promise<string[] | undefined> => {
    if (deps.listDirEntries !== undefined) return guard(() => deps.listDirEntries!(dir), undefined);
    try {
      return await fs.readdir(dir);
    } catch (error) {
      if (isAbsence(error)) return [];
      return undefined;
    }
  };
  const readTextFile = async (file: string): Promise<string | undefined> => {
    if (deps.readTextFile !== undefined) return guard(() => deps.readTextFile!(file), undefined);
    try {
      return await fs.readFile(file, "utf8");
    } catch (error) {
      if (!isAbsence(error)) conclusive = false;
      return undefined;
    }
  };

  // The same symlink refusal the purge starts with: a linked base means the
  // purge would touch nothing, but it also means we could not really look.
  for (const base of [ctx.hermesAgentDir, ctx.profileDir]) {
    let link: { isSymbolicLink: boolean } | undefined;
    try {
      link = await lstat(base);
    } catch (error) {
      link = undefined;
      if (!isAbsence(error)) return { actionable: false, conclusive: false };
    }
    if (link?.isSymbolicLink === true) return { actionable: false, conclusive: false };
  }

  // The same current-engine exemptions the purge applies — evaluated once,
  // here, so "would the purge remove anything" and "does the purge remove
  // anything" cannot drift apart (F2).
  const exempt = await guard(
    () => currentEngineExemptions(ctx, deps),
    { venv: true, git: true, stamp: true },
  );

  const found = await guard(() => detect(ctx, deps), undefined);
  const artifacts = normalizeArtifacts(found);
  if (artifacts === undefined) {
    conclusive = false;
  } else {
    if (!artifacts.scanOk) conclusive = false;
    const realRoot = await guard(() => realpath(ctx.hermesRoot), undefined);
    for (const entry of artifacts.registrations) {
      if (entry.ownership !== "unresolved") continue;
      if (await registrationIsProvablyOurs(ctx, entry, platform, realRoot, realpath)) {
        entry.ownership = "owned";
      }
    }
    // Anything the detector could not ATTRIBUTE is a machine we did not fully
    // read: an access-denied task query surfaces as an unresolved entry, not a
    // scan failure, and treating that as clean would omit the step on exactly
    // the machine whose leftovers we cannot see (verification pass 2026-08-14,
    // F5). Evaluated BEFORE the actionable early-return so a mixed
    // owned-plus-unresolved answer carries the honest flag out too.
    if (
      artifacts.registrations.some((entry) => entry.ownership === "unresolved")
      || artifacts.unresolvedPids.length > 0
    ) {
      conclusive = false;
    }
    if (
      artifacts.registrations.some((entry) => entry.ownership === "owned")
      || artifacts.gatewayPids.length > 0
      // The detector's gitCheckout is the same `.git` presence signal as the
      // disk check below — a CURRENT source install's checkout is not an
      // earlier install (F2).
      || (artifacts.gitCheckout && !exempt.git)
    ) {
      return { actionable: true, conclusive };
    }
  }

  // Class A on disk, exactly the purge's list (minus the current install's
  // own machinery — the exemptions above — and minus `local-api.json`:
  // that file did not exist before v1.4.0 and the HUD rewrites it at every
  // launch, so its presence marks a CURRENT install, not an earlier one.
  // Counting it kept the Earlier Install step alive on every healthy desktop
  // rerun. The purge still clears it whenever the step runs for a real
  // reason — deleting a stale recorded address is hygiene, not detection.)
  const engineParent = pathApi.dirname(ctx.hermesAgentDir);
  const engineName = pathApi.basename(ctx.hermesAgentDir);
  const listed = await listDirEntries(engineParent);
  if (listed === undefined) conclusive = false;
  const siblings = Array.isArray(listed) ? listed.filter((entry) => typeof entry === "string") : [];
  if (siblings.some((entry) => BACKUP_DIRECTORY.test(entry) && entry.startsWith(`${engineName}.replaced-`))) {
    return { actionable: true, conclusive };
  }
  for (const target of [
    ...(exempt.venv ? [] : [pathApi.join(ctx.hermesAgentDir, "venv")]),
    ...(exempt.git ? [] : [pathApi.join(ctx.hermesAgentDir, ".git")]),
    ...(exempt.stamp ? [] : [pathApi.join(ctx.hermesAgentDir, ".jarvis-engine.json")]),
  ]) {
    if (await exists(target)) return { actionable: true, conclusive };
  }

  // Class B: superseded credential lines in the profile .env — the same key
  // list rewriteEnv() removes.
  const envPath = pathApi.join(ctx.profileDir, ".env");
  let envInfo: { isSymbolicLink: boolean; isDirectory: boolean; isFile: boolean } | undefined;
  try {
    envInfo = await lstat(envPath);
  } catch (error) {
    envInfo = undefined;
    if (!isAbsence(error)) conclusive = false;
  }
  if (envInfo !== undefined && envInfo.isFile && !envInfo.isSymbolicLink) {
    const text = await readTextFile(envPath);
    if (text === undefined) {
      conclusive = false;
    } else {
      for (const line of text.split(/\r\n|\r|\n/)) {
        const key = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/.exec(line)?.[1];
        if (key !== undefined && REGENERABLE_ENV_KEYS.includes(key)) {
          return { actionable: true, conclusive };
        }
      }
    }
  }

  return { actionable: false, conclusive };
}
