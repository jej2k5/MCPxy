import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";
import { apiGet } from "../api/client";
import type { HealthResponse, MetricsResponse } from "../api/types";
import { StatCard, SectionCard } from "../components/Card";
import { subscribeTraffic } from "../api/sse";
import type { TrafficRecord } from "../api/types";

function formatUptime(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

interface Bucket {
  t: number;
  ok: number;
  err: number;
}

export default function Overview() {
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [buckets, setBuckets] = useState<Bucket[]>(() =>
    Array.from({ length: 30 }, (_, i) => ({ t: Date.now() - (29 - i) * 2000, ok: 0, err: 0 })),
  );

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const [m, h] = await Promise.all([
          apiGet<MetricsResponse>("/admin/api/metrics"),
          apiGet<HealthResponse>("/health"),
        ]);
        if (!cancelled) {
          setMetrics(m);
          setHealth(h);
        }
      } catch {
        /* tolerate transient errors */
      }
    }
    refresh();
    const iv = setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        for await (const evt of subscribeTraffic(ctrl.signal)) {
          if (evt.type !== "record") continue;
          const rec = evt.record as TrafficRecord;
          setBuckets((prev) => {
            const now = Date.now();
            const bucketIndex = prev.length - 1;
            const lastBucket = prev[bucketIndex];
            const isOk = rec.status === "ok";
            if (now - lastBucket.t < 2000) {
              const updated = [...prev];
              updated[bucketIndex] = {
                ...lastBucket,
                ok: lastBucket.ok + (isOk ? 1 : 0),
                err: lastBucket.err + (isOk ? 0 : 1),
              };
              return updated;
            }
            const next = prev.slice(1);
            next.push({ t: now, ok: isOk ? 1 : 0, err: isOk ? 0 : 1 });
            return next;
          });
        }
      } catch {
        /* stream ended */
      }
    })();
    return () => ctrl.abort();
  }, []);

  const upstreamCount = health ? Object.keys(health.upstreams).length : 0;
  const errorRate = metrics ? `${(metrics.error_rate * 100).toFixed(1)}%` : "—";
  const p95 = metrics ? `${metrics.latency_p95_ms.toFixed(0)} ms` : "—";
  const totalReqs = metrics ? metrics.total.toLocaleString() : "—";
  const uptime = health ? formatUptime(health.uptime_s) : "—";

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Overview</h1>
          <p className="text-sm text-slate-400">
            Live snapshot of MCPy health and traffic
          </p>
        </div>
        <div className="text-xs text-slate-500">
          {health?.version && <span>v{health.version}</span>}
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Uptime" value={uptime} hint={`${upstreamCount} upstreams`} />
        <StatCard
          label="Requests (5m)"
          value={totalReqs}
          hint={metrics ? `${metrics.errors} errors` : undefined}
        />
        <StatCard label="Error rate" value={errorRate} hint="last 5 minutes" />
        <StatCard label="p95 latency" value={p95} hint="last 5 minutes" />
      </div>

      <SectionCard title="Traffic (last minute)">
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={buckets}>
              <CartesianGrid stroke="#2a3142" strokeDasharray="3 3" />
              <XAxis
                dataKey="t"
                tickFormatter={(t) => new Date(t).toLocaleTimeString().slice(3, 8)}
                stroke="#6b7280"
                fontSize={11}
              />
              <YAxis allowDecimals={false} stroke="#6b7280" fontSize={11} width={30} />
              <Tooltip
                contentStyle={{
                  background: "#161a24",
                  border: "1px solid #2a3142",
                  borderRadius: 8,
                  fontSize: 12,
                }}
                labelFormatter={(v) => new Date(v as number).toLocaleTimeString()}
              />
              <Line type="monotone" dataKey="ok" stroke="#34d399" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="err" stroke="#f87171" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </SectionCard>

      {metrics && Object.keys(metrics.per_upstream).length > 0 && (
        <SectionCard title="Per-upstream">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase tracking-wider text-slate-400">
              <tr>
                <th className="py-2 text-left font-normal">Upstream</th>
                <th className="py-2 text-right font-normal">Total</th>
                <th className="py-2 text-right font-normal">Errors</th>
                <th className="py-2 text-right font-normal">p50</th>
                <th className="py-2 text-right font-normal">p95</th>
                <th className="py-2 text-right font-normal">p99</th>
              </tr>
            </thead>
            <tbody className="text-slate-200">
              {Object.entries(metrics.per_upstream).map(([name, m]) => (
                <tr key={name} className="border-t border-surface-700">
                  <td className="py-2 font-mono text-accent-400">{name}</td>
                  <td className="py-2 text-right font-mono">{m.total}</td>
                  <td className="py-2 text-right font-mono">{m.errors}</td>
                  <td className="py-2 text-right font-mono">{m.latency_p50_ms} ms</td>
                  <td className="py-2 text-right font-mono">{m.latency_p95_ms} ms</td>
                  <td className="py-2 text-right font-mono">{m.latency_p99_ms} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SectionCard>
      )}
    </div>
  );
}
