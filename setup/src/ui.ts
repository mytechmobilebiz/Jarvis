import type { StepStatus, WizardContext } from "./context.js";
import type { DeviceCodePrompt, SecretPromptGuide, SecretPromptOptions, SetupChoice } from "./io.js";
import { sanitizeSecret } from "./secret-text.js";

export const FILE_EXPLORER_ADDRESS_BAR_HINT =
  "Copy this path and paste it into File Explorer's ADDRESS BAR, then press Enter.";

export function withFileExplorerAddressBarHint(lines: string[]): string[] {
  return lines.some((line) => /[\\/]AppData[\\/]/i.test(line))
    && !lines.includes(FILE_EXPLORER_ADDRESS_BAR_HINT)
    ? [...lines, FILE_EXPLORER_ADDRESS_BAR_HINT]
    : lines;
}

export function greetingLine(): string {
  return `${timeGreeting()}, sir. Your new staff is nearly assembled.`;
}

export function timeGreeting(date = new Date()): string {
  const hour = date.getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

export function officeGreeting(date = new Date()): string {
  return `${timeGreeting(date)}, sir. The office is open — your staff and I are at your service. My morning brief arrives at 08:00.`;
}

export function note(ctx: WizardContext, message: string): void {
  ctx.io.note(message);
}

export function warn(ctx: WizardContext, message: string): void {
  ctx.io.warn(message);
}

/**
 * Surface a device-code sign-in. A dry run never spawns the engine, so it can
 * only describe what a real run would put on screen.
 */
export function deviceCode(ctx: WizardContext, prompt: DeviceCodePrompt): void {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would show a sign-in page and code for: ${prompt.message}`);
    return;
  }

  ctx.io.deviceCode(prompt);
}

export async function confirm(
  ctx: WizardContext,
  message: string,
  defaultValue: boolean,
): Promise<boolean> {
  if (ctx.dryRun) {
    const suffix = defaultValue ? "[Y/n]" : "[y/N]";
    note(ctx, `[dry-run] would ask: ${message} ${suffix}; default ${defaultValue ? "yes" : "no"}.`);
    return defaultValue;
  }

  return ctx.io.confirm(message, defaultValue);
}

export async function select<T extends string>(
  ctx: WizardContext,
  message: string,
  choices: SetupChoice<T>[],
  defaultKey: T,
): Promise<T> {
  if (ctx.dryRun) {
    const chosen = choices.find((choice) => choice.key === defaultKey);
    note(ctx, `[dry-run] would ask: ${message}; default ${chosen?.label ?? defaultKey}.`);
    return defaultKey;
  }

  const answer = await ctx.io.select(message, choices, defaultKey);
  return choices.find((choice) => choice.key === answer)?.key ?? defaultKey;
}

export async function promptText(
  ctx: WizardContext,
  message: string,
  defaultValue = "",
  guide?: SecretPromptGuide,
): Promise<string> {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would ask: ${message}; default ${defaultValue === "" ? "blank" : defaultValue}.`);
    return defaultValue;
  }

  return ctx.io.promptText(message, defaultValue, guide);
}

export async function promptSecret(
  ctx: WizardContext,
  message: string,
  guide?: SecretPromptGuide,
  options?: SecretPromptOptions,
): Promise<string> {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would ask for ${message}; input hidden; default blank.`);
    return "";
  }

  // Pasted keys arrive with stray newlines and zero-width characters that
  // .trim() cannot reach; they would reach the .env and fail as a silent 401.
  return sanitizeSecret(await ctx.io.promptSecret(message, guide, options));
}
