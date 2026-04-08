export type TrafficStatus = "ok" | "error" | "timeout" | "denied";

export interface TrafficRecord {
  timestamp: number;
  upstream: string;
  method: string | null;
  request_id: unknown;
  status: TrafficStatus;
  latency_ms: number;
  request_bytes: number;
  response_bytes: number;
  error_code: string | null;
  client_ip: string | null;
}

export interface TrafficListResponse {
  items: TrafficRecord[];
}

export interface UpstreamHealth {
  [key: string]: unknown;
}

export interface RouteSnapshot {
  [name: string]: {
    health: UpstreamHealth;
    discovery: {
      updated_at: number | null;
      ok: boolean | null;
      error?: string | null;
      tools: Array<{ name?: string; description?: string }>;
    };
  };
}

export interface MetricsResponse {
  window_s: number;
  total: number;
  errors: number;
  error_rate: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_p99_ms: number;
  per_upstream: Record<
    string,
    {
      total: number;
      errors: number;
      latency_p50_ms: number;
      latency_p95_ms: number;
      latency_p99_ms: number;
      by_status: Record<string, number>;
    }
  >;
  subscribers: number;
  dropped_for_subscribers: number;
  buffer_size: number;
  buffer_max: number;
  uptime_s?: number;
}

export interface HealthResponse {
  status: string;
  upstreams: Record<string, unknown>;
  telemetry: Record<string, unknown>;
  uptime_s: number;
  version: string;
}

export interface LogEntry {
  timestamp: number;
  logger: string;
  level: string;
  message: string;
  upstream: string | null;
}

export interface AppConfig {
  default_upstream?: string | null;
  auth?: { token_env?: string | null };
  admin?: {
    mount_name?: string;
    enabled?: boolean;
    require_token?: boolean;
    allowed_clients?: string[];
  };
  telemetry?: Record<string, unknown>;
  upstreams?: Record<string, Record<string, unknown>>;
}

export interface CatalogVariable {
  name: string;
  description: string;
  required: boolean;
  default?: string;
  secret: boolean;
}

export interface CatalogEntry {
  id: string;
  name: string;
  description: string;
  category: string;
  homepage: string;
  transport: "stdio" | "http";
  install_hint: string;
  tags: string[];
  variables: CatalogVariable[];
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
}

export interface CatalogResponse {
  version: number;
  updated_at: string;
  categories: string[];
  entries: CatalogEntry[];
}

export interface DiscoveredUpstream {
  source_client: string;
  name: string;
  config: Record<string, unknown>;
  origin_path: string;
  warnings: string[];
}

export interface DiscoveryClient {
  client_id: string;
  display_name: string;
  config_path: string | null;
  detected: boolean;
  upstreams: DiscoveredUpstream[];
}

export interface DiscoveryResponse {
  clients: DiscoveryClient[];
}
