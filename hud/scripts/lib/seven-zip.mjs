/**
 * Finding and running 7-Zip, in ONE place.
 *
 * Two gates need it: `verify-win-artifact.mjs` (PE header of the packed
 * `JARVIS HUD.exe`) and `verify-win-payload-archive.mjs` (every byte of the
 * packed payload actually decompresses). It moved here when the second one was
 * written, rather than being copied — the discovery order below encodes real
 * knowledge (electron-builder's own cached 7za is the one that certainly
 * matches the archive it wrote), and two copies of it would drift.
 *
 * Everything here is verbatim from verify-win-artifact.mjs, where it lived
 * alone since v1.4.0.
 */

import { readdir } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { spawnSync } from "node:child_process";

async function cachedSevenZipCandidates(cacheRoot) {
  const toolsetRoot = join(cacheRoot, "7zip@1.0.0");
  let entries;

  try {
    entries = await readdir(toolsetRoot, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") {
      return [];
    }
    throw error;
  }

  const names = process.platform === "win32"
    ? ["7za.exe", "7zz.exe"]
    : ["7za", "7zz"];
  const candidates = [];

  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue;
    }
    for (const name of names) {
      candidates.push(join(toolsetRoot, entry.name, "bin", name));
    }
  }

  return candidates;
}

function defaultCacheRoots() {
  const roots = [];
  const configuredCache = process.env.ELECTRON_BUILDER_CACHE?.trim();

  if (configuredCache && isAbsolute(configuredCache)) {
    roots.push(configuredCache);
  }

  if (process.platform === "darwin") {
    roots.push(join(homedir(), "Library", "Caches", "electron-builder"));
  } else if (process.platform === "win32") {
    if (process.env.LOCALAPPDATA) {
      roots.push(join(process.env.LOCALAPPDATA, "electron-builder", "Cache"));
    }
    roots.push(join(tmpdir(), "electron-builder-cache"));
  } else {
    roots.push(
      process.env.XDG_CACHE_HOME
        ? join(process.env.XDG_CACHE_HOME, "electron-builder")
        : join(homedir(), ".cache", "electron-builder"),
    );
  }

  return [...new Set(roots)];
}

function canRunSevenZip(candidate) {
  const result = spawnSync(candidate, ["i"], {
    stdio: "ignore",
    windowsHide: true,
  });
  return !result.error && result.status === 0;
}

export async function findSevenZip() {
  const candidates = [];
  const configuredBinary = process.env.ELECTRON_BUILDER_7ZIP_PATH?.trim();

  if (configuredBinary) {
    candidates.push(configuredBinary);
  }

  for (const cacheRoot of defaultCacheRoots()) {
    candidates.push(...await cachedSevenZipCandidates(cacheRoot));
  }

  candidates.push(
    ...(process.platform === "win32"
      ? ["7za.exe", "7zz.exe", "7z.exe"]
      : ["7za", "7zz", "7z"]),
  );

  for (const candidate of [...new Set(candidates)]) {
    if (canRunSevenZip(candidate)) {
      return candidate;
    }
  }

  throw new Error(
    "7-Zip was not found. Run electron-builder first, or set " +
      "ELECTRON_BUILDER_7ZIP_PATH to its 7za/7zz executable.",
  );
}

export function runSevenZip(sevenZip, args, context) {
  const result = spawnSync(sevenZip, args, {
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    windowsHide: true,
  });

  if (!result.error && result.status === 0) {
    return result.stdout ?? "";
  }

  const output = [result.stdout, result.stderr]
    .filter(Boolean)
    .join("\n")
    .trim()
    .split(/\r?\n/)
    .slice(-8)
    .join("\n");
  const reason = result.error?.message ?? `7-Zip exited with code ${result.status}`;
  throw new Error(`${context}: ${reason}${output ? `\n${output}` : ""}`);
}
