import type { RefObject } from "react";

/**
 * Generic radial traffic graph.
 *
 * The component is deliberately layout-agnostic: the parent computes the x/y
 * of every node and edge, and this component just paints SVG. For v1 the
 * only caller is the Graph page, which places a single ``kind: "proxy"``
 * node at the center and arranges ``kind: "upstream"`` nodes around it on a
 * circle. Keeping the API generic leaves room for a future client-fan-in
 * layout without touching this file.
 *
 * Pulses are NOT rendered here. The parent owns a ``SVGGElement`` ref
 * (``pulseLayerRef``) pointing at the ``<g data-layer="pulses">`` element
 * and appends ``<circle>`` nodes directly to it from the rAF-batched SSE
 * flush. That keeps per-request DOM churn out of React's reconciliation
 * path — important when the proxy is sustaining hundreds of rps.
 */

export interface GraphNode {
  id: string;
  kind: "proxy" | "upstream";
  label: string;
  x: number;
  y: number;
  radius: number;
  healthy: boolean;
  /** Optional transport label shown under the node (e.g. "stdio" | "http"). */
  sublabel?: string;
}

export interface GraphEdge {
  id: string;
  fromId: string;
  toId: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** Stroke width in SVG user units. */
  width: number;
  /** Hex color string (see COLORS below). */
  color: string;
  /** 0..1 stroke opacity. */
  opacity: number;
}

export interface TrafficGraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  viewBoxWidth: number;
  viewBoxHeight: number;
  hoveredId: string | null;
  onHoverNode: (id: string | null) => void;
  /** Imperatively-managed pulse layer. Parent appends circles on SSE events. */
  pulseLayerRef: RefObject<SVGGElement>;
}

// Hex mirrors of the Tailwind theme tokens. SVG stroke/fill attributes don't
// accept Tailwind class names, and the design review explicitly allowed
// raw hex when SVG / Recharts primitives require it. Keep in sync with
// frontend/tailwind.config.js.
export const COLORS = {
  ok: "#34d399",
  err: "#f87171",
  warn: "#fbbf24",
  accent: "#5b8cff",
  accentDim: "#7da3ff",
  edgeIdle: "#3a4257",
  proxyFill: "#161a24",
  nodeFill: "#11141c",
  labelPrimary: "#e2e8f0",
  labelMuted: "#94a3b8",
} as const;

const MONO_STACK = "ui-monospace, SFMono-Regular, Menlo, monospace";

export function TrafficGraph({
  nodes,
  edges,
  viewBoxWidth,
  viewBoxHeight,
  hoveredId,
  onHoverNode,
  pulseLayerRef,
}: TrafficGraphProps) {
  return (
    <svg
      viewBox={`0 0 ${viewBoxWidth} ${viewBoxHeight}`}
      className="w-full"
      style={{ maxHeight: "70vh", display: "block" }}
      role="img"
      aria-label="MCP proxy traffic topology"
    >
      <defs>
        <radialGradient id="mcpxy-proxy-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={COLORS.accent} stopOpacity="0.35" />
          <stop offset="70%" stopColor={COLORS.accent} stopOpacity="0.05" />
          <stop offset="100%" stopColor={COLORS.accent} stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* Edges — painted first so nodes cover the endpoints. */}
      <g data-layer="edges">
        {edges.map((e) => {
          const isHighlighted = hoveredId === e.fromId || hoveredId === e.toId;
          return (
            <line
              key={e.id}
              x1={e.x1}
              y1={e.y1}
              x2={e.x2}
              y2={e.y2}
              stroke={e.color}
              strokeWidth={e.width}
              strokeOpacity={isHighlighted ? Math.min(1, e.opacity + 0.25) : e.opacity}
              strokeLinecap="round"
              style={{
                transition:
                  "stroke-width 400ms ease-out, stroke 400ms ease-out, stroke-opacity 200ms ease-out",
              }}
            />
          );
        })}
      </g>

      {/*
       * Pulses live in this <g>. React never touches its children — the parent
       * Graph page appends <circle> elements directly and they self-remove
       * on animationend. See frontend/src/pages/Graph.tsx.
       */}
      <g data-layer="pulses" ref={pulseLayerRef} />

      {/* Nodes. */}
      <g data-layer="nodes">
        {nodes.map((n) => {
          const isProxy = n.kind === "proxy";
          const isHovered = hoveredId === n.id;
          const stroke = isProxy
            ? COLORS.accent
            : n.healthy
              ? COLORS.ok
              : COLORS.err;
          const fill = isProxy ? COLORS.proxyFill : COLORS.nodeFill;
          return (
            <g
              key={n.id}
              transform={`translate(${n.x}, ${n.y})`}
              onMouseEnter={() => onHoverNode(n.id)}
              onMouseLeave={() => onHoverNode(null)}
              style={{ cursor: "pointer" }}
            >
              {isProxy && <circle r={110} fill="url(#mcpxy-proxy-glow)" pointerEvents="none" />}
              {isHovered && (
                <circle
                  r={n.radius + 6}
                  fill="none"
                  stroke={stroke}
                  strokeOpacity={0.35}
                  strokeWidth={2}
                  pointerEvents="none"
                />
              )}
              <circle
                r={n.radius}
                fill={fill}
                stroke={stroke}
                strokeWidth={isProxy ? 3 : 2}
                style={{ transition: "r 400ms ease-out, stroke 400ms ease-out" }}
              />
              <text
                y={isProxy ? 5 : -(n.radius + 10)}
                textAnchor="middle"
                fontSize={isProxy ? 15 : 13}
                fontWeight={isProxy ? 600 : 500}
                fill={COLORS.labelPrimary}
                fontFamily={MONO_STACK}
                pointerEvents="none"
              >
                {n.label}
              </text>
              {!isProxy && n.sublabel && (
                <text
                  y={n.radius + 16}
                  textAnchor="middle"
                  fontSize={10}
                  fill={COLORS.labelMuted}
                  fontFamily={MONO_STACK}
                  pointerEvents="none"
                >
                  {n.sublabel}
                </text>
              )}
            </g>
          );
        })}
      </g>
    </svg>
  );
}
