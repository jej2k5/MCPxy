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

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

export type DatabaseDialect = "sqlite" | "postgresql" | "mysql" | string;

export interface OnboardingDatabaseInfo {
  current_url_masked: string;
  current_dialect: DatabaseDialect;
  is_default: boolean;
  bootstrap_file_present: boolean;
  available_dialects: DatabaseDialect[];
}

export interface OnboardingStatus {
  active: boolean;
  completed: boolean;
  expired: boolean;
  required: boolean;
  created_at?: number;
  admin_token_set_at?: number | null;
  first_upstream_at?: number | null;
  completed_at?: number | null;
  completed_by?: string | null;
  ttl_s?: number;
  expires_at?: number | null;
  database?: OnboardingDatabaseInfo;
}

export interface OnboardingSetTokenResponse {
  applied: boolean;
  onboarding: OnboardingStatus;
}

export interface OnboardingDatabaseRequest {
  /** Raw SQLAlchemy URL. Mutually exclusive with the structured fields. */
  url?: string;
  dialect?: DatabaseDialect;
  host?: string;
  port?: number | null;
  database?: string;
  user?: string;
  password?: string;
  sslmode?: string;
  /** Required on set_database when switching dialect. */
  secrets_key_ack?: boolean;
}

export interface OnboardingTestDatabaseResponse {
  ok: boolean;
  dialect?: DatabaseDialect;
  url_masked?: string;
  error?: string;
}

export type OnboardingSetDatabaseMode = "hot_swap" | "restart_required";

export interface OnboardingSetDatabaseResponse {
  ok: boolean;
  mode: OnboardingSetDatabaseMode;
  onboarding: OnboardingStatus;
}

// ---------------------------------------------------------------------------
// Manual upstream registration (form on Browse page → POST /admin/api/upstreams)
// ---------------------------------------------------------------------------

export type StdioUpstreamPayload = {
  type: "stdio";
  command: string;
  args: string[];
  env?: Record<string, string>;
  queue_size?: number;
};

export type HttpUpstreamPayload = {
  type: "http";
  url: string;
  timeout_s?: number;
  headers?: Record<string, string>;
  auth?: HttpAuthPayload | null;
};

export type HttpAuthPayload =
  | { type: "none" }
  | { type: "bearer"; token: string }
  | { type: "api_key"; header: string; value: string }
  | { type: "basic"; username: string; password: string }
  | {
      type: "oauth2";
      issuer?: string | null;
      authorization_endpoint?: string | null;
      token_endpoint?: string | null;
      registration_endpoint?: string | null;
      client_id?: string | null;
      client_secret?: string | null;
      scopes?: string[];
      audience?: string | null;
      redirect_uri?: string | null;
      dynamic_registration?: boolean;
    };

export type ManualUpstreamConfig = StdioUpstreamPayload | HttpUpstreamPayload;

export interface ManualUpstreamRequest {
  name: string;
  config: ManualUpstreamConfig;
  replace: boolean;
}

export interface ManualUpstreamResponse {
  applied: boolean;
  diff?: Record<string, unknown>;
  error?: string;
  warning?: string;
  status?: {
    health?: Record<string, unknown>;
    discovery?: Record<string, unknown>;
  };
}

// ---------------------------------------------------------------------------
// Authy multi-provider authentication
// ---------------------------------------------------------------------------

export type AuthyProviderKind = "local" | "google" | "m365" | "sso_oidc" | "sso_saml";

export interface ProvidersResponse {
  providers: string[];
  authy_enabled: boolean;
}

export interface LoginResponse {
  token: string;
  user: { id: string; email: string; name: string; provider: string } | null;
}

export interface MeResponse {
  user_id: number;
  email: string;
  role: string;
  provider: string;
  auth_mode: string;
}

export interface UserRow {
  id: number;
  email: string;
  username: string | null;
  name: string | null;
  provider: string;
  role: string;
  created_at: number;
  invited_by: number | null;
  activated_at: number | null;
  disabled_at: number | null;
}

export interface InviteResponse {
  id: number;
  email: string;
  role: string;
  created_at: number;
  expires_at: number;
  consumed_at: number | null;
  invited_by: number | null;
  plaintext_token?: string;
}

export interface TokenMappingRow {
  id: number;
  upstream: string;
  user_id: number;
  description: string;
  created_at: number;
  updated_at: number;
  token_preview: string;
}

export interface PatRow {
  id: number;
  user_id: number;
  name: string;
  token_prefix: string;
  created_at: number;
  last_used_at: number | null;
  expires_at: number | null;
  revoked_at: number | null;
  plaintext?: string;
}

