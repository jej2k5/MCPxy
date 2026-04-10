import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { apiGet, apiPost, apiDelete, ApiError } from "../api/client";
import type { TokenMappingRow, UserRow } from "../api/types";

export default function TokenMappings() {
  const [mappings, setMappings] = useState<TokenMappingRow[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [upstream, setUpstream] = useState("");
  const [userId, setUserId] = useState("");
  const [upstreamToken, setUpstreamToken] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [m, u] = await Promise.all([
        apiGet<TokenMappingRow[]>("/admin/api/token-mappings"),
        apiGet<UserRow[]>("/admin/api/users"),
      ]);
      setMappings(m);
      setUsers(u);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiPost<TokenMappingRow>("/admin/api/token-mappings", {
        upstream,
        user_id: parseInt(userId, 10),
        upstream_token: upstreamToken,
        description,
      });
      setShowCreate(false);
      setUpstream("");
      setUserId("");
      setUpstreamToken("");
      setDescription("");
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this mapping?")) return;
    try {
      await apiDelete<unknown>(`/admin/api/token-mappings/${id}`);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed");
    }
  }

  function userEmail(uid: number): string {
    return users.find((u) => u.id === uid)?.email || `user#${uid}`;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Token Mappings</h2>
          <p className="mt-1 text-sm text-slate-400">
            Map MCPxy user identities to upstream MCP server tokens. Used when an
            upstream's <code className="text-xs">token_transform</code> strategy is
            set to <code className="text-xs">map</code>.
          </p>
        </div>
        <button
          className="btn btn-primary flex items-center gap-2"
          onClick={() => setShowCreate(true)}
        >
          <Plus className="h-4 w-4" /> Add mapping
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
          {error}
        </div>
      )}

      {showCreate && (
        <form onSubmit={handleCreate} className="card space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                Upstream name
              </label>
              <input
                type="text"
                className="input"
                value={upstream}
                onChange={(e) => setUpstream(e.target.value)}
                placeholder="e.g. github"
                required
              />
            </div>
            <div>
              <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                User
              </label>
              <select
                className="input"
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                required
              >
                <option value="">Select user...</option>
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.email} ({u.role})
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
              Upstream token
            </label>
            <input
              type="password"
              className="input"
              value={upstreamToken}
              onChange={(e) => setUpstreamToken(e.target.value)}
              placeholder="Bearer token the upstream expects"
              required
            />
            <p className="mt-1 text-xs text-slate-500">
              Stored encrypted at rest. Not retrievable after saving.
            </p>
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
              Description
            </label>
            <input
              type="text"
              className="input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional note"
            />
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Saving..." : "Save mapping"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setShowCreate(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className="card overflow-hidden p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-surface-700 text-left text-xs uppercase tracking-wider text-slate-400">
              <th className="px-4 py-3">Upstream</th>
              <th className="px-4 py-3">User</th>
              <th className="px-4 py-3">Token</th>
              <th className="px-4 py-3">Description</th>
              <th className="px-4 py-3">Updated</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {mappings.map((m) => (
              <tr key={m.id} className="border-b border-surface-800 last:border-0">
                <td className="px-4 py-3 font-mono text-sm text-slate-200">{m.upstream}</td>
                <td className="px-4 py-3 text-slate-300">{userEmail(m.user_id)}</td>
                <td className="px-4 py-3">
                  <code className="rounded bg-surface-800 px-1.5 py-0.5 text-xs text-slate-400">
                    {m.token_preview}
                  </code>
                </td>
                <td className="px-4 py-3 text-slate-400">{m.description || "—"}</td>
                <td className="px-4 py-3 text-slate-400">
                  {new Date(m.updated_at * 1000).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    className="text-slate-500 hover:text-err"
                    onClick={() => handleDelete(m.id)}
                    title="Delete mapping"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </td>
              </tr>
            ))}
            {mappings.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                  No token mappings configured
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
