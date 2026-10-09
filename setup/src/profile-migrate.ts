import os from "node:os";
import path from "node:path";
import { WizardError, type WizardContext } from "./context.js";
import { pathExists, readTextIfExists } from "./fs-utils.js";
import { enginePythonPath, hermesInvocation, runCommand } from "./system.js";

// Profiles seeded before v1.3.2 carry `stt.provider: auto` — an invalid
// explicit provider that disables STT entirely (Hermes auto-detects only when
// the key is absent). Repair the live profile config in place.
//
// v1.4.0 SPEECH POLICY (final review 2026-08-14, F8): engine-side speech is
// OFF — voice is HUD-owned local Kokoro, and the engine's TTS defaults to
// Edge (every character to a Microsoft endpoint). Existing profiles are
// brought to the shipped policy: auto_tts forced false (the pre-1.4 versions
// of this very migration set it TRUE — engine voice mode WAS the speech path
// then), the tts toolset hard-suppressed, and tts.provider pointed at the
// fail-closed command provider (an UNKNOWN provider name silently falls back
// to Edge, so the off-switch must be a resolvable provider that fails).
// This same atomic repair installs the HUD's fixed-port MCP entry for profiles
// created before the entry shipped in agents/jarvis/config.yaml.
//
// The write is atomic and metadata-preserving: it follows a symlinked
// config.yaml to its target, keeps the original file mode (a 0600 config with
// secrets must not become 0644 via the umask), and replaces the file only
// after the new content is fully flushed. Comments are not preserved —
// the same trade-off `hermes config set` makes for this file.
export const MIGRATE_VOICE_CONFIG_PY = `
import os, shutil, sys, yaml
path = os.path.realpath(sys.argv[1])
try:
    with open(path) as handle:
        config = yaml.safe_load(handle) or {}
except FileNotFoundError:
    sys.exit(0)
changed = []
if not isinstance(config, dict):
    # A non-mapping root (a bare list, string, number) is wholly inert to the
    # engine: it fails to merge and the engine silently retains DEFAULT_CONFIG
    # — whose TTS provider is online Edge and whose toolsets are all armed
    # (round-4 review, F8 held: exiting 0 here reported "no change" for a
    # profile left fail-open). Preserve the original beside the config, then
    # rebuild it as a mapping so every layer below gets enforced.
    #
    # The backup is created O_EXCL under a collision-free name (round-5
    # review: a fixed-name copy2 silently destroyed a previous backup and
    # followed a planted destination symlink). ANY pre-existing name — file,
    # directory, or symlink — advances to the next candidate: the lexists
    # pre-check is what refuses a DANGLING symlink on Windows, where
    # CPython's os.open lacks a no-follow flag and O_EXCL would follow the
    # link and create its nonexistent target (round-6 review); on POSIX,
    # O_EXCL itself refuses every pre-existing name atomically. Only a real
    # filesystem error (permissions, I/O) raises, exits nonzero, and fails
    # setup loudly — never a silent overwrite.
    mode = os.stat(path).st_mode & 0o7777
    backup = path + ".malformed-backup"
    suffix = 2
    fd = None
    while fd is None:
        if os.path.lexists(backup):
            backup = path + ".malformed-backup-" + str(suffix)
            suffix += 1
            continue
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            backup = path + ".malformed-backup-" + str(suffix)
            suffix += 1
    with open(path, "rb") as src, os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())
    # os.open's mode argument is filtered by the umask; restore the
    # original's exact bits (on Windows this only affects read-only, fine).
    os.chmod(backup, mode)
    config = {}
    changed.append("rebuilt an unreadable profile config (the original is kept beside it as " + os.path.basename(backup) + ")")
stt = config.get("stt")
if isinstance(stt, dict) and stt.get("provider") == "auto":
    del stt["provider"]
    stt.setdefault("enabled", True)
    changed.append("removed invalid stt.provider=auto so Hermes auto-detects local speech-to-text")
mcp_servers = config.get("mcp_servers")
if not isinstance(mcp_servers, dict):
    if "mcp_servers" in config:
        changed.append("replaced an invalid mcp_servers setting with a mapping")
    mcp_servers = {}
    config["mcp_servers"] = mcp_servers
hud_mcp = {"url": "http://127.0.0.1:4870/mcp"}
if mcp_servers.get("hud") != hud_mcp:
    mcp_servers["hud"] = hud_mcp
    changed.append("registered the HUD dashboard connection")
gateway = config.setdefault("gateway", {})
message_timestamps = gateway.setdefault("message_timestamps", {})
if "enabled" not in message_timestamps:
    message_timestamps["enabled"] = True
    changed.append("included the local time in each message")
voice = config.get("voice")
if not isinstance(voice, dict):
    voice = {}
    config["voice"] = voice
if voice.get("auto_tts") is not False:
    voice["auto_tts"] = False
    changed.append("turned off engine-side spoken replies (JARVIS speaks through the app, on this machine)")
agent = config.get("agent")
if not isinstance(agent, dict):
    agent = {}
    config["agent"] = agent
disabled = agent.get("disabled_toolsets")
if not isinstance(disabled, list):
    disabled = []
if "tts" not in disabled:
    disabled.append("tts")
    agent["disabled_toolsets"] = disabled
    changed.append("kept the engine's speech tool switched off")
tts = config.get("tts")
if not isinstance(tts, dict):
    tts = {}
    config["tts"] = tts
providers = tts.get("providers")
if not isinstance(providers, dict):
    providers = {}
    tts["providers"] = providers
# The provider ENTRY is enforced by exact equality, not by shape (round-3
# review, F8 held): a dict that merely EXISTS under this name can still leak —
# an empty dict resolves to None and reaches the unknown-provider Edge
# fallback, and a replaced command could perform online TTS under our own
# provider name. Anything but the exact fail-closed entry gets rewritten.
off_entry = {"type": "command", "command": "exit 1"}
if tts.get("provider") != "jarvis-local-off":
    tts["provider"] = "jarvis-local-off"
    providers["jarvis-local-off"] = dict(off_entry)
    changed.append("pointed the engine's speech output at the app's own local voice instead of an online service")
elif providers.get("jarvis-local-off") != off_entry:
    providers["jarvis-local-off"] = dict(off_entry)
    changed.append("repaired the engine speech-off setting")
if changed:
    serialized = yaml.safe_dump(config, sort_keys=False)
    mode = os.stat(path).st_mode & 0o7777
    tmp_path = path + ".jarvis-migrate.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
print("; ".join(changed))
`;

/**
 * Repair pre-v1.3.2 voice settings in the live profile config. Returns a
 * human-readable summary of what changed, or null when there was nothing to
 * do (no profile, no Hermes venv, or the config is already correct).
 * Throws WizardError when the repair ran and failed.
 */
export async function migrateProfileVoiceConfig(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): Promise<string | null> {
  if (ctx.dryRun) return null;
  const configPath = path.join(ctx.profileDir, "config.yaml");
  // Either payload shape — venv OR site interpreter; venv-only silently
  // skipped this repair on every mac site install (Codex review 2026-08-14).
  const python = enginePythonPath(ctx, platform);
  if (!(await pathExists(configPath)) || python === undefined) return null;

  const result = await runCommand(ctx, python, ["-c", MIGRATE_VOICE_CONFIG_PY, configPath], {
    stream: false,
  });
  if (result.status !== 0) {
    throw new WizardError(
      "The profile voice-config repair did not complete.",
      // Plain-language remedy only; the old hint told customers to hand-add
      // 'auto_tts: true' — the exact setting the v1.4.0 speech policy turns
      // OFF (final re-review 2026-08-14, F8).
      "Run setup again from JARVIS settings — the Voice step repairs this by itself. If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    );
  }
  const changed = result.stdout.trim();
  return changed.length > 0 ? changed : null;
}

/**
 * F4 follow-through (Codex review round 1, findings 1–2): the shipped seed
 * pins `terminal.cwd` as the LITERAL `~/.hermes/profiles/jarvis`, which is
 * only right for a default POSIX install. Windows profiles live under
 * `%LOCALAPPDATA%\hermes` and HERMES_HOME can be overridden — there the
 * literal names a nonexistent (or someone else's) directory. And profiles
 * that already exist never receive the new seed at all: the Profile step
 * returns early when SOUL.md + config.yaml are present.
 *
 * This migration pins `terminal.cwd` to the machine's ACTUAL profile
 * directory, idempotently:
 *  - absent / placeholder (".", "auto", "cwd", "") → pinned;
 *  - the seed literal, when it does not expand to this machine's profile
 *    dir (Windows, custom HERMES_HOME) → pinned;
 *  - the seed literal on a default POSIX install → left alone (no rewrite,
 *    so the shipped file's comments survive);
 *  - anything else is the customer's own explicit choice → left alone.
 */
export const MIGRATE_TERMINAL_CWD_PY = `
import os, sys, yaml
path = os.path.realpath(sys.argv[1])
# argv[2] is the PIN TARGET — normally the profile dir; a legacy env-chosen
# workspace when one exists (review round 13). argv[3]="1" makes the seed
# literal pin unconditionally: the target must beat the seed even where the
# seed happens to expand to this machine's profile dir.
target = os.path.normpath(sys.argv[2])
# argv[3]: "0" ordinary pin, "1" pin unconditionally over the seed, "clear"
# REMOVE the seed and pin nothing — the mode for an interpolated legacy env
# value we refuse to resolve: the seed must not out-rank a dotenv setting we
# could not migrate faithfully (review round 19).
mode = sys.argv[3] if len(sys.argv) > 3 else "0"
seed_always = mode == "1"
clear_seed = mode == "clear"
try:
    with open(path) as handle:
        config = yaml.safe_load(handle) or {}
except FileNotFoundError:
    sys.exit(0)
if not isinstance(config, dict):
    # A malformed root is the voice migration's to repair; pinning a cwd into
    # a config the engine cannot merge would change nothing.
    sys.exit(0)
terminal = config.get("terminal")
if not isinstance(terminal, dict):
    terminal = {}
raw = terminal.get("cwd")
cur = raw.strip() if isinstance(raw, str) else ""
SEED = "~/.hermes/profiles/jarvis"
def expanded(value):
    try:
        return os.path.normpath(os.path.expanduser(value))
    except Exception:
        return ""
needs_pin = False
if clear_seed:
    # Only the SEED is cleared: a placeholder already falls through to the
    # env setting at the engine's bridge, and a customer-set path outranks
    # the env value legitimately.
    if cur != SEED:
        sys.exit(0)
elif cur in ("", ".", "./", "auto", "cwd"):
    needs_pin = True
elif cur == SEED and (seed_always or expanded(SEED) != target):
    needs_pin = True
if not (needs_pin or clear_seed):
    sys.exit(0)
if clear_seed:
    terminal.pop("cwd", None)
    if terminal:
        config["terminal"] = terminal
    else:
        config.pop("terminal", None)
else:
    terminal["cwd"] = target
    config["terminal"] = terminal
serialized = yaml.safe_dump(config, sort_keys=False)
mode = os.stat(path).st_mode & 0o7777
# The temp file is created O_EXCL under a collision-free name, never O_TRUNC
# on a fixed name: a planted symlink at the fixed name would be followed and
# its target overwritten, and os.replace would then install the symlink as
# the config itself. The lexists pre-check is what refuses a DANGLING symlink
# on Windows (same rationale as the voice migration's backup writer).
tmp_path = path + ".jarvis-migrate.tmp." + str(os.getpid())
suffix = 2
fd = None
while fd is None:
    if os.path.lexists(tmp_path):
        tmp_path = path + ".jarvis-migrate.tmp." + str(os.getpid()) + "-" + str(suffix)
        suffix += 1
        continue
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        tmp_path = path + ".jarvis-migrate.tmp." + str(os.getpid()) + "-" + str(suffix)
        suffix += 1
try:
    with os.fdopen(fd, "w") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)
except BaseException:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise
if clear_seed:
    print("released the shipped working-folder seed so the workspace configured in .env stays in charge")
else:
    print("pinned the assistant's working folder to the JARVIS profile")
`;

/** The seed's literal spelling of the working-folder pin. */
const TERMINAL_CWD_SEED = "~/.hermes/profiles/jarvis";
// "./" included (review round 7): the engine resolves it to the launch
// directory exactly like "." — the F4 bug spelled two characters longer.
const TERMINAL_CWD_PLACEHOLDERS = new Set(["", ".", "./", "auto", "cwd"]);

/**
 * Read `terminal.cwd` out of a profile config.yaml without a YAML parser —
 * the readModelDefault precedent, with one addition the CLI lane depends on:
 * a `conclusive` flag. Any `terminal:` spelling this parser does not
 * positively understand (multi-line flow style, tags, anything but the plain
 * block and single-line inline forms) is INCONCLUSIVE, and an inconclusive
 * read must never be treated as "no cwd configured" — that is how a
 * customer's chosen working directory would get overwritten (review round 4,
 * finding 5).
 */
export function readTerminalCwd(configText: string): { cwd: string | undefined; conclusive: boolean } {
  const lines = configText.split(/\r?\n/);
  // A whole-document flow mapping (`{terminal: {...}, ...}`) is valid YAML
  // this parser cannot walk — inconclusive, never "absent" (round 6). The
  // document-start marker is skipped when it stands alone and treated as a
  // same-line document start otherwise (`--- {terminal: ...}`, round 7).
  const firstContent = lines.find((line) => {
    const trimmed = line.trim();
    return trimmed !== "" && !trimmed.startsWith("#") && trimmed !== "---" && trimmed !== "...";
  });
  if (firstContent !== undefined) {
    const trimmed = firstContent.trimStart();
    if (/^[[{]/.test(trimmed)) return { cwd: undefined, conclusive: false };
    if (/^---\s+\S/.test(trimmed)) return { cwd: undefined, conclusive: false };
  }
  // ANY spelling that could name a terminal key counts as a sighting —
  // quoted keys, space-before-colon, indentation, tags (`!!str terminal:`)
  // and anchors (`&key terminal:`) included (review rounds 5–8). Only the
  // plain top-level `terminal:` spelling is then actually parsed; every
  // other sighting is inconclusive rather than "absent". Over-matching (a
  // value that merely CONTAINS "terminal:") is deliberate: declining to pin
  // costs only a retry through the full YAML lane — guessing wrong costs a
  // customer their setting.
  const sighting = (line: string): boolean => {
    if (line.trimStart().startsWith("#")) return false;
    if (/(^|[\s"'!&*{,[])["']?terminal["']?\s*:/.test(line)) return true;
    // The explicit-key form (`? terminal` on its own line, value on the
    // next) names the same key with no colon on this line (round 11).
    return /^\s*\?/.test(line) && /terminal/.test(line);
  };
  const terminalIndex = lines.findIndex(sighting);
  if (terminalIndex === -1) return { cwd: undefined, conclusive: true };
  const terminalLine = lines[terminalIndex];
  if (!/^terminal:/.test(terminalLine)) return { cwd: undefined, conclusive: false };

  // Inline form: `terminal: {cwd: /path}` (also `{}`), which the engine and
  // yaml.safe_dump can both produce; a trailing comment is fine.
  const inline = /^terminal:\s*\{(.*)\}\s*(#.*)?$/.exec(terminalLine);
  if (inline !== null) {
    const cwd = /(?:^|,)\s*["']?cwd["']?\s*:\s*([^,}]*)/.exec(inline[1]);
    if (cwd === null) return { cwd: undefined, conclusive: true };
    const scalar = cleanYamlScalar(cwd[1]);
    return { cwd: scalar.value, conclusive: scalar.conclusive };
  }

  // Block form: `terminal:` with nothing (or only a comment) after the colon.
  if (!/^terminal:\s*(#.*)?$/.test(terminalLine)) {
    return { cwd: undefined, conclusive: false };
  }
  for (let index = terminalIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim() === "" || line.trimStart().startsWith("#")) continue;
    if (!/^\s/.test(line)) return { cwd: undefined, conclusive: true }; // dedent ends the block
    const match = /^\s+cwd:\s*(.*)$/.exec(line);
    if (match === null) {
      // `cwd : /x`, `"cwd": /x` — valid spellings of the SAME key this
      // parser does not walk. Inconclusive, never "absent" (round 15).
      if (/^\s+["']?cwd["']?\s*:/.test(line)) return { cwd: undefined, conclusive: false };
      continue;
    }
    const scalar = cleanYamlScalar(match[1]);
    return { cwd: scalar.value, conclusive: scalar.conclusive };
  }
  return { cwd: undefined, conclusive: true };
}

/**
 * Reduce a raw scalar as written in YAML to the value the engine would see
 * (review round 9): trailing comments cut (a comment starts at whitespace +
 * '#' outside quotes), quotes unwrapped, null spellings (`null`/`~`/empty)
 * read as absent. Tags, anchors and aliases are inconclusive — the textual
 * lane never guesses at YAML it cannot positively evaluate.
 */
function cleanYamlScalar(raw: string): { value: string | undefined; conclusive: boolean } {
  let text = raw.trim();
  if (text === "" || text.startsWith("#")) return { value: undefined, conclusive: true };
  if (text.startsWith('"')) {
    const match = /^"((?:[^"\\]|\\.)*)"\s*(#.*)?$/.exec(text);
    return match === null
      ? { value: undefined, conclusive: false }
      : { value: match[1] === "" ? undefined : match[1], conclusive: true };
  }
  if (text.startsWith("'")) {
    const match = /^'((?:[^']|'')*)'\s*(#.*)?$/.exec(text);
    return match === null
      ? { value: undefined, conclusive: false }
      : { value: match[1] === "" ? undefined : match[1].replace(/''/g, "'"), conclusive: true };
  }
  if (/^[!&*]/.test(text)) return { value: undefined, conclusive: false };
  const comment = text.search(/\s#/);
  if (comment !== -1) text = text.slice(0, comment).trim();
  if (text === "" || /^(null|Null|NULL|~)$/.test(text)) return { value: undefined, conclusive: true };
  return { value: text, conclusive: true };
}

/**
 * Read a legacy TERMINAL_CWD / MESSAGING_CWD value the way the engine's own
 * loader (python-dotenv) would see it (review round 14): `export` prefixes
 * accepted, quotes unwrapped (double-quote escapes included), trailing
 * comments cut on unquoted values. Values the engine would INTERPOLATE
 * (`$`/backtick) are not guessed at: they report blockPin, and the whole
 * migration then declines to write anything rather than pin a wrong path
 * over a working env setting.
 */
export function readLegacyCwdValue(
  envText: string,
  key: string,
): { value?: string; blockPin: boolean } {
  // python-dotenv applies the LAST binding when a key repeats (round 15).
  const matches = [...envText.matchAll(new RegExp(`^[ \\t]*(?:export[ \\t]+)?${key}[ \\t]*=[ \\t]*(.*)$`, "gm"))];
  const match = matches.at(-1);
  if (match === undefined) return { blockPin: false };
  const raw = match[1].trim();
  if (raw === "") return { blockPin: false };
  let value: string;
  if (raw.startsWith('"')) {
    const quoted = /^"((?:[^"\\]|\\.)*)"/.exec(raw);
    if (quoted === null) return { blockPin: true };
    // python-dotenv semantics: only \\ and \" are unescaped; every other
    // backslash stays LITERAL — a Windows path like "C:\Users\Alice" must
    // survive intact (round 15).
    value = quoted[1].replace(/\\([\\"])/g, "$1");
  } else if (raw.startsWith("'")) {
    const quoted = /^'([^']*)'/.exec(raw);
    if (quoted === null) return { blockPin: true };
    value = quoted[1];
  } else {
    const comment = raw.search(/\s#/);
    value = (comment === -1 ? raw : raw.slice(0, comment)).trim();
  }
  if (value.includes("$") || value.includes("`")) return { blockPin: true };
  if (value === "" || TERMINAL_CWD_PLACEHOLDERS.has(value)) return { blockPin: false };
  return { value, blockPin: false };
}

/**
 * The pin decision, shared by both lanes and exported for tests: what value
 * (if any) should terminal.cwd be pinned to?
 *
 *  - A legacy env-chosen workspace (deprecated TERMINAL_CWD/MESSAGING_CWD)
 *    becomes the pin target — config.yaml outranks those settings at the
 *    gateway bridge, so a seed or placeholder sitting in config would
 *    silently replace the customer's workspace (review rounds 12-13).
 *    Migrating the value INTO config preserves the choice under config
 *    precedence; when it is the target, even a correct-looking seed is
 *    replaced.
 *  - Otherwise the profile dir is the target, pinned over placeholders and
 *    over a seed that does not expand to this machine's profile dir.
 *  - An explicit customer cwd in config always wins: no pin.
 */
export function planTerminalCwdPin(options: {
  configCwd: string | undefined;
  legacyEnvCwd: string | undefined;
  profileDir: string;
  home: string;
}): string | null {
  const target = options.legacyEnvCwd ?? options.profileDir;
  const seedAlways = options.legacyEnvCwd !== undefined;
  const value = (options.configCwd ?? "").trim();
  if (TERMINAL_CWD_PLACEHOLDERS.has(value)) return target;
  if (value === TERMINAL_CWD_SEED) {
    const expanded = path.normalize(TERMINAL_CWD_SEED.replace(/^~(?=$|\/)/, options.home));
    return seedAlways || expanded !== path.normalize(target) ? target : null;
  }
  return null; // the customer's own explicit config choice
}

/**
 * Pin `terminal.cwd` in the live profile config to this machine's actual
 * profile directory. Returns the plain-language summary when a pin was
 * written, or null when there was nothing to do. Throws WizardError when the
 * repair ran and failed.
 *
 * Two write lanes, matching the rest of setup (review round 2, findings
 * 2–3): the engine-python atomic transaction when a managed interpreter is
 * installed, else the Hermes CLI (`config set`) — which also serves the
 * supported PATH/global installs where no managed python ever exists. On a
 * machine where neither lane can run yet (fresh box, mid-repair), callers
 * retry: the migration runs at setup start, at profile install, and before
 * the gateway spawns.
 */
export async function migrateProfileTerminalCwd(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
  homeDir: string = os.homedir(),
): Promise<string | null> {
  if (ctx.dryRun) return null;
  const configPath = path.join(ctx.profileDir, "config.yaml");
  if (!(await pathExists(configPath))) return null;

  // A 1.3.x-era customer may have chosen their workspace through the
  // deprecated TERMINAL_CWD / MESSAGING_CWD env settings rather than
  // config.yaml (review rounds 12-13). config.yaml OUTRANKS those at the
  // gateway's bridge, so a seed or placeholder sitting in config would
  // silently replace their choice — the env value therefore becomes the pin
  // TARGET, migrated into config where it keeps winning under config
  // precedence (an explicit different config cwd still beats it, below).
  // ONLY the profile .env: `hermes -p jarvis` re-homes HERMES_HOME to the
  // profile before its dotenv load, so the root .env is the DEFAULT
  // profile's workspace, never a JARVIS cwd source (round 15).
  let legacyEnvCwd: string | undefined;
  let interpolatedEnvCwd = false;
  for (const envPath of [path.join(ctx.profileDir, ".env")]) {
    if (legacyEnvCwd !== undefined) break;
    let envText: string | undefined;
    try {
      envText = await readTextIfExists(envPath);
    } catch {
      // An UNREADABLE .env is not an absent one (round 16): it may carry a
      // legacy workspace we would out-rank. No pin until it can be read.
      return null;
    }
    if (envText === undefined) continue;
    for (const key of ["TERMINAL_CWD", "MESSAGING_CWD"]) {
      const legacy = readLegacyCwdValue(envText, key);
      // A value the engine would interpolate cannot be migrated faithfully —
      // never write a guessed path. But "write nothing" is only safe when
      // the config carries nothing that would OUT-RANK the env setting: the
      // shipped seed is a real path the engine prefers over dotenv, so it
      // must be RELEASED, not left standing (review round 19). The clear
      // lane below does exactly and only that.
      if (legacy.blockPin) {
        interpolatedEnvCwd = true;
        break;
      }
      if (legacy.value !== undefined) {
        legacyEnvCwd = legacy.value;
        break;
      }
    }
  }
  const target = legacyEnvCwd ?? ctx.profileDir;
  const seedAlways = legacyEnvCwd !== undefined;

  const python = enginePythonPath(ctx, platform);

  if (interpolatedEnvCwd) {
    // Clear lane: remove the seed (and nothing else) so the interpolated env
    // workspace keeps winning at the engine's bridge. Only the full YAML lane
    // can delete a key; without the interpreter we leave the file alone —
    // conservative, and retried with the YAML lane once the interpreter
    // exists, same recovery as an inconclusive read below.
    if (python === undefined) return null;
    const cleared = await runCommand(
      ctx,
      python,
      ["-c", MIGRATE_TERMINAL_CWD_PY, configPath, ctx.profileDir, "clear"],
      { stream: false },
    );
    if (cleared.status !== 0) {
      throw new WizardError(
        "The profile working-folder repair did not complete.",
        "Run setup again from JARVIS settings. If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
      );
    }
    const releasedNote = cleared.stdout.trim();
    return releasedNote.length > 0 ? releasedNote : null;
  }

  if (python !== undefined) {
    const result = await runCommand(
      ctx,
      python,
      ["-c", MIGRATE_TERMINAL_CWD_PY, configPath, target, seedAlways ? "1" : "0"],
      { stream: false },
    );
    if (result.status !== 0) {
      throw new WizardError(
        "The profile working-folder repair did not complete.",
        "Run setup again from JARVIS settings. If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
      );
    }
    const changed = result.stdout.trim();
    return changed.length > 0 ? changed : null;
  }

  // CLI lane: unmanaged installs (bare `hermes` on PATH) and managed ones
  // whose interpreter is not there yet. The read is textual and CONSERVATIVE:
  // a cwd we cannot positively classify is left alone.
  const text = await readTextIfExists(configPath);
  if (text === undefined) return null;
  const read = readTerminalCwd(text);
  // A config this parser cannot positively classify is left alone — better a
  // missing pin (retried with the full YAML lane once the interpreter
  // exists) than an overwritten customer setting.
  if (!read.conclusive) return null;
  const pin = planTerminalCwdPin({
    configCwd: read.cwd,
    legacyEnvCwd,
    profileDir: ctx.profileDir,
    home: homeDir,
  });
  if (pin === null) return null;
  const invocation = hermesInvocation(ctx, ["-p", "jarvis", "config", "set", "terminal.cwd", pin], platform);
  let result;
  try {
    result = await runCommand(ctx, invocation.command, invocation.args, { stream: false });
  } catch {
    result = { status: 127, stdout: "", stderr: "" };
  }
  if (result.status !== 0) {
    throw new WizardError(
      "The profile working-folder repair did not complete.",
      "Run setup again from JARVIS settings. If it keeps failing, use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    );
  }
  return "pinned the assistant's working folder to the JARVIS profile";
}
