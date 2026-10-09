/**
 * Parsing for the gateway's chat-completions SSE stream. The Hermes gateway
 * reports agent failures in-band — a finish chunk with finish_reason "error"
 * (or hermes.failed) carrying `error.message` and a `hermes` envelope — and
 * the HUD used to drop those frames, so every gateway-side failure rendered
 * as a blank "No response." with the real cause visible only in the
 * gateway's server-side log.
 */

export interface GatewayChunk {
  choices?: Array<{
    delta?: { content?: string };
    message?: { content?: string };
    finish_reason?: string | null;
  }>;
  error?: { message?: string };
  hermes?: { error?: string | null; failed?: boolean };
}

export interface SseFrame {
  token: string;
  error?: string;
}

/**
 * The gateway attaches its `hermes` envelope and `error` object only when
 * finish_reason is not "stop" (api_server.py, streaming and non-streaming
 * paths alike). An agent turn that "succeeds" while producing no text is
 * therefore a silent HTTP 200 with zero content and no diagnostic — exactly
 * what a customer saw as a bare "No response." with nothing to act on
 * (support/BUGS.md, Kevin Dunn 2026-08-09). Say where the answer actually is.
 */
export const EMPTY_REPLY_MESSAGE =
  "No response — the gateway accepted the message but the agent produced no text. "
  + "Run `hermes -p jarvis logs errors -n 50` to see why, or email jarvis.agent.tech@pm.me and a real person will help.";

/**
 * The gateway reads the profile .env only at process start, so an engine
 * launched before setup saved the API key answers every message with one of
 * these two shapes until it restarts: no key AND no model config yields
 * "No LLM provider configured" (hermes agent_init), config without a key
 * yields a relayed upstream "HTTP 401: Missing Authentication header"
 * (support/BUGS.md: Poran Singh, Ross Ledford, 2026-08-10). The HUD cannot
 * see the profile .env, so the same shapes also cover a customer who never
 * entered a key (e.g. gate-skipped the Model step) — the message must offer
 * both remedies, not promise that a restart alone fixes it. The engine text
 * stays in the bubble for support diagnosis, except its interactive
 * `hermes model` advice is replaced with the GUI workflow customers can use.
 */
// Anchored on the period: Hermes also emits "No LLM provider configured for
// task=<x>" for auxiliary features whose failure says nothing about the Brain.
const NO_PROVIDER_CONFIGURED = /no llm provider configured\./i;
const STALE_CREDENTIAL = /missing authentication header/i;

// `noBrainLine` comes from main, which imports the setup package's authority.
// This renderer module cannot import a package directly: index.html is file://
// and has neither a bundler nor an import map.
export function staleCredentialMessage(gatewayError: string, noBrainLine?: string): string | undefined {
  if (NO_PROVIDER_CONFIGURED.test(gatewayError)) return noBrainLine;
  if (!STALE_CREDENTIAL.test(gatewayError)) return undefined;
  const safeGatewayError = customerSafeGatewayError(gatewayError);
  return "JARVIS's engine can't find a working key. "
    + "If you finished setup just now, restart your computer, then send your message again. "
    + "If you have not set up a provider yet — or your key has expired — open JARVIS settings "
    + "and use RUN SETUP AGAIN: the Model step will take your key again and the engine restarts with it. "
    + "If none of that helps, email jarvis.agent.tech@pm.me and a real person will help.\n\n"
    + `(engine reported: ${safeGatewayError})`;
}

export function chatBubbleText(reply: string, gatewayError: string | undefined, noBrainLine?: string): string {
  const safeGatewayError = gatewayError === undefined ? undefined : customerSafeGatewayError(gatewayError);
  if (reply.trim() !== "") {
    return safeGatewayError === undefined
      ? reply
      : `${reply}\n\n⚠ Reply interrupted — gateway error: ${safeGatewayError}`;
  }
  if (safeGatewayError === undefined) return EMPTY_REPLY_MESSAGE;
  return staleCredentialMessage(safeGatewayError, noBrainLine) ?? `Gateway error: ${safeGatewayError}`;
}

function customerSafeGatewayError(error: string): string {
  // Per-step RUN AGAIN buttons no longer exist: RUN SETUP AGAIN re-runs the
  // whole sequence and stops at the Model step for input.
  return error.replace(
    /`?hermes model`?/gi,
    "JARVIS settings → RUN SETUP AGAIN (it stops at the Model step for your key)",
  );
}

export function chunkError(payload: GatewayChunk): string | undefined {
  const message = payload.error?.message || payload.hermes?.error || "";
  if (message !== "") return message;
  if (payload.choices?.[0]?.finish_reason === "error" || payload.hermes?.failed === true) {
    return "The agent run failed inside the gateway. Check the gateway log (hermes root, logs/errors.log).";
  }
  return undefined;
}

export function parseSseData(data: string): SseFrame | undefined {
  if (data === "[DONE]") return undefined;

  try {
    const payload = JSON.parse(data) as GatewayChunk;
    const choice = payload.choices?.[0];
    const token = choice?.delta?.content ?? choice?.message?.content ?? "";
    const error = chunkError(payload);
    if (token === "" && error === undefined) return undefined;
    return error === undefined ? { token } : { token, error };
  } catch {
    return data.trim() === "" ? undefined : { token: data };
  }
}
