import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const serverPath = resolve(root, "src/server.js");

const healthyLine = "  return response.status(200).json({ todos });";
const bugLine =
  "  return response.status(200).json({ todos: todos.filter((todo) => todo.complete) });";

const server = readFileSync(serverPath, "utf8");

if (server.includes(bugLine)) {
  console.log("Seeded bug is already present.");
  console.log("Expected symptom: GET /todos returns an empty list instead of both seeded todos.");
} else if (server.includes(healthyLine)) {
  writeFileSync(serverPath, server.replace(healthyLine, bugLine));
  console.log("Seeded bug applied to src/server.js.");
  console.log("Expected symptom: GET /todos returns an empty list instead of both seeded todos.");
} else {
  console.error("Could not seed the bug: src/server.js no longer matches the expected demo shape.");
  process.exitCode = 1;
}
