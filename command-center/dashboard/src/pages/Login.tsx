import { FormEvent, useState } from "react";
import { api, type User } from "../api";
import { ErrorNote } from "../components";

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
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="brand brand-lg">
          <span className="logo" aria-hidden>
            ∞
          </span>
          <div>
            <div className="brand-name">Infinite AI</div>
            <div className="brand-sub">Command Center</div>
          </div>
        </div>
        <label>
          Email
          <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <ErrorNote error={error} />
        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted small">Private system of Infinite Marketing. Access is logged.</p>
      </form>
    </div>
  );
}
