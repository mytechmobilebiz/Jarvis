import path from "node:path";
import type { PreparedPackagedContent } from "./packaged-content.js";
import { PACKAGED_CONTENT_DIRECTORY } from "./resources.js";

interface PreparePackagedSetupContentOptions {
  bundledRoot: string;
  hermesRoot: string;
  prepare: (bundledRoot: string, hermesRoot: string) => Promise<PreparedPackagedContent>;
  showErrorBox: (title: string, content: string) => void;
  /**
   * True when a wizard run depends on this content, so a failure is fatal to
   * setup and worth a dialog. False on a normal launch, where the call is a
   * background repair and must stay silent.
   */
  required?: boolean;
  /**
   * True when this launch may NOT fall back to the dashboard — provider safety
   * or the 1.3.x migration forced setup, and those are the exact states where
   * opening a dashboard (and starting an engine) is the unsafe outcome. Changes
   * only what the customer is told; the caller owns the decision.
   */
  mustNotOpenDashboard?: boolean;
  reportError?: (error: unknown) => void;
}

export interface PackagedSetupContentResult {
  setupMode: boolean;
  repoRoot: string;
  /** True when the staged copy was (re)written rather than found current. */
  refreshed: boolean;
  /**
   * Whether the content is actually staged. `setupMode` says what the caller
   * asked for; this says what the disk can support, and the two must not be
   * conflated — a failure used to be reported only as "setupMode: false", which
   * a forced launch then read as permission to open the dashboard.
   */
  staged: boolean;
}

/**
 * Staging also runs on ordinary launches, not only before a wizard run. Content
 * the app ships but the staged copy lacks — the Slack manifest the walkthrough
 * points at, for one — otherwise stays missing until the buyer happens to
 * trigger setup again, which for a finished install is never.
 */
export async function preparePackagedSetupContent(
  options: PreparePackagedSetupContentOptions,
): Promise<PackagedSetupContentResult> {
  const required = options.required ?? true;
  try {
    const prepared = await options.prepare(options.bundledRoot, options.hermesRoot);
    return { setupMode: true, repoRoot: prepared.repoRoot, refreshed: prepared.refreshed, staged: true };
  } catch (error) {
    const destination = path.join(options.hermesRoot, PACKAGED_CONTENT_DIRECTORY);
    const message = error instanceof Error ? error.message : String(error);
    options.reportError?.(error);
    if (required) {
      options.showErrorBox(
        "JARVIS setup could not start",
        `JARVIS could not prepare setup content at:\n${destination}\n\n${message}\n\n${
          options.mustNotOpenDashboard === true
            // Never "the dashboard will open instead" here: this launch was
            // forced into setup precisely because opening normally would put an
            // unsafe or unmigrated engine to work.
            ? "JARVIS has not started its engine, and will not until setup can run. "
              + "Free up disk space or reinstall JARVIS, then open it again."
            : "The dashboard will open instead."
        }`,
      );
    }
    return { setupMode: false, repoRoot: options.bundledRoot, refreshed: false, staged: false };
  }
}
