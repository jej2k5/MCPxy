import { useEffect, useMemo, useState } from "react";
import { ArrowRight, Check, Copy, KeyRound, RefreshCcw, Sparkles } from "lucide-react";
import {
  apiGet,
  apiPost,
  ApiError,
  setToken as persistToken,
} from "../api/client";
import type {
  CatalogEntry,
  CatalogResponse,
  OnboardingStatus,
  OnboardingSetTokenResponse,
} from "../api/types";

/**
 * First-run onboarding wizard.
 *
 * Reachable at ``/admin/onboarding`` and also rendered by ``App.tsx``
 * whenever ``GET /admin/api/onboarding/status`` reports ``required=true``
 * (i.e. the DB has a fresh ``onboarding`` row with no admin token set
 * and no ``completed_at`` timestamp).
 *
 * Flow:
 *
 *   Step 1  Welcome + what the wizard will do in the next ~2 minutes.
 *   Step 2  Admin token — generate client-side or paste one, copy, ack,
 *           POST /admin/api/onboarding/set_admin_token. The token is
 *           also stashed in ``localStorage`` via ``setToken`` so the
 *           subsequent dashboard load is already authenticated.
 *   Step 3  Optional first MCP server from a curated slice of the
 *           bundled catalog. Skip button.
 *   Step 4  Finish → POST /admin/api/onboarding/finish, redirect.
 */

type Step = 1 | 2 | 3 | 4;

const CURATED_CATALOG_IDS = [
  "filesystem",
  "git",
  "github",
  "memory",
  "sequential_thinking",
  "time",
];

// Parameters of the onboarding POST calls that we never send back through
// the normal ``apiPost`` helper because that one attaches the stored token
// from localStorage — and during onboarding we deliberately don't have
// one yet. Use a tiny dedicated fetch wrapper instead.
async function onboardingPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(res.status, text || res.statusText);
  }
  return (await res.json()) as T;
}

async function onboardingGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new ApiError(res.status, await res.text());
  }
  return (await res.json()) as T;
}

function generateToken(): string {
  // 256 bits of entropy → url-safe base64 (43 chars). Matches what the
  // backend accepts (>= 16 chars) and is immediately copy-pasteable.
  const buf = new Uint8Array(32);
  crypto.getRandomValues(buf);
  let bin = "";
  for (const b of buf) bin += String.fromCharCode(b);
  return btoa(bin).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function StepPill({ active, done, n, label }: { active: boolean; done: boolean; n: number; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className={
          "flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold " +
          (done
            ? "bg-accent-500 text-white"
            : active
            ? "bg-accent-500/20 text-accent-200 ring-1 ring-accent-400"
            : "bg-surface-700 text-slate-400")
        }
      >
        {done ? <Check className="h-4 w-4" /> : n}
      </div>
      <span className={active ? "text-slate-100" : "text-slate-500"}>{label}</span>
    </div>
  );
}

function Stepper({ step }: { step: Step }) {
  return (
    <div className="flex flex-wrap items-center gap-4 text-sm">
      <StepPill n={1} active={step === 1} done={step > 1} label="Welcome" />
      <span className="text-slate-600">—</span>
      <StepPill n={2} active={step === 2} done={step > 2} label="Admin token" />
      <span className="text-slate-600">—</span>
      <StepPill n={3} active={step === 3} done={step > 3} label="First server" />
      <span className="text-slate-600">—</span>
      <StepPill n={4} active={step === 4} done={step > 4} label="Finish" />
    </div>
  );
}

export default function Onboarding({
  onComplete,
  initialStatus,
}: {
  onComplete: () => void;
  initialStatus: OnboardingStatus | null;
}) {
  const [step, setStep] = useState<Step>(1);
  const [status, setStatus] = useState<OnboardingStatus | null>(initialStatus);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refreshStatus() {
    try {
      const next = await onboardingGet<OnboardingStatus>(
        "/admin/api/onboarding/status",
      );
      setStatus(next);
      return next;
    } catch (e) {
      return null;
    }
  }

  useEffect(() => {
    if (!status) {
      refreshStatus();
    }
  }, []);

  // Start on the right step if the operator reloaded mid-flow.
  useEffect(() => {
    if (status?.admin_token_set_at && step < 3) setStep(3);
  }, [status]);

  return (
    <div className="min-h-screen bg-surface-950 text-slate-100">
      <div className="mx-auto max-w-3xl px-6 py-12">
        <header className="mb-8 space-y-2">
          <div className="flex items-center gap-3">
            <Sparkles className="h-6 w-6 text-accent-400" />
            <h1 className="text-2xl font-semibold">Welcome to MCPy</h1>
          </div>
          <p className="text-sm text-slate-400">
            First-run setup. This takes about two minutes and unlocks the
            dashboard.
          </p>
        </header>

        <Stepper step={step} />

        {error && (
          <div className="mt-6 rounded-lg border border-err/40 bg-err/10 px-4 py-3 text-sm text-err">
            {error}
          </div>
        )}

        <div className="mt-8">
          {step === 1 && <WelcomeStep onNext={() => setStep(2)} />}
          {step === 2 && (
            <TokenStep
              busy={busy}
              onBusy={setBusy}
              onError={setError}
              onNext={async (token) => {
                try {
                  setError(null);
                  setBusy(true);
                  const res = await onboardingPost<OnboardingSetTokenResponse>(
                    "/admin/api/onboarding/set_admin_token",
                    { token },
                  );
                  setStatus(res.onboarding);
                  persistToken(token);
                  setStep(3);
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            />
          )}
          {step === 3 && (
            <FirstServerStep
              busy={busy}
              onBusy={setBusy}
              onError={setError}
              onSkip={() => setStep(4)}
              onInstalled={() => setStep(4)}
            />
          )}
          {step === 4 && (
            <FinishStep
              busy={busy}
              onBusy={setBusy}
              onError={setError}
              onFinish={async () => {
                try {
                  setError(null);
                  setBusy(true);
                  await onboardingPost<OnboardingStatus>(
                    "/admin/api/onboarding/finish",
                    {},
                  );
                  onComplete();
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — Welcome
// ---------------------------------------------------------------------------

function WelcomeStep({ onNext }: { onNext: () => void }) {
  return (
    <div className="card space-y-4">
      <div className="space-y-3 text-sm text-slate-300">
        <p>
          MCPy is a proxy that fans out <span className="font-mono">Model Context Protocol</span>{" "}
          clients to any number of upstream MCP servers.
        </p>
        <p>In the next few steps we'll:</p>
        <ol className="list-decimal space-y-1 pl-5 text-slate-300">
          <li>Set an admin bearer token so only you can manage this proxy.</li>
          <li>
            Optionally install your first MCP server from the bundled catalog.
          </li>
          <li>
            Finish up and land on the dashboard where you can wire your
            clients (Claude Desktop, Cursor, Continue, …) to this proxy.
          </li>
        </ol>
      </div>
      <div className="flex justify-end">
        <button className="btn btn-primary" onClick={onNext}>
          Get started
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — Admin token
// ---------------------------------------------------------------------------

function TokenStep({
  onNext,
  busy,
  onBusy,
  onError,
}: {
  onNext: (token: string) => Promise<void>;
  busy: boolean;
  onBusy: (v: boolean) => void;
  onError: (msg: string | null) => void;
}) {
  const [mode, setMode] = useState<"generate" | "paste">("generate");
  const [token, setToken] = useState<string>(() => generateToken());
  const [copied, setCopied] = useState(false);
  const [ack, setAck] = useState(false);

  const tokenTooShort = token.length < 16;

  async function copy() {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // no-op
    }
  }

  return (
    <div className="card space-y-5">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-accent-400" />
          <h2 className="text-lg font-semibold">Set your admin bearer token</h2>
        </div>
        <p className="text-sm text-slate-400">
          Every write to <span className="font-mono text-xs">/admin/api/*</span>{" "}
          will require this token. Store it somewhere safe — we only show
          it once.
        </p>
      </div>

      <div className="flex gap-2 text-xs">
        <button
          className={
            "rounded px-3 py-1 " +
            (mode === "generate"
              ? "bg-accent-500/20 text-accent-200 ring-1 ring-accent-400"
              : "bg-surface-800 text-slate-400 hover:text-slate-200")
          }
          onClick={() => {
            setMode("generate");
            setToken(generateToken());
            setAck(false);
          }}
        >
          Generate for me
        </button>
        <button
          className={
            "rounded px-3 py-1 " +
            (mode === "paste"
              ? "bg-accent-500/20 text-accent-200 ring-1 ring-accent-400"
              : "bg-surface-800 text-slate-400 hover:text-slate-200")
          }
          onClick={() => {
            setMode("paste");
            setToken("");
            setAck(false);
          }}
        >
          Paste my own
        </button>
      </div>

      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wider text-slate-400">
          Token
        </label>
        <div className="flex gap-2">
          <input
            className="input flex-1 font-mono text-xs"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            readOnly={mode === "generate"}
            placeholder={mode === "paste" ? "paste at least 16 chars" : ""}
          />
          {mode === "generate" && (
            <button className="btn" onClick={() => setToken(generateToken())}>
              <RefreshCcw className="h-4 w-4" />
              New
            </button>
          )}
          <button className="btn" onClick={copy} disabled={tokenTooShort}>
            <Copy className="h-4 w-4" />
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        {tokenTooShort && (
          <p className="text-xs text-err">
            Token must be at least 16 characters.
          </p>
        )}
      </div>

      <label className="flex items-start gap-2 text-sm text-slate-300">
        <input
          type="checkbox"
          checked={ack}
          onChange={(e) => setAck(e.target.checked)}
          className="mt-0.5"
        />
        <span>
          I've saved this token in a password manager / secrets store /
          <span className="font-mono text-xs"> .env</span> file. I understand
          I won't see it again.
        </span>
      </label>

      <div className="flex justify-end">
        <button
          className="btn btn-primary"
          disabled={busy || tokenTooShort || !ack}
          onClick={() => {
            onError(null);
            onNext(token);
          }}
        >
          {busy ? "Saving…" : "Save and continue"}
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — Optional first server
// ---------------------------------------------------------------------------

function FirstServerStep({
  onSkip,
  onInstalled,
  busy,
  onBusy,
  onError,
}: {
  onSkip: () => void;
  onInstalled: () => void;
  busy: boolean;
  onBusy: (v: boolean) => void;
  onError: (msg: string | null) => void;
}) {
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [selected, setSelected] = useState<CatalogEntry | null>(null);
  const [vars, setVars] = useState<Record<string, string>>({});

  useEffect(() => {
    apiGet<CatalogResponse>("/admin/api/catalog")
      .then((c) => setCatalog(c))
      .catch((e: Error) => onError(`Could not load catalog: ${e.message}`));
  }, []);

  const curated = useMemo(() => {
    if (!catalog) return [];
    const by_id = new Map(catalog.entries.map((e) => [e.id, e]));
    return CURATED_CATALOG_IDS.map((id) => by_id.get(id)).filter(
      (e): e is CatalogEntry => !!e,
    );
  }, [catalog]);

  async function install() {
    if (!selected) return;
    try {
      onError(null);
      onBusy(true);
      await apiPost("/admin/api/catalog/install", {
        id: selected.id,
        name: selected.id,
        variables: vars,
      });
      onInstalled();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onBusy(false);
    }
  }

  if (selected) {
    return (
      <div className="card space-y-4">
        <div>
          <h2 className="text-lg font-semibold">Install {selected.name}</h2>
          <p className="text-xs text-slate-400">{selected.description}</p>
        </div>
        {selected.variables.length > 0 && (
          <div className="space-y-3">
            {selected.variables.map((v) => (
              <div key={v.name}>
                <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                  {v.name}
                  {v.required && <span className="text-err"> *</span>}
                </label>
                <input
                  type={v.secret ? "password" : "text"}
                  className="input"
                  placeholder={v.description}
                  value={vars[v.name] ?? v.default ?? ""}
                  onChange={(e) =>
                    setVars({ ...vars, [v.name]: e.target.value })
                  }
                />
                <p className="mt-1 text-xs text-slate-500">{v.description}</p>
              </div>
            ))}
          </div>
        )}
        <div className="flex justify-between gap-2">
          <button
            className="btn"
            onClick={() => {
              setSelected(null);
              setVars({});
            }}
          >
            Back
          </button>
          <div className="flex gap-2">
            <button className="btn" onClick={onSkip} disabled={busy}>
              Skip
            </button>
            <button className="btn btn-primary" onClick={install} disabled={busy}>
              {busy ? "Installing…" : "Install and continue"}
              <ArrowRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="card space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Install your first MCP server</h2>
        <p className="text-sm text-slate-400">
          Pick one from the curated list below, or skip and add servers
          later from the <span className="font-mono text-xs">Browse</span>{" "}
          page.
        </p>
      </div>
      {!catalog && (
        <div className="text-sm text-slate-400">Loading catalog…</div>
      )}
      {catalog && curated.length === 0 && (
        <div className="text-sm text-slate-400">
          No curated entries found in the bundled catalog.
        </div>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {curated.map((entry) => (
          <button
            key={entry.id}
            className="rounded-lg border border-surface-600 bg-surface-900 p-3 text-left hover:border-accent-400"
            onClick={() => setSelected(entry)}
          >
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm text-accent-400">{entry.id}</span>
              <span className="rounded-full bg-surface-800 px-2 py-0.5 text-xs text-slate-300">
                {entry.category}
              </span>
            </div>
            <div className="mt-1 font-semibold">{entry.name}</div>
            <div className="mt-1 text-xs text-slate-400">{entry.description}</div>
          </button>
        ))}
      </div>
      <div className="flex justify-end">
        <button className="btn" onClick={onSkip} disabled={busy}>
          Skip — I'll add servers later
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 4 — Finish
// ---------------------------------------------------------------------------

function FinishStep({
  busy,
  onBusy,
  onError,
  onFinish,
}: {
  busy: boolean;
  onBusy: (v: boolean) => void;
  onError: (msg: string | null) => void;
  onFinish: () => Promise<void>;
}) {
  return (
    <div className="card space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-ok">You're all set</h2>
        <p className="text-sm text-slate-400">
          Click finish to lock in the settings and open the dashboard. From
          there you can:
        </p>
      </div>
      <ul className="list-disc space-y-1 pl-5 text-sm text-slate-300">
        <li>
          Add more MCP servers via the{" "}
          <span className="font-mono text-xs">Browse</span> or{" "}
          <span className="font-mono text-xs">Import</span> pages.
        </li>
        <li>
          Wire Claude Desktop / Cursor / Continue to this proxy via the{" "}
          <span className="font-mono text-xs">Connect</span> page.
        </li>
        <li>
          Watch live traffic and telemetry on{" "}
          <span className="font-mono text-xs">Overview</span> and{" "}
          <span className="font-mono text-xs">Traffic</span>.
        </li>
      </ul>
      <div className="flex justify-end">
        <button className="btn btn-primary" disabled={busy} onClick={onFinish}>
          {busy ? "Finishing…" : "Finish setup"}
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
