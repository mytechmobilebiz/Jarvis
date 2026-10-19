import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Finding, type Schedule, type Server, type Site, type SiteCheck, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Findings, Modal, Pill, SeverityIcon, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { cadence, confirmReason, coverage, pctText, shortWhen, TypedConfirm, type UptimeWindow } from "./shared";
import "./Websites.css";

/** A site's open issue (0.5.0 API): the stored finding plus the check it came from. */
export type OpenFinding = Finding & { source?: "quick" | "deep"; checked_at?: string };

/** /api/sites rows with the 0.5.0 open issues: the latest uptime check and the latest security scan together (C2-10). */
export type SiteView = Site & {
  open_findings?: OpenFinding[];
  open_findings_sources?: { quick: string | null; deep: string | null };
};

/** The site's open issues (warning and critical): from both checks when the API gives them, else from the latest check only. */
export function openIssues(site: SiteView): OpenFinding[] {
  const list = site.open_findings ?? site.last_check?.findings ?? [];
  return list.filter((f) => f.severity !== "info");
}

const SOURCE: Record<string, string> = { deep: "security scan", quick: "uptime check" };

/** "the security scan at 03:17 and the uptime check at 04:30", or null before the 0.5.0 API. */
export function issueSources(site: SiteView, now: number = Date.now()): string | null {
  const src = site.open_findings_sources;
  if (!src) return null;
  const parts = (["deep", "quick"] as const).filter((k) => src[k]).map((k) => `the ${SOURCE[k]} of ${shortWhen(src[k]!, now)}`);
  return parts.length ? parts.join(" and ") : null;
}

/** Whether repairs on a site count as production: its own environment, or its server's (as the API decides). */
function effectiveProduction(environment: string, server: Pick<Server, "environment"> | null | undefined): boolean {
  return environment === "production" || server?.environment === "production";
}

/** "run every 15 minutes", "are paused", "run only when asked": for the page's opening sentence (C2-15). */
function runs(c: string, plural: boolean): string {
  if (c === "paused") return plural ? "are paused" : "is paused";
  if (c === "not scheduled") return plural ? "run only when asked" : "runs only when asked";
  return `${plural ? "run" : "runs"} ${c}`;
}

function AddSite({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [client, setClient] = useState("");
  const [environment, setEnvironment] = useState("production");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/sites", { name, url, client, environment });
      setName("");
      setUrl("");
      setClient("");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <form className="grid-form" onSubmit={submit}>
      <input aria-label="Site name" placeholder="Name, e.g. Acme Plumbing" value={name} onChange={(e) => setName(e.target.value)} required />
      <input aria-label="Site URL" placeholder="URL or domain" value={url} onChange={(e) => setUrl(e.target.value)} required />
      <input aria-label="Client" placeholder="Client (optional)" value={client} onChange={(e) => setClient(e.target.value)} />
      <select aria-label="Site environment" value={environment} onChange={(e) => setEnvironment(e.target.value)}>
        <option value="production">production</option>
        <option value="staging">staging</option>
        <option value="test">test</option>
      </select>
      <button className="btn primary" type="submit">
        Add site
      </button>
      <ErrorNote error={error} />
    </form>
  );
}

const REPAIRS: { capability: string; label: string; plugin?: boolean; params?: Record<string, unknown> }[] = [
  { capability: "repair_restore_site_url", label: "Restore home/siteurl" },
  { capability: "repair_deactivate_plugin", label: "Deactivate plugin", plugin: true },
  { capability: "repair_activate_plugin", label: "Activate plugin", plugin: true },
  { capability: "repair_update_plugin", label: "Update plugin", plugin: true },
  { capability: "repair_reinstall_plugin", label: "Reinstall plugin", plugin: true },
  { capability: "repair_reinstall_core", label: "Reinstall WordPress core files" },
  { capability: "repair_quarantine_debug_log", label: "Quarantine debug.log" },
  { capability: "repair_maintenance_mode", label: "Maintenance mode off", params: { on: false } },
  { capability: "repair_flush_caches", label: "Flush caches" },
];

function uptimeTone(v: number | null | undefined): "good" | "warn" | "bad" | "muted" {
  return v == null ? "muted" : v >= 99.5 ? "good" : v >= 98 ? "warn" : "bad";
}

/** One window's uptime with what it rests on: "99.5% · 96 checks", or "Not enough data yet (2 checks since 05:27)". */
function UptimeFigure({ site, window }: { site: Site; window: UptimeWindow }) {
  const c = coverage(site.uptime, window);
  return (
    <span className="ws-figure">
      {c.pct != null ? <strong className="tabular">{pctText(c.pct)}</strong> : null}
      <span className={`small ${c.pct != null ? "muted" : "ws-nodata"}`}>{c.pct != null ? ` · ${c.text}` : c.text}</span>
    </span>
  );
}

function ServerLink({ site, onChanged }: { site: Site; onChanged: () => void }) {
  const servers = usePoll(() => api.get<{ servers: Server[] }>("/api/servers"), 0);
  const [serverId, setServerId] = useState(site.server_id ? String(site.server_id) : "");
  const [key, setKey] = useState(site.server_site_key);
  const [error, setError] = useState<string | null>(null);
  // A change that takes the site out of production waits here until the admin types its name (C3-06).
  const [pending, setPending] = useState<{ body: Record<string, unknown>; reason: string } | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const list = servers.data?.servers ?? [];
  const chosen = list.find((s) => String(s.id) === serverId);
  const reported = chosen?.last_hello.sites ?? {};
  const keys = Object.keys(reported);
  const keyHintId = `site-key-hint-${site.id}`;
  const serverOf = (id: number | null) => (id ? list.find((s) => s.id === id) : null);

  async function save(body: Record<string, unknown>, confirm?: string) {
    setError(null);
    setConfirmError(null);
    try {
      await api.patch(`/api/sites/${site.id}`, confirm ? { ...body, confirm } : body);
      setPending(null);
      onChanged();
    } catch (err) {
      const reason = confirmReason(err);
      if (reason && !confirm) setPending({ body, reason });
      else if (confirm) setConfirmError(err instanceof Error ? err.message : String(err));
      else setError(err instanceof Error ? err.message : String(err));
    }
  }

  /** Asks for the typed name first when the change visibly takes the site out of production; the API checks again either way. */
  function change(body: Record<string, unknown>, environment: string, newServerId: number | null) {
    const was = effectiveProduction(site.environment, serverOf(site.server_id));
    const will = effectiveProduction(environment, serverOf(newServerId));
    if (was && !will)
      setPending({
        body,
        reason: `this takes ${site.name} out of production (now ${environment}${newServerId ? ` on server ${serverOf(newServerId)?.name ?? newServerId}` : ", not linked to a server"}): its repairs would no longer need an admin's typed confirmation.`,
      });
    else void save(body);
  }

  return (
    <div className="boxed-section">
      <h3>Server link and environment (admin)</h3>
      <div className="grid-form">
        <select
          aria-label="Site environment setting"
          value={site.environment}
          onChange={(e) => e.target.value !== site.environment && change({ environment: e.target.value }, e.target.value, site.server_id)}
        >
          <option value="production">production</option>
          <option value="staging">staging</option>
          <option value="test">test</option>
        </select>
        <select aria-label="Server" value={serverId} onChange={(e) => setServerId(e.target.value)}>
          <option value="">no server</option>
          {list.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <input
          aria-label="Site key on the server"
          placeholder="site key in cc-gate config"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          list="site-keys"
          aria-describedby={chosen ? keyHintId : undefined}
        />
        {/* Each key with the address the server's cc-gate gives it: the API accepts only the key whose address is this site's. */}
        <datalist id="site-keys">
          {keys.map((k) => (
            <option key={k} value={k} label={`${k} (${reported[k]?.url || "no URL reported"})`} />
          ))}
        </datalist>
        <button
          className="btn small"
          onClick={() =>
            serverId
              ? change({ server_id: Number(serverId), server_site_key: key }, site.environment, Number(serverId))
              : change({ unlink_server: true }, site.environment, null)
          }
        >
          Save link
        </button>
      </div>
      {chosen ? (
        <p className="muted small" id={keyHintId}>
          {keys.length
            ? `Pick the key whose address is this site's (${site.url}); ${chosen.name} reports ${keys.length === 1 ? "one key" : `${keys.length} keys`}.`
            : `${chosen.name} has reported no site keys yet: run Test connection on the CWP / Servers page first.`}
        </p>
      ) : null}
      <ErrorNote error={error ?? servers.error} />
      {pending ? (
        <TypedConfirm
          title={`Take ${site.name} out of production`}
          name={site.name}
          action="Confirm the change"
          error={confirmError}
          onCancel={() => {
            setPending(null);
            setConfirmError(null);
          }}
          onConfirm={(typed) => save(pending.body, typed)}
        >
          {pending.reason.charAt(0).toUpperCase() + pending.reason.slice(1)}
        </TypedConfirm>
      ) : null}
    </div>
  );
}

function ServerSide({ site, production, onTask }: { site: Site; production: boolean | null; onTask: (t: Task) => void }) {
  const { canOperate } = useAuth();
  const [slug, setSlug] = useState("");
  const [error, setError] = useState<string | null>(null);
  const d = site.last_diagnosis ?? {};

  async function run(capability: string, params: Record<string, unknown>) {
    setError(null);
    try {
      const r = await api.post<{ task: Task }>("/api/agents/wordpress_security/tasks", { capability, params: { site_id: site.id, ...params } });
      onTask(r.task);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (!site.server_id) {
    return <p className="muted small">Not linked to a server: server-side checks and repairs are not available. An admin can link it below.</p>;
  }
  return (
    <div>
      <h3>
        Server-side (WP-CLI) {site.last_diagnosis_at ? <span className="muted small">(checked <Time iso={site.last_diagnosis_at} />)</span> : null}
      </h3>
      {d.at ? (
        <dl className="kv">
          <dt>WordPress</dt>
          <dd>
            {d.core_version ?? "version not read"} · core files{" "}
            {d.core_verify?.verified === true ? (
              "match the official checksums"
            ) : d.core_verify?.verified === false ? (
              <strong>{d.core_verify.extra_files?.length ? `modified (${d.core_verify.extra_files.length} unexpected file(s))` : "modified"}</strong>
            ) : (
              "not checked"
            )}
          </dd>
          <dt>home / siteurl</dt>
          <dd>
            {!d.site_urls ? (
              "not read"
            ) : d.site_urls.matches ? (
              "correct"
            ) : d.site_urls.subdirectory_install_suspected ? (
              `siteurl ${d.site_urls.suspected_siteurl ?? d.site_urls.siteurl}: WordPress in its own folder? (set "siteurl" in the cc-gate configuration)`
            ) : (
              <strong>{`${d.site_urls.home} / ${d.site_urls.siteurl} (expected ${d.site_urls.expected}${
                d.site_urls.expected_siteurl && d.site_urls.expected_siteurl !== d.site_urls.expected ? ` / ${d.site_urls.expected_siteurl}` : ""
              })`}</strong>
            )}
          </dd>
          <dt>Plugins</dt>
          <dd>
            {d.plugins
              ? d.plugins.map((p) => `${p.name} ${p.version}${p.status !== "active" ? ` (${p.status})` : ""}${p.update === "available" ? ` → ${p.update_version}` : ""}`).join(", ") || "none"
              : "not read"}
          </dd>
          <dt>Administrators</dt>
          <dd>
            {d.admins ? d.admins.map((a) => a.user_login).join(", ") || "—" : "not read"}
            {d.super_admins?.length ? ` · super admins: ${d.super_admins.join(", ")}` : ""}
          </dd>
          {d.errors && Object.keys(d.errors).length ? (
            <>
              <dt>Not checked</dt>
              <dd>
                <strong>{Object.keys(d.errors).join(", ")}</strong> <span className="muted small">(the server could not answer)</span>
              </dd>
            </>
          ) : null}
          {d.last_failed_attempt ? (
            <>
              <dt>Latest check</dt>
              <dd>
                <strong>failed</strong> <Time iso={d.last_failed_attempt.at} />: {d.last_failed_attempt.error}{" "}
                <span className="muted small">(the results above are older)</span>
              </dd>
            </>
          ) : null}
        </dl>
      ) : d.last_failed_attempt ? (
        <p className="small">
          <strong>The server-side check failed</strong> <Time iso={d.last_failed_attempt.at} />: {d.last_failed_attempt.error}
        </p>
      ) : (
        <p className="muted small">No server-side check yet.</p>
      )}
      {d.findings?.length ? <Findings findings={d.findings} /> : null}
      {canOperate ? (
        <>
          <div className="row-actions">
            <button className="btn small" onClick={() => void run("wp_diagnose", {})}>
              Run server-side check
            </button>
          </div>
          <h3>Repairs</h3>
          <p className="small muted">
            Each repair waits for approval
            {production === null
              ? ""
              : production
                ? site.environment === "production"
                  ? " by an admin who types the site's name (production)"
                  : " by an admin who types the site's name (the site's server is production)"
                : ""}
            , backs up the database first, changes one thing and verifies the site afterwards. Repairs only run if the server allows changes.
          </p>
          <div className="row-actions">
            <input aria-label="Plugin folder name" placeholder="plugin, e.g. akismet" value={slug} onChange={(e) => setSlug(e.target.value.trim())} />
          </div>
          <div className="repair-grid">
            {REPAIRS.map((r) => (
              <button
                key={r.capability}
                className="btn small"
                disabled={r.plugin && !slug}
                onClick={() => void run(r.capability, { ...(r.params ?? {}), ...(r.plugin ? { slug } : {}) })}
              >
                {r.label}
                {r.plugin && slug ? ` (${slug})` : ""}
              </button>
            ))}
          </div>
        </>
      ) : null}
      <ErrorNote error={error} />
    </div>
  );
}

function SiteDetail({ site, onClose, onChanged }: { site: SiteView; onClose: () => void; onChanged: () => void }) {
  const { user, canOperate } = useAuth();
  const detail = usePoll(() => api.get<{ site: SiteView }>(`/api/sites/${site.id}`), 10_000, [site.id]);
  const servers = usePoll(() => (site.server_id ? api.get<{ servers: Server[] }>("/api/servers") : Promise.resolve(null)), 30_000, [site.server_id]);
  const checks = usePoll(() => api.get<{ checks: SiteCheck[] }>(`/api/sites/${site.id}/checks?limit=30`), 5_000, [site.id]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const full: SiteView = { ...site, ...(detail.data?.site ?? {}) };
  const server = full.server_id ? servers.data?.servers.find((s) => s.id === full.server_id) : null;
  // Unknown until the server list has loaded; the note then says nothing rather than guess.
  const production = full.server_id && !servers.data ? (full.environment === "production" ? true : null) : effectiveProduction(full.environment, server);
  const issues = openIssues(full);
  const sources = issueSources(full);
  const latestDeep = checks.data?.checks.find((c) => c.details.deep);
  const latest = checks.data?.checks[0] ?? site.last_check;
  const redirect = latestDeep?.details.redirect_check;

  async function act(fn: () => Promise<unknown>, done: string) {
    setError(null);
    try {
      await fn();
      setMessage(done);
      onChanged();
      void detail.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function onTask(t: Task) {
    setMessage(t.status === "awaiting_approval" ? `Task #${t.id} is waiting for approval (Approvals page).` : `Task #${t.id} queued.`);
  }

  return (
    <Modal title={site.name} onClose={onClose} wide>
      <p className="ws-detail-head">
        <a href={site.url} target="_blank" rel="noreferrer noopener">
          {site.url}
        </a>{" "}
        {full.enabled ? <Pill value={full.last_status} /> : <Pill value="disabled" label="Paused" />} <Pill value={full.environment} />{" "}
        {site.client ? <span className="muted">· {site.client}</span> : null}
      </p>
      {canOperate ? (
        <div className="row-actions">
          <button
            className="btn primary small"
            onClick={() => void act(() => api.post("/api/agents/wordpress_security/tasks", { capability: "security_scan", params: { site_id: site.id } }), "Security scan queued.")}
          >
            Security scan
          </button>
          <button
            className="btn small"
            onClick={() => void act(() => api.post("/api/agents/wordpress_security/tasks", { capability: "security_report", params: { site_id: site.id } }), "Report queued; it appears under Drafts & reports.")}
          >
            Security report
          </button>
          <button className="btn small" onClick={() => void act(() => api.post(`/api/sites/${site.id}/check`, { deep: false }), "Uptime check queued.")}>
            Uptime check
          </button>
          <button className="btn small" onClick={() => void act(() => api.patch(`/api/sites/${site.id}`, { enabled: !site.enabled }), site.enabled ? "Monitoring paused." : "Monitoring resumed.")}>
            {site.enabled ? "Pause monitoring" : "Resume monitoring"}
          </button>
          <button
            className="btn danger small"
            onClick={() => {
              if (window.confirm(`Stop monitoring ${site.name} and delete its check history? The website itself is not touched.`))
                void act(() => api.del(`/api/sites/${site.id}`), "Removed.").then(onClose);
            }}
          >
            Remove
          </button>
        </div>
      ) : null}
      {message ? (
        <div className="alert alert-good">
          {message} <Link to="/tasks">Tasks</Link>
        </div>
      ) : null}
      <ErrorNote error={error ?? checks.error ?? detail.error} />
      <dl className="kv">
        <dt>Uptime 24 h</dt>
        <dd>
          <UptimeFigure site={full} window="24h" />
          {full.uptime?.avg_response_ms_24h != null ? <span className="muted small"> · avg {full.uptime.avg_response_ms_24h} ms</span> : null}
        </dd>
        <dt>Uptime 7 days</dt>
        <dd>
          <UptimeFigure site={full} window="7d" />
        </dd>
        <dt>Uptime 30 days</dt>
        <dd>
          <UptimeFigure site={full} window="30d" />
          {!full.enabled ? <span className="muted small"> · paused: not checked until monitoring is resumed</span> : null}
        </dd>
        {latest ? (
          <>
            <dt>Last check</dt>
            <dd>
              <Time iso={latest.checked_at} /> · HTTP {latest.http_status ?? "—"} · {latest.response_ms ?? "—"} ms
            </dd>
            <dt>WordPress</dt>
            <dd>{latest.is_wordpress ? `yes, ${latest.wp_version || "version hidden"}` : "not detected"}</dd>
            <dt>TLS certificate</dt>
            <dd>
              {latest.tls_days_left != null
                ? `${latest.tls_days_left} days left${latest.details.tls?.expires_at ? ` (expires ${latest.details.tls.expires_at.slice(0, 10)})` : ""}, ${latest.tls_issuer || "issuer unknown"}${latest.details.tls?.protocol ? `, ${latest.details.tls.protocol}` : ""}`
                : "—"}
            </dd>
          </>
        ) : null}
        {redirect ? (
          <>
            <dt>Redirects</dt>
            <dd>
              {redirect.chain && redirect.chain.length > 1 ? redirect.chain.join(" → ") : "none for ordinary visitors"}
              {Object.entries(redirect.variants ?? {}).map(([label, v]) => (
                <div key={label} className="small">
                  Visitors {label}: {v.offsite_target ? <strong>redirected to {v.offsite_target}</strong> : "same site"}
                </div>
              ))}
            </dd>
          </>
        ) : null}
        {latestDeep?.details.external_scripts ? (
          <>
            <dt>Third-party scripts</dt>
            <dd>{latestDeep.details.external_scripts.join(", ") || "none"}</dd>
          </>
        ) : null}
      </dl>
      <h3>Open issues {sources ? <span className="muted small">(from {sources})</span> : latest ? <span className="muted small">(from the latest check)</span> : null}</h3>
      {issues.length ? (
        <ul className="findings ws-open">
          {issues.map((f, i) => (
            <li key={`${f.code}-${i}`}>
              <SeverityIcon severity={f.severity} />
              <div>
                <strong>{f.title}</strong>
                {f.detail ? <div className="muted small">{f.detail}</div> : null}
                {f.source ? (
                  <div className="muted small">
                    Found by the {SOURCE[f.source] ?? f.source}
                    {f.checked_at ? (
                      <>
                        {" "}
                        <Time iso={f.checked_at} />
                      </>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{latest || sources ? "No open issues." : "Not checked yet."}</p>
      )}
      <h3>Latest security scan {latestDeep ? <span className="muted small">(<Time iso={latestDeep.checked_at} />, including notes)</span> : null}</h3>
      {latestDeep ? <Findings findings={latestDeep.findings} includeInfo /> : <p className="muted">No security scan in the latest 30 checks.</p>}
      <ServerSide site={full} production={production} onTask={onTask} />
      {user.role === "admin" ? <ServerLink site={full} onChanged={() => { onChanged(); void detail.refresh(); }} /> : null}
      {full.incidents_30d?.length ? (
        <>
          <h3>Downtime in the last 30 days</h3>
          <ul className="list small ws-incidents">
            {full.incidents_30d.map((i) => (
              <li key={i.start}>
                <Time iso={i.start} />, {i.minutes} min{i.end ? "" : " (ongoing)"}: {i.reason}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <h3>History</h3>
      <div className="table-wrap">
        <table className="table compact ws-history">
          <thead>
            <tr>
              <th>When</th>
              <th>Status</th>
              <th>HTTP</th>
              <th>ms</th>
              <th>Type</th>
              <th>Issues</th>
            </tr>
          </thead>
          <tbody>
            {(checks.data?.checks ?? []).map((c) => (
              <tr key={c.id}>
                <td>
                  <Time iso={c.checked_at} />
                </td>
                <td>
                  <Pill value={c.status} />
                </td>
                <td>{c.http_status ?? "—"}</td>
                <td>{c.response_ms ?? "—"}</td>
                <td>{c.details.deep ? "security" : "uptime"}</td>
                <td>{c.findings.filter((f) => f.severity !== "info").length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Modal>
  );
}

export default function Websites() {
  const { canOperate } = useAuth();
  const sites = usePoll(() => api.get<{ sites: SiteView[] }>("/api/sites"), 8_000);
  const schedules = usePoll(() => api.get<{ schedules: Schedule[] }>("/api/schedules"), 60_000);
  const [openId, setOpenId] = useState<number | null>(null);
  const open = sites.data?.sites.find((s) => s.id === openId) ?? null;

  // Summary of the same list the table shows: status of monitored (not paused) sites, the 7-day uptime column and the Issues column.
  const all = sites.data?.sites ?? [];
  const loaded = !!sites.data;
  const monitored = all.filter((s) => s.enabled);
  const count = (status: Site["last_status"]) => monitored.filter((s) => s.last_status === status).length;
  const [up, down, degraded, unchecked] = [count("up"), count("down"), count("degraded"), count("unknown")];
  const paused = all.length - monitored.length;
  // The 7-day average covers monitored sites with enough checks behind their figure; paused sites are left out (C2-07, C2-09).
  const weekly = monitored.map((s) => coverage(s.uptime, "7d"));
  const uptimes = weekly.map((c) => c.pct).filter((v): v is number => v != null);
  const thin = weekly.filter((c) => c.pct == null && (c.checks ?? 0) > 0).length;
  const youngest = weekly.filter((c) => c.pct != null).map((c) => c.since).filter((v): v is string => !!v).sort()[0] ?? null;
  const avgUptime = uptimes.length ? uptimes.reduce((a, b) => a + b, 0) / uptimes.length : null;
  // Open issues of monitored sites, from the latest uptime check and the latest security scan together (C2-10).
  const issues = monitored.flatMap(openIssues);
  const critical = issues.filter((f) => f.severity === "critical").length;
  const sitesWithIssues = monitored.filter((s) => openIssues(s).length).length;
  const bothChecks = monitored.some((s) => s.open_findings_sources);
  const pending = sites.error ?? "…";
  const tone = uptimeTone(avgUptime);
  const scheduleList = schedules.data?.schedules;
  const intro = scheduleList
    ? `Uptime and redirect checks ${runs(cadence(scheduleList, "monitoring", "check_all_sites"), true)} and the security scan ${runs(
        cadence(scheduleList, "wordpress_security", "security_scan_all"),
        false,
      )}, all read-only.`
    : "Uptime, redirect and security checks run on the schedules shown on Workflows, all read-only.";

  return (
    <div className="page ws-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Monitoring</div>
          <h1>Websites</h1>
          <p className="muted">
            {intro} Sites linked to a server also get WP-CLI checks; repairs run only after approval.
          </p>
        </div>
      </header>

      <div className="tiles cols-4">
        <Tile
          icon="globe"
          tone="cyan"
          label="Monitored"
          hint={
            loaded
              ? [all.length > monitored.length ? `${all.length - monitored.length} paused` : "", unchecked ? `${unchecked} not checked yet` : ""]
                  .filter(Boolean)
                  .join(" · ") || (monitored.length ? "None paused" : "No sites yet")
              : pending
          }
        >
          <span className="tile-value tabular">{loaded ? monitored.length : "—"}</span>
          {loaded && all.length > monitored.length ? <span className="tile-of">of {all.length}</span> : null}
        </Tile>
        <Tile
          icon="pulse"
          tone={!loaded || !monitored.length || unchecked === monitored.length ? "muted" : down ? "bad" : degraded ? "warn" : "green"}
          label="Up · down · degraded"
          hint={loaded ? `Latest check of each monitored site${unchecked ? ` · ${unchecked} not checked yet` : ""}${paused ? ` · ${paused} paused, not counted` : ""}` : pending}
        >
          <div className="ws-status tabular">
            <span className="ws-status-item">
              <span className={`ws-status-value ${up ? "text-good" : ""}`}>{loaded ? up : "—"}</span> up
            </span>
            <span className="ws-status-item">
              <span className={`ws-status-value ${down ? "text-bad" : ""}`}>{loaded ? down : "—"}</span> down
            </span>
            <span className="ws-status-item">
              <span className={`ws-status-value ${degraded ? "text-warn" : ""}`}>{loaded ? degraded : "—"}</span> degraded
            </span>
          </div>
        </Tile>
        <Tile
          icon="clock"
          tone={tone === "good" ? "green" : tone}
          label="Average uptime, 7 days"
          hint={
            loaded
              ? [
                  avgUptime == null
                    ? thin
                      ? "Not enough data yet"
                      : "No checks recorded yet"
                    : `Average of ${uptimes.length} monitored site${uptimes.length === 1 ? "" : "s"}${youngest ? ` · checks since ${shortWhen(youngest)}` : ""}`,
                  avgUptime != null && thin ? `${thin} with too few checks left out` : "",
                  paused ? `${paused} paused, not included` : "",
                ]
                  .filter(Boolean)
                  .join(" · ")
              : pending
          }
        >
          <span className="tile-value tabular">{avgUptime == null ? "—" : pctText(avgUptime)}</span>
          {avgUptime != null ? (
            <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Number(avgUptime.toFixed(2))} aria-label="Average uptime, 7 days">
              <span className={`meter-fill tone-${tone}`} style={{ width: `${Math.max(2, avgUptime)}%` }} />
            </div>
          ) : null}
        </Tile>
        <Tile
          icon="alert"
          tone={!loaded || !monitored.length ? "muted" : critical ? "bad" : issues.length ? "warn" : unchecked === monitored.length ? "muted" : "green"}
          label="Open issues"
          hint={
            loaded
              ? (issues.length
                  ? `${critical} critical · on ${sitesWithIssues} site${sitesWithIssues === 1 ? "" : "s"}`
                  : monitored.length
                    ? "None found"
                    : "No monitored sites") + (bothChecks ? " · latest uptime checks and security scans" : " · latest check of each site")
              : pending
          }
        >
          <span className="tile-value tabular">{loaded ? issues.length : "—"}</span>
        </Tile>
      </div>

      {canOperate ? (
        <Card
          title={
            <>
              <Icon name="plug" size={18} className="card-icon" /> Add a site
            </>
          }
        >
          <AddSite onAdded={() => void sites.refresh()} />
        </Card>
      ) : null}
      <Card
        title={
          <>
            <Icon name="globe" size={18} className="card-icon" /> Monitored sites ({sites.data ? sites.data.sites.length : "…"})
          </>
        }
        actions={sites.data?.sites.length ? <span className="muted small">Select a row for details</span> : null}
      >
        <ErrorNote error={sites.error} />
        {sites.data?.sites.length ? (
          <div className="table-wrap">
            <table className="table ws-table">
              <thead>
                <tr>
                  <th>Site</th>
                  <th>Status</th>
                  <th>Uptime 7 d</th>
                  <th>Last check</th>
                  <th>Response</th>
                  <th>WordPress</th>
                  <th>TLS</th>
                  <th>Issues</th>
                </tr>
              </thead>
              <tbody>
                {sites.data.sites.map((s) => {
                  const c = s.last_check;
                  const issues = openIssues(s);
                  const week = coverage(s.uptime, "7d");
                  const from = issueSources(s);
                  return (
                    <tr key={s.id} className={`clickable ${s.enabled ? "" : "ws-paused"}`} onClick={() => setOpenId(s.id)}>
                      <td className="ws-site">
                        {/* The row opens on a click; this button is the keyboard path to the same dialog. */}
                        <button
                          type="button"
                          className="link-button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setOpenId(s.id);
                          }}
                        >
                          <strong>{s.name}</strong>
                        </button>
                        <div className="muted small ws-url" title={s.url}>
                          {s.url}
                        </div>
                      </td>
                      <td className="ws-nowrap">
                        <Pill value={s.enabled ? s.last_status : "disabled"} label={s.enabled ? undefined : "Paused"} />
                        {s.environment && s.environment !== "production" ? (
                          <>
                            {" "}
                            <Pill value={s.environment} />
                          </>
                        ) : null}
                      </td>
                      <td className="ws-uptime tabular">
                        {week.pct != null ? (
                          <>
                            <span title={week.text}>{pctText(week.pct)}</span>
                            {s.enabled ? (
                              <div className="meter thin">
                                <span className={`meter-fill tone-${uptimeTone(week.pct)}`} style={{ width: `${Math.max(2, week.pct)}%` }} />
                              </div>
                            ) : null}
                            <div className="muted small ws-coverage">{week.text}</div>
                          </>
                        ) : (
                          <span className="small ws-nodata">{week.checks ? week.text.replace(/ \(.*\)$/, "") : "—"}</span>
                        )}
                      </td>
                      <td className="ws-nowrap">
                        <Time iso={s.last_checked_at} />
                      </td>
                      <td className="ws-nowrap tabular">{c?.response_ms != null ? `${c.response_ms} ms` : "—"}</td>
                      <td>{c ? (c.is_wordpress ? c.wp_version || "yes" : "no") : "—"}</td>
                      <td className="ws-nowrap tabular">{c?.tls_days_left != null ? `${c.tls_days_left} d` : "—"}</td>
                      <td>
                        {issues.length ? (
                          <span title={`${issues.map((f) => f.title).join("\n")}${from ? `\n(from ${from})` : ""}`}>
                            <Pill value={issues.some((f) => f.severity === "critical") ? "critical" : "warning"} label={String(issues.length)} />
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No sites yet.</Empty>
        )}
      </Card>
      {open ? <SiteDetail site={open} onClose={() => setOpenId(null)} onChanged={() => void sites.refresh()} /> : null}
    </div>
  );
}
