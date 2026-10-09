import { constants as fsConstants } from "node:fs";
import {
  access,
  mkdir,
  mkdtemp,
  open,
  readdir,
  rm,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import {
  basename,
  dirname,
  join,
  resolve,
} from "node:path";
import { fileURLToPath } from "node:url";

// Finding and running 7-Zip moved to lib/ when verify-win-payload-archive.mjs
// needed the same discovery order (v1.5.3) — one implementation, two gates.
import { findSevenZip, runSevenZip } from "./lib/seven-zip.mjs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const distDir = resolve(scriptDir, "..", "dist-app");
const payloadName = "app-64.7z";
const executableName = "JARVIS HUD.exe";

function isTargetInstaller(name) {
  return (
    /win-x64\.exe$/i.test(name) ||
    /^JARVIS HUD Setup .+\.exe$/i.test(name)
  );
}

async function findFilesNamed(root, expectedName) {
  let entries;

  try {
    entries = await readdir(root, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") {
      return [];
    }
    throw error;
  }

  const matches = [];
  for (const entry of entries) {
    const entryPath = join(root, entry.name);
    if (entry.isDirectory()) {
      matches.push(...await findFilesNamed(entryPath, expectedName));
    } else if (entry.isFile() && entry.name === expectedName) {
      matches.push(entryPath);
    }
  }
  return matches;
}

function machineDescription(machine) {
  const descriptions = new Map([
    [0x014c, "x86"],
    [0x8664, "x86-64"],
    [0xaa64, "ARM64"],
  ]);
  return descriptions.get(machine) ?? "unknown";
}

async function assertPe32PlusX64(executablePath) {
  const file = await open(executablePath, "r");

  try {
    const { size } = await file.stat();
    const dosHeader = Buffer.alloc(64);
    const dosRead = await file.read(dosHeader, 0, dosHeader.length, 0);

    if (dosRead.bytesRead < dosHeader.length || dosHeader.toString("ascii", 0, 2) !== "MZ") {
      throw new Error(`${executableName} does not have a valid DOS/PE header`);
    }

    const peOffset = dosHeader.readUInt32LE(0x3c);
    if (peOffset < 64 || peOffset + 26 > size) {
      throw new Error(`${executableName} has an invalid PE header offset`);
    }

    const peHeader = Buffer.alloc(26);
    const peRead = await file.read(peHeader, 0, peHeader.length, peOffset);
    if (
      peRead.bytesRead < peHeader.length ||
      !peHeader.subarray(0, 4).equals(Buffer.from([0x50, 0x45, 0x00, 0x00]))
    ) {
      throw new Error(`${executableName} does not have a valid PE signature`);
    }

    const machine = peHeader.readUInt16LE(4);
    const optionalHeaderMagic = peHeader.readUInt16LE(24);

    if (machine !== 0x8664 || optionalHeaderMagic !== 0x020b) {
      throw new Error(
        `${executableName} is machine 0x${machine.toString(16).padStart(4, "0")} ` +
          `(${machineDescription(machine)}) with optional-header magic ` +
          `0x${optionalHeaderMagic.toString(16).padStart(4, "0")}; ` +
          "expected PE32+ x86-64 (machine 0x8664, magic 0x020b)",
      );
    }
  } finally {
    await file.close();
  }
}

async function verifyInstaller(sevenZip, installerPath) {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "jarvis-win-artifact-"));
  const installerExtractDir = join(temporaryRoot, "installer");
  const payloadExtractDir = join(temporaryRoot, "payload");

  try {
    await mkdir(installerExtractDir, { recursive: true });
    // Extract the app archive WHEREVER NSIS put it. It has always lived in
    // $PLUGINSDIR, and the first real CI packaging run found zero there — the
    // path is electron-builder's internal business, while the thing we
    // actually assert (exactly ONE x64 app payload, holding exactly one
    // PE32+/x64 executable) is ours. Pinning their layout made a green
    // guarantee depend on someone else's implementation detail.
    runSevenZip(
      sevenZip,
      ["x", "-y", `-o${installerExtractDir}`, installerPath, `*${payloadName}`, "-r"],
      `could not inspect ${basename(installerPath)}`,
    );

    let payloads = await findFilesNamed(installerExtractDir, payloadName);
    if (payloads.length === 0) {
      await mkdir(payloadExtractDir, { recursive: true });
      // The CI-built NSIS embeds the app DIRECTLY rather than inside an
      // app-64.7z (electron-builder picks the layout; the listing in the error
      // below shows `JARVIS HUD.exe` and `resources\...` at the top level).
      // The container is theirs to choose; the assertion is ours, and it is
      // unchanged: exactly ONE JARVIS HUD.exe, and it must be PE32+/x64 — the
      // arm64 incident is caught either way.
      runSevenZip(
        sevenZip,
        ["x", "-y", `-o${payloadExtractDir}`, installerPath, executableName, "-r"],
        `could not inspect ${basename(installerPath)}`,
      );
      const direct = await findFilesNamed(payloadExtractDir, executableName);
      if (direct.length !== 1) {
        throw new Error(
          `${basename(installerPath)} contains neither one ${payloadName} nor exactly one `
            + `${executableName} (found ${direct.length}); the installer is not a verified x64 artifact.`,
        );
      }
      await assertPe32PlusX64(direct[0]);
      return;
    }
    if (payloads.length !== 1) {
      // Say what IS in there, so the next run explains itself rather than
      // costing another packaging cycle.
      let listing = "";
      try {
        listing = runSevenZip(sevenZip, ["l", installerPath], "could not list the installer")
          .split(/\r?\n/)
          .filter((line) => /\.(?:7z|exe|dll|zip)\b/i.test(line))
          .slice(0, 30)
          .join("\n  ");
      } catch {
        listing = "(the installer could not be listed)";
      }
      throw new Error(
        `${basename(installerPath)} does not contain exactly one ${payloadName} ` +
          `(found ${payloads.length}); the installer is not a verified x64 artifact.\n  ${listing}`,
      );
    }

    await mkdir(payloadExtractDir, { recursive: true });
    runSevenZip(
      sevenZip,
      ["x", "-y", `-o${payloadExtractDir}`, payloads[0], executableName],
      `could not inspect ${payloadName} in ${basename(installerPath)}`,
    );

    const executables = await findFilesNamed(payloadExtractDir, executableName);
    if (executables.length !== 1) {
      throw new Error(
        `${payloadName} in ${basename(installerPath)} does not contain exactly one ` +
          `${executableName} (found ${executables.length})`,
      );
    }

    await assertPe32PlusX64(executables[0]);
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
}

async function main() {
  await access(distDir, fsConstants.R_OK);
  const artifacts = (await readdir(distDir, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && isTargetInstaller(entry.name))
    .map((entry) => join(distDir, entry.name))
    .sort((left, right) => left.localeCompare(right));

  if (artifacts.length === 0) {
    throw new Error(
      `no Windows x64 installers found in ${distDir}; expected *win-x64.exe ` +
        "or JARVIS HUD Setup *.exe",
    );
  }

  const sevenZip = await findSevenZip();
  const failures = [];

  for (const artifact of artifacts) {
    try {
      await verifyInstaller(sevenZip, artifact);
      console.log(
        `[verify-win-artifact] OK: ${basename(artifact)} contains ${payloadName} ` +
          `with a PE32+ x86-64 ${executableName}`,
      );
    } catch (error) {
      failures.push(`${basename(artifact)}: ${error.message}`);
    }
  }

  if (failures.length > 0) {
    throw new Error(
      `${failures.length} Windows installer verification failure(s):\n- ` +
        failures.join("\n- "),
    );
  }

  console.log(`[verify-win-artifact] Verified ${artifacts.length} installer(s).`);
}

main().catch((error) => {
  console.error(`[verify-win-artifact] ERROR: ${error.message}`);
  process.exitCode = 1;
});
