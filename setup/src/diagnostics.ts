import fs from "node:fs/promises";
import path from "node:path";
import type { StepStatus, StepSummary } from "./context.js";
import { cachedInstallId, loadInstallId, readInstallId } from "./telemetry.js";

/**
 * Customer-volunteered support correlation. The support code is a short prefix
 * of the anonymous telemetry install id: server-side nothing changes and the
 * id stays unlinkable, but a customer who chooses to mention the code in an
 * email lets support find their setup events without timestamp guesswork.
 * The diagnostics block is the same idea for step results: plain text the
 * customer can read before sharing, never keys, .env contents, or messages.
 */

export interface LastRunStep {
  id: string;
  status: StepStatus;
  /** Step-authored telemetry props — closed-set codes, booleans and counts. */
  props?: Record<string, string | number | boolean>;
}

export interface LastRunRecord {
  at: string;
  appVersion: string;
  platform: string;
  arch: string;
  packaged: boolean;
  failed: boolean;
  steps: LastRunStep[];
}

export function lastRunFilePath(configDir: string): string {
  return path.join(configDir, "last-setup-run.json");
}

export function supportCodeFromInstallId(installId: string): string {
  return installId.replace(/-/g, "").slice(0, 8).toUpperCase();
}

/**
 * With mint (any real run) a missing install id is created on the spot — the
 * same file telemetry reads later, so the code shown always matches the id on
 * the wire. Without mint (dry runs, which promise to write nothing) only an
 * existing id yields a code.
 */
export async function loadSupportCode(
  configDir: string,
  options: { mint: boolean },
): Promise<string | undefined> {
  // Read-only path checks the in-process cache first, so a code the setup host
  // already minted (incl. on an unwritable dir, where nothing reached disk)
  // still shows here instead of "unavailable".
  const installId = options.mint
    ? await loadInstallId(configDir)
    : (cachedInstallId(configDir) ?? await readInstallId(configDir));
  return installId === undefined ? undefined : supportCodeFromInstallId(installId);
}

export function buildLastRunRecord(options: {
  summary: StepSummary[];
  failed: boolean;
  appVersion: string;
  packaged: boolean;
  platform?: string;
  arch?: string;
  now?: () => Date;
}): LastRunRecord {
  return {
    at: (options.now ?? (() => new Date()))().toISOString(),
    appVersion: options.appVersion,
    platform: options.platform ?? process.platform,
    arch: options.arch ?? process.arch,
    packaged: options.packaged,
    failed: options.failed,
    steps: options.summary.map((step) => {
      const props = sanitizeProps(step.props);
      return { id: step.id, status: step.status, ...(props === undefined ? {} : { props }) };
    }),
  };
}

/**
 * Notes and nextActions are free-form text and stay out by construction; props
 * are already anonymous by contract (see StepOutcome.props), the primitive
 * filter just keeps a future object-valued prop from smuggling structure in.
 */
function sanitizeProps(
  props: Record<string, unknown> | undefined,
): Record<string, string | number | boolean> | undefined {
  if (props === undefined) return undefined;
  const clean: Record<string, string | number | boolean> = {};
  for (const [key, value] of Object.entries(props)) {
    if (typeof value === "boolean" || typeof value === "number") clean[key] = value;
    else if (typeof value === "string") clean[key] = value.slice(0, 80);
  }
  return Object.keys(clean).length === 0 ? undefined : clean;
}

/** Best-effort: a failed write must never fail a setup run. */
export async function writeLastRun(configDir: string, record: LastRunRecord): Promise<void> {
  const target = lastRunFilePath(configDir);
  const tmpPath = `${target}.tmp`;
  try {
    await fs.mkdir(configDir, { recursive: true });
    await fs.writeFile(tmpPath, `${JSON.stringify(record, null, 2)}\n`, "utf8");
    await fs.rename(tmpPath, target);
  } catch {
    // Same stance as telemetry: diagnostics must never surface an error.
  }
}

export async function readLastRun(configDir: string): Promise<LastRunRecord | undefined> {
  try {
    const stored = JSON.parse(await fs.readFile(lastRunFilePath(configDir), "utf8")) as Partial<LastRunRecord>;
    if (typeof stored.at !== "string" || !Array.isArray(stored.steps)) return undefined;
    return {
      at: stored.at,
      appVersion: typeof stored.appVersion === "string" ? stored.appVersion : "unknown",
      platform: typeof stored.platform === "string" ? stored.platform : "unknown",
      arch: typeof stored.arch === "string" ? stored.arch : "unknown",
      packaged: stored.packaged === true,
      failed: stored.failed === true,
      steps: stored.steps.filter(
        (step): step is LastRunStep =>
          typeof step === "object" && step !== null
          && typeof (step as LastRunStep).id === "string"
          && typeof (step as LastRunStep).status === "string",
      ),
    };
  } catch {
    return undefined;
  }
}

/**
 * A non-clean shutdown, recorded anonymously so support can see that JARVIS died
 * on a machine without a server round-trip. A main-process death can't be caught
 * from inside the process it kills, so it is inferred on the next start from a
 * surviving session marker ("dirty-shutdown"); renderer/child deaths the main
 * process CAN observe are recorded directly. Same redaction contract as
 * telemetry: a closed-set reason, Electron's own closed-set detail code, the app
 * version, and a timestamp — never a stack, a message, or a path.
 */
export type CrashReason =
  | "dirty-shutdown"
  | "render-process-gone"
  | "child-process-gone"
  | "gpu-process-gone";

const CRASH_REASONS: readonly CrashReason[] = [
  "dirty-shutdown",
  "render-process-gone",
  "child-process-gone",
  "gpu-process-gone",
];

/**
 * Electron's own closed set of exit-reason codes (render-process-gone /
 * child-process-gone). The `detail` field is allowlisted against this at BOTH
 * the write and read boundaries so nothing free-form — a message, a path, a key
 * — can ever reach the shareable diagnostics block, even if a future caller
 * passes something it shouldn't.
 */
const CRASH_DETAILS: readonly string[] = [
  "clean-exit",
  "abnormal-exit",
  "killed",
  "crashed",
  "oom",
  "launch-failed",
  "integrity-failure",
];

function closedSetDetail(detail: string | undefined): string | undefined {
  return detail !== undefined && CRASH_DETAILS.includes(detail) ? detail : undefined;
}

// A crash record is shareable, so every stored field is shape-validated at both
// boundaries — a version/platform/timestamp only, never a path, message, or key
// that a tampered file (or a stray caller) could otherwise smuggle into it.
const SAFE_VERSION = /^[0-9A-Za-z.+_-]{1,40}$/;
const SAFE_PLATFORM = /^[a-z0-9]{1,16}$/;
const SAFE_AT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

function safeVersion(value: unknown): string {
  return typeof value === "string" && SAFE_VERSION.test(value) ? value : "unknown";
}

function safePlatform(value: unknown): string {
  return typeof value === "string" && SAFE_PLATFORM.test(value) ? value : "unknown";
}

export interface CrashRecord {
  reason: CrashReason;
  /** Electron's own closed-set code, e.g. "crashed" | "oom" | "killed". */
  detail?: string;
  appVersion: string;
  platform: string;
  at: string;
  /** Non-clean shutdowns seen on this machine — a repeat-offender signal. */
  count: number;
}

export function lastCrashFilePath(configDir: string): string {
  return path.join(configDir, "last-crash.json");
}

export async function readLastCrash(configDir: string): Promise<CrashRecord | undefined> {
  try {
    const stored = JSON.parse(await fs.readFile(lastCrashFilePath(configDir), "utf8")) as Partial<CrashRecord>;
    if (
      typeof stored.at !== "string"
      || !SAFE_AT.test(stored.at)
      || !CRASH_REASONS.includes(stored.reason as CrashReason)
    ) {
      return undefined;
    }
    const detail = closedSetDetail(typeof stored.detail === "string" ? stored.detail : undefined);
    return {
      reason: stored.reason as CrashReason,
      ...(detail === undefined ? {} : { detail }),
      appVersion: safeVersion(stored.appVersion),
      platform: safePlatform(stored.platform),
      at: stored.at,
      count: typeof stored.count === "number" && Number.isFinite(stored.count) && stored.count > 0
        ? Math.floor(stored.count)
        : 1,
    };
  } catch {
    return undefined;
  }
}

// Two near-simultaneous deaths (a renderer and the GPU process, say) would each
// read the same prior count and write count+1 through the same temp file — one
// record and one increment lost. Serialize every write through a single chain so
// the read-modify-write is atomic in-process; a per-pid temp name keeps two
// processes from colliding on the same scratch file.
let crashWriteChain: Promise<void> = Promise.resolve();

/**
 * Best-effort, like writeLastRun: recording a crash must never throw into the
 * lifecycle hook that noticed it. The running count is carried forward from the
 * prior record so the block can flag a machine that keeps dying.
 */
export function recordCrash(
  configDir: string,
  input: { reason: CrashReason; detail?: string; appVersion: string; platform: string; now?: () => Date },
): Promise<void> {
  const next = crashWriteChain.then(() => writeCrashRecord(configDir, input));
  crashWriteChain = next.catch(() => {});
  return next;
}

async function writeCrashRecord(
  configDir: string,
  input: { reason: CrashReason; detail?: string; appVersion: string; platform: string; now?: () => Date },
): Promise<void> {
  // Guard the write boundary too: never persist an out-of-set reason, and never
  // let an invalid injected clock throw `toISOString` before the write.
  if (!CRASH_REASONS.includes(input.reason)) return;
  const prior = await readLastCrash(configDir);
  const detail = closedSetDetail(input.detail);
  const stamp = (input.now ?? (() => new Date()))();
  const at = Number.isNaN(stamp.getTime()) ? new Date().toISOString() : stamp.toISOString();
  const record: CrashRecord = {
    reason: input.reason,
    ...(detail === undefined ? {} : { detail }),
    appVersion: safeVersion(input.appVersion),
    platform: safePlatform(input.platform),
    at,
    count: (prior?.count ?? 0) + 1,
  };
  const target = lastCrashFilePath(configDir);
  const tmpPath = `${target}.${process.pid}.tmp`;
  try {
    await fs.mkdir(configDir, { recursive: true });
    await fs.writeFile(tmpPath, `${JSON.stringify(record, null, 2)}\n`, "utf8");
    await fs.rename(tmpPath, target);
  } catch {
    // Same stance as telemetry and last-run: diagnostics never surface an error.
  }
}

export type BrainLedgerDiagnostics = Array<{
  provider: string;
  days: Array<{
    date: string;
    turns: number;
    inputTokens: number;
    outputTokens: number;
    byClass: Record<string, number>;
    models: Record<string, number>;
  }>;
}>;

export interface DiagnosticsInfo {
  supportCode?: string;
  appVersion: string;
  platform: string;
  arch: string;
  packaged: boolean;
  lastRun?: LastRunRecord;
  lastCrash?: CrashRecord;
  /**
   * Lane C (voice): live state of the bundled speech engines, as the HUD sees
   * it right now — which is often more useful than the last setup run, because
   * antivirus tends to quarantine a model days after a clean install.
   * Deliberately a closed set of codes and booleans: no paths, no device names.
   */
  voice?: VoiceDiagnostics;
  brainLedger?: BrainLedgerDiagnostics;
}

export interface VoiceDiagnostics {
  available: boolean;
  /** Closed-set code, e.g. "assets-corrupt". Absent when available. */
  reason?: string;
  voiceId?: string;
  speakReplies?: boolean;
  chatTts?: string;
  chatFailures?: number;
  /** "unknown" | "granted" | "denied" | "no-device" */
  mic?: string;
}

const PLATFORM_LABELS: Record<string, string> = {
  darwin: "macOS",
  win32: "Windows",
  linux: "Linux",
};

function platformLabel(platform: string, arch: string, packaged: boolean): string {
  const name = PLATFORM_LABELS[platform] ?? platform;
  return `${name} ${arch} (${packaged ? "packaged app" : "unpackaged"})`;
}

function whenLabel(at: string): string {
  // 2026-08-11T09:41:23.123Z -> 2026-08-11 09:41 UTC
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(at) ? `${at.slice(0, 16).replace("T", " ")} UTC` : at;
}

function formatVoice(voice: VoiceDiagnostics): string {
  if (!voice.available) return `unavailable (${voice.reason ?? "unknown"})`;
  const parts = [`ready, voice=${voice.voiceId ?? "bm_george"}`];
  if (voice.speakReplies !== undefined) parts.push(`speak-replies=${voice.speakReplies ? "on" : "off"}`);
  if (voice.chatFailures !== undefined) parts.push(`chat-failures=${voice.chatFailures}`);
  if (voice.chatTts !== undefined) parts.push(`chat-tts=${voice.chatTts}`);
  if (voice.mic !== undefined && voice.mic !== "unknown") parts.push(`mic=${voice.mic}`);
  return parts.join("  ");
}

function compactTokens(tokens: number): string {
  if (tokens < 1_000) return String(tokens);
  const [divisor, suffix] = tokens >= 1_000_000 ? [1_000_000, "M"] : [1_000, "k"];
  return `${Number((tokens / divisor).toPrecision(3))}${suffix}`;
}

export function formatDiagnostics(info: DiagnosticsInfo): string {
  const lines: string[] = [
    "JARVIS DIAGNOSTICS",
    "Safe to share with support: anonymous install code and setup step results only —",
    "no keys, no account details, no message content.",
    "",
    `support code   ${info.supportCode ?? "unavailable"}`,
    `app version    ${info.appVersion}`,
    `platform       ${platformLabel(info.platform, info.arch, info.packaged)}`,
  ];

  const crash = info.lastCrash;
  if (crash !== undefined) {
    const detail = crash.detail === undefined ? "" : ` (${crash.detail})`;
    const times = crash.count === 1 ? "" : `, ${crash.count} total`;
    lines.push(`last crash     ${whenLabel(crash.at)} — ${crash.reason}${detail} (v${crash.appVersion}${times})`);
  }

  // Lane C (voice): stated even when unavailable — "voice off (assets-corrupt)"
  // is the single most useful line support can get from a "he won't talk to me"
  // email, and its absence would read as "voice is fine".
  const voiceState = info.voice;
  if (voiceState !== undefined) {
    lines.push(`voice          ${formatVoice(voiceState)}`);
  }

  for (const provider of info.brainLedger ?? []) {
    const days = [...provider.days].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 7);
    if (days.length === 0) continue;
    lines.push(`brain          ${provider.provider}`);
    const rows = days.map((day) => {
      const classes = Object.entries(day.byClass).map(([name, turns]) => `${name} ${turns}`).join(", ");
      const models = Object.entries(day.models)
        .map(([name, turns]) => `${name.replace(/^claude-/, "")} ${turns}`)
        .join(", ");
      return [
        day.date,
        `${day.turns} turns${classes === "" ? "" : ` (${classes})`}`,
        `in ${compactTokens(day.inputTokens)}`,
        `out ${compactTokens(day.outputTokens)}`,
        models,
      ];
    });
    const widths = rows[0].slice(0, 4).map((_, index) => Math.max(...rows.map((row) => row[index].length)) + 3);
    for (const row of rows) {
      lines.push(`  ${row.slice(0, 4).map((column, index) => column.padEnd(widths[index])).join("")}${row[4]}`.trimEnd());
    }
  }

  const run = info.lastRun;
  if (run === undefined) {
    lines.push("last setup     no setup run recorded on this machine");
    return `${lines.join("\n")}\n`;
  }

  lines.push(
    `last setup     ${whenLabel(run.at)} (v${run.appVersion}) — ${run.failed ? "FAILED" : "completed"}`,
  );
  if (run.steps.length > 0) {
    lines.push("steps");
    const width = Math.max(...run.steps.map((step) => step.id.length));
    for (const step of run.steps) {
      const status = step.status === "failed" ? "FAILED" : step.status;
      const props = Object.entries(step.props ?? {})
        .map(([key, value]) => `${key}=${String(value)}`)
        .join("  ");
      lines.push(`  ${step.id.padEnd(width)}  ${status}${props === "" ? "" : `  ${props}`}`);
    }
  }
  return `${lines.join("\n")}\n`;
}
