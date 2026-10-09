import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = resolve(root, "scripts/pristine/server.js");
const targetPath = resolve(root, "src/server.js");

writeFileSync(targetPath, readFileSync(sourcePath, "utf8"));

console.log("Restored src/server.js from scripts/pristine/server.js.");
console.log("Expected symptom cleared: npm test should pass.");
