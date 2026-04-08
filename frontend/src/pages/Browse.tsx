import { useEffect, useMemo, useState } from "react";
import { apiGet, apiPost } from "../api/client";
import type { CatalogEntry, CatalogResponse } from "../api/types";

export default function Browse() {
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>("");
  const [selected, setSelected] = useState<CatalogEntry | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiGet<CatalogResponse>("/admin/api/catalog")
      .then((c) => {
        if (!cancelled) setCatalog(c);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    if (!catalog) return [];
    const q = query.trim().toLowerCase();
    return catalog.entries.filter((entry) => {
      if (category && entry.category !== category) return false;
      if (!q) return true;
      const hay = [entry.id, entry.name, entry.description, ...entry.tags]
        .join(" ")
        .toLowerCase();
      return q.split(/\s+/).every((part) => hay.includes(part));
    });
  }, [catalog, query, category]);

  if (error) {
    return <div className="card text-err">Failed to load catalog: {error}</div>;
  }
  if (!catalog) {
    return <div className="card text-slate-400">Loading catalog…</div>;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Browse MCP servers</h1>
        <p className="text-sm text-slate-400">
          Curated catalog of well-known MCP servers. One click materialises
          the entry into an upstream on this proxy.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          className="input flex-1 min-w-[240px]"
          placeholder="Search (name, description, tags)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="input w-48"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        >
          <option value="">All categories</option>
          {catalog.categories.map((cat) => (
            <option key={cat} value={cat}>
              {cat}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-500">
          {filtered.length} / {catalog.entries.length} servers
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {filtered.map((entry) => (
          <CatalogCard
            key={entry.id}
            entry={entry}
            onInstall={() => setSelected(entry)}
          />
        ))}
      </div>

      {selected && (
        <InstallDialog
          entry={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function CatalogCard({
  entry,
  onInstall,
}: {
  entry: CatalogEntry;
  onInstall: () => void;
}) {
  return (
    <div className="card space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm text-accent-400">{entry.id}</span>
            <span className="rounded-full bg-surface-800 px-2 py-0.5 text-xs text-slate-300">
              {entry.category}
            </span>
            <span className="rounded-full bg-surface-800 px-2 py-0.5 text-xs text-slate-400">
              {entry.transport}
            </span>
          </div>
          <h3 className="mt-1 text-base font-semibold text-slate-100">{entry.name}</h3>
        </div>
        <button className="btn btn-primary" onClick={onInstall}>
          Install
        </button>
      </div>
      <p className="text-sm text-slate-400">{entry.description}</p>
      {entry.install_hint && (
        <p className="text-xs text-slate-500">{entry.install_hint}</p>
      )}
      {entry.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {entry.tags.map((tag) => (
            <span
              key={tag}
              className="rounded bg-surface-800 px-1.5 py-0.5 text-xs text-slate-400"
            >
              {tag}
            </span>
          ))}
        </div>
      )}
      {entry.homepage && (
        <a
          href={entry.homepage}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-accent-400 hover:underline"
        >
          {entry.homepage}
        </a>
      )}
    </div>
  );
}

function InstallDialog({
  entry,
  onClose,
}: {
  entry: CatalogEntry;
  onClose: () => void;
}) {
  const [name, setName] = useState(entry.id);
  const [vars, setVars] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      entry.variables.map((v) => [v.name, v.default ?? ""]),
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  async function install() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await apiPost<{ applied?: boolean; installed?: { name: string } }>(
        "/admin/api/catalog/install",
        { id: entry.id, name, variables: vars },
      );
      if (res.applied) {
        setResult(`Installed as upstream '${res.installed?.name ?? name}'.`);
      } else {
        setError("install returned applied=false");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/60 p-4">
      <div className="card w-full max-w-lg space-y-4">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">
              Install {entry.name}
            </h2>
            <p className="text-xs text-slate-400">{entry.id}</p>
          </div>
          <button
            type="button"
            className="text-sm text-slate-400 hover:text-slate-200"
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <div>
          <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
            Upstream name
          </label>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        {entry.variables.length > 0 && (
          <div className="space-y-3">
            {entry.variables.map((v) => (
              <div key={v.name}>
                <label className="mb-1 block text-xs uppercase tracking-wider text-slate-400">
                  {v.name}
                  {v.required && <span className="text-err"> *</span>}
                </label>
                <input
                  type={v.secret ? "password" : "text"}
                  className="input"
                  placeholder={v.description}
                  value={vars[v.name] ?? ""}
                  onChange={(e) =>
                    setVars({ ...vars, [v.name]: e.target.value })
                  }
                />
                <p className="mt-1 text-xs text-slate-500">{v.description}</p>
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            {error}
          </div>
        )}
        {result && (
          <div className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok">
            {result}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button className="btn" onClick={onClose} disabled={busy}>
            Close
          </button>
          <button
            className="btn btn-primary"
            onClick={install}
            disabled={busy || !!result}
          >
            {busy ? "Installing…" : "Install"}
          </button>
        </div>
      </div>
    </div>
  );
}
