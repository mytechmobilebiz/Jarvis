import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { WizardError, type StepOutcome, type WizardContext } from "./context.js";
import { checkProvenance } from "./engine-pin.js";
import { pathExists, type PathStatus } from "./fs-utils.js";
import {
  hermesCommand,
  hermesVenvExecutablePath,
  runCommand,
  sitePythonPath,
  type CommandResult,
} from "./system.js";
import { warn } from "./ui.js";
import {
  PayloadDigestError,
  readVendorStamp,
  resolveVendorPayload,
  vendorStampPath,
  verifyPayloadDigests,
  type VendorManifest,
  type VendorPayload,
} from "./vendor.js";

/**
 * The offline engine install. Everything it needs ships inside the installer:
 * no git, no installer script, no pypi. The entire August failure classes 3/4/8
 * (git clone half-repos, dir-exists "already installed" lies, uv sync dying on
 * a filtered network) live in the path this replaces.
 *
 * Three properties this must keep:
 *  - HEALTH-GATED, never dir-exists gated (the v1.3.15 lesson): "already
 *    installed" is only ever claimed when the venv's own `hermes --version`
 *    runs AND the stamp says it is the engine this build ships.
 *  - Self-healing on rerun: a broken tree is moved aside and rebuilt.
 *  - Loud, offline-only failure: every pip invocation carries --no-index, so a
 *    missing wheel fails here with a support-shaped message instead of quietly
 *    reaching for the network the customer may not have.
 */
export type VendorInstallAction = "reused" | "installed" | "repaired";

export interface VendoredInstallResult {
  action: VendorInstallAction;
  notes: string[];
  /** Non-fatal detail codes for telemetry; closed set, see VendorFailure. */
  failure?: VendorFailure;
  pipExitCode?: number;
  /** payload-corrupt detail: WHICH digest check refused the payload (closed set). */
  payloadFailureKind?: string;
  /** payload-corrupt detail: the first refusing file, payload-relative. */
  payloadFailureFile?: string;
  /** payload-corrupt detail: how many payload files refused in total (scope). */
  payloadFailureCount?: number;
  /** payload-corrupt detail: the OS error code behind a file-unreadable refusal. */
  payloadFailureErrno?: string;
  /**
   * venv-unhealthy detail: which of the engine's own programs were ABSENT when
   * the health probe ran, engine-root-relative. This is the class where
   * antivirus is most likely — the venv's `hermes` console script is written
   * fresh, unsigned, seconds earlier — and the probe already knew which
   * executables it could not find; it just never said so (Michael Hall,
   * "hermes.exe is missing every time i rerun").
   */
  venvMissing?: string[];
  /** venv-unhealthy detail: programs that ARE there but could not be read. */
  venvUnreadable?: string[];
}

export type VendorFailure =
  | "payload-incomplete"
  | "payload-corrupt"
  | "payload-untrusted"
  | "python-replace-failed"
  | "import-probe-failed"
  | "venv-create-failed"
  | "wheel-install-failed"
  | "engine-install-failed"
  | "venv-unhealthy";

interface VendoredOverrides {
  platform?: NodeJS.Platform;
  run?: typeof runCommand;
  exists?: (filePath: string) => Promise<boolean>;
  copy?: (source: string, target: string) => Promise<void>;
  rename?: (source: string, target: string) => Promise<void>;
  removeDir?: (target: string) => Promise<void>;
  writeStamp?: (target: string, contents: string) => Promise<void>;
  /** Makes the site launcher executable; separated so tests need no real disk. */
  chmod?: (target: string, mode: number) => Promise<void>;
  readStamp?: typeof readVendorStamp;
  /** Target architecture for runtime selection; defaults to the running one. */
  arch?: string;
  /** Resolves symlinks when proving the installed runtime is self-contained. */
  realpath?: (filePath: string) => Promise<string>;
  /**
   * Errno-aware existence, for the DIAGNOSIS only: `exists` flattens "not
   * there" and "not allowed to look" into false, and telling a customer their
   * engine's program is gone when an ACL merely blocked us is the same false
   * precision this release removes everywhere else.
   */
  pathStatus?: (filePath: string) => Promise<PathStatus>;
  /** Digest verification seam; production hashes the real files. */
  verifyDigests?: (vendorRoot: string, manifest: VendorManifest) => Promise<number>;
  now?: () => Date;
}

export async function installVendoredEngine(
  ctx: WizardContext,
  vendorRoot: string,
  manifest: VendorManifest,
  overrides: VendoredOverrides = {},
): Promise<VendoredInstallResult> {
  const platform = overrides.platform ?? process.platform;
  const run = overrides.run ?? runCommand;
  const exists = overrides.exists ?? pathExists;
  // verbatimSymlinks: PBS mac runtimes ship RELATIVE interpreter symlinks
  // (bin/python3 -> python3.12). Without this, fs.cp resolves them against the
  // source and the copied runtime can end up pointing back INTO the .app —
  // an "independent" runtime that breaks the moment the app moves (r2 #10).
  const copy = overrides.copy
    ?? ((source, target) => fs.cp(source, target, { recursive: true, force: true, verbatimSymlinks: true }));
  const rename = overrides.rename ?? ((source, target) => fs.rename(source, target));
  const removeDir = overrides.removeDir ?? ((target) => fs.rm(target, { recursive: true, force: true }));
  const writeStamp = overrides.writeStamp
    ?? (async (target, contents) => {
      await fs.mkdir(path.dirname(target), { recursive: true });
      await fs.writeFile(target, contents, "utf8");
    });
  const chmod = overrides.chmod ?? ((target: string, mode: number) => fs.chmod(target, mode));
  const now = overrides.now ?? (() => new Date());
  const notes: string[] = [];
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const payload = resolveVendorPayload(vendorRoot, manifest, overrides.arch ?? process.arch);

  if (ctx.dryRun) {
    return {
      action: "installed",
      notes: [
        `[dry-run] would install the bundled Hermes ${manifest.engineTag} engine from ${vendorRoot} with no network access.`,
      ],
    };
  }

  await assertPayloadComplete(payload, exists);

  // The root of trust, enforced where it finally matters: on the customer's
  // machine, before a single file is installed. A payload that is perfectly
  // self-consistent but built from a different engine is exactly what this
  // catches (review r3, finding 9).
  if (ctx.packaged) {
    const problem = checkProvenance(manifest.source);
    if (problem !== undefined) {
      return {
        action: "installed",
        notes: [...notes, problem.reason],
        failure: "payload-untrusted",
      };
    }
  }

  // Contents, not just names. A payload that passes the structural check can
  // still be a truncated download or a tampered wheel; hashing it here is the
  // last point before its files become the customer's engine (r1, #6/#12).
  const verifyDigests = overrides.verifyDigests
    ?? ((root: string, target: VendorManifest) =>
      verifyPayloadDigests(root, target, {
        readFile: (filePath: string) => fs.readFile(filePath),
        hash: (data: Buffer) => crypto.createHash("sha256").update(data).digest("hex"),
        realpath: (filePath: string) => fs.realpath(filePath),
        listFiles: (payloadRoot: string) => listPayloadFiles(payloadRoot),
      }));
  try {
    const checked = await verifyDigests(vendorRoot, manifest);
    notes.push(`Checked ${checked} bundled engine files against their checksums.`);
  } catch (error) {
    return {
      action: "installed",
      notes: [...notes, error instanceof Error ? error.message : String(error)],
      failure: "payload-corrupt",
      // Which check, which file — the difference between "AV keeps eating the
      // same binary" and "scattered disk damage" in a support thread. The
      // note above reached the screen; only props reach COPY DIAGNOSTICS.
      ...(error instanceof PayloadDigestError
        ? {
          payloadFailureKind: error.kind,
          ...(error.relativeFile === undefined ? {} : { payloadFailureFile: error.relativeFile }),
          // Scope and cause, the two things a single filename cannot carry:
          // one file or eight hundred, deleted or merely unreadable.
          ...(error.failureCount === undefined ? {} : { payloadFailureCount: error.failureCount }),
          ...(error.errorCode === undefined ? {} : { payloadFailureErrno: error.errorCode }),
        }
        : {}),
    };
  }

  // Health-gated reuse. Both halves matter: the stamp proves the tree is the
  // engine THIS build ships (an older 1.3.x checkout fails it), and the venv
  // probe proves its programs actually run (a half-built venv fails it).
  const stamp = await (overrides.readStamp ?? readVendorStamp)(ctx.hermesAgentDir, platform);
  const shape: EngineShape = payload.site === undefined ? "venv" : "site";
  const healthy = await venvIsHealthy(ctx, platform, run, exists, shape, overrides.pathStatus);
  const sameBuild = stamp?.engineTag === manifest.engineTag
    // Same tag is not same build: a payload rebuilt with different extras or a
    // new Python pin must replace the old install, not be mistaken for it.
    && (manifest.digests === undefined || stamp?.payloadDigest === manifest.digests.sha256);
  if (sameBuild && healthy) {
    // Cheap, and the difference between "the stamp says it is fine" and "it
    // actually loads": a runtime whose files rotted since the last run (a
    // half-finished OS update, an antivirus quarantine) fails here and gets
    // rebuilt, instead of being reused into a crash-loop (r1 #12 remainder).
    const reusable = await importProbe(ctx, platform, run, exists);
    if (reusable.status === 0) {
      return {
        action: "reused",
        notes: [`The bundled Hermes ${manifest.engineTag} engine is already installed and runnable.`],
      };
    }
    notes.push("The installed engine no longer loads its own program files, so it is being rebuilt.");
  }

  let action: VendorInstallAction = "installed";
  if (await exists(ctx.hermesAgentDir)) {
    action = "repaired";
    const backup = pathApi.join(
      pathApi.dirname(ctx.hermesAgentDir),
      `hermes-agent.replaced-${stampSuffix(now())}`,
    );
    try {
      await rename(ctx.hermesAgentDir, backup);
      notes.push(
        stamp === undefined
          ? `The previous engine folder was moved to ${backup} and replaced with the bundled ${manifest.engineTag} engine.`
          : `The engine folder was rebuilt from the bundled ${manifest.engineTag} payload; the previous one is at ${backup}.`,
      );
    } catch {
      // A locked file (AV scan, an engine still running) can block the rename.
      // Deleting in place is the fallback; if that fails too, say so honestly
      // rather than installing over a half-tree.
      try {
        await removeDir(ctx.hermesAgentDir);
        notes.push("The previous engine folder could not be moved aside, so it was removed and rebuilt.");
      } catch {
        throw new WizardError(
          "The existing Hermes engine folder could not be replaced.",
          `Close JARVIS and any terminal using ${ctx.hermesAgentDir}, then run setup again.`,
        );
      }
    }
  }

  await copy(payload.source, ctx.hermesAgentDir);
  notes.push(`Unpacked the bundled Hermes ${manifest.engineTag} engine (no download needed).`);

  // The interpreter is installed OUT of the app bundle, next to the engine it
  // serves. Two reasons, both found the hard way:
  //  - `venv --copies` is not portable: the bundled macOS interpreter links
  //    against @executable_path/../lib/libpython3.12.dylib, which --copies does
  //    not copy, so the copied python dies with SIGABRT during ensurepip.
  //  - a venv that references the app bundle (by symlink or by pyvenv.cfg
  //    `home`) breaks when the app is updated, moved, or reinstalled — the
  //    engine would stop working for reasons no customer could diagnose.
  // A local runtime keyed by version makes both impossible, and a rerun copies
  // nothing when the version already matches.
  const localPython = await ensureLocalPython(ctx, payload, manifest, platform, {
    copy,
    removeDir,
    writeStamp,
    exists,
    realpath: overrides.realpath ?? ((filePath: string) => fs.realpath(filePath)),
  });
  if (localPython.copied) {
    notes.push(`Installed the bundled Python ${manifest.python.version} runtime for the engine.`);
  }

  // A SITE payload arrives with its libraries already installed and already
  // signed, so first run installs NOTHING: copy the tree, write the launcher,
  // and go straight to the health gate. This is the appliance the lane set out
  // to build — and on macOS it is not optional, because Apple's notary refuses
  // an app that carries unsigned Mach-O inside its wheels.
  if (payload.site !== undefined) {
    // Into the interpreter's OWN site-packages, not a sibling folder: python
    // finds them with no PYTHONPATH, so nothing has to remember to set an
    // environment variable before the engine will start — not the supervisor,
    // not a support script, not a customer in a terminal.
    const sitePath = pathApi.join(
      pathApi.dirname(pathApi.dirname(localPython.executable)),
      ...(platform === "win32"
        ? ["Lib", "site-packages"]
        : ["lib", `python${majorMinor(manifest.python.version)}`, "site-packages"]),
    );
    await copy(payload.site, sitePath);
    notes.push(
      "Installed the bundled engine libraries — already built into this release, so there is nothing to download or compile.",
    );
    return finishInstall();
  }

  const venvPath = pathApi.join(ctx.hermesAgentDir, "venv");
  const venv = await run(ctx, localPython.executable, ["-m", "venv", venvPath], { stream: true });
  if (venv.status !== 0) {
    return failure(notes, "venv-create-failed", venv, [
      "The bundled Python could not create the engine's environment.",
    ]);
  }

  const venvPython = venvPythonPath(ctx.hermesAgentDir, platform);
  const pipBase = [
    "-m",
    "pip",
    "install",
    // --no-index is the structural promise of this lane: pip may NEVER reach
    // the network on a customer machine. Tests assert it on every invocation.
    "--no-index",
    "--find-links",
    payload.wheelhouse,
    "--disable-pip-version-check",
    "--no-input",
  ];
  const deps = await run(ctx, venvPython, [...pipBase, "--requirement", payload.requirements], {
    cwd: ctx.hermesAgentDir,
    stream: true,
  });
  if (deps.status !== 0) {
    return failure(notes, "wheel-install-failed", deps, [
      "The bundled engine libraries did not install from the offline wheelhouse.",
    ]);
  }

  const engine = await run(ctx, venvPython, [...pipBase, "--no-deps", payload.engineWheel], {
    cwd: ctx.hermesAgentDir,
    stream: true,
  });
  if (engine.status !== 0) {
    return failure(notes, "engine-install-failed", engine, [
      "The bundled engine package did not install from the offline wheelhouse.",
    ]);
  }

  // The install shape decides WHICH program has to exist afterwards — see
  // probeVenvHealth. A wheelhouse install must end with the venv's own
  // launcher; a site install with the bundled interpreter.
  const health = await probeVenvHealth(ctx, platform, run, exists, shape, overrides.pathStatus);
  if (!health.healthy) {
    return {
      // NO PATHS ON SCREEN (Codex review r1, P1): the note is what the
      // customer reads, and the copy principle is plain words — the file
      // names live in the props, which only support ever sees.
      ...failure(notes, "venv-unhealthy", undefined, [
        health.unreadable.length > 0
          ? "The engine installed, but this computer would not let me read its own program afterwards."
          : health.missing.length > 0
            ? "The engine installed, but its own program was gone from the computer immediately afterwards."
            : "The engine installed but its programs did not run afterwards.",
      ]),
      // WHICH program went missing, for support. pip had just written it: a
      // freshly minted, unsigned console script is the single most likely
      // thing on this machine for antivirus to take (Leg 1's rationale).
      ...(health.missing.length === 0 ? {} : { venvMissing: health.missing }),
      ...(health.unreadable.length === 0 ? {} : { venvUnreadable: health.unreadable }),
    };
  }

  return finishInstall();

  /**
   * The gate both install shapes end at, unchanged by which one ran: the
   * engine's own programs must LOAD, and only then is the stamp written. A
   * stamp written before the probe would let a broken install claim to be
   * finished and be "reused" forever after (the v1.3.15 lesson).
   */
  async function finishInstall(): Promise<VendoredInstallResult> {
    const imports = await importProbe(ctx, platform, run, exists);
    if (imports.status !== 0) {
      return failure(notes, "import-probe-failed", imports, [
        "The engine installed but could not load its own program files.",
      ]);
    }

    await writeStamp(
      vendorStampPath(ctx.hermesAgentDir, platform),
      `${JSON.stringify(
        {
          engineTag: manifest.engineTag,
          engineVersion: manifest.engineVersion,
          installedAt: now().toISOString(),
          ...(manifest.digests === undefined ? {} : { payloadDigest: manifest.digests.sha256 }),
        },
        null,
        2,
      )}\n`,
    );
    notes.push(`Hermes ${manifest.engineTag} is installed and its programs run.`);
    return { action, notes };
  }
}

function failure(
  notes: string[],
  code: VendorFailure,
  result: CommandResult | undefined,
  extraNotes: string[],
): VendoredInstallResult {
  return {
    action: "installed",
    notes: [...notes, ...extraNotes],
    failure: code,
    ...(result === undefined ? {} : { pipExitCode: result.status }),
  };
}

/**
 * Turns an install failure into the step outcome. The copy deliberately never
 * mentions the internet: an offline install that failed is a BUILD or disk
 * problem, and sending a customer to check their network (v1.3.x did) wasted
 * real support hours on people whose network was fine.
 */
export function vendoredFailureOutcome(
  result: VendoredInstallResult,
  props: Record<string, unknown>,
): StepOutcome {
  return {
    status: "failed",
    notes: result.notes,
    props: {
      ...props,
      engine_vendored: true,
      hermes_venv_present: false,
      ...(result.failure === undefined ? {} : { engine_failure: result.failure }),
      ...(result.pipExitCode === undefined ? {} : { engine_exit_code: result.pipExitCode }),
      ...(result.payloadFailureKind === undefined ? {} : { payload_failure_kind: result.payloadFailureKind }),
      ...(result.payloadFailureFile === undefined ? {} : { payload_failure_file: result.payloadFailureFile }),
      ...(result.payloadFailureCount === undefined ? {} : { payload_failure_count: result.payloadFailureCount }),
      ...(result.payloadFailureErrno === undefined ? {} : { payload_failure_errno: result.payloadFailureErrno }),
      ...(result.venvMissing === undefined ? {} : { venv_missing_file: result.venvMissing.join(",") }),
      ...(result.venvUnreadable === undefined ? {} : { venv_unreadable_file: result.venvUnreadable.join(",") }),
    },
    nextActions: [
      // Honest about what we can and cannot know (Justin Curnutt, 3×
      // payload-corrupt): quarantine is ONE possible cause, and nothing in
      // this check can distinguish it from a damaged download or disk — so
      // the copy says exactly that instead of guessing.
      ...(result.failure === "payload-corrupt"
        ? [
          "Antivirus quarantine is one possible cause — JARVIS cannot tell it apart from a damaged download or a disk problem. If your antivirus shows a recent quarantine, restore the file and add the JARVIS folder to its exclusions, then run setup again.",
        ]
        : []),
      // The SAME honesty for the class where quarantine is most likely of all
      // (Leg 1, promoted 2026-08-17): the engine's own program is written
      // fresh, unsigned, seconds before this check, and an antivirus that
      // takes it leaves an install that rebuilds itself and loses it again on
      // every rerun ("hermes.exe is missing every time i rerun" — Michael
      // Hall). Different remedy from the payload case: the file to protect is
      // one JARVIS creates, so an exclusion has to cover the engine folder
      // BEFORE the next run, and there is nothing to restore from quarantine.
      ...(result.failure === "venv-unhealthy"
        ? [
          "Antivirus is one possible cause — the engine's own program file is brand new and unsigned when it is created, which some antivirus tools remove on sight. Add the JARVIS engine folder to your antivirus exclusions, then run setup again; if your antivirus lists something it removed in the last few minutes, allow it.",
        ]
        : []),
      "Restart this computer and run setup again — a running engine or antivirus scan can hold the engine files open.",
      "If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me; this install needs no internet, so the problem is on this machine or in the download.",
    ],
  };
}

async function assertPayloadComplete(
  payload: VendorPayload,
  exists: (filePath: string) => Promise<boolean>,
): Promise<void> {
  const required: Array<[string, string]> = [
    [payload.python, "its bundled Python"],
    [payload.pythonDir, "its bundled Python folder"],
    [payload.source, "the engine files"],
    // Each shape is checked for what IT ships. A site payload has no
    // wheelhouse and a wheelhouse payload has no site tree; demanding both
    // would make every payload incomplete.
    ...(payload.site === undefined
      ? ([
        [payload.wheelhouse, "its offline library folder"],
        [payload.requirements, "its library list"],
        [payload.engineWheel, "the engine package"],
      ] as Array<[string, string]>)
      : ([[payload.site, "its installed library folder"]] as Array<[string, string]>)),
  ];
  for (const [target, label] of required) {
    if (!(await exists(target))) {
      throw new WizardError(
        `The bundled JARVIS engine is incomplete: ${label} is missing.`,
        "Reinstall JARVIS from the download link in your purchase email — this copy did not unpack completely.",
      );
    }
  }
}

/**
 * The engine actually LOADS. A successful `hermes --version` only proves a
 * console script exists; this proves the interpreter can import the stdlib
 * pieces the gateway depends on and the engine package itself.
 */
async function importProbe(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: typeof runCommand,
  exists: (filePath: string) => Promise<boolean>,
): Promise<CommandResult> {
  try {
    // Probe through the SAME command the gateway will spawn, so what passes
    // here is the launch path that has to work, not an adjacent one. VENV
    // FIRST (review round 12): on a Windows wheelhouse machine the bootstrap
    // interpreter at <hermesRoot>/python exists too, but it carries none of
    // the wheel-installed dependencies — probing it condemned every healthy
    // Windows install to a rebuild. The venv is the launch interpreter
    // wherever it exists; the site interpreter is the launch path only on
    // site-shape installs, which have no venv at all.
    const venv = venvPythonPath(ctx.hermesAgentDir, platform);
    const python = (await exists(venv)) ? venv : sitePythonPath(ctx, platform);
    return await run(
      ctx,
      python,
      ["-c", "import sqlite3, ssl, hermes_cli.main"],
      { cwd: ctx.hermesAgentDir, stream: false },
    );
  } catch {
    return { status: 127, stdout: "", stderr: "" };
  }
}

export function localPythonDir(hermesRoot: string, platform: NodeJS.Platform): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return pathApi.join(hermesRoot, "python");
}

/**
 * Copies the payload's interpreter to `<hermesRoot>/python` unless the version
 * already installed there matches. The stamp is written LAST, so an interrupted
 * copy is retried rather than trusted.
 */
async function ensureLocalPython(
  ctx: WizardContext,
  payload: VendorPayload,
  manifest: VendorManifest,
  platform: NodeJS.Platform,
  io: {
    copy: (source: string, target: string) => Promise<void>;
    removeDir: (target: string) => Promise<void>;
    writeStamp: (target: string, contents: string) => Promise<void>;
    exists: (filePath: string) => Promise<boolean>;
    realpath?: (filePath: string) => Promise<string>;
  },
): Promise<{ executable: string; copied: boolean }> {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const target = localPythonDir(ctx.hermesRoot, platform);
  // The tail comes from the SELECTED runtime (universal payloads carry
  // python-arm64/ and python-x64/, not python/), so this is derived from the
  // resolved payload rather than the manifest's default entry.
  const relative = path.relative(payload.pythonDir, payload.python).split(path.sep);
  const executable = pathApi.join(target, ...relative);
  const stampPath = pathApi.join(target, ".jarvis-python.json");

  const stamped = await readJsonStamp(stampPath);
  const samePython = stamped?.version === manifest.python.version
    // A rebuilt payload can carry the same Python version with different
    // contents (a new prune list), so the digest decides, not the version.
    && (manifest.digests === undefined || stamped?.payloadDigest === manifest.digests.sha256);
  if (samePython && (await io.exists(executable))) {
    return { executable, copied: false };
  }

  try {
    await io.removeDir(target);
  } catch {
    // Merging a new runtime into a directory we could not clear leaves a
    // half-and-half interpreter that fails in ways nobody can diagnose
    // (review r1, finding 12) — fail honestly instead.
    throw new WizardError(
      "The previous bundled Python could not be replaced.",
      `Close JARVIS and any terminal using ${target}, then run setup again.`,
    );
  }
  // Payload paths are HOST paths (the payload sits next to the running app);
  // only the destination follows the target platform's separator rules.
  await io.copy(payload.pythonDir, target);
  // The copy is only "independent" if nothing inside it still points back into
  // the app bundle. Resolve the interpreter for real and require it to live
  // under the installed runtime directory (review r2, finding 10).
  if (io.realpath !== undefined) {
    let resolved: string;
    let resolvedTarget: string;
    try {
      resolved = await io.realpath(executable);
      // BOTH sides must be resolved. A machine whose install path crosses a
      // symlink (macOS /var -> /private/var, a redirected home, an OneDrive
      // junction) would otherwise fail this check on a perfectly good install.
      resolvedTarget = await io.realpath(target);
    } catch {
      throw new WizardError(
        "The bundled Python did not install correctly.",
        `Delete ${target} and run setup again.`,
      );
    }
    const boundary = pathApi.resolve(resolvedTarget) + pathApi.sep;
    if (!pathApi.resolve(resolved).startsWith(boundary)) {
      throw new WizardError(
        "The installed Python still points back inside the JARVIS app, so it would stop working when the app is updated.",
        `Delete ${target} and run setup again; if it repeats, reinstall JARVIS from your purchase email.`,
      );
    }
  }

  await io.writeStamp(
    stampPath,
    `${JSON.stringify(
      {
        version: manifest.python.version,
        kind: manifest.python.kind,
        ...(manifest.digests === undefined ? {} : { payloadDigest: manifest.digests.sha256 }),
      },
      null,
      2,
    )}\n`,
  );
  return { executable, copied: true };
}

async function readJsonStamp(
  filePath: string,
): Promise<{ version?: string; payloadDigest?: string } | undefined> {
  try {
    const text = await fs.readFile(filePath, "utf8");
    return JSON.parse(text) as { version?: string; payloadDigest?: string };
  } catch {
    return undefined;
  }
}

export function venvPythonPath(hermesAgentDir: string, platform: NodeJS.Platform): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return platform === "win32"
    ? pathApi.join(hermesAgentDir, "venv", "Scripts", "python.exe")
    : pathApi.join(hermesAgentDir, "venv", "bin", "python");
}

/**
 * The health probe, with its findings kept instead of thrown away.
 *
 * `missing` names the engine programs that were NOT on disk when the probe
 * ran, relative to the engine root — never absolute: these props are pasted
 * into support email, `sanitizeProps` truncates strings at 80 characters, and
 * an absolute path is mostly the customer's name and home directory.
 */
export interface VenvHealth {
  healthy: boolean;
  /** Engine-root-relative paths of the candidates that were genuinely ABSENT. */
  missing: string[];
  /** Engine-root-relative paths that exist but this computer would not let us read. */
  unreadable: string[];
}

/**
 * Which program THIS install shape must end up with: a wheelhouse install
 * builds a venv and mints its own launcher; a site install runs the bundled
 * interpreter directly.
 */
type EngineShape = "venv" | "site";

async function probeVenvHealth(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: typeof runCommand,
  exists: (filePath: string) => Promise<boolean>,
  /** When known, the shape whose program MUST be present (see below). */
  expected?: EngineShape,
  statusOf: (filePath: string) => Promise<PathStatus> = executableStatus,
): Promise<VenvHealth> {
  // Whichever shape this machine has — the site payload's launcher or the
  // wheelhouse payload's venv — asked through the same seam the rest of the
  // installer uses, so a test filesystem answers it and the real one is never
  // consulted behind the caller's back.
  // Whichever shape this machine has, asked through the same seam the gateway
  // uses — and through the injected `exists`, so a test filesystem answers it
  // and the real disk is never consulted behind the caller's back.
  const venvExecutable = hermesVenvExecutablePath(ctx, platform);
  const siteExecutable = sitePythonPath(ctx, platform);
  const present = new Set<string>();
  const absent: string[] = [];
  for (const candidate of [venvExecutable, siteExecutable]) {
    if (await exists(candidate)) present.add(candidate);
    else absent.push(candidate);
  }

  /**
   * WHAT COUNTS AS MISSING (Codex review r1, P2 — this is the whole point of
   * the diagnostic, and the first cut got it wrong).
   *
   * The two candidates are NOT mutually exclusive on Windows: a wheelhouse
   * install copies the bundled interpreter to <hermesRoot>/python AND builds
   * a venv whose `hermes.exe` pip mints. So when antivirus eats only
   * `venv/Scripts/hermes.exe` — Michael Hall's exact report, the case this
   * prop exists for — the interpreter is still there, and a rule of "report
   * only when NOTHING is on disk" would say nothing at all.
   *
   * So expectation comes from the install SHAPE when the caller knows it: a
   * wheelhouse install must have the venv launcher, a site install must have
   * the interpreter. Without that knowledge (the reuse gates, which only want
   * the boolean) only a total absence is reported, because there the sibling's
   * absence really is routine.
   */
  const required = expected === "venv" ? venvExecutable : expected === "site" ? siteExecutable : undefined;
  const reportable = required === undefined
    ? (present.size === 0 ? absent : [])
    : absent.filter((candidate) => candidate === required);

  // ABSENT is not the same as UNREADABLE (Codex review r4): the seam above
  // answers a boolean, and production's `pathExists` returns false for an
  // EACCES/EPERM just as it does for a file that is not there. Only a genuine
  // absence may be reported as a missing file; a blocked read gets its own
  // prop, because the remedies differ (restore/reinstall vs unblock).
  const missing: string[] = [];
  const unreadable: string[] = [];
  for (const candidate of reportable) {
    const relative = engineRelative(ctx, candidate, platform);
    // eslint-disable-next-line no-await-in-loop -- at most two candidates
    if ((await statusOf(candidate)) === "inaccessible") unreadable.push(relative);
    else missing.push(relative);
  }

  if (present.size === 0 || (required !== undefined && !present.has(required))) {
    return { healthy: false, missing, unreadable };
  }
  // ENFORCED, not merely reported (Codex review r2, P2): when the shape is
  // known, the probe runs THAT shape's program. Otherwise a wheelhouse install
  // whose launcher is gone could be called healthy by the bundled interpreter
  // that happens to sit beside it — the mask this diagnostic exists to lift.
  const usable = required === undefined ? present : new Set([required]);
  const command = hermesCommand(ctx, platform, (target) => usable.has(target));
  let ran = false;
  try {
    const version = await run(ctx, command.executable, [...command.args, "--version"], { stream: false });
    // A run that WORKED proves the shape is fine whatever else is absent.
    if (version.status === 0) return { healthy: true, missing: [], unreadable: [] };
    ran = true;
  } catch {
    ran = true;
  }

  // PRESENT BUT UNRUNNABLE is the third state (Codex review, 2026-08-17).
  // `exists` asks F_OK, so a launcher an ACL or antivirus filter blocks from
  // EXECUTING counts as present, never reaches the absent/unreadable sort
  // above, and the probe just fails with no diagnosis at all — on precisely
  // the machine that most needs one. Ask whether the program we required can
  // actually be run, and say so when it cannot.
  if (ran && required !== undefined && present.has(required)
    && (await statusOf(required)) === "inaccessible") {
    unreadable.push(engineRelative(ctx, required, platform));
  }
  return { healthy: false, missing, unreadable };
}

/**
 * Can this program be RUN? `pathStatus` answers F_OK, which a file with its
 * execute bit denied still satisfies; the health probe needs X_OK, and needs
 * "I was not allowed to look" kept distinct from "not there".
 */
async function executableStatus(filePath: string): Promise<PathStatus> {
  try {
    await fs.access(filePath, fs.constants.X_OK);
    return "present";
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return code === "ENOENT" || code === "ENOTDIR" ? "absent" : "inaccessible";
  }
}

async function venvIsHealthy(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: typeof runCommand,
  exists: (filePath: string) => Promise<boolean>,
  expected?: EngineShape,
  statusOf?: (filePath: string) => Promise<PathStatus>,
): Promise<boolean> {
  return (await probeVenvHealth(ctx, platform, run, exists, expected, statusOf)).healthy;
}

/**
 * A payload/engine path as the customer's own install would spell it —
 * `venv/bin/hermes`, `python/bin/python3` — with the home directory and
 * install root cut off. Falls back to the basename when the path is not under
 * the engine root at all, so a prop is never an absolute path by accident.
 */
function engineRelative(ctx: WizardContext, target: string, platform: NodeJS.Platform): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  for (const root of [ctx.hermesAgentDir, ctx.hermesRoot]) {
    const prefix = root.endsWith(pathApi.sep) ? root : `${root}${pathApi.sep}`;
    if (target.startsWith(prefix)) return target.slice(prefix.length).split(pathApi.sep).join("/");
  }
  return target.split(/[\\/]/).pop() ?? target;
}

/** "3.12.11" -> "3.12": the directory python itself uses for site-packages. */
function majorMinor(version: string): string {
  return version.split(".").slice(0, 2).join(".");
}

function stampSuffix(date: Date): string {
  return date.toISOString().replace(/[:.]/g, "-").replace(/Z$/, "");
}

export function warnOnce(ctx: WizardContext, message: string, notes: string[]): void {
  warn(ctx, message);
  notes.push(message);
}

/**
 * "Is the engine on disk the one THIS build ships?"
 *
 * A 1.3.x machine has a perfectly healthy network-installed engine, so a
 * gate on health alone leaves it in place forever and the appliance never
 * actually lands (review r1, finding 10). The stamp answers the only question
 * that matters — same engine tag AND same payload digest — and setup forces
 * the Hermes step whenever the answer is no.
 */
export async function vendoredEngineNeedsInstall(
  ctx: WizardContext,
  manifest: VendorManifest,
  platform: NodeJS.Platform = process.platform,
  readStamp: typeof readVendorStamp = readVendorStamp,
  run: typeof runCommand = runCommand,
  exists: (filePath: string) => Promise<boolean> = pathExists,
): Promise<boolean> {
  const stamp = await readStamp(ctx.hermesAgentDir, platform);
  if (stamp === undefined) return true;
  if (stamp.engineTag !== manifest.engineTag) return true;
  // A payload rebuilt from the same tag can still differ (different extras, a
  // new Python pin); the digest is what makes "same engine" precise.
  const expected = manifest.digests?.sha256;
  if (expected !== undefined && stamp.payloadDigest !== expected) return true;
  // The stamp says the right build was installed ONCE — not that it still
  // runs (F2 review round 10). While the purge deleted the stamp on every
  // rerun, a broken venv was accidentally repaired by the forced reinstall;
  // now that a current stamp survives, the retained-step decision itself
  // must demand a runnable engine, or RETRY SETUP loops on a machine whose
  // venv rotted or was deleted by hand. BOTH probes the installer's own
  // reuse gate runs (round 11): --version proves the program starts,
  // importProbe proves the gateway's dependencies still load — a runtime
  // that lost _sqlite3 or ssl passes the first and fails the second.
  //
  // A CANCELLED probe proves nothing: runCommand resolves 130 on abort,
  // which would read as "unhealthy" and start a rename-and-copy reinstall
  // after the customer closed the window (round 11). The run is ending;
  // retain and let the next run decide.
  const aborted = (): boolean => ctx.signal?.aborted === true;
  if (aborted()) return false;
  // The manifest names the shape this build installs, so the retained-step
  // decision asks about THAT program rather than whichever candidate exists.
  if (!(await venvIsHealthy(ctx, platform, run, exists, manifest.site === undefined ? "venv" : "site"))) {
    return !aborted();
  }
  const reusable = await importProbe(ctx, platform, run, exists);
  if (aborted()) return false;
  return reusable.status !== 0;
}

/**
 * Every file in the payload, posix-relative. Backs the extras sweep: a file
 * nobody vouched for is as dangerous as a modified one (review r2, finding 9).
 */
async function listPayloadFiles(root: string, prefix = ""): Promise<string[]> {
  const out: string[] = [];
  const entries = await fs.readdir(path.join(root, prefix), { withFileTypes: true });
  for (const entry of entries) {
    const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) out.push(...(await listPayloadFiles(root, relative)));
    else out.push(relative);
  }
  return out;
}
