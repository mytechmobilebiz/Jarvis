#!/usr/bin/env node
/**
 * build-vendor-payload.mjs — assemble the OFFLINE engine payload (v1.4.0 Lane B, B1).
 *
 * Builds `hud/vendor/engine/` so the packaged installer can install the Hermes
 * engine on a customer machine with ZERO network access. Everything that can
 * fail over the network fails HERE, at build time, on our machine — never in
 * a customer's living room at 2am.
 *
 * Output contract (FIXED — the runtime installer depends on it byte-for-byte):
 *
 *   engine/
 *     vendor-manifest.json     # written LAST — its presence = "payload complete"
 *     digests.sha256           # sha256 of EVERY payload file (see point 7 below)
 *     hermes-agent/            # pruned engine source tree, no .git
 *     wheelhouse/
 *       requirements.txt       # fully pinned name==version, one per line, LF, sorted
 *       *.whl                  # every wheel for requirements.txt + the engine's own wheel
 *     python/                  # relocatable CPython (single-arch payloads)
 *     python-arm64/            # darwin --arch universal ONLY: both runtimes,
 *     python-x64/              #   installer picks variants[process.arch]
 *
 * WHY each choice was made:
 *
 * 1. REQUIREMENTS DERIVATION — `uv export --frozen` from the engine's uv.lock.
 *    The lock is the engine's single source of truth for the full transitive
 *    closure; re-resolving with pip/uv-pip-compile could drift from what the
 *    engine team tested. `--frozen` asserts the lock is used as-is (no network,
 *    no re-resolution). We do NOT reimplement a uv.lock parser as a fallback:
 *    a hand-rolled resolver that gets extras/marker intersection subtly wrong
 *    would produce a payload that *looks* complete and fails on the customer
 *    machine — the exact failure class this script exists to kill. If `uv` is
 *    missing, we fail loudly with install instructions (CI: astral-sh/setup-uv;
 *    local: `curl -LsSf https://astral.sh/uv/install.sh | sh`).
 *
 * 2. MARKER EVALUATION IN THIS SCRIPT — `uv export` emits PEP 508 environment
 *    markers (e.g. `pywinpty==2.0.15 ; sys_platform == 'win32'`). `pip download
 *    --platform win_amd64` does NOT re-evaluate those markers for the target —
 *    pip evaluates markers against the HOST interpreter (long-standing pip
 *    limitation), so downloading a Windows wheelhouse from macOS would silently
 *    drop pywinpty/pywin32/colorama and ship a broken payload. We therefore
 *    evaluate the markers ourselves for the TARGET environment and write a
 *    marker-free, fully pinned requirements.txt. The evaluator handles exactly
 *    the marker grammar uv emits (==, !=, <, <=, >, >=, in, not in, and, or,
 *    parentheses) and THROWS on anything it cannot parse — never guesses.
 *
 * 3. WHEEL DOWNLOAD — `pip download --only-binary=:all: --no-deps` per pinned
 *    requirement set, with explicit --platform/--python-version/--implementation/
 *    --abi for the TARGET. `--no-deps` because the lock already IS the closure;
 *    letting pip resolve deps cross-platform would evaluate wheel metadata
 *    markers against the host (same trap as above) and could silently pull
 *    unpinned versions. Any locked dep without a compatible wheel => pip exits
 *    nonzero => build fails loudly with the package name. NEVER sdists: an sdist
 *    means "compile on the customer machine", which means "fail on the customer
 *    machine".
 *
 * 4. ENGINE WHEEL — `uv build --wheel` from the pruned source. Chosen over
 *    `pip wheel --no-build-isolation` because (a) build isolation honors the
 *    engine's pinned build backend (`setuptools>=77,<83` — PEP 639 license
 *    expression needs >=77) instead of whatever the host happens to have, and
 *    (b) uv picks/fetches an interpreter satisfying the engine's
 *    requires-python (>=3.11,<3.14) even when the host python is newer (pip
 *    refuses to build under 3.14 without --ignore-requires-python hacks).
 *    The wheel is pure Python (py3-none-any) so building on macOS for a
 *    Windows target is fine.
 *
 * 5. PYTHON RUNTIME — astral-sh/python-build-standalone `install_only` build,
 *    NOT the Windows embeddable package. The embeddable package ships no
 *    `venv`/`ensurepip` and its `._pth` file hard-codes sys.path in a way that
 *    breaks site-packages resolution — you end up patching `._pth` at install
 *    time and debugging import errors remotely. PBS is a full stdlib,
 *    relocatable, needs no admin rights, and is exactly what `uv` itself
 *    installs when it manages Pythons — the most battle-tested relocatable
 *    CPython that exists. Release + versions are pinned below; bump them
 *    together, deliberately, never implicitly.
 *
 * 6. MANIFEST LAST — vendor-manifest.json is written as the FINAL step. The
 *    runtime installer treats its presence as "payload is complete", so a
 *    build that dies halfway can never leave a payload that looks whole.
 *    Managed sub-paths are wiped at start for the same reason (idempotent:
 *    same inputs => same payload; stale wheels from a previous run with
 *    different extras can never linger).
 *
 * 7. CONTENT DIGESTS + PROVENANCE (review finding 6) — file NAMES surviving is
 *    not integrity: a corrupt wheel or a swapped python.exe used to pass
 *    verification because only names and the wheel count were checked. Now,
 *    after everything is assembled and BEFORE the manifest exists, we hash
 *    every payload file into `digests.sha256` (`<sha256>  <posix/rel/path>`,
 *    sorted, LF — the sha256sum format) and pin THAT file's hash in the
 *    manifest. Root of trust: manifest -> digests.sha256 -> every byte.
 *    verify-vendor-payload.mjs and the runtime installer both walk this same
 *    chain, so the format is a FIXED contract — do not change it casually.
 *    The manifest also records HOW the source was obtained (`source.kind`),
 *    anchored to the checked-in pin hud/vendor/engine-pin.json (r2 finding 9
 *    — self-computed hashes only prove the payload matches ITSELF): a local
 *    checkout is only trusted if git proves HEAD is EXACTLY the pinned COMMIT
 *    (not a same-named tag) with a clean tree; anything else needs
 *    --allow-dirty-source, is stamped "local-dirty", and is refused by the
 *    --release verifier. A downloaded tarball must hash to the pinned
 *    tarballSha256 ("tarball-sha256"); with the pin unset the observed hash
 *    is recorded but the kind is "tarball-unpinned", which --release refuses.
 *
 * 8. UNIVERSAL MAC = DUAL RUNTIME (review finding 7) — dist:mac produces a
 *    universal Electron app, but PBS ships no universal2 CPython. A host-arch
 *    python inside a universal .app means the OTHER architecture gets a
 *    binary it cannot exec — discovered by a customer, not by us. So
 *    `--platform darwin --arch universal` now vendors BOTH runtimes
 *    (python-arm64/ + python-x64/, each pruned identically) and the manifest
 *    lists them under python.variants; the runtime installer selects
 *    variants[process.arch] ?? python.executable. Single-arch payloads keep
 *    the plain python/ layout and omit variants.
 *
 * Requirements on the BUILD machine (all fail loudly if missing):
 *   node >= 20, `uv` on PATH, a python with pip (for `pip download`), `tar`
 *   (ships with Win10+ / macOS), network access to pypi.org + github.com.
 *
 * Runs unchanged on windows-latest GitHub Actions: no shell strings, argv
 * arrays only, `tar` resolves to System32 tar.exe on Windows.
 *
 * Usage:
 *   node hud/scripts/build-vendor-payload.mjs [options]
 *     --out <dir>            output dir (default hud/vendor/engine)
 *     --platform win32|darwin  target platform (default: host)
 *     --arch x64|arm64|universal  target arch (default: host; universal = mac only)
 *     --engine-src <dir>     local engine tree (default ~/.hermes/hermes-agent
 *                            if it exists, else download the tag tarball)
 *     --allow-dirty-source   accept a local --engine-src that is NOT a clean
 *                            checkout of the pinned tag. DEV ONLY: the payload
 *                            is stamped source.kind="local-dirty" and the
 *                            --release verifier refuses it.
 *     --tag <tag>            engine tag (default v2026.7.1)
 *     --extras a,b,c         extras (default cli,mcp,slack,google,anthropic,edge-tts)
 *     --python-version 3.12  target CPython minor (default 3.12)
 *     --skip-python          skip the CPython runtime (dev/testing only — the
 *                            resulting payload is NOT shippable)
 *     --clean                wipe the out dir entirely first
 */

import { spawnSync } from "node:child_process";
import { createWriteStream } from "node:fs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";
import { applyEnginePatches } from "./lib/engine-patches.mjs";
import { stripDistlibStubs } from "./lib/distlib-stubs.mjs";
import { dirSize, sha256File, writeDigestFile } from "./lib/payload-digest.mjs";

// ---------------------------------------------------------------------------
// Pinned constants — bump deliberately, together, with a release note.
// ---------------------------------------------------------------------------

/** Engine tag JARVIS pins (mirrors setup/src/steps/hermes.ts HERMES_VERSION). */
const DEFAULT_ENGINE_TAG = "v2026.7.1";
const ENGINE_REPO_TARBALL = (tag) =>
  `https://github.com/NousResearch/hermes-agent/archive/refs/tags/${tag}.tar.gz`;

/**
 * ROOT OF TRUST (review r2 finding 9) — hud/vendor/engine-pin.json, checked
 * into source control. Tags are movable pointers and a hash the builder
 * computed itself only proves the payload matches ITSELF; the pin is the one
 * identity that cannot be changed without a reviewed commit:
 *   { tag, commit, tarballSha256 }
 * The builder refuses to stamp a trusted source.kind unless the source
 * PROVABLY matches the pin (local HEAD commit === pin.commit, or downloaded
 * tarball sha256 === pin.tarballSha256), and verify-vendor-payload --release
 * independently re-reads this file and re-checks the manifest against it.
 *
 * To (re)fill after a deliberate engine bump:
 *   git ls-remote https://github.com/NousResearch/hermes-agent.git \
 *     refs/tags/<tag> "refs/tags/<tag>^{}"     # use the ^{} (dereferenced) sha
 *   curl -L -o /tmp/eng.tgz https://github.com/NousResearch/hermes-agent/archive/refs/tags/<tag>.tar.gz
 *   shasum -a 256 /tmp/eng.tgz
 */
const ENGINE_PIN_PATH = () => path.join(HUD_DIR, "vendor", "engine-pin.json");

function loadEnginePin() {
  const pinPath = ENGINE_PIN_PATH();
  let raw;
  try {
    raw = fs.readFileSync(pinPath, "utf8");
  } catch (err) {
    fail(
      `Engine pin file is missing: ${pinPath} (${err.code ?? err.message}).\n` +
      `  It is the checked-in root of trust for the vendored engine — a build ` +
      `without it cannot prove WHAT it is vendoring. Restore it from git.`,
    );
  }
  let pin;
  try {
    pin = JSON.parse(raw);
  } catch (err) {
    fail(`Engine pin file ${pinPath} is not valid JSON: ${err.message}`);
  }
  if (typeof pin.tag !== "string" || pin.tag === "") {
    fail(`Engine pin file ${pinPath} has no "tag"`);
  }
  if (typeof pin.commit !== "string" || (pin.commit !== "" && !/^[0-9a-f]{40}$/.test(pin.commit))) {
    fail(`Engine pin "commit" must be empty or a 40-hex sha (${pinPath})`);
  }
  if (
    typeof pin.tarballSha256 !== "string" ||
    (pin.tarballSha256 !== "" && !/^[0-9a-f]{64}$/.test(pin.tarballSha256))
  ) {
    fail(`Engine pin "tarballSha256" must be empty or a 64-hex sha256 (${pinPath})`);
  }
  return pin;
}

/** How to fill an unset pin — echoed in every "pin is unset" failure. */
const PIN_FILL_HINT = (tag) =>
  `Fill hud/vendor/engine-pin.json:\n` +
  `    commit:        git ls-remote https://github.com/NousResearch/hermes-agent.git ` +
  `refs/tags/${tag} "refs/tags/${tag}^{}"  (take the ^{} sha)\n` +
  `    tarballSha256: curl -L ${ENGINE_REPO_TARBALL(tag)} | shasum -a 256`;

/**
 * Extras the product actually uses. NOT `all` — `all` drags in matrix/modal/
 * daytona/etc. and hundreds of MB nobody launches.
 *
 * `voice` is deliberately ABSENT (2026-08-13, orchestrator decision on the
 * DECISION.md §3 split): Lane C moved voice wholly into the HUD — whisper.cpp
 * for speech-in and Kokoro for speech-out, bundled as their own child
 * processes — so the engine-side voice stack (faster-whisper, sounddevice)
 * is dead weight in our product path. NOTHING packaged may depend on the
 * engine's voice extra. If that ever changes, add it back HERE, rebuild, and
 * re-measure: the extra costs roughly 40 MB of wheels
 * (ctranslate2/onnxruntime/av) on Windows.
 *
 * `edge-tts` IS present (Peter's order, 2026-08-14): the engine's CLI
 * lazily pip-installs edge-tts==7.2.7 on its first chat (tools/tts_tool.py
 * probes the edge provider through tools/lazy_deps.ensure), which made the
 * first run a NETWORK dependency and put ~3 s of pip in front of the first
 * answer — on a product sold as fully-offline-installable. It is a declared
 * optional extra of the pinned engine (pyproject `edge-tts = ["edge-tts==
 * 7.2.7"]`), so baking it is the normal resolver path, no out-of-lock
 * addition: it adds exactly edge-tts 7.2.7 + tabulate (both pure Python;
 * aiohttp/certifi/typing-extensions were already in the tree). With the
 * package importable, lazy_deps.ensure() short-circuits before any network.
 * The voice-stack trim above is unaffected.
 *
 * The list is echoed into vendor-manifest.json so the trim stays visible.
 */
const DEFAULT_EXTRAS = ["cli", "mcp", "slack", "google", "anthropic", "edge-tts"];

/**
 * python-build-standalone pin. Release 20250612, verified to carry
 * install_only tarballs for every triple we target (win-arm64 does NOT exist
 * in this release — we fail loudly on that combo rather than pretend).
 * WHY PBS and not the Windows embeddable package: see header point 5.
 */
const PBS_RELEASE = "20250612";
const PBS_PYTHON_VERSIONS = {
  "3.11": "3.11.13",
  "3.12": "3.12.11",
  "3.13": "3.13.5",
};
const PBS_TRIPLES = {
  "win32/x64": "x86_64-pc-windows-msvc",
  "darwin/arm64": "aarch64-apple-darwin",
  "darwin/x64": "x86_64-apple-darwin",
};
const pbsUrl = (fullVersion, triple) =>
  `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/` +
  `cpython-${fullVersion}%2B${PBS_RELEASE}-${triple}-install_only.tar.gz`;

/**
 * macOS wheel platform baselines (pip expands a baseline DOWNWARD to older
 * macOS versions + universal2 variants, never upward). These are effectively
 * the minimum macOS version of the mac payload:
 *  - arm64 baseline 14.0 is dictated by the locked voice deps: av==17.0.0 and
 *    onnxruntime>=1.24 publish only macosx_14_0_arm64 wheels. Every Apple
 *    Silicon Mac can run macOS 14, so this costs only users who refuse to
 *    update the OS — and a lower baseline would simply fail the build (loudly).
 *  - x86_64 baseline 11.0 is dictated by av==17.0.0 (macosx_11_0_x86_64).
 *    NOTE: onnxruntime>=1.24 ships NO x86_64 mac wheels at all, so an Intel
 *    or universal mac payload with the `voice` extra is impossible at current
 *    pins — the build fails loudly on it, by design.
 */
const MAC_PLATFORM_TAGS = {
  arm64: "macosx_14_0_arm64",
  x64: "macosx_11_0_x86_64",
};

/** Dirs/patterns pruned from the shipped engine source tree.
 *  ANY depth: pure junk that must never ship, wherever it hides.
 *  TOP level only: real content dirs that are dev/CI/web-only — pruned by
 *  exact top-level name so a plugin that happens to contain e.g. a `web/`
 *  subfolder is not gutted. KEEP plugins/ and optional-skills/ (the cron
 *  scheduler provider loads from plugins/cron_providers/). */
const PRUNE_ANY_DEPTH = new Set([
  ".git",
  "node_modules",
  "venv",
  ".venv",
  "__pycache__",
  ".github",
  ".DS_Store",
]);
const PRUNE_ANY_DEPTH_SUFFIXES = [".pyc", ".egg-info"];
const PRUNE_TOP_LEVEL = new Set([
  "apps",
  "website",
  "web",
  "tests",
  "docs",
  "infographic",
  "datagen-config-examples",
  "demo",
]);

// ---------------------------------------------------------------------------
// Tiny utils
// ---------------------------------------------------------------------------

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HUD_DIR = path.resolve(__dirname, "..");

class BuildError extends Error {}
const fail = (msg) => {
  throw new BuildError(msg);
};
const log = (msg) => console.log(msg);
const step = (msg) => console.log(`\n=== ${msg}`);

const fmtBytes = (n) => {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
};

const rmrf = (p) => fs.rmSync(p, { recursive: true, force: true });

/**
 * Trims the bundled CPython to what the appliance actually runs, and NOTHING
 * more (orchestrator decision, 2026-08-13: the Windows runtime was 130 MB
 * against mac's 52 MB, almost all of it dead weight).
 *
 * The rule for this list: remove only things whose absence cannot change how
 * the engine behaves. Concretely —
 *   - `test` / `idlelib` / `turtledemo` / `lib2to3`: developer toys and the
 *     CPython test suite; nothing imports them at runtime.
 *   - tkinter + the tcl/tk libraries and DLLs: a GUI toolkit. The gateway is
 *     headless; the HUD's UI is Electron.
 *   - `*.pdb`: Windows debug symbols for CPython itself. Useless without a
 *     matching debugger session, and the bulk of the Windows overage.
 *   - `Doc` / `share/man` / `share/doc`: documentation.
 * Deliberately KEPT even though they look prunable: `ensurepip` and `venv`
 * (the install path IS `python -m venv` + pip), `libs/`/`include/` (a wheel
 * that ships a build hook can still want headers), `__pycache__` (removing it
 * only makes first start slower), and every stdlib C extension — `sqlite3`,
 * `ssl`, `ctypes` and friends are load-bearing for the engine.
 *
 * The caller runs a venv+pip+import smoke immediately after this on a
 * same-platform build, so an over-eager entry fails the BUILD, not a customer.
 *
 * Returns the payload-relative paths actually removed, which go into the
 * manifest so the trim is auditable from a shipped installer.
 */
function prunePythonRuntime(pythonRoot, platform, pyMinor, payloadDirName = "python") {
  const removed = [];
  const drop = (relative) => {
    const target = path.join(pythonRoot, ...relative.split("/"));
    if (!fs.existsSync(target)) return;
    rmrf(target);
    removed.push(`${payloadDirName}/${relative}`);
  };

  const stdlib = platform === "win32" ? "Lib" : `lib/python${pyMinor}`;
  for (const name of ["test", "idlelib", "turtledemo", "lib2to3", "tkinter"]) {
    drop(`${stdlib}/${name}`);
  }
  drop(`${stdlib}/turtle.py`);

  if (platform === "win32") {
    drop("tcl");
    drop("Doc");
    // tkinter's own extension module plus the tcl/tk runtime DLLs. Nothing
    // else is touched here — zlib/sqlite/ssl DLLs stay.
    const dllDir = path.join(pythonRoot, "DLLs");
    if (fs.existsSync(dllDir)) {
      for (const entry of fs.readdirSync(dllDir, { withFileTypes: true })) {
        if (/^(_tkinter\.pyd|tcl\w*\.dll|tk\w*\.dll)$/i.test(entry.name)) drop(`DLLs/${entry.name}`);
      }
    }
    // CPython's debug symbols, wherever they landed.
    for (const relative of walkFiles(pythonRoot).filter((f) => f.toLowerCase().endsWith(".pdb"))) {
      drop(relative);
    }
  } else {
    drop("share");
    drop(`${stdlib}/lib-dynload/_tkinter.cpython-${pyMinor.replace(".", "")}-darwin.so`);
    const libDir = path.join(pythonRoot, "lib");
    if (fs.existsSync(libDir)) {
      for (const entry of fs.readdirSync(libDir, { withFileTypes: true })) {
        // tcl8.6/, tk8.6/, itcl4.x/, libtcl8.6.dylib, libtk8.6.dylib, ...
        if (/^(lib)?(tcl|tk|itcl|thread)/i.test(entry.name)) drop(`lib/${entry.name}`);
      }
    }
  }
  return removed;
}

/** Payload-relative file list, for pattern sweeps (symlinks are not followed). */
/**
 * Files a build TOOL leaves behind, which are no part of the payload.
 *
 * Deliberately a closed list rather than "every dotfile": the engine source
 * tree legitimately ships dotfiles, and a blanket rule would silently drop one
 * the runtime needs.
 */
const BUILD_ARTIFACTS = ["wheelhouse/.gitignore", ".gitignore", ".DS_Store"];

function pruneBuildArtifacts(outDir) {
  for (const relative of BUILD_ARTIFACTS) {
    const target = path.join(outDir, ...relative.split("/"));
    if (fs.existsSync(target)) {
      fs.rmSync(target, { force: true });
      log(`  pruned build artifact ${relative}`);
    }
  }
}

function walkFiles(root, prefix = "") {
  const out = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) out.push(...walkFiles(path.join(root, entry.name), relative));
    else if (entry.isFile()) out.push(relative);
  }
  return out;
}

/** Run a command (argv array, NEVER a shell string — Windows CI safe).
 *  Fails the build on nonzero exit unless allowFailure. */
function run(cmd, args, { cwd, capture = false, allowFailure = false, env, quiet = false } = {}) {
  const pretty = `${cmd} ${args.join(" ")}`;
  // Signing runs this once per binary; echoing 76 identical command lines
  // buries the report the build actually has to communicate.
  if (!quiet) log(`  $ ${pretty}${cwd ? `   (cwd: ${cwd})` : ""}`);
  const res = spawnSync(cmd, args, {
    cwd,
    env: env ? { ...process.env, ...env } : process.env,
    stdio: capture ? ["ignore", "pipe", "pipe"] : ["ignore", "inherit", "inherit"],
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (res.error) {
    if (res.error.code === "ENOENT") {
      if (allowFailure) return { ok: false, missing: true, stdout: "", stderr: "" };
      fail(`Command not found: ${cmd} — is it installed and on PATH?`);
    }
    if (allowFailure) return { ok: false, stdout: "", stderr: String(res.error) };
    fail(`Failed to spawn "${pretty}": ${res.error.message}`);
  }
  if (res.status !== 0 && !allowFailure) {
    if (capture && res.stderr) console.error(res.stderr);
    fail(`Command failed (exit ${res.status}): ${pretty}`);
  }
  return { ok: res.status === 0, stdout: res.stdout ?? "", stderr: res.stderr ?? "" };
}

/** Download url -> destFile via global fetch (Node >= 18), following redirects.
 *  Retries transient failures (GitHub release CDN flakes are routine); a URL
 *  that still fails after retries fails the build with the real cause. */
async function download(url, destFile, attempts = 3) {
  fs.mkdirSync(path.dirname(destFile), { recursive: true });
  let lastErr;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      log(`  GET ${url}${attempt > 1 ? ` (attempt ${attempt}/${attempts})` : ""}`);
      const res = await fetch(url, { redirect: "follow" });
      if (!res.ok || !res.body) {
        fail(`Download failed: HTTP ${res.status} for ${url}`);
      }
      await pipeline(Readable.fromWeb(res.body), createWriteStream(destFile));
      const size = fs.statSync(destFile).size;
      if (size === 0) fail(`Download produced an empty file: ${url}`);
      log(`  -> ${destFile} (${fmtBytes(size)})`);
      return;
    } catch (err) {
      rmrf(destFile); // never leave a truncated download behind
      const cause = err?.cause ? ` (cause: ${err.cause.code ?? err.cause.message ?? err.cause})` : "";
      lastErr = new BuildError(`Download failed for ${url}: ${err.message}${cause}`);
      if (attempt < attempts) {
        const waitMs = attempt * 2000;
        log(`  download error${cause}: ${err.message} — retrying in ${waitMs / 1000}s`);
        await new Promise((r) => setTimeout(r, waitMs));
      }
    }
  }
  throw lastErr;
}

/** Extract a .tar.gz with the system tar (bsdtar on Win10+/macOS, GNU on Linux). */
function extractTarGz(tarball, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  run("tar", ["-xzf", tarball, "-C", destDir]);
}

/** Move a directory, falling back to copy+delete across devices (CI mounts
 *  tmp on a different volume sometimes; renameSync throws EXDEV there).
 *  verbatimSymlinks keeps the PBS mac runtime's bin/python3 -> python3.x
 *  links as links instead of exploding them into duplicate binaries. */
function moveDir(src, dst) {
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  try {
    fs.renameSync(src, dst);
  } catch (err) {
    if (err.code !== "EXDEV") throw err;
    fs.cpSync(src, dst, { recursive: true, verbatimSymlinks: true });
    rmrf(src);
  }
}

/** Would this repo-relative path be pruned out of the shipped tree anyway?
 *  Used to ignore venv/__pycache__/tests/... noise in the dirty-source check:
 *  only changes that could actually SHIP make a source "dirty". */
function isPrunedSourcePath(relPosix) {
  const clean = relPosix.replace(/\/+$/, "");
  const parts = clean.split("/");
  if (parts.some((part) => PRUNE_ANY_DEPTH.has(part))) return true;
  if (PRUNE_ANY_DEPTH_SUFFIXES.some((suffix) => parts[parts.length - 1].endsWith(suffix))) return true;
  if (PRUNE_TOP_LEVEL.has(parts[0])) return true;
  return false;
}

/**
 * Prove (or refuse to pretend) where a local engine tree came from.
 * "The manifest claims v2026.7.1" is only an honest claim if git confirms
 * HEAD is exactly the COMMIT the checked-in pin names AND nothing shippable
 * is modified. A local tag named v2026.7.1 proves nothing — tags are movable
 * pointers anyone can retarget with one command; the pin's commit sha is the
 * identity (review r2 finding 9). Anything less is a dev build: allowed only
 * behind --allow-dirty-source, stamped source.kind="local-dirty" so
 * verify --release refuses it forever after.
 * Returns the manifest `source` object.
 */
function assessLocalSourceProvenance(srcDir, tag, allowDirty, pin) {
  const problems = [];
  const gitRun = (args) =>
    run("git", ["-C", srcDir, ...args], { capture: true, allowFailure: true });

  const head = gitRun(["rev-parse", "HEAD"]);
  const commit = head.ok ? head.stdout.trim() : "";
  if (head.missing) {
    problems.push("git is not installed/on PATH — cannot prove what this tree is");
  } else if (!head.ok) {
    problems.push("not a git repository — cannot prove what this tree is");
  } else {
    if (tag !== pin.tag) {
      problems.push(
        `building tag ${tag} but the checked-in pin is for ${pin.tag} — ` +
        `only the pinned tag can produce a trusted payload (update engine-pin.json deliberately if bumping)`,
      );
    } else if (pin.commit === "") {
      problems.push(
        `engine-pin.json has an empty "commit" — there is no root of trust to compare HEAD against.\n` +
        `      ${PIN_FILL_HINT(tag).split("\n").join("\n      ")}`,
      );
    } else if (commit !== pin.commit) {
      problems.push(
        `HEAD is ${commit}, but the pinned ${tag} commit is ${pin.commit} ` +
        `(a local tag named ${tag} would NOT count — commits, not tag names, are identity)`,
      );
    }
    const status = gitRun(["status", "--porcelain"]);
    if (!status.ok) {
      problems.push(`git status failed: ${status.stderr.trim() || "unknown error"}`);
    } else {
      const dirty = [];
      for (const line of status.stdout.split("\n")) {
        if (!line.trim()) continue;
        // porcelain v1: "XY path" or "XY old -> new"; unusual names are quoted.
        let p = line.slice(3);
        const arrow = p.indexOf(" -> ");
        if (arrow !== -1) p = p.slice(arrow + 4);
        if (p.startsWith('"') && p.endsWith('"')) p = p.slice(1, -1);
        if (!isPrunedSourcePath(p)) dirty.push(`${line.slice(0, 2).trim() || "??"} ${p}`);
      }
      if (dirty.length > 0) {
        const shown = dirty.slice(0, 15);
        problems.push(
          `working tree has ${dirty.length} shippable change(s):\n` +
          shown.map((d) => `        ${d}`).join("\n") +
          (dirty.length > shown.length ? `\n        ... and ${dirty.length - shown.length} more` : ""),
        );
      }
    }
  }

  if (problems.length === 0) {
    log(`  provenance: clean checkout of pinned ${tag} commit ${commit.slice(0, 12)} — source.kind=git-tag-clean`);
    return { kind: "git-tag-clean", commit, tarballSha256: "", pinnedCommit: pin.commit };
  }
  if (!allowDirty) {
    fail(
      `Local engine source ${srcDir} cannot be trusted as ${tag}:\n` +
      problems.map((p) => `    - ${p}`).join("\n") +
      `\n  Fix the checkout, use the tag tarball (drop --engine-src), or — for a` +
      `\n  DEV build only — pass --allow-dirty-source.`,
    );
  }
  const banner = "!".repeat(74);
  console.warn(`\n${banner}`);
  console.warn("!!  DIRTY ENGINE SOURCE — DEV BUILD ONLY");
  console.warn(`!!  ${srcDir} is not a clean checkout of pinned ${tag}:`);
  for (const p of problems) console.warn(`!!    - ${p.split("\n")[0]}`);
  console.warn("!!  The manifest will record source.kind=\"local-dirty\".");
  console.warn("!!  This payload is NOT SHIPPABLE: verify-vendor-payload --release");
  console.warn("!!  (and therefore dist:win / dist:mac) will REFUSE it.");
  console.warn(`${banner}\n`);
  return { kind: "local-dirty", commit, tarballSha256: "", pinnedCommit: pin.commit };
}

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const opts = {
    out: path.join(HUD_DIR, "vendor", "engine"),
    platform: process.platform,
    arch: process.arch,
    engineSrc: null,
    allowDirtySource: false,
    tag: DEFAULT_ENGINE_TAG,
    extras: DEFAULT_EXTRAS,
    pythonMinor: "3.12",
    skipPython: false,
    clean: false,
  };
  const takeValue = (i, name) => {
    if (i + 1 >= argv.length) fail(`${name} requires a value`);
    return argv[i + 1];
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    switch (a) {
      case "--out": opts.out = path.resolve(takeValue(i, a)); i++; break;
      case "--platform": opts.platform = takeValue(i, a); i++; break;
      case "--arch": opts.arch = takeValue(i, a); i++; break;
      case "--engine-src": opts.engineSrc = path.resolve(expandHome(takeValue(i, a))); i++; break;
      case "--allow-dirty-source": opts.allowDirtySource = true; break;
      case "--tag": opts.tag = takeValue(i, a); i++; break;
      case "--extras":
        opts.extras = takeValue(i, a).split(",").map((s) => s.trim()).filter(Boolean);
        i++;
        break;
      case "--python-version": opts.pythonMinor = takeValue(i, a); i++; break;
      case "--skip-python": opts.skipPython = true; break;
      case "--clean": opts.clean = true; break;
      case "--help":
      case "-h":
        console.log("See the header comment of this file for usage.");
        process.exit(0);
        break;
      default:
        fail(`Unknown option: ${a}`);
    }
  }
  if (!["win32", "darwin"].includes(opts.platform)) {
    fail(`--platform must be win32 or darwin (got "${opts.platform}")`);
  }
  if (!["x64", "arm64", "universal"].includes(opts.arch)) {
    fail(`--arch must be x64, arm64 or universal (got "${opts.arch}")`);
  }
  if (opts.arch === "universal" && opts.platform !== "darwin") {
    fail(`--arch universal is only valid with --platform darwin`);
  }
  if (!PBS_PYTHON_VERSIONS[opts.pythonMinor]) {
    fail(
      `--python-version ${opts.pythonMinor} has no pinned python-build-standalone ` +
      `release. Pinned: ${Object.keys(PBS_PYTHON_VERSIONS).join(", ")}. ` +
      `Add a verified pin to PBS_PYTHON_VERSIONS before using it.`,
    );
  }
  // Default engine source: the local pinned checkout if present, else tarball.
  if (!opts.engineSrc) {
    const local = path.join(os.homedir(), ".hermes", "hermes-agent");
    if (fs.existsSync(path.join(local, "pyproject.toml"))) opts.engineSrc = local;
  }
  return opts;
}

const expandHome = (p) =>
  p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;

// ---------------------------------------------------------------------------
// PEP 508 marker evaluation (the subset uv export emits) — see header point 2.
// Throws on anything unrecognized: a marker we can't evaluate is a payload we
// can't trust.
// ---------------------------------------------------------------------------

const MARKER_VARS = new Set([
  "os_name", "sys_platform", "platform_system", "platform_machine",
  "platform_release", "platform_version", "implementation_name",
  "platform_python_implementation", "python_version", "python_full_version",
  "implementation_version", "extra",
]);

function tokenizeMarker(src) {
  const tokens = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === " " || c === "\t") { i++; continue; }
    if (c === "(" || c === ")") { tokens.push({ t: c }); i++; continue; }
    if (c === "'" || c === '"') {
      const end = src.indexOf(c, i + 1);
      if (end === -1) fail(`Unterminated string in marker: ${src}`);
      tokens.push({ t: "str", v: src.slice(i + 1, end) });
      i = end + 1;
      continue;
    }
    const two = src.slice(i, i + 2);
    if (["==", "!=", "<=", ">=", "~="].includes(two)) { tokens.push({ t: "op", v: two }); i += 2; continue; }
    if (c === "<" || c === ">") { tokens.push({ t: "op", v: c }); i++; continue; }
    const m = /^[A-Za-z_][A-Za-z0-9_.]*/.exec(src.slice(i));
    if (m) {
      const word = m[0];
      if (word === "and" || word === "or" || word === "in" || word === "not") {
        tokens.push({ t: "kw", v: word });
      } else {
        tokens.push({ t: "ident", v: word });
      }
      i += word.length;
      continue;
    }
    fail(`Cannot tokenize marker at "${src.slice(i)}" in: ${src}`);
  }
  return tokens;
}

/** Dotted-numeric version compare, lenient (enough for python_version markers). */
function cmpVersions(a, b) {
  const norm = (v) => v.split(".").map((part) => {
    const n = parseInt(part, 10);
    return Number.isNaN(n) ? 0 : n;
  });
  const pa = norm(a), pb = norm(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] ?? 0, y = pb[i] ?? 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

function evaluateMarker(markerSrc, env) {
  const tokens = tokenizeMarker(markerSrc);
  let pos = 0;
  const peek = () => tokens[pos];
  const next = () => tokens[pos++];
  const expect = (cond, what) => {
    if (!cond) fail(`Malformed marker (expected ${what}): ${markerSrc}`);
  };

  function parseValue() {
    const tok = next();
    expect(tok, "value");
    if (tok.t === "str") return { kind: "str", v: tok.v };
    if (tok.t === "ident") {
      if (!MARKER_VARS.has(tok.v)) fail(`Unknown marker variable "${tok.v}" in: ${markerSrc}`);
      if (!(tok.v in env)) fail(`Marker variable "${tok.v}" not provided for target env`);
      return { kind: "var", name: tok.v, v: env[tok.v] };
    }
    fail(`Expected a value in marker: ${markerSrc}`);
  }

  function parseComparison() {
    if (peek()?.t === "(") {
      next();
      const v = parseOr();
      expect(next()?.t === ")", "closing paren");
      return v;
    }
    const lhs = parseValue();
    let opTok = next();
    expect(opTok, "operator");
    let op;
    if (opTok.t === "op") op = opTok.v;
    else if (opTok.t === "kw" && opTok.v === "in") op = "in";
    else if (opTok.t === "kw" && opTok.v === "not") {
      const inTok = next();
      expect(inTok?.t === "kw" && inTok.v === "in", '"in" after "not"');
      op = "not in";
    } else fail(`Expected comparison operator in marker: ${markerSrc}`);
    const rhs = parseValue();

    const versionCmp =
      (lhs.kind === "var" && ["python_version", "python_full_version", "implementation_version"].includes(lhs.name)) ||
      (rhs.kind === "var" && ["python_version", "python_full_version", "implementation_version"].includes(rhs.name));
    const a = lhs.v, b = rhs.v;
    switch (op) {
      case "==": return versionCmp ? cmpVersions(a, b) === 0 : a === b;
      case "!=": return versionCmp ? cmpVersions(a, b) !== 0 : a !== b;
      case "<": return versionCmp ? cmpVersions(a, b) < 0 : a < b;
      case "<=": return versionCmp ? cmpVersions(a, b) <= 0 : a <= b;
      case ">": return versionCmp ? cmpVersions(a, b) > 0 : a > b;
      case ">=": return versionCmp ? cmpVersions(a, b) >= 0 : a >= b;
      case "in": return b.includes(a);
      case "not in": return !b.includes(a);
      default:
        fail(`Unsupported marker operator "${op}" in: ${markerSrc}`);
    }
  }

  function parseAnd() {
    let v = parseComparison();
    while (peek()?.t === "kw" && peek().v === "and") {
      next();
      const r = parseComparison();
      v = v && r;
    }
    return v;
  }
  function parseOr() {
    let v = parseAnd();
    while (peek()?.t === "kw" && peek().v === "or") {
      next();
      const r = parseAnd();
      v = v || r;
    }
    return v;
  }

  const result = parseOr();
  expect(pos === tokens.length, "end of marker");
  return result;
}

/** Marker environment for a target platform/arch/python. */
function markerEnv(platform, arch, pyFull) {
  const [maj, min] = pyFull.split(".");
  const common = {
    implementation_name: "cpython",
    platform_python_implementation: "CPython",
    python_version: `${maj}.${min}`,
    python_full_version: pyFull,
    implementation_version: pyFull,
    platform_release: "",
    platform_version: "",
    extra: "",
  };
  if (platform === "win32") {
    return {
      ...common,
      os_name: "nt",
      sys_platform: "win32",
      platform_system: "Windows",
      platform_machine: arch === "arm64" ? "ARM64" : "AMD64",
    };
  }
  return {
    ...common,
    os_name: "posix",
    sys_platform: "darwin",
    platform_system: "Darwin",
    platform_machine: arch === "arm64" ? "arm64" : "x86_64",
  };
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

/** Locate a host python that has pip (for `pip download`). */
function findHostPython() {
  const candidates =
    process.platform === "win32"
      ? [{ cmd: "py", base: ["-3"] }, { cmd: "python", base: [] }]
      : [{ cmd: "python3", base: [] }, { cmd: "python", base: [] }];
  if (process.env.JARVIS_VENDOR_PYTHON) {
    candidates.unshift({ cmd: process.env.JARVIS_VENDOR_PYTHON, base: [] });
  }
  for (const c of candidates) {
    const res = run(c.cmd, [...c.base, "-m", "pip", "--version"], {
      capture: true,
      allowFailure: true,
    });
    if (res.ok) {
      log(`  host pip: ${res.stdout.trim()}`);
      return c;
    }
  }
  fail(
    "No host python with pip found (tried python3/python" +
    (process.platform === "win32" ? "/py -3" : "") +
    "). pip is required for `pip download`. Set JARVIS_VENDOR_PYTHON to a python executable.",
  );
}

/**
 * The commit id a GitHub tag tarball was archived from. `git archive` output
 * starts with a pax global header whose `comment` record is the source commit
 * sha — reading it costs nothing and lets the manifest record WHICH commit a
 * checksummed tarball contained. Returns "" when absent/unparseable (the
 * sha256-vs-pin comparison is the authoritative gate, not this).
 */
function tarballEmbeddedCommit(tarball) {
  try {
    // Only the first tar block is needed; Z_SYNC_FLUSH tolerates the
    // deliberately truncated compressed input.
    const head = zlib.gunzipSync(fs.readFileSync(tarball).subarray(0, 65536), {
      finishFlush: zlib.constants.Z_SYNC_FLUSH,
    });
    if (head.length < 1024) return "";
    // Tar header: typeflag at byte 156; 'g' = pax global header, data at 512+.
    if (String.fromCharCode(head[156]) !== "g") return "";
    const pax = head.subarray(512, 1024).toString("utf8");
    const m = /(?:^|\n)\d+ comment=([0-9a-f]{40})\n/.exec(pax);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

/** Acquire the engine source tree (local dir or GitHub tag tarball).
 *  Returns { root, source } — `source` is the manifest provenance record,
 *  always shaped { kind, commit, tarballSha256, pinnedCommit }. */
async function acquireEngineSource(opts, tmpRoot, pin) {
  if (opts.engineSrc) {
    if (!fs.existsSync(path.join(opts.engineSrc, "pyproject.toml"))) {
      fail(`--engine-src ${opts.engineSrc} does not look like the engine (no pyproject.toml)`);
    }
    log(`  using local engine source: ${opts.engineSrc}`);
    const source = assessLocalSourceProvenance(opts.engineSrc, opts.tag, opts.allowDirtySource, pin);
    return { root: opts.engineSrc, source };
  }
  const url = ENGINE_REPO_TARBALL(opts.tag);
  const tarball = path.join(tmpRoot, "engine-src.tar.gz");
  await download(url, tarball);
  const tarballSha256 = sha256File(tarball);
  log(`  tarball sha256: ${tarballSha256}`);
  const embeddedCommit = tarballEmbeddedCommit(tarball);
  if (embeddedCommit) log(`  tarball archived from commit: ${embeddedCommit}`);

  // Pin gate (review r2 finding 9): hashing what we just downloaded only
  // proves self-consistency. Trust requires the hash to MATCH the checked-in
  // pin. A mismatch is a substituted/retagged artifact — hard fail, never a
  // downgrade to a warning.
  let kind;
  if (opts.tag !== pin.tag) {
    console.warn(
      `  WARNING: building tag ${opts.tag} but engine-pin.json pins ${pin.tag} — ` +
      `source.kind will be "tarball-unpinned" and verify --release will REFUSE this payload.`,
    );
    kind = "tarball-unpinned";
  } else if (pin.tarballSha256 === "") {
    console.warn(
      `  WARNING: engine-pin.json has an empty "tarballSha256" — the observed hash is\n` +
      `  RECORDED but NOT PINNED. source.kind will be "tarball-unpinned" and\n` +
      `  verify --release will REFUSE this payload until the pin is filled.\n` +
      `  ${PIN_FILL_HINT(opts.tag).split("\n").join("\n  ")}`,
    );
    kind = "tarball-unpinned";
  } else if (tarballSha256 !== pin.tarballSha256) {
    fail(
      `Downloaded ${opts.tag} tarball does NOT match the pinned root of trust:\n` +
      `    observed sha256: ${tarballSha256}\n` +
      `    pinned sha256:   ${pin.tarballSha256}  (hud/vendor/engine-pin.json)\n` +
      `  Either the tag was retargeted upstream, the download was tampered with, or\n` +
      `  the pin is stale after a deliberate engine bump. Investigate before touching the pin.`,
    );
  } else {
    kind = "tarball-sha256";
    log(`  provenance: tarball sha256 matches the checked-in pin — source.kind=tarball-sha256`);
  }
  if (embeddedCommit && pin.commit && embeddedCommit !== pin.commit) {
    fail(
      `Tarball for ${opts.tag} was archived from commit ${embeddedCommit}, but the pinned ` +
      `commit is ${pin.commit} (hud/vendor/engine-pin.json). Refusing to vendor it.`,
    );
  }
  const extractDir = path.join(tmpRoot, "engine-src");
  extractTarGz(tarball, extractDir);
  const entries = fs
    .readdirSync(extractDir, { withFileTypes: true })
    .filter((e) => e.isDirectory());
  if (entries.length !== 1) {
    fail(`Expected exactly one top-level dir in the ${opts.tag} tarball, got: ${entries.map((e) => e.name).join(", ")}`);
  }
  const root = path.join(extractDir, entries[0].name);
  if (!fs.existsSync(path.join(root, "pyproject.toml"))) {
    fail(`Tarball for ${opts.tag} has no pyproject.toml at its root — wrong tag or repo layout changed`);
  }
  if (!fs.existsSync(path.join(root, "uv.lock"))) {
    fail(`Tarball for ${opts.tag} has no uv.lock — cannot derive a frozen requirement set`);
  }
  return {
    root,
    source: { kind, commit: embeddedCommit, tarballSha256, pinnedCommit: pin.commit },
  };
}

/** Copy src -> dst applying the prune rules. */
function pruneCopy(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  const shouldSkip = (name, isTopLevel) => {
    if (PRUNE_ANY_DEPTH.has(name)) return true;
    if (PRUNE_ANY_DEPTH_SUFFIXES.some((suf) => name.endsWith(suf))) return true;
    if (isTopLevel && PRUNE_TOP_LEVEL.has(name)) return true;
    return false;
  };
  const walk = (from, to, topLevel) => {
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      if (shouldSkip(entry.name, topLevel)) continue;
      const s = path.join(from, entry.name);
      const d = path.join(to, entry.name);
      if (entry.isSymbolicLink()) {
        // A symlink in a payload we unpack on Windows is a landmine; the engine
        // tree has none in practice — refuse rather than ship one silently.
        fail(`Refusing to vendor symlink in engine source: ${s}`);
      } else if (entry.isDirectory()) {
        fs.mkdirSync(d, { recursive: true });
        walk(s, d, false);
      } else if (entry.isFile()) {
        fs.copyFileSync(s, d);
      }
    }
  };
  walk(src, dst, true);
}

/** uv export -> [{line, marker}] fully pinned entries. */
function exportLockedRequirements(stagingDir, extras) {
  const uvCheck = run("uv", ["--version"], { capture: true, allowFailure: true });
  if (!uvCheck.ok) {
    fail(
      "`uv` is required to export the engine's uv.lock and was not found on PATH.\n" +
      "  CI: use astral-sh/setup-uv.  Local: curl -LsSf https://astral.sh/uv/install.sh | sh\n" +
      "  (We deliberately do not hand-parse uv.lock — see the header comment.)",
    );
  }
  log(`  ${uvCheck.stdout.trim()}`);
  const args = [
    "export", "--frozen", "--no-dev", "--no-emit-project",
    "--no-hashes", "--no-header", "--format", "requirements-txt",
  ];
  for (const e of extras) args.push("--extra", e);
  const res = run("uv", args, { cwd: stagingDir, capture: true });
  const entries = [];
  for (const rawLine of res.stdout.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith("-")) continue;
    const semi = line.indexOf(";");
    const spec = (semi === -1 ? line : line.slice(0, semi)).trim();
    const marker = semi === -1 ? null : line.slice(semi + 1).trim();
    if (!/^[A-Za-z0-9._-]+(\[[A-Za-z0-9._,\s-]+\])?==\S+$/.test(spec)) {
      fail(
        `uv export produced a non-pinned or unrecognized requirement line: "${line}".\n` +
        `The wheelhouse must be fully pinned — refusing to continue.`,
      );
    }
    entries.push({ spec, marker });
  }
  if (entries.length === 0) fail("uv export produced zero requirements — something is very wrong");
  return entries;
}

/** Evaluate markers for one target env; returns sorted specs. */
function resolveForEnv(entries, env) {
  const out = [];
  for (const { spec, marker } of entries) {
    if (marker === null || evaluateMarker(marker, env)) out.push(spec);
  }
  return [...new Set(out)].sort();
}

/** pip download one fully-pinned, marker-free requirement file. */
function pipDownload(python, reqFile, destDir, { platformTags, pyMinor }) {
  const args = [
    ...python.base, "-m", "pip", "download",
    "-r", reqFile,
    "--dest", destDir,
    "--only-binary=:all:",
    "--no-deps",
    "--python-version", pyMinor,
    "--implementation", "cp",
    // Explicit ABI set: cpXY for compiled wheels, abi3 for stable-ABI wheels,
    // none for pure-python wheels. Passing a single --abi would silently
    // exclude py3-none-any wheels — i.e. most of the payload.
    "--abi", `cp${pyMinor.replace(".", "")}`,
    "--abi", "abi3",
    "--abi", "none",
    "--disable-pip-version-check",
  ];
  for (const tag of platformTags) args.push("--platform", tag);
  const res = run(python.cmd, args, { allowFailure: true });
  if (!res.ok) {
    fail(
      `pip download failed for platform tag(s) [${platformTags.join(", ")}].\n` +
      `At least one locked dependency has NO compatible wheel for the target — ` +
      `see pip's output above for the offending package. This build fails HERE ` +
      `so it can never fail on a customer machine. Never fall back to sdists.`,
    );
  }
}

/** Read the engine version from pyproject.toml (static field, exact-pinned repo). */
function readPyprojectVersion(stagingDir) {
  const text = fs.readFileSync(path.join(stagingDir, "pyproject.toml"), "utf8");
  const m = /^version\s*=\s*"([^"]+)"/m.exec(text);
  if (!m) fail("Could not read `version = \"...\"` from the engine's pyproject.toml");
  return m[1];
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------


/**
 * The Developer ID identity to sign the payload with, or undefined when this
 * machine has none (CI, a contributor's laptop). Never invents one: an
 * unsigned payload is a valid dev/CI artifact, and only the release gate cares.
 */
function macSigningIdentity() {
  const override = process.env.JARVIS_MAC_SIGN_IDENTITY?.trim();
  if (override) return override;
  if (process.env.CSC_IDENTITY_AUTO_DISCOVERY === "false") return undefined;
  const found = spawnSync("security", ["find-identity", "-v", "-p", "codesigning"], { encoding: "utf8" });
  if (found.status !== 0) return undefined;
  const match = /"(Developer ID Application: [^"]+)"/.exec(found.stdout ?? "");
  return match?.[1];
}

/** Every Mach-O under a tree, by CONTENT — never by extension or path. */
function machOFilesUnder(root) {
  const out = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const abs = path.join(dir, entry.name);
      // Symlinks are skipped: signing one signs its target, which we visit on
      // its own, and python's bin/ is three names for one file.
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) walk(abs);
      else if (entry.isFile() && isMachOFile(abs)) out.push(abs);
    }
  };
  walk(root);
  return out;
}

function isMachOFile(abs) {
  let fd;
  try {
    fd = fs.openSync(abs, "r");
    const head = Buffer.alloc(4);
    if (fs.readSync(fd, head, 0, 4, 0) < 4) return false;
    const magic = head.readUInt32BE(0);
    return magic === 0xfeedface || magic === 0xcefaedfe
      || magic === 0xfeedfacf || magic === 0xcffaedfe
      || magic === 0xcafebabe || magic === 0xbebafeca
      || magic === 0xcafebabf || magic === 0xbfbafeca;
  } catch {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

async function main() {
  const nodeMajor = parseInt(process.versions.node.split(".")[0], 10);
  if (nodeMajor < 20) fail(`Node >= 20 required (found ${process.versions.node})`);

  const opts = parseArgs(process.argv.slice(2));
  // Root of trust FIRST: a build that cannot read the checked-in pin must not
  // get far enough to produce anything that looks like a payload.
  const pin = loadEnginePin();
  log(`  engine pin: ${pin.tag} @ ${pin.commit || "<UNSET>"} (tarball ${pin.tarballSha256 ? pin.tarballSha256.slice(0, 12) + "..." : "<UNSET>"})`);
  const pyFull = PBS_PYTHON_VERSIONS[opts.pythonMinor];
  const outDir = opts.out;
  const wheelhouseDir = path.join(outDir, "wheelhouse");
  const engineOutDir = path.join(outDir, "hermes-agent");
  const pythonOutDir = path.join(outDir, "python");
  const manifestPath = path.join(outDir, "vendor-manifest.json");
  const digestsName = "digests.sha256";
  const digestsPath = path.join(outDir, digestsName);
  // universal mac = dual runtime (header point 8)
  const dualPython = opts.platform === "darwin" && opts.arch === "universal";
  const pythonArchs = opts.arch === "universal" ? ["arm64", "x64"] : [opts.arch];
  const pythonDirName = (arch) => (dualPython ? `python-${arch}` : "python");

  log(`build-vendor-payload: target ${opts.platform}/${opts.arch}, python ${pyFull}, tag ${opts.tag}`);
  log(`  extras: ${opts.extras.join(", ")}`);
  log(`  out: ${outDir}`);

  // Fail early on combos we cannot serve rather than half-building.
  if (opts.platform === "win32" && opts.arch !== "x64") {
    fail("win32 payloads are x64 only (the product ships no win-arm64 build)");
  }
  if (!opts.skipPython) {
    // universal expands to BOTH mac triples: PBS ships no universal2 runtime,
    // so a universal payload carries python-arm64/ AND python-x64/.
    for (const arch of pythonArchs) {
      if (!PBS_TRIPLES[`${opts.platform}/${arch}`]) {
        fail(
          `No pinned python-build-standalone build for ${opts.platform}/${arch} ` +
          `in release ${PBS_RELEASE} (win-arm64 does not exist there). ` +
          `Supported: ${Object.keys(PBS_TRIPLES).join(", ")}.`,
        );
      }
    }
  }

  step("0/7 Preparing output directory");
  if (opts.clean) {
    log(`  --clean: wiping ${outDir}`);
    rmrf(outDir);
  }
  // Always remove the manifest FIRST (kill the "payload complete" signal),
  // then the digest sidecar, then every managed sub-path, so a rebuild can
  // never mix runs. Both python layouts are wiped regardless of the current
  // target: switching universal -> arm64 must not leave a stale python-x64/.
  rmrf(manifestPath);
  rmrf(digestsPath);
  rmrf(engineOutDir);
  rmrf(wheelhouseDir);
  rmrf(pythonOutDir);
  rmrf(path.join(outDir, "python-arm64"));
  rmrf(path.join(outDir, "python-x64"));
  fs.mkdirSync(wheelhouseDir, { recursive: true });

  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "jarvis-vendor-"));
  try {
    step("1/7 Engine source");
    const { root: srcRoot, source: sourceInfo } = await acquireEngineSource(opts, tmpRoot, pin);
    const staging = path.join(tmpRoot, "staging");
    log(`  prune-copying to staging...`);
    pruneCopy(srcRoot, staging);
    if (!fs.existsSync(path.join(staging, "uv.lock"))) {
      fail(`Engine source at ${srcRoot} has no uv.lock — cannot derive a frozen requirement set`);
    }
    // The v1.5.0 patch series (never-import, replay excision, brain
    // transports) applies to the STAGING copy only — a local --engine-src
    // tree is never modified — and BEFORE the wheel build, so wheel, site
    // tree and shipped source all carry the patches. Provenance above was
    // assessed against the PRISTINE source; the manifest records the patch
    // digest alongside it. A patch that no longer applies fails the build.
    const patchesDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "vendor", "engine-patches");
    let enginePatches = null;
    if (fs.existsSync(path.join(patchesDir, "series"))) {
      const applied = applyEnginePatches(patchesDir, staging, { log });
      enginePatches = { count: applied.patches, filesAdded: applied.filesAdded, sha256: applied.digest };
      log(`  engine patches: series digest ${applied.digest.slice(0, 12)}… (${applied.patches} patches, ${applied.filesAdded} added files)`);
    } else {
      fail(`Engine patch series missing at ${patchesDir} — v1.5.0 payloads must carry the patched engine.`);
    }
    const prunedSize = dirSize(staging);
    log(`  pruned source tree: ${fmtBytes(prunedSize)}`);
    const engineVersion = readPyprojectVersion(staging);
    log(`  engine package version: ${engineVersion}`);

    step("2/7 Wheelhouse — export locked requirements");
    const entries = exportLockedRequirements(staging, opts.extras);
    log(`  ${entries.length} locked entries from uv.lock`);

    // Resolve markers per target env. "universal" = union of both mac arches,
    // and one pip pass per arch so each arch's wheel set is complete.
    const targetArchs = opts.arch === "universal" ? ["arm64", "x64"] : [opts.arch];
    const perArch = targetArchs.map((arch) => ({
      arch,
      specs: resolveForEnv(entries, markerEnv(opts.platform, arch, pyFull)),
    }));
    const unionSpecs = [...new Set(perArch.flatMap((p) => p.specs))].sort();
    const reqFile = path.join(wheelhouseDir, "requirements.txt");
    fs.writeFileSync(reqFile, unionSpecs.join("\n") + "\n", "utf8");
    log(`  wrote ${unionSpecs.length} pinned requirements -> ${reqFile}`);

    step("3/7 Wheelhouse — download wheels (binary only, no sdists, no deps)");
    const python = findHostPython();
    for (const { arch, specs } of perArch) {
      const platformTags =
        opts.platform === "win32"
          ? ["win_amd64"] // win/x64 is the only supported win combo (checked above)
          : [MAC_PLATFORM_TAGS[arch]]; // pip expands to universal2 + older majors
      let file = reqFile;
      if (specs.length !== unionSpecs.length) {
        // Arch-specific subset (universal builds): download only what this
        // arch actually needs, so a missing wheel fails for the right reason.
        file = path.join(tmpRoot, `requirements-${arch}.txt`);
        fs.writeFileSync(file, specs.join("\n") + "\n", "utf8");
      }
      log(`  pass: ${opts.platform}/${arch} (${specs.length} pins)`);
      pipDownload(python, file, wheelhouseDir, { platformTags, pyMinor: opts.pythonMinor });
    }

    step("4/7 Engine wheel");
    // uv build --wheel: honors the engine's pinned build backend via build
    // isolation and picks an interpreter satisfying requires-python even if
    // the host python is outside the engine's supported range (see header).
    const beforeWheels = new Set(fs.readdirSync(wheelhouseDir).filter((f) => f.endsWith(".whl")));
    run("uv", ["build", "--wheel", "--out-dir", wheelhouseDir, staging]);
    const newWheels = fs
      .readdirSync(wheelhouseDir)
      .filter((f) => f.endsWith(".whl") && !beforeWheels.has(f));
    if (newWheels.length !== 1) {
      fail(
        `Expected exactly one new wheel from the engine build, got: ` +
        `[${newWheels.join(", ")}]. Refusing to guess.`,
      );
    }
    const engineWheel = newWheels[0];
    log(`  engine wheel: ${engineWheel}`);
    // Scrub build junk the backend may have left in the staging tree so it
    // never ships inside hermes-agent/.
    for (const junk of ["build", "dist", ".eggs"]) rmrf(path.join(staging, junk));
    for (const entry of fs.readdirSync(staging)) {
      if (entry.endsWith(".egg-info")) rmrf(path.join(staging, entry));
    }

    // Staging is final now — move it into the payload.
    log(`  copying pruned engine tree -> ${engineOutDir}`);
    fs.cpSync(staging, engineOutDir, { recursive: true });

    step("5/7 Python runtime");
    let pythonInfo = null;
    if (opts.skipPython) {
      log("  --skip-python: SKIPPED. This payload is a DEV artifact — it is NOT");
      log("  shippable and must never reach electron-builder for a release.");
    } else {
      const variants = {};
      const prunedAll = [];
      for (const arch of pythonArchs) {
        const dirName = pythonDirName(arch);
        const runtimeDir = path.join(outDir, dirName);
        const triple = PBS_TRIPLES[`${opts.platform}/${arch}`];
        const url = pbsUrl(pyFull, triple);
        const tarball = path.join(tmpRoot, `python-${arch}.tar.gz`);
        await download(url, tarball);
        // install_only tarballs extract to a top-level `python/` directory.
        if (dualPython) {
          // Extract in tmp, then move under the arch-suffixed name, so the two
          // runtimes can never be extracted on top of each other.
          const extractTmp = path.join(tmpRoot, `pbs-${arch}`);
          extractTarGz(tarball, extractTmp);
          const extracted = path.join(extractTmp, "python");
          if (!fs.existsSync(extracted)) {
            fail(`PBS tarball for ${triple} did not contain a top-level python/ directory`);
          }
          moveDir(extracted, runtimeDir);
        } else {
          extractTarGz(tarball, outDir);
        }
        const exeRel = `${dirName}/${opts.platform === "win32" ? "python.exe" : "bin/python3"}`;
        const exeAbs = path.join(outDir, ...exeRel.split("/"));
        if (!fs.existsSync(exeAbs)) {
          fail(`Extracted python runtime is missing its interpreter at ${exeAbs}`);
        }

        // Trim the runtime BEFORE the execution checks, so the checks validate
        // what actually ships rather than what was downloaded.
        const beforePrune = dirSize(runtimeDir);
        const pruned = prunePythonRuntime(runtimeDir, opts.platform, opts.pythonMinor, dirName);
        const afterPrune = dirSize(runtimeDir);
        log(`  [${dirName}] pruned ${pruned.length} item(s): ${fmtBytes(beforePrune - afterPrune)} saved ` +
            `(${fmtBytes(beforePrune)} -> ${fmtBytes(afterPrune)})`);
        for (const entry of pruned) log(`    - ${entry}`);
        prunedAll.push(...pruned);

        if (opts.platform !== process.platform) {
          log(`  NOTE: target platform (${opts.platform}) != host (${process.platform});`);
          log(`  skipping the \`python -V\` / venv execution checks — CI on the target OS must cover them.`);
        } else if (arch === process.arch) {
          const v = run(exeAbs, ["-V"], { capture: true });
          const got = (v.stdout + v.stderr).trim();
          log(`  [${dirName}] interpreter check: ${got}`);
          if (!got.includes(pyFull)) {
            fail(`Bundled interpreter reports "${got}", expected Python ${pyFull}`);
          }
          // The prune's real gate: the install path is `python -m venv` +
          // `pip install`, so proving THOSE still work is what matters. ensurepip
          // and venv must survive every prune list — this catches an over-eager
          // one at BUILD time instead of on a customer machine.
          const probe = path.join(tmpRoot, "venv-probe");
          rmrf(probe);
          run(exeAbs, ["-m", "venv", probe]);
          const probePython = opts.platform === "win32"
            ? path.join(probe, "Scripts", "python.exe")
            : path.join(probe, "bin", "python");
          run(probePython, ["-m", "pip", "--version"], { capture: true });
          // sqlite3 and ssl are load-bearing for the engine (history DB, HTTPS).
          run(probePython, ["-c", "import sqlite3, ssl, venv, ensurepip, zlib, ctypes"], { capture: true });
          rmrf(probe);
          log(`  [${dirName}] post-prune check: venv + pip + sqlite3/ssl/ctypes import OK`);
        } else if (opts.platform === "darwin" && arch === "x64" && process.arch === "arm64") {
          // The x64 half of a dual payload on an Apple Silicon builder: Rosetta
          // can execute it if installed. Try honestly; never pretend.
          const rosetta = run("arch", ["-x86_64", exeAbs, "-V"], { capture: true, allowFailure: true });
          const got = (rosetta.stdout + rosetta.stderr).trim();
          if (rosetta.ok) {
            log(`  [${dirName}] interpreter check (via Rosetta): ${got}`);
            if (!got.includes(pyFull)) {
              fail(`Bundled x64 interpreter reports "${got}", expected Python ${pyFull}`);
            }
          } else {
            log(`  [${dirName}] CI-PENDING: cannot execute the x64 interpreter on this host ` +
                `(\`arch -x86_64\` failed${got ? `: ${got.split("\n")[0]}` : ""} — Rosetta 2 not installed?).`);
            log("  An x86_64 mac CI job must run this payload's execution checks before release.");
          }
        } else {
          log(`  [${dirName}] CI-PENDING: host is ${process.platform}/${process.arch}; ` +
              `the ${arch} interpreter cannot execute here. CI on ${arch} must cover it.`);
        }
        variants[arch] = exeRel;
      }
      pythonInfo = {
        kind: "python-build-standalone",
        version: pyFull,
        // Backwards-compatible pointer: arm64 for dual payloads. The runtime
        // installer selects variants[process.arch] ?? python.executable.
        executable: dualPython ? variants.arm64 : variants[pythonArchs[0]],
        pruned: prunedAll,
        ...(dualPython ? { variants } : {}),
      };
    }

    const wheelFiles = fs.readdirSync(wheelhouseDir).filter((f) => f.endsWith(".whl"));
    // -- SITE trees (macOS) -------------------------------------------------
    //
    // Apple's notary service opens every archive inside the app, .whl included,
    // and refuses any Mach-O it finds that is not signed by US with a secure
    // timestamp. Wheels from PyPI never are — they are unsigned or ad-hoc
    // signed — so a mac payload cannot ship a wheelhouse at all. It ships the
    // libraries ALREADY INSTALLED instead: every extension module becomes a
    // loose file that electron-builder signs with the app.
    //
    // Windows keeps the wheelhouse and its first-run pip: nothing there
    // notarizes, and changing a working install path for no reason is how a
    // release grows failures nobody asked for.
    const siteDirs = {};
    if (opts.platform === "darwin") {
      step("5b/8 Site trees — install the libraries, once, at build time");
      const sitePython = path.join(outDir, pythonDirName(pythonArchs[0]), "bin", "python3");
      if (!fs.existsSync(sitePython)) {
        fail(`Cannot build site trees: no interpreter at ${sitePython} (a --skip-python payload cannot be a site payload).`);
      }
      for (const { arch, specs } of perArch) {
        const siteDir = path.join(outDir, `site-${arch}`);
        rmrf(siteDir);
        const archReq = path.join(tmpRoot, `site-requirements-${arch}.txt`);
        fs.writeFileSync(archReq, specs.join("\n") + "\n", "utf8");
        const common = [
          "-m", "pip", "install",
          // The same structural promise the runtime makes: never the network.
          "--no-index",
          "--find-links", wheelhouseDir,
          "--target", siteDir,
          "--disable-pip-version-check",
          "--no-input",
          // .pyc files embed absolute paths and build timestamps, which would
          // make the payload differ between two identical builds. The engine
          // compiles what it needs on first import instead.
          "--no-compile",
          // Cross-installing x64 from an arm64 builder REQUIRES this, and
          // passing it for both arches keeps the two passes identical rather
          // than subtly different.
          "--only-binary=:all:",
          "--platform", MAC_PLATFORM_TAGS[arch],
          "--python-version", opts.pythonMinor,
        ];
        log(`  installing ${specs.length} libraries -> site-${arch}`);
        run(sitePython, [...common, "--requirement", archReq]);
        log(`  installing the engine package -> site-${arch}`);
        run(sitePython, [...common, "--no-deps", path.join(wheelhouseDir, engineWheel)]);
        siteDirs[arch] = `site-${arch}`;
      }
      // The audit trail the wheelhouse used to carry: exactly what went in.
      fs.copyFileSync(reqFile, path.join(outDir, "requirements.txt"));
      log(`  dropping the wheelhouse (${wheelFiles.length} wheels): a mac payload installs nothing at first run`);
      rmrf(wheelhouseDir);
    }

    // -- SIGN, then hash ----------------------------------------------------
    //
    // Order is the whole point. macOS signing REWRITES a Mach-O, so a payload
    // hashed before signing can never match afterwards — that mismatch is what
    // forced the first cut to carry a "these ten files are excused" map. Doing
    // it here, before the digests are computed, means the digest chain covers
    // the bytes we actually ship: plain hash verification, no excuses, nothing
    // to keep in step with a future payload.
    //
    // electron-builder is told to leave engine-vendor alone (mac.signIgnore in
    // hud/package.json). If that ever stops working, the packed verifier fails
    // on the very next build with a CONTENT MISMATCH naming the file — loudly,
    // in our own gate, not quietly at a customer.
    if (opts.platform === "darwin") {
      step("5c/8 Signing the payload's binaries");
      const identity = macSigningIdentity();
      if (identity === undefined) {
        // CI builds unsigned on purpose (CSC_IDENTITY_AUTO_DISCOVERY=false) and
        // must keep working: an unsigned payload is hashed exactly as before,
        // and only a RELEASE build refuses it (verify-vendor-payload --release).
        log("  no Developer ID identity in the keychain — leaving the payload unsigned (CI/dev build)");
      } else {
        const binaries = machOFilesUnder(outDir);
        log(`  signing ${binaries.length} Mach-O files as ${identity}`);
        for (const abs of binaries) {
          run("codesign", [
            "--force",
            // A secure timestamp is not optional: Apple's notary rejects a
            // signature without one, which is half of why the wheels failed.
            "--timestamp",
            "--options", "runtime",
            "--entitlements", path.join(HUD_DIR, "build", "entitlements.mac.plist"),
            "--sign", identity,
            abs,
          ], { quiet: true });
        }
        log(`  signed ${binaries.length} files`);
      }
    }

    step("6/7 Content digests");
    // Belt-and-braces: every pinned requirement must have a wheel present.
    const wheelNames = new Set(
      wheelFiles.map((f) => f.split("-")[0].toLowerCase().replace(/_/g, "-")),
    );
    const missing = unionSpecs.filter((spec) => {
      const name = spec.split("==")[0].replace(/\[.*\]/, "").toLowerCase().replace(/[._]/g, "-");
      return !wheelNames.has(name);
    });
    if (missing.length > 0) {
      fail(
        `Wheelhouse is INCOMPLETE — no wheel present for: ${missing.join(", ")}.\n` +
        `Refusing to write the manifest.`,
      );
    }

    // The payload is fully assembled — hash EVERY file into digests.sha256
    // (format is a FIXED contract with the verifier and the runtime installer:
    // `<sha256 hex>  <posix/relative/path>`, sorted by path, LF, excluding
    // only the manifest and the digest file themselves). Written BEFORE the
    // manifest so the manifest can pin its hash: a payload with a manifest
    // always has a complete, self-consistent digest chain.
    // Tool droppings are removed BEFORE hashing, not excluded from the hash:
    // an unlisted file on disk fails the verifier ("extras are as dangerous as
    // edits"), so the payload has to be free of them, not merely quiet about
    // them. `uv` writes `wheelhouse/.gitignore` containing `*` while it manages
    // that directory, which made the shipped payload differ from a clean
    // vendor:mac run by exactly one file (4986 vs 4985) — a payload that is not
    // byte-reproducible cannot be audited against its own build.
    pruneBuildArtifacts(outDir);
    // The antivirus bait goes out BEFORE the files are hashed, so the digest
    // list expects their absence and the runtime verifier never looks for a
    // file this payload deliberately does not ship (v1.5.1, Leg 2 — three
    // paying customers blocked on one quarantined ARM launcher stub).
    const strippedStubs = stripDistlibStubs(outDir, opts.platform);
    for (const relative of strippedStubs) log(`  stripped launcher stub ${relative}`);
    log(`  stripped ${strippedStubs.length} unusable distlib launcher stub(s) for ${opts.platform}`);
    const digests = writeDigestFile(outDir, fail);
    log(`  hashed ${digests.fileCount} files, ${fmtBytes(digests.bytes)} -> ${digestsName}`);
    log(`  ${digestsName} sha256: ${digests.sha256}`);

    step("7/7 Manifest");
    const srcSize = dirSize(engineOutDir);
    const wheelSize = fs.existsSync(wheelhouseDir) ? dirSize(wheelhouseDir) : 0;
    const siteSize = Object.keys(siteDirs)
      .reduce((sum, arch) => sum + dirSize(path.join(outDir, siteDirs[arch])), 0);
    const pySize = pythonInfo
      ? pythonArchs.reduce((sum, arch) => sum + dirSize(path.join(outDir, pythonDirName(arch))), 0)
      : 0;
    const totalSize = srcSize + wheelSize + siteSize + pySize;

    const manifest = {
      schema: 1,
      engineTag: opts.tag,
      engineVersion: opts.tag.replace(/^v/, ""),
      platform: opts.platform,
      arch: opts.arch,
      // Provenance: how the source tree was obtained (header point 7).
      // "local-dirty" marks a dev-only payload the release gate refuses.
      source: sourceInfo,
      // Which JARVIS engine patch series produced this payload (v1.5.0).
      enginePatches,
      // The launcher stubs this platform can never use, removed at vendoring
      // (v1.5.1) — recorded so the trim is auditable from a shipped installer.
      strippedStubs,
      python: pythonInfo ?? {
        kind: "python-build-standalone",
        version: pyFull,
        executable: opts.platform === "win32" ? "python/python.exe" : "python/bin/python3",
      },
      // Exactly one install shape, never both (setup/src/vendor.ts asserts it).
      ...(opts.platform === "darwin"
        ? { site: siteDirs }
        : { engineWheel: `wheelhouse/${engineWheel}`, requirements: "wheelhouse/requirements.txt" }),
      extras: opts.extras,
      wheelCount: wheelFiles.length,
      // Root of trust: manifest -> digests.sha256 -> every payload file.
      digests: {
        algorithm: "sha256",
        file: digestsName,
        sha256: digests.sha256,
        fileCount: digests.fileCount,
      },
      sizeBytes: totalSize,
      builtAt: new Date().toISOString(),
    };
    // Written LAST, atomically: presence of this file = payload complete.
    const tmpManifest = manifestPath + ".tmp";
    fs.writeFileSync(tmpManifest, JSON.stringify(manifest, null, 2) + "\n", "utf8");
    fs.renameSync(tmpManifest, manifestPath);
    if (opts.skipPython) {
      log("  WARNING: manifest written for a --skip-python DEV payload (no python/).");
    }

    log("\n================ payload report ================");
    log(`  engine source   ${fmtBytes(srcSize).padStart(10)}   ${engineOutDir}`);
    log(Object.keys(siteDirs).length > 0
      ? `  site trees      ${fmtBytes(siteSize).padStart(10)}   ${Object.keys(siteDirs).join(", ")} (installed from ${wheelFiles.length} wheels, wheelhouse dropped)`
      : `  wheelhouse      ${fmtBytes(wheelSize).padStart(10)}   ${wheelFiles.length} wheels`);
    log(`  python runtime  ${(pythonInfo ? fmtBytes(pySize) : "SKIPPED").padStart(10)}`);
    log(`  TOTAL           ${fmtBytes(totalSize).padStart(10)}`);
    log(`  manifest        ${manifestPath}`);
    log("================================================");
  } finally {
    rmrf(tmpRoot);
  }
}

main().catch((err) => {
  console.error(`\nBUILD FAILED: ${err instanceof BuildError ? err.message : err?.stack ?? err}`);
  process.exit(1);
});
