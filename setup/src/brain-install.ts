import { createHash } from "node:crypto";
import { constants, promises as fs } from "node:fs";
import path from "node:path";
import { BRAIN_CLI_PATH_ENV } from "./brain-probe.js";
import type { WizardContext } from "./context.js";
import { upsertEnv } from "./env-file.js";
import { runWatchedCommand } from "./system.js";

export type BrainVendor = "codex" | "claude";
export interface BrainInstallProgress { (line: string): void }

export interface BrainInstallDeps {
  fetch: (input: string | URL, init?: RequestInit) => Promise<Response>;
  run: typeof runWatchedCommand;
  verifyIntegrity: (actualSha256: string, actualSize: number, expectedSha256: string, expectedSize: number) => boolean;
}

interface BrainDownload {
  url: string;
  name: string;
  sha256: string;
  size: number;
}

const CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases";
const CODEX_LATEST = "https://api.github.com/repos/openai/codex/releases/latest";
const MIB = 1024 * 1024;

const DEFAULT_DEPS: BrainInstallDeps = {
  fetch: globalThis.fetch,
  run: runWatchedCommand,
  verifyIntegrity: (actualSha256, actualSize, expectedSha256, expectedSize) =>
    actualSha256 === expectedSha256 && actualSize === expectedSize,
};

export async function installBrainCli(
  ctx: WizardContext,
  vendor: BrainVendor,
  onProgress: BrainInstallProgress,
  deps: Partial<BrainInstallDeps> = {},
): Promise<{ binaryPath: string }> {
  const platform = ctx.platform!;
  const installDir = path.resolve(ctx.hermesRoot, "brain", vendor);
  const binaryPath = path.join(installDir, platform === "win32" ? `${vendor}.exe` : vendor);
  const helperPath = vendor === "codex"
    ? path.join(installDir, platform === "win32" ? "codex-code-mode-host.exe" : "codex-code-mode-host")
    : undefined;
  if (ctx.dryRun) {
    onProgress(`[dry-run] would fetch the ${vendor} app${vendor === "codex" ? " and its file tools helper" : ""}`);
    return { binaryPath };
  }

  const target = downloadTarget(ctx, vendor);
  const actual = { ...DEFAULT_DEPS, ...deps };

  const binaryReady = await runnableFile(binaryPath, platform) && await provesVersion(ctx, binaryPath, actual.run);
  const helperReady = helperPath === undefined || await runnableFile(helperPath, platform);
  if (binaryReady && helperReady) {
    await recordBinary(ctx, vendor, binaryPath);
    return { binaryPath };
  }

  await fs.mkdir(installDir, { recursive: true });
  let downloads: Array<{ download: BrainDownload; target: string; helper?: true }>;
  try {
    if (vendor === "claude") {
      downloads = [{ download: await resolveClaude(target, actual.fetch, ctx.signal), target: binaryPath }];
    } else {
      const [app, helper] = await resolveCodex(target, actual.fetch, ctx.signal);
      downloads = [{ download: app, target: binaryPath }, { download: helper, target: helperPath!, helper: true }];
    }
  } catch (error) {
    if (wasAborted(ctx, error)) throw error;
    throw new Error(`JARVIS couldn't find the official ${vendor} app download. Check the internet connection and try again.`);
  }

  for (const item of downloads) {
    const partialPath = path.join(installDir, `${item.download.name}.partial`);
    await fetchVerified(ctx, vendor, item.download, partialPath, onProgress, actual.fetch, actual.verifyIntegrity);
    try {
      await placeDownload(ctx, vendor, item.download, partialPath, item.target, installDir, actual.run);
    } catch {
      await fs.rm(partialPath, { force: true });
      throw item.helper === true ? fileToolsHelperError() : startError(vendor);
    }
  }

  if (!await provesVersion(ctx, binaryPath, actual.run)) throw startError(vendor);
  if (helperPath !== undefined && !await runnableFile(helperPath, platform)) throw fileToolsHelperError();
  await recordBinary(ctx, vendor, binaryPath);
  return { binaryPath };
}

function downloadTarget(ctx: WizardContext, vendor: BrainVendor): string {
  const platform = ctx.platform!;
  const arch = ctx.arch!;
  if (platform === "darwin" && arch === "arm64") return "darwin-arm64";
  if (platform === "darwin" && arch === "x64") return "darwin-x64";
  if (platform === "win32" && arch === "x64") return "win32-x64";
  if (platform === "win32" && arch === "arm64") return "win32-arm64";
  throw new Error(`JARVIS can't fetch the ${vendor} app for this computer (${platform}).`);
}

async function resolveClaude(
  target: string,
  fetchImpl: BrainInstallDeps["fetch"],
  signal?: AbortSignal,
): Promise<BrainDownload> {
  const version = (await responseText(fetchImpl, `${CLAUDE_RELEASES}/stable`, signal)).trim();
  const manifest = JSON.parse(
    await responseText(fetchImpl, `${CLAUDE_RELEASES}/${version}/manifest.json`, signal),
  ) as { platforms?: Record<string, { checksum?: unknown; size?: unknown }> };
  const entry = manifest.platforms?.[target];
  if (
    entry === undefined
    || typeof entry.checksum !== "string"
    || !/^[0-9a-f]{64}$/i.test(entry.checksum)
    || typeof entry.size !== "number"
    || entry.size <= 0
  ) throw new Error("Claude release metadata is incomplete.");
  const name = target.startsWith("win32-") ? "claude.exe" : "claude";
  return {
    url: `${CLAUDE_RELEASES}/${version}/${target}/${name}`,
    name,
    sha256: entry.checksum.toLowerCase(),
    size: entry.size,
  };
}

async function resolveCodex(
  target: string,
  fetchImpl: BrainInstallDeps["fetch"],
  signal?: AbortSignal,
): Promise<BrainDownload[]> {
  const names: Record<string, string> = {
    "darwin-arm64": "codex-aarch64-apple-darwin.tar.gz",
    "darwin-x64": "codex-x86_64-apple-darwin.tar.gz",
    "win32-x64": "codex-x86_64-pc-windows-msvc.exe",
    "win32-arm64": "codex-aarch64-pc-windows-msvc.exe",
  };
  const helperNames: Record<string, string> = {
    "darwin-arm64": "codex-code-mode-host-aarch64-apple-darwin.tar.gz",
    "darwin-x64": "codex-code-mode-host-x86_64-apple-darwin.tar.gz",
    "win32-x64": "codex-code-mode-host-x86_64-pc-windows-msvc.exe",
    "win32-arm64": "codex-code-mode-host-aarch64-pc-windows-msvc.exe",
  };
  const release = JSON.parse(await responseText(fetchImpl, CODEX_LATEST, signal)) as {
    assets?: Array<{ name?: unknown; size?: unknown; digest?: unknown; browser_download_url?: unknown }>;
  };
  return [names[target], helperNames[target]].map((name) => {
    const asset = release.assets?.find((candidate) => candidate.name === name);
    const digest = typeof asset?.digest === "string" ? /^sha256:([0-9a-f]{64})$/i.exec(asset.digest) : null;
    if (
      asset === undefined
      || typeof asset.name !== "string"
      || typeof asset.size !== "number"
      || asset.size <= 0
      || typeof asset.browser_download_url !== "string"
      || digest === null
    ) throw new Error("Codex release metadata is incomplete.");
    return {
      url: asset.browser_download_url,
      name: asset.name,
      sha256: digest[1].toLowerCase(),
      size: asset.size,
    };
  });
}

async function responseText(
  fetchImpl: BrainInstallDeps["fetch"],
  url: string,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetchImpl(url, { signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

async function fetchVerified(
  ctx: WizardContext,
  vendor: BrainVendor,
  download: BrainDownload,
  partialPath: string,
  onProgress: BrainInstallProgress,
  fetchImpl: BrainInstallDeps["fetch"],
  verifyIntegrity: BrainInstallDeps["verifyIntegrity"],
): Promise<void> {
  let file: Awaited<ReturnType<typeof fs.open>> | undefined;
  try {
    const response = await fetchImpl(download.url, { signal: ctx.signal });
    if (!response.ok || response.body === null) throw new Error(`HTTP ${response.status}`);
    file = await fs.open(partialPath, "w", 0o600);
    const hash = createHash("sha256");
    const interval = Math.max(5 * MIB, Math.ceil(download.size / 10));
    let received = 0;
    let nextReport = interval;
    onProgress(progressLine(vendor, 0, download.size));

    for await (const raw of response.body) {
      const chunk = Buffer.from(raw);
      hash.update(chunk);
      await file.write(chunk);
      received += chunk.length;
      if (received >= nextReport) {
        onProgress(progressLine(vendor, received, download.size));
        nextReport = received + interval;
      }
    }
    await file.close();
    file = undefined;
    if (!verifyIntegrity(hash.digest("hex"), received, download.sha256, download.size)) throw new Error("integrity mismatch");
  } catch (error) {
    await file?.close().catch(() => undefined);
    await fs.rm(partialPath, { force: true });
    if (wasAborted(ctx, error)) throw error;
    throw integrityError(vendor);
  }
}

async function placeDownload(
  ctx: WizardContext,
  vendor: BrainVendor,
  download: BrainDownload,
  partialPath: string,
  binaryPath: string,
  installDir: string,
  run: BrainInstallDeps["run"],
): Promise<void> {
  const platform = ctx.platform!;
  await fs.rm(binaryPath, { force: true });
  if (vendor !== "codex" || platform === "win32") {
    await fs.rename(partialPath, binaryPath);
  } else {
    const extractedPath = path.join(installDir, download.name.replace(/\.tar\.gz$/, ""));
    await fs.rm(extractedPath, { force: true });
    try {
      const result = await run(ctx, "/usr/bin/tar", ["-xzf", partialPath, "-C", installDir], { quiet: true });
      if (result.status !== 0 || (result.end !== undefined && result.end !== "exited")) throw new Error("tar failed");
      await fs.rename(extractedPath, binaryPath);
    } finally {
      await fs.rm(partialPath, { force: true });
      await fs.rm(extractedPath, { force: true });
    }
  }
  if (platform === "darwin") await fs.chmod(binaryPath, 0o755);
}

async function runnableFile(binaryPath: string, platform: NodeJS.Platform): Promise<boolean> {
  try {
    if (!(await fs.stat(binaryPath)).isFile()) return false;
    if (platform !== "win32") await fs.access(binaryPath, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

async function provesVersion(
  ctx: WizardContext,
  binaryPath: string,
  run: BrainInstallDeps["run"],
): Promise<boolean> {
  try {
    const result = await run(ctx, binaryPath, ["--version"], { quiet: true, timeoutMs: 30_000 });
    return result.status === 0 && (result.end === undefined || result.end === "exited");
  } catch {
    return false;
  }
}

async function recordBinary(ctx: WizardContext, vendor: BrainVendor, binaryPath: string): Promise<void> {
  await upsertEnv(ctx, path.join(ctx.profileDir, ".env"), {
    [BRAIN_CLI_PATH_ENV[vendor]]: binaryPath,
  });
}

function progressLine(vendor: BrainVendor, received: number, total: number): string {
  const name = vendor === "claude" ? "Claude" : "Codex";
  return `Fetching the ${name} app… ${Math.round(received / MIB)} of ${Math.round(total / MIB)} MB`;
}

function wasAborted(ctx: WizardContext, error: unknown): boolean {
  return ctx.signal?.aborted === true || (error instanceof Error && error.name === "AbortError");
}

function integrityError(vendor: BrainVendor): Error {
  return new Error(`The download of the ${vendor} app didn't arrive intact. Check the internet connection and try again.`);
}

function startError(vendor: BrainVendor): Error {
  return new Error(`The ${vendor} app was fetched but won't start on this computer.`);
}

function fileToolsHelperError(): Error {
  return new Error("The codex app was fetched, but its file tools helper won't start on this computer.");
}
