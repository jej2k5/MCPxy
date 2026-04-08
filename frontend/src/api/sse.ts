import { getToken } from "./client";
import type { TrafficRecord } from "./types";

/**
 * Minimal SSE-over-fetch consumer that carries the bearer token.
 *
 * Native EventSource cannot set custom headers, so we use streaming fetch
 * and parse the `data:` lines ourselves. Yields parsed traffic records.
 */
export async function* subscribeTraffic(
  signal?: AbortSignal,
): AsyncGenerator<
  { type: "snapshot"; items: TrafficRecord[] } | { type: "record"; record: TrafficRecord }
> {
  const token = getToken();
  const res = await fetch("/admin/api/traffic/stream", {
    headers: {
      Accept: "text/event-stream",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`SSE connect failed: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "message";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let dataLine = "";
        currentEvent = "message";
        for (const line of block.split("\n")) {
          if (line.startsWith(":")) continue; // comment / heartbeat
          if (line.startsWith("event:")) {
            currentEvent = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLine += line.slice(5).trim();
          }
        }
        if (!dataLine) continue;
        try {
          const parsed = JSON.parse(dataLine);
          if (currentEvent === "snapshot" && Array.isArray(parsed?.items)) {
            yield { type: "snapshot", items: parsed.items as TrafficRecord[] };
          } else {
            yield { type: "record", record: parsed as TrafficRecord };
          }
        } catch {
          // ignore malformed event
        }
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* noop */
    }
  }
}
