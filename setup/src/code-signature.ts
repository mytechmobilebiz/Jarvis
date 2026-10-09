import { execFile } from "node:child_process";

/**
 * Code-signature verification for the executables JARVIS ships beside itself.
 *
 * Why this exists: packaging SIGNS the voice executables, which rewrites them —
 * so their SHA-256 no longer matches the manifest staged at build time, and a
 * signed release reported its own payload as `assets-corrupt` (voice dead for
 * every customer, blamed on antivirus). Hashing cannot survive signing, and
 * dropping the check for those files would leave the most dangerous files in
 * the payload — the ones we execute — unprotected.
 *
 * So for those files, and ONLY when their hash no longer matches, the operating
 * system's own signature becomes the evidence. It is a stronger guarantee than
 * our hash: on macOS the kernel refuses to execute a broken signature under the
 * hardened runtime, and the check below additionally PINS the identity.
 *
 * Pinning matters more than validity: `codesign --verify` is happy with
 * anything validly signed by ANYONE, including an attacker's own Developer ID.
 * A swapped binary that is validly signed by someone else must fail, so the
 * Team Identifier is compared against ours.
 */

/** Our Apple Team ID. A binary signed by anyone else is not ours. */
export const JARVIS_MAC_TEAM_ID = "N96E5LWA75";

/**
 * Apple's documented Developer ID requirement, with our team pinned into it.
 *
 * The previous version read the TeamIdentifier that `codesign -d` DISPLAYS and
 * compared it to ours. That proves a signature is internally consistent, not
 * that Apple issued it: a self-signed certificate whose subject carries
 * `OU=N96E5LWA75` displays exactly the same TeamIdentifier and would have
 * passed (integration review r2, finding 3).
 *
 * Requirement language is evaluated by the OS against the CHAIN:
 *  - `anchor apple generic` — the chain terminates at Apple's root.
 *  - `certificate 1[field.1.2.840.113635.100.6.2.6]` — the intermediate is the
 *    Developer ID Certification Authority.
 *  - `certificate leaf[field.1.2.840.113635.100.6.1.13]` — the leaf is a
 *    Developer ID Application certificate (not, say, a Mac App Store one).
 *  - `certificate leaf[subject.OU] = <team>` — and it is OUR team's.
 *
 * Source: Apple TN3127 "Inside Code Signing: Requirements", which gives this
 * exact conjunction as the Developer ID requirement.
 */
export function developerIdRequirement(
  teamId: string = JARVIS_MAC_TEAM_ID,
  identifier?: string,
): string {
  return "anchor apple generic"
    + " and certificate 1[field.1.2.840.113635.100.6.2.6]"
    + " and certificate leaf[field.1.2.840.113635.100.6.1.13]"
    + ` and certificate leaf[subject.OU] = ${teamId}`
    // The signing identifier is baked into the signature, so it names WHICH of
    // our binaries this is. Without it, any other binary we ever signed could
    // be renamed `whisper-cli` and inherit its permission to run (review r3,
    // P3: the allowlist was basename-only).
    + (identifier === undefined ? "" : ` and identifier "${identifier}"`);
}

/**
 * The ONLY executables a signature may vouch for, by name.
 *
 * Pinning the closed set means a different binary — even one legitimately
 * signed by us — cannot be dropped in as `whisper-cli` and inherit its
 * permission to run (review r2, finding 3, "prefer also pinning expected
 * identifiers or closed executable paths").
 */
export const SIGNABLE_EXECUTABLES = new Map<string, string | undefined>([
  // basename → the signing identifier codesign records for it. macOS derives
  // these from the file name at signing time, so they are stable across builds
  // and different for any other binary.
  ["whisper-cli", "whisper-cli"],
  ["sherpa-onnx-offline-tts", "sherpa-onnx-offline-tts"],
  ["libonnxruntime.dylib", "libonnxruntime"],
  // Windows helpers: hash-verified today (no certificate yet), so no
  // identifier to pin until the Authenticode branch exists.
  ["whisper-cli.exe", undefined],
  ["sherpa-onnx-offline-tts.exe", undefined],
  ["onnxruntime.dll", undefined],
  ["onnxruntime_providers_shared.dll", undefined],
]);

export type SignatureVerdict =
  /** Validly signed by US. The only verdict that may stand in for a hash. */
  | { state: "ours" }
  /** Signed, valid, but by someone else — the swap this pin exists to catch. */
  | { state: "foreign"; detail: string }
  /** No signature, a broken one, or no way to check on this platform. */
  | { state: "unverified"; detail: string };

export interface SignatureCheckOptions {
  platform?: NodeJS.Platform;
  teamId?: string;
  /** Pins WHICH of our binaries this must be; see developerIdRequirement. */
  identifier?: string;
  /** Test seam: replaces the OS calls. */
  run?: (command: string, args: string[]) => Promise<{ status: number; stdout: string; stderr: string }>;
}

export async function verifyCodeSignature(
  filePath: string,
  options: SignatureCheckOptions = {},
): Promise<SignatureVerdict> {
  const platform = options.platform ?? process.platform;
  const run = options.run ?? runCommand;
  if (platform === "darwin") {
    return verifyMac(filePath, options.teamId ?? JARVIS_MAC_TEAM_ID, run, options.identifier);
  }
  if (platform === "win32") return verifyWindows();
  return { state: "unverified", detail: `no signature verification on ${platform}` };
}

async function verifyMac(
  filePath: string,
  teamId: string,
  run: NonNullable<SignatureCheckOptions["run"]>,
  identifier?: string,
): Promise<SignatureVerdict> {
  // ONE call decides it. --strict proves the file is intact under its own
  // signature (the hash check's job, done by the OS); -R makes the OS evaluate
  // the certificate CHAIN against Apple's documented Developer ID requirement
  // with our team pinned in. Nothing this function reads afterwards can widen
  // that answer — the remaining calls only describe a decision already made.
  const requirement = developerIdRequirement(teamId, identifier);
  const verified = await run("codesign", ["--verify", "--strict", `-R=${requirement}`, filePath])
    .catch(() => undefined);
  if (verified === undefined) return { state: "unverified", detail: "codesign could not be run" };
  if (verified.status === 0) return { state: "ours" };

  // It failed. The verdict is already "not ours"; the only question left is
  // what to TELL the customer, and that is diagnostics — never a second chance.
  const described = await run("codesign", ["-d", "--verbose=2", filePath]).catch(() => undefined);
  const text = `${described?.stdout ?? ""}\n${described?.stderr ?? ""}`;
  const displayed = /^TeamIdentifier=(\S+)$/m.exec(text)?.[1];
  if (displayed !== undefined && displayed !== "not" && displayed !== teamId) {
    return { state: "foreign", detail: `signed by team ${displayed}, not ${teamId}` };
  }
  if (displayed === teamId) {
    if (identifier !== undefined) {
      // Distinguish "not ours at all" from "ours, but not THIS helper" — a
      // real binary of ours renamed to another helper's filename. Both are
      // refused; only the sentence differs, and support needs the difference.
      const withoutIdentifier = await run(
        "codesign",
        ["--verify", "--strict", `-R=${developerIdRequirement(teamId)}`, filePath],
      ).catch(() => undefined);
      if (withoutIdentifier?.status === 0) {
        return {
          state: "foreign",
          detail: `signed by us, but it is not ${identifier} — a different program under that name`,
        };
      }
    }
    // The dangerous case, and the reason this check is chain-based: the
    // signature CLAIMS our team but does not chain to Apple as a Developer ID
    // certificate. A self-signed certificate with OU=<our team> looks exactly
    // like this.
    return {
      state: "foreign",
      detail: `claims team ${teamId} but does not chain to an Apple-issued Developer ID certificate`,
    };
  }
  return {
    state: "unverified",
    detail: "no signature that meets the Apple Developer ID requirement for our team",
  };
}

/**
 * Windows: hash mode until a certificate is configured.
 *
 * The seam is here so the switch is one function when the Certum cert lands —
 * Authenticode validity plus a pinned subject/thumbprint, via
 * Get-AuthenticodeSignature. Until then the shipped binaries are unsigned,
 * their hashes still match the manifest, and returning "unverified" keeps the
 * verifier on the hash it can actually check.
 */
function verifyWindows(): SignatureVerdict {
  return { state: "unverified", detail: "no Windows signing certificate is configured yet" };
}

async function runCommand(
  command: string,
  args: string[],
): Promise<{ status: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    execFile(command, args, { encoding: "utf8" }, (error, stdout, stderr) => {
      const status = error === null ? 0 : ((error as NodeJS.ErrnoException & { code?: number }).code ?? 1);
      resolve({ status: typeof status === "number" ? status : 1, stdout, stderr });
    });
  });
}
