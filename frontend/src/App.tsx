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
import Users from "./pages/Users";
import Tokens from "./pages/Tokens";
import { apiGet, getToken } from "./api/client";
import type { MeResponse, OnboardingStatus } from "./api/types";

export default function App() {
  const [onboarding, setOnboarding] = useState<OnboardingStatus | null>(null);
  const [onboardingResolved, setOnboardingResolved] = useState<boolean>(false);
  const [authed, setAuthed] = useState<boolean>(false);
  const [checking, setChecking] = useState<boolean>(false);
  const [me, setMe] = useState<MeResponse | null>(null);

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

  useEffect(() => {
    if (!authed) {
      setChecking(false);
      setMe(null);
      return;
    }
    let cancelled = false;
    // Try /admin/api/authy/me first, then fall back to a config probe
    // for legacy bearer-token deployments.
    apiGet<MeResponse>("/admin/api/authy/me")
      .then((data) => {
        if (!cancelled) {
          setMe(data);
          setChecking(false);
        }
      })
      .catch(() => {
        if (cancelled) return;
        // Legacy fallback: probe an auth-gated endpoint.
        apiGet<unknown>("/admin/api/config")
          .then(() => {
            if (!cancelled) {
              setMe({ user_id: -1, email: "admin", role: "admin", provider: "legacy", auth_mode: "legacy" });
              setChecking(false);
            }
          })
          .catch(() => {
            if (!cancelled) {
              setAuthed(false);
              setChecking(false);
            }
          });
      });
    return () => {
      cancelled = true;
    };
  }, [authed]);

  if (!onboardingResolved) {
    return (
      <div className="flex h-screen items-center justify-center text-slate-400">
        Connecting to MCPy...
      </div>
    );
  }

  if (onboarding?.required) {
    return (
      <Onboarding
        initialStatus={onboarding}
        onComplete={() => {
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

  const isAdmin = me?.role === "admin";

  return (
    <div className="flex h-screen">
      <Sidebar onSignOut={() => setAuthed(false)} isAdmin={isAdmin} />
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
            <Route path="/tokens" element={<Tokens />} />
            {isAdmin && <Route path="/users" element={<Users />} />}
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
