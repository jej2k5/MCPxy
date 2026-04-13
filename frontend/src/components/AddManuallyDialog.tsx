import { useMemo, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { apiPost, ApiError } from "../api/client";
import type {
  HttpAuthPayload,
  ManualUpstreamConfig,
  ManualUpstreamRequest,
  ManualUpstreamResponse,
} from "../api/types";

/**
 * "Add manually" dialog for the Browse page.
 *
 * Lets operators register an arbitrary stdio or http upstream without
 * going through the catalog. Posts to ``/admin/api/upstreams`` — the
 * same endpoint the CLI ``mcpxy-proxy register`` and the catalog
 * installer use, so it routes through the runtime apply pipeline and
 * inherits ${secret:NAME} expansion + atomic rollback for free.
 *
 * The form covers every config shape the backend supports today:
 *
 *   - stdio: command + args + per-upstream env vars + queue_size
 *   - http:  url + timeout + free-form headers
 *   - http auth taxonomy: none / bearer / api_key / basic / oauth2
 *
 * For oauth2 the form just persists the configuration block; the
 * actual interactive linking flow lives on the Routes page (where the
 * operator clicks "Link" and the backend opens the authorization URL).
 * That separation matches how the backend models OAuth: config first,
 * tokens second.
 *
 * Secret values can be entered as literals OR as ``${secret:NAME}``
 * placeholders that resolve at apply time. Bearer / API key / Basic
 * password / OAuth client_secret all hint at this in their helper
 * text but don't enforce a format because operators may legitimately
 * want a literal during local development.
 */

const NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_\-]*$/;

type Transport = "stdio" | "http";
type AuthType = "none" | "bearer" | "api_key" | "basic" | "oauth2";

type KeyValue = { key: string; value: string };

function emptyKv(): KeyValue {
  return { key: "", value: "" };
}

function kvToRecord(items: KeyValue[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const { key, value } of items) {
    const k = key.trim();
    if (!k) continue;
    out[k] = value;
  }
  return out;
}

export default function AddManuallyDialog({
  onClose,
  onInstalled,
}: {
  onClose: () => void;
  onInstalled: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<Transport>("stdio");

  // stdio fields
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [stdioEnv, setStdioEnv] = useState<KeyValue[]>([emptyKv()]);
  const [queueSize, setQueueSize] = useState<string>("");

  // http fields
  const [url, setUrl] = useState("");
  const [timeoutS, setTimeoutS] = useState<string>("");
  const [httpHeaders, setHttpHeaders] = useState<KeyValue[]>([emptyKv()]);
  const [authType, setAuthType] = useState<AuthType>("none");

  // bearer
  const [bearerToken, setBearerToken] = useState("");
  // api_key
  const [apiKeyHeader, setApiKeyHeader] = useState("X-Api-Key");
  const [apiKeyValue, setApiKeyValue] = useState("");
  // basic
  const [basicUser, setBasicUser] = useState("");
  const [basicPass, setBasicPass] = useState("");
  // oauth2
  const [oauthIssuer, setOauthIssuer] = useState("");
  const [oauthAuthEndpoint, setOauthAuthEndpoint] = useState("");
  const [oauthTokenEndpoint, setOauthTokenEndpoint] = useState("");
  const [oauthClientId, setOauthClientId] = useState("");
  const [oauthClientSecret, setOauthClientSecret] = useState("");
  const [oauthScopes, setOauthScopes] = useState("");
  const [oauthDynamicReg, setOauthDynamicReg] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [replace, setReplace] = useState(false);

  const nameValid = NAME_RE.test(name);
  const canSubmit = useMemo(() => {
    if (!nameValid) return false;
    if (transport === "stdio") {
      return command.trim().length > 0;
    }
    if (transport === "http") {
      if (!url.trim()) return false;
      if (authType === "bearer" && !bearerToken) return false;
      if (authType === "api_key" && (!apiKeyHeader || !apiKeyValue)) return false;
      if (authType === "basic" && !basicUser) return false;
      if (authType === "oauth2") {
        const hasEndpoints = oauthAuthEndpoint && oauthTokenEndpoint;
        if (!oauthIssuer && !hasEndpoints) return false;
        if (!oauthClientId && !oauthDynamicReg) return false;
      }
      return true;
    }
    return false;
  }, [
    nameValid,
    transport,
    command,
    url,
    authType,
    bearerToken,
    apiKeyHeader,
    apiKeyValue,
    basicUser,
    oauthIssuer,
    oauthAuthEndpoint,
    oauthTokenEndpoint,
    oauthClientId,
    oauthDynamicReg,
  ]);

  function buildAuthPayload(): HttpAuthPayload | null | undefined {
    if (authType === "none") return undefined;
    if (authType === "bearer") return { type: "bearer", token: bearerToken };
    if (authType === "api_key")
      return { type: "api_key", header: apiKeyHeader, value: apiKeyValue };
    if (authType === "basic")
      return { type: "basic", username: basicUser, password: basicPass };
    if (authType === "oauth2") {
      const scopes = oauthScopes
        .split(/[,\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      return {
        type: "oauth2",
        issuer: oauthIssuer || null,
        authorization_endpoint: oauthAuthEndpoint || null,
        token_endpoint: oauthTokenEndpoint || null,
        client_id: oauthClientId || null,
        client_secret: oauthClientSecret || null,
        scopes,
        dynamic_registration: oauthDynamicReg,
      };
    }
    return null;
  }

  function buildPayload(): ManualUpstreamConfig | null {
    if (transport === "stdio") {
      const args = argsText
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      const cfg: ManualUpstreamConfig = {
        type: "stdio",
        command: command.trim(),
        args,
      };
      const env = kvToRecord(stdioEnv);
      if (Object.keys(env).length > 0) cfg.env = env;
      const qs = parseInt(queueSize, 10);
      if (!Number.isNaN(qs) && qs > 0) cfg.queue_size = qs;
      return cfg;
    }
    if (transport === "http") {
      const cfg: ManualUpstreamConfig = {
        type: "http",
        url: url.trim(),
      };
      const t = parseFloat(timeoutS);
      if (!Number.isNaN(t) && t > 0) cfg.timeout_s = t;
      const headers = kvToRecord(httpHeaders);
      if (Object.keys(headers).length > 0) cfg.headers = headers;
      const auth = buildAuthPayload();
      if (auth !== undefined) cfg.auth = auth;
      return cfg;
    }
    return null;
  }

  async function submit() {
    setError(null);
    setResult(null);
    setWarning(null);
    const config = buildPayload();
    if (config === null) {
      setError("could not build upstream payload");
      return;
    }
    const body: ManualUpstreamRequest = { name, config, replace };
    setBusy(true);
    try {
      const res = await apiPost<ManualUpstreamResponse>(
        "/admin/api/upstreams",
        body,
      );
      if (res.applied) {
        if (res.warning) {
          setResult(`Registered upstream '${name}', but it may not be working:`);
          setWarning(res.warning);
        } else {
          setResult(`Registered upstream '${name}'.`);
        }
        onInstalled(name);
      } else {
        setError(res.error || "registration returned applied=false");
      }
    } catch (e) {
      if (e instanceof ApiError) {
        setError(`${e.status}: ${e.message}`);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/60 p-4">
      <div className="card max-h-[90vh] w-full max-w-2xl space-y-4 overflow-y-auto">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">
              Add MCP server manually
            </h2>
            <p className="text-xs text-slate-400">
              Register an arbitrary stdio command or HTTP upstream.
              Values may use{" "}
              <span className="font-mono">${`{secret:NAME}`}</span> for
              anything you've stored under <span className="font-mono">/admin/api/secrets</span>.
            </p>
          </div>
          <button
            type="button"
            className="text-sm text-slate-400 hover:text-slate-200"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <Field label="Upstream name" hint="Lowercase letters, digits, _ and - only">
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. linear, github-pat, internal-rag"
          />
          {name && !nameValid && (
            <p className="mt-1 text-xs text-err">
              Must match [A-Za-z0-9_][A-Za-z0-9_-]*
            </p>
          )}
        </Field>

        <Field label="Transport">
          <div className="flex gap-2">
            {(["stdio", "http"] as Transport[]).map((t) => (
              <button
                key={t}
                type="button"
                className={
                  "rounded px-3 py-1 text-xs " +
                  (transport === t
                    ? "bg-accent-500/20 text-accent-200 ring-1 ring-accent-400"
                    : "bg-surface-800 text-slate-400 hover:text-slate-200")
                }
                onClick={() => setTransport(t)}
              >
                {t}
              </button>
            ))}
          </div>
        </Field>

        {transport === "stdio" && (
          <>
            <Field label="Command" hint="Executable invoked by create_subprocess_exec">
              <input
                className="input"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="e.g. npx, uvx, python"
              />
            </Field>
            <Field label="Arguments (one per line)">
              <textarea
                className="input min-h-[100px] font-mono text-xs"
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
                placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/data"}
              />
            </Field>
            <KeyValueEditor
              label="Environment variables"
              hint="Per-upstream env overlay. Use ${secret:NAME} for credentials."
              items={stdioEnv}
              onChange={setStdioEnv}
              valuePlaceholder="${secret:github_token}"
            />
            <Field label="Queue size (advanced)">
              <input
                type="number"
                min={1}
                className="input w-32"
                value={queueSize}
                onChange={(e) => setQueueSize(e.target.value)}
                placeholder="200"
              />
            </Field>
          </>
        )}

        {transport === "http" && (
          <>
            <Field label="URL">
              <input
                type="url"
                className="input"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://api.example.com/mcp"
              />
            </Field>
            <Field label="Timeout (seconds)">
              <input
                type="number"
                min={1}
                step={0.5}
                className="input w-32"
                value={timeoutS}
                onChange={(e) => setTimeoutS(e.target.value)}
                placeholder="30"
              />
            </Field>
            <KeyValueEditor
              label="Static headers"
              hint="Sent on every request. Use ${secret:NAME} for credentials."
              items={httpHeaders}
              onChange={setHttpHeaders}
              keyPlaceholder="X-Workspace-Id"
              valuePlaceholder="wk_42"
            />
            <Field label="Authentication">
              <div className="flex flex-wrap gap-2">
                {(
                  ["none", "bearer", "api_key", "basic", "oauth2"] as AuthType[]
                ).map((a) => (
                  <button
                    key={a}
                    type="button"
                    className={
                      "rounded px-3 py-1 text-xs " +
                      (authType === a
                        ? "bg-accent-500/20 text-accent-200 ring-1 ring-accent-400"
                        : "bg-surface-800 text-slate-400 hover:text-slate-200")
                    }
                    onClick={() => setAuthType(a)}
                  >
                    {a}
                  </button>
                ))}
              </div>
            </Field>

            {authType === "bearer" && (
              <Field label="Bearer token" hint="Sent as 'Authorization: Bearer <token>'">
                <input
                  type="text"
                  className="input font-mono text-xs"
                  value={bearerToken}
                  onChange={(e) => setBearerToken(e.target.value)}
                  placeholder="${secret:linear_token}"
                />
              </Field>
            )}

            {authType === "api_key" && (
              <>
                <Field label="Header name">
                  <input
                    className="input"
                    value={apiKeyHeader}
                    onChange={(e) => setApiKeyHeader(e.target.value)}
                  />
                </Field>
                <Field label="Header value">
                  <input
                    type="text"
                    className="input font-mono text-xs"
                    value={apiKeyValue}
                    onChange={(e) => setApiKeyValue(e.target.value)}
                    placeholder="${secret:notion_api_key}"
                  />
                </Field>
              </>
            )}

            {authType === "basic" && (
              <>
                <Field label="Username">
                  <input
                    className="input"
                    value={basicUser}
                    onChange={(e) => setBasicUser(e.target.value)}
                  />
                </Field>
                <Field label="Password">
                  <input
                    type="password"
                    className="input"
                    value={basicPass}
                    onChange={(e) => setBasicPass(e.target.value)}
                    placeholder="${secret:foo_password}"
                  />
                </Field>
              </>
            )}

            {authType === "oauth2" && (
              <div className="space-y-3 rounded-lg border border-surface-700 bg-surface-900/40 p-3">
                <p className="text-xs text-slate-400">
                  Either fill in <span className="font-mono">issuer</span> for
                  RFC 8414 discovery, or supply both endpoint URLs explicitly.
                  Once registered, link the upstream from the Routes page —
                  that triggers the interactive PKCE flow.
                </p>
                <Field label="Issuer URL (preferred)">
                  <input
                    className="input"
                    value={oauthIssuer}
                    onChange={(e) => setOauthIssuer(e.target.value)}
                    placeholder="https://auth.example.com"
                  />
                </Field>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <Field label="Authorization endpoint (override)">
                    <input
                      className="input"
                      value={oauthAuthEndpoint}
                      onChange={(e) => setOauthAuthEndpoint(e.target.value)}
                    />
                  </Field>
                  <Field label="Token endpoint (override)">
                    <input
                      className="input"
                      value={oauthTokenEndpoint}
                      onChange={(e) => setOauthTokenEndpoint(e.target.value)}
                    />
                  </Field>
                </div>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <Field label="Client ID">
                    <input
                      className="input"
                      value={oauthClientId}
                      onChange={(e) => setOauthClientId(e.target.value)}
                      disabled={oauthDynamicReg}
                    />
                  </Field>
                  <Field label="Client secret">
                    <input
                      type="password"
                      className="input"
                      value={oauthClientSecret}
                      onChange={(e) => setOauthClientSecret(e.target.value)}
                      placeholder="${secret:oauth_client_secret}"
                      disabled={oauthDynamicReg}
                    />
                  </Field>
                </div>
                <Field label="Scopes (space- or comma-separated)">
                  <input
                    className="input"
                    value={oauthScopes}
                    onChange={(e) => setOauthScopes(e.target.value)}
                    placeholder="read write"
                  />
                </Field>
                <label className="flex items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={oauthDynamicReg}
                    onChange={(e) => setOauthDynamicReg(e.target.checked)}
                  />
                  Register dynamically (RFC 7591) — skip pre-issued client_id
                </label>
              </div>
            )}
          </>
        )}

        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={replace}
            onChange={(e) => setReplace(e.target.checked)}
          />
          Replace if an upstream with this name already exists
        </label>

        {error && (
          <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            {error}
          </div>
        )}
        {warning && (
          <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn font-mono break-all">
            {warning}
          </div>
        )}
        {result && (
          <div className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok">
            {result}
          </div>
        )}

        <div className="flex justify-end gap-2 border-t border-surface-700 pt-3">
          <button className="btn" onClick={onClose} disabled={busy}>
            Close
          </button>
          <button
            className="btn btn-primary"
            disabled={!canSubmit || busy || !!result}
            onClick={submit}
          >
            {busy ? "Registering…" : "Register upstream"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

function KeyValueEditor({
  label,
  hint,
  items,
  onChange,
  keyPlaceholder = "KEY",
  valuePlaceholder = "value",
}: {
  label: string;
  hint?: string;
  items: KeyValue[];
  onChange: (next: KeyValue[]) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}) {
  function update(idx: number, patch: Partial<KeyValue>) {
    onChange(items.map((it, i) => (i === idx ? { ...it, ...patch } : it)));
  }
  function remove(idx: number) {
    const next = items.filter((_, i) => i !== idx);
    onChange(next.length > 0 ? next : [emptyKv()]);
  }
  function add() {
    onChange([...items, emptyKv()]);
  }
  return (
    <div>
      <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
        {label}
      </label>
      <div className="space-y-2">
        {items.map((it, idx) => (
          <div key={idx} className="flex gap-2">
            <input
              className="input flex-1 font-mono text-xs"
              value={it.key}
              onChange={(e) => update(idx, { key: e.target.value })}
              placeholder={keyPlaceholder}
            />
            <input
              className="input flex-[2] font-mono text-xs"
              value={it.value}
              onChange={(e) => update(idx, { value: e.target.value })}
              placeholder={valuePlaceholder}
            />
            <button
              type="button"
              className="rounded px-2 text-slate-500 hover:text-err"
              onClick={() => remove(idx)}
              aria-label="Remove row"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="text-xs text-accent-400 hover:text-accent-300"
          onClick={add}
        >
          <Plus className="mr-1 inline h-3 w-3" />
          Add row
        </button>
      </div>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}
