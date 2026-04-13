import { useEffect, useState } from "react";
import { RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { apiGet, apiPost, apiDelete } from "../api/client";
import type { LogEntry, RouteSnapshot } from "../api/types";
import { SectionCard } from "../components/Card";
import { Badge } from "../components/Badge";

function levelColor(level: string): string {
  switch (level) {
    case "ERROR": return "text-err";
    case "WARNING": return "text-warn";
    case "DEBUG": return "text-slate-500";
    default: return "text-slate-300";
  }
}

function UpstreamLogs({ name }: { name: string }) {
  const [logs, setLogs] = useState<LogEntry[] | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const data = await apiGet<LogEntry[]>(
        `/admin/api/logs?upstream=${encodeURIComponent(name)}`,
      );
      setLogs(data.reverse());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load logs");
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next) load();
  }

  return (
    <div className="mt-4 text-xs text-slate-400">
      <button
        className="cursor-pointer select-none hover:text-slate-200"
        onClick={toggle}
      >
        {open ? "\u25BC" : "\u25B6"} Logs
        {logs !== null && ` (${logs.length})`}
      </button>
      {open && (
        <div className="mt-2 max-h-60 overflow-auto scroll-thin rounded-md bg-surface-900 p-2">
          {error && <p className="text-err">{error}</p>}
          {logs !== null && logs.length === 0 && (
            <p className="text-slate-500">No log entries for this upstream.</p>
          )}
          {logs !== null && logs.length > 0 && (
            <table className="w-full">
              <tbody className="font-mono text-[11px]">
                {logs.map((entry, i) => (
                  <tr key={i} className="border-t border-surface-700/40 first:border-0">
                    <td className="w-20 py-0.5 pr-2 text-slate-500 whitespace-nowrap">
                      {new Date(entry.timestamp * 1000).toLocaleTimeString()}
                    </td>
                    <td className={`w-14 py-0.5 pr-2 ${levelColor(entry.level)}`}>
                      {entry.level}
                    </td>
                    <td className="py-0.5 text-slate-200 break-all">{entry.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

export default function RoutesPage() {
  const [routes, setRoutes] = useState<RouteSnapshot>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const data = await apiGet<RouteSnapshot>("/admin/api/routes");
      setRoutes(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load routes");
    }
  }

  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 8000);
    return () => clearInterval(iv);
  }, []);

  async function rediscover() {
    setBusy("__refresh__");
    try {
      const data = await apiPost<RouteSnapshot>("/admin/api/routes/refresh", {});
      setRoutes(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rediscovery failed");
    } finally {
      setBusy(null);
    }
  }

  async function restart(name: string) {
    setBusy(name);
    try {
      await apiPost("/admin/api/restart", { name });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Restart failed");
    } finally {
      setBusy(null);
    }
  }

  async function remove(name: string) {
    if (!confirm(`Remove upstream '${name}'? It will be unregistered and its process stopped.`)) return;
    setBusy(name);
    try {
      await apiDelete(`/admin/api/upstreams/${encodeURIComponent(name)}`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Remove failed");
    } finally {
      setBusy(null);
    }
  }

  const entries = Object.entries(routes);

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Routes</h1>
          <p className="text-sm text-slate-400">
            Upstream MCP servers and their discovered tools
          </p>
        </div>
        <button className="btn" onClick={rediscover} disabled={busy === "__refresh__"}>
          <RefreshCw className={`h-4 w-4 ${busy === "__refresh__" ? "animate-spin" : ""}`} />
          Rediscover tools
        </button>
      </header>

      {error && (
        <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
          {error}
        </div>
      )}

      {entries.length === 0 ? (
        <SectionCard title="No upstreams">
          <p className="text-sm text-slate-400">
            No upstreams are configured. Add one from the Config page.
          </p>
        </SectionCard>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {entries.map(([name, info]) => {
            const health = info.health as Record<string, unknown>;
            const transport = health?.type as string | undefined;
            const healthOk =
              health?.status === "ok" || health?.running === true;
            const tools = info.discovery?.tools ?? [];
            const discoveryOk = info.discovery?.ok;
            const lastError = (health?.last_error as string) || null;
            const discoveryError = (info.discovery?.error as string) || null;
            const restartAttempts = (health?.restart_attempts as number) || 0;
            return (
              <div key={name} className="card card-hover">
                <div className="flex items-start justify-between">
                  <div>
                    <div className="font-mono text-lg text-accent-400">{name}</div>
                    <div className="mt-1 flex items-center gap-2">
                      <Badge tone={healthOk ? "ok" : "err"}>
                        {healthOk ? "healthy" : "unhealthy"}
                      </Badge>
                      {transport && <Badge>{transport}</Badge>}
                      {discoveryOk === true && <Badge tone="accent">{tools.length} tools</Badge>}
                      {discoveryOk === false && <Badge tone="warn">discovery failed</Badge>}
                      {restartAttempts > 0 && (
                        <Badge tone="warn">{restartAttempts} restart{restartAttempts > 1 ? "s" : ""}</Badge>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      className="btn"
                      onClick={() => restart(name)}
                      disabled={busy === name}
                      title="Restart upstream"
                    >
                      <RotateCcw className={`h-4 w-4 ${busy === name ? "animate-spin" : ""}`} />
                      Restart
                    </button>
                    <button
                      className="text-slate-500 hover:text-err"
                      onClick={() => remove(name)}
                      disabled={busy === name}
                      title="Remove upstream"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {(lastError || discoveryError) && (
                  <div className="mt-3 rounded-md border border-err/30 bg-err/5 px-3 py-2">
                    {lastError && (
                      <p className="font-mono text-xs text-err break-all">{lastError}</p>
                    )}
                    {discoveryError && discoveryError !== lastError && (
                      <p className="font-mono text-xs text-amber-400 break-all">
                        discovery: {discoveryError}
                      </p>
                    )}
                  </div>
                )}
                {tools.length > 0 && (
                  <div className="mt-4">
                    <div className="mb-2 text-xs uppercase tracking-wider text-slate-400">
                      Tools
                    </div>
                    <ul className="max-h-40 space-y-1 overflow-auto scroll-thin">
                      {tools.map((t, i) => (
                        <li key={i} className="truncate font-mono text-xs text-slate-300">
                          <span className="text-accent-400">{t.name}</span>
                          {t.description && (
                            <span className="ml-2 text-slate-500">— {t.description}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <UpstreamLogs name={name} />
                <details className="mt-4 text-xs text-slate-400">
                  <summary className="cursor-pointer select-none">Raw health</summary>
                  <pre className="mt-2 whitespace-pre-wrap rounded-md bg-surface-900 p-2 font-mono text-[11px]">
                    {JSON.stringify(info.health, null, 2)}
                  </pre>
                </details>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
