import { FormEvent, useState } from "react";
import { api, type EditionInfo, type ManagedUser, type SettingsPage } from "../api";
import { useAuth } from "../App";
import { Card, ErrorNote, Pill, Time } from "../components";
import { usePoll } from "../hooks";

function BusinessSettings({ page, onSaved }: { page: SettingsPage; onSaved: () => void }) {
  const [draft, setDraft] = useState<Record<string, string | number>>(() =>
    Object.fromEntries(Object.entries(page.settings).map(([k, f]) => [k, f.value])),
  );
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    const changes = Object.fromEntries(Object.entries(draft).filter(([k, v]) => page.settings[k].value !== v));
    try {
      await api.patch("/api/settings", changes);
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <form onSubmit={save} className="settings-form">
      {Object.entries(page.settings).map(([key, field]) => (
        <label key={key}>
          {field.label}
          {field.kind === "choice" ? (
            <select value={String(draft[key])} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
              {field.choices.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          ) : (
            <input
              type={field.kind === "integer" ? "number" : "text"}
              value={draft[key]}
              onChange={(e) => setDraft({ ...draft, [key]: field.kind === "integer" ? Number(e.target.value) : e.target.value })}
            />
          )}
          <span className="muted small">{field.description}</span>
        </label>
      ))}
      <ErrorNote error={error} />
      {saved ? <div className="alert alert-good">Saved.</div> : null}
      <button className="btn primary" type="submit">
        Save settings
      </button>
    </form>
  );
}

function SecretField({ name, info, available, onChanged }: { name: string; info: SettingsPage["secrets"][string]; available: boolean; onChanged: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.put(`/api/settings/secrets/${name}`, { value });
      setValue("");
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function remove() {
    if (!window.confirm("Remove this key? AI features fall back to rule-based behaviour.")) return;
    await api.del(`/api/settings/secrets/${name}`).catch((err) => setError(String(err)));
    onChanged();
  }

  return (
    <div className="secret-field">
      <div>
        <strong>{info.label}</strong>{" "}
        {info.set ? <Pill value="up" label={`set ${info.hint}`} /> : <Pill value="unknown" label="not set" />}
        <div className="muted small">{info.description}</div>
        {info.set ? (
          <div className="muted small">
            updated <Time iso={info.updated_at} /> by {info.updated_by}
          </div>
        ) : null}
      </div>
      {available ? (
        <form className="inline-form secret-form" onSubmit={save}>
          <input
            type="password"
            autoComplete="off"
            aria-label={`New ${info.label}`}
            placeholder={info.set ? "Replace with a new key" : "Paste the key"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button className="btn primary" type="submit" disabled={value.length < 8}>
            Save key
          </button>
          {info.set ? (
            <button type="button" className="btn danger" onClick={() => void remove()}>
              Remove
            </button>
          ) : null}
        </form>
      ) : (
        <p className="text-warn small">CC_SECRETS_KEY is not set on this installation, so keys cannot be stored.</p>
      )}
      <ErrorNote error={error} />
    </div>
  );
}

function EditionPanel({ edition, onChanged }: { edition: EditionInfo; onChanged: () => void }) {
  const [license, setLicense] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function install(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/system/license", { license });
      setLicense("");
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const limit = (n: number | null) => (n === null ? "unlimited" : String(n));
  return (
    <>
      <dl className="kv">
        <dt>Edition</dt>
        <dd>
          <strong>{edition.title}</strong> <span className="muted small">({edition.source === "license" ? "from license" : "from configuration"})</span>
        </dd>
        {edition.license ? (
          <>
            <dt>License</dt>
            <dd>
              {edition.license.license_id} · {edition.license.customer} · expires {edition.license.expires_at ?? "never"}
            </dd>
          </>
        ) : null}
        <dt>Websites</dt>
        <dd>
          {edition.usage.sites} of {limit(edition.limits.max_sites)}
        </dd>
        <dt>Users</dt>
        <dd>
          {edition.usage.users} of {limit(edition.limits.max_users)}
        </dd>
      </dl>
      {edition.notes.map((n) => (
        <p key={n} className="text-warn small">
          {n}
        </p>
      ))}
      <ul className="list small">
        {Object.entries(edition.features).map(([k, f]) => (
          <li key={k}>
            <Pill value={f.enabled ? "up" : "disabled"} label={f.enabled ? "included" : "not included"} /> {f.description}
          </li>
        ))}
      </ul>
      <form onSubmit={install}>
        <label>
          Install a license file
          <textarea className="mono" rows={3} placeholder='{"payload": {...}, "signature": "..."}' value={license} onChange={(e) => setLicense(e.target.value)} />
        </label>
        <ErrorNote error={error} />
        <button className="btn small" type="submit" disabled={!license.trim()}>
          Install license
        </button>
      </form>
    </>
  );
}

function Users() {
  const { user: me } = useAuth();
  const users = usePoll(() => api.get<{ users: ManagedUser[] }>("/api/users"), 0);
  const [form, setForm] = useState({ email: "", display_name: "", role: "operator", password: "" });
  const [error, setError] = useState<string | null>(null);

  async function add(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/users", form);
      setForm({ email: "", display_name: "", role: "operator", password: "" });
      await users.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function patch(u: ManagedUser, body: Partial<ManagedUser>) {
    setError(null);
    try {
      await api.patch(`/api/users/${u.id}`, body);
      await users.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <ErrorNote error={users.error ?? error} />
      <table className="table compact">
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th title="Access to the isolated Credit Repair client data">Sensitive data</th>
            <th>Last sign-in</th>
            <th>State</th>
          </tr>
        </thead>
        <tbody>
          {(users.data?.users ?? []).map((u) => (
            <tr key={u.id}>
              <td>
                {u.email}
                {u.id === me.id ? <span className="muted small"> (you)</span> : null}
              </td>
              <td>
                <select aria-label={`Role for ${u.email}`} value={u.role} onChange={(e) => void patch(u, { role: e.target.value as ManagedUser["role"] })}>
                  <option value="admin">admin</option>
                  <option value="operator">operator</option>
                  <option value="viewer">viewer</option>
                </select>
              </td>
              <td>
                <label className="check">
                  <input
                    type="checkbox"
                    aria-label={`Sensitive data access for ${u.email}`}
                    checked={u.can_access_sensitive}
                    onChange={(e) => void patch(u, { can_access_sensitive: e.target.checked })}
                  />{" "}
                  {u.can_access_sensitive ? "allowed" : "no"}
                </label>
              </td>
              <td>
                <Time iso={u.last_login_at} />
              </td>
              <td>
                {u.id === me.id ? (
                  <Pill value="up" label="active" />
                ) : (
                  <button className="btn small" onClick={() => void patch(u, { is_active: !u.is_active })}>
                    {u.is_active ? "Deactivate" : "Reactivate"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="inline-form users-form" onSubmit={add}>
        <input aria-label="New user email" type="email" placeholder="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        <select aria-label="New user role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
          <option value="operator">operator</option>
          <option value="viewer">viewer</option>
          <option value="admin">admin</option>
        </select>
        <input
          aria-label="New user password"
          type="password"
          autoComplete="new-password"
          placeholder="initial password (12+ characters)"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          required
          minLength={12}
        />
        <button className="btn primary" type="submit">
          Add user
        </button>
      </form>
    </>
  );
}

export default function Settings() {
  const { user } = useAuth();
  const page = usePoll(() => api.get<SettingsPage>("/api/settings"), 0);
  const edition = usePoll(() => api.get<EditionInfo>("/api/system/edition"), 0);
  if (user.role !== "admin") {
    return (
      <div className="page">
        <h1>Settings</h1>
        <p className="muted">Only admins can change settings.</p>
      </div>
    );
  }
  const refresh = () => {
    void page.refresh();
    void edition.refresh();
  };
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">
            Business settings, your AI provider and key, edition and license, and who can sign in. Installation settings (database, ports, deployment
            secrets) live in the server's .env file and are not shown here.
          </p>
        </div>
      </header>
      <ErrorNote error={page.error ?? edition.error} />
      <div className="grid-2">
        <Card title="Business">{page.data ? <BusinessSettings key={JSON.stringify(page.data.settings)} page={page.data} onSaved={refresh} /> : null}</Card>
        <div>
          <Card title="AI provider">
            {page.data ? (
              <>
                <p>
                  Now using:{" "}
                  {page.data.ai.provider === "anthropic" ? (
                    <Pill value="live" label={`Claude · ${page.data.ai.model}`} />
                  ) : (
                    <Pill value="info" label="rule-based planner" />
                  )}{" "}
                  <span className="muted small">
                    {page.data.ai.reason || `key from ${page.data.ai.key_source} settings`} · cap {page.data.ai.daily_cap}/day
                  </span>
                </p>
                {Object.entries(page.data.secrets).map(([name, info]) => (
                  <SecretField key={name} name={name} info={info} available={page.data!.secrets_available} onChanged={refresh} />
                ))}
              </>
            ) : null}
          </Card>
          <Card title="Edition and license">{edition.data ? <EditionPanel edition={edition.data} onChanged={refresh} /> : null}</Card>
        </div>
      </div>
      <Card title="Users">
        <Users />
      </Card>
    </div>
  );
}
