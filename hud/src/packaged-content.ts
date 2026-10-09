import fs from "node:fs/promises";
import path from "node:path";
import { PACKAGED_CONTENT_DIRECTORY } from "./resources.js";

const CONTENT_MARKER = "jarvis-content.json";
const MANAGED_CONTENT_ENTRIES = ["agents", "playbooks", "demo", "examples", "slack", CONTENT_MARKER] as const;
const REQUIRED_DIRECTORIES = ["agents", "playbooks", "demo", "examples", "slack"] as const;
/**
 * A present-but-empty directory used to satisfy the check above, so a staged
 * copy could look current while the file the Slack walkthrough sends buyers to
 * was absent. The files that customer-facing instructions name are asserted.
 */
const REQUIRED_FILES = [["slack", "jarvis-manifest.json"]] as const;

interface ContentMarker {
  name: string;
  version: string;
}

export interface PreparedPackagedContent {
  repoRoot: string;
  refreshed: boolean;
}

/**
 * Materialize the immutable content bundled beside app.asar under Hermes.
 *
 * Only the five product-owned entries above are replaced. Profiles, runtime
 * state, and even unrelated files placed directly in jarvis-content are left
 * alone. jarvis-content.json is moved last and acts as the commit marker: an
 * interrupted or older copy is refreshed on the next setup run.
 */
export async function preparePackagedContent(
  bundledRoot: string,
  hermesRoot: string,
): Promise<PreparedPackagedContent> {
  const sourceMarker = await readMarker(bundledRoot);
  await validateContentDirectories(bundledRoot);

  const repoRoot = path.join(hermesRoot, PACKAGED_CONTENT_DIRECTORY);
  if (await contentIsCurrent(repoRoot, sourceMarker)) {
    return { repoRoot, refreshed: false };
  }

  await fs.mkdir(hermesRoot, { recursive: true });
  const stagingParent = await fs.mkdtemp(path.join(hermesRoot, ".jarvis-content-stage-"));
  const stagingRoot = path.join(stagingParent, PACKAGED_CONTENT_DIRECTORY);

  try {
    await fs.mkdir(stagingRoot);
    for (const entry of MANAGED_CONTENT_ENTRIES) {
      await fs.cp(path.join(bundledRoot, entry), path.join(stagingRoot, entry), {
        preserveTimestamps: true,
        recursive: true,
      });
    }

    await fs.mkdir(repoRoot, { recursive: true });
    // Invalidate before mutating: the swap below replaces entries one at a
    // time, and a failure part-way (a Windows file lock on a live entry, say)
    // would otherwise leave a mixed old/new tree still vouched for by the old
    // marker. With no marker present, the mixture can never read as current
    // and the next launch restages from scratch.
    await fs.rm(path.join(repoRoot, CONTENT_MARKER), { force: true });
    // The marker moves last so a partial refresh can never look current.
    for (const entry of MANAGED_CONTENT_ENTRIES) {
      const destination = path.join(repoRoot, entry);
      await fs.rm(destination, { force: true, recursive: true });
      await fs.rename(path.join(stagingRoot, entry), destination);
    }
  } finally {
    await fs.rm(stagingParent, { force: true, recursive: true });
  }

  return { repoRoot, refreshed: true };
}

async function contentIsCurrent(repoRoot: string, expected: ContentMarker): Promise<boolean> {
  try {
    const actual = await readMarker(repoRoot);
    if (actual.name !== expected.name || actual.version !== expected.version) return false;
    await validateContentDirectories(repoRoot);
    return true;
  } catch {
    return false;
  }
}

async function readMarker(root: string): Promise<ContentMarker> {
  const raw = JSON.parse(await fs.readFile(path.join(root, CONTENT_MARKER), "utf8")) as {
    name?: unknown;
    version?: unknown;
  };
  if (typeof raw.name !== "string" || raw.name === "" || typeof raw.version !== "string" || raw.version === "") {
    throw new Error(`Packaged JARVIS content has no valid name/version marker at ${root}.`);
  }
  return { name: raw.name, version: raw.version };
}

async function validateContentDirectories(root: string): Promise<void> {
  for (const entry of REQUIRED_DIRECTORIES) {
    const stat = await fs.stat(path.join(root, entry));
    if (!stat.isDirectory()) {
      throw new Error(`Packaged JARVIS content entry is not a directory: ${path.join(root, entry)}`);
    }
  }
  for (const entry of REQUIRED_FILES) {
    const stat = await fs.stat(path.join(root, ...entry));
    if (!stat.isFile()) {
      throw new Error(`Packaged JARVIS content file is missing: ${path.join(root, ...entry)}`);
    }
  }
}
