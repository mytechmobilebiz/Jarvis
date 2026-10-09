#!/usr/bin/env node
/**
 * asar-extract-file.mjs — read ONE file out of an .asar, with no dependencies.
 *
 * The release gate that compares the packed app's trust anchor against the
 * workspace copy used `npx --yes @electron/asar`, and the macOS runner answered
 * `sh: asar: command not found` — a network fetch and a bin-name resolution
 * standing between us and a check that must never flake. The archive format is
 * eight bytes of header size, a JSON directory, then the payload, so reading
 * one file from it is a page of code and no supply chain at all.
 *
 *   node hud/scripts/asar-extract-file.mjs <archive.asar> <path/inside> <out>
 */
import fs from "node:fs";

function usage() {
  console.error("usage: asar-extract-file.mjs <archive.asar> <path/inside/archive> <output-file>");
  process.exit(2);
}

const [archive, wanted, output] = process.argv.slice(2);
if (archive === undefined || wanted === undefined || output === undefined) usage();

const handle = fs.openSync(archive, "r");
try {
  // Chromium pickle, verified against a real packed app rather than assumed:
  //
  //   u32 @0  = 4            (size of the field that follows)
  //   u32 @4  = headerSize   (bytes of pickle payload starting at offset 8)
  //   u32 @8  = payloadSize  (the pickle's own length prefix)
  //   u32 @12 = jsonLength   (the string's length prefix)
  //   JSON @16 .. 16+jsonLength, then padding to a 4-byte boundary
  const sizes = Buffer.alloc(8);
  fs.readSync(handle, sizes, 0, 8, 0);
  const headerSize = sizes.readUInt32LE(4);
  const headerPayload = Buffer.alloc(headerSize);
  fs.readSync(handle, headerPayload, 0, headerSize, 8);
  const jsonLength = headerPayload.readUInt32LE(4);
  const header = JSON.parse(headerPayload.toString("utf8", 8, 8 + jsonLength));
  const baseOffset = 8 + headerSize;

  let node = header;
  for (const segment of wanted.split("/")) {
    node = node?.files?.[segment];
    if (node === undefined) {
      console.error(`asar-extract-file: ${archive} has no ${wanted}`);
      process.exit(1);
    }
  }
  if (node.files !== undefined) {
    console.error(`asar-extract-file: ${wanted} is a directory`);
    process.exit(1);
  }
  if (node.unpacked === true) {
    console.error(`asar-extract-file: ${wanted} is stored unpacked beside the archive`);
    process.exit(1);
  }

  const size = Number(node.size);
  const buffer = Buffer.alloc(size);
  fs.readSync(handle, buffer, 0, size, baseOffset + Number(node.offset));
  fs.writeFileSync(output, buffer);
  console.log(`asar-extract-file: ${wanted} -> ${output} (${size} bytes)`);
} finally {
  fs.closeSync(handle);
}
