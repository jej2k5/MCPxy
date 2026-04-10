import { useEffect, useState } from "react";
import { Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { apiGet, apiPost, apiDelete, ApiError } from "../api/client";
import type { PatRow } from "../api/types";

export default function Tokens() {
  const [pats, setPats] = useState<PatRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [tokenName, setTokenName] = useState("");
  const [ttlDays, setTtlDays] = useState<string>("");
  const [created, setCreated] = useState<PatRow | null>(null);
  const [busy, setBusy] = useState(false);

  async function loadPats() {
    try {
      const data = await apiGet<PatRow[]>("/admin/api/pats");
      setPats(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tokens");
    }
  }

  useEffect(() => {
    loadPats();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await apiPost<PatRow>("/admin/api/pats", {
        name: tokenName || "Untitled",
        ttl_days: ttlDays ? parseInt(ttlDays, 10) : null,
      });
      setCreated(result);
      loadPats();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(patId: number) {
    if (!confirm("Revoke this token? Clients using it will lose access.")) return;
    try {
      await apiDelete<unknown>(`/admin/api/pats/${patId}`);
      loadPats();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Revoke failed");
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-100">Personal Access Tokens</h2>
        <button
          className="btn btn-primary flex items-center gap-2"
          onClick={() => {
            setShowCreate(true);
            setCreated(null);
            setTokenName("");
            setTtlDays("");
          }}
        >
          <Plus className="h-4 w-4" /> New token
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
          {error}
        </div>
      )}

      {showCreate && (
        <div className="card space-y-4">
          {created && created.plaintext ? (
            <div className="space-y-3">
              <p className="text-sm font-medium text-amber-300">
                Copy this token now — you won't see it again.
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 rounded bg-surface-800 px-3 py-2 text-sm text-slate-200 break-all">
                  {created.plaintext}
                </code>
                <button
                  className="btn btn-secondary"
                  onClick={() => navigator.clipboard.writeText(created.plaintext || "")}
                >
                  <Copy className="h-4 w-4" />
                </button>
              </div>
              <p className="text-xs text-slate-400">
                Use this as the bearer token in your MCP client config:
              </p>
              <pre className="rounded bg-surface-800 px-3 py-2 text-xs text-slate-300">
{`"headers": { "Authorization": "Bearer ${created.plaintext}" }`}
              </pre>
              <button className="btn btn-secondary" onClick={() => setShowCreate(false)}>
                Done
              </button>
            </div>
          ) : (
            <form onSubmit={handleCreate} className="space-y-3">
              <div>
                <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                  Token name
                </label>
                <input
                  type="text"
                  className="input"
                  placeholder="e.g. Claude Desktop - laptop"
                  value={tokenName}
                  onChange={(e) => setTokenName(e.target.value)}
                />
              </div>
              <div>
                <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                  Expires in (days, leave empty for no expiry)
                </label>
                <input
                  type="number"
                  className="input"
                  placeholder="No expiry"
                  value={ttlDays}
                  onChange={(e) => setTtlDays(e.target.value)}
                  min={1}
                />
              </div>
              <div className="flex gap-2">
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? "Creating..." : "Create token"}
                </button>
                <button type="button" className="btn btn-secondary" onClick={() => setShowCreate(false)}>
                  Cancel
                </button>
              </div>
            </form>
          )}
        </div>
      )}

      <div className="card overflow-hidden p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-surface-700 text-left text-xs uppercase tracking-wider text-slate-400">
              <th className="px-4 py-3">Name</th>
              <th className="px-4 py-3">Prefix</th>
              <th className="px-4 py-3">Created</th>
              <th className="px-4 py-3">Last used</th>
              <th className="px-4 py-3">Expires</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {pats.map((p) => (
              <tr key={p.id} className="border-b border-surface-800 last:border-0">
                <td className="px-4 py-3 text-slate-200">{p.name}</td>
                <td className="px-4 py-3">
                  <code className="rounded bg-surface-800 px-1.5 py-0.5 text-xs text-slate-300">
                    {p.token_prefix}...
                  </code>
                </td>
                <td className="px-4 py-3 text-slate-400">
                  {new Date(p.created_at * 1000).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-slate-400">
                  {p.last_used_at
                    ? new Date(p.last_used_at * 1000).toLocaleDateString()
                    : "Never"}
                </td>
                <td className="px-4 py-3 text-slate-400">
                  {p.expires_at
                    ? new Date(p.expires_at * 1000).toLocaleDateString()
                    : "Never"}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    className="text-slate-500 hover:text-err"
                    onClick={() => handleRevoke(p.id)}
                    title="Revoke token"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </td>
              </tr>
            ))}
            {pats.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                  No tokens yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
