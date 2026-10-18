import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Server, type Site, type SiteCheck, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Findings, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";

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

function pct(v: number | null | undefined) {
  return v == null ? "—" : `${v}%`;
}

function ServerLink({ site, onChanged }: { site: Site; onChanged: () => void }) {
  const servers = usePoll(() => api.get<{ servers: Server[] }>("/api/servers"), 0);
  const [serverId, setServerId] = useState(site.server_id ? String(site.server_id) : "");
  const [key, setKey] = useState(site.server_site_key);
  const [error, setError] = useState<string | null>(null);
  const chosen = servers.data?.servers.find((s) => String(s.id) === serverId);
  const keys = Object.keys(chosen?.last_hello.sites ?? {});

  async function save(body: Record<string, unknown>) {
    setError(null);
    try {
      await api.patch(`/api/sites/${site.id}`, body);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="boxed-section">
      <h3>Server link and environment (admin)</h3>
      <div className="grid-form">
        <select aria-label="Site environment setting" value={site.environment} onChange={(e) => void save({ environment: e.target.value })}>
          <option value="production">production</option>
          <option value="staging">staging</option>
          <option value="test">test</option>
        </select>
        <select aria-label="Server" value={serverId} onChange={(e) => setServerId(e.target.value)}>
          <option value="">no server</option>
          {(servers.data?.servers ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <input aria-label="Site key on the server" placeholder="site key in cc-gate config" value={key} onChange={(e) => setKey(e.target.value)} list="site-keys" />
        <datalist id="site-keys">
          {keys.map((k) => (
            <option key={k} value={k} />
          ))}
        </datalist>
        <button
          className="btn small"
          onClick={() => void save(serverId ? { server_id: Number(serverId), server_site_key: key } : { unlink_server: true })}
        >
          Save link
        </button>
      </div>
      <ErrorNote error={error ?? servers.error} />
    </div>
  );
}

function ServerSide({ site, onTask }: { site: Site; onTask: (t: Task) => void }) {
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
            ) : (
              <strong>{`${d.site_urls.home} / ${d.site_urls.siteurl} (expected ${d.site_urls.expected}${
                d.site_urls.expected_siteurl && d.site_urls.expected_siteurl !== d.site_urls.expected ? ` / ${d.site_urls.expected_siteurl}` : ""
              })`}</strong>
            )}
          </dd>
          <dt>Plugins</dt>
          <dd>
            {(d.plugins ?? []).map((p) => `${p.name} ${p.version}${p.status !== "active" ? ` (${p.status})` : ""}${p.update === "available" ? ` → ${p.update_version}` : ""}`).join(", ") || "none"}
          </dd>
          <dt>Administrators</dt>
          <dd>
            {(d.admins ?? []).map((a) => a.user_login).join(", ") || "—"}
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
        </dl>
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
            Each repair waits for approval{site.environment === "production" ? " by an admin who types the site's name (production)" : ""}, backs up the database
            first, changes one thing and verifies the site afterwards. Repairs only run if the server allows changes.
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

function SiteDetail({ site, onClose, onChanged }: { site: Site; onClose: () => void; onChanged: () => void }) {
  const { user, canOperate } = useAuth();
  const detail = usePoll(() => api.get<{ site: Site }>(`/api/sites/${site.id}`), 10_000, [site.id]);
  const checks = usePoll(() => api.get<{ checks: SiteCheck[] }>(`/api/sites/${site.id}/checks?limit=30`), 5_000, [site.id]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const full = detail.data?.site ?? site;
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
      <p>
        <a href={site.url} target="_blank" rel="noreferrer noopener">
          {site.url}
        </a>{" "}
        <Pill value={site.last_status} /> <Pill value={full.environment} /> {site.client ? <span className="muted">· {site.client}</span> : null}
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
        <dt>Uptime</dt>
        <dd>
          24 h {pct(full.uptime?.["24h"])} · 7 days {pct(full.uptime?.["7d"])} · 30 days {pct(full.uptime?.["30d"])}
          {full.uptime?.avg_response_ms_24h != null ? ` · avg ${full.uptime.avg_response_ms_24h} ms` : ""}
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
      <h3>Security and health findings {latestDeep ? <span className="muted small">(security scan <Time iso={latestDeep.checked_at} />)</span> : null}</h3>
      {latestDeep ? <Findings findings={latestDeep.findings} includeInfo /> : <p className="muted">No security scan yet.</p>}
      <ServerSide site={full} onTask={onTask} />
      {user.role === "admin" ? <ServerLink site={full} onChanged={() => { onChanged(); void detail.refresh(); }} /> : null}
      {full.incidents_30d?.length ? (
        <>
          <h3>Downtime in the last 30 days</h3>
          <ul className="list small">
            {full.incidents_30d.map((i) => (
              <li key={i.start}>
                <Time iso={i.start} />, {i.minutes} min{i.end ? "" : " (ongoing)"}: {i.reason}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <h3>History</h3>
      <table className="table compact">
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
    </Modal>
  );
}

export default function Websites() {
  const { canOperate } = useAuth();
  const sites = usePoll(() => api.get<{ sites: Site[] }>("/api/sites"), 8_000);
  const [openId, setOpenId] = useState<number | null>(null);
  const open = sites.data?.sites.find((s) => s.id === openId) ?? null;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Websites</h1>
          <p className="muted">
            Uptime and redirect checks every 15 minutes and a nightly security scan, all read-only. Sites linked to a server also get WP-CLI checks; repairs
            run only after approval.
          </p>
        </div>
      </header>
      {canOperate ? (
        <Card title="Add a site">
          <AddSite onAdded={() => void sites.refresh()} />
        </Card>
      ) : null}
      <Card title={`Monitored sites (${sites.data?.sites.length ?? 0})`}>
        <ErrorNote error={sites.error} />
        {sites.data?.sites.length ? (
          <table className="table">
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
                const issues = c ? c.findings.filter((f) => f.severity !== "info") : [];
                return (
                  <tr key={s.id} className="clickable" onClick={() => setOpenId(s.id)}>
                    <td>
                      <strong>{s.name}</strong>
                      <div className="muted small">{s.url}</div>
                    </td>
                    <td>
                      <Pill value={s.enabled ? s.last_status : "disabled"} label={s.enabled ? undefined : "paused"} />
                      {s.environment && s.environment !== "production" ? (
                        <>
                          {" "}
                          <Pill value={s.environment} />
                        </>
                      ) : null}
                    </td>
                    <td>{pct(s.uptime?.["7d"])}</td>
                    <td>
                      <Time iso={s.last_checked_at} />
                    </td>
                    <td>{c?.response_ms != null ? `${c.response_ms} ms` : "—"}</td>
                    <td>{c ? (c.is_wordpress ? c.wp_version || "yes" : "no") : "—"}</td>
                    <td>{c?.tls_days_left != null ? `${c.tls_days_left} d` : "—"}</td>
                    <td>
                      {issues.length ? (
                        <Pill value={issues.some((f) => f.severity === "critical") ? "critical" : "warning"} label={String(issues.length)} />
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <Empty>No sites yet.</Empty>
        )}
      </Card>
      {open ? <SiteDetail site={open} onClose={() => setOpenId(null)} onChanged={() => void sites.refresh()} /> : null}
    </div>
  );
}
