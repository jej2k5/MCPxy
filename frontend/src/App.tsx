import { useEffect, useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import LoginGate from "./components/LoginGate";
import Overview from "./pages/Overview";
import RoutesPage from "./pages/Routes";
import Graph from "./pages/Graph";
import Traffic from "./pages/Traffic";
import Policies from "./pages/Policies";
import Connect from "./pages/Connect";
import Browse from "./pages/Browse";
import Import from "./pages/Import";
import Logs from "./pages/Logs";
import Config from "./pages/Config";
import Onboarding from "./pages/Onboarding";
import { apiGet, getToken } from "./api/client";
import type { OnboardingStatus } from "./api/types";

/**
 * App shell with three startup states:
 *
 *  1. **Onboarding required** — ``GET /admin/api/onboarding/status`` returns
 *     ``required=true`` (fresh DB, no admin token yet). Render the wizard
 *     instead of LoginGate; the wizard writes the token and localStorage.
 *  2. **Needs login** — onboarding not required and no stored token; show
 *     LoginGate so the operator pastes their bearer.
 *  3. **Authed** — stored token exists and the probe against
 *     ``/admin/api/config`` succeeds; render the normal dashboard.
 *
 * We probe the onboarding status BEFORE consulting localStorage because a
 * stale token from a previous install shouldn't mask a fresh DB.
 */
export default function App() {
  const [onboarding, setOnboarding] = useState<OnboardingStatus | null>(null);
  const [onboardingResolved, setOnboardingResolved] = useState<boolean>(false);
  const [authed, setAuthed] = useState<boolean>(false);
  const [checking, setChecking] = useState<boolean>(false);

  // Always fetch onboarding status first.
  useEffect(() => {
    let cancelled = false;
    fetch("/admin/api/onboarding/status", {
      headers: { Accept: "application/json" },
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((body: OnboardingStatus | null) => {
        if (cancelled) return;
        setOnboarding(body);
        setOnboardingResolved(true);
        if (!body || !body.required) {
          // Not onboarding: fall through to the normal token flow.
          const haveToken = Boolean(getToken());
          setAuthed(haveToken);
          setChecking(haveToken);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setOnboardingResolved(true);
        const haveToken = Boolean(getToken());
        setAuthed(haveToken);
        setChecking(haveToken);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Probe an auth-gated endpoint so stale tokens drop us back to LoginGate.
  useEffect(() => {
    if (!authed) {
      setChecking(false);
      return;
    }
    let cancelled = false;
    apiGet<unknown>("/admin/api/config")
      .then(() => {
        if (!cancelled) setChecking(false);
      })
      .catch(() => {
        if (!cancelled) {
          setAuthed(false);
          setChecking(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authed]);

  if (!onboardingResolved) {
    return (
      <div className="flex h-screen items-center justify-center text-slate-400">
        Connecting to MCPy…
      </div>
    );
  }

  if (onboarding?.required) {
    return (
      <Onboarding
        initialStatus={onboarding}
        onComplete={() => {
          // Flip the gate off and let the normal token probe run. The
          // wizard has already persisted the bearer into localStorage
          // via ``setToken``, so the next useEffect finds it.
          setOnboarding({ ...onboarding, required: false, completed: true, active: false });
          setAuthed(Boolean(getToken()));
          setChecking(Boolean(getToken()));
        }}
      />
    );
  }

  if (!authed) {
    return <LoginGate onAuthed={() => setAuthed(true)} />;
  }
  if (checking) {
    return (
      <div className="flex h-screen items-center justify-center text-slate-400">
        Connecting to MCPy...
      </div>
    );
  }

  return (
    <div className="flex h-screen">
      <Sidebar onSignOut={() => setAuthed(false)} />
      <main className="flex-1 overflow-auto scroll-thin">
        <div className="mx-auto max-w-6xl p-6">
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/routes" element={<RoutesPage />} />
            <Route path="/graph" element={<Graph />} />
            <Route path="/traffic" element={<Traffic />} />
            <Route path="/policies" element={<Policies />} />
            <Route path="/connect" element={<Connect />} />
            <Route path="/browse" element={<Browse />} />
            <Route path="/import" element={<Import />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/config" element={<Config />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
