import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../api/client";
import type { DiscoveryClient, DiscoveryResponse } from "../api/types";

export default function Import() {
  const [data, setData] = useState<DiscoveryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Record<string, Set<string>>>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    reload();
  }, []);

  function reload() {
    setError(null);
    apiGet<DiscoveryResponse>("/admin/api/discovery/clients")
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }

  function toggle(clientId: string, upstreamName: string) {
    setSelected((prev) => {
      const next = { ...prev };
      const set = new Set(next[clientId] ?? []);
      if (set.has(upstreamName)) set.delete(upstreamName);
      else set.add(upstreamName);
      next[clientId] = set;
      return next;
    });
  }

  async function importClient(client: DiscoveryClient) {
    const chosen = Array.from(selected[client.client_id] ?? []);
    if (chosen.length === 0) {
      setError(`select at least one upstream from ${client.display_name}`);
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await apiPost<{
        applied?: boolean;
        imported?: string[];
      }>("/admin/api/discovery/import", {
        client: client.client_id,
        upstreams: chosen,
        replace: true,
      });
      if (res.applied) {
        setResult(
          `Imported ${res.imported?.length ?? chosen.length} upstream(s) from ${client.display_name}.`,
        );
        setSelected((prev) => ({ ...prev, [client.client_id]: new Set() }));
      } else {
        setError("import returned applied=false");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) {
    return <div className="card text-err">{error}</div>;
  }
  if (!data) {
    return <div className="card text-slate-400">Scanning client configs…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Import from clients</h1>
          <p className="text-sm text-slate-400">
            MCP servers detected in Claude Desktop, Claude Code, Cursor,
            Windsurf, and Continue config files on this machine. Select the
            ones you want to bring into MCPxy.
          </p>
        </div>
        <button className="btn" onClick={reload}>
          Rescan
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
          {error}
        </div>
      )}
      {result && (
        <div className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok">
          {result}
        </div>
      )}

      <div className="space-y-4">
        {data.clients.map((client) => (
          <div key={client.client_id} className="card space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-base font-semibold text-slate-100">
                  {client.display_name}
                </h2>
                {client.config_path ? (
                  <p className="font-mono text-xs text-slate-500">
                    {client.config_path}
                  </p>
                ) : (
                  <p className="text-xs text-slate-500">
                    no config detected on this machine
                  </p>
                )}
              </div>
              {client.detected && client.upstreams.length > 0 && (
                <button
                  className="btn btn-primary"
                  disabled={busy || !(selected[client.client_id]?.size)}
                  onClick={() => importClient(client)}
                >
                  {busy ? "Importing…" : `Import selected`}
                </button>
              )}
            </div>
            {client.upstreams.length === 0 ? (
              <p className="text-xs text-slate-500">
                {client.detected
                  ? "(no MCP servers in this client config)"
                  : "(client not installed)"}
              </p>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="py-1 pr-2"></th>
                    <th className="py-1 pr-2">Name</th>
                    <th className="py-1 pr-2">Transport</th>
                    <th className="py-1 pr-2">Command / URL</th>
                  </tr>
                </thead>
                <tbody>
                  {client.upstreams.map((upstream) => {
                    const isSelected =
                      selected[client.client_id]?.has(upstream.name) ?? false;
                    const transport = String(upstream.config["type"] ?? "?");
                    const detail =
                      transport === "stdio"
                        ? `${upstream.config["command"]} ${(
                            (upstream.config["args"] as string[]) ?? []
                          ).join(" ")}`
                        : String(upstream.config["url"] ?? "");
                    return (
                      <tr
                        key={upstream.name}
                        className="border-t border-surface-800 align-top"
                      >
                        <td className="py-1 pr-2">
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() =>
                              toggle(client.client_id, upstream.name)
                            }
                          />
                        </td>
                        <td className="py-1 pr-2 font-mono text-accent-400">
                          {upstream.name}
                        </td>
                        <td className="py-1 pr-2 text-slate-300">{transport}</td>
                        <td className="py-1 pr-2 font-mono text-xs text-slate-400">
                          {detail}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
