import { useEffect, useState } from "react";
import { apiGet, apiPost, setToken, ApiError } from "../api/client";
import type { ProvidersResponse, LoginResponse } from "../api/types";

export default function LoginGate({ onAuthed }: { onAuthed: () => void }) {
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [legacyToken, setLegacyToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch("/admin/api/authy/providers", { headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data) setProviders(data as ProvidersResponse);
      })
      .catch(() => {});
  }, []);

  // Legacy bearer-token login
  async function submitLegacy(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setToken(legacyToken || null);
    try {
      await apiGet<unknown>("/admin/api/config");
      onAuthed();
    } catch (err) {
      setToken(null);
      setError(err instanceof Error ? err.message : "Failed to authenticate.");
    } finally {
      setBusy(false);
    }
  }

  // Authy local login
  async function submitLocal(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await apiPost<LoginResponse>("/admin/api/authy/login", {
        email,
        password,
      });
      if (result.token) {
        setToken(result.token);
      }
      onAuthed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed.");
    } finally {
      setBusy(false);
    }
  }

  // Federated login
  async function startFederated(provider: string) {
    setBusy(true);
    setError(null);
    try {
      const result = await apiPost<{ authorization_url: string }>(
        "/admin/api/authy/login/start",
        { provider },
      );
      window.location.href = result.authorization_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed.");
      setBusy(false);
    }
  }

  const isLegacy = !providers || !providers.authy_enabled;
  const hasLocal = providers?.providers.includes("local") ?? false;
  const federated = (providers?.providers ?? []).filter((p) => p !== "local");

  return (
    <div className="flex h-screen items-center justify-center bg-surface-950">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-accent-500 text-xl font-bold text-white">
            M
          </div>
          <h1 className="text-xl font-semibold text-slate-100">MCPy Admin</h1>
          <p className="mt-1 text-sm text-slate-400">
            {isLegacy ? "Sign in with your proxy admin token" : "Sign in to continue"}
          </p>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            {error}
          </div>
        )}

        {isLegacy ? (
          <form onSubmit={submitLegacy} className="card space-y-4">
            <div>
              <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                Admin token
              </label>
              <input
                type="password"
                value={legacyToken}
                onChange={(e) => setLegacyToken(e.target.value)}
                className="input"
                placeholder="Paste bearer token"
                autoFocus
              />
            </div>
            <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy}>
              {busy ? "Signing in..." : "Sign in"}
            </button>
          </form>
        ) : (
          <div className="card space-y-4">
            {hasLocal && (
              <form onSubmit={submitLocal} className="space-y-3">
                <div>
                  <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                    Email
                  </label>
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="input"
                    placeholder="admin@example.com"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                    Password
                  </label>
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="input"
                    placeholder="Password"
                  />
                </div>
                <button
                  type="submit"
                  className="btn btn-primary w-full justify-center"
                  disabled={busy}
                >
                  {busy ? "Signing in..." : "Sign in"}
                </button>
              </form>
            )}

            {hasLocal && federated.length > 0 && (
              <div className="flex items-center gap-3">
                <div className="h-px flex-1 bg-surface-700" />
                <span className="text-xs text-slate-500">or</span>
                <div className="h-px flex-1 bg-surface-700" />
              </div>
            )}

            {federated.map((provider) => (
              <button
                key={provider}
                className="btn btn-secondary w-full justify-center"
                disabled={busy}
                onClick={() => startFederated(provider)}
              >
                Sign in with{" "}
                {provider === "google"
                  ? "Google"
                  : provider === "m365"
                  ? "Microsoft 365"
                  : provider.startsWith("sso")
                  ? "SSO"
                  : provider}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
