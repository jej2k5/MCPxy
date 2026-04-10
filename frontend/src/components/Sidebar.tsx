import { NavLink } from "react-router-dom";
import {
  Activity,
  Cable,
  Compass,
  Download,
  FileText,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Plug,
  Settings2,
  ShieldCheck,
  Users2,
} from "lucide-react";
import { logout } from "../api/client";

const items = [
  { to: "/overview", label: "Overview", icon: LayoutDashboard },
  { to: "/routes", label: "Routes", icon: Cable },
  { to: "/traffic", label: "Traffic", icon: Activity },
  { to: "/policies", label: "Policies", icon: ShieldCheck },
  { to: "/browse", label: "Browse", icon: Compass },
  { to: "/import", label: "Import", icon: Download },
  { to: "/connect", label: "Connect", icon: Plug },
  { to: "/logs", label: "Logs", icon: FileText },
  { to: "/config", label: "Config", icon: Settings2 },
  { to: "/tokens", label: "Tokens", icon: KeyRound },
];

const adminItems = [
  { to: "/users", label: "Users", icon: Users2 },
];

export default function Sidebar({ onSignOut, isAdmin }: { onSignOut: () => void; isAdmin?: boolean }) {
  return (
    <aside className="flex w-56 flex-col border-r border-surface-700 bg-surface-900">
      <div className="flex items-center gap-2 border-b border-surface-700 px-5 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-accent-500 font-bold text-white">
          M
        </div>
        <div>
          <div className="text-sm font-semibold text-slate-100">MCPy</div>
          <div className="text-xs text-slate-400">Admin</div>
        </div>
      </div>
      <nav className="flex-1 space-y-1 p-3">
        {items.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                isActive
                  ? "bg-accent-500/15 text-accent-400"
                  : "text-slate-300 hover:bg-surface-800 hover:text-slate-100"
              }`
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
        {isAdmin && adminItems.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                isActive
                  ? "bg-accent-500/15 text-accent-400"
                  : "text-slate-300 hover:bg-surface-800 hover:text-slate-100"
              }`
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>
      <button
        className="flex items-center gap-3 border-t border-surface-700 px-5 py-3 text-sm text-slate-400 hover:bg-surface-800 hover:text-slate-100"
        onClick={() => {
          logout().then(onSignOut);
        }}
      >
        <LogOut className="h-4 w-4" />
        Sign out
      </button>
    </aside>
  );
}
