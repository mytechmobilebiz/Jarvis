// Drives the agent history panel end to end under Electron: opens the dashboard,
// clicks an agent tile, pages the session list, walks into a transcript, switches
// agents, and asserts what rendered. Setup composes the whole household into the
// single `jarvis` profile, so the fixture models one shared archive — and it is
// built in a temp dir; the operator's Hermes home is never opened by this script.
import { BrowserWindow, app, ipcMain } from "electron";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { getTranscript, listSessions } from "../dist/history.js";
import { DEFAULT_SETTINGS } from "../dist/settings.js";
import { createHudStateServer } from "../dist/state-server.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const profilesDir = mkdtempSync(path.join(os.tmpdir(), "jarvis-hud-history-smoke-"));
const storageDir = mkdtempSync(path.join(os.tmpdir(), "jarvis-hud-history-state-"));
const settings = { ...DEFAULT_SETTINGS, stateServerPort: 4872 };
const problems = [];
// Hermes assigns these ids at `cron create`; jobs.json maps them back to the
// `--name` the wizard registered for each staff member.
const JOB_IDS = { brief: "0209a1abb7b7", triage: "aa11bb22cc33" };

const LIST_REPORT = `(() => {
  const rows = [...document.querySelectorAll("#history-body .session:not(.load-more)")];
  if (rows.length === 0) return null;
  const markers = { DELEGATED: 0, BRANCH: 0, CONTINUED: 0 };
  for (const row of rows) {
    for (const tag of row.querySelectorAll(".delegated, .lineage")) {
      if (markers[tag.textContent] !== undefined) markers[tag.textContent] += 1;
    }
  }
  return {
    open: document.getElementById("history").classList.contains("open"),
    agent: document.getElementById("history-agent").textContent,
    subtitle: document.getElementById("history-subtitle").textContent,
    titles: rows.map((row) => row.querySelector(".session-title").textContent),
    count: rows.length,
    loadMore: document.querySelector("#history-body .load-more") !== null,
    markers,
  };
})()`;

app.commandLine.appendSwitch("disable-gpu");

seedComposedProfile();

const server = createHudStateServer({ storageDir, port: settings.stateServerPort, persist: false, demoMode: false });

app.whenReady().then(async () => {
  await server.start();

  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(root, "dist", "preload.cjs"),
    },
  });

  ipcMain.handle("settings:get", () => settings);
  ipcMain.handle("settings:save", () => settings);
  ipcMain.handle("shell:open-external", () => undefined);
  ipcMain.handle("history:list-sessions", (_event, role, offset) =>
    listSessions(String(role ?? ""), { profilesDir, offset: typeof offset === "number" ? offset : 0 }),
  );
  ipcMain.handle("history:get-transcript", (_event, role, sessionId) =>
    getTranscript(String(role ?? ""), String(sessionId ?? ""), { profilesDir }),
  );

  window.webContents.on("console-message", (event, level, message) => {
    const text = String(event?.message ?? message ?? "");
    const severity = event?.level ?? level;
    const isError = severity === "error" || severity === 2 || severity === 3;
    if (isError && !text.includes("Electron Security Warning")) {
      problems.push(`console: ${text}`);
    }
  });
  window.webContents.on("preload-error", (_event, file, error) => {
    problems.push(`preload ${file}: ${error.message}`);
  });

  await window.loadFile(path.join(root, "src", "index.html"));

  const tiles = await waitFor(window, `document.querySelectorAll(".agent").length || null`);
  if (tiles !== 6) problems.push(`expected 6 agent tiles, saw ${tiles}`);

  await clickTile(window, "jarvis");
  const list = await waitFor(window, LIST_REPORT);

  if (list === null) problems.push("the session list never rendered");
  else {
    if (!list.open) problems.push("the history panel did not slide open");
    if (list.agent !== "JARVIS") problems.push(`panel header says ${list.agent}`);
    if (list.subtitle !== "100 SESSIONS+") problems.push(`panel subtitle says ${list.subtitle}`);
    if (list.titles[0] !== "Morning brief") problems.push(`first session is ${list.titles[0]}`);
    if (list.markers.DELEGATED !== 1) problems.push(`expected 1 DELEGATED marker, saw ${list.markers.DELEGATED}`);
    if (list.markers.BRANCH !== 1) problems.push(`expected 1 BRANCH marker, saw ${list.markers.BRANCH}`);
    if (list.markers.CONTINUED !== 1) problems.push(`expected 1 CONTINUED marker, saw ${list.markers.CONTINUED}`);
    if (!list.loadMore) problems.push("no LOAD MORE row for a household past one page");
    if (list.titles.includes("Inbox triage")) problems.push("Sarah's cron run leaked onto the Jarvis tile");
  }

  await window.webContents.executeJavaScript(`document.querySelector("#history-body .load-more").click(), true`);
  const paged = await waitFor(
    window,
    `(() => {
      const rows = document.querySelectorAll("#history-body .session:not(.load-more)").length;
      return rows > 100 ? rows : null;
    })()`,
  );
  if (paged === null) problems.push("LOAD MORE did not append the next page");

  await window.webContents.executeJavaScript(`document.querySelector("#history-body .session").click(), true`);
  const transcript = await waitFor(
    window,
    `(() => {
      const blocks = [...document.querySelectorAll("#history-body .transcript-msg")];
      if (blocks.length === 0) return null;
      return {
        speakers: blocks.map((block) => block.querySelector(".msg-meta span").textContent),
        bubbles: blocks.map((block) => block.querySelector(".chat-bubble")?.textContent ?? ""),
        toolCalls: [...document.querySelectorAll("#history-body .output summary")].map((s) => s.textContent),
        truncated: document.querySelectorAll("#history-body .msg-truncated").length,
        back: document.getElementById("history-back").hidden === false,
        markup: document.getElementById("history-body").innerHTML,
      };
    })()`,
  );

  if (transcript === null) problems.push("the transcript never rendered");
  else {
    if (!transcript.back) problems.push("the back button stayed hidden inside a transcript");
    if (transcript.speakers[0] !== "YOU") problems.push(`first speaker label is ${transcript.speakers[0]}`);
    if (!transcript.speakers.includes("JARVIS")) problems.push("no assistant bubble was labelled JARVIS");
    if (!transcript.toolCalls.some((text) => text.includes("TERMINAL"))) problems.push("the tool call block is missing");
    if (transcript.truncated !== 1) problems.push(`expected 1 truncated message marker, saw ${transcript.truncated}`);
    if (transcript.markup.includes("sk-live-do-not-leak")) problems.push("a secret reached the renderer unredacted");
    if (!transcript.bubbles.some((text) => text.includes("[redacted-token]"))) problems.push("redaction never applied");
  }

  await window.webContents.executeJavaScript(`document.getElementById("history-back").click(), true`);
  const back = await waitFor(window, `document.querySelectorAll("#history-body .session").length || null`);
  if (back === null || back < 100) problems.push(`back button did not restore the session list (saw ${back})`);

  // Clicking another tile switches the open panel to that agent.
  await clickTile(window, "sarah");
  const sarah = await waitFor(
    window,
    `(() => {
      const report = ${LIST_REPORT};
      return report !== null && report.agent === "SARAH" ? report : null;
    })()`,
  );
  if (sarah === null) problems.push("switching to Sarah never rendered her sessions");
  else {
    if (sarah.count !== 1) problems.push(`expected 1 session on Sarah's tile, saw ${sarah.count}`);
    if (sarah.titles[0] !== "Inbox triage") problems.push(`Sarah's session is ${sarah.titles[0]}`);
    if (sarah.loadMore) problems.push("a LOAD MORE row appeared for a single-session agent");
  }

  await window.webContents.executeJavaScript(`document.querySelector("#history-body .session").click(), true`);
  const sarahTranscript = await waitFor(
    window,
    `(() => {
      const labels = [...document.querySelectorAll("#history-body .transcript-msg .msg-meta span")].map((s) => s.textContent);
      return labels.length > 0 ? labels : null;
    })()`,
  );
  if (sarahTranscript === null || !sarahTranscript.includes("SARAH")) {
    problems.push("Sarah's cron transcript did not render under her own name");
  }

  // Tom has no schedule of his own: an available-but-empty archive, not a
  // missing profile.
  await clickTile(window, "tom");
  const tom = await waitFor(window, `document.querySelector("#history-body .history-note b")?.textContent ?? null`);
  if (tom !== "NO SESSIONS") problems.push(`Tom's empty state says ${tom}`);

  await window.webContents.executeJavaScript(`document.getElementById("history-close").click(), true`);
  const closed = await window.webContents.executeJavaScript(
    `document.getElementById("history").classList.contains("open") === false`,
  );
  if (!closed) problems.push("the close button left the panel open");

  window.destroy();
  await server.stop();
  rmSync(profilesDir, { force: true, recursive: true });
  rmSync(storageDir, { force: true, recursive: true });

  if (problems.length > 0) {
    for (const problem of problems) console.error(`history smoke: ${problem}`);
    process.exitCode = 1;
  } else {
    console.log("history smoke ok: attribution, paging, transcripts, agent switch, empty state and close all rendered");
  }
  app.quit();
});

function clickTile(window, role) {
  return window.webContents.executeJavaScript(`document.querySelector('.agent[data-role="${role}"]').click(), true`);
}

async function waitFor(window, expression, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await window.webContents.executeJavaScript(expression);
    if (result !== null && result !== undefined) return result;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return null;
}

function seedComposedProfile() {
  const dir = path.join(profilesDir, "jarvis");
  mkdirSync(path.join(dir, "cron"), { recursive: true });
  writeFileSync(
    path.join(dir, "cron", "jobs.json"),
    JSON.stringify({
      jobs: [
        { id: JOB_IDS.brief, name: "morning-brief" },
        { id: JOB_IDS.triage, name: "triage-inbox" },
      ],
    }),
    "utf8",
  );

  const db = new DatabaseSync(path.join(dir, "state.db"));
  db.exec(`
    CREATE TABLE sessions (
      id TEXT PRIMARY KEY, source TEXT NOT NULL, display_name TEXT, title TEXT, model_config TEXT,
      parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
      message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
      estimated_cost_usd REAL, actual_cost_usd REAL, archived INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
      tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      compacted INTEGER NOT NULL DEFAULT 0
    );
  `);

  const now = Math.floor(Date.now() / 1_000);
  const session = db.prepare(
    `INSERT INTO sessions (id, source, display_name, title, model_config, parent_session_id, started_at, ended_at,
                           end_reason, message_count, tool_call_count, estimated_cost_usd, actual_cost_usd, archived)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );

  session.run("s-chat", "cli", null, "Morning brief", null, null, now - 600, now - 300, null, 5, 1, 0.31, 0.42, 0);
  session.run("s-archived", "cli", null, "Archived run", null, null, now - 90, null, null, 1, 0, null, null, 1);
  session.run(`cron_${JOB_IDS.triage}_20260730_090000`, "cron", null, "Inbox triage", null, null, now - 900, null, null, 1, 0, null, null, 0);

  // One of each lineage shape, plus the parents they hang off.
  session.run("s-parent-compressed", "cli", null, "Long haul", null, null, now - 2_000, now - 1_900, "compression", 4, 0, null, null, 0);
  session.run("s-parent-branched", "cli", null, "Explored", null, null, now - 2_200, now - 2_100, "branched", 4, 0, null, null, 0);
  session.run("s-delegate", "cli", null, "Subagent run", JSON.stringify({ _delegate_from: "s-chat" }), "s-chat", now - 700, null, null, 2, 0, null, null, 0);
  session.run("s-branch", "cli", null, "Branch run", JSON.stringify({ _branched_from: "s-parent-branched" }), "s-parent-branched", now - 2_050, null, null, 2, 0, null, null, 0);
  session.run("s-continued", "cli", null, "Continued run", null, "s-parent-compressed", now - 1_850, null, null, 2, 0, null, null, 0);

  // Enough older sessions that the first page fills and LOAD MORE appears.
  for (let index = 0; index < 100; index += 1) {
    session.run(`s-bulk-${index}`, "cli", null, `Bulk ${index}`, null, null, now - 10_000 - index, null, null, 1, 0, null, null, 0);
  }

  const message = db.prepare(
    "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp, active) VALUES (?, ?, ?, ?, ?, ?, ?)",
  );
  const toolCalls = JSON.stringify([
    { id: "call_1", type: "function", function: { name: "terminal", arguments: '{"command": "ls -la /tmp"}' } },
  ]);
  message.run("s-chat", "user", "Compose the brief with sk-live-do-not-leak", null, null, now - 590, 1);
  message.run("s-chat", "assistant", "", toolCalls, null, now - 580, 1);
  message.run("s-chat", "tool", '{"output": "ok"}', null, "terminal", now - 570, 1);
  message.run("s-chat", "session_meta", null, null, null, now - 565, 1);
  message.run("s-chat", "assistant", `Brief is ready, sir. ${"detail ".repeat(3_000)}`, null, null, now - 560, 1);
  message.run(`cron_${JOB_IDS.triage}_20260730_090000`, "assistant", "Eleven tickets triaged, sir.", null, null, now - 890, 1);
  db.close();
}
