import { useEffect, useState } from "react";
import { Check, ClipboardCheck, FileDiff } from "lucide-react";
import { apiGet, apiPost } from "../api/client";
import type { AppConfig } from "../api/types";
import { SectionCard } from "../components/Card";
import { Badge } from "../components/Badge";

export default function Config() {
  const [current, setCurrent] = useState<AppConfig | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [result, setResult] = useState<string | null>(null);
  const [resultTone, setResultTone] = useState<"ok" | "err">("ok");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const cfg = await apiGet<AppConfig>("/admin/api/config");
      setCurrent(cfg);
      setDraft(JSON.stringify(cfg, null, 2));
      setResult(null);
    } catch (err) {
      setResultTone("err");
      setResult(err instanceof Error ? err.message : "Failed to load config");
    }
  }

  useEffect(() => {
    load();
  }, []);

  function parseDraft(): unknown | null {
    try {
      return JSON.parse(draft);
    } catch (err) {
      setResultTone("err");
      setResult(`Invalid JSON: ${(err as Error).message}`);
      return null;
    }
  }

  async function validate() {
    const cfg = parseDraft();
    if (!cfg) return;
    setBusy(true);
    try {
      const out = await apiPost<{ valid: boolean; error: string | null }>(
        "/admin/api/config/validate",
        { config: cfg },
      );
      setResultTone(out.valid ? "ok" : "err");
      setResult(out.valid ? "Config is valid." : out.error ?? "Invalid config");
    } catch (err) {
      setResultTone("err");
      setResult(err instanceof Error ? err.message : "Validation failed");
    } finally {
      setBusy(false);
    }
  }

  async function preview() {
    const cfg = parseDraft();
    if (!cfg) return;
    setBusy(true);
    try {
      const out = await apiPost<unknown>("/admin/api/config", {
        config: cfg,
        dry_run: true,
      });
      setResultTone("ok");
      setResult(JSON.stringify(out, null, 2));
    } catch (err) {
      setResultTone("err");
      setResult(err instanceof Error ? err.message : "Dry run failed");
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    const cfg = parseDraft();
    if (!cfg) return;
    if (!confirm("Apply config changes to the running proxy?")) return;
    setBusy(true);
    try {
      const out = await apiPost<unknown>("/admin/api/config", { config: cfg });
      setResultTone("ok");
      setResult(`Applied.\n${JSON.stringify(out, null, 2)}`);
      await load();
    } catch (err) {
      setResultTone("err");
      setResult(err instanceof Error ? err.message : "Apply failed");
    } finally {
      setBusy(false);
    }
  }

  const upstreams = current?.upstreams ?? {};
  const upstreamEntries = Object.entries(upstreams);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-slate-100">Configuration</h1>
        <p className="text-sm text-slate-400">
          View and edit the live proxy configuration. Changes apply via hot reload.
        </p>
      </header>

      {upstreamEntries.length > 0 && (
        <SectionCard title="Upstreams">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {upstreamEntries.map(([name, settings]) => {
              const s = settings as Record<string, unknown>;
              return (
                <div key={name} className="rounded-lg border border-surface-600 bg-surface-900 p-3">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-accent-400">{name}</div>
                    <Badge tone="accent">{String(s.type ?? "?")}</Badge>
                  </div>
                  <div className="mt-2 font-mono text-xs text-slate-400">
                    {s.type === "stdio" && (
                      <>
                        {String(s.command ?? "")} {Array.isArray(s.args) ? s.args.join(" ") : ""}
                      </>
                    )}
                    {s.type === "http" && <>{String(s.url ?? "")}</>}
                  </div>
                </div>
              );
            })}
          </div>
        </SectionCard>
      )}

      <SectionCard
        title="Raw JSON"
        action={
          <div className="flex gap-2">
            <button className="btn" disabled={busy} onClick={validate}>
              <ClipboardCheck className="h-4 w-4" />
              Validate
            </button>
            <button className="btn" disabled={busy} onClick={preview}>
              <FileDiff className="h-4 w-4" />
              Dry run
            </button>
            <button className="btn btn-primary" disabled={busy} onClick={apply}>
              <Check className="h-4 w-4" />
              Apply
            </button>
          </div>
        }
      >
        <textarea
          className="input min-h-[320px] font-mono text-xs"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        {result && (
          <pre
            className={`mt-3 max-h-60 overflow-auto scroll-thin whitespace-pre-wrap rounded-lg border px-3 py-2 font-mono text-xs ${
              resultTone === "ok"
                ? "border-ok/40 bg-ok/5 text-ok"
                : "border-err/40 bg-err/5 text-err"
            }`}
          >
            {result}
          </pre>
        )}
      </SectionCard>
    </div>
  );
}
