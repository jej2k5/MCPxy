import { useState } from "react";
import { apiGet, setToken } from "../api/client";

export default function LoginGate({ onAuthed }: { onAuthed: () => void }) {
  const [token, setTokenInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setToken(token || null);
    try {
      // Hit an auth-gated endpoint so a wrong token is actually rejected.
      // /health is public and would accept anything.
      await apiGet<unknown>("/admin/api/config");
      onAuthed();
    } catch (err) {
      setToken(null);
      setError(
        err instanceof Error ? err.message : "Failed to authenticate with MCPy.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-surface-950">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-accent-500 text-xl font-bold text-white">
            M
          </div>
          <h1 className="text-xl font-semibold text-slate-100">MCPy Admin</h1>
          <p className="mt-1 text-sm text-slate-400">
            Sign in with your proxy admin token
          </p>
        </div>
        <form onSubmit={submit} className="card space-y-4">
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
              Admin token
            </label>
            <input
              type="password"
              value={token}
              onChange={(e) => setTokenInput(e.target.value)}
              className="input"
              placeholder="Paste bearer token"
              autoFocus
            />
            <p className="mt-2 text-xs text-slate-500">
              Leave empty if your server has no token configured.
            </p>
          </div>
          {error && (
            <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
              {error}
            </div>
          )}
          <button
            type="submit"
            className="btn btn-primary w-full justify-center"
            disabled={busy}
          >
            {busy ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
