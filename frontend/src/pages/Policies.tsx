import { useEffect, useState } from "react";
import { Plus, Save, Trash2 } from "lucide-react";
import { apiGet, apiPost } from "../api/client";
import { SectionCard } from "../components/Card";
import { Badge } from "../components/Badge";

interface MethodPolicy {
  allow?: string[] | null;
  deny?: string[] | null;
}

interface RateLimitPolicy {
  requests_per_second: number;
  burst: number;
  scope: "upstream" | "client_ip" | "both";
}

interface SizePolicy {
  max_request_bytes: number;
}

interface UpstreamPolicies {
  methods?: MethodPolicy | null;
  rate_limit?: RateLimitPolicy | null;
  size?: SizePolicy | null;
}

interface PoliciesConfig {
  global?: UpstreamPolicies | null;
  per_upstream: Record<string, UpstreamPolicies>;
}

function tagListEditor(
  label: string,
  value: string[] | null | undefined,
  onChange: (next: string[]) => void,
) {
  const [draft, setDraft] = useState("");
  return (
    <div>
      <div className="mb-1 text-xs uppercase tracking-wider text-slate-400">{label}</div>
      <div className="flex flex-wrap gap-1">
        {(value ?? []).map((v, i) => (
          <span key={i} className="badge border-accent-500/40 bg-accent-500/10 text-accent-400">
            <span className="font-mono">{v}</span>
            <button
              className="ml-1 text-slate-400 hover:text-err"
              onClick={() => {
                const next = (value ?? []).filter((_, idx) => idx !== i);
                onChange(next);
              }}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="mt-2 flex gap-2">
        <input
          className="input !text-xs"
          placeholder="method or wildcard"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && draft.trim()) {
              e.preventDefault();
              onChange([...(value ?? []), draft.trim()]);
              setDraft("");
            }
          }}
        />
      </div>
    </div>
  );
}

function PolicyCard({
  name,
  policy,
  onChange,
  onRemove,
}: {
  name: string;
  policy: UpstreamPolicies;
  onChange: (next: UpstreamPolicies) => void;
  onRemove?: () => void;
}) {
  const setMethods = (m: MethodPolicy | null) => onChange({ ...policy, methods: m });
  const setRate = (r: RateLimitPolicy | null) => onChange({ ...policy, rate_limit: r });
  const setSize = (s: SizePolicy | null) => onChange({ ...policy, size: s });

  return (
    <div className="card space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-lg text-accent-400">{name}</div>
          <Badge tone="default">policies</Badge>
        </div>
        {onRemove && (
          <button className="btn btn-ghost text-err" onClick={onRemove}>
            <Trash2 className="h-4 w-4" />
            Remove
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3">
        {tagListEditor(
          "Allow methods (whitelist)",
          policy.methods?.allow ?? [],
          (next) => setMethods({ ...(policy.methods ?? {}), allow: next.length ? next : null }),
        )}
        {tagListEditor(
          "Deny methods",
          policy.methods?.deny ?? [],
          (next) => setMethods({ ...(policy.methods ?? {}), deny: next.length ? next : null }),
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <div className="mb-1 text-xs uppercase tracking-wider text-slate-400">Rate (req/s)</div>
          <input
            className="input"
            type="number"
            min="0"
            step="0.5"
            value={policy.rate_limit?.requests_per_second ?? ""}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              if (Number.isNaN(v) || v <= 0) {
                setRate(null);
              } else {
                setRate({
                  requests_per_second: v,
                  burst: policy.rate_limit?.burst ?? Math.ceil(v),
                  scope: policy.rate_limit?.scope ?? "upstream",
                });
              }
            }}
          />
        </div>
        <div>
          <div className="mb-1 text-xs uppercase tracking-wider text-slate-400">Burst</div>
          <input
            className="input"
            type="number"
            min="1"
            disabled={!policy.rate_limit}
            value={policy.rate_limit?.burst ?? ""}
            onChange={(e) =>
              setRate(
                policy.rate_limit
                  ? { ...policy.rate_limit, burst: parseInt(e.target.value, 10) || 1 }
                  : null,
              )
            }
          />
        </div>
        <div>
          <div className="mb-1 text-xs uppercase tracking-wider text-slate-400">Scope</div>
          <select
            className="input"
            disabled={!policy.rate_limit}
            value={policy.rate_limit?.scope ?? "upstream"}
            onChange={(e) =>
              setRate(
                policy.rate_limit
                  ? { ...policy.rate_limit, scope: e.target.value as RateLimitPolicy["scope"] }
                  : null,
              )
            }
          >
            <option value="upstream">upstream</option>
            <option value="client_ip">client_ip</option>
            <option value="both">both</option>
          </select>
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs uppercase tracking-wider text-slate-400">
          Max request bytes
        </div>
        <input
          className="input"
          type="number"
          min="0"
          value={policy.size?.max_request_bytes ?? ""}
          onChange={(e) => {
            const v = parseInt(e.target.value, 10);
            if (Number.isNaN(v) || v <= 0) setSize(null);
            else setSize({ max_request_bytes: v });
          }}
        />
      </div>
    </div>
  );
}

export default function Policies() {
  const [config, setConfig] = useState<PoliciesConfig>({ per_upstream: {} });
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; message: string } | null>(null);
  const [newUpstream, setNewUpstream] = useState("");

  async function load() {
    try {
      const data = await apiGet<PoliciesConfig>("/admin/api/policies");
      setConfig(data);
      setFeedback(null);
    } catch (err) {
      setFeedback({ ok: false, message: err instanceof Error ? err.message : "Load failed" });
    }
  }

  useEffect(() => {
    load();
  }, []);

  function setPerUpstream(name: string, p: UpstreamPolicies | null) {
    setConfig((prev) => {
      const next = { ...prev, per_upstream: { ...prev.per_upstream } };
      if (p === null) {
        delete next.per_upstream[name];
      } else {
        next.per_upstream[name] = p;
      }
      return next;
    });
  }

  async function save() {
    setBusy(true);
    try {
      const out = await apiPost<{ applied: boolean; error?: string }>(
        "/admin/api/policies",
        { policies: config },
      );
      if (out.applied) {
        setFeedback({ ok: true, message: "Policies applied." });
        load();
      } else {
        setFeedback({ ok: false, message: out.error ?? "Apply failed" });
      }
    } catch (err) {
      setFeedback({ ok: false, message: err instanceof Error ? err.message : "Apply failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Policies</h1>
          <p className="text-sm text-slate-400">
            Method ACLs, rate limits, and size caps. Applied via hot reload.
          </p>
        </div>
        <button className="btn btn-primary" onClick={save} disabled={busy}>
          <Save className="h-4 w-4" />
          Save and apply
        </button>
      </header>

      {feedback && (
        <div
          className={`rounded-lg border px-3 py-2 text-sm ${
            feedback.ok ? "border-ok/40 bg-ok/10 text-ok" : "border-err/40 bg-err/10 text-err"
          }`}
        >
          {feedback.message}
        </div>
      )}

      <SectionCard title="Global">
        <PolicyCard
          name="(global)"
          policy={config.global ?? {}}
          onChange={(p) => setConfig((prev) => ({ ...prev, global: p }))}
        />
      </SectionCard>

      <SectionCard
        title="Per upstream"
        action={
          <div className="flex gap-2">
            <input
              className="input !w-40"
              placeholder="upstream name"
              value={newUpstream}
              onChange={(e) => setNewUpstream(e.target.value)}
            />
            <button
              className="btn"
              disabled={!newUpstream.trim()}
              onClick={() => {
                setPerUpstream(newUpstream.trim(), {});
                setNewUpstream("");
              }}
            >
              <Plus className="h-4 w-4" />
              Add
            </button>
          </div>
        }
      >
        <div className="grid grid-cols-1 gap-4">
          {Object.entries(config.per_upstream).map(([name, p]) => (
            <PolicyCard
              key={name}
              name={name}
              policy={p}
              onChange={(next) => setPerUpstream(name, next)}
              onRemove={() => setPerUpstream(name, null)}
            />
          ))}
          {Object.keys(config.per_upstream).length === 0 && (
            <p className="text-sm text-slate-400">
              No per-upstream policies configured. Use the global card above for fleet-wide
              rules.
            </p>
          )}
        </div>
      </SectionCard>
    </div>
  );
}
