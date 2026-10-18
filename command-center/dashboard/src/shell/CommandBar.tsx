import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type SupervisorRequest, type Task } from "../api";
import { useAuth } from "../App";
import { ErrorNote, Pill } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { speechSupported, useSpeech } from "./useSpeech";

const CONSENT_KEY = "cc.voice.consent";
const TERMINAL = new Set(["completed", "failed", "awaiting_approval"]);
const SUGGESTIONS = ["Check all websites", "Run a security scan on every site", "Give me the daily briefing", "Is the system healthy?"];

function readConsent(): boolean {
  try {
    return localStorage.getItem(CONSENT_KEY) === "yes";
  } catch {
    return false;
  }
}

function saveConsent() {
  try {
    localStorage.setItem(CONSENT_KEY, "yes");
  } catch {
    /* private window: ask again next time */
  }
}

/** The live answer to the request just sent from the command bar. */
function RequestResult({ id, onClose }: { id: number; onClose: () => void }) {
  const [done, setDone] = useState(false);
  const detail = usePoll(() => api.get<{ request: SupervisorRequest; tasks: Task[] }>(`/api/supervisor/requests/${id}`), done ? 0 : 2_000, [id, done]);
  const req = detail.data?.request;
  useEffect(() => {
    if (req && TERMINAL.has(req.status)) setDone(true);
  }, [req]);
  const steps = (detail.data?.tasks ?? []).filter((t) => !(t.agent === "supervisor" && ["plan_request", "summarize_request"].includes(t.capability)));
  const waiting = steps.filter((t) => t.status === "awaiting_approval").length;
  return (
    <div className="command-result" role="status" aria-live="polite">
      <div className="command-result-head">
        <span className="muted small">Request #{id}</span>
        {req ? <Pill value={req.status} /> : null}
        <button className="btn ghost icon" onClick={onClose} aria-label="Close the answer">
          <Icon name="close" size={16} />
        </button>
      </div>
      <ErrorNote error={detail.error} />
      {!req || req.status === "planning" ? <p className="muted">The AI Supervisor is planning…</p> : null}
      {req && req.status !== "planning" ? <p className="command-reply">{req.summary || req.reply || "Working on it…"}</p> : null}
      {steps.length ? (
        <ul className="command-steps">
          {steps.slice(0, 5).map((t) => (
            <li key={t.id}>
              <Pill value={t.status} /> <code>{t.agent}.{t.capability}</code>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="command-result-foot">
        {waiting ? (
          <Link className="btn small primary" to="/approvals" onClick={onClose}>
            Review {waiting} approval{waiting > 1 ? "s" : ""}
          </Link>
        ) : null}
        <Link className="btn small" to={`/supervisor?id=${id}`} onClick={onClose}>
          Open in AI Supervisor
        </Link>
      </div>
    </div>
  );
}

export default function CommandBar() {
  const { canOperate } = useAuth();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [requestId, setRequestId] = useState<number | null>(null);
  const [focused, setFocused] = useState(false);
  const [askConsent, setAskConsent] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const voice = useSpeech(setMessage);
  const voiceAvailable = canOperate && speechSupported();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) {
        setFocused(false);
        setAskConsent(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  async function execute(e?: FormEvent) {
    e?.preventDefault();
    const text = message.trim();
    if (!text || busy) return;
    voice.stop();
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ request: SupervisorRequest }>("/api/supervisor/requests", { message: text });
      setRequestId(r.request.id);
      setMessage("");
      setFocused(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function toggleVoice() {
    if (voice.listening) return voice.stop();
    if (!readConsent()) return setAskConsent(true);
    voice.start();
  }

  const showSuggestions = focused && canOperate && !message && !requestId;
  return (
    <div className="command" ref={box}>
      <form className={`command-bar ${voice.listening ? "listening" : ""}`} onSubmit={execute} role="search">
        <Icon name="sparkles" size={18} className="command-icon" />
        <input
          ref={input}
          aria-label="Command for the AI Supervisor"
          placeholder={canOperate ? "Ask the AI Supervisor… e.g. Check all websites" : "Viewers can read, but not send commands"}
          value={message}
          disabled={!canOperate}
          maxLength={4000}
          onChange={(e) => setMessage(e.target.value)}
          onFocus={() => setFocused(true)}
          onKeyDown={(e) => e.key === "Escape" && (e.currentTarget.blur(), setFocused(false))}
        />
        <kbd className="command-kbd" aria-hidden>
          Ctrl K
        </kbd>
        {voiceAvailable ? (
          <button
            type="button"
            className={`btn ghost icon command-mic ${voice.listening ? "active" : ""}`}
            onClick={toggleVoice}
            aria-label={voice.listening ? "Stop voice input" : "Voice input"}
            aria-pressed={voice.listening}
            title={voice.listening ? "Listening… click to stop" : "Voice input (fills the box; nothing runs until you press Execute)"}
          >
            <Icon name="mic" size={18} />
          </button>
        ) : null}
        <button className="btn primary command-go" type="submit" disabled={!canOperate || busy || !message.trim()}>
          <Icon name="play" size={14} />
          <span>{busy ? "Sending…" : "Execute"}</span>
        </button>
      </form>
      {askConsent ? (
        <div className="command-pop" role="dialog" aria-label="Voice input">
          <p>
            <strong>Voice input uses your browser's speech service.</strong> In Chrome and Edge the audio is sent to Google or Microsoft to be turned into
            text. The text only fills the command box; nothing runs until you press Execute.
          </p>
          <div className="row-actions">
            <button
              className="btn small primary"
              onClick={() => {
                saveConsent();
                setAskConsent(false);
                voice.start();
              }}
            >
              Use voice input
            </button>
            <button className="btn small" onClick={() => setAskConsent(false)}>
              Not now
            </button>
          </div>
        </div>
      ) : null}
      {voice.error ? <div className="command-pop command-note">{voice.error}</div> : null}
      {error ? (
        <div className="command-pop">
          <ErrorNote error={error} />
        </div>
      ) : null}
      {showSuggestions ? (
        <div className="command-pop" role="listbox" aria-label="Suggestions">
          <div className="muted small">Try</div>
          <div className="chips">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                className="chip"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  setMessage(s);
                  input.current?.focus();
                }}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="muted small">Anything that publishes, sends, spends or changes a server waits for an approval.</div>
        </div>
      ) : null}
      {requestId ? (
        <div className="command-pop">
          <RequestResult id={requestId} onClose={() => setRequestId(null)} />
        </div>
      ) : null}
    </div>
  );
}
