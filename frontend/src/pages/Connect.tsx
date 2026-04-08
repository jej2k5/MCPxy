import { useEffect, useMemo, useState } from "react";
import { Check, Copy, Terminal } from "lucide-react";
import { apiGet } from "../api/client";
import { SectionCard } from "../components/Card";
import { Badge } from "../components/Badge";

const CLIENTS = [
  {
    id: "claude-desktop",
    label: "Claude Desktop",
    blurb:
      "Claude Desktop only supports stdio MCP servers. The install command registers `mcp-proxy stdio --connect URL` so the desktop app can talk to your running MCPy proxy.",
  },
  {
    id: "claude-code",
    label: "Claude Code",
    blurb: "Claude Code (CLI) supports HTTP transport. The snippet adds an HTTP MCP server entry.",
  },
  {
    id: "chatgpt",
    label: "ChatGPT",
    blurb:
      "ChatGPT does not write a local config file. Copy the snippet below and paste it into the connector configuration.",
  },
];

interface InstallSnippet {
  client: string;
  supports_auto_install: boolean;
  entry: Record<string, unknown>;
  merged: Record<string, unknown>;
  config_paths: string[];
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="btn btn-ghost text-xs"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* ignore */
        }
      }}
    >
      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export default function Connect() {
  const [tab, setTab] = useState(CLIENTS[0].id);
  const [url, setUrl] = useState(window.location.origin);
  const [tokenEnv, setTokenEnv] = useState("");
  const [snippet, setSnippet] = useState<InstallSnippet | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams();
    params.set("url", url);
    if (tokenEnv) params.set("token_env", tokenEnv);
    apiGet<InstallSnippet>(`/admin/api/install/${tab}?${params.toString()}`)
      .then(setSnippet)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed"));
  }, [tab, url, tokenEnv]);

  const installCommand = useMemo(() => {
    const parts = [
      "mcp-proxy install",
      `--client ${tab}`,
      `--url ${JSON.stringify(url)}`,
    ];
    if (tokenEnv) parts.push(`--token-env ${tokenEnv}`);
    return parts.join(" ");
  }, [tab, url, tokenEnv]);

  const entryText = snippet ? JSON.stringify(snippet.entry, null, 2) : "";
  const mergedText = snippet ? JSON.stringify(snippet.merged, null, 2) : "";
  const active = CLIENTS.find((c) => c.id === tab)!;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-slate-100">Connect a client</h1>
        <p className="text-sm text-slate-400">
          One-click setup for Claude Desktop, Claude Code, and ChatGPT.
        </p>
      </header>

      <div className="card">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
              MCPy URL
            </label>
            <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
              Token env var (optional)
            </label>
            <input
              className="input"
              value={tokenEnv}
              onChange={(e) => setTokenEnv(e.target.value)}
              placeholder="MCP_PROXY_TOKEN"
            />
          </div>
        </div>
      </div>

      <div className="flex gap-2">
        {CLIENTS.map((c) => (
          <button
            key={c.id}
            className={`btn ${tab === c.id ? "btn-primary" : ""}`}
            onClick={() => setTab(c.id)}
          >
            {c.label}
          </button>
        ))}
      </div>

      <SectionCard title={active.label}>
        <p className="mb-4 text-sm text-slate-400">{active.blurb}</p>

        {snippet?.supports_auto_install && (
          <div className="mb-4 rounded-lg border border-accent-500/40 bg-accent-500/5 p-4">
            <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-accent-400">
              <Terminal className="h-4 w-4" />
              One-line install
            </div>
            <div className="flex items-center justify-between gap-2">
              <code className="block flex-1 break-all rounded-md bg-surface-900 p-2 font-mono text-xs text-slate-200">
                {installCommand}
              </code>
              <CopyButton text={installCommand} />
            </div>
            {snippet.config_paths.length > 0 && (
              <p className="mt-2 text-xs text-slate-500">
                Will write to: <code>{snippet.config_paths[0]}</code>
              </p>
            )}
          </div>
        )}

        {snippet && (
          <>
            <div className="mb-4">
              <div className="mb-2 flex items-center justify-between">
                <Badge tone="accent">JSON entry</Badge>
                <CopyButton text={entryText} />
              </div>
              <pre className="max-h-60 overflow-auto scroll-thin rounded-lg bg-surface-900 p-3 font-mono text-xs text-slate-200">
                {entryText}
              </pre>
            </div>

            <div>
              <div className="mb-2 flex items-center justify-between">
                <Badge>Full merged config (preview)</Badge>
                <CopyButton text={mergedText} />
              </div>
              <pre className="max-h-60 overflow-auto scroll-thin rounded-lg bg-surface-900 p-3 font-mono text-xs text-slate-200">
                {mergedText}
              </pre>
            </div>
          </>
        )}

        {error && !snippet && (
          <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            {error}
          </div>
        )}
      </SectionCard>
    </div>
  );
}
