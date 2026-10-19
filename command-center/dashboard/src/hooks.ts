import { useCallback, useEffect, useRef, useState } from "react";

/** Fetches with `load`, then again every `intervalMs` while the tab is visible (and at once when the tab comes back).
 * `error` is the last attempt's error (null after a success); `data` keeps the last good answer, so a caller that shows it
 * as current must also look at `error` and `lastSuccessAt` (ms since the epoch, null before the first success). */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastSuccessAt, setLastSuccessAt] = useState<number | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
      setLastSuccessAt(Date.now());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let busy = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (cancelled || busy) return;
      if (timer) clearTimeout(timer);
      busy = true;
      try {
        if (document.visibilityState !== "hidden") await refresh();
      } finally {
        busy = false;
      }
      if (!cancelled && intervalMs > 0) timer = setTimeout(tick, intervalMs);
    };
    // A tab that was in the background has old data: fetch now rather than at the next tick.
    const onVisible = () => {
      if (document.visibilityState === "visible" && intervalMs > 0) void tick();
    };
    void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, refresh, setData, lastSuccessAt };
}

/** The current time (ms since the epoch), updated every `intervalMs`: for labels such as "stale for 40 s". */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
