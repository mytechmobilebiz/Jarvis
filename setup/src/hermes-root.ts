import os from "node:os";
import path from "node:path";
import { readHermesRootPointer, type HermesRootPointerOverrides } from "./hermes-root-pointer.js";

export { hermesRootPointerPath } from "./hermes-root-pointer.js";

export interface HermesRootOverrides extends HermesRootPointerOverrides {}

export function resolveHermesRoot(
  env: NodeJS.ProcessEnv = process.env,
  overrides: HermesRootOverrides = {},
): string {
  const platform = overrides.platform ?? process.platform;
  const homeDir = overrides.homeDir ?? os.homedir();
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const raw = env.HERMES_HOME?.trim();
  if (raw) {
    const expanded = raw.startsWith("~")
      ? pathApi.resolve(homeDir, raw.slice(1).replace(/^[\\/]+/, ""))
      : raw;
    const home = pathApi.resolve(expanded);
    // Hermes exports HERMES_HOME=<root>/profiles/<name> for profile subprocesses;
    // the root is the grandparent in that case.
    return pathApi.basename(pathApi.dirname(home)) === "profiles"
      ? pathApi.dirname(pathApi.dirname(home))
      : home;
  }

  const persisted = readHermesRootPointer(env, { ...overrides, platform, homeDir });
  if (persisted !== undefined) return pathApi.resolve(persisted);

  return platformDefaultHermesRoot(env, { platform, homeDir });
}

export function platformDefaultHermesRoot(
  env: NodeJS.ProcessEnv = process.env,
  overrides: Pick<HermesRootOverrides, "platform" | "homeDir"> = {},
): string {
  const platform = overrides.platform ?? process.platform;
  const homeDir = overrides.homeDir ?? os.homedir();
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  if (platform === "win32") {
    const localAppData = env.LOCALAPPDATA?.trim() || pathApi.join(homeDir, "AppData", "Local");
    return pathApi.resolve(localAppData, "hermes");
  }

  return pathApi.resolve(homeDir, ".hermes");
}

export function isPlatformDefaultHermesRoot(
  hermesRoot: string,
  env: NodeJS.ProcessEnv = process.env,
  overrides: Pick<HermesRootOverrides, "platform" | "homeDir"> = {},
): boolean {
  const platform = overrides.platform ?? process.platform;
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const actual = pathApi.resolve(hermesRoot);
  const expected = platformDefaultHermesRoot(env, overrides);
  return platform === "win32"
    ? actual.toLowerCase() === expected.toLowerCase()
    : actual === expected;
}
