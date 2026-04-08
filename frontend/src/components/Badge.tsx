import type { TrafficStatus } from "../api/types";

const STATUS_STYLES: Record<TrafficStatus, string> = {
  ok: "border-ok/30 bg-ok/10 text-ok",
  error: "border-err/30 bg-err/10 text-err",
  timeout: "border-warn/30 bg-warn/10 text-warn",
  denied: "border-denied/30 bg-denied/10 text-denied",
};

export function StatusBadge({ status }: { status: TrafficStatus | string }) {
  const style = STATUS_STYLES[status as TrafficStatus] ?? "border-surface-600 bg-surface-700 text-slate-300";
  return <span className={`badge ${style}`}>{status}</span>;
}

export function Badge({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?: "default" | "ok" | "warn" | "err" | "accent";
}) {
  const tones: Record<string, string> = {
    default: "border-surface-600 bg-surface-700 text-slate-300",
    ok: "border-ok/30 bg-ok/10 text-ok",
    warn: "border-warn/30 bg-warn/10 text-warn",
    err: "border-err/30 bg-err/10 text-err",
    accent: "border-accent-500/40 bg-accent-500/10 text-accent-400",
  };
  return <span className={`badge ${tones[tone]}`}>{children}</span>;
}
