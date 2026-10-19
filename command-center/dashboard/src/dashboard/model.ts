// Pure rules behind the dashboard's figures and states, kept apart from the components so they can be tested directly.
import type { Agent, Site, SystemStatus, TaskActivity } from "../api";
import { isStale, type StatusMeta } from "../status";

/** Dot and text tones: good, warn, bad; muted = not in use or loading; unknown = no current data to judge by. */
export type Tone = "good" | "warn" | "bad" | "muted" | "unknown";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function ms(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/** "<1s", "45s", "2m 14s", "1h 05m". */
export function formatDuration(milliseconds: number): string {
  const s = Math.max(0, Math.floor(milliseconds / 1000));
  if (s < 1) return "<1s";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** "40 s", "12 min", "3 h", "2 days". */
export function span(milliseconds: number): string {
  const s = Math.max(0, Math.round(milliseconds / 1000));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return `${d} day${d === 1 ? "" : "s"}`;
}

/** "40 s ago", "12 min ago", "3 h ago", "2 days ago". */
export function ago(milliseconds: number): string {
  return `${span(milliseconds)} ago`;
}

/** "10:22 AM" in the viewer's locale. */
export function clock(at: number | string): string {
  return new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** "Oct 12" in the viewer's locale. */
export function shortDate(at: number | string): string {
  return new Date(at).toLocaleDateString([], { month: "short", day: "numeric" });
}

/* ---- stat cards ---- */

/** Fewer checks than this and the uptime figure says "Not enough data yet" rather than a percentage. */
export const MIN_UPTIME_CHECKS = 4;

export interface UptimeSummary {
  /** Share of checks that found a monitored site up over 30 days, weighted by checks; null without checks. */
  value: number | null;
  checks: number;
  sites: number;
  /** The oldest check when it is younger than 30 days (a young install), else null. */
  since: string | null;
}

/** Check-weighted 30-day website uptime of the monitored (enabled) sites. Paused sites are left out. */
export function websiteUptime(sites: Site[], now: number = Date.now()): UptimeSummary {
  let up = 0;
  let checks = 0;
  let count = 0;
  let first: number | null = null;
  for (const s of sites) {
    const u = s.uptime;
    if (!s.enabled || !u || u["30d"] == null) continue;
    const n = u.n_30d ?? u.checks_30d ?? 0;
    if (n <= 0) continue;
    up += (u["30d"] / 100) * n;
    checks += n;
    count += 1;
    const f = ms(u.first_check_at);
    if (f !== null && (first === null || f < first)) first = f;
  }
  const young = first !== null && now - first < 30 * DAY - HOUR;
  return { value: checks ? (up / checks) * 100 : null, checks, sites: count, since: young ? new Date(first!).toISOString() : null };
}

/** "99.9%", never rounded up to a figure the checks did not reach ("100%" only when every check was up). */
export function formatUptime(v: number): string {
  if (v >= 100) return "100%";
  return `${(Math.floor(v * 10) / 10).toFixed(1)}%`;
}

export function uptimeTone(v: number): Tone {
  return v < 98 ? "bad" : v < 99.5 ? "warn" : "good";
}

/** Below this many tasks yesterday by the same time, a percentage change means little. */
export const MIN_TREND_BASE = 5;

/** Today's completed tasks against yesterday's by the same clock time. */
export function tasksTrend(a: TaskActivity): { text: string; dir: "up" | "down" | "same" | null } {
  const y = a.yesterday_same_time.succeeded;
  const t = a.today.succeeded;
  if (y < MIN_TREND_BASE) return { text: "No comparison yet", dir: null };
  const pct = Math.round(((t - y) / y) * 100);
  if (pct === 0) return { text: "Same as yesterday", dir: "same" };
  return { text: `${Math.abs(pct)}% vs yesterday`, dir: pct > 0 ? "up" : "down" };
}

/** The date card's status line: "System Online" only after a successful status poll under 30 s ago, with the database
 * answering and a worker alive. */
export function systemLine(status: SystemStatus | null, meta: StatusMeta, now: number = Date.now()): { tone: Tone; text: string } {
  const last = meta.lastSuccessAt;
  if (meta.error) return { tone: "bad", text: `Cannot reach the Command Center${last ? ` · last update ${clock(last)}` : ""}` };
  if (!status || last === null) return { tone: "muted", text: "Checking…" };
  if (isStale(meta, now)) return { tone: "warn", text: `Not updated since ${clock(last)}` };
  if (!status.database.ok) return { tone: "bad", text: "Database not answering" };
  if (!status.workers.some((w) => w.alive)) return { tone: "warn", text: "Worker not reporting" };
  return { tone: "good", text: "System Online" };
}

/* ---- agents ---- */

export function specialists(agents: Agent[]): Agent[] {
  return agents.filter((a) => a.role === "agent");
}

/** "Active" is green only for an agent that has really run; one that never ran is switched on, nothing more (C2-06). */
export function agentStatus(a: Agent): { tone: Tone; text: string } {
  if (!a.entitled) return { tone: "muted", text: "Not in this edition" };
  if (!a.enabled) return { tone: "muted", text: "Switched off" };
  if ((a.stats_24h?.failed ?? 0) > 0) return { tone: "warn", text: "Needs attention" };
  return { tone: a.last_run_at ? "good" : "muted", text: "Active" };
}

/** Current work from `active`, e.g. "2 running · 1 queued", or "Idle · last ran 5 min ago". `workerAlive` false marks
 * running tasks as stuck; null when the status is unknown. */
export function agentActivity(a: Agent, workerAlive: boolean | null, now: number = Date.now()): { text: string; tone?: Tone } {
  const act = a.active;
  const parts: string[] = [];
  if (act?.running) parts.push(`${act.running} running`);
  if (act?.queued) parts.push(`${act.queued} queued`);
  if (act?.awaiting_approval) parts.push(`${act.awaiting_approval} awaiting approval`);
  if (parts.length) {
    if (act?.running && workerAlive === false) return { text: `${parts.join(" · ")} · worker not reporting`, tone: "warn" };
    return { text: parts.join(" · ") };
  }
  const last = ms(a.last_run_at);
  // Without `active` (an older API) nothing says the agent is idle, so only the last run is shown.
  const when = last === null ? "not run yet" : `ran ${ago(now - last)}`;
  return { text: act ? `Idle · ${when}` : when.charAt(0).toUpperCase() + when.slice(1) };
}

/** Share of the tasks created in the last 24 hours that finished and succeeded. */
export function successRate(a: Agent): { rate: number | null; runs: number; failed: number } {
  const ok = a.stats_24h?.succeeded ?? 0;
  const failed = a.stats_24h?.failed ?? 0;
  const runs = ok + failed;
  return { rate: runs ? ok / runs : null, runs, failed };
}
