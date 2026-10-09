/**
 * The payload digest chain, in ONE place.
 *
 * `digests.sha256` is a FIXED contract between three programs: the vendoring
 * build that writes it (build-vendor-payload.mjs), the release verifier that
 * re-checks it (verify-vendor-payload.mjs), and the installer that verifies it
 * on the customer's machine (setup/src/vendor.ts). Format: `<sha256 hex>  <posix
 * relative path>`, sorted by path, LF-terminated, excluding only the manifest
 * and the digest file themselves.
 *
 * These helpers used to live inside the build script. They moved here when
 * v1.5.1 needed to REWRITE the chain of an already-built payload (the distlib
 * stub strip, without re-vendoring the engine): two implementations of "which
 * files are in the payload and in what order" would be two chances to write a
 * payload that verifies on our machine and not on a customer's.
 */

import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const throwing = (message) => {
  throw new Error(message);
};

/** sha256 of a file's contents (follows symlinks — see walkDigestFiles). */
export function sha256File(absPath) {
  return createHash("sha256").update(fs.readFileSync(absPath)).digest("hex");
}

/** Recursive byte size of a file or directory; missing paths count as 0. */
export function dirSize(p) {
  let total = 0;
  const st = fs.statSync(p, { throwIfNoEntry: false });
  if (!st) return 0;
  if (st.isFile()) return st.size;
  for (const entry of fs.readdirSync(p, { withFileTypes: true })) {
    const child = path.join(p, entry.name);
    if (entry.isDirectory()) total += dirSize(child);
    else if (entry.isFile()) total += fs.statSync(child).size;
    // symlinks: PBS mac runtimes contain them; counting the link itself as 0
    // (lstat) is fine for a *report*; extraction preserved them via tar.
  }
  return total;
}

/**
 * Every file in the payload, as sorted posix-relative paths, for digesting.
 * Symlinks to FILES are listed (their followed content gets hashed — the same
 * bytes hash the same at install time, so the chain stays consistent whether
 * or not the archive round-trip preserved the link) but their target must
 * resolve INSIDE the payload root: a link reaching outside would be digested
 * as whatever foreign bytes it points at today and installed as something
 * else tomorrow. Dangling or directory symlinks are refused outright: a
 * payload we cannot verify per-file is a payload we do not ship.
 *
 * The final list must also be unique under case folding (review r2 finding
 * 9): `A.whl` and `a.whl` are distinct on the Linux CI filesystem that built
 * the payload but the SAME file on the customer's Windows/macOS disk — such
 * a payload can never verify after extraction, so it must not be built.
 */
export function walkDigestFiles(root, fail = throwing) {
  const rootReal = fs.realpathSync(root);
  const out = [];
  const walk = (dir, prefix) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
      const abs = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) {
        const st = fs.statSync(abs, { throwIfNoEntry: false });
        if (!st) fail(`Dangling symlink in payload: ${relative} — refusing to digest a broken payload`);
        if (st.isDirectory()) fail(`Directory symlink in payload: ${relative} — not per-file verifiable, refusing`);
        const real = fs.realpathSync(abs);
        const relToRoot = path.relative(rootReal, real);
        if (relToRoot.startsWith("..") || path.isAbsolute(relToRoot)) {
          fail(
            `Symlink escapes the payload root: ${relative} -> ${real} — ` +
            `refusing to digest bytes that live outside the payload`,
          );
        }
        out.push(relative);
      } else if (entry.isDirectory()) {
        walk(abs, relative);
      } else if (entry.isFile()) {
        out.push(relative);
      } else {
        fail(`Unsupported file type in payload: ${relative}`);
      }
    }
  };
  walk(root, "");
  out.sort();
  const folded = new Map();
  for (const rel of out) {
    const key = rel.toLowerCase();
    const prev = folded.get(key);
    if (prev !== undefined) {
      fail(
        `Case-fold collision in payload: "${prev}" vs "${rel}" — these are one ` +
        `file on a case-insensitive filesystem (Windows/macOS), so the payload ` +
        `could never verify after install. Refusing to build it.`,
      );
    }
    folded.set(key, rel);
  }
  return out;
}

/** The digest-file name every payload uses; the manifest must point at it. */
export const DIGESTS_FILE = "digests.sha256";
export const MANIFEST_FILE = "vendor-manifest.json";

/**
 * Rewrites `digests.sha256` from what is on disk RIGHT NOW and returns the
 * facts the manifest has to pin (its own sha256, and the file count). The
 * manifest and the digest file are never digested — the chain runs
 * manifest -> digests.sha256 -> every other byte.
 */
export function writeDigestFile(payloadRoot, fail = throwing) {
  const files = walkDigestFiles(payloadRoot, fail)
    .filter((relative) => relative !== DIGESTS_FILE && relative !== MANIFEST_FILE);
  let bytes = 0;
  const lines = files.map((relative) => {
    const abs = path.join(payloadRoot, ...relative.split("/"));
    bytes += fs.statSync(abs).size;
    return `${sha256File(abs)}  ${relative}`;
  });
  const digestsPath = path.join(payloadRoot, DIGESTS_FILE);
  fs.writeFileSync(digestsPath, lines.join("\n") + "\n", "utf8");
  return { sha256: sha256File(digestsPath), fileCount: files.length, bytes };
}
