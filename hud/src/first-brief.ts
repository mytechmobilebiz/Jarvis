import { promises as fs } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

/**
 * Day one shouldn't be dark. Right after the HUD boots for the first time
 * (straight out of setup), ask the freshly-installed agent to compose its
 * morning brief through the SAME local gateway the chat bar uses. Because this
 * goes through the chat path and not the cron gateway, nothing is delivered to
 * Slack — the morning-brief skill only composes its reply, calls
 * `mcp_hud_hud_store_brief` (which populates the HUD), and writes a local copy.
 *
 * It is best-effort and self-limiting: if the gateway isn't up yet or anything
 * fails, the daily cron and the empty-state copy still cover the customer, so a
 * miss is never worse than today. A marker bounds this to a handful of boots so
 * the model is never re-invoked in a loop — at most MAX_ATTEMPTS unsolicited
 * calls per install, and none once one is accepted.
 */

const MAX_ATTEMPTS = 3;
const DEFAULT_TIMEOUT_MS = 120_000;
const sessionId = randomUUID();

const BRIEF_PROMPT =
  "Please compose my very first morning brief now, exactly as your morning-brief "
  + "skill specifies, and call mcp_hud_hud_store_brief with the full text so it shows on my "
  + "HUD. This is my first run, so most sections will have no data yet — keep it "
  + "short and honest, omit any section you have no numbers for, never invent "
  + "figures. Do not send it to Slack or anywhere else; only compose it and store it.";

interface FirstBriefMarker {
  status: "pending" | "done";
  attempts: number;
}

interface FirstBriefOptions {
  configDir: string;
  gatewayPort: number;
  apiKey: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

function markerPath(configDir: string): string {
  return path.join(configDir, "first-brief.json");
}

async function readMarker(configDir: string): Promise<FirstBriefMarker | undefined> {
  try {
    const stored = JSON.parse(await fs.readFile(markerPath(configDir), "utf8")) as Partial<FirstBriefMarker>;
    return {
      status: stored.status === "done" ? "done" : "pending",
      attempts: typeof stored.attempts === "number" && stored.attempts > 0 ? Math.floor(stored.attempts) : 0,
    };
  } catch {
    return undefined;
  }
}

async function writeMarker(configDir: string, marker: FirstBriefMarker): Promise<boolean> {
  const target = markerPath(configDir);
  const tmpPath = `${target}.${process.pid}.tmp`;
  try {
    await fs.mkdir(configDir, { recursive: true });
    // Atomic: a half-written marker could otherwise be read as attempts:0 and
    // reopen the cap. tmp+rename leaves either the old marker or the new one.
    await fs.writeFile(tmpPath, `${JSON.stringify(marker, null, 2)}\n`, "utf8");
    await fs.rename(tmpPath, target);
    return true;
  } catch {
    return false;
  }
}

/**
 * Fire-and-forget: callers must NOT await this on the boot path. Returns the
 * marker status it settled on purely so tests can assert behaviour.
 *
 * The cap is enforced by a persisted counter, claimed (written) before the call.
 * In practice there is exactly one caller per install — launchHud() runs once
 * per process and guards re-entry — so the read-modify-write is sequential, the
 * cap is exact, and "done" is sticky once it persists. The only way to exceed it
 * is two JARVIS processes launched at once (this app takes no single-instance
 * lock, and its fixed gateway/state ports mean concurrent instances don't really
 * function anyway); that would cost at most a couple extra unsolicited calls,
 * still small and bounded, for a best-effort day-one nicety — deliberately not
 * worth a cross-process lock.
 */
export async function maybeGenerateFirstBrief(options: FirstBriefOptions): Promise<FirstBriefMarker["status"] | "skipped"> {
  const { configDir, gatewayPort, apiKey } = options;
  if (apiKey === "") return "skipped"; // an unpaired HUD has no gateway credential to use

  const marker = await readMarker(configDir);
  if (marker?.status === "done") return "done";
  const attempts = marker?.attempts ?? 0;
  if (attempts >= MAX_ATTEMPTS) return "skipped";

  // Claim this attempt BEFORE the call so a crash mid-brief still counts against
  // the cap — the model is never re-invoked more than MAX_ATTEMPTS times. If the
  // claim can't be persisted (unwritable dir) we skip: without a durable counter
  // the cap can't be enforced, and a per-boot model call is the worse failure.
  if (!(await writeMarker(configDir, { status: "pending", attempts: attempts + 1 }))) {
    return "skipped";
  }

  const fetchImpl = options.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  try {
    const response = await fetchImpl(`http://127.0.0.1:${gatewayPort}/v1/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${apiKey}`,
        "X-Hermes-Session-Id": sessionId,
      },
      body: JSON.stringify({
        model: "hermes-agent",
        stream: false,
        messages: [{ role: "user", content: BRIEF_PROMPT }],
      }),
      signal: controller.signal,
    });
    if (response.ok) {
      // --- Lane A (v1.4.0): a 200 is not a brief. ------------------------
      // With no provider configured the gateway answers 200 with an engine
      // envelope ("No LLM provider configured"), which used to mark the brief
      // done forever — so a customer who signed in AFTER setup never got the
      // one thing the first run promises. Require real assistant text.
      if (!(await hasBriefContent(response))) {
        return "pending";
      }
      // --- end Lane A ----------------------------------------------------
      // Only claim "done" if it actually persisted; otherwise the durable marker
      // is still "pending" and a later boot would honestly (and boundedly) retry.
      const persisted = await writeMarker(configDir, { status: "done", attempts: attempts + 1 });
      return persisted ? "done" : "pending";
    }
    // A reachable-but-erroring gateway stays pending for a later boot, bounded
    // by MAX_ATTEMPTS so a persistent 5xx can't invoke the model forever.
    return "pending";
  } catch {
    // Gateway not up yet, or the request timed out: stays pending (bounded).
    return "pending";
  } finally {
    clearTimeout(timer);
  }
}

/**
 * True only when the completion carries real assistant text.
 *
 * The gateway returns HTTP 200 for "no provider configured" and for other
 * engine-level refusals, carrying them in the body — so `response.ok` alone
 * cannot tell a brief from a diagnostic.
 */
async function hasBriefContent(response: Response): Promise<boolean> {
  try {
    const payload = (await response.json()) as {
      choices?: { message?: { content?: unknown } }[];
      error?: unknown;
      hermes?: { error?: unknown };
    };
    if (payload.error !== undefined || payload.hermes?.error !== undefined) return false;
    const content = payload.choices?.[0]?.message?.content;
    return typeof content === "string" && content.trim() !== "";
  } catch {
    // An unreadable body proves nothing, so it stays pending and bounded.
    return false;
  }
}
