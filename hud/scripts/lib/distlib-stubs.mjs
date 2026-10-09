/**
 * The distlib launcher stubs pip vendors, and which of them a payload must not
 * carry (v1.5.1, AV-BAIT-STRIP — spec/v150/PLAN.md, Leg 2).
 *
 * pip ships six tiny prebuilt Windows launchers in `pip/_vendor/distlib/`. It
 * appends one of them to a zipped entry point to MINT a console script — that
 * is how `hermes.exe` comes into existence on Windows. They are unsigned PE
 * binaries sitting inside our installer, which makes them a classic antivirus
 * false-positive target, and in August 2026 they became a real one: THREE
 * paying customers (Bryan Proctor 4C7E6D3B, Michael Hall #65, Justin Curnutt
 * 6F2A8E5E) were blocked with the payload digest check naming the SAME file —
 * `pip/_vendor/distlib/t64-arm.exe`, an ARM launcher on an x64 machine, gone
 * from disk with no quarantine trace. Bryan's folder photo showed a surgical
 * strip: both ARM stubs removed, every x64 stub and every .py left alone.
 *
 * So: ship only the stubs the target platform can ever use.
 *  - macOS never mints a Windows exe, so all six are dead weight.
 *  - Windows x64 needs `t64.exe` (console) and `w64.exe` (windowed) as the
 *    templates pip appends to. Removing THOSE would kill console-script
 *    generation — no hermes.exe, a much worse bug than the one being fixed —
 *    so they stay, and Leg 1's antivirus copy plus the exclusions guidance
 *    cover them as the residual bait.
 *
 * This list is enforced in three places, deliberately: the vendoring build
 * applies it, `restamp-vendor-payload.mjs` applies it to an already-built
 * payload, and `verify-vendor-payload.mjs --release` REFUSES a payload that
 * still carries a stripped stub (or that lost a kept one).
 */

import fs from "node:fs";
import path from "node:path";

/** Every launcher stub pip vendors, whatever the platform. */
export const ALL_DISTLIB_STUBS = [
  "t32.exe",
  "t64.exe",
  "t64-arm.exe",
  "w32.exe",
  "w64.exe",
  "w64-arm.exe",
];

/** The stubs that must SURVIVE, per target platform — pip needs these to mint console scripts. */
const KEEP_BY_PLATFORM = {
  win32: ["t64.exe", "w64.exe"],
  darwin: [],
};

/** Payload-relative directory a stub must live under to be considered ours. */
const DISTLIB_DIR_SUFFIX = "pip/_vendor/distlib";

/** The stub filenames to remove from a payload built for `platform`. */
export function stubsToStrip(platform) {
  const keep = KEEP_BY_PLATFORM[platform];
  if (keep === undefined) {
    throw new Error(`No distlib stub policy for platform "${platform}" — add one before shipping it.`);
  }
  return ALL_DISTLIB_STUBS.filter((name) => !keep.includes(name));
}

/** The stub filenames that MUST still be present in a payload for `platform`. */
export function stubsToKeep(platform) {
  const keep = KEEP_BY_PLATFORM[platform];
  if (keep === undefined) {
    throw new Error(`No distlib stub policy for platform "${platform}" — add one before shipping it.`);
  }
  return [...keep];
}

/**
 * Every `pip/_vendor/distlib/<stub>.exe` in the payload, posix-relative.
 *
 * Walks rather than guessing paths: the stubs live inside whichever bundled
 * CPython runtimes a payload carries (`python/` on Windows, `python-arm64/`
 * and `python-x64/` on a universal mac payload), and a future payload shape
 * that vendors pip somewhere else must not silently escape the strip.
 * Symlinks are never followed — a payload's own symlinks are verified
 * elsewhere and a link is not a stub.
 */
export function findDistlibStubs(payloadRoot) {
  const found = [];
  const walk = (dir, prefix) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), relative);
      else if (entry.isFile() && ALL_DISTLIB_STUBS.includes(entry.name)
        && relative.endsWith(`${DISTLIB_DIR_SUFFIX}/${entry.name}`)) {
        found.push(relative);
      }
    }
  };
  walk(payloadRoot, "");
  return found.sort();
}

/**
 * Removes the stubs this platform can never use. Returns the payload-relative
 * paths actually removed, sorted — the caller logs them and (at build time)
 * records them in the manifest, so the trim is auditable from a shipped
 * installer rather than being invisible surgery.
 */
export function stripDistlibStubs(payloadRoot, platform) {
  const strip = new Set(stubsToStrip(platform));
  const removed = [];
  for (const relative of findDistlibStubs(payloadRoot)) {
    if (!strip.has(path.posix.basename(relative))) continue;
    fs.rmSync(path.join(payloadRoot, ...relative.split("/")), { force: true });
    removed.push(relative);
  }
  return removed;
}

/**
 * The release gate: what is wrong with this payload's stub set, as plain
 * sentences (empty array = nothing wrong). Catches BOTH directions — a
 * re-vendor that forgot to strip, and an over-eager strip that took the
 * launchers pip needs to create `hermes.exe`.
 */
export function auditDistlibStubs(payloadRoot, platform) {
  const problems = [];
  const present = findDistlibStubs(payloadRoot);
  const presentNames = new Set(present.map((relative) => path.posix.basename(relative)));
  for (const relative of present) {
    if (stubsToStrip(platform).includes(path.posix.basename(relative))) {
      problems.push(
        `payload still carries the antivirus-bait launcher stub ${relative} — `
        + `strip it (hud/scripts/restamp-vendor-payload.mjs) and regenerate the digests`,
      );
    }
  }
  for (const name of stubsToKeep(platform)) {
    if (!presentNames.has(name)) {
      problems.push(
        `payload is missing the launcher stub ${name}, which pip appends to CREATE hermes.exe — `
        + `without it the venv builds no console scripts at all`,
      );
    }
  }
  return problems;
}
