// The Research Center (0.7.0; docs/RESEARCH_GATEWAY_PLAN.md section 3.16): web searches through the Research Gateway, for
// operators and admins only (owner decision 13: a viewer sees nothing here and the API refuses them). Searches run in the
// worker, through the private SearXNG service or, in mock mode, a stand-in that never leaves this PC; this page only asks
// for them and shows what came back. Web text is untrusted: titles, snippets and addresses are plain text (citations.tsx),
// and nothing here can approve, run or change anything. Not in this build: DeepSeek, page fetching, and promoting a page
// to the JARVIS Brain.
import { Component, FormEvent, Fragment, KeyboardEvent, ReactNode, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  ApiError,
  type Agent,
  type Capability,
  type ResearchBlocked,
  type ResearchHealth,
  type ResearchMode,
  type ResearchPolicy,
  type ResearchQuery,
  type ResearchSettings,
  type ResearchSource,
  type ResearchState,
  type ResearchUsage,
  type Task,
} from "../api";
import { useAuth } from "../App";
import { CAPS, CitationList, DEEPSEEK_NOTE, SENT_NOTICE, safeHref, toCitations, utcDate, utcTime, webText, webUrl } from "../citations";
import { Card, Empty, ErrorNote, Modal, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { TypedConfirm } from "./shared";
import "./Research.css";

const BASE = "/api/research";

/** A query is refused above this many characters (after its canonical form), never cut. */
export const MAX_QUERY = 128;
export const FETCH_NOTE = "Page fetching: not available in this build";
export const LIVE_CONFIRM = "ENABLE LIVE RESEARCH";
export const PURGE_CONFIRM = "PURGE RESEARCH";
export const NOT_FOR_VIEWERS = "The Research Center is for operators and admins.";

/** Searching capabilities started only from Policy and settings (backend app/tasks.py RESEARCH_TEST). */
const INTERNAL = new Set(["supervisor.research_connectivity_test"]);

// ---------------------------------------------------------------------------------------------------------------------
// Names and words (backend app/research: health.py, normalize.py STATUSES, intake.py EXPLANATIONS, budget.py).

const AGENT_TITLES: Record<string, string> = {
  supervisor: "AI Supervisor",
  wordpress_security: "WordPress Security & Repair",
  seo_content: "SEO & Content",
  social_marketing: "Social Media & Marketing",
  server_management: "VPS & Server Management",
  communications: "Email & Customer Communications",
  monitoring: "Website & Infrastructure Monitoring",
  credit_repair: "Credit Repair & Dispute Management",
};
const ENGINE_TITLES: Record<string, string> = { duckduckgo: "DuckDuckGo", brave: "Brave", wikipedia: "Wikipedia" };

/** [Pill value, label, Tile tone] per SearXNG state. */
const STATE: Record<ResearchState, [string, string, string]> = {
  off: ["disabled", "Off", "muted"],
  mock: ["mock", "Mock", "cyan"],
  healthy: ["up", "Healthy", "green"],
  degraded: ["degraded", "Degraded", "warn"],
  all_engines_failing: ["down", "All engines failing", "bad"],
  unreachable: ["down", "Unreachable", "bad"],
  pc_offline: ["warning", "PC offline", "warn"],
};
const STATE_TEXT: Record<ResearchState, string> = {
  off: "Web research is switched off: every search is refused.",
  mock: "Mock mode: the answers are made up inside the Command Center and nothing leaves this PC.",
  healthy: "SearXNG answers and its search engines are working.",
  degraded: "SearXNG answers, but some search engines are failing or paused.",
  all_engines_failing: "SearXNG answers, but every search engine is failing or paused.",
  unreachable: "The worker cannot reach SearXNG.",
  pc_offline: "This PC has no internet connection, so searches cannot leave it.",
};

/** [Pill value, label] per search status. */
const STATUS: Record<string, [string, string]> = {
  running: ["running", "Running"],
  ok: ["up", "OK"],
  partial: ["degraded", "Partial"],
  empty: ["info", "No results"],
  provider_down: ["down", "Engines failing"],
  unreachable: ["down", "Unreachable"],
  bad_request: ["failed", "Bad request"],
  provider_misconfigured: ["failed", "Misconfigured"],
  blocked: ["rejected", "Blocked"],
  refused_policy: ["rejected", "Refused"],
  refused_budget: ["warning", "Cap reached"],
  refused_rate: ["warning", "Too fast"],
  failed: ["failed", "Failed"],
};
const ERROR_STATUSES = "provider_down,unreachable,bad_request,provider_misconfigured,failed";
const STATUS_FILTERS: [string, string][] = [
  ["", "Any status"],
  ["ok,partial,empty", "Answered"],
  ["blocked,refused_policy", "Refused by the screen or policy"],
  ["refused_budget,refused_rate", "Refused by a cap or the pace"],
  [ERROR_STATUSES, "Errors"],
  ["running", "Running"],
];

const TRIGGER: Record<string, string> = {
  dashboard: "Run by a person",
  supervisor: "AI Supervisor request",
  retry: "Retry by a person",
  test: "Connectivity test",
  schedule: "Schedule (refused)",
  n8n: "n8n (refused)",
  service: "Service token (refused)",
  unknown: "Unknown (refused)",
};

/** What a reason code means (fixed texts; a refused attempt keeps its code only, never the text). */
export const REASONS: Record<string, string> = {
  research_off: "web research is switched off",
  settings_unreadable: "the research settings cannot be read (an admin saves them again)",
  not_entitled: "this edition does not include web research",
  credit_repair: "Credit Repair never searches the web",
  no_policy: "this agent has no research policy",
  search_disabled: "web search is switched off for this agent",
  policy_unreadable: "this agent's research policy cannot be read",
  policy_error: "the research policy could not be checked",
  not_a_research_capability: "this capability does not search the web",
  empty_query: "the query is empty",
  too_long: "the query is longer than 128 characters (it is never cut)",
  mixed_script: "a word mixes alphabets",
  non_ascii_digits: "it contains digits that are not 0-9",
  search_syntax: "a word starts with !, ?, : or < (search-engine commands)",
  encoded_data: "it contains encoded data or a long random-looking token",
  restricted_data: "it contains restricted data (an SSN, card or bank number, a date of birth)",
  credit_topic: "credit topics are never searched",
  secret: "it contains a password, key or token",
  contact_detail: "it contains an e-mail address or a phone number",
  personal_record: "it names a person or business from this business's own records",
  too_many_blocked: "too many of this person's searches were refused today (as many as their daily search cap, or 5 naming this business's own records)",
  credit_repair_index_incomplete: "the Credit Repair check is not ready",
  matches_private_text: "it repeats private text from the JARVIS archive or memory",
  internal_host: "it names an internal address, host or server",
  file_path: "it contains a file path",
  client_name: "it names a client's business or website",
  owner_name: "it names this business (not allowed for this agent)",
  software_version: "it contains a version number or CVE id (not allowed for this agent)",
  class_not_allowed: "this agent only searches advisories for a software name with its version",
  blocked_domain: "it names a domain this agent's policy blocks",
  screen_error: "the query could not be checked",
  global_cap: "today's searches for everyone are used up",
  person_cap: "this person's searches for today are used up",
  agent_cap: "this agent's searches for today are used up",
  cap_zero: "a cap of 0 refuses every search",
  budget_error: "the daily caps could not be checked",
  person_not_allowed: "only admins and operators start searches",
  task_has_approval: "a search never runs inside an approved task",
  not_supported: "page fetching is not in this build",
  all_engines_cooling: "every search engine is paused",
  timeout: "SearXNG did not answer in time",
  abandoned: "the worker stopped during the search",
};

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function plural(n: number, word: string, many = `${word}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? word : many}`;
}

function words(key: string): string {
  const s = key.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function agentTitle(agent: string): string {
  return AGENT_TITLES[agent] ?? words(agent);
}

function engineTitle(engine: string): string {
  return ENGINE_TITLES[engine] ?? webText(engine, CAPS.engine);
}

function StatusPill({ status }: { status: string }) {
  const [v, l] = STATUS[status] ?? ["unknown", words(status)];
  return <Pill value={v} label={l} />;
}

function StatePill({ state }: { state: ResearchState }) {
  const [v, l] = STATE[state] ?? ["unknown", words(state)];
  return <Pill value={v} label={l} />;
}

function Reason({ code }: { code: string }) {
  if (!code) return <span className="muted">—</span>;
  return (
    <>
      <code>{code}</code>
      {REASONS[code] ? <span className="rc-reason muted small">{REASONS[code]}</span> : null}
    </>
  );
}

/** "Lead #42" or "Ticket #7", linked to its page: the record that caused a block (a poisoned lead shows up here). */
function BlockedBy({ value }: { value: string | null }) {
  const m = value ? /^(lead|ticket):(\d+)$/.exec(value) : null;
  if (!m) return <span className="muted">—</span>;
  return <Link to={m[1] === "lead" ? "/leads" : "/support"}>{`${m[1] === "lead" ? "Lead" : "Ticket"} #${m[2]}`}</Link>;
}

/** An absolute time with its age as the tooltip, or "—". */
function When({ iso }: { iso: string | null | undefined }) {
  if (!iso || Number.isNaN(Date.parse(iso))) return <span className="muted">—</span>;
  return (
    <span className="rc-when">
      <Time iso={iso} />
      <span className="muted small">{utcTime(iso)}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// The local pre-check (plan section 3.16 screen 2): patterns only. It never downloads lead, client or archive data, and
// the server's screen is the one that counts.

const FORMAT = /\p{Cf}/gu;
const EMAIL = /[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}\b/;
/** "jane at example dot com", "jane (at) example [dot] com" (backend canon.spelled_out). */
const AT = /\s*[([{<]\s*(?:at|@)\s*[)\]}>]\s*|\s+at\s+|\s*@\s*/g;
const DOT = /\s*[([{<]\s*(?:dot|\.)\s*[)\]}>]\s*|\s+(?:dot|period)\s+|\s+\.\s*|\s*\.\s+(?=[a-z]{2,24}\b)/g;
/** One written number, with the separators that may sit between its digits ("404.555.0199", "4 0 4 5 5 5 0 1 9 9"). */
const NUMBER = /\d(?:[\s.\-()/+_]*\d)*/g;

/** The query as the server measures it: NFKC, invisible characters removed, spaces collapsed. */
export function canonicalQuery(query: string): string {
  return query.normalize("NFKC").replace(FORMAT, "").replace(/\s+/g, " ").trim();
}

/** Why the server would probably refuse `query`, in words; [] when no pattern matched (which proves nothing). */
export function precheck(query: string): string[] {
  const text = canonicalQuery(query);
  const lower = text.toLowerCase();
  const found: string[] = [];
  if (text.length > MAX_QUERY) found.push("it is longer than 128 characters (a query is never cut)");
  if (EMAIL.test(lower) || EMAIL.test(lower.replace(AT, "@").replace(DOT, "."))) found.push("it contains an e-mail address");
  const runs = (lower.match(NUMBER) ?? []).map((n) => n.replace(/\D/g, ""));
  if (runs.some((r) => r.length >= 10 && r.length <= 15)) found.push("it contains a phone number");
  if (runs.some((r) => r.length === 9 || r.length > 15)) found.push("it contains a long number (shaped like an ID, card or account number)");
  if (text.split(" ").some((w) => /^[!?:<]/.test(w))) found.push("a word starts with !, ?, : or < (search-engine commands)");
  return found;
}

// ---------------------------------------------------------------------------------------------------------------------
// One search and its citations.

/** Rows of `unresponsive`: [{engine, reason}] from the API, shown as "Brave: captcha". */
function unresponsiveText(rows: unknown[]): string {
  return rows
    .flatMap((r) => {
      if (!r || typeof r !== "object") return [];
      const { engine, reason } = r as { engine?: unknown; reason?: unknown };
      return typeof engine === "string" ? [`${engineTitle(engine)}${typeof reason === "string" && reason ? `: ${webText(reason, CAPS.engine)}` : ""}`] : [];
    })
    .join(" · ");
}

function optionsText(options: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof options.count === "number") parts.push(`${options.count} results`);
  if (typeof options.language === "string") parts.push(options.language);
  if (typeof options.time_range === "string" && options.time_range) parts.push(`past ${options.time_range}`);
  return parts.join(" · ");
}

function QueryFacts({ q }: { q: ResearchQuery }) {
  const refused = q.status === "blocked" || q.status === "refused_policy";
  const unresponsive = unresponsiveText(q.unresponsive ?? []);
  return (
    <dl className="kv rc-kv">
      <dt>When</dt>
      <dd>
        <When iso={q.created_at} />
      </dd>
      <dt>Agent</dt>
      <dd>
        {agentTitle(q.agent)} {q.capability ? <code className="small">{q.capability}</code> : null}
      </dd>
      <dt>Asked by</dt>
      <dd>
        {q.requested_by || "—"} <span className="muted small">· {TRIGGER[q.trigger_source] ?? words(q.trigger_source)}</span>
      </dd>
      <dt>Query</dt>
      <dd>{q.query_text ? <span className="rc-query">{q.query_text}</span> : <span className="muted">{refused ? "Not kept: a refused attempt keeps its reason code only" : "—"}</span>}</dd>
      {optionsText(q.options ?? {}) ? (
        <>
          <dt>Options</dt>
          <dd>{optionsText(q.options)}</dd>
        </>
      ) : null}
      <dt>Status</dt>
      <dd>
        <StatusPill status={q.status} /> {q.reason_code ? <Reason code={q.reason_code} /> : null}
        {q.http_status ? <span className="muted small"> · HTTP {q.http_status}</span> : null}
      </dd>
      {q.blocked_by_ref ? (
        <>
          <dt>Caused by</dt>
          <dd>
            <BlockedBy value={q.blocked_by_ref} />
          </dd>
        </>
      ) : null}
      <dt>Provider</dt>
      <dd>
        {q.provider || "—"}
        {q.provider_version ? <span className="muted small"> {q.provider_version}</span> : null}
        <span className="muted small"> · {q.mode} mode</span>
      </dd>
      {q.engines?.length ? (
        <>
          <dt>Engines asked</dt>
          <dd>{q.engines.map(engineTitle).join(", ")}</dd>
        </>
      ) : null}
      {unresponsive ? (
        <>
          <dt>Did not answer</dt>
          <dd>{unresponsive}</dd>
        </>
      ) : null}
      <dt>Results</dt>
      <dd>
        {q.result_count} {q.cached ? <Pill value="info" label="from the cache" /> : null}
        {q.latency_ms != null ? <span className="muted small"> · {q.latency_ms} ms</span> : null}
      </dd>
      {q.task_id || q.request_id ? (
        <>
          <dt>From</dt>
          <dd>
            {q.task_id ? <Link to={`/tasks?id=${q.task_id}`}>task #{q.task_id}</Link> : null}
            {q.task_id && q.request_id ? " · " : null}
            {q.request_id ? <Link to={`/supervisor?id=${q.request_id}`}>request #{q.request_id}</Link> : null}
          </dd>
        </>
      ) : null}
    </dl>
  );
}

export function Unverified() {
  return (
    <div className="alert alert-warn rc-unverified" role="note">
      <strong>Unverified web content.</strong> These pages come from public search engines. Nobody has checked them, no AI model read them, and nothing in
      them can approve, run or change anything.
    </div>
  );
}

/** A search with its numbered citations (GET /api/research/queries/{id}). */
function QueryResult({ id, onOpenSource }: { id: number; onOpenSource?: (id: number) => void }) {
  const detail = usePoll(() => api.get<{ query: ResearchQuery; citations: unknown[] }>(`${BASE}/queries/${id}`), 0, [id]);
  if (detail.error) return <ErrorNote error={`Could not load search #${id}: ${detail.error}`} />;
  if (!detail.data) return <p className="muted">Loading…</p>;
  const citations = toCitations(detail.data.citations);
  return (
    <>
      <QueryFacts q={detail.data.query} />
      {citations.length ? (
        <>
          <Unverified />
          <CitationList citations={citations} onOpenSource={onOpenSource} />
        </>
      ) : null}
    </>
  );
}

function QueryModal({ id, onClose, onOpenSource }: { id: number; onClose: () => void; onOpenSource: (id: number) => void }) {
  return (
    <Modal title={`Search #${id}`} onClose={onClose} wide>
      <QueryResult id={id} onOpenSource={onOpenSource} />
      <p className="muted small">{DEEPSEEK_NOTE}. Promoting a page to the JARVIS Brain is not in this build.</p>
    </Modal>
  );
}

function SourceModal({ id, onClose, onOpenQuery }: { id: number; onClose: () => void; onOpenQuery: (id: number) => void }) {
  const detail = usePoll(
    () => api.get<{ source: ResearchSource; times_cited: number; cited_by: { query_id: number; agent: string; created_at: string | null; rank: number }[] }>(`${BASE}/sources/${id}`),
    0,
    [id],
  );
  const d = detail.data;
  const href = d ? safeHref(d.source.url) : null;
  return (
    <Modal title={`Page #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error ? `Could not load page #${id}: ${detail.error}` : null} />
      {!d ? (
        detail.error ? null : <p className="muted">Loading…</p>
      ) : (
        <>
          <Unverified />
          <dl className="kv rc-kv">
            <dt>Title</dt>
            <dd className="rc-web">{webText(d.source.title, CAPS.title) || "(no title)"}</dd>
            <dt>Address</dt>
            <dd className="rc-url">
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer nofollow">
                  {webUrl(d.source.url)}
                </a>
              ) : (
                <span className="muted">Address not linked: it is not an http(s) address</span>
              )}
            </dd>
            <dt>Domain</dt>
            <dd>{webText(d.source.domain, CAPS.domain) || "—"}</dd>
            {d.source.site_name ? (
              <>
                <dt>Site</dt>
                <dd>{webText(d.source.site_name, CAPS.site)}</dd>
              </>
            ) : null}
            <dt>Published</dt>
            <dd>{utcDate(d.source.published_at) || <span className="muted">no date given</span>}</dd>
            <dt>First seen</dt>
            <dd>{utcTime(d.source.first_retrieved_at)}</dd>
            <dt>Last seen</dt>
            <dd>{utcTime(d.source.last_retrieved_at)}</dd>
            <dt>Kept until</dt>
            <dd>{utcTime(d.source.expires_at) || <span className="muted">—</span>}</dd>
            <dt>Cited</dt>
            <dd>{plural(d.times_cited, "time")}</dd>
          </dl>
          {d.cited_by.length ? (
            <>
              <h3>Cited by</h3>
              <ul className="list small">
                {d.cited_by.map((c) => (
                  <li key={`${c.query_id}-${c.rank}`}>
                    <button type="button" className="link-button rc-link" onClick={() => onOpenQuery(c.query_id)}>
                      Search #{c.query_id}
                    </button>
                    <span className="muted">
                      result [{c.rank}] · {agentTitle(c.agent)} · {utcTime(c.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          <p className="muted small">Page text is not fetched and promoting a page to the JARVIS Brain is not in this build.</p>
        </>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Overview (screen 1).

function SearxngLine({ health }: { health: ResearchHealth }) {
  const s = health.searxng;
  const paused = Object.entries(s.engines ?? {}).filter(([, e]) => e.state === "suspended");
  return (
    <div className="rc-provider">
      <div className="rc-provider-head">
        <strong>SearXNG</strong> <StatePill state={s.state} />
      </div>
      <p className="muted small">{STATE_TEXT[s.state] ?? ""}</p>
      {s.message ? <p className="text-warn small">{s.message}</p> : null}
      {health.mode === "live" ? (
        <div className="rc-engines">
          {Object.entries(s.engines ?? {}).map(([name, e]) => (
            <span key={name} className={`rc-engine rc-engine-${e.state === "ok" ? "ok" : e.state === "suspended" ? "paused" : e.state === "failing" ? "failing" : "unknown"}`}>
              {engineTitle(name)}: {e.state === "suspended" ? `paused${e.until ? ` until ${utcTime(e.until)}` : ""}` : words(e.state)}
            </span>
          ))}
        </div>
      ) : null}
      {paused.length && health.mode === "live" ? <p className="muted small">The gateway leaves paused engines out of its searches until their time is up.</p> : null}
    </div>
  );
}

function Overview({ health, healthError, openTab, openQuery }: { health: ResearchHealth | null; healthError: string | null; openTab: (t: TabKey) => void; openQuery: (id: number) => void }) {
  const usage = usePoll(() => api.get<ResearchUsage>(`${BASE}/usage`), 30_000);
  const errors = usePoll(() => api.get<{ queries: ResearchQuery[]; total: number }>(`${BASE}/queries${qs({ status: ERROR_STATUSES, limit: 5 })}`), 60_000);
  const u = usage.data;
  const today = u?.days.filter((d) => d.day === u.today.day) ?? [];
  const blockedToday = today.reduce((n, d) => n + d.blocked, 0);
  const state = health?.searxng.state;
  const loading = (failed: boolean) => (failed ? "Could not load" : "Loading…");
  const of = (used: number, cap: number) => (
    <>
      <span className="tile-value tabular">{used}</span>
      <span className="tile-of">of {cap}</span>
    </>
  );
  return (
    <>
      <ErrorNote error={healthError ? `Could not load the research status: ${healthError}` : usage.error ? `Could not load today's usage: ${usage.error}` : null} />
      <div className="tiles cols-4">
        <Tile icon="globe" tone={state ? STATE[state]?.[2] ?? "muted" : "muted"} label="Web research" hint={state ? STATE_TEXT[state] : loading(!!healthError)}>
          <span className="rc-tile-state">{state ? <StatePill state={state} /> : "—"}</span>
          {health ? <span className="tile-of">{health.mode} mode</span> : null}
        </Tile>
        <Tile icon="search" tone="blue" label="Searches today" hint={u ? `Everyone, ${u.provider} · resets at 00:00 UTC` : loading(!!usage.error)}>
          {u ? of(u.today.global.used, u.today.global.cap) : <span className="tile-value">—</span>}
        </Tile>
        <Tile icon="user" tone="purple" label="Your searches today" hint={u ? "Supervisor steps count for the person who typed the request" : loading(!!usage.error)}>
          {u ? of(u.today.person.used, u.today.person.cap) : <span className="tile-value">—</span>}
        </Tile>
        <Tile icon="shield" tone={blockedToday ? "orange" : "green"} label="Blocked today" hint={u ? "Refused before anything left this PC" : loading(!!usage.error)}>
          <span className="tile-value tabular">{u ? blockedToday : "—"}</span>
        </Tile>
      </div>
      <div className="grid-2 rc-grid">
        <Card
          title={
            <>
              <Icon name="radar" size={18} className="card-icon" /> Providers
            </>
          }
          actions={
            <button type="button" className="btn outline small" onClick={() => openTab("status")}>
              Details
            </button>
          }
        >
          {health ? <SearxngLine health={health} /> : <p className="muted">{loading(!!healthError)}</p>}
          <div className="rc-provider">
            <div className="rc-provider-head">
              <strong>Page fetch</strong> <Pill value="disabled" label="not in this build" />
            </div>
            <p className="muted small">{FETCH_NOTE}.</p>
          </div>
          <div className="rc-provider">
            <div className="rc-provider-head">
              <strong>DeepSeek</strong> <Pill value="disabled" label="not in this build" />
            </div>
            <p className="muted small">{DEEPSEEK_NOTE}.</p>
          </div>
        </Card>
        <Card
          title={
            <>
              <Icon name="agents" size={18} className="card-icon" /> Today per agent
            </>
          }
          actions={
            <button type="button" className="btn outline small" onClick={() => openTab("usage")}>
              Usage
            </button>
          }
        >
          {!u ? (
            <p className="muted">{loading(!!usage.error)}</p>
          ) : (
            <ul className="list rc-agents">
              {u.today.agents.map((a) => (
                <li key={a.agent}>
                  <span className="rc-agent-name">{agentTitle(a.agent)}</span>
                  {a.search_enabled ? (
                    <span className="tabular">
                      {a.used} <span className="muted">of {a.cap}</span>
                    </span>
                  ) : (
                    <Pill value="disabled" label={a.agent === "credit_repair" ? "never" : "off"} />
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      <Card
        title={
          <>
            <Icon name="alert" size={18} className="card-icon" /> Recent errors
          </>
        }
      >
        <ErrorNote error={errors.error ? `Could not load recent errors: ${errors.error}` : null} />
        {!errors.data ? (
          errors.error ? null : <p className="muted">Loading…</p>
        ) : !errors.data.queries.length ? (
          <Empty>No errors.</Empty>
        ) : (
          <ul className="list small">
            {errors.data.queries.map((q) => (
              <li key={q.id}>
                <button type="button" className="link-button rc-link" onClick={() => openQuery(q.id)}>
                  Search #{q.id}
                </button>
                <StatusPill status={q.status} />
                <code>{q.reason_code || q.status}</code>
                <span className="muted">
                  {agentTitle(q.agent)} · <Time iso={q.created_at} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// New research (screen 2).

interface Choice {
  key: string;
  agent: Agent;
  cap: Capability;
  /** null while the policy is not known yet. */
  allowed: boolean | null;
  why: string;
}

const LANGUAGES: [string, string][] = [
  ["en-US", "English (US)"],
  ["en-GB", "English (UK)"],
  ["es-ES", "Spanish"],
  ["fr-FR", "French"],
  ["de-DE", "German"],
  ["all", "Any language"],
];
const TIME_RANGES: [string, string][] = [
  ["", "Any time"],
  ["day", "Past day"],
  ["week", "Past week"],
  ["month", "Past month"],
  ["year", "Past year"],
];
const ACTIVE = ["queued", "running"];

function defaultRange(cap: Capability | undefined): string {
  const d = cap?.params_schema.properties?.time_range?.default;
  return typeof d === "string" ? d : "";
}

/** The task a search was queued as, until it finishes; then its citations. */
function SearchProgress({ id, openQuery }: { id: number; openQuery: (id: number) => void }) {
  const [done, setDone] = useState(false);
  const detail = usePoll(() => api.get<{ task: Task }>(`/api/tasks/${id}`), done ? 0 : 2_000, [id, done]);
  const task = detail.data?.task;
  useEffect(() => {
    if (task && !ACTIVE.includes(task.status)) setDone(true);
  }, [task]);
  const research = (task?.result?.data as { research?: { query_id?: unknown } } | undefined)?.research;
  const queryId = typeof research?.query_id === "number" ? research.query_id : null;
  return (
    <div className="rc-progress" aria-live="polite">
      <p>
        Task <Link to={`/tasks?id=${id}`}>#{id}</Link> {task ? <Pill value={task.status} /> : null}
        {task && ACTIVE.includes(task.status) ? <span className="muted small"> The worker runs it in a moment…</span> : null}
      </p>
      <ErrorNote error={detail.error} />
      {task?.summary && !ACTIVE.includes(task.status) ? <p>{task.summary}</p> : null}
      {task?.error ? <p className="text-bad">{task.error}</p> : null}
      {queryId !== null ? (
        <>
          <QueryResult id={queryId} />
          <button type="button" className="btn small" onClick={() => openQuery(queryId)}>
            Open search #{queryId}
          </button>
        </>
      ) : null}
    </div>
  );
}

function NewResearch({ health, openQuery }: { health: ResearchHealth | null; openQuery: (id: number) => void }) {
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 0);
  const usage = usePoll(() => api.get<ResearchUsage>(`${BASE}/usage`), 30_000);
  const [picked, setPicked] = useState("");
  const [query, setQuery] = useState("");
  const [software, setSoftware] = useState("");
  const [version, setVersion] = useState("");
  const [count, setCount] = useState(10);
  const [language, setLanguage] = useState("en-US");
  const [range, setRange] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<number | null>(null);

  const policy = new Map((usage.data?.today.agents ?? []).map((a) => [a.agent, a]));
  const choices: Choice[] = (agents.data?.agents ?? []).flatMap((agent) =>
    agent.capabilities
      .filter((cap) => cap.research && !INTERNAL.has(`${agent.name}.${cap.name}`))
      .map((cap) => {
        const p = policy.get(agent.name);
        const why = !agent.enabled ? "agent switched off" : agent.entitled === false ? "not in your edition" : usage.data && !p?.search_enabled ? "off in the research policy" : "";
        return { key: `${agent.name}.${cap.name}`, agent, cap, allowed: usage.data ? !why : why ? false : null, why };
      }),
  );
  const choice = choices.find((c) => c.key === picked) ?? choices.find((c) => c.allowed !== false) ?? null;
  const advisory = choice?.cap.name === "check_advisories";
  const timeRange = range ?? defaultRange(choice?.cap);
  const sent = advisory ? `${software} ${version} security advisory` : query;
  const length = canonicalQuery(sent).length;
  const typed = advisory ? software.trim() !== "" || version.trim() !== "" : query.trim() !== "";
  const warnings = typed ? precheck(sent) : [];
  const mode = health?.mode;
  const off = mode === "off" || health?.entitled === false;
  const ready = !!choice && choice.allowed !== false && !off && (advisory ? !!software.trim() && !!version.trim() : !!query.trim());

  function pick(key: string) {
    setPicked(key);
    setRange(null);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!choice || !ready) return;
    setBusy(true);
    setError(null);
    setTaskId(null);
    const params: Record<string, unknown> = { count, language, time_range: timeRange || null };
    if (advisory) Object.assign(params, { software, version });
    else params.query = query;
    try {
      const r = await api.post<{ task: Task }>(`/api/agents/${choice.agent.name}/tasks`, { capability: choice.cap.name, params });
      setTaskId(r.task.id);
      void usage.refresh();
    } catch (err) {
      // A refusal is answered with a fixed explanation and the reason; the query itself is never echoed back.
      setError(err instanceof ApiError && err.status === 400 ? `Refused: ${err.message}` : errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid-2 rc-grid rc-new">
      <Card
        title={
          <>
            <Icon name="search" size={18} className="card-icon" /> New research
          </>
        }
      >
        <ErrorNote error={agents.error ? `Could not load the agents: ${agents.error}` : null} />
        {off ? (
          <div className="alert alert-warn" role="status">
            {health?.entitled === false ? "This edition does not include web research." : "Web research is switched off."} An admin switches it on in Policy and
            settings.
          </div>
        ) : mode === "mock" ? (
          <div className="alert alert-info" role="status">
            Mock mode: the answers are made up inside the Command Center and nothing leaves this PC.
          </div>
        ) : null}
        <form className="rc-form" onSubmit={submit}>
          <label>
            Agent and capability
            <select value={choice?.key ?? ""} onChange={(e) => pick(e.target.value)} disabled={!choices.length}>
              {!choices.length ? <option value="">{agents.data ? "No agent searches the web in this build" : "Loading…"}</option> : null}
              {choices.map((c) => (
                <option key={c.key} value={c.key} disabled={c.allowed === false}>
                  {c.agent.title}: {c.cap.name}
                  {c.why ? ` (${c.why})` : ""}
                </option>
              ))}
            </select>
            {choice ? <span className="muted small">{choice.cap.description}</span> : null}
          </label>
          {advisory ? (
            <div className="rc-row">
              <label>
                Software
                <input value={software} onChange={(e) => setSoftware(e.target.value)} maxLength={60} placeholder="e.g. WordPress, OpenSSH" autoComplete="off" />
              </label>
              <label>
                Version
                <input value={version} onChange={(e) => setVersion(e.target.value)} maxLength={30} placeholder="e.g. 6.9 or 9.6p1" autoComplete="off" />
              </label>
              <span className="muted small rc-wide">Type a public software name and version yourself: never one copied from a scan, a site or a server.</span>
            </div>
          ) : (
            <label>
              Search for
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                maxLength={256}
                placeholder="In your own words: never names, contact details or anything private"
                autoComplete="off"
                spellCheck
                aria-describedby="rc-counter"
              />
            </label>
          )}
          <div className={`rc-counter small${length > MAX_QUERY ? " text-bad" : " muted"}`} id="rc-counter" aria-live="polite">
            {length} / {MAX_QUERY} characters{advisory && typed ? ` · sent as: ${canonicalQuery(sent)}` : ""}
          </div>
          {warnings.length ? (
            <div className="alert alert-warn rc-precheck" role="status">
              This would be blocked: {warnings.join("; ")}. The server's check is the one that counts.
            </div>
          ) : null}
          <div className="rc-row rc-options">
            <label>
              Results
              <select value={count} onChange={(e) => setCount(Number(e.target.value))}>
                {[5, 10, 20].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Language
              <select value={language} onChange={(e) => setLanguage(e.target.value)}>
                {LANGUAGES.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Time range
              <select value={timeRange} onChange={(e) => setRange(e.target.value)}>
                {TIME_RANGES.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="rc-notice small">
            <Icon name="globe" size={15} /> {SENT_NOTICE}
            {mode === "mock" ? " (in mock mode it is not sent anywhere)" : ""}.
          </p>
          <p className="muted small">{DEEPSEEK_NOTE}.</p>
          <ErrorNote error={error} />
          <button className="btn primary" type="submit" disabled={busy || !ready}>
            <Icon name="search" size={16} /> {busy ? "Sending…" : "Search"}
          </button>
        </form>
      </Card>
      <Card
        title={
          <>
            <Icon name="docLines" size={18} className="card-icon" /> Result
          </>
        }
      >
        {taskId ? (
          <SearchProgress key={taskId} id={taskId} openQuery={openQuery} />
        ) : (
          <div className="muted small rc-how">
            <p>Each search is checked before anything is stored or sent. A query is refused, with the reason, when it holds:</p>
            <ul className="rc-list">
              <li>e-mail addresses, phone numbers, or ID, card or bank numbers;</li>
              <li>passwords, keys or tokens;</li>
              <li>names of leads, support customers, Credit Repair clients or (for most agents) clients' businesses;</li>
              <li>internal addresses, server names or file paths;</li>
              <li>credit topics, or text copied from the JARVIS archive or memory.</li>
            </ul>
            <p>The results come back as numbered, unverified citations. Nothing is summarised by an AI model, and nothing a page says can start an action.</p>
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Search history (screen 3).

const PAGE = 50;

function Pager({ offset, shown, total, page, onOffset }: { offset: number; shown: number; total: number; page: number; onOffset: (n: number) => void }) {
  if (total <= page) return null;
  return (
    <div className="rc-pager">
      <span className="muted small">
        {(offset + 1).toLocaleString()}–{(offset + shown).toLocaleString()} of {total.toLocaleString()}
      </span>
      <button className="btn small" type="button" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - page))}>
        Previous
      </button>
      <button className="btn small" type="button" disabled={offset + shown >= total} onClick={() => onOffset(offset + page)}>
        Next
      </button>
    </div>
  );
}

function AgentFilter({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <select aria-label="Agent" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Any agent</option>
      {Object.entries(AGENT_TITLES)
        .filter(([k]) => k !== "credit_repair")
        .map(([k, t]) => (
          <option key={k} value={k}>
            {t}
          </option>
        ))}
    </select>
  );
}

function History({ openQuery }: { openQuery: (id: number) => void }) {
  const [agent, setAgent] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const key = JSON.stringify({ agent, status, offset });
  const list = usePoll(
    () => api.get<{ queries: ResearchQuery[]; total: number }>(`${BASE}/queries${qs({ agent, status, limit: PAGE, offset })}`).then((r) => ({ key, r })),
    15_000,
    [key],
  );
  const current = list.data && list.data.key === key ? list.data.r : null;
  return (
    <Card
      title={
        <>
          <Icon name="history" size={18} className="card-icon" /> Search history
        </>
      }
      actions={current ? <span className="small">{plural(current.total, "search", "searches")}</span> : null}
    >
      <div className="rc-filters">
        <AgentFilter
          value={agent}
          onChange={(v) => {
            setAgent(v);
            setOffset(0);
          }}
        />
        <select
          aria-label="Status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setOffset(0);
          }}
        >
          {STATUS_FILTERS.map(([v, l]) => (
            <option key={l} value={v}>
              {l}
            </option>
          ))}
        </select>
      </div>
      <p className="muted small">Every attempt is listed, refused ones too. A refused attempt keeps its reason code only, never its text.</p>
      <ErrorNote error={list.error ? `Could not load the history: ${list.error}` : null} />
      {!current ? (
        list.error ? null : <Empty>Loading…</Empty>
      ) : !current.queries.length ? (
        <Empty>{agent || status ? "No searches match these filters." : "No searches yet."}</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table rc-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Search</th>
                <th scope="col">Agent</th>
                <th scope="col">Person</th>
                <th scope="col">Provider</th>
                <th scope="col">Status</th>
                <th scope="col">Results</th>
              </tr>
            </thead>
            <tbody>
              {current.queries.map((q) => (
                <tr key={q.id}>
                  <td className="rc-c-when" data-label="When">
                    <When iso={q.created_at} />
                  </td>
                  <td className="rc-c-main">
                    <button type="button" className="link-button rc-row-title" onClick={() => openQuery(q.id)}>
                      #{q.id} {q.query_text ? <span className="rc-query">{q.query_text}</span> : <span className="muted">(no text kept)</span>}
                    </button>
                  </td>
                  <td data-label="Agent">
                    {agentTitle(q.agent)}
                    <div className="muted small">{q.capability}</div>
                  </td>
                  <td data-label="Person">
                    {q.requested_by || "—"}
                    <div className="muted small">{TRIGGER[q.trigger_source] ?? words(q.trigger_source)}</div>
                  </td>
                  <td data-label="Provider">
                    {q.provider || "—"}
                    {q.engines?.length ? <div className="muted small">{q.engines.map(engineTitle).join(", ")}</div> : null}
                  </td>
                  <td data-label="Status">
                    <StatusPill status={q.status} />
                    {q.reason_code ? <div className="small">{<Reason code={q.reason_code} />}</div> : null}
                  </td>
                  <td className="tabular" data-label="Results">
                    {q.result_count}
                    {q.cached ? <div className="muted small">cached</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {current ? <Pager offset={offset} shown={current.queries.length} total={current.total} page={PAGE} onOffset={setOffset} /> : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Sources (screen 5).

function Sources({ openSource }: { openSource: (id: number) => void }) {
  const [form, setForm] = useState("");
  const [domain, setDomain] = useState("");
  const [offset, setOffset] = useState(0);
  const key = JSON.stringify({ domain, offset });
  const list = usePoll(
    () => api.get<{ sources: ResearchSource[]; total: number }>(`${BASE}/sources${qs({ domain, limit: PAGE, offset })}`).then((r) => ({ key, r })),
    0,
    [key],
  );
  const current = list.data && list.data.key === key ? list.data.r : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
    setDomain(form.trim());
  }

  return (
    <Card
      title={
        <>
          <Icon name="globe" size={18} className="card-icon" /> Sources
        </>
      }
      actions={current ? <span className="small">{plural(current.total, "page")}</span> : null}
    >
      <form className="rc-filters" onSubmit={submit}>
        <label className="rc-search-q">
          <span className="sr-only">Domain</span>
          <Icon name="search" size={16} className="rc-search-icon" />
          <input type="search" value={form} onChange={(e) => setForm(e.target.value)} placeholder="Domain, e.g. wordpress.org" />
        </label>
        <button className="btn primary" type="submit">
          Apply
        </button>
      </form>
      <p className="muted small">
        Every distinct page the searches returned, kept for the retention period after it was last seen. Unverified: titles and addresses are shown as plain
        text. Promoting a page to the JARVIS Brain is not in this build.
      </p>
      <ErrorNote error={list.error ? `Could not load the sources: ${list.error}` : null} />
      {!current ? (
        list.error ? null : <Empty>Loading…</Empty>
      ) : !current.sources.length ? (
        <Empty>{domain ? "No pages from this domain." : "No pages yet."}</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table rc-table">
            <thead>
              <tr>
                <th scope="col">Page</th>
                <th scope="col">First seen</th>
                <th scope="col">Last seen</th>
                <th scope="col">Cited</th>
              </tr>
            </thead>
            <tbody>
              {current.sources.map((s) => (
                <tr key={s.id}>
                  <td className="rc-c-main">
                    <button type="button" className="link-button rc-row-title" onClick={() => openSource(s.id)}>
                      {webText(s.title, CAPS.title) || "(no title)"}
                    </button>
                    <div className="muted small rc-url">
                      {webText(s.domain, CAPS.domain)} · {webUrl(s.url)}
                    </div>
                  </td>
                  <td data-label="First seen" className="small">
                    {utcTime(s.first_retrieved_at)}
                  </td>
                  <td data-label="Last seen" className="small">
                    {utcTime(s.last_retrieved_at)}
                  </td>
                  <td className="tabular" data-label="Cited">
                    {s.times_cited ?? 0}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {current ? <Pager offset={offset} shown={current.sources.length} total={current.total} page={PAGE} onOffset={setOffset} /> : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Usage (screen 7, without costs: SearXNG costs nothing and DeepSeek is not in this build).

function isoDay(offsetDays: number): string {
  return new Date(Date.now() - offsetDays * 86_400_000).toISOString().slice(0, 10);
}

function Usage() {
  const [form, setForm] = useState({ from: isoDay(29), to: isoDay(0) });
  const [applied, setApplied] = useState(form);
  const usage = usePoll(() => api.get<ResearchUsage>(`${BASE}/usage${qs(applied)}`), 60_000, [applied.from, applied.to]);
  const u = usage.data;

  function submit(e: FormEvent) {
    e.preventDefault();
    setApplied({ ...form });
  }

  return (
    <>
      <Card
        title={
          <>
            <Icon name="pulse" size={18} className="card-icon" /> Today against the caps
          </>
        }
      >
        <ErrorNote error={usage.error ? `Could not load the usage: ${usage.error}` : null} />
        {!u ? (
          usage.error ? null : <p className="muted">Loading…</p>
        ) : (
          <>
            <dl className="kv rc-kv">
              <dt>Everyone</dt>
              <dd>
                <Meter used={u.today.global.used} cap={u.today.global.cap} />
              </dd>
              <dt>You ({u.today.person.who})</dt>
              <dd>
                <Meter used={u.today.person.used} cap={u.today.person.cap} />
              </dd>
              {u.today.agents.map((a) => (
                <Fragment key={a.agent}>
                  <dt>{agentTitle(a.agent)}</dt>
                  <dd>{a.search_enabled ? <Meter used={a.used} cap={a.cap} /> : <Pill value="disabled" label={a.agent === "credit_repair" ? "never" : "off"} />}</dd>
                </Fragment>
              ))}
            </dl>
            <p className="muted small">
              Counted per UTC day from {u.today.day}. The agents' caps add up to more than the overall cap: the overall cap is reached first. A cap of 0 refuses
              every search. No costs: SearXNG is free, and {DEEPSEEK_NOTE.charAt(0).toLowerCase() + DEEPSEEK_NOTE.slice(1)}.
            </p>
          </>
        )}
      </Card>
      <Card
        title={
          <>
            <Icon name="calendar" size={18} className="card-icon" /> By day, agent and provider
          </>
        }
      >
        <form className="rc-filters" onSubmit={submit}>
          <label className="rc-date">
            From
            <input type="date" value={form.from} onChange={(e) => setForm({ ...form, from: e.target.value })} />
          </label>
          <label className="rc-date">
            To
            <input type="date" value={form.to} onChange={(e) => setForm({ ...form, to: e.target.value })} />
          </label>
          <button className="btn primary" type="submit">
            Apply
          </button>
        </form>
        {!u ? null : !u.days.length ? (
          <Empty>No searches in this period.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table rc-table">
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Agent</th>
                  <th scope="col">Provider</th>
                  <th scope="col">Sent</th>
                  <th scope="col">From the cache</th>
                  <th scope="col">Blocked</th>
                  <th scope="col">Failed</th>
                </tr>
              </thead>
              <tbody>
                {u.days.map((d) => (
                  <tr key={`${d.day}-${d.agent}-${d.provider}-${d.kind}`}>
                    <td className="rc-c-main">{d.day}</td>
                    <td data-label="Agent">{agentTitle(d.agent)}</td>
                    <td data-label="Provider">{d.provider}</td>
                    <td className="tabular" data-label="Sent">
                      {d.sent}
                    </td>
                    <td className="tabular" data-label="From the cache">
                      {d.cache_hits}
                    </td>
                    <td className="tabular" data-label="Blocked">
                      {d.blocked}
                    </td>
                    <td className="tabular" data-label="Failed">
                      {d.failed}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

function Meter({ used, cap }: { used: number; cap: number }) {
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 100;
  const tone = cap === 0 || used >= cap ? "bad" : pct >= 80 ? "warn" : "good";
  return (
    <span className="rc-meter-line">
      <span className="tabular">
        {used} of {cap}
      </span>
      <span className="rc-meter" role="meter" aria-valuemin={0} aria-valuemax={cap} aria-valuenow={used} aria-label={`${used} of ${cap}`}>
        <span className={`rc-meter-fill rc-meter-${tone}`} style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Provider status and errors (screen 8).

function ProviderStatus({ health, healthError, openQuery }: { health: ResearchHealth | null; healthError: string | null; openQuery: (id: number) => void }) {
  const errors = usePoll(() => api.get<{ queries: ResearchQuery[]; total: number }>(`${BASE}/queries${qs({ status: ERROR_STATUSES, limit: 25 })}`), 60_000);
  const s = health?.searxng;
  return (
    <>
      <ErrorNote error={healthError ? `Could not load the research status: ${healthError}` : null} />
      <Card
        title={
          <>
            <Icon name="radar" size={18} className="card-icon" /> SearXNG
          </>
        }
        actions={s ? <StatePill state={s.state} /> : null}
      >
        {!health || !s ? (
          <p className="muted">{healthError ? "Could not load" : "Loading…"}</p>
        ) : (
          <>
            <p>{STATE_TEXT[s.state]}</p>
            {s.message ? <div className="alert alert-warn">{s.message}</div> : null}
            {s.version && s.version_ok === false ? (
              <div className="alert alert-warn" role="status">
                Version drift: SearXNG reports {webText(s.version, 60)}, but this build pins {s.pinned}. Install the Command Center again from its folder so the
                pinned image is used.
              </div>
            ) : null}
            {!health.settings_readable ? <div className="alert alert-warn">The research settings cannot be read, so every search is refused.</div> : null}
            <dl className="kv rc-kv">
              <dt>Mode</dt>
              <dd>{health.mode}</dd>
              <dt>This PC's internet</dt>
              <dd>{health.internet}</dd>
              <dt>Last probe</dt>
              <dd>
                <When iso={s.probed_at} />
              </dd>
              <dt>Last answered</dt>
              <dd>
                <When iso={s.last_ok_at} />
              </dd>
              <dt>Last error</dt>
              <dd>
                {s.last_error ? <code>{s.last_error}</code> : <span className="muted">none</span>} {s.last_error_at ? <When iso={s.last_error_at} /> : null}
              </dd>
              <dt>Failures in a row</dt>
              <dd className="tabular">{s.consecutive_failures}</dd>
              <dt>Version</dt>
              <dd>
                {s.version ? webText(s.version, 60) : <span className="muted">not read yet</span>} <span className="muted small">· pinned {s.pinned}</span>
              </dd>
              <dt>Engines switched on</dt>
              <dd>{s.enabled_engines?.length ? s.enabled_engines.map(engineTitle).join(", ") : <span className="muted">not read yet</span>}</dd>
            </dl>
            <div className="table-wrap">
              <table className="table rc-table">
                <thead>
                  <tr>
                    <th scope="col">Engine</th>
                    <th scope="col">State</th>
                    <th scope="col">Reason</th>
                    <th scope="col">Paused until</th>
                    <th scope="col">Failures</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(s.engines ?? {}).map(([name, e]) => (
                    <tr key={name}>
                      <td className="rc-c-main">{engineTitle(name)}</td>
                      <td data-label="State">
                        <Pill value={e.state === "ok" ? "up" : e.state === "suspended" ? "warning" : e.state === "failing" ? "down" : "unknown"} label={words(e.state)} />
                      </td>
                      <td data-label="Reason">{e.reason ? <code>{webText(e.reason, CAPS.engine)}</code> : <span className="muted">—</span>}</td>
                      <td data-label="Paused until">{e.until ? utcTime(e.until) : <span className="muted">—</span>}</td>
                      <td className="tabular" data-label="Failures">
                        {e.failures}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted small">
              The worker probes SearXNG every 60 seconds while research is live, and reads its version and engines every hour. A paused engine is left out of
              searches until its time is up; a block by Cloudflare or reCAPTCHA can last 1 to 15 days.
            </p>
          </>
        )}
      </Card>
      <Card
        title={
          <>
            <Icon name="alert" size={18} className="card-icon" /> Errors
          </>
        }
      >
        <ErrorNote error={errors.error ? `Could not load the errors: ${errors.error}` : null} />
        {!errors.data ? (
          errors.error ? null : <p className="muted">Loading…</p>
        ) : !errors.data.queries.length ? (
          <Empty>No errors.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table rc-table">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Search</th>
                  <th scope="col">Status</th>
                  <th scope="col">Code</th>
                </tr>
              </thead>
              <tbody>
                {errors.data.queries.map((q) => (
                  <tr key={q.id}>
                    <td className="rc-c-when" data-label="When">
                      <When iso={q.created_at} />
                    </td>
                    <td className="rc-c-main">
                      <button type="button" className="link-button rc-row-title" onClick={() => openQuery(q.id)}>
                        Search #{q.id}
                      </button>
                      <div className="muted small">{agentTitle(q.agent)}</div>
                    </td>
                    <td data-label="Status">
                      <StatusPill status={q.status} />
                    </td>
                    <td data-label="Code">
                      {q.reason_code ? <code>{q.reason_code}</code> : <span className="muted">—</span>}
                      {q.http_status ? <div className="muted small">HTTP {q.http_status}</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card
        title={
          <>
            <Icon name="plug" size={18} className="card-icon" /> Not in this build
          </>
        }
      >
        <ul className="list small">
          <li>{FETCH_NOTE}.</li>
          <li>{DEEPSEEK_NOTE}.</li>
        </ul>
      </Card>
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Blocked attempts (screen 9).

function Blocked() {
  const [agent, setAgent] = useState("");
  const [offset, setOffset] = useState(0);
  const key = JSON.stringify({ agent, offset });
  const list = usePoll(
    () => api.get<{ blocked: ResearchBlocked[]; total: number }>(`${BASE}/blocked${qs({ agent, limit: PAGE, offset })}`).then((r) => ({ key, r })),
    30_000,
    [key],
  );
  const current = list.data && list.data.key === key ? list.data.r : null;
  return (
    <Card
      title={
        <>
          <Icon name="shield" size={18} className="card-icon" /> Blocked attempts
        </>
      }
      actions={current ? <span className="small">{plural(current.total, "attempt")}</span> : null}
    >
      <div className="rc-filters">
        <AgentFilter
          value={agent}
          onChange={(v) => {
            setAgent(v);
            setOffset(0);
          }}
        />
      </div>
      <p className="muted small">
        Searches refused before anything was stored or sent. Each keeps its reason code only, never the text and never a hash of it. When a lead or a ticket
        caused it, its number is shown to people with the sensitive-data permission, so a planted record shows up for them.
      </p>
      <ErrorNote error={list.error ? `Could not load the blocked attempts: ${list.error}` : null} />
      {!current ? (
        list.error ? null : <Empty>Loading…</Empty>
      ) : !current.blocked.length ? (
        <Empty>No blocked attempts.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table rc-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Reason</th>
                <th scope="col">Agent</th>
                <th scope="col">Person</th>
                <th scope="col">Lead or ticket</th>
              </tr>
            </thead>
            <tbody>
              {current.blocked.map((b) => (
                <tr key={b.id}>
                  <td className="rc-c-when" data-label="When">
                    <When iso={b.created_at} />
                  </td>
                  <td className="rc-c-main">
                    <StatusPill status={b.status} /> <Reason code={b.reason_code} />
                  </td>
                  <td data-label="Agent">
                    {agentTitle(b.agent)}
                    <div className="muted small">{b.capability}</div>
                  </td>
                  <td data-label="Person">
                    {b.requested_by || "—"}
                    <div className="muted small">{TRIGGER[b.trigger_source] ?? words(b.trigger_source)}</div>
                  </td>
                  <td data-label="Lead or ticket">
                    <BlockedBy value={b.blocked_by_ref} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {current ? <Pager offset={offset} shown={current.blocked.length} total={current.total} page={PAGE} onOffset={setOffset} /> : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Policy and settings (screen 11, admins). The settings form is also the Research card on the Settings page.

const MODES: { key: ResearchMode; label: string; text: string }[] = [
  { key: "off", label: "Off", text: "No searches: every research capability is refused. The default." },
  { key: "mock", label: "Mock", text: "Made-up answers from inside the Command Center, for trying the screens. Nothing leaves this PC." },
  { key: "live", label: "Live", text: `${SENT_NOTICE}. Needs the typed confirmation.` },
];

const LIMITS: { key: "global_daily_cap" | "person_daily_cap" | "sources_days" | "logs_days" | "usage_days"; label: string; hint: string; min: number }[] = [
  { key: "global_daily_cap", label: "Searches per day, everyone", hint: "50 to start with. 0 refuses every search.", min: 0 },
  { key: "person_daily_cap", label: "Searches per day, per person", hint: "30 to start with.", min: 0 },
  { key: "sources_days", label: "Keep pages (days)", hint: "After a page was last seen; 30 by default.", min: 1 },
  { key: "logs_days", label: "Keep the search log (days)", hint: "90 by default.", min: 1 },
  { key: "usage_days", label: "Keep daily totals (days)", hint: "730 (two years) by default.", min: 1 },
];

function LimitsForm({ settings, onSaved }: { settings: ResearchSettings; onSaved: (s: ResearchSettings) => void }) {
  const [draft, setDraft] = useState<Record<string, string>>(() => ({
    ...Object.fromEntries(LIMITS.map((l) => [l.key, String(settings[l.key])])),
    owner_terms: settings.owner_terms.join("\n"),
  }));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const terms = draft.owner_terms
    .split("\n")
    .map((t) => t.trim())
    .filter(Boolean);

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    const changes: Record<string, unknown> = {};
    for (const l of LIMITS) if (Number(draft[l.key]) !== settings[l.key]) changes[l.key] = Number(draft[l.key]);
    if (terms.join("\n") !== settings.owner_terms.join("\n")) changes.owner_terms = terms;
    if (!Object.keys(changes).length) {
      setError("Nothing was changed.");
      return;
    }
    try {
      const r = await api.patch<{ settings: ResearchSettings }>(`${BASE}/settings`, changes);
      setSaved(true);
      onSaved(r.settings);
    } catch (err) {
      setError(errText(err));
    }
  }

  return (
    <form onSubmit={save} className="rc-limits">
      <div className="rc-limit-grid">
        {LIMITS.map((l) => (
          <label key={l.key}>
            {l.label}
            <input type="number" min={l.min} max={l.key.endsWith("_cap") ? 10_000 : 3650} value={draft[l.key]} onChange={(e) => setDraft({ ...draft, [l.key]: e.target.value })} required />
            <span className="muted small">{l.hint}</span>
          </label>
        ))}
      </div>
      <label>
        Your business names
        <textarea rows={3} value={draft.owner_terms} onChange={(e) => setDraft({ ...draft, owner_terms: e.target.value })} placeholder="One per line" />
        <span className="muted small">
          One per line (class O). Agents allowed class O may search for them; for the others, a query that names them is refused.
        </span>
      </label>
      <ErrorNote error={error} />
      {saved ? <div className="alert alert-good">Saved.</div> : null}
      <button className="btn primary" type="submit">
        Save limits
      </button>
    </form>
  );
}

/** Mode (with the typed confirmation for live), caps, retention and the owner's business names. */
export function ResearchSettingsForm() {
  const s = usePoll(() => api.get<{ settings: ResearchSettings }>(`${BASE}/settings`), 0);
  const [confirmLive, setConfirmLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const settings = s.data?.settings;

  async function setMode(mode: ResearchMode, confirm?: string) {
    setError(null);
    setLiveError(null);
    try {
      const r = await api.patch<{ settings: ResearchSettings }>(`${BASE}/settings`, confirm ? { mode, confirm } : { mode });
      s.setData(r);
      setConfirmLive(false);
    } catch (err) {
      if (confirm) setLiveError(errText(err));
      else setError(errText(err));
    }
  }

  if (!settings) return s.error ? <ErrorNote error={`Could not load the research settings: ${s.error}`} /> : <p className="muted">Loading…</p>;
  return (
    <div className="rc-settings">
      {!settings.readable ? (
        <div className="alert alert-warn" role="status">
          The research settings could not be read, so every search is refused. Choosing a mode or saving the limits stores them again.
        </div>
      ) : null}
      {!settings.entitled ? <p className="text-warn small">This edition does not include web research: it stays off.</p> : null}
      <fieldset className="rc-modes">
        <legend>Mode</legend>
        {MODES.map((m) => (
          <label key={m.key} className={`rc-mode${settings.mode === m.key ? " rc-mode-on" : ""}`}>
            <input
              type="radio"
              name="research-mode"
              value={m.key}
              checked={settings.mode === m.key}
              disabled={m.key !== "off" && !settings.entitled}
              onChange={() => (m.key === "live" ? setConfirmLive(true) : void setMode(m.key))}
            />
            <span>
              <strong>{m.label}</strong>
              <span className="muted small">{m.text}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <ErrorNote error={error} />
      <LimitsForm key={JSON.stringify(settings)} settings={settings} onSaved={(next) => s.setData({ settings: next })} />
      <p className="muted small">
        Engines: DuckDuckGo, Brave and Wikipedia, fixed in this build. {DEEPSEEK_NOTE}. {FETCH_NOTE}.
      </p>
      {confirmLive ? (
        <TypedConfirm
          title="Switch live web research on"
          name={settings.live_confirm || LIVE_CONFIRM}
          action="Switch live research on"
          error={liveError}
          onConfirm={(typed) => setMode("live", typed)}
          onCancel={() => {
            setConfirmLive(false);
            setLiveError(null);
          }}
        >
          Live searches are sent to DuckDuckGo, Brave and Wikipedia from this PC's internet address. Every query is screened first, and the daily caps keep the
          pace low, but a search engine that sees too many searches may ask this PC's browsers for CAPTCHAs too.
        </TypedConfirm>
      ) : null}
    </div>
  );
}

const CLASSES: [string, string][] = [
  ["G", "General public topics"],
  ["S", "Software names and versions, CVE ids, typed by a person"],
  ["O", "Your own business names"],
  ["C", "Clients' business names and websites"],
];

function PolicyRow({ p, onSaved }: { p: ResearchPolicy; onSaved: (p: ResearchPolicy) => void }) {
  const [enabled, setEnabled] = useState(p.search_enabled);
  const [cap, setCap] = useState(String(p.search_daily_cap));
  const [classes, setClasses] = useState<string[]>(p.data_classes);
  const [domains, setDomains] = useState(p.blocked_domains.join("\n"));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const domainList = domains
    .split(/[\s,]+/)
    .map((d) => d.trim())
    .filter(Boolean);
  const changes: Record<string, unknown> = {};
  if (enabled !== p.search_enabled) changes.search_enabled = enabled;
  if (Number(cap) !== p.search_daily_cap) changes.search_daily_cap = Number(cap);
  if ([...classes].sort().join() !== [...p.data_classes].sort().join()) changes.data_classes = classes;
  if (domainList.join("\n") !== p.blocked_domains.join("\n")) changes.blocked_domains = domainList;
  const dirty = Object.keys(changes).length > 0;
  const title = p.title || agentTitle(p.agent);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.patch<{ policy: ResearchPolicy }>(`${BASE}/policy/${p.agent}`, changes);
      onSaved(r.policy);
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  if (p.never) {
    return (
      <tr className="rc-never">
        <td className="rc-c-main">
          <strong>{title}</strong>
        </td>
        <td colSpan={5} data-label="Web search">
          <Pill value="disabled" label="never" /> <span className="muted small">Credit Repair never searches the web: enforced in code, whatever this table says.</span>
        </td>
      </tr>
    );
  }

  return (
    <tr>
      <td className="rc-c-main">
        <strong>{title}</strong>
        <div className="muted small">{p.capabilities.length ? p.capabilities.join(", ") : "no research capability"}</div>
        {p.updated_by ? <div className="muted small">changed by {p.updated_by}</div> : null}
        <ErrorNote error={error} />
      </td>
      <td data-label="Web search">
        <label className="check">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} aria-label={`Web search for ${title}`} /> {enabled ? "on" : "off"}
        </label>
      </td>
      <td data-label="Per day">
        <input className="rc-cap" type="number" min={0} max={10_000} value={cap} onChange={(e) => setCap(e.target.value)} aria-label={`Searches per day for ${title}`} />
      </td>
      <td data-label="Classes">
        <span className="rc-classes">
          {CLASSES.map(([c, help]) => (
            <label key={c} className="check" title={help}>
              <input
                type="checkbox"
                checked={classes.includes(c)}
                onChange={(e) => setClasses(e.target.checked ? [...classes, c] : classes.filter((x) => x !== c))}
                aria-label={`Class ${c} (${help}) for ${title}`}
              />{" "}
              {c}
            </label>
          ))}
        </span>
      </td>
      <td data-label="Blocked domains">
        <textarea className="rc-domains" rows={2} value={domains} onChange={(e) => setDomains(e.target.value)} placeholder="one per line" aria-label={`Blocked domains for ${title}`} />
      </td>
      <td>
        <button type="button" className="btn small primary" disabled={!dirty || busy} onClick={() => void save()}>
          Save
        </button>
      </td>
    </tr>
  );
}

function PolicyMatrix() {
  const list = usePoll(() => api.get<{ policies: ResearchPolicy[] }>(`${BASE}/policy`), 0);
  function saved(p: ResearchPolicy) {
    list.setData((prev) => (prev ? { policies: prev.policies.map((x) => (x.agent === p.agent ? p : x)) } : prev));
  }
  return (
    <Card
      title={
        <>
          <Icon name="agents" size={18} className="card-icon" /> Agent permissions
        </>
      }
    >
      <p className="muted small">
        Which agents may search, how often, and what a query may name. Classes: G general public topics; S software names and versions or CVE ids, typed
        by a person; O your own business names; C clients' business names and websites. Personal data, leads, Credit Repair, secrets and internal names are
        never allowed, for any agent.
      </p>
      <ErrorNote error={list.error ? `Could not load the agent permissions: ${list.error}` : null} />
      {!list.data ? (
        list.error ? null : <p className="muted">Loading…</p>
      ) : (
        <div className="table-wrap">
          <table className="table rc-table rc-matrix">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Web search</th>
                <th scope="col">Per day</th>
                <th scope="col">Classes</th>
                <th scope="col">Blocked domains</th>
                <th scope="col">
                  <span className="sr-only">Save</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {list.data.policies.map((p) => (
                // A saved row starts again from what the API stored; the other rows keep what is being typed in them.
                <PolicyRow key={`${p.agent}:${p.updated_at ?? ""}`} p={p} onSaved={saved} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function ConnectivityTest({ mode }: { mode: ResearchMode | undefined }) {
  const [taskId, setTaskId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ task: Task }>(`${BASE}/test`, {});
      setTaskId(r.task.id);
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={
        <>
          <Icon name="pulse" size={18} className="card-icon" /> Connectivity test
        </>
      }
    >
      <p className="muted small">
        One fixed search ("Wikipedia", 3 results), run by the worker like any other: screened, counted against today's caps and logged. In mock mode it never
        leaves this PC.
      </p>
      <button type="button" className="btn" disabled={busy || mode === "off" || mode === undefined} onClick={() => void run()}>
        <Icon name="play" size={14} /> Run the connectivity test
      </button>
      {mode === "off" ? <p className="muted small">Switch research to mock or live first.</p> : null}
      <ErrorNote error={error} />
      {taskId ? <TestProgress key={taskId} id={taskId} /> : null}
    </Card>
  );
}

function TestProgress({ id }: { id: number }) {
  const [done, setDone] = useState(false);
  const detail = usePoll(() => api.get<{ task: Task }>(`/api/tasks/${id}`), done ? 0 : 2_000, [id, done]);
  const task = detail.data?.task;
  useEffect(() => {
    if (task && !ACTIVE.includes(task.status)) setDone(true);
  }, [task]);
  return (
    <p className="rc-progress" aria-live="polite">
      Task <Link to={`/tasks?id=${id}`}>#{id}</Link> {task ? <Pill value={task.status} /> : null} {task?.summary ? <span>{task.summary}</span> : null}
      {task?.error ? <span className="text-bad"> {task.error}</span> : null}
    </p>
  );
}

function Purge() {
  const [days, setDays] = useState("90");
  const [usage, setUsage] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ purged: Record<string, number>; note: string } | null>(null);

  async function purge(confirm: string) {
    setError(null);
    try {
      const r = await api.post<{ purged: Record<string, number>; note: string }>(`${BASE}/purge`, { older_than_days: Number(days), include_usage: usage, confirm });
      setDone(r);
      setConfirming(false);
    } catch (err) {
      setError(errText(err));
    }
  }

  return (
    <Card
      title={
        <>
          <Icon name="alert" size={18} className="card-icon" /> Purge research records
        </>
      }
    >
      <p className="muted small">
        Removes searches, their results, pages and cached answers older than the days you give, and clears the parameters and citations of research tasks and
        the query of web searches in Supervisor plans. Today's caps keep counting. Supervisor requests keep their message, reply and summary, as every request
        does. Container logs and earlier backups are not purged.
      </p>
      <div className="rc-row">
        <label>
          Older than (days)
          <input type="number" min={0} max={3650} value={days} onChange={(e) => setDays(e.target.value)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={usage} onChange={(e) => setUsage(e.target.checked)} /> Daily totals too
        </label>
      </div>
      <button type="button" className="btn danger" disabled={days === "" || Number(days) < 0} onClick={() => setConfirming(true)}>
        Purge…
      </button>
      {done ? (
        <div className="alert alert-good" role="status">
          Purged: {Object.entries(done.purged)
            .map(([k, n]) => `${n} ${k}`)
            .join(", ")}
          . {done.note}
        </div>
      ) : null}
      {confirming ? (
        <TypedConfirm
          title="Purge research records"
          name={PURGE_CONFIRM}
          action="Purge"
          error={error}
          onConfirm={purge}
          onCancel={() => {
            setConfirming(false);
            setError(null);
          }}
        >
          This permanently removes research records older than {Number(days)} {Number(days) === 1 ? "day" : "days"}
          {usage ? ", and the daily totals" : ""}. It cannot be undone. Container logs and earlier backups keep what they hold: recreate the search container to
          clear its log.
        </TypedConfirm>
      ) : null}
    </Card>
  );
}

function PolicyAndSettings({ health }: { health: ResearchHealth | null }) {
  return (
    <>
      <Card
        title={
          <>
            <Icon name="settings" size={18} className="card-icon" /> Research settings
          </>
        }
      >
        <ResearchSettingsForm />
      </Card>
      <PolicyMatrix />
      <div className="grid-2 rc-grid">
        <ConnectivityTest mode={health?.mode} />
        <Purge />
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// The page.

/** Keeps one part of this page from taking the whole dashboard down when the API answers in a shape this build does not
 * expect: that part says so instead (the same guard as the JARVIS Brain page). */
class Contained extends Component<{ children: ReactNode; what: string; onClose?: () => void }, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(err: unknown) {
    return { error: err instanceof Error ? err.message : String(err) };
  }

  componentDidCatch(err: unknown) {
    console.error(err);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const alert = (
      <div className="alert alert-bad" role="alert">
        {this.props.what} could not be shown: the API answered in a form this dashboard does not understand ({this.state.error}).
      </div>
    );
    return this.props.onClose ? (
      <Modal title="Could not be shown" onClose={this.props.onClose}>
        {alert}
      </Modal>
    ) : (
      alert
    );
  }
}

type TabKey = "overview" | "new" | "history" | "sources" | "usage" | "status" | "blocked" | "settings";
const TABS: { key: TabKey; label: string; icon: IconName; admin?: boolean }[] = [
  { key: "overview", label: "Overview", icon: "dashboard" },
  { key: "new", label: "New research", icon: "search" },
  { key: "history", label: "Search history", icon: "history" },
  { key: "sources", label: "Sources", icon: "globe" },
  { key: "usage", label: "Usage", icon: "pulse" },
  { key: "status", label: "Provider status", icon: "radar" },
  { key: "blocked", label: "Blocked attempts", icon: "shield" },
  { key: "settings", label: "Policy and settings", icon: "settings", admin: true },
];

function ResearchCenter({ isAdmin }: { isAdmin: boolean }) {
  const [params, setParams] = useSearchParams();
  const tabs = TABS.filter((t) => !t.admin || isAdmin);
  const tab: TabKey = tabs.find((t) => t.key === params.get("tab"))?.key ?? "overview";
  const tabRefs = useRef<Partial<Record<TabKey, HTMLButtonElement | null>>>({});
  const health = usePoll(() => api.get<ResearchHealth>(`${BASE}/health`), 30_000);
  const queryId = Number(params.get("query")) || null;
  const sourceId = Number(params.get("source")) || null;

  function update(changes: Record<string, string | null>) {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(changes)) {
          if (v) next.set(k, v);
          else next.delete(k);
        }
        return next;
      },
      { replace: true },
    );
  }

  const selectTab = (key: TabKey) => update({ tab: key === "overview" ? null : key, query: null, source: null });
  const openQuery = (n: number) => update({ query: String(n), source: null });
  const openSource = (n: number) => update({ source: String(n), query: null });

  function onTabKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = tabs.findIndex((t) => t.key === tab);
    const n = tabs.length;
    const next = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    selectTab(tabs[next].key);
    tabRefs.current[tabs[next].key]?.focus();
  }

  const h = health.data;
  return (
    <div className="page rc-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Research Center</h1>
          <p className="muted">
            Web searches for the agents, through the Command Center's private search service. Every query is checked before it leaves this PC, every result is
            an unverified citation, and nothing a page says can approve, run or change anything.
          </p>
        </div>
      </header>

      <div className="tabs rc-tabs" role="tablist" aria-label="Research Center" onKeyDown={onTabKey}>
        {tabs.map((t) => (
          <button
            key={t.key}
            ref={(el) => {
              tabRefs.current[t.key] = el;
            }}
            type="button"
            role="tab"
            id={`rc-tab-${t.key}`}
            aria-selected={tab === t.key}
            aria-controls={`rc-panel-${t.key}`}
            tabIndex={tab === t.key ? 0 : -1}
            className={tab === t.key ? "active" : ""}
            onClick={() => selectTab(t.key)}
          >
            <Icon name={t.icon} size={15} className="rc-tab-icon" />
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      <div className="rc-panel" role="tabpanel" id={`rc-panel-${tab}`} aria-labelledby={`rc-tab-${tab}`}>
        <Contained key={tab} what="This tab">
          {tab === "overview" ? <Overview health={h} healthError={health.error} openTab={selectTab} openQuery={openQuery} /> : null}
          {tab === "new" ? <NewResearch health={h} openQuery={openQuery} /> : null}
          {tab === "history" ? <History openQuery={openQuery} /> : null}
          {tab === "sources" ? <Sources openSource={openSource} /> : null}
          {tab === "usage" ? <Usage /> : null}
          {tab === "status" ? <ProviderStatus health={h} healthError={health.error} openQuery={openQuery} /> : null}
          {tab === "blocked" ? <Blocked /> : null}
          {tab === "settings" && isAdmin ? <PolicyAndSettings health={h} /> : null}
        </Contained>
      </div>

      {queryId ? (
        <Contained key={`query:${queryId}`} what="This search" onClose={() => update({ query: null })}>
          <QueryModal id={queryId} onClose={() => update({ query: null })} onOpenSource={openSource} />
        </Contained>
      ) : null}
      {sourceId ? (
        <Contained key={`source:${sourceId}`} what="This page" onClose={() => update({ source: null })}>
          <SourceModal id={sourceId} onClose={() => update({ source: null })} onOpenQuery={openQuery} />
        </Contained>
      ) : null}
    </div>
  );
}

export default function Research() {
  const { user, canOperate } = useAuth();
  // Viewers: nothing here, and nothing is asked of the API (owner decision 13).
  if (!canOperate) {
    return (
      <div className="page rc-page">
        <header className="page-head">
          <div>
            <div className="eyebrow">Operations</div>
            <h1>Research Center</h1>
            <p className="muted">{NOT_FOR_VIEWERS}</p>
          </div>
        </header>
      </div>
    );
  }
  return <ResearchCenter isAdmin={user.role === "admin"} />;
}
