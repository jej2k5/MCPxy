import { useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play, Trash2 } from "lucide-react";
import { apiGet } from "../api/client";
import type { TrafficListResponse, TrafficRecord, TrafficStatus } from "../api/types";
import { subscribeTraffic } from "../api/sse";
import { SectionCard } from "../components/Card";
import { StatusBadge } from "../components/Badge";

const STATUS_OPTIONS: (TrafficStatus | "")[] = ["", "ok", "error", "timeout", "denied"];

export default function Traffic() {
  const [records, setRecords] = useState<TrafficRecord[]>([]);
  const [paused, setPaused] = useState(false);
  const [upstreamFilter, setUpstreamFilter] = useState("");
  const [methodFilter, setMethodFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<TrafficStatus | "">("");
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    // Initial snapshot fetch so the table is pre-populated.
    apiGet<TrafficListResponse>("/admin/api/traffic?limit=200")
      .then((r) => setRecords(r.items))
      .catch(() => {
        /* tolerate */
      });

    const ctrl = new AbortController();
    (async () => {
      try {
        for await (const evt of subscribeTraffic(ctrl.signal)) {
          if (pausedRef.current) continue;
          if (evt.type === "snapshot") {
            setRecords(evt.items);
          } else {
            setRecords((prev) => [evt.record, ...prev].slice(0, 500));
          }
        }
      } catch {
        /* stream ended */
      }
    })();
    return () => ctrl.abort();
  }, []);

  const filtered = useMemo(() => {
    return records.filter((r) => {
      if (upstreamFilter && r.upstream !== upstreamFilter) return false;
      if (methodFilter && !(r.method ?? "").includes(methodFilter)) return false;
      if (statusFilter && r.status !== statusFilter) return false;
      return true;
    });
  }, [records, upstreamFilter, methodFilter, statusFilter]);

  const uniqueUpstreams = useMemo(
    () => Array.from(new Set(records.map((r) => r.upstream))).sort(),
    [records],
  );

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Live traffic</h1>
          <p className="text-sm text-slate-400">
            Streaming from <span className="kbd">/admin/api/traffic/stream</span>
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn" onClick={() => setPaused((p) => !p)}>
            {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
            {paused ? "Resume" : "Pause"}
          </button>
          <button className="btn btn-ghost" onClick={() => setRecords([])}>
            <Trash2 className="h-4 w-4" />
            Clear
          </button>
        </div>
      </header>

      <SectionCard
        title={`${filtered.length} events`}
        action={
          <div className="flex gap-2">
            <select
              className="input !w-40"
              value={upstreamFilter}
              onChange={(e) => setUpstreamFilter(e.target.value)}
            >
              <option value="">All upstreams</option>
              {uniqueUpstreams.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
            <input
              className="input !w-40"
              placeholder="method filter"
              value={methodFilter}
              onChange={(e) => setMethodFilter(e.target.value)}
            />
            <select
              className="input !w-32"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as TrafficStatus | "")}
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s || "all status"}
                </option>
              ))}
            </select>
          </div>
        }
      >
        <div className="max-h-[600px] overflow-auto scroll-thin">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-surface-800 text-slate-400">
              <tr>
                <th className="py-2 text-left font-normal">Time</th>
                <th className="py-2 text-left font-normal">Upstream</th>
                <th className="py-2 text-left font-normal">Method</th>
                <th className="py-2 text-left font-normal">Status</th>
                <th className="py-2 text-right font-normal">Latency</th>
                <th className="py-2 text-right font-normal">In / Out</th>
                <th className="py-2 text-left font-normal">Error</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-6 text-center text-slate-500">
                    {paused ? "Paused." : "Waiting for requests…"}
                  </td>
                </tr>
              )}
              {filtered.map((r, i) => (
                <tr
                  key={`${r.timestamp}-${String(r.request_id)}-${i}`}
                  className="border-t border-surface-700/60 hover:bg-surface-700/30"
                >
                  <td className="py-1 pr-2 text-slate-400">
                    {new Date(r.timestamp * 1000).toLocaleTimeString()}
                  </td>
                  <td className="py-1 pr-2 text-accent-400">{r.upstream}</td>
                  <td className="py-1 pr-2 text-slate-200">{r.method ?? "—"}</td>
                  <td className="py-1 pr-2">
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="py-1 pr-2 text-right text-slate-200">
                    {r.latency_ms.toFixed(1)} ms
                  </td>
                  <td className="py-1 pr-2 text-right text-slate-400">
                    {r.request_bytes}/{r.response_bytes}
                  </td>
                  <td className="py-1 pr-2 text-err">{r.error_code ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SectionCard>
    </div>
  );
}
