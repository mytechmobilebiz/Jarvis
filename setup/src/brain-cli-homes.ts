/**
 * Where the vendors' own installers put the ChatGPT (codex) and Claude tools.
 *
 * ONE list, in ONE place, because there are TWO consumers that must never
 * disagree about it:
 *
 *  1. the wizard — `augmentedPath` (system.ts) puts these directories on the
 *     PATH the probes and the engine spawn with, so a bare-name spawn finds a
 *     tool the customer installed by hand;
 *  2. the ENGINE — `find_brain_cli` (agent/brain/providers.py, shipped through
 *     the engine-patch series) walks the same locations itself when its own
 *     PATH lookup comes up empty.
 *
 * When those two lists diverge the customer gets the wizard-says-no /
 * engine-says-yes disagreement that `HERMES_CODEX_CLI_PATH` was invented to
 * end: the wizard reports "nothing installed" and slides to the key path while
 * the engine would have run the tool perfectly well. `brain-cli-homes.test.ts`
 * parses the Python and pins the two lists in sync — edit one without the
 * other and the suite fails.
 *
 * FIELD DRIVER (v1.5.2): OpenAI's official Windows installer
 * (`irm https://chatgpt.com/codex/install.ps1 | iex`) puts codex.exe under
 * `%USERPROFILE%\.codex\bin`, and Claude's native installer puts claude.exe
 * under `%USERPROFILE%\.local\bin` (Scott Reed's box, 2026-08-18: the install
 * SUCCEEDED and `claude` was still invisible — the elevated shell predated the
 * user-PATH update, and JARVIS inherits Explorer's stale environment). NEITHER
 * directory was on the Windows side of either list before this file existed:
 * the wizard's win branch held only the hermes dirs, execPath and PNPM_HOME,
 * and the engine's `nt` branch held only `%USERPROFILE%\.local\bin` and
 * `%LOCALAPPDATA%\Programs\<bin>\<bin>.exe`. So a customer who ran the
 * vendor's own one-liner was told the app was not installed.
 *
 * TAIL, not head (see the callers): these go AFTER the buyer's own PATH
 * entries, so a customer who has chosen which `codex` comes first keeps that
 * choice, and this only heals a PATH that never had these at all — which is
 * exactly the stale-environment case above.
 */

import path from "node:path";
import { readEnvValue } from "./system.js";

/**
 * `%LOCALAPPDATA%`, or the near-universal layout when the variable is missing
 * or blank. Read through `readEnvValue` because Windows environment names are
 * case-insensitive and a redirected profile is a real (roaming) deployment —
 * hardcoding `AppData\Local` under the home directory is wrong there, which is
 * what the engine's `nt` branch did.
 */
export function localAppData(homeDir: string, env: NodeJS.ProcessEnv): string {
  const value = readEnvValue(env, "LOCALAPPDATA", "win32");
  if (value !== undefined && value.trim() !== "") return value;
  return path.win32.join(homeDir, "AppData", "Local");
}

/**
 * The vendor install homes for this platform, in probe order.
 *
 * Deliberately NOT per-CLI: the engine probes both binaries in every location
 * (its POSIX branch already looks for `claude` under `~/.codex/bin`), a
 * PATH is per-process and not per-program anyway, and a uniform list is the
 * one shape the sync pin can express. A location that cannot hold the binary
 * simply never matches — every consumer checks the file, not the directory.
 */
export function brainCliHomeDirs(
  platform: NodeJS.Platform,
  homeDir: string,
  env: NodeJS.ProcessEnv = process.env,
): string[] {
  if (platform === "win32") {
    const programs = path.win32.join(localAppData(homeDir, env), "Programs");
    return [
      // Claude's native installer, and where `install.ps1` puts a user-local
      // binary generally. Confirmed on a customer machine (Scott Reed).
      path.win32.join(homeDir, ".local", "bin"),
      // OpenAI's official Codex installer for Windows.
      path.win32.join(homeDir, ".codex", "bin"),
      // The same installer's other plausible layout: the binary directly in
      // the home folder rather than a `bin` subfolder. Unconfirmed at the time
      // of writing (the field answer from Justin Curnutt had not arrived), so
      // BOTH are probed — an extra `isFile` on a folder that does not exist
      // costs nothing, and guessing only one of the two would have left the
      // exact customer this change is for undetected.
      path.win32.join(homeDir, ".codex"),
      // The npm-local Claude layout, mirroring the POSIX `~/.claude/local`.
      path.win32.join(homeDir, ".claude", "local"),
      // OpenAI's current native installer location (L0, v1.6.0).
      path.win32.join(programs, "OpenAI", "Codex", "bin"),
      // Per-app Programs directories — what the engine's `nt` branch already
      // probed, kept, and now resolved through %LOCALAPPDATA% rather than an
      // assumed `AppData\Local`.
      path.win32.join(programs, "codex"),
      path.win32.join(programs, "claude"),
    ];
  }
  return [
    path.posix.join(homeDir, ".local", "bin"),
    path.posix.join(homeDir, ".codex", "bin"),
    path.posix.join(homeDir, ".claude", "local"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
  ];
}
