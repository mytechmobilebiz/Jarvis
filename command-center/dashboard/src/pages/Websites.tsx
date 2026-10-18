import { FormEvent, useState } from "react";
import { api, type Site, type SiteCheck } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Findings, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";

function AddSite({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [client, setClient] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/sites", { name, url, client });
      setName("");
      setUrl("");
      setClient("");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <form className="inline-form" onSubmit={submit}>
      <input aria-label="Site name" placeholder="Name, e.g. Acme Plumbing" value={name} onChange={(e) => setName(e.target.value)} required />
      <input aria-label="Site URL" placeholder="URL or domain" value={url} onChange={(e) => setUrl(e.target.value)} required />
      <input aria-label="Client" placeholder="Client (optional)" value={client} onChange={(e) => setClient(e.target.value)} />
      <button className="btn primary" type="submit">
        Add site
      </button>
      <ErrorNote error={error} />
    </form>
  );
}

function SiteDetail({ site, onClose, onChanged }: { site: Site; onClose: () => void; onChanged: () => void }) {
  const { canOperate } = useAuth();
  const checks = usePoll(() => api.get<{ checks: SiteCheck[] }>(`/api/sites/${site.id}/checks?limit=30`), 5_000, [site.id]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const latestDeep = checks.data?.checks.find((c) => c.details.deep);
  const latest = checks.data?.checks[0] ?? site.last_check;

  async function act(fn: () => Promise<unknown>, done: string) {
    setError(null);
    try {
      await fn();
      setMessage(done);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Modal title={site.name} onClose={onClose} wide>
      <p>
        <a href={site.url} target="_blank" rel="noreferrer noopener">
          {site.url}
        </a>{" "}
        <Pill value={site.last_status} /> {site.client ? <span className="muted">· {site.client}</span> : null}
      </p>
      {canOperate ? (
        <div className="row-actions">
          <button className="btn primary small" onClick={() => void act(() => api.post(`/api/sites/${site.id}/check`, { deep: true }), "Full check queued.")}>
            Run full check
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
      {message ? <div className="alert alert-good">{message}</div> : null}
      <ErrorNote error={error ?? checks.error} />
      {latest ? (
        <dl className="kv">
          <dt>Last check</dt>
          <dd>
            <Time iso={latest.checked_at} /> · HTTP {latest.http_status ?? "—"} · {latest.response_ms ?? "—"} ms
          </dd>
          <dt>WordPress</dt>
          <dd>{latest.is_wordpress ? `yes, ${latest.wp_version || "version hidden"}` : "not detected"}</dd>
          <dt>TLS certificate</dt>
          <dd>{latest.tls_days_left != null ? `${latest.tls_days_left} days left (${latest.tls_issuer || "issuer unknown"})` : "—"}</dd>
          {latest.details.plugins_detected?.length ? (
            <>
              <dt>Plugins seen</dt>
              <dd>{latest.details.plugins_detected.join(", ")}</dd>
            </>
          ) : null}
        </dl>
      ) : (
        <p className="muted">Not checked yet.</p>
      )}
      <h3>Security and health findings {latestDeep ? <span className="muted small">(full check <Time iso={latestDeep.checked_at} />)</span> : null}</h3>
      {latestDeep ? <Findings findings={latestDeep.findings} includeInfo /> : <p className="muted">No full check yet. Run one to see exposure findings.</p>}
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
              <td>{c.details.deep ? "full" : "uptime"}</td>
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
            Read-only monitoring by the WordPress Monitor: uptime every 15 minutes, a full exposure scan nightly. It never logs in to or changes a site.
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
                    </td>
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
