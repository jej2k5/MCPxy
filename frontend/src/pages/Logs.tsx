import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { apiGet } from "../api/client";
import type { LogEntry } from "../api/types";
import { SectionCard } from "../components/Card";

const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"];

function levelColor(level: string): string {
  switch (level) {
    case "ERROR":
      return "text-err";
    case "WARNING":
      return "text-warn";
    case "DEBUG":
      return "text-slate-500";
    default:
      return "text-slate-200";
  }
}

export default function Logs() {
  const [items, setItems] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState("");
  const [upstream, setUpstream] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    const params = new URLSearchParams();
    if (level) params.set("level", level);
    if (upstream) params.set("upstream", upstream);
    try {
      const data = await apiGet<LogEntry[]>(`/admin/api/logs?${params.toString()}`);
      setItems(data.reverse());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load logs");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Logs</h1>
          <p className="text-sm text-slate-400">Recent in-memory log entries</p>
        </div>
        <button className="btn" onClick={refresh} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </header>

      <SectionCard
        title={`${items.length} entries`}
        action={
          <div className="flex gap-2">
            <select
              className="input !w-32"
              value={level}
              onChange={(e) => setLevel(e.target.value)}
              onBlur={refresh}
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l || "all levels"}
                </option>
              ))}
            </select>
            <input
              className="input !w-40"
              placeholder="upstream"
              value={upstream}
              onChange={(e) => setUpstream(e.target.value)}
              onBlur={refresh}
            />
          </div>
        }
      >
        {error && (
          <div className="mb-3 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            {error}
          </div>
        )}
        <div className="max-h-[600px] overflow-auto scroll-thin">
          <table className="w-full text-xs">
            <tbody className="font-mono">
              {items.length === 0 && (
                <tr>
                  <td className="py-6 text-center text-slate-500" colSpan={4}>
                    No log entries match.
                  </td>
                </tr>
              )}
              {items.map((e, i) => (
                <tr key={i} className="border-t border-surface-700/60">
                  <td className="w-32 py-1 pr-2 text-slate-500">
                    {new Date(e.timestamp * 1000).toLocaleTimeString()}
                  </td>
                  <td className={`w-16 py-1 pr-2 ${levelColor(e.level)}`}>{e.level}</td>
                  <td className="w-32 py-1 pr-2 text-accent-400">{e.upstream ?? e.logger}</td>
                  <td className="py-1 pr-2 text-slate-200">{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SectionCard>
    </div>
  );
}
