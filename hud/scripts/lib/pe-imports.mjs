/**
 * Minimal PE import-table reader.
 *
 * Exists to answer one release-blocking question at build time: does a Windows
 * executable we ship depend on the Visual C++ redistributable? A binary that
 * imports VCRUNTIME140.dll fails before `main()` on a clean Windows machine
 * that has never had Visual Studio on it — and CI hides that, because the
 * GitHub windows-latest image ships the redistributable.
 *
 * Pure JavaScript so it runs on the macOS dev machine too: `dumpbin` is
 * Windows-only and would make this checkable only where it is least needed.
 */

import { promises as fs } from "node:fs";

/** CRT DLLs that mean "needs the Visual C++ redistributable installed". */
export const VC_RUNTIME_DLLS = [
  "vcruntime140.dll",
  "vcruntime140_1.dll",
  "msvcp140.dll",
  "msvcp140_1.dll",
  "msvcp140_2.dll",
  "concrt140.dll",
  "vccorlib140.dll",
];

/** Every DLL named in the PE import directory, lowercased. */
export async function readImports(filePath) {
  const buffer = await fs.readFile(filePath);
  if (buffer.length < 0x40 || buffer.readUInt16LE(0) !== 0x5a4d) {
    throw new Error(`Not a PE image: ${filePath}`);
  }
  const peOffset = buffer.readUInt32LE(0x3c);
  if (buffer.readUInt32LE(peOffset) !== 0x00004550) throw new Error(`Bad PE signature: ${filePath}`);

  const coff = peOffset + 4;
  const sectionCount = buffer.readUInt16LE(coff + 2);
  const optionalSize = buffer.readUInt16LE(coff + 16);
  const optional = coff + 20;
  const magic = buffer.readUInt16LE(optional);
  const plus = magic === 0x20b; // PE32+ (x64)
  // The import directory is entry 1 of the data directories, which sit after
  // the fixed part of the optional header (96 bytes for PE32, 112 for PE32+).
  const dataDirectories = optional + (plus ? 112 : 96);
  const importRva = buffer.readUInt32LE(dataDirectories + 8);
  if (importRva === 0) return [];

  const sections = [];
  const sectionTable = optional + optionalSize;
  for (let i = 0; i < sectionCount; i += 1) {
    const entry = sectionTable + i * 40;
    sections.push({
      virtualAddress: buffer.readUInt32LE(entry + 12),
      virtualSize: buffer.readUInt32LE(entry + 8),
      rawSize: buffer.readUInt32LE(entry + 16),
      rawPointer: buffer.readUInt32LE(entry + 20),
    });
  }

  const toOffset = (rva) => {
    for (const section of sections) {
      const size = Math.max(section.virtualSize, section.rawSize);
      if (rva >= section.virtualAddress && rva < section.virtualAddress + size) {
        return section.rawPointer + (rva - section.virtualAddress);
      }
    }
    return -1;
  };

  const names = [];
  for (let entry = toOffset(importRva); entry >= 0 && entry + 20 <= buffer.length; entry += 20) {
    const nameRva = buffer.readUInt32LE(entry + 12);
    // The import descriptor array is terminated by an all-zero entry.
    if (nameRva === 0 && buffer.readUInt32LE(entry) === 0) break;
    const nameOffset = toOffset(nameRva);
    if (nameOffset < 0) break;
    const end = buffer.indexOf(0, nameOffset);
    names.push(buffer.subarray(nameOffset, end < 0 ? nameOffset : end).toString("ascii").toLowerCase());
  }
  return names;
}

/**
 * Imports that would require the VC++ redistributable. Empty means the binary
 * is statically linked against the CRT and is safe on a bare Windows install.
 */
export async function crtImports(filePath) {
  const imports = await readImports(filePath);
  return imports.filter((name) => VC_RUNTIME_DLLS.includes(name));
}
