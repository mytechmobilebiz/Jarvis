import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Server, type ServerCommand, type SshKey, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { confirmReason, TypedConfirm } from "./shared";
import "./Servers.css";

/** POST /api/servers/{id}/host-key/scan: matches_pinned and pinned_fingerprint are null when no key is pinned (0.5.0 API). */
interface ScanResult {
  fingerprint: string;
  host_key: string;
  matches_pinned?: boolean | null;
  pinned_fingerprint?: string | null;
}

/** A trust request the API refused until the server's name is typed (a different key than the one trusted before). */
interface PendingTrust {
  body: { fingerprint?: string; host_key?: string };
  reason: string;
}

async function runTask(capability: string, params: Record<string, unknown>) {
  const r = await api.post<{ task: Task }>("/api/agents/server_management/tasks", { capability, params });
  return r.task;
}

function KeyPanel() {
  const key = usePoll(() => api.get<{ key: SshKey | null }>("/api/ssh/key"), 0);
  const [error, setError] = useState<string | null>(null);

  async function create(replace: boolean) {
    if (replace && !window.confirm("Replace the SSH key? Every server loses access until it gets the new key (run install-gate.sh again).")) return;
    setError(null);
    try {
      await api.post("/api/ssh/key", { replace });
      await key.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const k = key.data?.key;
  return (
    <Card
      className="sv-key"
      title={
        <>
          <Icon name="lock" size={18} className="card-icon" /> This installation's SSH key
        </>
      }
    >
      <ErrorNote error={key.error ?? error} />
      {k ? (
        <>
          <p className="small muted sv-wrap">
            Fingerprint <code>{k.fingerprint}</code>, created by {k.created_by} <Time iso={k.created_at} />. The private half is stored encrypted and is never
            shown, sent to an AI model or written to logs.
          </p>
          <p className="small">Line for the restricted account's authorized_keys (install-gate.sh writes it for you):</p>
          <pre className="plain boxed small sv-keyline" aria-label="authorized_keys line">
            {k.authorized_keys_line}
          </pre>
          <button className="btn small" onClick={() => void create(true)}>
            <Icon name="refresh" size={14} /> Replace key…
          </button>
        </>
      ) : (
        <>
          <p className="muted small">No key yet. Create one, then install it on each server with install-gate.sh.</p>
          <button className="btn primary" onClick={() => void create(false)}>
            <Icon name="lock" size={16} /> Create SSH key
          </button>
        </>
      )}
    </Card>
  );
}

function AddServer({ onAdded }: { onAdded: () => void }) {
  const [form, setForm] = useState({ name: "", host: "", port: "22", username: "ccagent", environment: "production" });
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/servers", { ...form, port: Number(form.port) });
      setForm({ name: "", host: "", port: "22", username: "ccagent", environment: "production" });
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <form className="grid-form sv-add" onSubmit={submit}>
      <input aria-label="Server name" placeholder="name, e.g. cwp-1" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
      <input aria-label="Host" placeholder="host name or IP" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} required />
      <input aria-label="SSH port" placeholder="port" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} inputMode="numeric" />
      <input aria-label="Account" placeholder="account (never root)" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
      <select aria-label="Environment" value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
        <option value="production">production</option>
        <option value="staging">staging</option>
        <option value="test">test</option>
      </select>
      <button className="btn primary" type="submit">
        Add server
      </button>
      <ErrorNote error={error} />
    </form>
  );
}

function HostKey({ server, onChanged }: { server: Server; onChanged: () => void }) {
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [pasted, setPasted] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingTrust | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  // The scan found a different key than the pinned one: another machine may be answering at this address (C3-05).
  const changed = !!scan && server.host_key_pinned && scan.matches_pinned === false;
  const same = !!scan && scan.matches_pinned === true;

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  /** Posts the key to trust; when the API asks for the server's name (a replaced key), asks the admin to type it. */
  async function trust(body: PendingTrust["body"], confirm?: string) {
    setError(null);
    setConfirmError(null);
    try {
      await api.post(`/api/servers/${server.id}/host-key/trust`, confirm ? { ...body, confirm } : body);
      setScan(null);
      setPasted("");
      setPending(null);
      onChanged();
    } catch (err) {
      const reason = confirmReason(err);
      if (reason && !confirm) setPending({ body, reason });
      else if (confirm) setConfirmError(err instanceof Error ? err.message : String(err));
      else setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="boxed-section">
      <h3>Host key</h3>
      {server.host_key_pinned ? (
        <p className="small sv-wrap">
          Pinned: <code>{server.host_key_fingerprint}</code> by {server.host_key_trusted_by} <Time iso={server.host_key_trusted_at} />. The Command Center refuses
          to connect if the server ever presents a different key.
        </p>
      ) : (
        <p className="small muted">Not pinned: no command can run on this server until its host key is pinned.</p>
      )}
      <div className="row-actions">
        <button className="btn small" onClick={() => void act(async () => setScan(await api.post<ScanResult>(`/api/servers/${server.id}/host-key/scan`)))}>
          Scan host key
        </button>
        {server.host_key_pinned ? (
          <button className="btn small" onClick={() => void act(() => api.del(`/api/servers/${server.id}/host-key`))}>
            Unpin
          </button>
        ) : null}
      </div>
      {changed && scan ? (
        <div className="alert alert-bad sv-wrap sv-changed" role="alert">
          <p className="sv-changed-title">
            <Icon name="alert" size={18} /> <strong>Host key CHANGED</strong>
          </p>
          <p className="small">
            The server at {server.host}:{server.port} now presents <code>{scan.fingerprint}</code>, not the pinned <code>{scan.pinned_fingerprint ?? server.host_key_fingerprint}</code>.
            A different machine may be answering at this address. Replace the pinned key only if the server was reinstalled or its key was changed on purpose,
            after comparing the new fingerprint on the server itself (<code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>). Approvals given before
            for this server and its sites stop applying.
          </p>
          <button
            className="btn danger small"
            onClick={() =>
              setPending({
                body: { fingerprint: scan.fingerprint },
                reason: `host key CHANGED: ${server.name} now presents ${scan.fingerprint} instead of the pinned ${scan.pinned_fingerprint ?? server.host_key_fingerprint}. Approvals given before for this server and its sites stop applying.`,
              })
            }
          >
            Replace the pinned key…
          </button>
        </div>
      ) : same && scan ? (
        <div className="alert alert-good sv-wrap" role="status">
          <p className="small">
            The server presents the pinned key <code>{scan.fingerprint}</code>. Nothing to change.
          </p>
        </div>
      ) : scan ? (
        <div className="alert alert-info sv-wrap">
          <p className="small">
            The server presents <code>{scan.fingerprint}</code>. Compare it with the fingerprint install-gate.sh printed on the server (or run{" "}
            <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code> there) before trusting it.
          </p>
          <button className="btn primary small" onClick={() => void trust({ fingerprint: scan.fingerprint })}>
            The fingerprints match: trust this key
          </button>
        </div>
      ) : null}
      <details>
        <summary className="small">Or paste the server's public host key</summary>
        <div className="inline-form">
          <input aria-label="Public host key" placeholder="ssh-ed25519 AAAA…" value={pasted} onChange={(e) => setPasted(e.target.value)} />
          <button className="btn small" disabled={!pasted.trim()} onClick={() => void trust({ host_key: pasted })}>
            Pin pasted key
          </button>
        </div>
      </details>
      <ErrorNote error={error} />
      {pending ? (
        <TypedConfirm
          title={`Replace the host key of ${server.name}`}
          name={server.name}
          action="Replace the pinned key"
          error={confirmError}
          onCancel={() => {
            setPending(null);
            setConfirmError(null);
          }}
          onConfirm={(typed) => trust(pending.body, typed)}
        >
          {pending.reason.charAt(0).toUpperCase() + pending.reason.slice(1)}
        </TypedConfirm>
      ) : null}
    </div>
  );
}

/** Changes the server's environment; taking it out of production asks for its name first (C3-06). */
function EnvironmentSelect({ server, onChanged, onError }: { server: Server; onChanged: () => void; onError: (e: string | null) => void }) {
  const [pending, setPending] = useState<{ environment: string; reason: string } | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  async function save(environment: string, confirm?: string) {
    onError(null);
    setConfirmError(null);
    try {
      await api.patch(`/api/servers/${server.id}`, confirm ? { environment, confirm } : { environment });
      setPending(null);
      onChanged();
    } catch (err) {
      const reason = confirmReason(err);
      if (reason && !confirm) setPending({ environment, reason });
      else if (confirm) setConfirmError(err instanceof Error ? err.message : String(err));
      else onError(err instanceof Error ? err.message : String(err));
    }
  }

  function choose(environment: string) {
    if (environment === server.environment) return;
    if (server.environment === "production")
      setPending({
        environment,
        reason: `this takes ${server.name} out of production: changes on it and on its sites would no longer need an admin's typed confirmation.`,
      });
    else void save(environment);
  }

  return (
    <>
      <select aria-label="Server environment" value={server.environment} onChange={(e) => choose(e.target.value)}>
        <option value="production">production</option>
        <option value="staging">staging</option>
        <option value="test">test</option>
      </select>
      {pending ? (
        <TypedConfirm
          title={`Take ${server.name} out of production`}
          name={server.name}
          action={`Change to ${pending.environment}`}
          error={confirmError}
          onCancel={() => {
            setPending(null);
            setConfirmError(null);
          }}
          onConfirm={(typed) => save(pending.environment, typed)}
        >
          {pending.reason.charAt(0).toUpperCase() + pending.reason.slice(1)}
        </TypedConfirm>
      ) : null}
    </>
  );
}

function ServerDetail({ server, onClose, onChanged }: { server: Server; onClose: () => void; onChanged: () => void }) {
  const { user, canOperate } = useAuth();
  const isAdmin = user.role === "admin";
  const commands = usePoll(() => api.get<{ commands: ServerCommand[]; total: number }>(`/api/servers/${server.id}/commands?limit=50`), 5_000, [server.id]);
  const [service, setService] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hello = server.last_hello;

  async function act(fn: () => Promise<Task | unknown>) {
    setError(null);
    setNote(null);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "id" in r && "status" in r) {
        const t = r as Task;
        setNote(t.status === "awaiting_approval" ? `Task #${t.id} is waiting for approval.` : `Task #${t.id} queued.`);
      }
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Modal title={`${server.name} (${server.username}@${server.host}:${server.port})`} onClose={onClose} wide>
      <p className="sv-detail-head">
        <Pill value={server.environment} /> {server.enabled ? null : <Pill value="disabled" />}{" "}
        {hello.gate_version ? (
          <span className="small muted">
            cc-gate {hello.gate_version} as {hello.user} (uid {hello.uid}) on {hello.hostname}; changes {hello.allow_changes ? "allowed" : "switched off"} on the
            server; last contact <Time iso={server.last_seen_at} />
          </span>
        ) : (
          <span className="small muted">Never connected.</span>
        )}
      </p>
      <ErrorNote error={error} />
      {note ? <div className="alert alert-good">{note}</div> : null}
      {isAdmin ? <HostKey server={server} onChanged={onChanged} /> : null}
      {canOperate ? (
        <div className="row-actions">
          <button className="btn primary small" onClick={() => void act(() => runTask("test_connection", { server_id: server.id }))}>
            Test connection
          </button>
          <button className="btn small" onClick={() => void act(() => runTask("server_overview", { server_id: server.id }))}>
            Overview (disk, memory, load)
          </button>
          <button className="btn small" onClick={() => void act(() => runTask("check_services", { server_id: server.id }))}>
            Check services
          </button>
          {hello.restartable_services?.length ? (
            <>
              <select aria-label="Service to restart" value={service} onChange={(e) => setService(e.target.value)}>
                <option value="">restart a service…</option>
                {hello.restartable_services.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
              <button className="btn small" disabled={!service} onClick={() => void act(() => runTask("restart_service", { server_id: server.id, service }))}>
                Restart (needs approval)
              </button>
            </>
          ) : null}
        </div>
      ) : null}
      {isAdmin ? (
        <div className="row-actions">
          <EnvironmentSelect server={server} onChanged={onChanged} onError={setError} />
          <button className="btn small" onClick={() => void act(() => api.patch(`/api/servers/${server.id}`, { enabled: !server.enabled }))}>
            {server.enabled ? "Disable" : "Enable"}
          </button>
        </div>
      ) : null}
      <h3>Sites on this server</h3>
      {server.sites.length ? (
        <ul className="list small sv-sites">
          {server.sites.map((s) => (
            <li key={s.id}>
              {s.name} (site key <code>{s.server_site_key}</code>, {s.environment})
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">None linked. Link a site on the Websites page (admin).</p>
      )}
      <h3>Command audit trail ({commands.data ? commands.data.total : "…"})</h3>
      {commands.data?.commands.length ? (
        <div className="table-wrap">
          <table className="table compact sv-audit">
            <thead>
              <tr>
                <th>When</th>
                <th>Operation</th>
                <th>Kind</th>
                <th>Result</th>
                <th>Requested / approved</th>
                <th>Task</th>
              </tr>
            </thead>
            <tbody>
              {commands.data.commands.map((c) => (
                <tr key={c.id}>
                  <td className="sv-nowrap">
                    <Time iso={c.started_at} />
                  </td>
                  <td className="sv-op">
                    <code>
                      {c.op} {Object.values(c.args).join(" ")}
                    </code>
                  </td>
                  <td>{c.kind === "change" ? <Pill value="warning" label="change" /> : "read"}</td>
                  <td>
                    {c.ok ? <Pill value="succeeded" label="ok" /> : c.refused ? <Pill value="rejected" label="refused" /> : c.ok === false ? <Pill value="failed" /> : "…"}
                    {c.error ? <div className="small muted truncate" title={c.error}>{c.error}</div> : null}
                  </td>
                  <td className="small sv-who">
                    {c.requested_by}
                    {c.approved_by ? ` / ${c.approved_by}` : ""}
                  </td>
                  <td>{c.task_id ? <Link to={`/tasks?id=${c.task_id}`}>#{c.task_id}</Link> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted small">No commands yet.</p>
      )}
    </Modal>
  );
}

export default function Servers() {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const servers = usePoll(() => api.get<{ servers: Server[] }>("/api/servers"), 8_000);
  const [openId, setOpenId] = useState<number | null>(null);
  const open = servers.data?.servers.find((s) => s.id === openId) ?? null;

  // Summary of the same list the table shows.
  const all = servers.data?.servers ?? [];
  const loaded = !!servers.data;
  const pending = servers.error ?? "…";
  const enabled = all.filter((s) => s.enabled).length;
  const pinned = all.filter((s) => s.host_key_pinned).length;
  const environments = (["production", "staging", "test"] as const)
    .map((env) => [env, all.filter((s) => s.environment === env).length] as const)
    .filter(([, n]) => n > 0)
    .map(([env, n]) => `${n} ${env}`)
    .join(" · ");
  const latest = all.reduce<Server | null>((best, s) => (s.last_seen_at && (!best?.last_seen_at || s.last_seen_at > best.last_seen_at) ? s : best), null);

  return (
    <div className="page sv-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Infrastructure</div>
          <h1>CWP / Servers</h1>
          <p className="muted">
            Servers the VPS & Server Management and WordPress Security agents reach over SSH. They log in as a restricted, non-root account whose key can
            only run <code>cc-gate</code>: a short list of allowlisted operations. Every command is recorded; changes need approval, and production changes
            need an admin who types the target's name.
          </p>
        </div>
      </header>

      <div className="tiles cols-4">
        <Tile icon="server" tone="blue" label="Servers" hint={loaded ? environments || "No servers yet" : pending}>
          <span className="tile-value tabular">{loaded ? all.length : "—"}</span>
        </Tile>
        <Tile
          icon="check"
          tone={!loaded || !all.length ? "muted" : enabled === all.length ? "green" : "warn"}
          label="Enabled"
          hint={loaded ? (all.length === enabled ? (all.length ? "All servers enabled" : "Nothing to enable yet") : `${all.length - enabled} disabled: no connections are made`) : pending}
        >
          <span className="tile-value tabular">{loaded ? enabled : "—"}</span>
          {loaded && all.length ? <span className="tile-of">of {all.length}</span> : null}
        </Tile>
        <Tile
          icon="lock"
          tone={!loaded || !all.length ? "muted" : pinned === all.length ? "purple" : "warn"}
          label="Host keys pinned"
          hint={
            loaded
              ? pinned === all.length
                ? all.length
                  ? "Connections to a different key are refused"
                  : "Pin each server's key after adding it"
                : `${all.length - pinned} without a pinned key: no command can run there`
              : pending
          }
        >
          <span className="tile-value tabular">{loaded ? pinned : "—"}</span>
          {loaded && all.length ? <span className="tile-of">of {all.length}</span> : null}
        </Tile>
        <Tile
          icon="clock"
          tone={latest ? "cyan" : "muted"}
          label="Last contact"
          hint={
            loaded
              ? latest
                ? `${latest.name}${latest.last_hello.gate_version ? `, cc-gate ${latest.last_hello.gate_version}` : ""}`
                : "No server has answered yet"
              : pending
          }
        >
          <span className="tile-value sv-tile-time">{loaded ? latest ? <Time iso={latest.last_seen_at} /> : "Never" : "—"}</span>
        </Tile>
      </div>

      {isAdmin ? (
        <>
          <Card
            className="sv-setup"
            title={
              <>
                <Icon name="book" size={18} className="card-icon" /> Setting up a server (once per server, by you, on the server)
              </>
            }
          >
            <ol className="small sv-steps">
              <li>Create the SSH key below.</li>
              <li>
                Copy the <code>server</code> folder of this package to the server and run, as root:{" "}
                <code>sh install-gate.sh --public-key "&lt;the ssh-ed25519 line below&gt;"</code>
              </li>
              <li>
                Edit <code>/etc/cc-gate/config.json</code> (sites, services; <code>allow_changes</code> stays false until you want repairs), then run{" "}
                <code>sh install-gate.sh --sync-sites</code>.
              </li>
              <li>Add the server here, scan its host key, compare the fingerprint, trust it, and test the connection.</li>
            </ol>
            <p className="small muted">Full guide: docs/SERVER_SETUP.md. Nothing here is installed on a server automatically.</p>
          </Card>
          <KeyPanel />
          <Card
            title={
              <>
                <Icon name="plug" size={18} className="card-icon" /> Add a server
              </>
            }
          >
            <AddServer onAdded={() => void servers.refresh()} />
          </Card>
        </>
      ) : null}
      <Card
        title={
          <>
            <Icon name="server" size={18} className="card-icon" /> Servers ({servers.data ? servers.data.servers.length : "…"})
          </>
        }
        actions={servers.data?.servers.length ? <span className="muted small">Select a row for details</span> : null}
      >
        <ErrorNote error={servers.error} />
        {servers.data?.servers.length ? (
          <div className="table-wrap">
            <table className="table sv-table">
              <thead>
                <tr>
                  <th>Server</th>
                  <th>Environment</th>
                  <th>Host key</th>
                  <th>Last contact</th>
                  <th>Sites</th>
                </tr>
              </thead>
              <tbody>
                {servers.data.servers.map((s) => (
                  <tr key={s.id} className="clickable" onClick={() => setOpenId(s.id)}>
                    <td>
                      <div className="sv-server">
                        <span className="sv-server-icon" aria-hidden>
                          <Icon name="server" size={16} />
                        </span>
                        <div className="sv-server-main">
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
                          <div className="muted small sv-addr" title={`${s.username}@${s.host}:${s.port}`}>
                            {s.username}@{s.host}:{s.port}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="sv-nowrap">
                      <Pill value={s.environment} /> {s.enabled ? null : <Pill value="disabled" />}
                    </td>
                    <td className="sv-nowrap">{s.host_key_pinned ? <Pill value="succeeded" label="pinned" /> : <Pill value="warning" label="not pinned" />}</td>
                    <td className="sv-nowrap">
                      <Time iso={s.last_seen_at} />
                    </td>
                    <td className="tabular">{s.sites.length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No servers yet.</Empty>
        )}
      </Card>
      {open ? <ServerDetail server={open} onClose={() => setOpenId(null)} onChanged={() => void servers.refresh()} /> : null}
    </div>
  );
}
