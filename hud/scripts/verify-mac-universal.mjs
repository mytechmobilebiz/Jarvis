#!/usr/bin/env node
/**
 * Proves a universal Mac payload actually WORKS on both architectures.
 *
 * A universal DMG carries one runtime per architecture (PBS ships no
 * universal2 CPython), and until now nothing installed from the non-host one:
 * the builder ran `python -V` at best and the unit test mocked the copy
 * (review r3, finding 7). Half of the Mac customers would have discovered that
 * for us.
 *
 * This performs the REAL thing for each variant, into a throwaway hermes root:
 *   unpack -> install the runtime -> lay down the libraries (a SITE payload
 *   copies them already installed; a wheelhouse payload builds a venv and pip
 *   installs offline) ->
 *   `hermes --version` -> import probe.
 * The x64 variant runs under Rosetta on Apple Silicon, which is exactly how an
 * Intel-targeted binary behaves for us here; a native x64 runner would be
 * better and CI can add one, but this catches everything except CPU-specific
 * codegen bugs.
 *
 * The release checklist runs this before cutting a Mac build:
 *   pnpm --dir hud run verify:mac-universal
 * Exit code is the gate: nonzero means DO NOT SHIP.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const payloadRoot = process.argv[2] ?? path.join(repoRoot, "hud", "vendor", "engine");

function fail(message) {
  console.error(`[verify-mac-universal] FAIL: ${message}`);
  process.exit(1);
}

if (process.platform !== "darwin") fail(`this check must run on macOS (host is ${process.platform})`);

const manifestPath = path.join(payloadRoot, "vendor-manifest.json");
if (!fs.existsSync(manifestPath)) fail(`no payload at ${payloadRoot} — build it with vendor:mac first`);
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
if (manifest.platform !== "darwin") fail(`payload targets ${manifest.platform}, not darwin`);
if (manifest.arch !== "universal") fail(`payload arch is ${manifest.arch}; a universal DMG needs a universal payload`);

const variants = manifest.python?.variants ?? {};
for (const arch of ["arm64", "x64"]) {
  if (typeof variants[arch] !== "string") fail(`manifest has no python.variants.${arch}`);
}

// Rosetta is required to exercise the x64 runtime on Apple Silicon. Its absence
// must FAIL rather than silently skip — a skipped check is how #7 happened.
if (os.arch() === "arm64") {
  try {
    execFileSync("arch", ["-x86_64", "/usr/bin/true"], { stdio: "ignore" });
  } catch {
    fail(
      "Rosetta 2 is required to verify the x64 runtime on Apple Silicon. "
      + "Install it with: softwareupdate --install-rosetta --agree-to-license",
    );
  }
}

const setupDist = path.join(repoRoot, "setup", "dist");
if (!fs.existsSync(path.join(setupDist, "vendored-engine.js"))) {
  fail("setup/dist is not built — run `pnpm -r build` first");
}

const { readVendorManifest } = await import(path.join(setupDist, "vendor.js"));
const { installVendoredEngine } = await import(path.join(setupDist, "vendored-engine.js"));
const { hermesInvocation } = await import(path.join(setupDist, "system.js"));

/**
 * P1 regression gate (2026-08-14): `cron create` through the EXACT command
 * shape the wizard spawns. The battery used to prove install/import/gateway
 * but never a hermes SUBCOMMAND through hermesInvocation(), so a spawn that
 * dropped the site payload's `-c <entry>` prefix shipped: the Compose Roles
 * step ran `python3 -p jarvis cron create …`, Python rejected `-p`, and the
 * customer's schedules silently never existed. This drives create -> list ->
 * remove against a throwaway profile inside the throwaway hermes root
 * (HERMES_HOME with a `profiles/` parent is trusted as the profile dir by the
 * engine, so nothing here can touch the operator's real ~/.hermes).
 */
function verifyCronRoundTrip(ctx, hermesRoot, arch) {
  const scratchProfile = path.join(hermesRoot, "profiles", "scratch");
  fs.mkdirSync(scratchProfile, { recursive: true });
  const env = {
    ...process.env,
    HERMES_HOME: scratchProfile,
    PYTHONDONTWRITEBYTECODE: "1",
  };
  const runHermes = (args) => {
    const invocation = hermesInvocation(ctx, args);
    return execFileSync(invocation.command, invocation.args, { encoding: "utf8", env, timeout: 120_000 });
  };

  const created = runHermes([
    "cron", "create", "0 6 * * *", "Round-trip probe; never runs.", "--name", "verify-roundtrip",
  ]);
  const jobId = /Created job:\s*(\S+)/.exec(created)?.[1];
  if (jobId === undefined) throw new Error(`cron create did not confirm a job:\n${created.slice(0, 400)}`);
  const listed = runHermes(["cron", "list"]);
  if (!listed.includes("verify-roundtrip")) throw new Error(`cron list does not show the created job:\n${listed.slice(0, 400)}`);
  const removed = runHermes(["cron", "remove", jobId]);
  if (!/Removed job/i.test(removed)) throw new Error(`cron remove did not confirm:\n${removed.slice(0, 400)}`);
  console.log(`[verify-mac-universal] ${arch}: cron create -> list -> remove OK through the wizard's spawn shape`);
}

const parsed = await readVendorManifest(payloadRoot);
let failures = 0;

for (const arch of ["arm64", "x64"]) {
  const hermesRoot = fs.mkdtempSync(path.join(os.tmpdir(), `jarvis-verify-${arch}-`));
  const ctx = {
    dryRun: false,
    packaged: false,
    homeDir: os.homedir(),
    repoRoot,
    hermesRoot,
    hermesAgentDir: path.join(hermesRoot, "hermes-agent"),
    profileDir: path.join(hermesRoot, "profiles", "jarvis"),
    io: { inheritsStdio: false, command: () => {}, commandOutput: () => {}, note: () => {}, warn: () => {} },
    state: {},
    summary: [],
  };
  const started = Date.now();
  try {
    const result = await installVendoredEngine(ctx, payloadRoot, parsed, { arch });
    const seconds = Math.round((Date.now() - started) / 1000);
    if (result.failure !== undefined) {
      console.error(`[verify-mac-universal] ${arch}: FAILED (${result.failure}) — ${result.notes.join(" | ")}`);
      failures += 1;
    } else {
      // The install path already ran `hermes --version` and the import probe;
      // reaching here without a failure means both passed on this runtime.
      const interpreter = path.join(hermesRoot, "python", "bin", "python3");
      const reported = execFileSync(interpreter, ["-V"], { encoding: "utf8" }).trim();
      console.log(`[verify-mac-universal] ${arch}: OK in ${seconds}s (${reported}, engine ${parsed.engineTag})`);
      verifyCronRoundTrip(ctx, hermesRoot, arch);
    }
  } catch (error) {
    console.error(`[verify-mac-universal] ${arch}: FAILED — ${error.message}`);
    failures += 1;
  } finally {
    fs.rmSync(hermesRoot, { recursive: true, force: true });
  }
}

if (failures > 0) fail(`${failures} architecture(s) could not run the bundled engine`);
console.log("[verify-mac-universal] OK: both architectures installed and ran the bundled engine.");
