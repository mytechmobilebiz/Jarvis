// How current the system status is, and what each internal service's state may honestly be shown as (C2-03, C2-04).
import type { SystemStatus } from "./api";

/** What the shell knows about its own status poll. Passed to pages together with the last status. */
export interface StatusMeta {
  /** The last poll's error, or null when the last poll succeeded (or none has finished yet). */
  error: string | null;
  /** When a poll last succeeded, in ms since the epoch; null before the first success. */
  lastSuccessAt: number | null;
}

/** A status older than this is no longer shown as current. The poll runs every 10 s, so this allows two misses. */
export const STATUS_STALE_MS = 30_000;
/** n8n counts as connected only if it answered within this time (and has not failed since). */
export const N8N_FRESH_MS = 3 * 60_000;

/** Fields the 0.5.0 API adds to /api/system/status. Optional, so an older API's answer still fits. */
export type StatusWithHealth = SystemStatus & {
  n8n_editor_url?: string | null;
  llm: SystemStatus["llm"] & { last_ok_at?: string | null; last_error_at?: string | null; last_error?: string };
};

/** True when the status on screen cannot be trusted as current: the last poll failed, or the last success is too old. */
export function isStale(meta: StatusMeta, now: number = Date.now()): boolean {
  return meta.error !== null || (meta.lastSuccessAt !== null && now - meta.lastSuccessAt > STATUS_STALE_MS);
}

function ms(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

export type EngineState = "none" | "not-contacted" | "connected" | "failing" | "not-checked";

/** The workflow engine's state. Ages are measured on the server's clock (`status.time`), so a wrong PC clock cannot make
 * an old check look fresh; `now` is only the fallback when the status carries no time. */
export function engineState(s: SystemStatus, now: number = Date.now()): EngineState {
  if (s.workflow_engine === "none") return "none";
  if (!s.n8n) return "not-contacted";
  const ok = ms(s.n8n.last_ok_at);
  const failed = ms(s.n8n.last_error_at);
  if (failed !== null && (ok === null || failed > ok)) return "failing";
  if (ok === null) return "not-checked";
  const ref = ms(s.time) ?? now;
  return ref - ok <= N8N_FRESH_MS ? "connected" : "not-checked";
}

export type AiState = "rule-based" | "configured" | "ok" | "failing";

/** The AI planner: rule-based, configured (no call yet), last call OK, or last call failed. */
export function aiState(s: SystemStatus): AiState {
  const llm = (s as StatusWithHealth).llm;
  if (!llm.configured) return "rule-based";
  const ok = ms(llm.last_ok_at);
  const failed = ms(llm.last_error_at);
  if (failed !== null && (ok === null || failed > ok)) return "failing";
  return ok !== null ? "ok" : "configured";
}

/** Dot states: good, warn, bad; muted = not in use or still loading; unknown = no current status to judge by. */
export type Health = "good" | "warn" | "bad" | "muted" | "unknown";

export interface ServiceHealth {
  worker: Health;
  n8n: Health;
  database: Health;
  ai: Health;
}

/** One state per internal service. Everything is "unknown" while the status is stale, so an old answer never shows green. */
export function serviceHealth(s: SystemStatus | null, meta?: StatusMeta, now: number = Date.now()): ServiceHealth {
  if (!s) return { worker: "muted", n8n: "muted", database: "muted", ai: "muted" };
  if (meta && isStale(meta, now)) return { worker: "unknown", n8n: "unknown", database: "unknown", ai: "unknown" };
  const engine = engineState(s, now);
  const ai = aiState(s);
  return {
    worker: s.workers.some((w) => w.alive) ? "good" : "bad",
    // No workflow engine set up, or not contacted yet: nothing to report, not a problem.
    n8n: engine === "none" || engine === "not-contacted" ? "muted" : engine === "connected" ? "good" : "warn",
    database: s.database.ok ? "good" : "bad",
    ai: ai === "rule-based" ? "muted" : ai === "failing" ? "warn" : "good",
  };
}

/** "10:21:05" in the viewer's locale. */
export function clockTime(at: number | string): string {
  return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** The n8n editor address to link to, or null: only an http(s) URL the API reports. */
export function n8nEditorUrl(s: SystemStatus | null): string | null {
  const url = (s as StatusWithHealth | null)?.n8n_editor_url;
  return url && /^https?:\/\//i.test(url) ? url : null;
}
