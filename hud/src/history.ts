import fs from "node:fs";
import path from "node:path";
// Driver choice: node:sqlite ships inside the runtime, so the HUD reads Hermes
// archives with zero native dependencies — nothing for electron-builder to
// rebuild on a buyer's machine and nothing to break when Electron bumps its ABI.
// Verified working unflagged in Electron 37 (Node 22.21) and in plain Node 22.17,
// including read-only reads across a live WAL, which better-sqlite3 would only
// match at the cost of a native module in the installer.
import { DatabaseSync } from "node:sqlite";
import { redactSecrets } from "@jarvis/setup/engine";
import { resolveHermesRoot } from "@jarvis/setup/hermes-root";
import { HUD_AGENT_ROLES, type AgentRole } from "./state-server.js";

/**
 * Setup composes the whole household into one Hermes profile: `compose-roles.ts`
 * merges every staff member's skills, knowledge and persona into `jarvis` and
 * registers their schedules with `hermes -p jarvis cron create --name <jobName>`.
 * So one archive holds everyone's work, and a staff member's own sessions are
 * exactly the cron runs of the jobs that were registered for them.
 */
const COMPOSED_PROFILE: AgentRole = "jarvis";

/**
 * Cron job name → the staff member it belongs to, mirroring the `name` field of
 * every `agents/<role>/cron/jobs.json` in this repo. These names are
 * product-authored and MUST stay unique across `agents/*` — a duplicate would
 * silently hand one agent's sessions to another. `history.test.ts` reads those
 * files and fails if the names drift from this table or collide.
 */
export const CRON_JOB_ROLES: Readonly<Record<string, AgentRole>> = {
  "morning-brief": "jarvis",
  "triage-inbox": "sarah",
  "roas-report": "bobby",
  "weekly-sweep": "scout",
  "write-hooks": "ava",
};

export const SESSION_PAGE_SIZE = 100;
/** Paging stops here: the page served at this offset is always the last one. */
export const MAX_SESSION_OFFSET = 5_000;
const MESSAGE_LIMIT = 2_000;
const CONTENT_LIMIT = 20_000;
const ARGS_LIMIT = 220;

// `cron_<jobId>_<YYYYmmdd>_<HHMMSS>` (cron/scheduler.py). The job id is lazy so
// an id containing underscores still yields the right capture.
const CRON_SESSION_ID = /^cron_(.+?)_\d{8}_\d{6}$/;
// GLOB metacharacters would turn a job id into a wildcard, so ids are only used
// in a pattern when they are plain identifiers.
const SAFE_JOB_ID = /^[A-Za-z0-9_-]+$/;

export type HistoryUnavailableReason = "unknown-agent" | "no-profile" | "unknown-session" | "unreadable";
export type TranscriptRole = "user" | "assistant" | "tool" | "system";
export type SessionKind = "normal" | "delegated" | "branch" | "continuation";

export interface HistorySession {
  id: string;
  title: string | null;
  source: string;
  startedAt: number;
  endedAt: number | null;
  messageCount: number;
  toolCallCount: number;
  costUsd: number | null;
  kind: SessionKind;
}

export interface HistoryToolCall {
  name: string;
  argsSummary: string;
}

export interface HistoryMessage {
  role: TranscriptRole;
  content: string;
  contentTruncated: boolean;
  toolName: string | null;
  toolCalls: HistoryToolCall[];
  timestamp: number;
}

export interface SessionListResult {
  available: boolean;
  reason?: HistoryUnavailableReason;
  sessions: HistorySession[];
  hasMore: boolean;
}

export interface TranscriptResult {
  available: boolean;
  reason?: HistoryUnavailableReason;
  messages: HistoryMessage[];
  truncated: boolean;
}

export interface HistoryOptions {
  /** Overridable so tests read fixture archives instead of the live Hermes home. */
  profilesDir?: string;
  /** How many sessions to skip; the renderer pages through with this. */
  offset?: number;
}

export function defaultProfilesDir(): string {
  return path.join(resolveHermesRoot(), "profiles");
}

/**
 * The one place a HUD role becomes a filesystem path. The allow-list doubles as
 * path-traversal defence: a role that is not in HUD_AGENT_ROLES never reaches
 * the filesystem.
 */
export function archivePathFor(role: string, options: HistoryOptions = {}): string | null {
  if (!isAgentRole(role)) return null;
  return path.join(options.profilesDir ?? defaultProfilesDir(), role, "state.db");
}

export function listSessions(role: string, options: HistoryOptions = {}): SessionListResult {
  if (!isAgentRole(role)) {
    return { available: false, reason: "unknown-agent", sessions: [], hasMore: false };
  }

  const offset = normalizeOffset(options.offset);
  // Each source is ordered newest first, so reading this many rows from every
  // source is enough to merge the requested window correctly.
  const window = offset + SESSION_PAGE_SIZE + 1;
  const jobRoles = readCronJobRoles(options);
  const collected: HistorySession[] = [];
  let opened = false;
  let failed = false;

  for (const source of sourcesFor(role, options)) {
    const result = readSessions(source.file, window, source.attributeFor, jobRoles);
    if (result === null) continue;
    if (result.failed) {
      failed = true;
      continue;
    }
    opened = true;
    collected.push(...result.sessions);
  }

  if (!opened) {
    return { available: false, reason: failed ? "unreadable" : "no-profile", sessions: [], hasMore: false };
  }

  collected.sort((left, right) => right.startedAt - left.startedAt);
  return {
    available: true,
    sessions: collected.slice(offset, offset + SESSION_PAGE_SIZE),
    // The cap is terminal, not a silent clamp: a page served at the ceiling
    // reports no more, so paging cannot loop on the same rows forever.
    hasMore: offset >= MAX_SESSION_OFFSET ? false : collected.length > offset + SESSION_PAGE_SIZE,
  };
}

export function getTranscript(role: string, sessionId: string, options: HistoryOptions = {}): TranscriptResult {
  if (!isAgentRole(role)) {
    return { available: false, reason: "unknown-agent", messages: [], truncated: false };
  }

  const jobRoles = readCronJobRoles(options);
  let reachable = false;

  for (const source of sourcesFor(role, options)) {
    // In the composed archive a session only belongs to this agent if its cron
    // job was registered for them; a session id from the renderer that fails
    // this check is refused even though the row exists.
    if (source.attributeFor !== null && attributedRole(sessionId, jobRoles) !== source.attributeFor) {
      reachable = reachable || fs.existsSync(source.file);
      continue;
    }

    const result = readTranscript(source.file, sessionId);
    if (result === null) continue;
    reachable = true;
    if (result.found) return { available: true, messages: result.messages, truncated: result.truncated };
  }

  return {
    available: false,
    reason: reachable ? "unknown-session" : "no-profile",
    messages: [],
    truncated: false,
  };
}

interface ArchiveSource {
  file: string;
  /** The role to attribute composed-archive rows to, or null to take every row. */
  attributeFor: AgentRole | null;
}

/**
 * The composed `jarvis` archive is always consulted. A power user who really did
 * install a standalone profile for a staff member gets that archive merged in on
 * top — same allow-list, so still no path traversal.
 */
function sourcesFor(role: AgentRole, options: HistoryOptions): ArchiveSource[] {
  const composed = archivePathFor(COMPOSED_PROFILE, options);
  const sources: ArchiveSource[] = composed === null ? [] : [{ file: composed, attributeFor: role }];
  if (role === COMPOSED_PROFILE) return sources;

  const own = archivePathFor(role, options);
  if (own !== null && fs.existsSync(own)) {
    sources.push({ file: own, attributeFor: null });
  }
  return sources;
}

interface SessionRead {
  sessions: HistorySession[];
  failed: boolean;
}

function readSessions(
  file: string,
  window: number,
  attributeFor: AgentRole | null,
  jobRoles: Map<string, AgentRole>,
): SessionRead | null {
  const db = openReadOnly(file);
  if (db === null) return null;

  try {
    const available = columnsOf(db, "sessions");
    if (available.size === 0) return null;
    const lineage = lineageColumns(available);
    const filter = attributeFor === null ? { sql: "", params: [] } : attributionClause(attributeFor, jobRoles);

    const rows = db
      .prepare(
        `SELECT s.id, s.title, s.display_name, s.source, s.started_at, s.ended_at,
                s.message_count, s.tool_call_count, s.estimated_cost_usd, s.actual_cost_usd${lineage.select}
         FROM sessions s${lineage.join}
         WHERE s.archived = 0${filter.sql}
         ORDER BY s.started_at DESC
         LIMIT ?`,
      )
      .all(...filter.params, window);

    return {
      failed: false,
      sessions: rows.map((row) => ({
        id: text(row.id),
        title: optionalText(row.title) ?? optionalText(row.display_name),
        source: text(row.source) || "unknown",
        startedAt: numberOr(row.started_at, 0),
        endedAt: optionalNumber(row.ended_at),
        messageCount: numberOr(row.message_count, 0),
        toolCallCount: numberOr(row.tool_call_count, 0),
        costUsd: optionalNumber(row.actual_cost_usd) ?? optionalNumber(row.estimated_cost_usd),
        kind: sessionKind(row),
      })),
    };
  } catch (error) {
    console.warn(`Unable to list sessions in ${file}.`, error);
    return { sessions: [], failed: true };
  } finally {
    db.close();
  }
}

interface TranscriptRead {
  found: boolean;
  messages: HistoryMessage[];
  truncated: boolean;
}

function readTranscript(file: string, sessionId: string): TranscriptRead | null {
  const db = openReadOnly(file);
  if (db === null) return null;

  try {
    const known = db.prepare("SELECT id FROM sessions WHERE id = ? LIMIT 1").get(sessionId);
    if (known === undefined) return { found: false, messages: [], truncated: false };

    const rows = db
      .prepare(
        `SELECT role, content, tool_name, tool_calls, timestamp
         FROM messages
         WHERE session_id = ?${visibleMessagesClause(columnsOf(db, "messages"))}
         ORDER BY id ASC
         LIMIT ?`,
      )
      .all(sessionId, MESSAGE_LIMIT + 1);

    const messages: HistoryMessage[] = [];
    for (const row of rows.slice(0, MESSAGE_LIMIT)) {
      const full = text(row.content);
      const message: HistoryMessage = {
        role: transcriptRole(row.role),
        content: truncate(full, CONTENT_LIMIT),
        contentTruncated: full.length > CONTENT_LIMIT,
        toolName: optionalText(row.tool_name),
        toolCalls: parseToolCalls(row.tool_calls),
        timestamp: numberOr(row.timestamp, 0),
      };
      // Bookkeeping rows (session_meta and friends) carry nothing to show and
      // would otherwise render as blank bubbles.
      if (message.content === "" && message.toolCalls.length === 0 && message.toolName === null) continue;
      messages.push(message);
    }

    return { found: true, messages, truncated: rows.length > MESSAGE_LIMIT };
  } catch (error) {
    console.warn(`Unable to read a transcript in ${file}.`, error);
    return { found: false, messages: [], truncated: false };
  } finally {
    db.close();
  }
}

/**
 * Hermes's own discoverability rule (hermes_state.py): live rows and
 * compaction-archived rows are both real history — `archive_and_compact` flips
 * the original turns to `active = 0, compacted = 1` and inserts the summary as
 * fresh active rows. Only rewind/undo rows (`active = 0, compacted = 0`) are
 * hidden. A compacted session therefore reads as the original turns followed by
 * the summary, which is the honest account of what happened.
 */
function visibleMessagesClause(available: Set<string>): string {
  if (!available.has("active")) return "";
  return available.has("compacted") ? " AND (active = 1 OR compacted = 1)" : " AND active = 1";
}

function openReadOnly(file: string): DatabaseSync | null {
  try {
    // Read-only, always: the Hermes home is the buyer's production state. SQLite still
    // reads a live WAL through this handle, and the HUD never writes a byte.
    return new DatabaseSync(file, { readOnly: true, enableForeignKeyConstraints: false, timeout: 2_000 });
  } catch {
    return null;
  }
}

/**
 * Which staff member owns a session in the composed archive: cron runs belong to
 * the agent their job was registered for, everything else is Jarvis's own work.
 * A cron job the household did not author (or one whose name has drifted) stays
 * with Jarvis rather than disappearing from every tile.
 */
function attributedRole(sessionId: string, jobRoles: Map<string, AgentRole>): AgentRole {
  const jobId = CRON_SESSION_ID.exec(sessionId)?.[1];
  if (jobId === undefined) return COMPOSED_PROFILE;
  return jobRoles.get(jobId) ?? COMPOSED_PROFILE;
}

function attributionClause(role: AgentRole, jobRoles: Map<string, AgentRole>): { sql: string; params: string[] } {
  const own: string[] = [];
  const others: string[] = [];
  for (const [jobId, jobRole] of jobRoles) {
    (jobRole === role ? own : others).push(`cron_${jobId}_*`);
  }

  if (role === COMPOSED_PROFILE) {
    if (others.length === 0) return { sql: "", params: [] };
    return { sql: ` AND ${others.map(() => "s.id NOT GLOB ?").join(" AND ")}`, params: others };
  }

  // No registered schedule means no sessions of their own in the shared archive.
  if (own.length === 0) return { sql: " AND 0", params: [] };
  return { sql: ` AND (${own.map(() => "s.id GLOB ?").join(" OR ")})`, params: own };
}

/**
 * Hermes job ids are assigned at registration, so the profile's own jobs.json is
 * the only link from the wizard's `--name` to the id embedded in session ids.
 */
function readCronJobRoles(options: HistoryOptions): Map<string, AgentRole> {
  const jobRoles = new Map<string, AgentRole>();
  const file = path.join(options.profilesDir ?? defaultProfilesDir(), COMPOSED_PROFILE, "cron", "jobs.json");

  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return jobRoles;
  }

  const jobs = (parsed as { jobs?: unknown })?.jobs;
  if (!Array.isArray(jobs)) return jobRoles;

  for (const job of jobs) {
    if (typeof job !== "object" || job === null) continue;
    const record = job as Record<string, unknown>;
    const id = typeof record.id === "string" ? record.id : "";
    const name = typeof record.name === "string" ? record.name : "";
    const role = CRON_JOB_ROLES[name];
    if (role !== undefined && SAFE_JOB_ID.test(id)) {
      jobRoles.set(id, role);
    }
  }
  return jobRoles;
}

/**
 * Lineage markers, straight from Hermes: the `_delegate_from` and `_branched_from`
 * keys in model_config, plus the legacy heuristics on the parent's end_reason.
 * Older archives may predate model_config, so the columns are probed first and
 * the query degrades to whatever the schema really has.
 */
function lineageColumns(available: Set<string>): { select: string; join: string } {
  const hasParent = available.has("parent_session_id");
  const hasEndReason = available.has("end_reason");
  const parts: string[] = [];

  if (available.has("model_config")) {
    parts.push(
      ", json_extract(COALESCE(s.model_config, '{}'), '$._delegate_from') AS delegate_from",
      ", json_extract(COALESCE(s.model_config, '{}'), '$._branched_from') AS branched_from",
    );
  }
  if (hasParent && hasEndReason) {
    parts.push(", p.end_reason AS parent_end_reason");
  }

  return {
    select: parts.join(""),
    join: hasParent && hasEndReason ? " LEFT JOIN sessions p ON p.id = s.parent_session_id" : "",
  };
}

function sessionKind(row: Record<string, unknown>): SessionKind {
  const parentEndReason = optionalText(row.parent_end_reason);
  if (optionalText(row.delegate_from) !== null) return "delegated";
  if (optionalText(row.branched_from) !== null || parentEndReason === "branched") return "branch";
  if (parentEndReason === "compression") return "continuation";
  return "normal";
}

function columnsOf(db: DatabaseSync, table: string): Set<string> {
  const columns = new Set<string>();
  for (const row of db.prepare(`PRAGMA table_info(${table})`).all()) {
    const name = (row as Record<string, unknown>).name;
    if (typeof name === "string") columns.add(name);
  }
  return columns;
}

function parseToolCalls(raw: unknown): HistoryToolCall[] {
  const source = optionalText(raw);
  if (source === null) return [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];

  const calls: HistoryToolCall[] = [];
  for (const entry of parsed) {
    if (typeof entry !== "object" || entry === null) continue;
    const record = entry as Record<string, unknown>;
    const fn = (typeof record.function === "object" && record.function !== null ? record.function : {}) as Record<string, unknown>;
    const name = optionalText(fn.name) ?? optionalText(record.name) ?? "tool";
    calls.push({ name, argsSummary: summarizeArgs(fn.arguments ?? record.arguments ?? record.input) });
  }
  return calls;
}

function summarizeArgs(raw: unknown): string {
  let value = raw;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed === "") return "";
    try {
      value = JSON.parse(trimmed);
    } catch {
      return truncate(clean(trimmed), ARGS_LIMIT);
    }
  }

  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return value === undefined ? "" : truncate(clean(stringify(value)), ARGS_LIMIT);
  }

  const parts = Object.entries(value as Record<string, unknown>).map(([key, entry]) => `${key}=${stringify(entry)}`);
  return truncate(clean(parts.join("  ")), ARGS_LIMIT);
}

function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return String(value);
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function transcriptRole(raw: unknown): TranscriptRole {
  const value = text(raw).toLowerCase();
  if (value === "user" || value === "assistant" || value === "tool") return value;
  return "system";
}

/**
 * The single redaction choke-point. Every string in these payloads is built here
 * or by `text()`/`optionalText()`, so transcripts that echoed an API key back at
 * the operator leave the main process already scrubbed.
 */
function clean(value: string): string {
  return redactSecrets(value.replace(/\s+/g, " ").trim());
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  return redactSecrets(String(value)).trim();
}

function optionalText(value: unknown): string | null {
  const result = text(value);
  return result === "" ? null : result;
}

function numberOr(value: unknown, fallback: number): number {
  const result = typeof value === "bigint" ? Number(value) : value;
  return typeof result === "number" && Number.isFinite(result) ? result : fallback;
}

function optionalNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const result = typeof value === "bigint" ? Number(value) : value;
  return typeof result === "number" && Number.isFinite(result) ? result : null;
}

function normalizeOffset(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return 0;
  return Math.min(value, MAX_SESSION_OFFSET);
}

function truncate(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function isAgentRole(value: string): value is AgentRole {
  return (HUD_AGENT_ROLES as readonly string[]).includes(value);
}
