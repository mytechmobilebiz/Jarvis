import { stripSpeak, SPOKEN_REPLY_INSTRUCTION } from "./voice-chunker.js";
import { createReactor, type ReactorController } from "./reactor.js";
import { FIRST_RUN_HINT, agentNote, isFirstRun } from "./first-run.js";
import { chatBubbleText, chunkError, parseSseData, type GatewayChunk } from "./sse.js";
import { createTypingDots } from "./typing-dots.js";
import { DEFAULT_SETTINGS, DEFAULT_STATE_SERVER_PORT, normalizeSettings, type HudSettings } from "./settings.js";
import type { AgentRole, AgentStatus, ApprovalDecision, ApprovalRequest, HudState } from "./state-server.js";
import type { HistoryMessage, HistorySession, HistoryToolCall, SessionKind } from "./history.js";
import type { JarvisBridge } from "./preload.cjs";
// --- Lane C (voice) ---------------------------------------------------------
import { createVoiceUi, type VoiceUi } from "./voice-renderer.js";
// --- end Lane C -------------------------------------------------------------

declare global {
  interface Window {
    jarvis?: JarvisBridge;
  }
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

const AGENT_ROLES: AgentRole[] = ["jarvis", "sarah", "tom", "bobby", "scout", "ava"];
// Honest lineage labels rather than clever merging: a session says what it is.
const KIND_LABELS: Record<SessionKind, string> = {
  normal: "",
  delegated: "DELEGATED",
  branch: "BRANCH",
  continuation: "CONTINUED",
};
const STATUS_LABELS: Record<AgentStatus, string> = {
  idle: "IDLE",
  working: "WORKING",
  waiting_approval: "APPROVAL",
  offline: "OFFLINE",
};

let settings: HudSettings = { ...DEFAULT_SETTINGS };
let noBrainLine: string | undefined;
let state: HudState | null = null;
let reactor: ReactorController | null = null;
let reconnectTimer = 0;
let cursorTimer = 0;
let autoPlayedBriefKey = "";
let chatMessages: ChatMessage[] = [];
const chatSessionId = crypto.randomUUID();
let approvalRequest: ApprovalRequest | null = null;
let chatBusy = false;
let historyRole: AgentRole | null = null;
let historyLoaded = 0;
// Every history fetch carries a token; a reply for a panel the operator has
// already navigated away from is dropped instead of painting the wrong agent.
let historyToken = 0;

const els = {
  root: document.documentElement,
  body: document.body,
  clock: must<HTMLElement>("clock"),
  approvals: must<HTMLElement>("approvals"),
  approvalDialog: must<HTMLDialogElement>("approval-dialog"),
  approvalAgent: must<HTMLElement>("approval-agent"),
  approvalSummary: must<HTMLElement>("approval-summary"),
  approvalCommand: must<HTMLElement>("approval-command"),
  approvalCwd: must<HTMLElement>("approval-cwd"),
  approvalApprove: must<HTMLButtonElement>("approval-approve"),
  approvalDeny: must<HTMLButtonElement>("approval-deny"),
  briefCountdown: must<HTMLElement>("brief-countdown"),
  connection: must<HTMLElement>("connection"),
  agents: must<HTMLElement>("agents"),
  agentsHint: must<HTMLElement>("agents-hint"),
  activity: must<HTMLElement>("activity"),
  reactorCanvas: must<HTMLCanvasElement>("reactor"),
  mrr: must<HTMLElement>("mrr"),
  progressLabel: must<HTMLElement>("progress-label"),
  today: must<HTMLElement>("today"),
  arr: must<HTMLElement>("arr"),
  target: must<HTMLElement>("target"),
  briefDate: must<HTMLElement>("brief-date"),
  briefText: must<HTMLElement>("brief-text"),
  briefPlay: must<HTMLButtonElement>("brief-play"),
  briefStatus: must<HTMLElement>("brief-status"),
  chat: must<HTMLElement>("chat"),
  chatToggle: must<HTMLButtonElement>("chat-toggle"),
  chatForm: must<HTMLFormElement>("chat-form"),
  chatInput: must<HTMLInputElement>("chat-input"),
  chatSend: must<HTMLButtonElement>("chat-send"),
  chatLog: must<HTMLElement>("chat-log"),
  demoBadge: must<HTMLElement>("demo-badge"),
  settingsOpen: must<HTMLButtonElement>("settings-open"),
  settingsDialog: must<HTMLDialogElement>("settings-dialog"),
  settingsForm: must<HTMLFormElement>("settings-form"),
  apiKey: must<HTMLInputElement>("api-key"),
  gatewayPort: must<HTMLInputElement>("gateway-port"),
  startAtLogin: must<HTMLInputElement>("start-at-login"),
  demoMode: must<HTMLInputElement>("demo-mode"),
  kioskMode: must<HTMLInputElement>("kiosk-mode"),
  settingsCancel: must<HTMLButtonElement>("settings-cancel"),
  rerunSetup: must<HTMLButtonElement>("rerun-setup"),
  connectSlack: must<HTMLButtonElement>("connect-slack"),
  supportField: must<HTMLElement>("support-field"),
  supportCode: must<HTMLElement>("support-code"),
  appVersion: must<HTMLElement>("app-version"),
  copyDiagnostics: must<HTMLButtonElement>("copy-diagnostics"),
  history: must<HTMLElement>("history"),
  historyAgent: must<HTMLElement>("history-agent"),
  historyClose: must<HTMLButtonElement>("history-close"),
  historyBack: must<HTMLButtonElement>("history-back"),
  historySubtitle: must<HTMLElement>("history-subtitle"),
  historyBody: must<HTMLElement>("history-body"),
  // --- Lane C (voice) -------------------------------------------------------
  voiceMic: must<HTMLButtonElement>("voice-mic"),
  voiceStop: must<HTMLButtonElement>("voice-stop"),
  speakReplies: must<HTMLInputElement>("speak-replies"),
  voiceState: must<HTMLElement>("voice-state"),
  voiceDetail: must<HTMLElement>("voice-detail"),
  voiceVoiceId: must<HTMLElement>("voice-voice-id"),
  // --- end Lane C -----------------------------------------------------------
};

/** Lane C (voice): null until wireUi runs, and null forever without a bridge. */
let voice: VoiceUi | null = null;
function safeVoice<T>(run: () => T): T | undefined {
  try { return run(); }
  catch (error) { console.warn("[voice] operation failed", error); return undefined; }
}

void boot().catch((error) => {
  renderBootError(error);
});

async function boot(): Promise<void> {
  // ARRIVAL (the birth's HUD side, TRANSITION-DESIGN.md §8): the class must
  // land before first paint, so it goes on synchronously, before any await.
  const born = arrivalMode();
  if (born === "full") els.body.classList.add("arrival");

  [settings, noBrainLine] = await Promise.all([loadSettings(), loadNoBrainLine()]);
  applySettings(settings);
  reactor = createReactor(els.reactorCanvas, { color: "#35c6ff", coreColor: "#eaf7ff", quality: "auto" });

  wireUi();
  if (born === "full") beginFullArrival();
  else if (born === "fast") expandChat(true);
  connectStateSocket();
  tickClock();
  window.setInterval(tickClock, 1_000);
  window.setInterval(checkBriefPlayback, 10_000);
}

// ---------------------------------------------------------------------------
// ARRIVAL — only a page born out of the setup transition has a query param;
// a normal launch takes none of these paths and boots exactly as today.
// ---------------------------------------------------------------------------

/** How this page was born. Reduced motion turns the full arrival into fast. */
function arrivalMode(): "full" | "fast" | null {
  const born = new URLSearchParams(location.search).get("born");
  if (born !== "full" && born !== "fast") return null;
  if (born === "full") {
    try {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return "fast";
    } catch {
      // No preference readable — assume motion is welcome.
    }
  }
  return born;
}

/** While true, renderState leaves the reactor's intensity to the ramp. */
let arrivalRamping = false;

/**
 * The full arrival: the reactor ramps 0.08 → its state-driven cruise over
 * 1.2 s (the landing's rampReactor, easeOutCubic), panels holo in staggered
 * via CSS, and at ~900 ms the chat bar expands with the cursor landing where
 * "what would you like me to handle first?" gets answered. Any input jumps
 * every animation to its end state. Data wiring runs untouched underneath.
 */
function beginFullArrival(): void {
  arrivalRamping = true;
  reactor?.setIntensity(0.08);
  const t0 = performance.now();
  const RAMP_MS = 1200;
  const frame = (t: number): void => {
    const p = Math.min(1, (t - t0) / RAMP_MS);
    const eased = 1 - Math.pow(1 - p, 3);
    reactor?.setIntensity(0.08 + (reactorTarget() - 0.08) * eased);
    if (p < 1) requestAnimationFrame(frame);
    else arrivalRamping = false;
  };
  requestAnimationFrame(frame);

  const chatTimer = window.setTimeout(() => expandChat(true), 900);
  const finish = (): void => {
    window.clearTimeout(failsafe);
    window.clearTimeout(chatTimer);
    window.removeEventListener("pointerdown", jump, { capture: true });
    window.removeEventListener("keydown", jump, { capture: true });
    expandChat(true);
    els.body.classList.remove("arrival", "arrival-done");
  };
  // The chat panel animates last (+360 ms), so its animationend closes the
  // arrival; the failsafe covers a machine that drops the event.
  const failsafe = window.setTimeout(finish, 1_600);
  els.chat.addEventListener("animationend", finish, { once: true });
  const jump = (): void => {
    // Every animation to its end state immediately; the office is theirs.
    els.body.classList.add("arrival-done");
    finish();
  };
  window.addEventListener("pointerdown", jump, { capture: true });
  window.addEventListener("keydown", jump, { capture: true });
}

/** The state-driven cruise intensity renderState normally applies. */
function reactorTarget(): number {
  const progress = state !== null && state.revenue.target > 0
    ? clamp(state.revenue.mrr / state.revenue.target, 0, 1)
    : 0;
  return 0.34 + progress * 0.66;
}

/** Chat open, expanded for the accessibility tree, cursor in the input. */
function expandChat(focus: boolean): void {
  els.chat.classList.remove("collapsed");
  els.chatToggle.setAttribute("aria-expanded", "true");
  if (focus) els.chatInput.focus();
}

async function loadSettings(): Promise<HudSettings> {
  if (window.jarvis) {
    return normalizeSettings(await window.jarvis.getSettings());
  }

  try {
    const raw = window.localStorage.getItem("jarvis-hud-settings");
    return raw ? normalizeSettings(JSON.parse(raw)) : { ...DEFAULT_SETTINGS };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

async function loadNoBrainLine(): Promise<string | undefined> {
  return await window.jarvis?.getNoBrainLine();
}

function wireUi(): void {
  window.jarvis?.onApprovalRequest(renderApproval);
  els.approvalApprove.addEventListener("click", () => answerApproval("approve"));
  els.approvalDeny.addEventListener("click", () => answerApproval("deny"));
  els.approvalDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    answerApproval("deny");
  });
  els.chatToggle.addEventListener("click", () => {
    els.chat.classList.toggle("collapsed");
    els.chatToggle.setAttribute("aria-expanded", String(!els.chat.classList.contains("collapsed")));
  });

  els.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void sendChat();
  });

  // --- Lane C (voice) -------------------------------------------------------
  // Voice attaches only when the preload bridge is there. In a plain browser
  // (the smoke harness) the mic simply never appears and chat is unchanged.
  const bridge = window.jarvis;
  if (bridge?.voice) {
    voice = createVoiceUi({
      bridge: bridge.voice,
      speakReplies: settings.speakReplies,
      elements: {
        mic: els.voiceMic,
        stop: els.voiceStop,
        speakReplies: els.speakReplies,
        state: els.voiceState,
        form: els.chatForm,
        input: els.chatInput,
        detail: els.voiceDetail,
        voiceId: els.voiceVoiceId,
        // W4a: a hold pops the drawer open so dictation is visible.
        chat: els.chat,
        chatToggle: els.chatToggle,
      },
      submit: () => {
        void sendChat();
      },
      isBusy: () => chatBusy,
    });
    void voice.refresh();
  }
  // --- end Lane C -----------------------------------------------------------

  els.settingsOpen.addEventListener("click", () => {
    populateSettingsForm();
    void refreshSupportCode();
    void refreshVersion();
    els.settingsDialog.showModal();
  });

  els.settingsCancel.addEventListener("click", () => {
    els.settingsDialog.close();
  });

  if (window.jarvis?.rerunSetup) {
    els.rerunSetup.addEventListener("click", () => {
      void window.jarvis?.rerunSetup();
    });
  } else {
    els.rerunSetup.closest(".field")?.setAttribute("hidden", "");
  }

  if (window.jarvis?.connectSlack) {
    els.connectSlack.addEventListener("click", () => {
      void window.jarvis?.connectSlack();
    });
  } else {
    els.connectSlack.closest(".field")?.setAttribute("hidden", "");
  }

  if (window.jarvis?.getDiagnostics) {
    els.copyDiagnostics.addEventListener("click", () => {
      void copyDiagnostics();
    });
  } else {
    els.supportField.setAttribute("hidden", "");
  }

  els.settingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveSettingsFromForm();
  });

  els.briefPlay.addEventListener("click", () => {
    void playBrief();
  });

  els.historyClose.addEventListener("click", () => {
    closeHistory();
  });

  els.historyBack.addEventListener("click", () => {
    if (historyRole) void showSessionList(historyRole);
  });

  window.addEventListener("keydown", (event) => {
    // The settings dialog closes itself on Escape; don't collapse both at once.
    if (event.key === "Escape" && historyRole !== null && !els.settingsDialog.open) {
      event.preventDefault();
      closeHistory();
    }
  });

  for (const eventName of ["mousemove", "keydown", "pointerdown"]) {
    window.addEventListener(eventName, resetCursorTimer, { passive: true });
  }

  window.jarvis?.onSettingsChanged((next) => {
    settings = normalizeSettings(next);
    applySettings(settings);
  });
}

function connectStateSocket(): void {
  window.clearTimeout(reconnectTimer);
  setConnection("CONNECTING");
  const socket = new WebSocket(stateWsUrl(settings));

  socket.addEventListener("open", () => {
    setConnection("STATE ONLINE");
  });

  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(String(event.data)) as { type?: string; state?: HudState };
      if (payload.state) {
        state = payload.state;
        renderState();
      }
    } catch (error) {
      console.warn("Invalid HUD state frame.", error);
    }
  });

  socket.addEventListener("close", () => {
    setConnection("STATE OFFLINE");
    reconnectTimer = window.setTimeout(connectStateSocket, 1_500);
  });

  socket.addEventListener("error", () => {
    setConnection("STATE OFFLINE");
  });
}

function renderState(): void {
  if (!state) {
    return;
  }

  const progress = state.revenue.target > 0 ? clamp(state.revenue.mrr / state.revenue.target, 0, 1) : 0;
  reactor?.setProgress(progress);
  // During the arrival the ramp owns intensity (it reads the same target).
  if (!arrivalRamping) reactor?.setIntensity(0.34 + progress * 0.66);
  els.root.style.setProperty("--reactor-progress", `${Math.round(progress * 100)}%`);

  els.mrr.textContent = formatCurrency(state.revenue.mrr, state.revenue.currency, 0);
  els.progressLabel.textContent = `${Math.round(progress * 100)}% TO TARGET`;
  els.today.textContent = formatCurrency(state.revenue.today, state.revenue.currency, 0);
  els.arr.textContent = formatCurrency(state.revenue.arr, state.revenue.currency, 0);
  els.target.textContent = formatCurrency(state.revenue.target, state.revenue.currency, 0);
  els.approvals.textContent = state.approvals.pending > 0 ? `${state.approvals.pending} APPROVAL${state.approvals.pending === 1 ? "" : "S"}` : "NO APPROVALS";
  els.approvals.classList.toggle("hot", state.approvals.pending > 0);

  renderAgents(state);
  renderActivity(state);
  renderBrief(state);
  checkBriefPlayback();
}

function renderAgents(next: HudState): void {
  // A fresh install has nothing to report until its first scheduled run; say so
  // rather than showing six OFFLINE / "No signal" panels that read as broken.
  const firstRun = isFirstRun(next);
  els.agentsHint.textContent = firstRun ? FIRST_RUN_HINT : "";
  els.agentsHint.hidden = !firstRun;

  const fragment = document.createDocumentFragment();
  for (const role of AGENT_ROLES) {
    const agent = next.agents[role];
    const tile = document.createElement("article");
    tile.className = `agent status-${agent.status}${role === historyRole ? " selected" : ""}`;
    // A heading and a paragraph cannot live inside a <button>, so the tile keeps
    // its article markup and borrows the button role for keyboard operators.
    tile.setAttribute("role", "button");
    tile.dataset.role = role;
    tile.tabIndex = 0;
    tile.setAttribute("aria-controls", "history");
    tile.setAttribute("aria-expanded", String(role === historyRole));
    tile.setAttribute("aria-label", `${role.toUpperCase()} session history`);
    tile.addEventListener("click", () => {
      void openHistory(role);
    });
    tile.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      void openHistory(role);
    });

    const header = document.createElement("div");
    header.className = "agent-head";

    const name = document.createElement("h2");
    name.textContent = role.toUpperCase();

    const status = document.createElement("span");
    status.className = "agent-status";
    status.textContent = STATUS_LABELS[agent.status];

    const note = document.createElement("p");
    note.textContent = agentNote(agent, firstRun);

    const time = document.createElement("time");
    time.dateTime = agent.updatedAt;
    time.textContent = relativeTime(agent.updatedAt);

    header.append(name, status);
    tile.append(header, note, time);
    fragment.append(tile);
  }

  els.agents.replaceChildren(fragment);
}

function renderActivity(next: HudState): void {
  const fragment = document.createDocumentFragment();
  for (const entry of next.activity.slice(0, 80)) {
    const item = document.createElement("article");
    item.className = `activity-item role-${cssName(entry.role)}`;

    const meta = document.createElement("div");
    meta.className = "activity-meta";

    const role = document.createElement("span");
    role.textContent = entry.role.toUpperCase();

    const time = document.createElement("time");
    time.dateTime = entry.ts;
    time.textContent = relativeTime(entry.ts);

    const text = document.createElement("p");
    text.textContent = entry.text;

    meta.append(role, time);
    item.append(meta, text);
    fragment.append(item);
  }

  els.activity.replaceChildren(fragment);
}

function renderBrief(next: HudState): void {
  els.briefDate.textContent = next.brief.date || "NO BRIEF";
  els.briefText.textContent = next.brief.text || "Awaiting daily brief.";
  // Lane C (voice): with a working voice engine, brief text alone is enough to
  // offer PLAY — the clip is synthesized on demand (and normally already
  // cached from pre-generation). Without one, the old rule stands.
  const canSynthesize = voice?.status?.available === true;
  els.briefPlay.disabled = briefPreparing || (canSynthesize ? next.brief.text === "" : !next.brief.audioPath);
}

async function openHistory(role: AgentRole): Promise<void> {
  historyRole = role;
  els.history.classList.add("open");
  els.history.setAttribute("aria-hidden", "false");
  els.historyAgent.textContent = role.toUpperCase();
  syncAgentSelection();
  els.historyClose.focus();
  await showSessionList(role);
}

function closeHistory(): void {
  // Hand the keyboard back to the tile that opened the panel; tiles are rebuilt
  // on every state frame, so look the current one up rather than holding a ref.
  const tile = els.agents.querySelector<HTMLElement>(`.agent[data-role="${historyRole}"]`);
  historyRole = null;
  historyToken += 1;
  tile?.focus();
  els.history.classList.remove("open");
  els.history.setAttribute("aria-hidden", "true");
  els.historyBody.replaceChildren();
  syncAgentSelection();
}

function syncAgentSelection(): void {
  for (const tile of Array.from(els.agents.children)) {
    const selected = tile instanceof HTMLElement && tile.dataset.role === historyRole;
    tile.classList.toggle("selected", selected);
    tile.setAttribute("aria-expanded", String(selected));
  }
}

async function showSessionList(role: AgentRole): Promise<void> {
  const token = ++historyToken;
  historyLoaded = 0;
  els.historyBack.hidden = true;
  els.historySubtitle.textContent = "";
  renderHistoryNote("LOADING", `Retrieving ${titleCase(role)}'s archive.`, true);

  const bridge = window.jarvis;
  if (!bridge) {
    renderHistoryNote("UNAVAILABLE", "Session history is only available inside the JARVIS HUD desktop app.");
    return;
  }

  const result = await bridge.listSessions(role, 0).catch(() => null);
  if (token !== historyToken) {
    return;
  }

  if (result === null || !result.available) {
    if (result?.reason === "unreadable") {
      renderHistoryNote("ARCHIVE LOCKED", `${titleCase(role)}'s archive could not be read. Try again in a moment.`);
    } else {
      // "hasn't been activated" read as a licensing/activation failure to a
      // customer who just paid (Andy Werick, first day). This is the empty
      // state of a healthy new install, and the panel is the natural click
      // target for "talk to JARVIS" — so redirect to the actual chat bar.
      renderHistoryNote(
        "NO REPORTS YET",
        role === "jarvis"
          ? "Jarvis hasn't filed any reports here yet — they appear after his first scheduled run. To talk to JARVIS now, type in the chat bar at the bottom of the screen (labeled HERMES AGENT)."
          : `${titleCase(role)} hasn't filed any reports here yet — they appear after their first scheduled run. To talk to JARVIS now, type in the chat bar at the bottom of the screen (labeled HERMES AGENT).`,
      );
    }
    return;
  }

  if (result.sessions.length === 0) {
    // Everyone but Jarvis works inside his profile, so an empty tile means their
    // own schedule has not run yet — not that they are missing.
    if (role === "jarvis") {
      renderHistoryNote("NO SESSIONS", "Jarvis is on staff but hasn't held a session yet. To talk to JARVIS now, type in the chat bar at the bottom of the screen (labeled HERMES AGENT).");
    } else {
      renderHistoryNote(
        "NO SESSIONS",
        `${titleCase(role)} hasn't held a session of their own yet. Their scheduled work will appear here once their schedule is enabled; everything else runs through Jarvis.`,
      );
    }
    return;
  }

  els.historyBody.replaceChildren(sessionPage(role, result.sessions, result.hasMore));
  els.historyBody.scrollTop = 0;
}

function sessionPage(role: AgentRole, sessions: HistorySession[], hasMore: boolean): DocumentFragment {
  const fragment = document.createDocumentFragment();
  for (const session of sessions) {
    fragment.append(sessionRow(role, session));
  }

  historyLoaded += sessions.length;
  els.historySubtitle.textContent = `${historyLoaded} SESSION${historyLoaded === 1 ? "" : "S"}${hasMore ? "+" : ""}`;
  if (hasMore) {
    fragment.append(loadMoreRow(role));
  }
  return fragment;
}

function loadMoreRow(role: AgentRole): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "session load-more";
  button.textContent = "LOAD MORE";
  button.addEventListener("click", () => {
    void loadMoreSessions(role, button);
  });
  return button;
}

async function loadMoreSessions(role: AgentRole, button: HTMLButtonElement): Promise<void> {
  const token = historyToken;
  const offset = historyLoaded;
  button.disabled = true;
  button.textContent = "LOADING";

  const result = await window.jarvis?.listSessions(role, offset).catch(() => null);
  // The panel may have closed or switched agents while this page was in flight.
  if (token !== historyToken || !button.isConnected) {
    return;
  }

  if (!result || !result.available || result.sessions.length === 0) {
    button.textContent = "NO MORE SESSIONS";
    return;
  }

  button.replaceWith(sessionPage(role, result.sessions, result.hasMore));
}

function sessionRow(role: AgentRole, session: HistorySession): HTMLButtonElement {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "session";

  const meta = document.createElement("span");
  meta.className = "session-meta";

  const source = document.createElement("span");
  source.textContent = session.source.toUpperCase();

  const when = document.createElement("span");
  when.className = "when";
  when.textContent = sessionDate(session.startedAt);

  const title = document.createElement("span");
  title.className = "session-title";
  title.textContent = stripSpeak(session.title ?? "Untitled session");

  const stats = document.createElement("span");
  stats.className = "session-stats";
  for (const stat of sessionStats(session)) {
    const item = document.createElement("span");
    item.textContent = stat;
    stats.append(item);
  }
  const marker = KIND_LABELS[session.kind];
  if (marker) {
    const lineage = document.createElement("span");
    lineage.className = session.kind === "delegated" ? "delegated" : "lineage";
    lineage.textContent = marker;
    stats.append(lineage);
  }

  meta.append(source, when);
  row.append(meta, title, stats);
  row.addEventListener("click", () => {
    void showTranscript(role, session);
  });
  return row;
}

function sessionStats(session: HistorySession): string[] {
  const stats = [`${session.messageCount} MSG`];
  if (session.toolCallCount > 0) {
    stats.push(`${session.toolCallCount} TOOL${session.toolCallCount === 1 ? "" : "S"}`);
  }
  const duration = sessionDuration(session);
  if (duration) {
    stats.push(duration);
  }
  if (session.costUsd !== null && session.costUsd > 0) {
    stats.push(formatCost(session.costUsd));
  }
  return stats;
}

async function showTranscript(role: AgentRole, session: HistorySession): Promise<void> {
  const token = ++historyToken;
  els.historyBack.hidden = false;
  els.historySubtitle.textContent = (session.title ?? "Untitled session").toUpperCase();
  renderHistoryNote("LOADING", "Retrieving the transcript.", true);

  const result = await window.jarvis?.getTranscript(role, session.id).catch(() => null);
  if (token !== historyToken) {
    return;
  }

  if (!result || !result.available) {
    renderHistoryNote("TRANSCRIPT UNAVAILABLE", "This session could not be read back.");
    return;
  }

  if (result.messages.length === 0) {
    renderHistoryNote("EMPTY SESSION", "Nothing was said in this session.");
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const message of result.messages) {
    fragment.append(transcriptMessage(role, message));
  }
  if (result.truncated) {
    fragment.append(historyNote("TRUNCATED", "Only the first 2,000 messages of this session are shown."));
  }
  els.historyBody.replaceChildren(fragment);
  els.historyBody.scrollTop = 0;
}

function transcriptMessage(role: AgentRole, message: HistoryMessage): HTMLElement {
  const block = document.createElement("article");
  block.className = `transcript-msg ${message.role}`;

  const meta = document.createElement("div");
  meta.className = "msg-meta";

  const speaker = document.createElement("span");
  speaker.textContent = speakerLabel(role, message);

  const time = document.createElement("time");
  const iso = epochToIso(message.timestamp);
  time.dateTime = iso;
  time.textContent = clockTime(message.timestamp);

  meta.append(speaker, time);
  if (message.contentTruncated) {
    const cut = document.createElement("span");
    cut.className = "msg-truncated";
    cut.textContent = "· TRUNCATED";
    meta.append(cut);
  }
  block.append(meta);

  if (message.content !== "") {
    if (message.role === "tool") {
      block.append(outputBlock("TOOL RESULT", message.content));
    } else {
      const bubble = document.createElement("div");
      bubble.className = `chat-bubble ${message.role === "user" ? "user" : "assistant"}`;
      bubble.textContent = stripSpeak(message.content);
      block.append(bubble);
    }
  }

  for (const call of message.toolCalls) {
    block.append(toolCallBlock(call));
  }

  return block;
}

function toolCallBlock(call: HistoryToolCall): HTMLDetailsElement {
  const details = document.createElement("details");
  details.className = "output";

  const summary = document.createElement("summary");
  summary.textContent = `TOOL CALL · ${call.name.toUpperCase()}`;
  details.append(summary);

  const args = document.createElement("div");
  args.className = "cmd";
  args.textContent = call.argsSummary || "(no arguments)";
  details.append(args);

  return details;
}

function outputBlock(label: string, content: string): HTMLDetailsElement {
  const details = document.createElement("details");
  details.className = "output";

  const summary = document.createElement("summary");
  summary.textContent = label;
  details.append(summary);

  const pre = document.createElement("pre");
  pre.textContent = content;
  details.append(pre);

  return details;
}

function speakerLabel(role: AgentRole, message: HistoryMessage): string {
  if (message.role === "user") return "YOU";
  if (message.role === "assistant") return role.toUpperCase();
  if (message.role === "tool") return (message.toolName ?? "TOOL").toUpperCase();
  return "SYSTEM";
}

function renderHistoryNote(label: string, text: string, loading = false): void {
  els.historyBody.replaceChildren(historyNote(label, text, loading));
}

function historyNote(label: string, text: string, loading = false): HTMLElement {
  const note = document.createElement("p");
  note.className = loading ? "history-note loading" : "history-note";

  const heading = document.createElement("b");
  heading.textContent = label;

  note.append(heading, document.createTextNode(text));
  return note;
}

function sessionDate(startedAt: number): string {
  const date = new Date(startedAt * 1_000);
  if (!Number.isFinite(date.getTime())) {
    return "--";
  }
  const stamp = date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return `${stamp.toUpperCase()} · ${relativeTime(date.toISOString())}`;
}

function sessionDuration(session: HistorySession): string {
  if (session.endedAt === null) {
    return "";
  }
  const seconds = Math.max(0, Math.round(session.endedAt - session.startedAt));
  if (seconds < 60) {
    return `${seconds}S`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}M`;
  }
  return `${Math.floor(minutes / 60)}H ${minutes % 60}M`;
}

function clockTime(timestamp: number): string {
  const date = new Date(timestamp * 1_000);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "--";
}

function epochToIso(timestamp: number): string {
  const date = new Date(timestamp * 1_000);
  return Number.isFinite(date.getTime()) ? date.toISOString() : "";
}

function formatCost(value: number): string {
  return value < 0.01 ? "<$0.01" : `$${value.toFixed(2)}`;
}

function titleCase(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function tickClock(): void {
  const now = new Date();
  els.clock.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  renderBriefCountdown();
}

function renderBriefCountdown(): void {
  if (!state?.nextBriefAt) {
    els.briefCountdown.textContent = "BRIEF --";
    return;
  }

  const due = Date.parse(state.nextBriefAt);
  if (!Number.isFinite(due)) {
    els.briefCountdown.textContent = "BRIEF --";
    return;
  }

  const ms = due - Date.now();
  if (ms <= 0) {
    els.briefCountdown.textContent = "BRIEF DUE";
    return;
  }

  const totalMinutes = Math.ceil(ms / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  els.briefCountdown.textContent = hours > 0 ? `BRIEF ${hours}H ${minutes}M` : `BRIEF ${minutes}M`;
}

/** Prevent an old status-clear timer from clearing a newer preparation's status. */
let briefPlayToken = 0;
let briefPreparing = false;

function setBriefStatus(text: string, kind = ""): void {
  els.briefStatus.textContent = text;
  els.briefStatus.className = `brief-status${kind === "" ? "" : ` ${kind}`}`;
  els.briefStatus.toggleAttribute("hidden", text === "");
}

async function playBrief(): Promise<void> {
  if (briefPreparing) return;
  let audioPath = state?.brief.audioPath ?? "";
  // Lane C (voice): a fresh install has text but no clip yet. Ask for one and
  // SAY SO visibly — the old behaviour was to return silently, which reads as
  // a dead button. Pre-generation on store means this is usually a cache hit.
  if (!audioPath && state?.brief.text) {
    const bridge = window.jarvis?.voice;
    if (!bridge) return;
    briefPlayToken += 1;
    const mine = briefPlayToken;
    const wantedDate = state.brief.date;
    const wantedText = state.brief.text;
    briefPreparing = true;
    els.briefPlay.disabled = true;
    setBriefStatus("Preparing voice…");
    try {
      audioPath = (await bridge.prepareBrief(wantedDate, wantedText))?.audioPath ?? "";
    } catch {
      audioPath = "";
    } finally {
      briefPreparing = false;
      if (state) renderBrief(state);
    }
    // A newer brief landed while this was synthesizing.
    // Playing now would read yesterday's words under today's headline.
    if (state?.brief.text !== wantedText || state?.brief.date !== wantedDate) {
      setBriefStatus("");
      return;
    }
    if (!audioPath) {
      setBriefStatus("Voice unavailable", "error");
      window.setTimeout(() => {
        if (mine === briefPlayToken) setBriefStatus("");
      }, 4000);
      return;
    }
    setBriefStatus("");
  }
  if (!audioPath) {
    return;
  }

  const audio = new Audio(await resolveAudioUrl(audioPath));
  audio.preload = "auto";
  try {
    await audio.play();
  } catch (error) {
    console.warn("Brief playback was blocked or failed.", error);
  }
}

function checkBriefPlayback(): void {
  if (!state?.brief.audioPath || !state.nextBriefAt) {
    return;
  }

  const due = Date.parse(state.nextBriefAt);
  const key = `${state.brief.date}:${state.nextBriefAt}:${state.brief.audioPath}`;
  if (Number.isFinite(due) && Date.now() >= due && autoPlayedBriefKey !== key) {
    autoPlayedBriefKey = key;
    void playBrief();
  }
}

function renderApproval(request: ApprovalRequest | null): void {
  const changed = approvalRequest?.id !== request?.id;
  approvalRequest = request;
  if (!request) {
    els.approvalDialog.close();
    return;
  }
  els.approvalAgent.textContent = `${request.agent.toUpperCase()} NEEDS APPROVAL`;
  els.approvalSummary.textContent = request.summary;
  els.approvalCommand.textContent = request.command;
  els.approvalCwd.textContent = request.cwd;
  els.approvalApprove.disabled = false;
  els.approvalDeny.disabled = false;
  if (!els.approvalDialog.open) els.approvalDialog.showModal();
  if (changed) els.approvalDeny.focus();
}

function answerApproval(decision: ApprovalDecision): void {
  if (!approvalRequest) return;
  els.approvalApprove.disabled = true;
  els.approvalDeny.disabled = true;
  window.jarvis?.answerApproval(approvalRequest.id, decision);
}

async function sendChat(): Promise<void> {
  const text = els.chatInput.value.trim();
  if (!text || chatBusy) {
    return;
  }

  chatBusy = true;
  els.chatInput.value = "";
  els.chatSend.disabled = true;
  addChatBubble("user", text);
  chatMessages.push({ role: "user", content: text });

  const assistantBubble = addChatBubble("assistant", "");
  // The pre-first-token gap gets the messenger typing indicator (Peter,
  // 2026-08-14) — the first `textContent =` write below replaces it wholesale,
  // so every token/error/offline path clears it without extra bookkeeping.
  assistantBubble.append(createTypingDots(document));
  let reply = "";
  const replyGeneration = safeVoice(() => voice?.beginReply()) ?? 0;
  try {
    const response = await fetch(`http://127.0.0.1:${settings.gatewayPort}/v1/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${settings.apiKey}`,
        "X-Hermes-Session-Id": chatSessionId,
      },
      body: JSON.stringify({
        model: "hermes-agent",
        stream: true,
        messages: [{ role: "system", content: SPOKEN_REPLY_INSTRUCTION }, ...chatMessages.slice(-20)],
      }),
    });

    if (!response.ok) {
      throw new Error(`Gateway returned ${response.status}`);
    }

    let gatewayError: string | undefined;
    if (!response.body) {
      const payload = (await response.json()) as GatewayChunk;
      reply = payload.choices?.[0]?.message?.content ?? "";
      gatewayError = chunkError(payload);
      assistantBubble.textContent = stripSpeak(reply);
      safeVoice(() => voice?.onReplyToken(reply, replyGeneration));
    } else {
      const sse = await readSseReply(response, (token) => {
        reply += token;
        assistantBubble.textContent = stripSpeak(reply, true);
        safeVoice(() => voice?.onReplyToken(token, replyGeneration));
        els.chatLog.scrollTop = els.chatLog.scrollHeight;
      });
      reply = sse.text;
      gatewayError = sse.error;
    }
    if (gatewayError === undefined) safeVoice(() => voice?.onReplyEnd(replyGeneration));
    else safeVoice(() => voice?.interrupt());
    assistantBubble.textContent = stripSpeak(chatBubbleText(reply, gatewayError, noBrainLine));
    // Only the model's own words enter the transcript. chatMessages is replayed
    // to the gateway (and on to the provider) on the next turn, so a gateway
    // diagnostic pushed here would ship local paths and upstream error text
    // straight back out; a failed turn simply leaves no assistant message.
    if (reply.trim() !== "") {
      chatMessages.push({ role: "assistant", content: reply });
    }
  } catch (error) {
    const offline = error instanceof TypeError ? "The office is asleep — start the gateway" : `Gateway error: ${error instanceof Error ? error.message : "unknown"}`;
    assistantBubble.textContent = offline;
    safeVoice(() => voice?.interrupt());
  } finally {
    chatBusy = false;
    els.chatSend.disabled = false;
    els.chatInput.focus();
  }
}

function addChatBubble(role: ChatMessage["role"], text: string): HTMLElement {
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.textContent = stripSpeak(text);
  els.chatLog.append(bubble);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return bubble;
}

async function readSseReply(
  response: Response,
  onToken: (token: string) => void,
): Promise<{ text: string; error?: string }> {
  const reader = response.body?.getReader();
  if (!reader) {
    return { text: "" };
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let full = "";
  let error: string | undefined;
  const consumeData = (data: string): void => {
    const frame = parseSseData(data);
    if (frame === undefined) return;
    if (frame.error !== undefined) error = frame.error;
    if (frame.token) {
      full += frame.token;
      onToken(frame.token);
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const result = drainSseBuffer(buffer, consumeData);
    buffer = result;
  }

  buffer += decoder.decode();
  if (buffer.trim() !== "") {
    drainSseBuffer(`${buffer}\n\n`, consumeData);
  }

  return { text: full, error };
}

function drainSseBuffer(buffer: string, onData: (data: string) => void): string {
  let remaining = buffer.replace(/\r\n/g, "\n");
  let boundary = remaining.indexOf("\n\n");
  while (boundary >= 0) {
    const frame = remaining.slice(0, boundary);
    remaining = remaining.slice(boundary + 2);
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) {
      onData(data);
    }
    boundary = remaining.indexOf("\n\n");
  }
  return remaining;
}

function populateSettingsForm(): void {
  // Read-only: the engine key and port belong to the supervised child and are
  // reissued every session, so there is nothing here for a user to set.
  els.apiKey.value = settings.apiKey;
  els.gatewayPort.value = settings.gatewayPort > 0 ? String(settings.gatewayPort) : "";
  els.startAtLogin.checked = settings.startAtLogin;
  els.demoMode.checked = settings.demoMode;
  els.kioskMode.checked = settings.kiosk;
}

async function refreshSupportCode(): Promise<void> {
  if (!window.jarvis?.getDiagnostics) return;
  try {
    const report = await window.jarvis.getDiagnostics();
    els.supportCode.textContent = report.supportCode ?? "unavailable";
  } catch {
    els.supportCode.textContent = "unavailable";
  }
}

async function refreshVersion(): Promise<void> {
  if (!window.jarvis?.getVersion) return;
  try {
    els.appVersion.textContent = `v${await window.jarvis.getVersion()}`;
  } catch {
    els.appVersion.textContent = "unavailable";
  }
}

async function copyDiagnostics(): Promise<void> {
  const bridge = window.jarvis;
  if (!bridge?.getDiagnostics) return;
  try {
    const report = await bridge.getDiagnostics();
    await bridge.writeClipboardText(report.text);
    els.copyDiagnostics.textContent = "COPIED — PASTE INTO YOUR EMAIL";
  } catch {
    els.copyDiagnostics.textContent = "COPY FAILED — TRY AGAIN";
  }
  window.setTimeout(() => {
    els.copyDiagnostics.textContent = "COPY DIAGNOSTICS";
  }, 4000);
}

async function saveSettingsFromForm(): Promise<void> {
  // Lane B: the engine key and port are NOT among those fields. They belong to
  // the supervised child, are reissued every session, and the form shows them
  // read-only — sending them back would be a lie main has to strip.
  const patch = {
    startAtLogin: els.startAtLogin.checked,
    demoMode: els.demoMode.checked,
    kiosk: els.kioskMode.checked,
  };
  const next = normalizeSettings({ ...settings, ...patch });

  if (window.jarvis) {
    settings = normalizeSettings(await window.jarvis.saveSettings(patch));
  } else {
    settings = next;
    window.localStorage.setItem("jarvis-hud-settings", JSON.stringify(settings));
  }

  applySettings(settings);
  els.settingsDialog.close();
}

function applySettings(next: HudSettings): void {
  settings = next;
  els.body.classList.toggle("kiosk", next.kiosk);
  els.demoBadge.hidden = !next.demoMode;
  resetCursorTimer();
}

function resetCursorTimer(): void {
  window.clearTimeout(cursorTimer);
  els.body.classList.remove("cursor-hidden");
  if (!settings.kiosk) {
    return;
  }
  cursorTimer = window.setTimeout(() => {
    els.body.classList.add("cursor-hidden");
  }, 5_000);
}

function stateWsUrl(next: HudSettings): string {
  const port =
    Number.isInteger(next.stateServerPort) && next.stateServerPort > 0 && next.stateServerPort < 65_536
      ? next.stateServerPort
      : DEFAULT_STATE_SERVER_PORT;
  return `ws://127.0.0.1:${port}/ws`;
}

function renderBootError(error: unknown): void {
  console.error("Unable to boot JARVIS HUD.", error);
  const message = error instanceof Error ? error.message : "Unknown startup error";
  const container = document.createElement("main");
  container.style.minHeight = "100vh";
  container.style.display = "grid";
  container.style.placeItems = "center";
  container.style.padding = "32px";
  container.style.background = "#07090f";
  container.style.color = "#f3f7ff";
  container.style.fontFamily = "system-ui, sans-serif";

  const panel = document.createElement("section");
  panel.style.maxWidth = "640px";
  panel.style.border = "1px solid rgba(255, 255, 255, 0.24)";
  panel.style.borderRadius = "8px";
  panel.style.padding = "24px";
  panel.style.background = "rgba(255, 255, 255, 0.06)";

  const title = document.createElement("h1");
  title.textContent = "HUD failed to start";
  title.style.margin = "0 0 12px";
  title.style.fontSize = "24px";

  const detail = document.createElement("p");
  detail.textContent = message;
  detail.style.margin = "0";
  detail.style.lineHeight = "1.5";

  panel.append(title, detail);
  container.append(panel);
  document.body.replaceChildren(container);
}

function setConnection(text: string): void {
  els.connection.textContent = text;
  els.connection.classList.toggle("offline", text.includes("OFFLINE"));
}

async function resolveAudioUrl(value: string): Promise<string> {
  if (/^(file|https?|blob):/i.test(value)) {
    return value;
  }
  // Async by necessity: the conversion runs in the MAIN process. The sandboxed
  // preload's `url` shim has no pathToFileURL — calling it there threw on every
  // reply in v1.4.0, and the speech queue swallowed the error into silence.
  try {
    return (await window.jarvis?.pathToFileUrl(value)) ?? value;
  } catch {
    return value;
  }
}

function formatCurrency(value: number, currency: string, maximumFractionDigits: number): string {
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      maximumFractionDigits,
    }).format(value);
  } catch {
    return `$${Math.round(value).toLocaleString("en-US")}`;
  }
}

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) {
    return "--";
  }
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1_000));
  if (seconds < 45) {
    return "NOW";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}M`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}H`;
  }
  return `${Math.floor(hours / 24)}D`;
}

function cssName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
}

function must<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) {
    throw new Error(`Missing element #${id}`);
  }
  return el as T;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
