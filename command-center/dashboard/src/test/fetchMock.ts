import { vi } from "vitest";

export type Route = (body: unknown, init: RequestInit) => { status?: number; json: unknown } | undefined;

/** Installs a fetch mock answering "METHOD /path" keys; records every call. */
export function mockFetch(routes: Record<string, Route | unknown>) {
  const calls: { method: string; path: string; init: RequestInit; body: unknown }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname + url.search, init, body });
    const key = Object.keys(routes).find((k) => {
      const [m, p] = k.split(" ");
      return m === method && (p === url.pathname || p === url.pathname + url.search || (p.endsWith("*") && url.pathname.startsWith(p.slice(0, -1))));
    });
    if (!key) return new Response(JSON.stringify({ detail: `no mock for ${method} ${url.pathname}` }), { status: 404 });
    const route = routes[key];
    // A function computes the reply; an object with a `json` key is a full reply
    // (optionally with `status`); anything else is the JSON body of a 200.
    const result =
      typeof route === "function"
        ? (route as Route)(body, init)
        : route && typeof route === "object" && "json" in (route as object)
          ? (route as { status?: number; json: unknown })
          : { json: route };
    return new Response(JSON.stringify(result?.json ?? {}), { status: result?.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}
