export class ApiError extends Error {
  constructor(public status: number, public body: any) {
    super(body?.detail ?? `HTTP ${status}`);
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    cache: "no-store",
    credentials: "same-origin",
    // X-SwingDesk: the portal refuses changes without it, so a cross-site form can't act for a signed-in user.
    headers: { "X-SwingDesk": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const isJson = (r.headers.get("content-type") ?? "").includes("application/json");
  const data = isJson ? await r.json().catch(() => ({})) : {};
  if (r.status === 401 && data?.signin) window.dispatchEvent(new Event("swingdesk:signin"));
  if (!r.ok) throw new ApiError(r.status, data);
  if (!isJson) throw new ApiError(r.status, { detail: `Unexpected non-JSON reply from ${path} (is the API server up to date?)` });
  return data as T;
}

export const api = {
  get: <T,>(p: string) => req<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => req<T>("POST", p, b ?? {}),
  put: <T,>(p: string, b?: unknown) => req<T>("PUT", p, b ?? {}),
  del: <T,>(p: string) => req<T>("DELETE", p),
};

export type Result = "pass" | "fail" | "warn" | "unknown";
export type Status = "QUALIFIES" | "QUALIFIES_WITH_WARNING" | "NEAR_MISS" | "NEEDS_REVIEW" | "NO_SETUP";

export interface Rule {
  id: string; name: string; threshold: string; observed: string; result: Result; margin: string | null; facts: string[];
}
export interface Fact { label: string; value: any; inputs?: Record<string, any>; source?: Record<string, any> }
export interface Evaluation {
  symbol: string; date: string; status: Status; failed: string[]; unknown: string[]; rules: Rule[];
  facts: Record<string, Fact>; suggested_stop: number | null; entry_assumption: number; strategy?: string; strategy_name?: string;
  plan: { entry_date: string; exit_date: string; sessions: string[] }; data_timestamp: string;
}
export interface Params {
  strategy: string;
  ema_period: number; volume_lookback: number; volume_ratio_max: number; hold_sessions: number;
  breakout_lookback: number; volume_ratio_min: number; close_range_min: number; gap_min_pct: number;
  rsi_period: number; rsi_max: number; trend_sma: number; fast_ema: number; slow_sma: number; slope_lookback: number;
  atr_period: number; atr_mult: number;
  earnings_mode: "warn" | "exclude"; stop_buffer_pct: number; risk_per_trade_pct: number; max_position_pct: number;
}
export interface StrategyInfo { id: string; name: string; style: string; template: string; params: string[]; core: string[]; stop_rule: string }
export interface Interpretation { phrase: string; definition: string; params: string[] }
export interface Ruleset {
  version: number | null; name: string; source_text: string; params: Params; interpretations: Interpretation[]; confirmed_at: string | null;
}
export interface LiveFetch {
  fetched_at: string; expected_last_session: string;
  report: Record<string, { ok: boolean; last_bar?: string; bars?: number; next_earnings?: string | null; earnings_timing?: string | null; error?: string }>;
}
export interface Status_ {
  data_mode: "demo" | "live"; live_fetch: LiveFetch | null; watchlist: string[]; data_provider: "yahoo" | "alpaca" | "demo";
  scheduler: { enabled: boolean; thread_alive: boolean; next_run: string | null; running: boolean;
    last: { at: string; trigger: string; result: string } | null } | null;
  last_refresh: { at: string; trigger: string; as_of: string; events: string[]; failed: string[] } | null;
  kill_switch: { on: boolean; reason?: string | null; at?: string };
  reconciliation: Reconciliation | null;
  broker_connected: boolean; environment: "paper" | "real"; live_trading: boolean; ibkr_configured: boolean; mode_label: string;
  stream: { target: string | null; transport: "websocket" | "poll" | null; connected: boolean; last_event_at: string | null;
    events: number; last_error: string | null; reconnects: number; last_sync_at: string | null } | null;
  as_of: string | null; mode: string; alpaca_configured: boolean; llm: string; data_label: string; simulated_data: boolean;
  account: { equity: number; cash: number; buying_power: number; starting_equity: number; positions: any[] };
  fill_model: { slippage_bps: number; participation_cap: number };
}
export interface Order {
  id: string; client_order_id: string; broker: string; symbol: string; side: string; qty: number; order_type: string;
  limit_price: number | null; stop_price: number | null; purpose: string; parent_id: string | null; status: string;
  status_reason: string | null; filled_qty: number; avg_fill_price: number | null; created_at: string;
  fills: { session_date: string; qty: number; price: number; note: string }[];
  children: { id: string; purpose: string; status: string; status_reason: string; qty: number; filled_qty: number; avg_fill_price: number | null; stop_price: number | null }[];
}

export interface RiskCheck {
  id: string; name: string; kind: "hard" | "soft"; threshold: string; observed: string;
  result: "pass" | "block" | "override" | "info"; detail: string;
}
export interface RiskResult { limits_version: number; checks: RiskCheck[]; blocked: boolean; needs_override: string[] }
export interface Reconciliation {
  at: string; mode: string; ok: boolean; checked: number; error?: string;
  breaks: { type: string; symbol: string; detail: string }[];
}
