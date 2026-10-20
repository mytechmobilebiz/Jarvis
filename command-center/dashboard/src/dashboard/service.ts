// Service Status rows: each says what was checked and when, and claims nothing it cannot verify (owner request 3).
import type { Integration, ResearchHealth, Server, SystemStatus } from "../api";
import { aiState, engineState, isStale, statusAge, type StatusMeta } from "../status";
import { ago, clock, ms, type Tone } from "./model";

export interface ServiceRow {
  key: string;
  name: string;
  tone: Tone;
  /** The state, short: "Running", "Mock", "Not configured". */
  state: string;
  /** What was checked and when: "Probed 40 s ago". */
  detail: string;
  /** Extra detail for a tooltip, e.g. the last error. */
  title?: string;
}

interface Loaded<T> {
  data: T | null;
  error: string | null;
}

/** A server-side age: both times from the API's clock, so a wrong PC clock cannot make an old check look fresh. */
const serverAge = statusAge;

function stale(meta: StatusMeta): string {
  return meta.lastSuccessAt ? `Last status ${clock(meta.lastSuccessAt)}` : "No status yet";
}

function engineRow(s: SystemStatus, now: number): ServiceRow {
  const webhook = s.workflow_engine === "webhook";
  const name = webhook ? "Webhook" : "n8n";
  const st = engineState(s, now);
  const last = s.n8n;
  if (st === "none") return { key: "engine", name: "Workflow engine", tone: "muted", state: "Not configured", detail: "Events are recorded, not handed on" };
  // The webhook is never probed: only its deliveries are known, so it is never "Running" and quiet time is no warning (C2-04).
  if (st === "not-contacted")
    return webhook
      ? { key: "engine", name, tone: "muted", state: "No delivery yet", detail: "Not probed; only deliveries are recorded" }
      : { key: "engine", name, tone: "muted", state: "Not contacted yet", detail: "Waiting for the first check" };
  if (st === "failing")
    return {
      key: "engine",
      name,
      tone: "warn",
      state: webhook ? "Last delivery failed" : "Failing",
      detail: `Failed ${ago(serverAge(s, last?.last_error_at, now) ?? 0)}`,
      title: last?.last_error || undefined,
    };
  const okAge = serverAge(s, last?.last_ok_at, now) ?? 0;
  if (st === "delivered") return { key: "engine", name, tone: "muted", state: "Last delivery OK", detail: `${ago(okAge)} · not probed` };
  if (st === "not-checked") return { key: "engine", name, tone: "unknown", state: "Not checked recently", detail: `Last answered ${ago(okAge)}` };
  if ((last?.detail as { ready?: boolean | null } | undefined)?.ready === false)
    return { key: "engine", name, tone: "warn", state: "Not ready", detail: `Answered ${ago(okAge)}, still starting` };
  return { key: "engine", name, tone: "good", state: "Running", detail: `Probed ${ago(okAge)}` };
}

function databaseRow(s: SystemStatus | null, meta: StatusMeta, now: number): ServiceRow {
  const base = { key: "database", name: "PostgreSQL / API" };
  if (meta.error) return { ...base, tone: "bad", state: "Not answering", detail: stale(meta), title: meta.error };
  if (!s || meta.lastSuccessAt === null) return { ...base, tone: "muted", state: "Checking…", detail: "Waiting for the first status" };
  if (isStale(meta, now)) return { ...base, tone: "unknown", state: "Unknown", detail: stale(meta) };
  if (!s.database.ok) return { ...base, tone: "bad", state: "Error", detail: `Answered ${ago(now - meta.lastSuccessAt)}` };
  return { ...base, tone: "good", state: "Running", detail: `Answered ${ago(now - meta.lastSuccessAt)}` };
}

function workerRow(s: SystemStatus, now: number): ServiceRow {
  const base = { key: "worker", name: "Worker" };
  const newest = [...s.workers].sort((a, b) => (ms(b.last_seen_at) ?? 0) - (ms(a.last_seen_at) ?? 0))[0];
  if (!newest) return { ...base, tone: "bad", state: "Not reporting", detail: "No heartbeat yet" };
  const age = serverAge(s, newest.last_seen_at, now) ?? 0;
  if (!s.workers.some((w) => w.alive)) return { ...base, tone: "bad", state: "Not reporting", detail: `Last heartbeat ${ago(age)}` };
  return { ...base, tone: "good", state: "Running", detail: `Heartbeat ${ago(age)}` };
}

function aiRow(s: SystemStatus, now: number): ServiceRow {
  const base = { key: "ai", name: "AI (Claude)" };
  const llm = s.llm;
  const st = aiState(s);
  if (st === "rule-based") return { ...base, name: "AI planner", tone: "muted", state: "Rule-based", detail: llm.reason ? sentence(llm.reason) : "No Anthropic API key set" };
  const model = llm.model ? `${llm.model} · ` : "";
  if (llm.daily_cap > 0 && llm.requests_today >= llm.daily_cap)
    return { ...base, tone: "warn", state: "Daily cap reached", detail: `${llm.requests_today} of ${llm.daily_cap} calls today (UTC); rules until midnight UTC` };
  if (st === "failing")
    return { ...base, tone: "warn", state: "Last call failed", detail: `${model}${ago(serverAge(s, llm.last_error_at, now) ?? 0)}`, title: llm.last_error || undefined };
  if (st === "ok") return { ...base, tone: "good", state: "Last call OK", detail: `${model}${ago(serverAge(s, llm.last_ok_at, now) ?? 0)}` };
  return { ...base, tone: "muted", state: "Configured", detail: `${model}not used yet` };
}

function sentence(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** A day without SSH contact and a server no longer counts as verified. */
export const SERVER_FRESH_MS = 24 * 3_600_000;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function since(iso: string | null, now: number): number | null {
  const t = ms(iso);
  return t === null ? null : now - t;
}

function notCheckedIn(age: number): string {
  return `not checked in ${plural(Math.max(1, Math.floor(age / 86_400_000)), "day")}`;
}

/** Each switched-on server is judged on its own: "Verified" only when every one had SSH contact within SERVER_FRESH_MS, so
 * a recently contacted server cannot hide one that was never, or long ago, contacted. */
function serversRow(servers: Loaded<Server[]>, status: SystemStatus | null, now: number): ServiceRow {
  const base = { key: "servers", name: "CWP servers" };
  if (!servers.data) return servers.error ? { ...base, tone: "unknown", state: "Unavailable", detail: servers.error } : { ...base, tone: "muted", state: "Checking…", detail: "" };
  // The list on screen is from before the failure: it says nothing about now.
  if (servers.error) return { ...base, tone: "unknown", state: "Unknown", detail: `Not updated · ${servers.error}` };
  const all = servers.data;
  if (!all.length) return { ...base, tone: "muted", state: "None added", detail: "No servers added" };
  const on = all.filter((x) => x.enabled);
  if (!on.length) return { ...base, tone: "muted", state: "Switched off", detail: `${plural(all.length, "server")}, none switched on` };
  const count = plural(on.length, "server");
  // Ages on the API's clock when there is a status; null = never contacted.
  const ages = on.map((x) => ({ name: x.name, age: status ? statusAge(status, x.last_seen_at, now) : since(x.last_seen_at, now) }));
  const unverified = ages.filter((a) => a.age === null || a.age > SERVER_FRESH_MS);
  if (!unverified.length) {
    const oldest = Math.max(...ages.map((a) => a.age ?? 0));
    return { ...base, tone: "good", state: "Verified", detail: `${count} · ${on.length > 1 ? "oldest " : ""}SSH contact ${ago(oldest)}` };
  }
  const title = unverified.map((a) => `${a.name}: ${a.age === null ? "no SSH contact yet" : `last SSH contact ${ago(a.age)}`}`).join("\n");
  // The worst first: a server never contacted, else the one contacted longest ago.
  const worst = unverified.reduce((a, b) => (a.age === null ? a : b.age === null || b.age > a.age ? b : a));
  if (on.length === 1) {
    if (worst.age === null) return { ...base, tone: "unknown", state: "Not checked", detail: `${count} · no SSH contact yet` };
    const text = notCheckedIn(worst.age);
    return { ...base, tone: "unknown", state: text.charAt(0).toUpperCase() + text.slice(1), detail: `${count} · last SSH contact ${ago(worst.age)}` };
  }
  const more = unverified.length - 1;
  return {
    ...base,
    tone: "unknown",
    state: `${on.length - unverified.length} of ${on.length} verified`,
    detail: `${worst.name}: ${worst.age === null ? "no SSH contact yet" : notCheckedIn(worst.age)}${more ? ` · ${more} more not verified` : ""}`,
    title,
  };
}

function channelRow(key: string, name: string, item: Integration | undefined, integrations: Loaded<Integration[]>, unsent: string, now: number): ServiceRow {
  const base = { key, name };
  if (!integrations.data) return integrations.error ? { ...base, tone: "unknown", state: "Unavailable", detail: integrations.error } : { ...base, tone: "muted", state: "Checking…", detail: "" };
  if (integrations.error) return { ...base, tone: "unknown", state: "Unknown", detail: `Not updated · ${integrations.error}` };
  if (!item) return { ...base, tone: "muted", state: "Not set up", detail: "Not in this installation" };
  if (item.mode === "mock") return { ...base, tone: "muted", state: "Mock", detail: `Recorded, not ${unsent}` };
  if (item.mode === "disabled") return { ...base, tone: "muted", state: "Off", detail: "Switched off" };
  const st = item.status;
  const ok = ms(st?.last_ok_at);
  const failed = ms(st?.last_error_at);
  if (failed !== null && (ok === null || failed > ok)) return { ...base, tone: "warn", state: "Last use failed", detail: ago(now - failed), title: st?.last_error || undefined };
  if (ok !== null) return { ...base, tone: "good", state: "Live", detail: `Last used ${ago(now - ok)}` };
  return { ...base, tone: "muted", state: "Live", detail: "Not used yet" };
}

/** Web research (0.7.0): the SearXNG service as the worker last found it, or the mode when research is off or a mock. Shown
 * to admins and operators only; the API refuses viewers (owner decision 13). */
function researchRow(research: Loaded<ResearchHealth>, now: number): ServiceRow {
  const base = { key: "research", name: "Web research" };
  if (!research.data) return research.error ? { ...base, tone: "unknown", state: "Unavailable", detail: research.error } : { ...base, tone: "muted", state: "Checking…", detail: "" };
  if (research.error) return { ...base, tone: "unknown", state: "Unknown", detail: `Not updated · ${research.error}` };
  const s = research.data.searxng;
  const probed = ms(s.probed_at);
  const when = probed === null ? "not probed yet" : `probed ${ago(Math.max(0, now - probed))}`;
  switch (s.state) {
    case "off":
      return { ...base, tone: "muted", state: "Off", detail: "Switched off; no searches" };
    case "mock":
      return { ...base, tone: "muted", state: "Mock", detail: "Made-up answers; nothing leaves this PC" };
    case "healthy":
      return { ...base, tone: "good", state: "SearXNG healthy", detail: sentence(when) };
    case "degraded":
      return { ...base, tone: "warn", state: "Degraded", detail: "Some search engines paused", title: s.message || undefined };
    case "all_engines_failing":
      return { ...base, tone: "warn", state: "All engines failing", detail: "SearXNG answers; its engines do not", title: s.message || undefined };
    case "pc_offline":
      return { ...base, tone: "warn", state: "PC offline", detail: "Searches cannot leave this PC" };
    case "unreachable":
      return { ...base, tone: "bad", state: "Unreachable", detail: `SearXNG not answering · ${when}`, title: s.last_error || undefined };
    default:
      return { ...base, tone: "unknown", state: "Unknown", detail: "" };
  }
}

/** Rows in the reference's order, then web research when `research` is given (admins and operators). Every row but the
 * database's turns "Unknown" while the status is stale. */
export function serviceRows(
  status: SystemStatus | null,
  meta: StatusMeta,
  servers: Loaded<Server[]>,
  integrations: Loaded<Integration[]>,
  now: number = Date.now(),
  research?: Loaded<ResearchHealth>,
): ServiceRow[] {
  const db = databaseRow(status, meta, now);
  // No status yet: still checking, or unknown once the first poll has failed.
  const none = (key: string, name: string): ServiceRow =>
    meta.error ? { key, name, tone: "unknown", state: "Unknown", detail: "No status yet" } : { key, name, tone: "muted", state: "Checking…", detail: "" };
  const fromStatus: ServiceRow[] = !status
    ? [none("engine", "n8n"), db, none("worker", "Worker"), none("ai", "AI (Claude)")]
    : [engineRow(status, now), db, workerRow(status, now), aiRow(status, now)];
  const list = integrations.data ?? [];
  const rows = [
    ...fromStatus,
    serversRow(servers, status, now),
    channelRow("email", "Email", list.find((i) => i.name === "email"), integrations, "sent", now),
    channelRow("postonomous", "Postonomous", list.find((i) => i.name === "postonomous"), integrations, "published", now),
    ...(research ? [researchRow(research, now)] : []),
  ];
  // While the API is not answering (or its last answer is too old), no row may look current: every row turns unknown, with
  // when the status was last known.
  if (!isStale(meta, now)) return rows;
  return rows.map((r) => (r.key === "database" ? r : { ...r, tone: "unknown" as Tone, state: "Unknown", detail: stale(meta), title: undefined }));
}
