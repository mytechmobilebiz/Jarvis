import { createHash, randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import path from "node:path";
import type { Duplex } from "node:stream";
import { DEFAULT_STATE_SERVER_PORT } from "./settings.js";

export const HUD_AGENT_ROLES = ["jarvis", "sarah", "tom", "bobby", "scout", "ava"] as const;
export const HUD_AGENT_STATUSES = ["idle", "working", "waiting_approval", "offline"] as const;

export type AgentRole = (typeof HUD_AGENT_ROLES)[number];
export type AgentStatus = (typeof HUD_AGENT_STATUSES)[number];

export interface ApprovalRequest {
  id: string;
  agent: AgentRole;
  kind: "exec" | "apply_patch" | "slack_post";
  command: string;
  cwd: string;
  summary: string;
}

export type ApprovalDecision = "approve" | "deny";

export interface RevenueState {
  today: number;
  mrr: number;
  arr: number;
  target: number;
  currency: string;
}

export interface AgentState {
  status: AgentStatus;
  note: string;
  updatedAt: string;
}

export interface ActivityEntry {
  ts: string;
  role: string;
  text: string;
}

export interface HudState {
  revenue: RevenueState;
  agents: Record<AgentRole, AgentState>;
  activity: ActivityEntry[];
  approvals: {
    pending: number;
  };
  brief: {
    date: string;
    text: string;
    audioPath: string | null;
  };
  nextBriefAt: string | null;
  /** Provenance marker: true while the state holds seedDemoState fixtures. */
  demoSeeded?: boolean;
}

export interface HudStateServerOptions {
  storageDir: string;
  port?: number;
  host?: string;
  demoMode?: boolean;
  persist?: boolean;
  profileDir?: string;
  slackApiUrl?: string;
  onApprovalRequest?: (request: ApprovalRequest | null) => void;
  /**
   * Lane C (voice): fired after a brief is stored, so the HUD can pre-generate
   * spoken audio for it. Called with the stored brief; a brief that already
   * carries an audioPath needs nothing. Never awaited — a slow or failing
   * listener must not delay the tool call that stored the brief.
   */
  onBriefStored?: (brief: HudState["brief"]) => void;
}

export interface HudStateServer {
  readonly port: number;
  readonly host: string;
  start(): Promise<void>;
  stop(): Promise<void>;
  getState(): HudState;
  getApprovalRequest(): ApprovalRequest | null;
  answerApproval(id: string, decision: ApprovalDecision): void;
  setDemoMode(enabled: boolean): void;
  setMetric(key: keyof RevenueState, value: number | string): HudState;
  postActivity(role: string, text: string): HudState;
  setStatus(role: AgentRole, status: AgentStatus, note?: string): HudState;
  storeBrief(date: string, text: string, audioPath?: string | null): HudState;
  /**
   * Lane C (voice): attach audio to the brief ONLY if it is still the brief on
   * screen. Late-arriving synthesis for a superseded brief must not restore
   * stale text or move nextBriefAt.
   */
  setBriefAudioPathIfCurrent(date: string, text: string, audioPath: string): boolean;
}

type HudPatch = Partial<{
  revenue: Partial<RevenueState>;
  agents: Partial<Record<AgentRole, AgentState>>;
  activity: ActivityEntry[];
  approvals: { pending: number };
  brief: HudState["brief"];
  nextBriefAt: string | null;
}>;

type SocketMessage =
  | { type: "state"; state: HudState }
  | { type: "patch"; patch: HudPatch; state: HudState };

interface WsClient {
  readonly socket: Duplex;
  open: boolean;
  send(message: string): void;
  close(): void;
}

interface McpServerLike {
  registerTool(
    name: string,
    config: unknown,
    handler: (args: Record<string, unknown>, extra?: { signal?: AbortSignal }) => Promise<unknown> | unknown,
  ): void;
  connect(transport: McpTransportLike): Promise<void>;
  close(): Promise<void> | void;
}

interface McpTransportLike {
  close(): Promise<void> | void;
  handleRequest(req: IncomingMessage, res: ServerResponse, parsedBody?: unknown): Promise<void>;
}

interface McpDeps {
  McpServer: new (...args: unknown[]) => McpServerLike;
  StreamableHTTPServerTransport: new (...args: unknown[]) => McpTransportLike;
  z: {
    enum(values: readonly string[]): unknown;
    number(): unknown;
    string(): { optional(): unknown };
    union(values: unknown[]): unknown;
  };
}

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = DEFAULT_STATE_SERVER_PORT;
const STATE_FILE = "hud-state.json";
const ACTIVITY_LIMIT = 200;
const SAVE_DEBOUNCE_MS = 350;
const DEMO_MIN_MS = 8_000;
const DEMO_MAX_MS = 20_000;
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

let mcpDepsPromise: Promise<McpDeps | null> | null = null;

export function createHudStateServer(options: HudStateServerOptions): HudStateServer {
  return new HudStateServerImpl(options);
}

function createDefaultState(): HudState {
  const now = new Date().toISOString();
  const agents = Object.fromEntries(
    HUD_AGENT_ROLES.map((role) => [
      role,
      {
        status: "offline" satisfies AgentStatus,
        note: "No signal",
        updatedAt: now,
      },
    ]),
  ) as Record<AgentRole, AgentState>;

  return {
    revenue: {
      today: 0,
      mrr: 0,
      arr: 0,
      target: 30_000,
      currency: "USD",
    },
    agents,
    activity: [],
    approvals: {
      pending: 0,
    },
    brief: {
      date: "",
      text: "",
      audioPath: null,
    },
    nextBriefAt: null,
  };
}

class HudStateServerImpl implements HudStateServer {
  readonly port: number;
  readonly host: string;

  private readonly storageDir: string;
  private readonly profileDir: string | undefined;
  private readonly slackApiUrl: string;
  private readonly persistEnabled: boolean;
  private readonly statePath: string;
  private state: HudState = createDefaultState();
  private httpServer: Server | null = null;
  private readonly clients = new Set<WsClient>();
  private readonly pendingApprovals = new Map<string, {
    request: ApprovalRequest;
    settle: (decision: ApprovalDecision) => void;
    timer: NodeJS.Timeout;
  }>();
  private readonly approvalResponses = new Set<ServerResponse>();
  private readonly approvalListener: HudStateServerOptions["onApprovalRequest"];
  private saveTimer: NodeJS.Timeout | null = null;
  private demoTimer: NodeJS.Timeout | null = null;
  private demoMode: boolean;
  private started = false;
  /** Lane C (voice): optional brief listener. */
  private readonly briefListener: ((brief: HudState["brief"]) => void) | undefined;

  constructor(options: HudStateServerOptions) {
    this.port = options.port ?? DEFAULT_PORT;
    this.host = options.host ?? DEFAULT_HOST;
    this.storageDir = options.storageDir;
    this.profileDir = options.profileDir;
    this.slackApiUrl = options.slackApiUrl ?? "https://slack.com/api";
    this.persistEnabled = options.persist ?? true;
    this.demoMode = options.demoMode ?? false;
    this.statePath = path.join(this.storageDir, STATE_FILE);
    this.approvalListener = options.onApprovalRequest;
    this.briefListener = options.onBriefStored;
  }

  async start(): Promise<void> {
    if (this.started) {
      return;
    }

    await fs.mkdir(this.storageDir, { recursive: true });
    await this.loadState();

    if (this.demoMode) {
      this.seedDemoState(false);
    }

    this.httpServer = createServer((req, res) => {
      void this.handleHttp(req, res);
    });
    this.httpServer.on("upgrade", (req, socket, head) => {
      const url = new URL(req.url ?? "/", `http://${req.headers.host ?? `${this.host}:${this.port}`}`);
      if (url.pathname !== "/ws") {
        socket.destroy();
        return;
      }

      const client = createWsClient(req, socket);
      if (!client) {
        return;
      }

      this.clients.add(client);
      client.socket.on("close", () => {
        client.open = false;
        this.clients.delete(client);
      });
      client.socket.on("error", () => {
        client.open = false;
        this.clients.delete(client);
      });
      if (head.length > 0) {
        client.socket.unshift(head);
      }
      this.send(client, { type: "state", state: this.getState() });
    });

    await new Promise<void>((resolve, reject) => {
      const server = this.httpServer;
      if (!server) {
        reject(new Error("HTTP server was not created."));
        return;
      }

      const onError = (error: Error): void => {
        server.off("listening", onListening);
        reject(error);
      };
      const onListening = (): void => {
        server.off("error", onError);
        resolve();
      };

      server.once("error", onError);
      server.once("listening", onListening);
      server.listen(this.port, this.host);
    });

    this.started = true;
    if (this.demoMode) {
      this.scheduleDemoTick();
    }
  }

  async stop(): Promise<void> {
    this.clearDemoTimer();
    for (const response of this.approvalResponses) response.destroy();
    this.approvalResponses.clear();
    for (const { settle, timer } of this.pendingApprovals.values()) {
      clearTimeout(timer);
      settle("deny");
    }
    this.pendingApprovals.clear();
    this.approvalListener?.(null);

    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
      await this.persistNow();
    }

    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    const closeHttp = new Promise<void>((resolve, reject) => {
      if (!this.httpServer) {
        resolve();
        return;
      }

      this.httpServer.close((error) => {
        if (error && (error as NodeJS.ErrnoException).code !== "ERR_SERVER_NOT_RUNNING") {
          reject(error);
          return;
        }
        resolve();
      });
      this.httpServer = null;
    });

    await closeHttp;
    this.started = false;
  }

  getState(): HudState {
    const state = clone(this.state);
    // Approval state is transient; the underlying statuses remain on disk.
    for (const { request } of this.pendingApprovals.values()) {
      state.agents[request.agent].status = "waiting_approval";
    }
    state.approvals.pending += this.pendingApprovals.size;
    return state;
  }

  getApprovalRequest(): ApprovalRequest | null {
    return this.pendingApprovals.values().next().value?.request ?? null;
  }

  answerApproval(id: string, decision: ApprovalDecision): void {
    const pending = this.pendingApprovals.get(id);
    if (!pending || (decision !== "approve" && decision !== "deny")) return;
    const visible = this.getApprovalRequest()?.id === id;
    clearTimeout(pending.timer);
    this.pendingApprovals.delete(id);
    pending.settle(decision);
    this.broadcast({ type: "state", state: this.getState() });
    if (visible) this.approvalListener?.(this.getApprovalRequest());
  }

  private async requestApproval(req: IncomingMessage, res: ServerResponse): Promise<void> {
    let body: unknown;
    try {
      body = await readJsonBody(req);
    } catch {
      writeJson(res, 400, { error: "Invalid JSON body" });
      return;
    }
    if (!isRecord(body) || typeof body.id !== "string" || !body.id.trim()
      || !isAgentRole(body.agent) || (body.kind !== "exec" && body.kind !== "apply_patch")
      || typeof body.command !== "string" || typeof body.cwd !== "string" || typeof body.summary !== "string") {
      writeJson(res, 400, { error: "Invalid approval request" });
      return;
    }
    if (this.pendingApprovals.has(body.id)) {
      writeJson(res, 409, { error: "Approval id already pending" });
      return;
    }
    const request: ApprovalRequest = {
      id: body.id, agent: body.agent, kind: body.kind,
      command: body.command, cwd: body.cwd, summary: body.summary,
    };
    if (res.destroyed) return;
    this.approvalResponses.add(res);
    const onClose = (): void => {
      if (this.pendingApprovals.get(request.id)?.request === request) this.answerApproval(request.id, "deny");
    };
    res.on("close", onClose);
    const decision = await this.awaitApproval(request, 170_000);
    res.off("close", onClose);
    this.approvalResponses.delete(res);
    if (!res.destroyed) writeJson(res, 200, { decision });
  }

  private awaitApproval(request: ApprovalRequest, timeoutMs: number): Promise<ApprovalDecision> {
    return new Promise((settle) => {
      const timer = setTimeout(() => this.answerApproval(request.id, "deny"), timeoutMs);
      this.pendingApprovals.set(request.id, { request, settle, timer });
      this.broadcast({ type: "state", state: this.getState() });
      if (this.pendingApprovals.size === 1) this.approvalListener?.(request);
    });
  }

  setDemoMode(enabled: boolean): void {
    if (this.demoMode === enabled) {
      return;
    }

    this.demoMode = enabled;
    this.clearDemoTimer();
    if (enabled) {
      this.seedDemoState(true);
      this.scheduleDemoTick();
      return;
    }

    // Demo seeding replaced and persisted the whole state; without this reset
    // the fake MRR, approvals, and activity would keep rendering as if live
    // once the DEMO badge disappears.
    this.state = createDefaultState();
    this.persistSoon();
    this.broadcast({ type: "state", state: this.getState() });
  }

  setMetric(key: keyof RevenueState, value: number | string): HudState {
    if (!(key in this.state.revenue)) {
      throw new Error(`Unknown revenue metric: ${String(key)}`);
    }

    if (key === "currency") {
      const currency = String(value || "USD").trim().toUpperCase().slice(0, 8) || "USD";
      this.state.revenue.currency = currency;
      this.mutate({ revenue: { currency } });
      return this.getState();
    }

    const next = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(next)) {
      throw new Error(`Metric ${String(key)} must be a finite number.`);
    }

    const rounded = Math.max(0, Math.round(next * 100) / 100);
    this.state.revenue[key] = rounded;
    if (key === "mrr") {
      this.state.revenue.arr = Math.round(rounded * 12 * 100) / 100;
    }

    this.mutate({ revenue: { [key]: rounded, ...(key === "mrr" ? { arr: this.state.revenue.arr } : {}) } });
    return this.getState();
  }

  postActivity(role: string, text: string): HudState {
    const cleanRole = String(role || "jarvis").trim().slice(0, 32) || "jarvis";
    const cleanText = String(text || "").trim();
    if (!cleanText) {
      throw new Error("Activity text is required.");
    }

    const entry: ActivityEntry = {
      ts: new Date().toISOString(),
      role: cleanRole,
      text: cleanText.slice(0, 1_000),
    };
    this.state.activity = [entry, ...this.state.activity].slice(0, ACTIVITY_LIMIT);
    this.mutate({ activity: [entry] });
    return this.getState();
  }

  setStatus(role: AgentRole, status: AgentStatus, note?: string): HudState {
    if (!isAgentRole(role)) {
      throw new Error(`Unknown agent role: ${role}`);
    }
    if (!isAgentStatus(status)) {
      throw new Error(`Unknown agent status: ${status}`);
    }

    const updated: AgentState = {
      status,
      note: note == null || note.trim() === "" ? this.state.agents[role].note : note.trim().slice(0, 240),
      updatedAt: new Date().toISOString(),
    };
    this.state.agents[role] = updated;
    this.mutate({ agents: { [role]: updated } });
    return this.getState();
  }

  storeBrief(date: string, text: string, audioPath?: string | null): HudState {
    const cleanDate = String(date || new Date().toISOString().slice(0, 10)).trim();
    const cleanText = String(text || "").trim();
    if (!cleanText) {
      throw new Error("Brief text is required.");
    }

    this.state.brief = {
      date: cleanDate,
      text: cleanText.slice(0, 4_000),
      audioPath: audioPath ? String(audioPath) : null,
    };
    this.state.nextBriefAt = getNextBriefAtIso();
    this.mutate({ brief: this.state.brief, nextBriefAt: this.state.nextBriefAt });
    // Lane C (voice): notify after the state is committed and broadcast, and
    // never let a listener's failure turn a stored brief into a tool error.
    try {
      this.briefListener?.(this.state.brief);
    } catch {
      // A voice listener is a nicety; the brief itself is already stored.
    }
    return this.getState();
  }

  /**
   * Attach a spoken clip to the CURRENT brief, or do nothing.
   *
   * Synthesis of a long brief takes tens of seconds, so brief A can finish
   * after brief B has replaced it. Re-running storeBrief() would resurrect A's
   * text and recompute nextBriefAt; matching on date AND text means a
   * superseded clip is simply dropped.
   */
  setBriefAudioPathIfCurrent(date: string, text: string, audioPath: string): boolean {
    const current = this.state.brief;
    if (current.date !== String(date).trim()) return false;
    if (current.text !== String(text || "").trim().slice(0, 4_000)) return false;
    if (current.audioPath === audioPath) return true;
    this.state.brief = { ...current, audioPath };
    this.mutate({ brief: this.state.brief });
    return true;
  }

  private async handleHttp(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const origin = allowedCorsOrigin(req.headers.origin);
    if (req.headers.origin !== undefined && origin === null) {
      res.writeHead(403).end();
      return;
    }

    if (origin !== null) {
      res.setHeader("Access-Control-Allow-Origin", origin);
      res.setHeader("Vary", "Origin");
    }
    res.setHeader("Access-Control-Allow-Headers", "content-type, accept, mcp-session-id, last-event-id");
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.setHeader("Access-Control-Expose-Headers", "mcp-session-id");

    if (req.method === "OPTIONS") {
      res.writeHead(204).end();
      return;
    }

    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? `${this.host}:${this.port}`}`);
    if (url.pathname === "/approvals") {
      if (req.method !== "POST") {
        writeJson(res, 405, { error: "Method not allowed" });
        return;
      }
      await this.requestApproval(req, res);
      return;
    }
    if (url.pathname === "/state" && req.method === "GET") {
      writeJson(res, 200, this.getState());
      return;
    }

    if (url.pathname !== "/mcp") {
      writeJson(res, 404, { error: "Not found" });
      return;
    }

    // v1 intentionally has bearer auth disabled. The server binds only to
    // 127.0.0.1 so local agents can push state without credential plumbing.
    if (req.method !== "POST") {
      writeJson(res, 405, {
        jsonrpc: "2.0",
        error: { code: -32000, message: "Method not allowed." },
        id: null,
      });
      return;
    }

    let body: unknown;
    try {
      body = await readJsonBody(req);
    } catch (error) {
      writeJson(res, 400, {
        jsonrpc: "2.0",
        error: { code: -32700, message: error instanceof Error ? error.message : "Parse error" },
        id: null,
      });
      return;
    }

    await this.handleMcpRequest(req, res, body);
  }

  private async handleMcpRequest(req: IncomingMessage, res: ServerResponse, body: unknown): Promise<void> {
    const sdk = await this.createMcpServer();
    if (!sdk) {
      this.handleFallbackMcp(body, res);
      return;
    }

    const { server: mcp, transport } = sdk;
    let closePromise: Promise<void> | null = null;
    const closeSdk = (): Promise<void> => {
      closePromise ??= Promise.allSettled([transport.close(), mcp.close()]).then(() => undefined);
      return closePromise;
    };
    const onClose = (): void => {
      void closeSdk();
    };
    res.on("close", onClose);

    try {
      await mcp.connect(transport);
      await transport.handleRequest(req, res, body);
    } catch (error) {
      console.error("HUD MCP request failed.", error);
      if (!res.headersSent) {
        writeJson(res, 500, {
          jsonrpc: "2.0",
          error: { code: -32603, message: "Internal server error" },
          id: null,
        });
      }
    } finally {
      res.off("close", onClose);
      await closeSdk();
    }
  }

  private async createMcpServer(): Promise<{ server: McpServerLike; transport: McpTransportLike } | null> {
    const deps = await loadMcpDeps();
    if (!deps) {
      return null;
    }

    const { McpServer, StreamableHTTPServerTransport, z } = deps;
    const server = new McpServer({ name: "jarvis-hud", version: "0.1.0" });

    server.registerTool(
      "hud_set_metric",
      {
        title: "Set HUD Revenue Metric",
        description: "Set a revenue field on the local JARVIS HUD.",
        inputSchema: {
          key: z.enum(["today", "mrr", "arr", "target", "currency"]),
          value: z.union([z.number(), z.string()]),
        },
      },
      async ({ key, value }: Record<string, unknown>) => {
        const state = this.setMetric(key as keyof RevenueState, value as number | string);
        return mcpText({ ok: true, revenue: state.revenue });
      },
    );

    server.registerTool(
      "hud_post_activity",
      {
        title: "Post HUD Activity",
        description: "Add an activity line to the local JARVIS HUD.",
        inputSchema: {
          role: z.string(),
          text: z.string(),
        },
      },
      async ({ role, text }: Record<string, unknown>) => {
        const state = this.postActivity(String(role ?? ""), String(text ?? ""));
        return mcpText({ ok: true, activity: state.activity[0] });
      },
    );

    server.registerTool(
      "hud_set_status",
      {
        title: "Set HUD Agent Status",
        description: "Update one local JARVIS HUD agent tile.",
        inputSchema: {
          role: z.enum(HUD_AGENT_ROLES),
          status: z.enum(HUD_AGENT_STATUSES),
          note: z.string().optional(),
        },
      },
      async ({ role, status, note }: Record<string, unknown>) => {
        const agentRole = role as AgentRole;
        const state = this.setStatus(agentRole, status as AgentStatus, typeof note === "string" ? note : undefined);
        return mcpText({ ok: true, agent: state.agents[agentRole] });
      },
    );

    server.registerTool(
      "hud_store_brief",
      {
        title: "Store HUD Brief",
        description: "Store the latest daily brief for the local JARVIS HUD.",
        inputSchema: {
          date: z.string(),
          text: z.string(),
          audioPath: z.string().optional(),
        },
      },
      async ({ date, text, audioPath }: Record<string, unknown>) => {
        const state = this.storeBrief(
          String(date ?? ""),
          String(text ?? ""),
          typeof audioPath === "string" ? audioPath : undefined,
        );
        return mcpText({ ok: true, brief: state.brief, nextBriefAt: state.nextBriefAt });
      },
    );

    server.registerTool(
      "hud_slack_post",
      {
        title: "Post to Slack",
        description: "Post a message to a Slack channel as the JARVIS bot after the owner approves it in the HUD. channel is a channel ID (C…) or #name.",
        inputSchema: { channel: z.string(), text: z.string() },
      },
      async ({ channel, text }: Record<string, unknown>, extra?: { signal?: AbortSignal }) => {
        // The ChatGPT route's MCP client (Codex) abandons calls after 60 s by default, so expire the prompt first.
        const approvalTimeoutMs = 55_000;
        const id = randomUUID();
        const approval = this.awaitApproval({
          id, agent: "jarvis", kind: "slack_post",
          summary: `Post to Slack ${channel}`, command: String(text), cwd: "",
        }, approvalTimeoutMs);
        if (extra?.signal?.aborted) this.answerApproval(id, "deny");
        else extra?.signal?.addEventListener("abort", () => this.answerApproval(id, "deny"), { once: true });
        const decision = await approval;
        if (decision !== "approve") {
          return mcpText({ ok: false, error: "The owner did not approve this Slack post." });
        }
        let token = "";
        if (this.profileDir) {
          const env = await fs.readFile(path.join(this.profileDir, ".env"), "utf8").catch(() => "");
          token = /^\s*SLACK_BOT_TOKEN\s*=\s*"?([^"\r\n]+)"?\s*$/m.exec(env)?.[1]?.trim() ?? "";
        }
        if (!token) {
          return mcpText({ ok: false, error: "Slack is not connected. Connect it in HUD Settings → CONNECT SLACK." });
        }
        try {
          const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json; charset=utf-8" };
          const response = await fetch(`${this.slackApiUrl}/chat.postMessage`, {
            method: "POST", headers, body: JSON.stringify({ channel, text }),
            signal: AbortSignal.timeout(4_000),
          });
          const result = await response.json() as { ok: boolean; channel: string; ts: string; error: string };
          if (!result.ok) return mcpText({ ok: false, error: result.error });
          let permalink: string | undefined;
          try {
            const query = new URLSearchParams({ channel: result.channel, message_ts: result.ts });
            const response = await fetch(`${this.slackApiUrl}/chat.getPermalink?${query}`, { headers, signal: AbortSignal.timeout(4_000) });
            const link = await response.json() as { ok: boolean; permalink?: string };
            if (link.ok) permalink = link.permalink;
          } catch { /* The post succeeded even if its permalink is unavailable. */ }
          return mcpText({ ok: true, channel: result.channel, ts: result.ts, permalink });
        } catch (error) {
          return mcpText({ ok: false, error: error instanceof Error ? error.message : String(error) });
        }
      },
    );

    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    return { server, transport };
  }

  private handleFallbackMcp(body: unknown, res: ServerResponse): void {
    const messages = Array.isArray(body) ? body : [body];
    const replies = messages
      .map((message) => this.handleFallbackMcpMessage(message))
      .filter((reply): reply is Record<string, unknown> => reply !== null);

    writeJson(res, 200, Array.isArray(body) ? replies : replies[0] ?? null);
  }

  private handleFallbackMcpMessage(message: unknown): Record<string, unknown> | null {
    if (!isRecord(message)) {
      return rpcError(null, -32600, "Invalid Request");
    }

    const id = "id" in message ? message.id : null;
    const method = typeof message.method === "string" ? message.method : "";
    if (!("id" in message)) {
      return null;
    }

    if (method === "initialize") {
      return rpcResult(id, {
        protocolVersion: "2025-11-25",
        capabilities: { tools: {} },
        serverInfo: { name: "jarvis-hud", version: "0.1.0" },
      });
    }

    if (method === "tools/list") {
      return rpcResult(id, { tools: fallbackTools() });
    }

    if (method === "tools/call") {
      return this.handleFallbackToolCall(id, isRecord(message.params) ? message.params : {});
    }

    return rpcError(id, -32601, `Method not found: ${method}`);
  }

  private handleFallbackToolCall(id: unknown, params: Record<string, unknown>): Record<string, unknown> {
    const name = typeof params.name === "string" ? params.name : "";
    const args = isRecord(params.arguments) ? params.arguments : {};

    try {
      if (name === "hud_set_metric") {
        const key = args.key as keyof RevenueState;
        const value = args.value as number | string;
        const next = this.setMetric(key, value);
        return rpcResult(id, mcpText({ ok: true, revenue: next.revenue }));
      }

      if (name === "hud_post_activity") {
        const next = this.postActivity(String(args.role ?? ""), String(args.text ?? ""));
        return rpcResult(id, mcpText({ ok: true, activity: next.activity[0] }));
      }

      if (name === "hud_set_status") {
        const next = this.setStatus(
          args.role as AgentRole,
          args.status as AgentStatus,
          typeof args.note === "string" ? args.note : undefined,
        );
        return rpcResult(id, mcpText({ ok: true, agent: next.agents[args.role as AgentRole] }));
      }

      if (name === "hud_store_brief") {
        const next = this.storeBrief(
          String(args.date ?? ""),
          String(args.text ?? ""),
          typeof args.audioPath === "string" ? args.audioPath : undefined,
        );
        return rpcResult(id, mcpText({ ok: true, brief: next.brief, nextBriefAt: next.nextBriefAt }));
      }

      return rpcError(id, -32602, `Unknown tool: ${name}`);
    } catch (error) {
      return rpcError(id, -32602, error instanceof Error ? error.message : "Invalid tool arguments");
    }
  }

  private async loadState(): Promise<void> {
    if (!this.persistEnabled) {
      return;
    }

    try {
      const raw = await fs.readFile(this.statePath, "utf8");
      this.state = normalizeState(JSON.parse(raw));
      // A state file seeded by an earlier demo session must not render as
      // live data when the app relaunches with demo mode off.
      if (this.state.demoSeeded && !this.demoMode) {
        this.state = createDefaultState();
        this.persistSoon();
      }
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "ENOENT") {
        console.warn("Unable to load HUD state, starting fresh.", error);
      }
      this.state = createDefaultState();
    }
  }

  private persistSoon(): void {
    if (!this.persistEnabled) {
      return;
    }

    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
    }

    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      void this.persistNow();
    }, SAVE_DEBOUNCE_MS);
    this.saveTimer.unref?.();
  }

  private async persistNow(): Promise<void> {
    if (!this.persistEnabled) {
      return;
    }

    await fs.mkdir(this.storageDir, { recursive: true });
    const tmpPath = `${this.statePath}.tmp`;
    await fs.writeFile(tmpPath, `${JSON.stringify(this.state, null, 2)}\n`, "utf8");
    await fs.rename(tmpPath, this.statePath);
  }

  private mutate(patch: HudPatch): void {
    this.persistSoon();
    this.broadcast({ type: "patch", patch, state: this.getState() });
  }

  private broadcast(message: SocketMessage): void {
    for (const client of this.clients) {
      this.send(client, message);
    }
  }

  private send(socket: WsClient, message: SocketMessage): void {
    if (!socket.open) {
      return;
    }

    socket.send(JSON.stringify(message));
  }

  private seedDemoState(broadcast: boolean): void {
    const now = Date.now();
    const ts = (minutesAgo: number): string => new Date(now - minutesAgo * 60_000).toISOString();
    const yesterday = new Date(now - 24 * 60 * 60_000).toISOString().slice(0, 10);

    this.state = {
      demoSeeded: true,
      revenue: {
        today: 1_284,
        mrr: 18_200,
        arr: 218_400,
        target: 30_000,
        currency: "USD",
      },
      agents: {
        jarvis: { status: "working", note: "Coordinating morning queue", updatedAt: ts(2) },
        sarah: { status: "idle", note: "Support inbox clear", updatedAt: ts(8) },
        tom: { status: "working", note: "Fixing crash-on-launch; tests running", updatedAt: ts(4) },
        bobby: { status: "waiting_approval", note: "Budget shift needs sign-off", updatedAt: ts(12) },
        scout: { status: "idle", note: "Watching competitor shifts", updatedAt: ts(18) },
        ava: { status: "working", note: "Hooks drafted from Scout's angles", updatedAt: ts(9) },
      },
      activity: [
        { ts: ts(1), role: "jarvis", text: "MRR trend is pacing 61 percent toward target." },
        { ts: ts(5), role: "ava", text: "Wrote 5 hooks from yesterday's top comments." },
        { ts: ts(8), role: "sarah", text: "Closed 11 support replies; sentiment remains positive." },
        { ts: ts(13), role: "bobby", text: "Requested approval: shift $40 toward Morning Routine v2." },
        { ts: ts(17), role: "scout", text: "Detected two price-positioning changes in the market." },
        { ts: ts(22), role: "jarvis", text: "Daily brief queued for office playback." },
        { ts: ts(28), role: "tom", text: "Pull request open: crash-on-launch fix, tests green." },
        { ts: ts(34), role: "sarah", text: "Refund risk low; no urgent tickets waiting." },
        { ts: ts(41), role: "scout", text: "Competitor landing claim saved for review." },
        { ts: ts(49), role: "sarah", text: "FAQ draft updated from this week's objections." },
        { ts: ts(57), role: "jarvis", text: "Night automation completed without incident." },
        { ts: ts(66), role: "ava", text: "Scheduled 3 posts via Postiz, pending approval." },
      ],
      approvals: {
        pending: 1,
      },
      brief: {
        date: yesterday,
        text:
          "Yesterday closed with MRR at $18.2k, one approval pending, and support latency below target. Recommended next move: approve the checkout copy and let Tom expand the winning hook.",
        audioPath: null,
      },
      nextBriefAt: getNextBriefAtIso(),
    };

    this.persistSoon();
    if (broadcast) {
      this.broadcast({ type: "state", state: this.getState() });
    }
  }

  private scheduleDemoTick(): void {
    if (!this.demoMode || !this.started) {
      return;
    }

    const delay = DEMO_MIN_MS + Math.floor(Math.random() * (DEMO_MAX_MS - DEMO_MIN_MS));
    this.demoTimer = setTimeout(() => {
      this.demoTimer = null;
      this.runDemoTick();
      this.scheduleDemoTick();
    }, delay);
    this.demoTimer.unref?.();
  }

  private runDemoTick(): void {
    const roll = Math.random();
    if (roll < 0.42) {
      const delta = 12 + Math.random() * 120;
      this.state.revenue.mrr = Math.round((this.state.revenue.mrr + delta) * 100) / 100;
      this.state.revenue.arr = Math.round(this.state.revenue.mrr * 12 * 100) / 100;
      this.state.revenue.today = Math.round((this.state.revenue.today + delta * 0.08) * 100) / 100;
      this.mutate({
        revenue: {
          today: this.state.revenue.today,
          mrr: this.state.revenue.mrr,
          arr: this.state.revenue.arr,
        },
      });
      return;
    }

    if (roll < 0.78) {
      const lines = [
        ["jarvis", "Signal check complete; no blockers outside the approval queue."],
        ["tom", "Worktree tests green; drafting the pull request summary."],
        ["sarah", "Priority support queue remains empty."],
        ["scout", "Fresh market note added to the morning packet."],
        ["bobby", "ROAS steady at 3.1; no budget action warranted."],
        ["ava", "Two hooks queued for the afternoon slot."],
      ] as const;
      const [role, text] = lines[Math.floor(Math.random() * lines.length)] ?? lines[0];
      this.postActivity(role, text);
      return;
    }

    const role = HUD_AGENT_ROLES[Math.floor(Math.random() * HUD_AGENT_ROLES.length)] ?? "jarvis";
    const statuses: AgentStatus[] = ["idle", "working", "waiting_approval", "offline"];
    const status = statuses[Math.floor(Math.random() * statuses.length)] ?? "idle";
    const notes: Record<AgentStatus, string> = {
      idle: "Standing by",
      working: "Processing live queue",
      waiting_approval: "Needs operator approval",
      offline: "No current signal",
    };
    this.setStatus(role, status, notes[status]);
  }

  private clearDemoTimer(): void {
    if (this.demoTimer) {
      clearTimeout(this.demoTimer);
      this.demoTimer = null;
    }
  }
}

function normalizeState(raw: unknown): HudState {
  const base = createDefaultState();
  if (!isRecord(raw)) {
    return base;
  }

  return {
    revenue: normalizeRevenue(raw.revenue, base.revenue),
    agents: normalizeAgents(raw.agents, base.agents),
    activity: normalizeActivity(raw.activity),
    approvals: {
      pending: normalizeNumber(isRecord(raw.approvals) ? raw.approvals.pending : undefined, 0),
    },
    brief: normalizeBrief(raw.brief, base.brief),
    nextBriefAt: typeof raw.nextBriefAt === "string" ? raw.nextBriefAt : null,
    ...(raw.demoSeeded === true ? { demoSeeded: true } : {}),
  };
}

function normalizeRevenue(raw: unknown, base: RevenueState): RevenueState {
  const source = isRecord(raw) ? raw : {};
  return {
    today: normalizeNumber(source.today, base.today),
    mrr: normalizeNumber(source.mrr, base.mrr),
    arr: normalizeNumber(source.arr, base.arr),
    target: normalizeNumber(source.target, base.target),
    currency: typeof source.currency === "string" && source.currency ? source.currency : base.currency,
  };
}

function normalizeAgents(raw: unknown, base: Record<AgentRole, AgentState>): Record<AgentRole, AgentState> {
  const source = isRecord(raw) ? raw : {};
  const agents = { ...base };
  for (const role of HUD_AGENT_ROLES) {
    const candidate = source[role];
    if (!isRecord(candidate)) {
      continue;
    }

    agents[role] = {
      status: isAgentStatus(candidate.status) ? candidate.status : base[role].status,
      note: typeof candidate.note === "string" ? candidate.note : base[role].note,
      updatedAt: typeof candidate.updatedAt === "string" ? candidate.updatedAt : base[role].updatedAt,
    };
  }
  return agents;
}

function normalizeActivity(raw: unknown): ActivityEntry[] {
  if (!Array.isArray(raw)) {
    return [];
  }

  return raw
    .filter(isRecord)
    .map((entry) => ({
      ts: typeof entry.ts === "string" ? entry.ts : new Date().toISOString(),
      role: typeof entry.role === "string" ? entry.role : "jarvis",
      text: typeof entry.text === "string" ? entry.text : "",
    }))
    .filter((entry) => entry.text.length > 0)
    .slice(0, ACTIVITY_LIMIT);
}

function normalizeBrief(raw: unknown, base: HudState["brief"]): HudState["brief"] {
  const source = isRecord(raw) ? raw : {};
  return {
    date: typeof source.date === "string" ? source.date : base.date,
    text: typeof source.text === "string" ? source.text : base.text,
    audioPath: typeof source.audioPath === "string" ? source.audioPath : null,
  };
}

function normalizeNumber(value: unknown, fallback: number): number {
  const next = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(next) ? next : fallback;
}

function allowedCorsOrigin(origin: string | string[] | undefined): string | null {
  if (typeof origin !== "string") {
    return null;
  }

  if (origin === "null" || origin.toLowerCase().startsWith("file://")) {
    return origin;
  }

  try {
    const url = new URL(origin);
    if (
      (url.protocol === "http:" || url.protocol === "https:") &&
      (url.hostname === "127.0.0.1" || url.hostname === "localhost")
    ) {
      return origin;
    }
  } catch {
    return null;
  }

  return null;
}

function isAgentRole(value: unknown): value is AgentRole {
  return typeof value === "string" && HUD_AGENT_ROLES.includes(value as AgentRole);
}

function isAgentStatus(value: unknown): value is AgentStatus {
  return typeof value === "string" && HUD_AGENT_STATUSES.includes(value as AgentStatus);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function mcpText(data: unknown): { content: Array<{ type: "text"; text: string }>; structuredContent: unknown } {
  return {
    content: [{ type: "text", text: JSON.stringify(data) }],
    structuredContent: data,
  };
}

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.byteLength;
    if (size > 1_000_000) {
      throw new Error("Request body too large.");
    }
    chunks.push(buffer);
  }

  const body = Buffer.concat(chunks).toString("utf8").trim();
  return body ? JSON.parse(body) : undefined;
}

function writeJson(res: ServerResponse, status: number, value: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(value));
}

function getNextBriefAtIso(): string {
  const next = new Date();
  next.setHours(9, 0, 0, 0);
  if (next.getTime() <= Date.now()) {
    next.setDate(next.getDate() + 1);
  }
  return next.toISOString();
}

async function loadMcpDeps(): Promise<McpDeps | null> {
  if (!mcpDepsPromise) {
    mcpDepsPromise = (async () => {
      try {
        const mcpModuleName = "@modelcontextprotocol/sdk/server/mcp.js";
        const transportModuleName = "@modelcontextprotocol/sdk/server/streamableHttp.js";
        const zodModuleName = "zod/v4";
        const [mcpModule, transportModule, zodModule] = (await Promise.all([
          import(mcpModuleName),
          import(transportModuleName),
          import(zodModuleName),
        ])) as Array<Record<string, unknown>>;

        if (
          typeof mcpModule.McpServer !== "function" ||
          typeof transportModule.StreamableHTTPServerTransport !== "function"
        ) {
          throw new Error("MCP SDK exports were not found.");
        }

        return {
          McpServer: mcpModule.McpServer as McpDeps["McpServer"],
          StreamableHTTPServerTransport:
            transportModule.StreamableHTTPServerTransport as McpDeps["StreamableHTTPServerTransport"],
          z: zodModule as unknown as McpDeps["z"],
        };
      } catch (error) {
        console.warn("MCP SDK unavailable; using limited local MCP fallback.", error);
        return null;
      }
    })();
  }

  return mcpDepsPromise;
}

function createWsClient(req: IncomingMessage, socket: Duplex): WsClient | null {
  const key = req.headers["sec-websocket-key"];
  if (typeof key !== "string" || String(req.headers.upgrade ?? "").toLowerCase() !== "websocket") {
    socket.destroy();
    return null;
  }

  const accept = createHash("sha1").update(`${key}${WS_GUID}`).digest("base64");
  socket.write(
    [
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "",
      "",
    ].join("\r\n"),
  );

  const client: WsClient = {
    socket,
    open: true,
    send(message: string): void {
      if (!client.open) {
        return;
      }
      socket.write(encodeWsTextFrame(message));
    },
    close(): void {
      if (!client.open) {
        return;
      }
      client.open = false;
      socket.end(Buffer.from([0x88, 0x00]));
    },
  };

  socket.on("data", (chunk: Buffer) => {
    if (chunk.length > 0 && (chunk[0] & 0x0f) === 0x08) {
      client.close();
    }
  });

  return client;
}

function encodeWsTextFrame(message: string): Buffer {
  const payload = Buffer.from(message, "utf8");
  if (payload.length < 126) {
    return Buffer.concat([Buffer.from([0x81, payload.length]), payload]);
  }

  if (payload.length <= 65_535) {
    const header = Buffer.allocUnsafe(4);
    header[0] = 0x81;
    header[1] = 126;
    header.writeUInt16BE(payload.length, 2);
    return Buffer.concat([header, payload]);
  }

  const header = Buffer.allocUnsafe(10);
  header[0] = 0x81;
  header[1] = 127;
  header.writeBigUInt64BE(BigInt(payload.length), 2);
  return Buffer.concat([header, payload]);
}

function fallbackTools(): Array<Record<string, unknown>> {
  return [
    {
      name: "hud_set_metric",
      description: "Set a revenue field on the local JARVIS HUD.",
      inputSchema: {
        type: "object",
        properties: {
          key: { type: "string", enum: ["today", "mrr", "arr", "target", "currency"] },
          value: { anyOf: [{ type: "number" }, { type: "string" }] },
        },
        required: ["key", "value"],
      },
    },
    {
      name: "hud_post_activity",
      description: "Add an activity line to the local JARVIS HUD.",
      inputSchema: {
        type: "object",
        properties: {
          role: { type: "string" },
          text: { type: "string" },
        },
        required: ["role", "text"],
      },
    },
    {
      name: "hud_set_status",
      description: "Update one local JARVIS HUD agent tile.",
      inputSchema: {
        type: "object",
        properties: {
          role: { type: "string", enum: HUD_AGENT_ROLES },
          status: { type: "string", enum: HUD_AGENT_STATUSES },
          note: { type: "string" },
        },
        required: ["role", "status"],
      },
    },
    {
      name: "hud_store_brief",
      description: "Store the latest daily brief for the local JARVIS HUD.",
      inputSchema: {
        type: "object",
        properties: {
          date: { type: "string" },
          text: { type: "string" },
          audioPath: { type: "string" },
        },
        required: ["date", "text"],
      },
    },
  ];
}

function rpcResult(id: unknown, result: unknown): Record<string, unknown> {
  return { jsonrpc: "2.0", id, result };
}

function rpcError(id: unknown, code: number, message: string): Record<string, unknown> {
  return {
    jsonrpc: "2.0",
    id,
    error: { code, message },
  };
}
