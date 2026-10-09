/**
 * The provider-safety revision, alone in its own module.
 *
 * The authorization gate needs it, and provider-safety.ts needs the gateway
 * restart machinery — importing the whole of provider-safety.ts into the gate
 * would make a cycle (authorization → provider-safety → gateway-restart →
 * context). One constant, no cycle.
 *
 * Bump when the safety rules change: a mismatch re-runs the migration AND
 * de-authorizes the gateway until the new pass completes.
 */
/**
 * REVISION 2 (2026-08-14) — the Claude Code credential rules.
 *
 * Revision 1 cleared the Codex route and the unkeyed-Anthropic seed. It did not
 * cover what a MEASURED experiment then showed: the engine resolves the
 * customer's `~/.claude/.credentials.json` ABOVE their own pasted
 * ANTHROPIC_API_KEY, and refreshing it rewrites that file without
 * `subscriptionType` — silently downgrading a product they bought from someone
 * else (upstream #83338).
 *
 * Bumping the revision re-runs safety on every existing profile, which is the
 * point: a customer who pasted a key months ago and installed Claude Code
 * yesterday is exposed, and nothing about their profile changed to announce it.
 */
export const PROVIDER_SAFETY_REVISION = 2;
