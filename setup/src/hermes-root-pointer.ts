import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface HermesRootPointerOverrides {
  /** Test seam; production always uses the current process platform. */
  platform?: NodeJS.Platform;
  /** Test/setup-context seam; production uses os.homedir(). */
  homeDir?: string;
  /** Test seam for pointer-file reads. */
  readFileSync?: (filePath: string) => string;
  /** Test seam for validating pointer targets. */
  existsSync?: (filePath: string) => boolean;
}

export function hermesRootPointerPath(
  env: NodeJS.ProcessEnv = process.env,
  overrides: Pick<HermesRootPointerOverrides, "platform" | "homeDir"> = {},
): string {
  const platform = overrides.platform ?? process.platform;
  const homeDir = overrides.homeDir ?? os.homedir();
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const configDir = platform === "win32"
    ? env.APPDATA?.trim() || pathApi.join(homeDir, "AppData", "Roaming")
    : pathApi.join(homeDir, ".config");
  return pathApi.join(configDir, "jarvis", "hermes-root");
}

export function readHermesRootPointer(
  env: NodeJS.ProcessEnv = process.env,
  overrides: HermesRootPointerOverrides = {},
): string | undefined {
  try {
    const readFileSync = overrides.readFileSync ?? ((filePath: string) => fs.readFileSync(filePath, "utf8"));
    const existsSync = overrides.existsSync ?? fs.existsSync;
    const platform = overrides.platform ?? process.platform;
    const pathApi = platform === "win32" ? path.win32 : path.posix;
    const firstLine = readFileSync(hermesRootPointerPath(env, overrides)).split(/\r?\n/, 1)[0]?.trim();
    if (!firstLine || !pathApi.isAbsolute(firstLine)) return undefined;
    return existsSync(pathApi.join(firstLine, "profiles")) ? firstLine : undefined;
  } catch {
    return undefined;
  }
}

export function writeHermesRootPointer(
  hermesRoot: string,
  env: NodeJS.ProcessEnv = process.env,
  overrides: Pick<HermesRootPointerOverrides, "platform" | "homeDir"> = {},
): void {
  const platform = overrides.platform ?? process.platform;
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const pointerPath = hermesRootPointerPath(env, overrides);
  fs.mkdirSync(pathApi.dirname(pointerPath), { recursive: true });
  fs.writeFileSync(pointerPath, `${pathApi.resolve(hermesRoot)}\n`, "utf8");
}

export function removeHermesRootPointer(
  env: NodeJS.ProcessEnv = process.env,
  overrides: Pick<HermesRootPointerOverrides, "platform" | "homeDir"> = {},
): void {
  fs.rmSync(hermesRootPointerPath(env, overrides), { force: true });
}
