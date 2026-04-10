import { useEffect, useState } from "react";
import { Copy, Plus, Trash2, UserPlus } from "lucide-react";
import { apiGet, apiPost, apiDelete, ApiError } from "../api/client";
import type { UserRow, InviteResponse } from "../api/types";

export default function Users() {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showInvite, setShowInvite] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin">("member");
  const [inviteResult, setInviteResult] = useState<InviteResponse | null>(null);
  const [busy, setBusy] = useState(false);

  async function loadUsers() {
    try {
      const data = await apiGet<UserRow[]>("/admin/api/users");
      setUsers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load users");
    }
  }

  useEffect(() => {
    loadUsers();
  }, []);

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await apiPost<InviteResponse>("/admin/api/users/invite", {
        email: inviteEmail,
        role: inviteRole,
      });
      setInviteResult(result);
      loadUsers();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Invite failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(userId: number) {
    if (!confirm("Delete this user? This cannot be undone.")) return;
    try {
      await apiDelete<unknown>(`/admin/api/users/${userId}`);
      loadUsers();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed");
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-100">Users</h2>
        <button
          className="btn btn-primary flex items-center gap-2"
          onClick={() => {
            setShowInvite(true);
            setInviteResult(null);
            setInviteEmail("");
          }}
        >
          <UserPlus className="h-4 w-4" /> Invite user
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
          {error}
        </div>
      )}

      {showInvite && (
        <div className="card space-y-4">
          {inviteResult ? (
            <div className="space-y-3">
              <p className="text-sm text-slate-300">
                Invite created for <strong>{inviteResult.email}</strong> ({inviteResult.role}).
                Share the token below — it will not be shown again.
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 rounded bg-surface-800 px-3 py-2 text-sm text-slate-200 break-all">
                  {inviteResult.plaintext_token}
                </code>
                <button
                  className="btn btn-secondary"
                  onClick={() => navigator.clipboard.writeText(inviteResult.plaintext_token || "")}
                >
                  <Copy className="h-4 w-4" />
                </button>
              </div>
              <button className="btn btn-secondary" onClick={() => setShowInvite(false)}>
                Done
              </button>
            </div>
          ) : (
            <form onSubmit={handleInvite} className="space-y-3">
              <div>
                <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                  Email
                </label>
                <input
                  type="email"
                  className="input"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                  Role
                </label>
                <select
                  className="input"
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value as "member" | "admin")}
                >
                  <option value="member">Member</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
              <div className="flex gap-2">
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? "Sending..." : "Send invite"}
                </button>
                <button type="button" className="btn btn-secondary" onClick={() => setShowInvite(false)}>
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
              <th className="px-4 py-3">Email</th>
              <th className="px-4 py-3">Name</th>
              <th className="px-4 py-3">Provider</th>
              <th className="px-4 py-3">Role</th>
              <th className="px-4 py-3">Created</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-b border-surface-800 last:border-0">
                <td className="px-4 py-3 text-slate-200">{u.email}</td>
                <td className="px-4 py-3 text-slate-300">{u.name || "—"}</td>
                <td className="px-4 py-3 text-slate-400">{u.provider}</td>
                <td className="px-4 py-3">
                  <span
                    className={
                      "rounded px-2 py-0.5 text-xs font-medium " +
                      (u.role === "admin"
                        ? "bg-accent-500/20 text-accent-300"
                        : "bg-surface-700 text-slate-400")
                    }
                  >
                    {u.role}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-400">
                  {new Date(u.created_at * 1000).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    className="text-slate-500 hover:text-err"
                    onClick={() => handleDelete(u.id)}
                    title="Delete user"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                  No users yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
