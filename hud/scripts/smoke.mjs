
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHudStateServer } from "../dist/state-server.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const root = path.resolve(__dirname, "..");
const storageDir = await mkdtemp(path.join(tmpdir(), "jarvis-hud-smoke-"));
const port = 4871;
const mcpUrl = `http://127.0.0.1:${port}/mcp`;
const wsUrl = `ws://127.0.0.1:${port}/ws`;
const messages = [];

const server = createHudStateServer({
  storageDir,
  port,
  persist: false,
  demoMode: false,
});

try {
  await server.start();
  const ws = await connectWebSocket(wsUrl);

  const list = await postMcp({
    jsonrpc: "2.0",
    id: 1,
    method: "tools/list",
    params: {},
  });
  const tools = list?.result?.tools;
  if (!Array.isArray(tools) || !tools.some((tool) => tool.name === "hud_post_activity")) {
    throw new Error(`tools/list did not expose hud_post_activity: ${JSON.stringify(list)}`);
  }

  const call = await postMcp({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      name: "hud_post_activity",
      arguments: {
        role: "smoke",
        text: "Smoke script patched the HUD state.",
      },
    },
  });
  if (call?.result?.isError) {
    throw new Error(`tools/call returned an MCP error: ${JSON.stringify(call)}`);
  }

  const patch = await waitForPatch((message) => {
    return message.type === "patch" && Array.isArray(message.patch?.activity) && message.patch.activity[0]?.role === "smoke";
  });
  if (!patch) {
    throw new Error("WebSocket client did not receive the activity patch.");
  }

  ws.close();
  console.log("smoke ok: tools/list, tools/call, and WebSocket patch succeeded");
} finally {
  await server.stop();
  await rm(storageDir, { force: true, recursive: true });
}

function connectWebSocket(url) {
  return new Promise((resolve, reject) => {
    if (typeof WebSocket !== "function") {
      reject(new Error("This Node runtime does not expose a built-in WebSocket client."));
      return;
    }

    const ws = new WebSocket(url);
    const timer = setTimeout(() => {
      reject(new Error("Timed out connecting WebSocket."));
      ws.close();
    }, 3_000);

    ws.addEventListener("open", () => {
      clearTimeout(timer);
      resolve(ws);
    });
    ws.addEventListener("message", (event) => {
      try {
        messages.push(JSON.parse(String(event.data)));
      } catch (error) {
        reject(error);
      }
    });
    ws.addEventListener("error", () => {
      reject(new Error("WebSocket client error."));
    });
  });
}

async function postMcp(payload) {
  // The streamable-HTTP MCP transport answers with an SSE stream that stays
  // open; read until the first complete event frame, then abort.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5_000);
  try {
    const res = await fetch(mcpUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const type = res.headers.get("content-type") ?? "";
    if (type.includes("application/json")) {
      return await res.json();
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!buffer.includes("\n\n")) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
    }
    controller.abort();
    return parseMcpOutput(buffer);
  } finally {
    clearTimeout(timer);
  }
}

function parseMcpOutput(output) {
  const trimmed = output.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return JSON.parse(trimmed);
  }

  const frames = trimmed.replace(/\r\n/g, "\n").split(/\n\n+/);
  for (const frame of frames) {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n")
      .trim();
    if (data) {
      return JSON.parse(data);
    }
  }

  throw new Error(`Unable to parse MCP output: ${output}`);
}

function waitForPatch(predicate) {
  const existing = messages.find(predicate);
  if (existing) {
    return Promise.resolve(existing);
  }

  return new Promise((resolve) => {
    const started = messages.length;
    const timer = setTimeout(() => resolve(null), 3_000);
    const interval = setInterval(() => {
      const next = messages.slice(started).find(predicate);
      if (next) {
        clearInterval(interval);
        clearTimeout(timer);
        resolve(next);
      }
    }, 25);
  });
}
