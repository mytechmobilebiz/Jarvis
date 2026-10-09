/**
 * The instant-messenger "typing" indicator, shared by the setup wizard's voice
 * log and the HUD chat (Peter's smoke feedback, 2026-08-14: any operation that
 * can take more than a second shows that JARVIS is working — above all the
 * key verification, and the chat's pre-first-token gap).
 *
 * RENDERER-REACHABLE MODULE: loaded by file:// pages — no bare import
 * specifiers, no imports at all. The style is injected once per document so
 * both pages get the identical component without touching two stylesheets.
 */

const STYLE_ID = "typing-dots-style";

const STYLE_TEXT = `
.typing-dots { display: inline-flex; align-items: center; gap: 4px; padding: 2px 0; }
.typing-dots i {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; opacity: 0.25; font-style: normal;
  animation: typing-dots-pulse 1.2s infinite ease-in-out;
}
.typing-dots i:nth-child(2) { animation-delay: 0.2s; }
.typing-dots i:nth-child(3) { animation-delay: 0.4s; }
@keyframes typing-dots-pulse {
  0%, 60%, 100% { opacity: 0.25; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-2px); }
}
`;

export function ensureTypingDotsStyle(doc: Document): void {
  if (doc.getElementById(STYLE_ID) !== null) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = STYLE_TEXT;
  doc.head.append(style);
}

/**
 * Three pulsing dots. `label` is for screen readers only — the visual stays
 * wordless like every messenger's typing indicator.
 */
export function createTypingDots(doc: Document, label = "JARVIS is working"): HTMLElement {
  ensureTypingDotsStyle(doc);
  const dots = doc.createElement("span");
  dots.className = "typing-dots";
  dots.setAttribute("role", "status");
  dots.setAttribute("aria-label", label);
  for (let i = 0; i < 3; i += 1) dots.append(doc.createElement("i"));
  return dots;
}
