import { useEffect, useState } from "react";
import { RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { apiGet, apiPost, apiDelete } from "../api/client";
import type { RouteSnapshot } from "../api/types";
import { SectionCard } from "../components/Card";
import { Badge } from "../components/Badge";

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
            const transport = (info.health as Record<string, unknown>)?.type as
              | string
              | undefined;
            const healthOk =
              (info.health as Record<string, unknown>)?.status === "ok" ||
              (info.health as Record<string, unknown>)?.running === true;
            const tools = info.discovery?.tools ?? [];
            const discoveryOk = info.discovery?.ok;
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
