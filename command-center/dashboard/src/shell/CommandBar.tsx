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
  const mic = useRef<HTMLButtonElement>(null);
  const consentButton = useRef<HTMLButtonElement>(null);
  const voice = useSpeech(setMessage);
  const clearVoiceError = voice.clearError;
  const voiceAvailable = canOperate && speechSupported();

  // Ctrl/Cmd+K jumps to the command input (no single-key shortcut), but not out of an open dialog.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.ctrlKey || e.metaKey) && !document.querySelector('[aria-modal="true"]')) {
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
        setError(null);
        clearVoiceError();
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [clearVoiceError]);

  // The consent question takes the focus while it is open.
  useEffect(() => {
    if (askConsent) consentButton.current?.focus();
  }, [askConsent]);

  async function execute(e?: FormEvent) {
    e?.preventDefault();
    const text = message.trim();
    if (!text || busy) return;
    // abort, not stop: a late final result would put the sent command back in the box.
    voice.abort();
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

  function closeConsent() {
    setAskConsent(false);
    mic.current?.focus();
  }

  function clearErrors() {
    setError(null);
    clearVoiceError();
  }

  // One pop at a time, most recent need first: the consent question, then an error, then the answer, then suggestions.
  const showError = !askConsent && Boolean(error || voice.error);
  const showSuggestions = focused && canOperate && !message && !requestId && !askConsent && !voice.error && !error;
  return (
    <div
      className="command"
      ref={box}
      onBlur={(e) => {
        // Focus moved to something outside the command bar (a click on nothing is handled by the mousedown listener).
        if (e.relatedTarget && !e.currentTarget.contains(e.relatedTarget as Node)) {
          setFocused(false);
          setAskConsent(false);
        }
      }}
    >
      <form className={`command-bar ${voice.listening ? "listening" : ""}`} onSubmit={execute} role="search">
        <Icon name="sparkles" size={18} className="command-icon" />
        <input
          ref={input}
          aria-label="Command for the AI Supervisor"
          placeholder={canOperate ? "Ask the AI Supervisor… e.g. Check all websites" : "Viewers can read, but not send commands"}
          value={message}
          disabled={!canOperate}
          maxLength={4000}
          onChange={(e) => {
            setMessage(e.target.value);
            clearErrors();
          }}
          onFocus={() => setFocused(true)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setFocused(false);
              clearErrors();
            }
          }}
        />
        <kbd className="command-kbd" aria-hidden>
          Ctrl K
        </kbd>
        {voiceAvailable ? (
          <button
            ref={mic}
            type="button"
            className={`btn ghost icon command-mic ${voice.listening ? "active" : ""}`}
            onClick={toggleVoice}
            aria-label="Voice input"
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
        <div className="command-pop" role="dialog" aria-label="Voice input" onKeyDown={(e) => e.key === "Escape" && closeConsent()}>
          <p>
            <strong>Voice input uses your browser's speech service.</strong> In Chrome and Edge the audio is sent to Google or Microsoft to be turned into
            text. The text only fills the command box; nothing runs until you press Execute.
          </p>
          <div className="row-actions">
            <button
              ref={consentButton}
              className="btn small primary"
              onClick={() => {
                saveConsent();
                closeConsent();
                voice.start();
              }}
            >
              Use voice input
            </button>
            <button className="btn small" onClick={closeConsent}>
              Not now
            </button>
          </div>
        </div>
      ) : null}
      {showError ? (
        <div className="command-pop command-pop-dismissable">
          <div className="command-pop-main">
            <ErrorNote error={error} />
            {voice.error ? (
              <div className="command-note" role="alert">
                {voice.error}
              </div>
            ) : null}
          </div>
          <button
            type="button"
            className="btn ghost icon"
            onClick={() => {
              clearErrors();
              // The button goes away with the message; the focus goes back to the input instead of the page.
              input.current?.focus();
            }}
            aria-label="Dismiss"
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      ) : null}
      {showSuggestions ? (
        <div className="command-pop" role="group" aria-label="Suggestions">
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
        // Hidden, not unmounted, while another pop is open, so the answer keeps updating. A new request gets a fresh RequestResult.
        <div className="command-pop" hidden={askConsent || showError}>
          <RequestResult key={requestId} id={requestId} onClose={() => setRequestId(null)} />
        </div>
      ) : null}
    </div>
  );
}
