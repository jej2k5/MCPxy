import { useEffect, useRef, useState } from "react";
import { apiGet } from "../api/client";
import type {
  MetricsResponse,
  RouteSnapshot,
  TrafficRecord,
  TrafficStatus,
} from "../api/types";
import { subscribeTraffic } from "../api/sse";
import { Badge } from "../components/Badge";
import { SectionCard } from "../components/Card";
import {
  COLORS,
  TrafficGraph,
  type GraphEdge,
  type GraphNode,
} from "../components/TrafficGraph";

/**
 * Real-time proxy → upstream traffic graph.
 *
 * Data sources:
 *  - ``GET /admin/api/routes``  — polled every 5s for topology + health.
 *  - ``GET /admin/api/metrics`` — polled every 5s for the hover tooltip
 *    (authoritative per-upstream counters and latency percentiles).
 *  - ``GET /admin/api/traffic/stream`` — SSE of every ``TrafficRecord``,
 *    consumed via ``subscribeTraffic()``.
 *
 * Hot-path invariants:
 *  1. Incoming SSE events are buffered into ``pendingRef`` and flushed at
 *     most once per animation frame via ``requestAnimationFrame``. At high
 *     rps this caps re-renders to 60/s instead of one-per-record.
 *  2. Pulse ``<circle>``s are appended **imperatively** to ``pulseLayerRef``
 *     via the Web Animations API. They bypass React reconciliation entirely
 *     and self-remove on ``animationend``.
 *  3. If the tab is hidden we drop the SSE events on the floor — otherwise
 *     a backgrounded wall-display accumulates a huge catch-up burst.
 *
 * Edge intensity is tracked as an exponentially-decaying EWMA
 * (``okEWMA`` + ``errEWMA``). The decay factor depends on the selected
 * window. This is intentionally divorced from ``/admin/api/metrics`` — the
 * decaying counters drive the *live feel* (edge width, pulse spawning),
 * while the metrics endpoint is the ground truth shown in the hover
 * tooltip.
 */

type WindowS = 60 | 300 | 900;

interface EdgeStats {
  okEWMA: number;
  errEWMA: number;
}

const PROXY_ID = "__mcpy_proxy__";
const VIEW_W = 800;
const VIEW_H = 600;
const CENTER_X = VIEW_W / 2;
const CENTER_Y = VIEW_H / 2;
const RING_RADIUS = 220;

// Per-1s decay factor. Chosen so that intensity visibly drops within the
// selected window but doesn't snap to zero the instant a burst ends.
const DECAY_PER_SEC: Record<WindowS, number> = {
  60: 0.94, // ~11s half-life
  300: 0.985, // ~46s half-life
  900: 0.995, // ~140s half-life
};

// Larger k → saturates slower → "busier" needed to max out width.
const INTENSITY_K: Record<WindowS, number> = {
  60: 6,
  300: 25,
  900: 90,
};

const PULSE_DURATION_MS = 700;

function computeLayout(
  routes: RouteSnapshot,
): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  positions.set(PROXY_ID, { x: CENTER_X, y: CENTER_Y });
  const names = Object.keys(routes).sort();
  const n = names.length;
  if (n === 0) return positions;
  for (let i = 0; i < n; i++) {
    // Start at top (-π/2) and distribute clockwise.
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
    positions.set(names[i], {
      x: CENTER_X + RING_RADIUS * Math.cos(angle),
      y: CENTER_Y + RING_RADIUS * Math.sin(angle),
    });
  }
  return positions;
}

function isHealthy(info: RouteSnapshot[string] | undefined): boolean {
  if (!info) return false;
  const h = info.health as Record<string, unknown>;
  if (!h) return false;
  return h.status === "ok" || h.running === true;
}

function transportOf(info: RouteSnapshot[string] | undefined): string | undefined {
  if (!info) return undefined;
  const h = info.health as Record<string, unknown>;
  const t = h?.type;
  return typeof t === "string" ? t : undefined;
}

function widthForIntensity(intensity: number, windowS: WindowS): number {
  const k = INTENSITY_K[windowS];
  const normalized = 1 - Math.exp(-intensity / k);
  return 1.5 + normalized * 12.5; // 1.5..14 px
}

function colorForStats(stats: EdgeStats): string {
  const total = stats.okEWMA + stats.errEWMA;
  if (total < 0.05) return COLORS.edgeIdle;
  const errRatio = stats.errEWMA / total;
  if (errRatio > 0.25) return COLORS.err;
  if (errRatio > 0.05) return COLORS.warn;
  return COLORS.ok;
}

function opacityForIntensity(intensity: number): number {
  if (intensity < 0.15) return 0.3;
  return 0.85;
}

function radiusForIntensity(intensity: number): number {
  return 26 + Math.min(12, Math.log2(1 + intensity) * 2.5);
}

export default function Graph() {
  const [routes, setRoutes] = useState<RouteSnapshot>({});
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [windowS, setWindowS] = useState<WindowS>(60);

  // Rolling per-edge EWMAs. Mutated in-place from the rAF flush; React never
  // reads this via state, only forceRender'd to pick it up.
  const edgeStatsRef = useRef<Record<string, EdgeStats>>({});
  // Coordinates for every node (proxy + upstreams). Recomputed each render.
  const positionsRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  // Dedicated SVG group receiving imperatively-created pulse circles.
  const pulseLayerRef = useRef<SVGGElement>(null);
  // Buffer of SSE records waiting for the next rAF flush.
  const pendingRef = useRef<TrafficRecord[]>([]);
  // Guard so we schedule exactly one rAF per frame regardless of rps.
  const scheduledRef = useRef<boolean>(false);

  // Using a discarded useState tick as a forceRender primitive.
  const [, setTick] = useState(0);
  const forceRender = () => setTick((t) => (t + 1) % 1_000_000);

  // ---------------------------------------------------------------------
  // 1. Poll routes + metrics every 5s. Metrics feeds the hover tooltip;
  //    routes supplies topology + health.
  // ---------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const [r, m] = await Promise.all([
          apiGet<RouteSnapshot>("/admin/api/routes"),
          apiGet<MetricsResponse>("/admin/api/metrics"),
        ]);
        if (!cancelled) {
          setRoutes(r);
          setMetrics(m);
          setFatalError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setFatalError(
            err instanceof Error ? err.message : "Failed to load graph data",
          );
        }
      }
    }
    refresh();
    const iv = setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  // ---------------------------------------------------------------------
  // 2. SSE subscription. Push records into pendingRef and schedule a rAF
  //    flush. Drops events while the tab is hidden so a backgrounded
  //    dashboard doesn't accumulate a catch-up burst.
  // ---------------------------------------------------------------------
  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        for await (const evt of subscribeTraffic(ctrl.signal)) {
          if (evt.type !== "record") continue;
          if (document.visibilityState !== "visible") continue;
          pendingRef.current.push(evt.record);
          if (!scheduledRef.current) {
            scheduledRef.current = true;
            requestAnimationFrame(flushPending);
          }
        }
      } catch {
        /* stream ended — consistent with Overview/Traffic behavior */
      }
    })();
    return () => {
      ctrl.abort();
      // Clear any lingering imperatively-created pulses on unmount so the
      // DOM doesn't leak across page navigations.
      const layer = pulseLayerRef.current;
      if (layer) {
        while (layer.firstChild) layer.removeChild(layer.firstChild);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------------------------------------------------------------------
  // 3. Decay tick. Every 1s, multiply every EWMA by the window's decay
  //    factor. Drops entries that have decayed below a noise floor.
  // ---------------------------------------------------------------------
  useEffect(() => {
    const iv = setInterval(() => {
      const decay = DECAY_PER_SEC[windowS];
      const stats = edgeStatsRef.current;
      let touched = false;
      for (const key of Object.keys(stats)) {
        const s = stats[key];
        const sum = s.okEWMA + s.errEWMA;
        if (sum < 0.01) {
          delete stats[key];
          touched = true;
        } else {
          s.okEWMA *= decay;
          s.errEWMA *= decay;
          touched = true;
        }
      }
      if (touched) forceRender();
    }, 1000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowS]);

  // ---------------------------------------------------------------------
  // Hot-path helpers. Re-created per render but only close over refs.
  // ---------------------------------------------------------------------
  function flushPending() {
    scheduledRef.current = false;
    const pending = pendingRef.current;
    if (pending.length === 0) return;
    pendingRef.current = [];

    const stats = edgeStatsRef.current;
    // Track the first status seen per edge this frame — we only spawn one
    // pulse per edge per frame regardless of how many records land.
    const firstStatusByEdge = new Map<string, TrafficStatus>();

    for (const rec of pending) {
      const key = rec.upstream;
      if (!stats[key]) stats[key] = { okEWMA: 0, errEWMA: 0 };
      if (rec.status === "ok") stats[key].okEWMA += 1;
      else stats[key].errEWMA += 1;
      if (!firstStatusByEdge.has(key)) {
        firstStatusByEdge.set(key, rec.status);
      }
    }

    for (const [edge, status] of firstStatusByEdge) {
      spawnPulse(edge, status);
    }
    forceRender();
  }

  function spawnPulse(upstream: string, status: TrafficStatus) {
    const layer = pulseLayerRef.current;
    if (!layer) return;
    const start = positionsRef.current.get(PROXY_ID);
    const end = positionsRef.current.get(upstream);
    if (!start || !end) return;

    const NS = "http://www.w3.org/2000/svg";
    const g = document.createElementNS(NS, "g");
    g.setAttribute("pointer-events", "none");
    const ring = document.createElementNS(NS, "circle");
    ring.setAttribute("cx", "0");
    ring.setAttribute("cy", "0");
    ring.setAttribute("r", "6");
    ring.setAttribute("fill", status === "ok" ? COLORS.ok : COLORS.err);
    ring.setAttribute("opacity", "0.85");
    g.appendChild(ring);
    layer.appendChild(g);

    try {
      const anim = g.animate(
        [
          {
            transform: `translate(${start.x}px, ${start.y}px)`,
            opacity: 0.2,
          },
          {
            transform: `translate(${start.x}px, ${start.y}px)`,
            opacity: 1,
            offset: 0.12,
          },
          {
            transform: `translate(${end.x}px, ${end.y}px)`,
            opacity: 0.1,
          },
        ],
        {
          duration: PULSE_DURATION_MS,
          easing: "ease-out",
          fill: "forwards",
        },
      );
      const cleanup = () => {
        try {
          g.remove();
        } catch {
          /* noop */
        }
      };
      anim.onfinish = cleanup;
      anim.oncancel = cleanup;
    } catch {
      // WAAPI unavailable — drop immediately rather than leak DOM.
      try {
        g.remove();
      } catch {
        /* noop */
      }
    }
  }

  // ---------------------------------------------------------------------
  // Derive nodes/edges for the current render.
  // ---------------------------------------------------------------------
  // positionsRef is mutated here and read asynchronously from spawnPulse.
  // The assignment is idempotent (replacing with a freshly computed map),
  // so it's safe even if React double-invokes render in strict mode.
  positionsRef.current = computeLayout(routes);

  const proxyPos = positionsRef.current.get(PROXY_ID)!;
  const nodes: GraphNode[] = [
    {
      id: PROXY_ID,
      kind: "proxy",
      label: "MCPy",
      x: proxyPos.x,
      y: proxyPos.y,
      radius: 46,
      healthy: true,
    },
  ];
  const edges: GraphEdge[] = [];

  for (const [name, info] of Object.entries(routes)) {
    const pos = positionsRef.current.get(name);
    if (!pos) continue;
    const stats = edgeStatsRef.current[name] ?? { okEWMA: 0, errEWMA: 0 };
    const intensity = stats.okEWMA + stats.errEWMA;

    nodes.push({
      id: name,
      kind: "upstream",
      label: name,
      x: pos.x,
      y: pos.y,
      radius: radiusForIntensity(intensity),
      healthy: isHealthy(info),
      sublabel: transportOf(info),
    });

    edges.push({
      id: `proxy->${name}`,
      fromId: PROXY_ID,
      toId: name,
      x1: proxyPos.x,
      y1: proxyPos.y,
      x2: pos.x,
      y2: pos.y,
      width: widthForIntensity(intensity, windowS),
      color: colorForStats(stats),
      opacity: opacityForIntensity(intensity),
    });
  }

  const upstreamCount = Object.keys(routes).length;
  const droppedSubs = metrics?.dropped_for_subscribers ?? 0;
  const hoveredRoute =
    hoveredId && hoveredId !== PROXY_ID ? routes[hoveredId] : null;
  const hoveredMetrics =
    hoveredId && hoveredId !== PROXY_ID
      ? metrics?.per_upstream?.[hoveredId]
      : null;

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Graph</h1>
          <p className="text-sm text-slate-400">
            Real-time topology of traffic flowing from MCPy to upstream servers
          </p>
        </div>
        <div className="flex items-center gap-3">
          {droppedSubs > 0 && (
            <Badge tone="warn">{droppedSubs} dropped</Badge>
          )}
          <div className="flex overflow-hidden rounded-lg border border-surface-600 bg-surface-800">
            {([60, 300, 900] as const).map((w) => (
              <button
                key={w}
                onClick={() => setWindowS(w)}
                className={`px-3 py-1.5 text-xs font-medium transition ${
                  windowS === w
                    ? "bg-accent-500/15 text-accent-400"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {w === 60 ? "60s" : w === 300 ? "5m" : "15m"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {fatalError && (
        <div className="rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
          {fatalError}
        </div>
      )}

      {upstreamCount === 0 ? (
        <SectionCard title="No upstreams">
          <p className="text-sm text-slate-400">
            No upstreams are configured. Add one from the Browse or Config page
            and it will appear on the graph automatically.
          </p>
        </SectionCard>
      ) : (
        <SectionCard title="Traffic topology">
          <div className="relative">
            <TrafficGraph
              nodes={nodes}
              edges={edges}
              viewBoxWidth={VIEW_W}
              viewBoxHeight={VIEW_H}
              hoveredId={hoveredId}
              onHoverNode={setHoveredId}
              pulseLayerRef={pulseLayerRef}
            />

            {hoveredRoute && (
              <div className="pointer-events-none absolute left-3 top-3 w-64 rounded-lg border border-surface-600 bg-surface-900/95 p-3 shadow-xl">
                <div className="flex items-center justify-between gap-2">
                  <div className="truncate font-mono text-sm text-accent-400">
                    {hoveredId}
                  </div>
                  <Badge tone={isHealthy(hoveredRoute) ? "ok" : "err"}>
                    {isHealthy(hoveredRoute) ? "healthy" : "unhealthy"}
                  </Badge>
                </div>
                {transportOf(hoveredRoute) && (
                  <div className="mt-1 text-xs text-slate-400">
                    transport:{" "}
                    <span className="font-mono text-slate-300">
                      {transportOf(hoveredRoute)}
                    </span>
                  </div>
                )}
                {hoveredMetrics ? (
                  <table className="mt-3 w-full text-xs">
                    <tbody className="text-slate-300">
                      <tr className="border-t border-surface-700">
                        <td className="py-1 text-slate-400">total</td>
                        <td className="py-1 text-right font-mono">
                          {hoveredMetrics.total}
                        </td>
                      </tr>
                      <tr className="border-t border-surface-700">
                        <td className="py-1 text-slate-400">errors</td>
                        <td className="py-1 text-right font-mono">
                          {hoveredMetrics.errors}
                        </td>
                      </tr>
                      <tr className="border-t border-surface-700">
                        <td className="py-1 text-slate-400">p50</td>
                        <td className="py-1 text-right font-mono">
                          {hoveredMetrics.latency_p50_ms} ms
                        </td>
                      </tr>
                      <tr className="border-t border-surface-700">
                        <td className="py-1 text-slate-400">p95</td>
                        <td className="py-1 text-right font-mono">
                          {hoveredMetrics.latency_p95_ms} ms
                        </td>
                      </tr>
                      <tr className="border-t border-surface-700">
                        <td className="py-1 text-slate-400">p99</td>
                        <td className="py-1 text-right font-mono">
                          {hoveredMetrics.latency_p99_ms} ms
                        </td>
                      </tr>
                    </tbody>
                  </table>
                ) : (
                  <div className="mt-3 text-xs text-slate-500">
                    No traffic in metrics window
                  </div>
                )}
              </div>
            )}

            <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap items-center gap-3 rounded-lg border border-surface-600 bg-surface-900/80 px-3 py-2 text-xs text-slate-400">
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-2 w-4 rounded-sm"
                  style={{ background: COLORS.ok }}
                />
                healthy traffic
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-2 w-4 rounded-sm"
                  style={{ background: COLORS.warn }}
                />
                some errors
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-2 w-4 rounded-sm"
                  style={{ background: COLORS.err }}
                />
                many errors
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-2 w-4 rounded-sm"
                  style={{ background: COLORS.edgeIdle }}
                />
                idle
              </span>
            </div>
          </div>
        </SectionCard>
      )}
    </div>
  );
}
