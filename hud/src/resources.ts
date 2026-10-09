import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGED_CONTENT_DIRECTORY = "jarvis-content";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Packaged setup contract:
 *
 * - Resources/jarvis-content contains only byte-identical runtime content:
 *   agents/, playbooks/, demo/, examples/, and a jarvis-content.json version
 *   marker.
 * - The jarvis-content.json marker version is the sole refresh trigger for
 *   staged content; content-only changes require bumping it.
 * - The setup engine is imported from @jarvis/setup in the HUD's own packaged
 *   node_modules. setup/dist, workspace metadata, build tools, and source tests
 *   are intentionally not duplicated in jarvis-content.
 * - Before setup uses that content, it copies the tree to
 *   <hermesRoot>/jarvis-content. That stable copy becomes ctx.repoRoot and the
 *   value baked into Tom's {{JARVIS_REPO_ROOT}} profile substitution; a path
 *   inside an installed .app/Resources bundle must never enter the profile.
 * - Packaged setup skips install-app entirely because the running app is
 *   already installed. Development and repo-zip setup continue using the
 *   checkout directly, so their content and behavior remain unchanged.
 */
export function resolveRepoRoot(
  overrides: { isPackaged?: boolean; resourcesPath?: string } = {},
): { repoRoot: string; packaged: boolean } {
  let packaged = overrides.isPackaged;
  let resourcesPath = overrides.resourcesPath;

  if (packaged === undefined || (packaged && resourcesPath === undefined)) {
    const runtime = readElectronRuntime();
    packaged ??= runtime.isPackaged;
    resourcesPath ??= runtime.resourcesPath;
  }

  return {
    repoRoot: packaged
      ? path.join(resourcesPath ?? process.cwd(), PACKAGED_CONTENT_DIRECTORY)
      : path.resolve(__dirname, "../.."),
    packaged,
  };
}

export function readElectronRuntime(
  loadElectron: () => unknown = () => createRequire(import.meta.url)("electron") as unknown,
): { isPackaged: boolean; resourcesPath: string } {
  // Loading Electron only when resolution actually needs it keeps plain Node
  // imports of setup-host.js usable in unit tests and non-GUI tooling.
  let electron: unknown;
  try {
    electron = loadElectron();
  } catch (error) {
    if (typeof error === "object" && error !== null && "code" in error && error.code === "MODULE_NOT_FOUND") {
      return { isPackaged: false, resourcesPath: process.cwd() };
    }
    throw error;
  }
  const electronApp = typeof electron === "object" && electron !== null && "app" in electron
    ? (electron as { app?: { isPackaged?: boolean } }).app
    : undefined;
  const runtimeProcess = process as NodeJS.Process & { resourcesPath?: string };
  return {
    isPackaged: electronApp?.isPackaged === true,
    resourcesPath: runtimeProcess.resourcesPath ?? process.cwd(),
  };
}
