import path from "node:path";
import { readTextIfExists } from "./fs-utils.js";

/**
 * The build-time engine payload (`hud/scripts/build-vendor-payload.mjs`) that
 * makes a packaged install work with NO network at all: the pinned Hermes
 * source, an offline wheelhouse, and a relocatable CPython.
 *
 * `vendor-manifest.json` is written LAST by the build script, so its presence
 * is the "payload is complete" signal. Everything here validates before
 * believing it — a half-copied payload must fail loudly at install time, not
 * produce a half-built venv on a customer machine.
 */
export const VENDOR_MANIFEST_FILE = "vendor-manifest.json";
export const VENDOR_DIRECTORY = "engine-vendor";
export const VENDOR_STAMP_FILE = ".jarvis-engine.json";

export interface VendorManifest {
  schema: number;
  engineTag: string;
  engineVersion: string;
  platform: string;
  arch: string;
  python: {
    kind: string;
    version: string;
    executable: string;
    /** Per-architecture interpreters in a universal mac payload. */
    variants?: Record<string, string>;
  };
  /**
   * WHEELHOUSE payloads (Windows) carry wheels and pip-install them on first
   * run. SITE payloads (macOS, v1.4.0) carry the libraries already installed,
   * one tree per architecture, and install nothing.
   *
   * The split is not a preference: Apple's notary service opens every archive
   * in the app — .whl included — and refuses any Mach-O inside that is not
   * signed by us with a secure timestamp. Wheels from PyPI never are. Shipping
   * them installed makes every extension module a loose file that gets signed
   * with the app, which is both what notarization requires and a better
   * posture than signing a zip's contents and calling it done.
   */
  engineWheel?: string;
  requirements?: string;
  /** Per-architecture pre-installed library trees, e.g. { arm64: "site-arm64" }. */
  site?: Record<string, string>;
  extras: string[];
  wheelCount: number;
  sizeBytes?: number;
  builtAt?: string;
  /**
   * The root of trust for payload CONTENTS. The manifest carries the digest of
   * the sidecar digest file, and that file carries a digest for every other
   * file — so a corrupt or substituted wheel cannot pass by keeping its name
   * and the file count intact (review r1, finding 6).
   */
  digests?: { algorithm: string; file: string; sha256: string; fileCount?: number };
  /** How the engine source was obtained; release builds refuse "local-dirty". */
  source?: { kind: string; commit?: string; tarballSha256?: string };
}

export interface VendorPayload {
  root: string;
  manifest: VendorManifest;
  /** Absolute path of the bundled interpreter that creates the venv. */
  python: string;
  /**
   * The runtime DIRECTORY that interpreter lives in — "python" for a
   * single-arch payload, "python-arm64"/"python-x64" for a universal one. The
   * installer copies THIS; assuming "python" broke every universal mac install
   * before the venv was even created (review r2, new finding 1).
   */
  pythonDir: string;
  /** Absolute path of the pinned engine source tree inside the payload. */
  source: string;
  wheelhouse: string;
  requirements: string;
  engineWheel: string;
  /**
   * The pre-installed library tree for this architecture, when the payload has
   * one. Its presence is what tells the installer there is nothing to install:
   * copy it, point the launcher at it, and run the health gate.
   */
  site?: string;
}

/** Does this payload ship its libraries already installed? */
export function isSitePayload(manifest: VendorManifest): boolean {
  return manifest.site !== undefined;
}

/** Written into the installed engine dir so a rerun can skip a finished install. */
export interface VendorStamp {
  engineTag: string;
  engineVersion: string;
  installedAt: string;
  /** The payload digest this install came from; "same tag" is not "same build". */
  payloadDigest?: string;
}

/**
 * Where the payload lives. Packaged builds carry it in the app's resources
 * next to `jarvis-content`; a checkout keeps it at `hud/vendor/engine` so
 * `pnpm wizard` can exercise the same path. JARVIS_ENGINE_VENDOR overrides
 * both — that is how CI points the setup tests at a payload it just built.
 */
export function resolveVendorRoot(options: {
  packaged: boolean;
  resourcesPath?: string;
  repoRoot?: string;
  env?: NodeJS.ProcessEnv;
}): string | undefined {
  const override = options.env?.JARVIS_ENGINE_VENDOR;
  if (override !== undefined && override !== "") return override;
  if (options.packaged) {
    return options.resourcesPath === undefined
      ? undefined
      : path.join(options.resourcesPath, VENDOR_DIRECTORY);
  }
  return options.repoRoot === undefined
    ? undefined
    : path.join(options.repoRoot, "hud", "vendor", "engine");
}

/**
 * Reads and validates the manifest. Returns undefined for "no payload here"
 * (a source checkout, a dev build) — the caller then takes the network install
 * path. Throws only when a payload exists but is unusable, because silently
 * falling back to the network is exactly the failure this lane deletes.
 */
export async function readVendorManifest(
  vendorRoot: string | undefined,
  options: { requireTrust?: boolean } = {},
): Promise<VendorManifest | undefined> {
  if (vendorRoot === undefined || vendorRoot === "") return undefined;
  const text = await readTextIfExists(path.join(vendorRoot, VENDOR_MANIFEST_FILE));
  if (text === undefined) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("The bundled engine payload manifest is not readable JSON.");
  }
  return validateManifest(parsed, options);
}

/**
 * Mach-O magic: 32- and 64-bit, both endiannesses, and both universal (fat)
 * containers — FAT64 (`cafebabf`) as well as the classic `cafebabe`, because a
 * payload that outgrows 4 GB slices would otherwise stop being recognised as
 * executable content and start being read as data.
 */
export function isMachO(contents: Buffer): boolean {
  if (contents.length < 4) return false;
  const magic = contents.readUInt32BE(0);
  return magic === 0xfeedface // 32-bit, big endian
    || magic === 0xcefaedfe // 32-bit, little endian
    || magic === 0xfeedfacf // 64-bit, big endian
    || magic === 0xcffaedfe // 64-bit, little endian
    || magic === 0xcafebabe // universal (fat)
    || magic === 0xbebafeca // universal, swapped
    || magic === 0xcafebabf // universal (fat64)
    || magic === 0xbfbafeca; // universal (fat64), swapped
}

export function validateManifest(
  parsed: unknown,
  options: { requireTrust?: boolean } = {},
): VendorManifest {
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("The bundled engine payload manifest is not an object.");
  }
  const raw = parsed as Record<string, unknown>;
  if (raw.schema !== 1) {
    throw new Error(`The bundled engine payload manifest has unsupported schema ${String(raw.schema)}.`);
  }
  const python = raw.python;
  if (typeof python !== "object" || python === null || Array.isArray(python)) {
    throw new Error("The bundled engine payload manifest has no python entry.");
  }
  const pythonRaw = python as Record<string, unknown>;
  const manifest: VendorManifest = {
    schema: 1,
    engineTag: requireString(raw.engineTag, "engineTag"),
    engineVersion: requireString(raw.engineVersion, "engineVersion"),
    platform: requireString(raw.platform, "platform"),
    arch: requireString(raw.arch, "arch"),
    python: {
      kind: requireString(pythonRaw.kind, "python.kind"),
      version: requireString(pythonRaw.version, "python.version"),
      executable: requireString(pythonRaw.executable, "python.executable"),
      ...(readVariants(pythonRaw.variants) === undefined
        ? {}
        : { variants: readVariants(pythonRaw.variants) }),
    },
    ...readInstallShape(raw),
    ...(readDigests(raw.digests) === undefined ? {} : { digests: readDigests(raw.digests) }),
    ...(readSource(raw.source) === undefined ? {} : { source: readSource(raw.source) }),
    extras: Array.isArray(raw.extras) ? raw.extras.filter((entry): entry is string => typeof entry === "string") : [],
    wheelCount: typeof raw.wheelCount === "number" ? raw.wheelCount : 0,
    ...(typeof raw.sizeBytes === "number" ? { sizeBytes: raw.sizeBytes } : {}),
    ...(typeof raw.builtAt === "string" ? { builtAt: raw.builtAt } : {}),
  };

  // A PACKAGED app must never accept a manifest that cannot be trusted. Without
  // this, a digestless-but-parseable manifest was "valid", the same-tag stamp
  // then said no install was needed, and an already-done Hermes step kept the
  // old network engine in place (review r3, finding 6).
  if (options.requireTrust === true) {
    if (manifest.digests === undefined) {
      throw new Error("The bundled engine payload manifest carries no content checksums.");
    }
    if (manifest.digests.file !== "digests.sha256") {
      throw new Error("The bundled engine payload manifest points its checksums at an unexpected file.");
    }
    if (!/^[0-9a-f]{64}$/.test(manifest.digests.sha256)) {
      throw new Error("The bundled engine payload manifest has an invalid checksum pin.");
    }
    if (manifest.source === undefined) {
      throw new Error("The bundled engine payload manifest does not record where its engine came from.");
    }
  }
  return manifest;
}

function readVariants(value: unknown): Record<string, string> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const variants: Record<string, string> = {};
  for (const [arch, executable] of Object.entries(value as Record<string, unknown>)) {
    if (typeof executable === "string" && executable !== "") variants[arch] = executable;
  }
  return Object.keys(variants).length === 0 ? undefined : variants;
}

function readDigests(value: unknown): VendorManifest["digests"] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  if (typeof raw.file !== "string" || typeof raw.sha256 !== "string") return undefined;
  return {
    algorithm: typeof raw.algorithm === "string" ? raw.algorithm : "sha256",
    file: raw.file,
    sha256: raw.sha256,
    ...(typeof raw.fileCount === "number" ? { fileCount: raw.fileCount } : {}),
  };
}

function readSource(value: unknown): VendorManifest["source"] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  if (typeof raw.kind !== "string" || raw.kind === "") return undefined;
  return {
    kind: raw.kind,
    ...(typeof raw.commit === "string" ? { commit: raw.commit } : {}),
    ...(typeof raw.tarballSha256 === "string" ? { tarballSha256: raw.tarballSha256 } : {}),
  };
}

/**
 * A payload installs its libraries one of two ways, and must declare exactly
 * one: `site` (already installed, one tree per architecture — macOS) or
 * `engineWheel` + `requirements` (a wheelhouse to pip-install — Windows).
 *
 * Declaring neither is a half-built payload; declaring both would leave the
 * installer to guess, and guessing about WHERE a customer's engine comes from
 * is precisely what this lane exists to stop.
 */
function readInstallShape(raw: Record<string, unknown>): Pick<VendorManifest, "engineWheel" | "requirements" | "site"> {
  const site = readVariants(raw.site);
  const hasWheelhouse = typeof raw.engineWheel === "string" && typeof raw.requirements === "string";
  if (site !== undefined && hasWheelhouse) {
    throw new Error(
      "The bundled engine payload manifest claims both a pre-installed library tree and a wheelhouse.",
    );
  }
  if (site !== undefined) {
    if (Object.keys(site).length === 0) {
      throw new Error("The bundled engine payload manifest has an empty site entry.");
    }
    return { site };
  }
  return {
    engineWheel: requireString(raw.engineWheel, "engineWheel"),
    requirements: requireString(raw.requirements, "requirements"),
  };
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== "string" || value === "") {
    throw new Error(`The bundled engine payload manifest is missing ${field}.`);
  }
  return value;
}

/**
 * Manifest paths are POSIX-relative by contract; resolve them against the
 * payload root with the platform's own separator. Anything that escapes the
 * payload root is rejected: the manifest ships inside our own installer, but a
 * path traversal here would write outside the engine dir.
 */
/** Closed set of ways the digest verification can refuse a payload. */
export type PayloadDigestFailureKind =
  | "no-digests"
  | "bad-algorithm"
  | "checksum-list-missing"
  | "checksum-list-mismatch"
  | "checksum-list-malformed"
  | "unsafe-path"
  | "duplicate-entry"
  | "outside-payload"
  | "file-missing"
  | "file-unreadable"
  | "file-modified"
  | "count-mismatch"
  | "extra-files"
  | "payload-unreadable";

/**
 * A digest failure that KNOWS what refused it. Support gets three payload-
 * corrupt reports from one customer and needs to know whether it is the same
 * file every time (antivirus quarantining one binary) or scattered (disk /
 * damaged download) — the message alone reached the screen but never the
 * copied diagnostics (2026-08-15, Justin Curnutt 3× repro).
 */
export class PayloadDigestError extends Error {
  /**
   * How many payload files refused in total, when the whole manifest was
   * scanned. "1 file missing" and "800 files missing from the archive tail"
   * are different diseases — a targeted antivirus removal versus an aborted
   * extraction — and fail-fast reporting could not tell support which one it
   * was looking at (Bryan Proctor, 2026-08-16: artifact verified complete,
   * disk healthy, one file genuinely absent, and no way to prove that
   * remotely).
   */
  failureCount?: number;
  /** The OS error code behind a `file-unreadable` refusal (EACCES, EPERM, EBUSY…). */
  errorCode?: string;

  constructor(
    message: string,
    readonly kind: PayloadDigestFailureKind,
    readonly relativeFile?: string,
    details: { failureCount?: number; errorCode?: string } = {},
  ) {
    super(message);
    this.name = "PayloadDigestError";
    if (details.failureCount !== undefined) this.failureCount = details.failureCount;
    if (details.errorCode !== undefined) this.errorCode = details.errorCode;
  }
}

/**
 * ENOENT means the file is GONE; anything else means it is there and we were
 * not allowed to read it — a scan lock, a filter-driver denial, a permission
 * problem (EPERM/EACCES/EBUSY). v1.5.0 reported both as `file-missing`, so the
 * prop could not tell an antivirus DELETION from an antivirus BLOCK, which is
 * exactly the discriminator support needed on a box whose exclusions were
 * already correct (Bryan Proctor's 1.5.0 telemetry, 2026-08-16).
 */
/**
 * Only ENOENT means GONE (Codex review r1, P3): an error we cannot identify is
 * an error we cannot identify, and telling support a file is missing when all
 * we know is that reading it failed is exactly the false precision this split
 * was added to remove. Every real filesystem error carries a code.
 */
export function errorCodeOf(error: unknown): string | undefined {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === "string" && code !== "" ? code : undefined;
}

function readFailure(
  error: unknown,
  relative: string,
): { kind: "file-missing" | "file-unreadable"; message: string; errorCode?: string } {
  const code = errorCodeOf(error);
  if (code === "ENOENT") {
    return { kind: "file-missing", message: `The bundled engine payload is missing ${relative}.` };
  }
  return {
    kind: "file-unreadable",
    message: `The bundled engine payload file ${relative} is on this computer but could not be read.`,
    errorCode: code,
  };
}

/**
 * Verifies the payload's CONTENTS before a single file of it is used.
 *
 * Chain: the manifest holds the digest of `digests.sha256`; that file holds a
 * digest for every other file. Verifying both ends means a corrupt wheel, a
 * substituted interpreter or a truncated copy is caught at install time on the
 * customer's machine — not merely at build time on ours (review r1, #6/#12).
 *
 * Returns the number of files checked, or throws a PayloadDigestError with a
 * customer-facing message naming the first file that failed.
 */
export async function verifyPayloadDigests(
  vendorRoot: string,
  manifest: VendorManifest,
  io: {
    readFile: (filePath: string) => Promise<Buffer>;
    hash: (data: Buffer) => string;
    /** Every file in the payload, posix-relative — for the extras sweep. */
    listFiles?: (root: string) => Promise<string[]>;
    /** Resolves symlinks, so a listed file cannot point outside the payload. */
    realpath?: (filePath: string) => Promise<string>;
  },
): Promise<number> {
  const digests = manifest.digests;
  if (digests === undefined) {
    throw new PayloadDigestError("The bundled engine payload carries no content digests, so it cannot be trusted.", "no-digests");
  }
  if (digests.algorithm !== "sha256") {
    throw new PayloadDigestError(`The bundled engine payload uses an unsupported digest algorithm (${digests.algorithm}).`, "bad-algorithm");
  }
  const digestPath = path.resolve(vendorRoot, ...digests.file.split("/"));
  let listing: Buffer;
  try {
    listing = await io.readFile(digestPath);
  } catch (error) {
    // The KIND stays as it is — consumers and support docs read it — but the
    // errno rides along, so "the list is gone" and "this computer would not
    // let us read the list" stop looking identical in diagnostics (Codex
    // review r2, P3).
    const code = errorCodeOf(error);
    throw new PayloadDigestError(
      code === undefined || code === "ENOENT"
        ? "The bundled engine payload is missing its checksum list."
        : "The bundled engine payload's checksum list is on this computer but could not be read.",
      "checksum-list-missing",
      undefined,
      { ...(code === undefined ? {} : { errorCode: code }) },
    );
  }
  if (io.hash(listing) !== digests.sha256) {
    throw new PayloadDigestError("The bundled engine payload's checksum list does not match the manifest.", "checksum-list-mismatch");
  }

  // The runtime applies the SAME rules as the release verifier (review r2,
  // finding 9). A payload that reached a customer intact-but-modified — an
  // added file, a case-collision on a case-insensitive disk, a symlink aimed
  // out of the payload — must fail here too, not only on our build machine.
  const boundary = path.resolve(vendorRoot) + path.sep;
  // BOTH sides resolved (review r3, finding 9). A payload under a symlinked
  // path — /tmp -> /private/tmp on macOS, a redirected home, a junction — would
  // otherwise have every one of its files look like an escape.
  let realBoundary = boundary;
  if (io.realpath !== undefined) {
    try {
      realBoundary = path.resolve(await io.realpath(vendorRoot)) + path.sep;
    } catch {
      throw new PayloadDigestError("The bundled engine payload folder could not be read.", "payload-unreadable");
    }
  }
  const listed = new Set<string>();
  const caseFolded = new Set<string>();
  let checked = 0;
  /**
   * PER-FILE refusals are collected, not thrown on sight: the whole manifest
   * is scanned so the report can carry HOW MANY files refused alongside the
   * first one. Structural and security refusals above and below this loop
   * still fail fast — a checksum list that is malformed, self-contradictory,
   * or aiming outside the payload is not a damage report to be counted, and
   * walking further into a payload that points outside itself buys nothing.
   */
  const failures: PayloadDigestError[] = [];
  for (const line of listing.toString("utf8").split(/\r?\n/)) {
    if (line.trim() === "") continue;
    const match = /^([0-9a-f]{64})\s+(.+)$/.exec(line.trim());
    if (match === null) {
      throw new PayloadDigestError("The bundled engine payload's checksum list is malformed.", "checksum-list-malformed");
    }
    const [, expected, relative] = match;
    if (relative.includes("\\") || relative.startsWith("/") || /(^|\/)\.\.(\/|$)/.test(relative)) {
      throw new PayloadDigestError(`The bundled engine payload's checksum list has an unsafe path: ${relative}.`, "unsafe-path", relative);
    }
    if (caseFolded.has(relative.toLowerCase())) {
      // Two entries differing only by case cannot both exist on Windows or a
      // default macOS volume — one would silently shadow the other.
      throw new PayloadDigestError(`The bundled engine payload lists ${relative} twice.`, "duplicate-entry", relative);
    }
    caseFolded.add(relative.toLowerCase());
    listed.add(relative);

    const target = path.resolve(vendorRoot, ...relative.split("/"));
    if (!target.startsWith(boundary)) {
      throw new PayloadDigestError(`The bundled engine payload's checksum list points outside the payload: ${relative}.`, "outside-payload", relative);
    }
    if (io.realpath !== undefined) {
      let resolvedTarget: string | undefined;
      try {
        resolvedTarget = path.resolve(await io.realpath(target));
      } catch (error) {
        const refusal = readFailure(error, relative);
        failures.push(new PayloadDigestError(refusal.message, refusal.kind, relative, {
          ...(refusal.errorCode === undefined ? {} : { errorCode: refusal.errorCode }),
        }));
        continue;
      }
      if (!resolvedTarget.startsWith(realBoundary)) {
        // Security, not damage: fail fast (see the `failures` note above).
        throw new PayloadDigestError(`The bundled engine payload file ${relative} points outside the payload.`, "outside-payload", relative);
      }
    }
    let contents: Buffer;
    try {
      contents = await io.readFile(target);
    } catch (error) {
      const refusal = readFailure(error, relative);
      failures.push(new PayloadDigestError(refusal.message, refusal.kind, relative, {
        ...(refusal.errorCode === undefined ? {} : { errorCode: refusal.errorCode }),
      }));
      continue;
    }
    if (io.hash(contents) !== expected) {
      // No exceptions, on any platform, packaged or not. The payload's own
      // binaries are signed BEFORE its digests are computed (see
      // hud/scripts/build-vendor-payload.mjs), and electron-builder is told
      // not to re-sign them, so the bytes that ship are the bytes that were
      // hashed. A mismatch is a mismatch.
      failures.push(new PayloadDigestError(`The bundled engine payload file ${relative} is damaged or has been modified.`, "file-modified", relative));
      continue;
    }
    checked += 1;
  }

  // The FIRST refusal keeps the kind, the file and the message it has always
  // had — support threads and telemetry already read those. What is new is
  // the scope: how many files refused in total, said plainly on screen when
  // it is more than one.
  if (failures.length > 0) {
    const first = failures[0];
    const message = failures.length === 1
      ? first.message
      : `${first.message} ${failures.length} payload files could not be verified in total.`;
    throw new PayloadDigestError(message, first.kind, first.relativeFile, {
      failureCount: failures.length,
      ...(first.errorCode === undefined ? {} : { errorCode: first.errorCode }),
    });
  }

  if (digests.fileCount !== undefined && checked !== digests.fileCount) {
    throw new PayloadDigestError(
      `The bundled engine payload lists ${checked} files but its manifest claims ${digests.fileCount}.`,
      "count-mismatch",
    );
  }

  // Extras sweep: a file nobody vouched for is as dangerous as a modified one.
  if (io.listFiles !== undefined) {
    const ignored = new Set([manifest.digests?.file ?? "digests.sha256", VENDOR_MANIFEST_FILE]);
    // Typed like every other refusal (review r3 P2): a plain filesystem error
    // here would reach diagnostics without payload_failure_kind, defeating
    // the support detail for exactly the unreadable-directory case.
    let allFiles: string[];
    try {
      allFiles = await io.listFiles(vendorRoot);
    } catch {
      throw new PayloadDigestError("The bundled engine payload folder could not be read.", "payload-unreadable");
    }
    const extras = allFiles.filter(
      (entry) => !listed.has(entry) && !ignored.has(entry),
    );
    if (extras.length > 0) {
      throw new PayloadDigestError(
        `The bundled engine payload contains ${extras.length} file(s) that are not in its checksum list, starting with ${extras[0]}.`,
        "extra-files",
        extras[0],
      );
    }
  }
  return checked;
}

export function resolveVendorPayload(
  vendorRoot: string,
  manifest: VendorManifest,
  arch: string = process.arch,
): VendorPayload {
  const inside = (relative: string, field: string): string => {
    const resolved = path.resolve(vendorRoot, ...relative.split("/"));
    const boundary = path.resolve(vendorRoot) + path.sep;
    if (!resolved.startsWith(boundary)) {
      throw new Error(`The bundled engine payload manifest points ${field} outside the payload.`);
    }
    return resolved;
  };
  // A universal mac payload carries one interpreter per architecture; running
  // the wrong one is not a subtle failure, it simply cannot execute.
  const pythonRelative = manifest.python.variants?.[arch] ?? manifest.python.executable;
  // The runtime directory is the first segment of the interpreter path, by the
  // same contract that produced it: "python/bin/python3" -> "python",
  // "python-arm64/bin/python3" -> "python-arm64".
  const pythonDirRelative = pythonRelative.split("/")[0] ?? "python";
  const siteRelative = manifest.site?.[arch];
  return {
    root: vendorRoot,
    manifest,
    python: inside(pythonRelative, "python.executable"),
    pythonDir: inside(pythonDirRelative, "python directory"),
    source: path.join(vendorRoot, "hermes-agent"),
    wheelhouse: path.join(vendorRoot, "wheelhouse"),
    // A site payload has no wheelhouse to point at; these stay resolvable so
    // every existing caller keeps type-checking, and the install path branches
    // on `site` rather than on whether a string happens to be empty.
    requirements: manifest.requirements === undefined
      ? path.join(vendorRoot, "wheelhouse", "requirements.txt")
      : inside(manifest.requirements, "requirements"),
    engineWheel: manifest.engineWheel === undefined
      ? path.join(vendorRoot, "wheelhouse")
      : inside(manifest.engineWheel, "engineWheel"),
    ...(siteRelative === undefined ? {} : { site: inside(siteRelative, `site.${arch}`) }),
  };
}

export function vendorStampPath(hermesAgentDir: string, platform: NodeJS.Platform = process.platform): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return pathApi.join(hermesAgentDir, VENDOR_STAMP_FILE);
}

export async function readVendorStamp(
  hermesAgentDir: string,
  platform: NodeJS.Platform = process.platform,
): Promise<VendorStamp | undefined> {
  const text = await readTextIfExists(vendorStampPath(hermesAgentDir, platform));
  if (text === undefined) return undefined;
  try {
    const parsed = JSON.parse(text) as Partial<VendorStamp>;
    if (typeof parsed.engineTag !== "string" || parsed.engineTag === "") return undefined;
    return {
      engineTag: parsed.engineTag,
      engineVersion: typeof parsed.engineVersion === "string" ? parsed.engineVersion : "",
      installedAt: typeof parsed.installedAt === "string" ? parsed.installedAt : "",
      ...(typeof parsed.payloadDigest === "string" ? { payloadDigest: parsed.payloadDigest } : {}),
    };
  } catch {
    return undefined;
  }
}
