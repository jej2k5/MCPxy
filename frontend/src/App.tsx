import { useEffect, useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import LoginGate from "./components/LoginGate";
import Overview from "./pages/Overview";
import RoutesPage from "./pages/Routes";
import Traffic from "./pages/Traffic";
import Policies from "./pages/Policies";
import Connect from "./pages/Connect";
import Browse from "./pages/Browse";
import Import from "./pages/Import";
import Logs from "./pages/Logs";
import Config from "./pages/Config";
import { apiGet, getToken } from "./api/client";

export default function App() {
  const [authed, setAuthed] = useState<boolean>(Boolean(getToken()));
  const [checking, setChecking] = useState<boolean>(Boolean(getToken()));

  useEffect(() => {
    if (!authed) {
      setChecking(false);
      return;
    }
    let cancelled = false;
    // Probe an auth-gated endpoint so a stale token drops us back to LoginGate.
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
