import express from "express";
import { fileURLToPath } from "node:url";

export const todos = [
  { id: 1, title: "Wire Sarah to the support queue", complete: false },
  { id: 2, title: "Ask Tom to inspect the demo workflow", complete: false },
];

export function healthHandler(_request, response) {
  return response.status(200).json({ ok: true });
}

export function todosHandler(_request, response) {
  return response.status(200).json({ todos });
}

export const app = express();

app.get("/health", healthHandler);
app.get("/todos", todosHandler);

const isDirectRun = process.argv[1] === fileURLToPath(import.meta.url);

if (isDirectRun) {
  const port = Number(process.env.PORT ?? 3000);
  app.listen(port, () => {
    console.log(`Sample app listening on http://localhost:${port}`);
  });
}
