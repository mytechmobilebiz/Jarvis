#!/usr/bin/env node
/**
 * Stage the bundled voice payload at BUILD time.
 *
 * Appliance doctrine: a customer's machine never downloads a model. Everything
 * the voice path needs is pinned here by URL and SHA-256, fetched on the build
 * machine, verified, pruned, and written into hud/voice-assets/ — which
 * electron-builder copies into the installer as resources/voice/.
 *
 *   node hud/scripts/fetch-voice-assets.mjs            # this platform
 *   node hud/scripts/fetch-voice-assets.mjs --check    # verify a staged tree
 *   node hud/scripts/fetch-voice-assets.mjs --clean
 *
 * Downloads are cached in hud/.voice-cache so a rebuild costs nothing. Every
 * cached file is re-hashed before use, so a corrupt or tampered cache entry is
 * re-fetched rather than shipped.
 *
 * Why these artifacts, in short (full reasoning in spec/v140/voice-codex-report.md):
 *  - whisper-cli is COMPILED from the pinned source on both platforms: upstream
 *    publishes no macOS CLI at all, and its prebuilt Windows binaries import
 *    the Visual C++ runtime, which a clean Windows machine does not have.
 *  - sherpa-onnx's Windows build is the MT (statically linked CRT) variant, so
 *    no Visual C++ redistributable has to be chased on a buyer's machine. Both
 *    are proved CRT-free by reading their PE import tables at stage time.
 *  - espeak-ng is GPL-3.0 and is linked INTO sherpa's own executable upstream.
 *    We invoke that executable as a separate child process and ship its source
 *    link and license text; no GPL code is linked into JARVIS itself.
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createWriteStream, promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";
import { crtImports } from "./lib/pe-imports.mjs";

const HUD_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CACHE_DIR = path.join(HUD_ROOT, ".voice-cache");
const OUT_DIR = path.join(HUD_ROOT, "voice-assets");

const WHISPER_VERSION = "1.9.2";
const SHERPA_VERSION = "1.13.5";
const KOKORO_BUNDLE = "kokoro-multi-lang-v1_0";

/** bm_george / bm_fable speaker ids inside the bundle's voices.bin. Read out of
 *  the model's own speaker2id metadata; asserted below so a bundle swap that
 *  renumbers the voices fails the build instead of shipping a stranger. */
const VOICE_IDS = { bm_george: 26, bm_fable: 25 };

const DOWNLOADS = {
  whisperModel: {
    url: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q5_1.bin",
    sha256: "4baf70dd0d7c4247ba2b81fafd9c01005ac77c2f9ef064e00dcf195d0e2fdd2f",
    bytes: 59_721_011,
    file: "ggml-base.en-q5_1.bin",
  },
  whisperWin: {
    url: `https://github.com/ggml-org/whisper.cpp/releases/download/v${WHISPER_VERSION}/whisper-bin-x64.zip`,
    sha256: "49dcc16de826f20bd53d44f947a1ae49dfa81f86cad67a64d80820cb192d674a",
    bytes: 8_194_445,
    file: "whisper-bin-x64.zip",
  },
  whisperSrc: {
    url: `https://github.com/ggml-org/whisper.cpp/archive/refs/tags/v${WHISPER_VERSION}.tar.gz`,
    sha256: "a6abd064fcca8b85e794d205abf328c522e9451db43a3eadc178b883b7d0e9cd",
    bytes: 9_622_263,
    file: `whisper.cpp-${WHISPER_VERSION}.tar.gz`,
  },
  sherpaWin: {
    url: `https://github.com/k2-fsa/sherpa-onnx/releases/download/v${SHERPA_VERSION}/sherpa-onnx-v${SHERPA_VERSION}-win-x64-shared-MT-Release.tar.bz2`,
    sha256: "70b7193e40ce5c393a218c6383069a64cf6479962ae87cdc19373b8b57e3f285",
    bytes: 24_499_217,
    file: "sherpa-win-x64.tar.bz2",
  },
  sherpaMac: {
    url: `https://github.com/k2-fsa/sherpa-onnx/releases/download/v${SHERPA_VERSION}/sherpa-onnx-v${SHERPA_VERSION}-osx-universal2-shared.tar.bz2`,
    sha256: "1a89631e5975519dbdaf8e68ef5c8687693117e8b4155480c346601b1c3c6156",
    bytes: 42_840_577,
    file: "sherpa-osx-universal2.tar.bz2",
  },
  kokoro: {
    url: `https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${KOKORO_BUNDLE}.tar.bz2`,
    sha256: "c5f7e2d2caf082bc1d20fb70334a61d99d20b484500aad32e7cf84c128ea3298",
    bytes: 349_906_910,
    file: `${KOKORO_BUNDLE}.tar.bz2`,
  },
};

const LICENSES = [
  {
    out: "whisper.cpp-MIT.txt",
    url: `https://raw.githubusercontent.com/ggml-org/whisper.cpp/v${WHISPER_VERSION}/LICENSE`,
    sha256: "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d",
  },
  {
    out: "openai-whisper-MIT.txt",
    url: "https://raw.githubusercontent.com/openai/whisper/v20250625/LICENSE",
    sha256: "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d",
  },
  {
    out: "sherpa-onnx-Apache-2.0.txt",
    url: `https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v${SHERPA_VERSION}/LICENSE`,
    sha256: "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
  },
  {
    out: "onnxruntime-MIT.txt",
    url: "https://raw.githubusercontent.com/microsoft/onnxruntime/v1.17.1/LICENSE",
    sha256: "2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c",
  },
  {
    out: "espeak-ng-GPL-3.0.txt",
    url: "https://raw.githubusercontent.com/espeak-ng/espeak-ng/1.52.0/COPYING",
    sha256: "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
  },
];

/**
 * Windows whisper-cli is COMPILED here, not taken from the upstream release.
 *
 * The pinned `whisper-bin-x64.zip` is dynamically linked against the Visual
 * C++ runtime: its import table names MSVCP140.dll, VCRUNTIME140.dll and
 * VCRUNTIME140_1.dll. On a clean Windows 10/11 machine that has never had
 * Visual Studio installed, that binary fails to load before `main()` runs —
 * and CI would never notice, because the GitHub windows-latest image ships
 * the redistributable. Bundling Microsoft's redistributable DLLs instead was
 * considered and rejected: their download URL is evergreen, so they cannot be
 * pinned by hash the way every other byte in this payload is.
 *
 * So Windows follows the same rule sherpa-onnx already does (its MT build is
 * verified CRT-free): link the CRT statically.
 *
 * The cost is the ggml runtime CPU-dispatch DLLs, which require a shared
 * build. GGML_NATIVE=OFF therefore targets the x86-64 baseline: slower than a
 * dispatched AVX2 path on a modern CPU, but it CANNOT require an instruction
 * set the customer's machine lacks — and measured STT headroom is about 7x
 * against the latency budget, whereas a missing DLL is a hard failure.
 */
const WHISPER_WIN_CMAKE_FLAGS = [
  "-DCMAKE_BUILD_TYPE=Release",
  "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded",
  "-DCMAKE_POLICY_DEFAULT_CMP0091=NEW",
  "-DBUILD_SHARED_LIBS=OFF",
  "-DWHISPER_BUILD_TESTS=OFF",
  "-DWHISPER_BUILD_SERVER=OFF",
  "-DWHISPER_BUILD_EXAMPLES=ON",
  "-DGGML_BACKEND_DL=OFF",
  "-DGGML_NATIVE=OFF",
  "-DGGML_AVX2=OFF",
  "-DGGML_FMA=OFF",
  "-DGGML_F16C=OFF",
];

/**
 * Single source of truth for the macOS whisper-cli build, so the flags in the
 * manifest are provably the flags that were used rather than a copy that can
 * drift. Metal is off deliberately: base.en on a five-second utterance is a
 * third of a second on CPU, and a Metal build would drag a shader library and
 * a GPU dependency into a signed child process for no measurable gain.
 */
const WHISPER_CMAKE_FLAGS = [
  "-DCMAKE_BUILD_TYPE=Release",
  "-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64",
  "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0",
  "-DWHISPER_BUILD_TESTS=OFF",
  "-DWHISPER_BUILD_SERVER=OFF",
  "-DWHISPER_BUILD_EXAMPLES=ON",
  "-DBUILD_SHARED_LIBS=OFF",
  "-DGGML_METAL=OFF",
  "-DGGML_ACCELERATE=ON",
];

const LICENSE_OF = {
  "models/whisper/": "MIT (OpenAI Whisper weights, ggml conversion)",
  "models/kokoro/espeak-ng-data/": "GPL-3.0 (eSpeak NG data)",
  "models/kokoro/": "Apache-2.0 (Kokoro-82M v1.0)",
  "bin/win32/whisper": "MIT (whisper.cpp, compiled here from pinned source)",
  "bin/win32/ggml": "MIT (whisper.cpp / ggml)",
  "bin/win32/sherpa": "GPL-3.0 (combined work: Apache-2.0 sherpa-onnx statically linked with GPL-3.0 eSpeak NG)",
  "bin/win32/onnxruntime": "MIT (ONNX Runtime)",
  "bin/darwin/whisper": "MIT (whisper.cpp)",
  "bin/darwin/sherpa": "GPL-3.0 (combined work: Apache-2.0 sherpa-onnx statically linked with GPL-3.0 eSpeak NG)",
  "bin/darwin/libonnxruntime": "MIT (ONNX Runtime)",
  "licenses/": "see file",
};

const SOURCE_OF = {
  "models/whisper/": "https://huggingface.co/ggerganov/whisper.cpp",
  "models/kokoro/": `https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${KOKORO_BUNDLE}.tar.bz2`,
  "bin/win32/sherpa": DOWNLOADS.sherpaWin.url,
  "bin/win32/onnxruntime": DOWNLOADS.sherpaWin.url,
  // Compiled from the pinned SOURCE tarball, not the prebuilt release.
  "bin/win32/": DOWNLOADS.whisperSrc.url,
  "bin/darwin/sherpa": DOWNLOADS.sherpaMac.url,
  "bin/darwin/libonnxruntime": DOWNLOADS.sherpaMac.url,
  "bin/darwin/": DOWNLOADS.whisperSrc.url,
  "licenses/": "upstream project repositories, pinned by tag",
};

async function main() {
  const args = process.argv.slice(2);
  assertKnownFlags(args);
  const platform = readFlag(args, "--platform") ?? process.platform;
  const outDir = readFlag(args, "--out") ?? OUT_DIR;

  if (args.includes("--clean")) {
    await fs.rm(outDir, { recursive: true, force: true });
    console.log(`Removed ${rel(outDir)}`);
    return;
  }
  if (args.includes("--check")) {
    await check(outDir, platform);
    return;
  }
  if (platform !== "win32" && platform !== "darwin") {
    throw new Error(`Voice assets are only staged for win32 and darwin, not ${platform}.`);
  }

  await fs.mkdir(CACHE_DIR, { recursive: true });
  await fs.rm(outDir, { recursive: true, force: true });
  await fs.mkdir(outDir, { recursive: true });

  const work = await fs.mkdtemp(path.join(os.tmpdir(), "jarvis-voice-"));
  try {
    await stageModels(outDir, work);
    if (platform === "win32") await stageWindowsBinaries(outDir, work);
    else await stageMacBinaries(outDir, work, args.includes("--no-build"));
    await stageLicenses(outDir);
    // Order matters: NOTICES.md must exist BEFORE the manifest is written, or
    // it is absent from the asset list and the runtime extras check correctly
    // reports the payload as tampered with. Then the anchor, which digests the
    // finished manifest.
    await writeNotices(outDir, platform);
    const manifest = await writeManifest(outDir, platform);
    await writeTrustAnchor(outDir, platform);
    console.log(`\nStaged ${manifest.assets.length} files (${mb(await treeBytes(outDir))} MB) into ${rel(outDir)}`);
  } finally {
    await fs.rm(work, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------- downloads

async function fetchPinned(spec) {
  const cached = path.join(CACHE_DIR, spec.file);
  if (await hashOf(cached) === spec.sha256) return cached;

  console.log(`  downloading ${spec.file} (${mb(spec.bytes)} MB)`);
  const response = await fetch(spec.url, { redirect: "follow" });
  if (!response.ok || response.body === null) {
    throw new Error(`Download failed for ${spec.url}: HTTP ${response.status}`);
  }
  const tmp = `${cached}.part`;
  await pipeline(Readable.fromWeb(response.body), createWriteStream(tmp));
  const actual = await hashOf(tmp);
  if (actual !== spec.sha256) {
    await fs.rm(tmp, { force: true });
    throw new Error(`SHA-256 mismatch for ${spec.url}\n  expected ${spec.sha256}\n  actual   ${actual}`);
  }
  await fs.rename(tmp, cached);
  return cached;
}

/**
 * bsdtar handles .tar.gz, .tar.bz2 AND .zip, and ships in the box on macOS and
 * on Windows 10 1803+. One extractor for every archive here means no zip/bz2
 * dependency in package.json.
 */
function extract(archive, into) {
  execFileSync("tar", ["-xf", archive, "-C", into], { stdio: ["ignore", "ignore", "inherit"] });
}

// ------------------------------------------------------------------- models

async function stageModels(outDir, work) {
  console.log("Models");
  const whisper = await fetchPinned(DOWNLOADS.whisperModel);
  await copyInto(whisper, path.join(outDir, "models/whisper/ggml-base.en-q5_1.bin"));

  const kokoroArchive = await fetchPinned(DOWNLOADS.kokoro);
  extract(kokoroArchive, work);
  const src = path.join(work, KOKORO_BUNDLE);
  await assertSpeakerIds(path.join(src, "model.onnx"));

  const dst = path.join(outDir, "models/kokoro");
  for (const file of ["model.onnx", "voices.bin", "tokens.txt", "lexicon-gb-en.txt"]) {
    await copyInto(path.join(src, file), path.join(dst, file));
  }

  // English is forced, so the Chinese lexicon, the jieba dict/ tree and the
  // zh normalisation FSTs (about 17 MB) never get read. The espeak-ng data
  // directory is pruned to the shared tables plus en_dict for the same reason.
  const espeakSrc = path.join(src, "espeak-ng-data");
  const espeakDst = path.join(dst, "espeak-ng-data");
  for (const entry of await fs.readdir(espeakSrc, { withFileTypes: true })) {
    if (entry.name.endsWith("_dict") && entry.name !== "en_dict") continue;
    await copyRecursive(path.join(espeakSrc, entry.name), path.join(espeakDst, entry.name));
  }
  await copyInto(path.join(src, "LICENSE"), path.join(outDir, "licenses/kokoro-Apache-2.0.txt"));
}

/**
 * The speaker id is an integer index into voices.bin — nothing at runtime can
 * tell you it picked the wrong person, it just sounds like someone else. So the
 * mapping is asserted against the model's own metadata at build time.
 */
async function assertSpeakerIds(modelPath) {
  const blob = await fs.readFile(modelPath);
  const marker = blob.lastIndexOf(Buffer.from("speaker2id"));
  if (marker === -1) throw new Error("Kokoro model carries no speaker2id metadata; refusing to guess voice ids.");
  const meta = blob.subarray(marker, marker + 4_000).toString("utf8");
  for (const [name, id] of Object.entries(VOICE_IDS)) {
    if (!meta.includes(`${name}->${id}`)) {
      throw new Error(`Kokoro bundle maps ${name} to a different speaker id than the pinned ${id}.`);
    }
  }
  console.log(`  voices verified: ${Object.entries(VOICE_IDS).map(([n, i]) => `${n}=${i}`).join(", ")}`);
}

// ----------------------------------------------------------------- binaries

async function stageWindowsBinaries(outDir, work) {
  console.log("Windows binaries");
  // Staging a Windows payload from macOS is now the NORMAL path: v1.4.0 builds
  // the Windows installer in a local wine container, and everything here is
  // either a pinned download (sherpa-onnx, onnxruntime) or the frozen,
  // hash-pinned whisper-cli.exe. Only --rebuild-whisper needs a real Windows
  // machine, because only MSVC can produce a new one — and it says so itself.
  if (process.platform !== "win32" && process.argv.includes("--rebuild-whisper")) {
    throw new Error(
      "--rebuild-whisper needs a Windows machine with MSVC: whisper-cli.exe is compiled with "
      + "/MT so it does not depend on the Visual C++ redistributable, and MSVC cannot be "
      + "cross-invoked. Without the flag, the pinned binary in hud/vendor/voice/win32 is used.",
    );
  }

  await stageWhisperCliWindows(work, path.join(outDir, "bin/win32/whisper-cli.exe"), {
    rebuild: process.argv.includes("--rebuild-whisper"),
  });

  const sherpaArchive = await fetchPinned(DOWNLOADS.sherpaWin);
  extract(sherpaArchive, work);
  const sherpaBin = path.join(work, `sherpa-onnx-v${SHERPA_VERSION}-win-x64-shared-MT-Release`, "bin");
  for (const file of ["sherpa-onnx-offline-tts.exe", "onnxruntime.dll", "onnxruntime_providers_shared.dll"]) {
    await copyInto(path.join(sherpaBin, file), path.join(outDir, "bin/win32", file));
  }

  await assertNoCrtDependency(path.join(outDir, "bin/win32"));
}

/**
 * Windows whisper-cli.exe comes from a FROZEN, pinned binary — it is no longer
 * compiled here.
 *
 * It was the only part of the Windows build that needed MSVC, and v1.4.0 moved
 * Windows packaging off GitHub Actions (the free tier was exhausted in a day)
 * onto a local wine container, which cannot run MSVC at all. So the binary
 * built by CI run 31692680757 is vendored with its provenance and its hash;
 * see hud/vendor/voice/win32/whisper-pin.json for how to change the pin.
 *
 * The integrity chain is untouched: the hash is verified HERE, the voice
 * manifest hashes the staged file again, and the ASAR anchor pins the manifest.
 * `--rebuild-whisper` still compiles from source for anyone on a real Windows
 * machine with MSVC — that is how a new pin gets produced.
 */
async function stageWhisperCliWindows(work, target, options = {}) {
  if (options.rebuild === true) return buildWhisperCliWindows(work, target);

  const pinPath = path.join(HUD_ROOT, "vendor", "voice", "win32", "whisper-pin.json");
  const pin = JSON.parse(await fs.readFile(pinPath, "utf8"));
  const frozen = path.join(path.dirname(pinPath), pin.file);
  const contents = await fs.readFile(frozen).catch(() => undefined);
  if (contents === undefined) {
    throw new Error(
      `The pinned Windows whisper-cli.exe is missing from ${frozen}. `
      + "It ships in the repository; restore it, or pass --rebuild-whisper on a Windows machine with MSVC.",
    );
  }
  const digest = createHash("sha256").update(contents).digest("hex");
  if (digest !== pin.sha256) {
    throw new Error(
      `The pinned Windows whisper-cli.exe does not match its recorded hash.\n`
      + `  expected ${pin.sha256}\n  actual   ${digest}\n`
      + "Refusing to ship a binary whose provenance cannot be proven.",
    );
  }
  if (contents.length !== pin.bytes) {
    throw new Error(`The pinned whisper-cli.exe is ${contents.length} bytes, expected ${pin.bytes}.`);
  }
  console.log(`  using the pinned whisper-cli.exe (whisper ${pin.whisperVersion}, ${pin.builtBy})`);
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, contents);
}

async function buildWhisperCliWindows(work, target) {
  const tarball = await fetchPinned(DOWNLOADS.whisperSrc);
  extract(tarball, work);
  const src = path.join(work, `whisper.cpp-${WHISPER_VERSION}`);
  const build = path.join(src, "build");
  console.log("  compiling whisper-cli.exe (static CRT, baseline x86-64)");
  try {
    execFileSync("cmake", ["-B", build, "-A", "x64", ...WHISPER_WIN_CMAKE_FLAGS], {
      cwd: src, stdio: ["ignore", "ignore", "inherit"],
    });
    execFileSync("cmake", ["--build", build, "--config", "Release", "--target", "whisper-cli"], {
      cwd: src, stdio: ["ignore", "ignore", "inherit"],
    });
  } catch (error) {
    throw new Error(`whisper-cli.exe did not build. MSVC and cmake are required on Windows build machines. ${error.message}`);
  }
  // cmake puts multi-config output under build/bin/Release.
  for (const candidate of ["bin/Release/whisper-cli.exe", "bin/whisper-cli.exe", "Release/whisper-cli.exe"]) {
    const built = path.join(build, candidate);
    if (await exists(built)) {
      await copyInto(built, target);
      return;
    }
  }
  throw new Error("whisper-cli.exe was built but could not be located under the cmake output tree.");
}

/**
 * The release gate for finding #3. A binary that imports the VC++ runtime is
 * a crash on a clean Windows machine, and the failure is invisible on a
 * developer box or a CI image that happens to have Visual Studio installed —
 * so the import table is checked directly rather than trusted.
 */
async function assertNoCrtDependency(binDir) {
  const offenders = [];
  for (const entry of await fs.readdir(binDir)) {
    if (!/\.(exe|dll)$/i.test(entry)) continue;
    const needed = await crtImports(path.join(binDir, entry));
    if (needed.length > 0) offenders.push(`${entry} -> ${needed.join(", ")}`);
  }
  if (offenders.length > 0) {
    throw new Error(
      "These Windows binaries require the Visual C++ redistributable, which a clean Windows "
      + `machine does not have:\n  ${offenders.join("\n  ")}`,
    );
  }
  console.log("  verified: no Visual C++ redistributable dependency");
}

async function stageMacBinaries(outDir, work, noBuild) {
  console.log("macOS binaries");
  const sherpaArchive = await fetchPinned(DOWNLOADS.sherpaMac);
  extract(sherpaArchive, work);
  const sherpaRoot = path.join(work, `sherpa-onnx-v${SHERPA_VERSION}-osx-universal2-shared`);
  // The executable's LC_RPATH already contains @loader_path, so the runtime
  // dylib sits beside it and no DYLD_* variable is needed — which matters
  // because the hardened runtime strips DYLD_* from signed child processes.
  await copyInto(path.join(sherpaRoot, "bin/sherpa-onnx-offline-tts"), path.join(outDir, "bin/darwin/sherpa-onnx-offline-tts"), 0o755);
  await copyInto(path.join(sherpaRoot, "lib/libonnxruntime.dylib"), path.join(outDir, "bin/darwin/libonnxruntime.dylib"), 0o755);

  const target = path.join(outDir, "bin/darwin/whisper-cli");
  if (noBuild) throw new Error("--no-build was passed but whisper-cli has no macOS prebuilt upstream; it must be compiled.");
  await buildWhisperCli(work, target);
}

/**
 * whisper.cpp publishes prebuilt binaries for Windows and Linux but only an
 * xcframework for Apple platforms, so the mac CLI is compiled here from the
 * pinned source tarball (hash verified by fetchPinned). The resulting binary
 * lands in extraResources and is code-signed with the app by electron-builder.
 */
async function buildWhisperCli(work, target) {
  const tarball = await fetchPinned(DOWNLOADS.whisperSrc);
  extract(tarball, work);
  const src = path.join(work, `whisper.cpp-${WHISPER_VERSION}`);
  const build = path.join(src, "build");
  console.log("  compiling whisper-cli (universal2, cmake)");
  const configure = ["-B", build, ...WHISPER_CMAKE_FLAGS];
  try {
    execFileSync("cmake", configure, { cwd: src, stdio: ["ignore", "ignore", "inherit"] });
    execFileSync("cmake", ["--build", build, "--config", "Release", "-j", String(os.cpus().length), "--target", "whisper-cli"], {
      cwd: src, stdio: ["ignore", "ignore", "inherit"],
    });
  } catch (error) {
    throw new Error(`whisper-cli did not build. cmake and a C++ toolchain are required on macOS build machines. ${error.message}`);
  }
  await copyInto(path.join(build, "bin/whisper-cli"), target, 0o755);
}

// ----------------------------------------------------------------- licenses

async function stageLicenses(outDir) {
  console.log("Licenses");
  for (const license of LICENSES) {
    const cached = path.join(CACHE_DIR, license.out);
    if (await hashOf(cached) !== license.sha256) {
      const response = await fetch(license.url, { redirect: "follow" });
      if (!response.ok) throw new Error(`License fetch failed: ${license.url} (HTTP ${response.status})`);
      await fs.writeFile(cached, Buffer.from(await response.arrayBuffer()));
      const actual = await hashOf(cached);
      if (actual !== license.sha256) {
        throw new Error(`SHA-256 mismatch for ${license.url}\n  expected ${license.sha256}\n  actual   ${actual}`);
      }
    }
    await copyInto(cached, path.join(outDir, "licenses", license.out));
  }
}

async function writeNotices(outDir, platform) {
  const text = `# JARVIS voice — third-party notices

JARVIS bundles the components below to run speech recognition and speech
synthesis entirely on your own computer. Full license texts are in
\`licenses/\` beside this file.

| Component | Version | License | Source |
|---|---|---|---|
| whisper.cpp | v${WHISPER_VERSION} | MIT | https://github.com/ggml-org/whisper.cpp |
| Whisper \`base.en\` weights (ggml q5_1) | — | MIT | https://huggingface.co/ggerganov/whisper.cpp |
| Kokoro-82M v1.0 (fp32 ONNX) + \`bm_george\`, \`bm_fable\` | v1.0 | Apache-2.0 | https://huggingface.co/hexgrad/Kokoro-82M |
| sherpa-onnx (source project) | v${SHERPA_VERSION} | Apache-2.0 | https://github.com/k2-fsa/sherpa-onnx |
| \`sherpa-onnx-offline-tts\` **executable as shipped** | v${SHERPA_VERSION} | **GPL-3.0** (combined work) | see below |
| ONNX Runtime | bundled with sherpa-onnx | MIT | https://github.com/microsoft/onnxruntime |
| eSpeak NG — code inside \`sherpa-onnx-offline-tts\` | 1.52.0 | GPL-3.0 | https://github.com/espeak-ng/espeak-ng |
| eSpeak NG — \`models/kokoro/espeak-ng-data/\` (phoneme tables, \`en_dict\`) | 1.52.0 | GPL-3.0 | https://github.com/espeak-ng/espeak-ng |

## About eSpeak NG and the GPL

eSpeak NG is licensed under the **GPL-3.0**, and JARVIS ships it in two forms:

1. **Code.** eSpeak NG is statically linked into the upstream
   \`sherpa-onnx-offline-tts\` executable, which JARVIS ships unmodified and
   runs as a **separate child process**. Because GPL-3.0 code is linked into
   that binary, **the executable as shipped is a combined work governed by the
   GPL-3.0**, even though sherpa-onnx's own source is Apache-2.0. The manifest
   labels it that way. No eSpeak NG or sherpa-onnx code is linked into JARVIS
   itself, which is why the application remains separate and proprietary.

   The corresponding source for that exact binary is the upstream sherpa-onnx
   release tag below together with the release's own build configuration; we
   ship the binary byte-for-byte as published and do not patch it. Its SHA-256
   and the exact release archive it came from are recorded in the \`sources\`
   block of \`manifest.json\`.
2. **Data.** The \`models/kokoro/espeak-ng-data/\` directory in this
   installation contains eSpeak NG's own pronunciation tables and the English
   dictionary (\`en_dict\`, \`phondata\`, \`phonindex\`, \`phontab\`,
   \`intonations\`, and the \`lang/\` and \`voices/\` trees). **These files are
   part of eSpeak NG and are covered by the GPL-3.0**, not by Kokoro's
   Apache-2.0 licence. Dictionaries for languages JARVIS does not speak have
   been removed; nothing else about them has been modified.

Complete corresponding source for sherpa-onnx v${SHERPA_VERSION} (including its
eSpeak NG) is published at
https://github.com/k2-fsa/sherpa-onnx/tree/v${SHERPA_VERSION} and the eSpeak NG
source, which is the corresponding source for the data files above, at
https://github.com/espeak-ng/espeak-ng/tree/1.52.0. The full GPL-3.0 text is in
\`licenses/espeak-ng-GPL-3.0.txt\`.

## Privacy

Speech recognition and speech synthesis run locally on this computer and send
no audio anywhere. The text of your request is still sent to whichever AI
provider you configured.

Generated for ${platform}.
`;
  await fs.writeFile(path.join(outDir, "NOTICES.md"), text, "utf8");
}

// ----------------------------------------------------------------- manifest

async function writeManifest(outDir, platform) {
  const files = (await walk(outDir)).filter((file) => file !== "manifest.json").sort();
  const assets = [];
  for (const file of files) {
    const full = path.join(outDir, file);
    assets.push({
      path: file,
      bytes: (await fs.stat(full)).size,
      sha256: await hashOf(full),
      license: labelFor(file, LICENSE_OF, "see NOTICES.md"),
      source: labelFor(file, SOURCE_OF, ""),
      platform,
      // Everything under bin/ is EXECUTED, and packaging signs it — which
      // rewrites the file and invalidates the hash recorded here. Flagged so a
      // packaged build may fall back to the OS signature, pinned to our
      // identity, for these files and no others. Data files (models, lexicons,
      // licenses) are never flagged: nothing rewrites them, so nothing may
      // excuse them from their hash.
      ...(file.startsWith("bin/") ? { signable: true } : {}),
    });
  }

  // Per-file hashes prove the INSTALL is intact. These record where the bytes
  // came from, so the payload can be reproduced (or audited) from the manifest
  // alone, without reading this script. Only the archives actually consumed on
  // this platform are listed.
  // Windows builds whisper-cli from source too (see WHISPER_WIN_CMAKE_FLAGS),
  // so the prebuilt dynamic-CRT release archive is NOT part of the payload's
  // provenance and must not be recorded as if it were.
  const used = platform === "win32"
    ? ["whisperModel", "whisperSrc", "sherpaWin", "kokoro"]
    : ["whisperModel", "whisperSrc", "sherpaMac", "kokoro"];
  const sources = used.map((key) => ({
    id: key,
    file: DOWNLOADS[key].file,
    url: DOWNLOADS[key].url,
    bytes: DOWNLOADS[key].bytes,
    sha256: DOWNLOADS[key].sha256,
  }));
  sources.push(...LICENSES.map((license) => ({
    id: `license:${license.out}`,
    file: license.out,
    url: license.url,
    sha256: license.sha256,
  })));

  const manifest = {
    manifestVersion: 1,
    generatedFor: platform,
    stt: { engine: "whisper.cpp", version: `v${WHISPER_VERSION}`, model: "ggml-base.en-q5_1.bin" },
    tts: {
      engine: "sherpa-onnx",
      version: `v${SHERPA_VERSION}`,
      bundle: KOKORO_BUNDLE,
      model: `${KOKORO_BUNDLE}/model.onnx`,
      quantization: "fp32",
      voice: "bm_george",
      voiceId: "bm_george",
      speakerId: VOICE_IDS.bm_george,
      fallbackVoice: "bm_fable",
      fallbackSpeakerId: VOICE_IDS.bm_fable,
    },
    sources,
    // whisper-cli is compiled on BOTH platforms, for different reasons, and
    // "built from the pinned tarball" is only a reproducibility claim if the
    // build itself is described too.
    build: {
      [platform === "win32" ? "whisper-cli.exe" : "whisper-cli"]: {
        from: DOWNLOADS.whisperSrc.file,
        sourceSha256: DOWNLOADS.whisperSrc.sha256,
        toolchain: platform === "win32" ? "cmake + MSVC (x64)" : "cmake + AppleClang",
        cmakeFlags: platform === "win32" ? WHISPER_WIN_CMAKE_FLAGS : WHISPER_CMAKE_FLAGS,
        note: platform === "win32"
          ? "Compiled at build time with a STATIC CRT so no Visual C++ redistributable is required; "
            + "the upstream prebuilt release imports MSVCP140/VCRUNTIME140 and is deliberately unused. "
            + "Baseline x86-64: AVX2 is never required. Import table asserted CRT-free at stage time."
          : "Compiled at build time; no macOS CLI is published upstream. Code-signed with the app by electron-builder.",
      },
    },
    assets,
  };
  await fs.writeFile(path.join(outDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  return manifest;
}

/**
 * The trust anchor for runtime verification.
 *
 * manifest.json lists the expected hash of every voice file, but it lives in
 * the same mutable resources tree as the files it vouches for — so by itself
 * it proves nothing: anyone who can swap a model can swap the manifest. This
 * writes the manifest's OWN digest into hud/build/, which electron-builder
 * packs inside app.asar, and the ASAR is covered by the code signature. The
 * chain becomes: signature -> asar -> manifest digest -> per-file hashes.
 */
async function writeTrustAnchor(outDir, platform) {
  const manifestPath = path.join(outDir, "manifest.json");
  const anchor = {
    manifestSha256: await hashOf(manifestPath),
    generatedFor: platform,
    stagedAt: new Date().toISOString(),
  };
  const target = path.join(HUD_ROOT, "build", "voice-manifest-digest.json");
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, `${JSON.stringify(anchor, null, 2)}\n`, "utf8");
  console.log(`  trust anchor ${anchor.manifestSha256.slice(0, 16)}… -> ${rel(target)}`);
}

function labelFor(file, table, fallback) {
  const key = Object.keys(table)
    .filter((prefix) => file.startsWith(prefix))
    .sort((a, b) => b.length - a.length)[0];
  return key === undefined ? fallback : table[key];
}

async function check(outDir, platform) {
  const manifest = JSON.parse(await fs.readFile(path.join(outDir, "manifest.json"), "utf8"));
  let bad = 0;
  let signed = 0;
  for (const asset of manifest.assets) {
    if (asset.platform !== undefined && asset.platform !== platform) continue;
    const full = path.join(outDir, asset.path);
    const actual = await hashOf(full);
    if (actual === asset.sha256) continue;
    // The SAME rule the app applies at runtime, so this check answers the
    // question that matters: will the shipped build accept its own payload?
    // Packaging SIGNS the executables, which rewrites them; a signable file may
    // therefore prove itself by an OS signature pinned to our identity. Data
    // files never can, and an unsigned tree cannot fake one.
    if (asset.signable === true && await signedByUs(full, platform)) {
      signed += 1;
      continue;
    }
    console.error(`  MISMATCH ${asset.path}`);
    bad += 1;
  }
  if (bad > 0) {
    console.error(`${bad} staged file(s) do not match manifest.json`);
    process.exitCode = 1;
    return;
  }

  const anchorPath = path.join(HUD_ROOT, "build", "voice-manifest-digest.json");
  const anchor = await fs.readFile(anchorPath, "utf8").then(JSON.parse).catch(() => null);
  const manifestDigest = await hashOf(path.join(outDir, "manifest.json"));
  if (anchor === null) {
    console.error("  MISSING trust anchor build/voice-manifest-digest.json — restage the payload");
    process.exitCode = 1;
    return;
  }
  if (anchor.manifestSha256 !== manifestDigest) {
    console.error("  trust anchor does not match manifest.json — restage the payload");
    process.exitCode = 1;
    return;
  }

  const binDir = path.join(outDir, "bin", platform === "win32" ? "win32" : "darwin");
  if (platform === "win32" && await exists(binDir)) {
    try {
      await assertNoCrtDependency(binDir);
    } catch (error) {
      console.error(`  ${error.message}`);
      process.exitCode = 1;
      return;
    }
  }

  console.log(
    `manifest.json verified: ${manifest.assets.length} files, ${mb(await treeBytes(outDir))} MB, anchor ok`
    + (signed > 0 ? `, ${signed} signed executable(s) verified by signature` : ""),
  );
}

/**
 * Delegates to the app's own verifier so the pinned identity lives in ONE
 * place. Requires the workspace to be built (CI builds before staging); an
 * unbuilt checkout gets an explicit message rather than a silent pass.
 */
async function signedByUs(filePath, platform) {
  let verify;
  let helpers;
  try {
    ({ verifyCodeSignature: verify, SIGNABLE_EXECUTABLES: helpers } =
      await import("@jarvis/setup/code-signature"));
  } catch {
    console.error("  (cannot check signatures: run `pnpm -r build` first)");
    return false;
  }
  // The SAME map the runtime uses, including the per-helper signing identifier.
  // Without it this check would pass a build whose identifiers the app will
  // later refuse — a green CI run for an installer that ships voice dead
  // (integration review r4, finding 3).
  const name = path.basename(filePath);
  if (!helpers.has(name)) {
    console.error(`  ${name} is not a helper this build may sign-verify`);
    return false;
  }
  const identifier = helpers.get(name);
  const verdict = await verify(filePath, {
    platform,
    ...(identifier === undefined ? {} : { identifier }),
  });
  if (verdict.state === "ours") return true;
  console.error(`  signature check: ${verdict.state}${verdict.detail === undefined ? "" : ` — ${verdict.detail}`}`);
  return false;
}

// -------------------------------------------------------------------- utils

async function hashOf(file) {
  try {
    return createHash("sha256").update(await fs.readFile(file)).digest("hex");
  } catch {
    return null;
  }
}

async function exists(target) {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

async function copyInto(from, to, mode) {
  await fs.mkdir(path.dirname(to), { recursive: true });
  await fs.copyFile(from, to);
  if (mode !== undefined) await fs.chmod(to, mode);
}

async function copyRecursive(from, to) {
  const stat = await fs.stat(from);
  if (!stat.isDirectory()) return copyInto(from, to);
  await fs.mkdir(to, { recursive: true });
  for (const entry of await fs.readdir(from)) {
    await copyRecursive(path.join(from, entry), path.join(to, entry));
  }
}

async function walk(root, prefix = "") {
  const out = [];
  for (const entry of await fs.readdir(path.join(root, prefix), { withFileTypes: true })) {
    const relPath = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) out.push(...await walk(root, relPath));
    else out.push(relPath);
  }
  return out;
}

async function treeBytes(root) {
  let total = 0;
  for (const file of await walk(root)) total += (await fs.stat(path.join(root, file))).size;
  return total;
}

const KNOWN_FLAGS = ["--platform", "--out", "--check", "--clean", "--no-build", "--rebuild-whisper"];

/**
 * Accepts BOTH `--out=<dir>` and `--out <dir>`.
 *
 * It used to accept only the first, and silently ignored the second — so
 * `--out /tmp/somewhere` fell back to the DEFAULT staging directory and wiped
 * the payload it was told not to touch. A flag that means "write over there"
 * must never quietly mean "write over here" (found while verifying the
 * integration review's finding 4 fix).
 */
function readFlag(args, name) {
  const inline = args.find((arg) => arg.startsWith(`${name}=`));
  if (inline !== undefined) return inline.slice(name.length + 1);
  const index = args.indexOf(name);
  if (index === -1) return undefined;
  const value = args[index + 1];
  if (value === undefined || value.startsWith("--")) {
    throw new Error(`${name} needs a value, e.g. ${name}=<path>`);
  }
  return value;
}

/** A typo must not resolve to the destructive default. */
function assertKnownFlags(args) {
  const valueOf = new Set();
  for (const [index, arg] of args.entries()) {
    if (!arg.startsWith("--")) {
      // A bare word is only legal as the value of a space-form flag.
      if (!valueOf.has(index)) throw new Error(`Unrecognized argument: ${arg}`);
      continue;
    }
    const name = arg.includes("=") ? arg.slice(0, arg.indexOf("=")) : arg;
    if (!KNOWN_FLAGS.includes(name)) {
      throw new Error(`Unrecognized flag: ${name}. Known flags: ${KNOWN_FLAGS.join(", ")}`);
    }
    if (!arg.includes("=") && (name === "--platform" || name === "--out")) valueOf.add(index + 1);
  }
}

const mb = (bytes) => (bytes / 1_000_000).toFixed(1);
const rel = (target) => path.relative(process.cwd(), target) || ".";

main().catch((error) => {
  console.error(`\nvoice assets: ${error.message}`);
  process.exitCode = 1;
});
