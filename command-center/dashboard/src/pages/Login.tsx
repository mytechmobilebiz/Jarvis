import { FormEvent, useState } from "react";
import { api, type User } from "../api";
import { ErrorNote } from "../components";
import { Icon } from "../icons";
import "./Login.css";

export default function Login({ onSignedIn }: { onSignedIn: (u: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ user: User }>("/api/auth/login", { email, password });
      onSignedIn(r.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page lo-page">
      <div className="lo-backdrop" aria-hidden />
      <form className="login-card lo-card" onSubmit={submit}>
        <div className="lo-brand">
          <span className="logo lo-logo" aria-hidden>
            ∞
          </span>
          <h1 className="lo-title">
            <span className="lo-title-main">Infinite AI</span> <span className="lo-title-sub">Command Center</span>
          </h1>
          <p className="muted lo-lead">Your AI agents, websites and servers in one place.</p>
        </div>
        <label>
          Email
          <span className="lo-field">
            <Icon name="mail" size={17} className="lo-field-icon" />
            <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          </span>
        </label>
        <label>
          Password
          <span className="lo-field">
            <Icon name="lock" size={17} className="lo-field-icon" />
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </span>
        </label>
        <ErrorNote error={error} />
        <button className="btn primary lo-submit" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted small lo-foot">
          <Icon name="shield" size={14} /> Private system of Infinite Marketing. Access is logged.
        </p>
      </form>
    </div>
  );
}
