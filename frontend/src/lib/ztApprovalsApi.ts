// ZT add-on: order approvals API (extensions/zt_approvals/api_routes.py).
// Same auth helper as lib/api.ts (bearer key from authHeaders(); loopback dev
// mode when no key is stored) and the same ApiError type, so callers can use
// isAuthRequiredError from lib/api.
import { ApiError, AUTH_REQUIRED_MESSAGE } from "@/lib/api";
import { authHeaders } from "@/lib/apiAuth";

export type ProposalStatus =
  | "PENDING"
  | "APPROVED"
  | "SUBMITTED"
  | "FILLED"
  | "FAILED"
  | "REJECTED"
  | "EXPIRED";

export const PROPOSAL_STATUSES: ProposalStatus[] = [
  "PENDING",
  "APPROVED",
  "SUBMITTED",
  "FILLED",
  "FAILED",
  "REJECTED",
  "EXPIRED",
];

export interface ProposalOrder {
  symbol: string;
  side: "buy" | "sell";
  qty: number | null;
  notional: number | null;
  order_type: "market" | "limit";
  limit_price: number | null;
  tif: string;
}

export interface OrderDetail extends ProposalOrder {
  reference_price?: number | null;
  reference_date?: string | null;
  reference_source?: string | null;
  est_qty?: number | null;
  est_notional_usd?: number | null;
}

export interface ProposalSummary {
  id: string;
  status: ProposalStatus;
  created_utc: string;
  expires_utc: string;
  expires_in_seconds: number | null;
  broker: string;
  environment: string;
  account: string;
  orders: ProposalOrder[];
  validation_ok: boolean;
  failed_checks: string[];
  advisory_checks: string[];
  origin: string;
  approvable: boolean;
  content_hash_tail: string;
  rationale: string;
}

export interface ApprovalPolicy {
  max_position_pct?: number;
  max_gross_exposure?: number;
  max_net_exposure?: number;
  max_order_notional_usd?: number;
  price_collar_pct?: number;
  short_capable_sources?: string[];
  source?: string;
  error?: string | null;
}

export interface ProposalList {
  approval_mode: "required" | "off";
  ttl_minutes: number;
  server_time_utc: string;
  policy: ApprovalPolicy;
  ledger: { ok: boolean; record_count: number; first_break: unknown };
  proposals: ProposalSummary[];
}

export interface ValidationCheck {
  name: string;
  status: "PASS" | "FAIL";
  detail: string;
  blocking: boolean;
}

export interface Validation {
  ok: boolean;
  checked_utc: string;
  checks: ValidationCheck[];
}

export interface ProposalSignal {
  source: string;
  symbol: string | null;
  direction: string;
  rationale: string;
  evidence_ids: string[];
  approach: "long_only" | "long_short";
}

export interface DirectionPermission {
  symbol: string;
  current_qty: number | null;
  projected_qty: number | null;
  creates_or_increases_short: boolean;
  short_capable_sources: string[];
  long_only_bearish_sources: string[];
  status: "PASS" | "FAIL";
}

export interface ClampEvent {
  limit: string;
  symbol: string | null;
  before: number;
  after: number;
}

export interface Exposure {
  long_usd?: number | null;
  short_usd?: number | null;
  gross_usd?: number | null;
  net_usd?: number | null;
  gross?: number | null;
  net?: number | null;
  max_name?: number | null;
  max_name_symbol?: string | null;
  leverage?: number | null;
  unpriced?: string[];
}

export interface DecisionRecord {
  rationale?: string;
  evidence_ids?: string[];
  signals?: ProposalSignal[];
  equity?: number | null;
  equity_basis?: string;
  cash?: number | null;
  book_source?: string;
  current_positions?: Record<string, number>;
  projected_positions?: Record<string, number>;
  current_weights?: Record<string, number>;
  requested_targets?: Record<string, number> | null;
  target_weights?: Record<string, number> | null;
  projected_weights?: Record<string, number>;
  clamp_events?: ClampEvent[];
  direction_permissions?: DirectionPermission[];
  exposure_before?: Exposure;
  projected_exposure?: Exposure;
  orders_detail?: OrderDetail[];
  limits?: ApprovalPolicy;
}

export interface Transition {
  from: string | null;
  to: string;
  at_utc: string;
  actor: string;
  reason: string;
  ledger_seq?: number;
  ledger_record_hash?: string;
}

export interface LedgerEvent {
  seq: number;
  at_utc: string;
  event: string;
  from: string | null;
  to: string;
  actor: string;
  reason: string;
  record_hash: string;
}

export interface SubmissionResult {
  symbol: string;
  side: string;
  qty: number | null;
  notional: number | null;
  status: "filled" | "submitted" | "failed" | "not_submitted";
  error: string | null;
  fill_price?: number | null;
  fill_qty?: number | null;
}

export interface ProposalDetail {
  id: string;
  status: ProposalStatus;
  created_utc: string;
  expires_utc: string;
  ttl_minutes: number;
  origin: { kind?: string; tool?: string; actor?: string; session_id?: string };
  broker: { key: string; profile_id: string; environment: string; live: boolean; label?: string };
  account_scope: { account: string; symbols: string[] };
  orders: ProposalOrder[];
  decision_record: DecisionRecord;
  validation: Validation;
  route: { kind: string; target: string | null };
  content_hash?: string;
  content_hash_tail: string;
  expires_in_seconds: number | null;
  approvable: boolean;
  transitions: Transition[];
  approval: { approver: string; approved_utc: string } | null;
  submission: { submitted_utc: string; results: SubmissionResult[] } | null;
  integrity?: { ok: boolean; reason: string | null };
  ledger_events?: LedgerEvent[];
  revalidation?: Validation;
}

export interface PaperPosition {
  symbol: string;
  qty: number;
  avg_cost: number | null;
  mark: number | null;
  mark_date: string | null;
  mark_source: string | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number | null;
  weight: number | null;
}

export interface PaperFill {
  event: "fill" | "reset";
  fill_id?: string;
  proposal_id?: string;
  symbol?: string;
  side?: string;
  qty?: number;
  price?: number;
  price_date?: string;
  notional?: number;
  starting_cash?: number;
  at_utc: string;
}

export interface PaperAccount {
  account: string;
  label: string;
  currency: string;
  cash: number;
  equity: number | null;
  starting_cash: number;
  max_leverage: number;
  gross_exposure: number;
  net_exposure: number;
  leverage: number | null;
  realized_pnl: number;
  positions: PaperPosition[];
  unpriced: string[];
  price_source: string;
  valuation: string;
  updated_utc: string | null;
  fills_count: number;
  recent_fills: PaperFill[];
}

/** An API error that may carry the proposal as the server saw it (e.g. 422 revalidation). */
export class ZtApprovalError extends ApiError {
  proposal?: ProposalDetail;

  constructor(message: string, status: number, code?: string, proposal?: ProposalDetail) {
    super(message, status, code);
    this.name = "ZtApprovalError";
    this.proposal = proposal;
  }
}

async function errorFrom(res: Response): Promise<ZtApprovalError> {
  let message = `HTTP ${res.status}`;
  let code: string | undefined;
  let proposal: ProposalDetail | undefined;
  try {
    const body = await res.json();
    const detail = body?.detail ?? body?.message ?? body?.error;
    if (typeof detail === "string" && detail) {
      message = detail;
    } else if (detail && typeof detail === "object") {
      const structured = detail as { code?: unknown; message?: unknown; proposal?: unknown };
      if (typeof structured.code === "string") code = structured.code;
      if (typeof structured.message === "string" && structured.message) message = structured.message;
      else if (code) message = code;
      if (structured.proposal && typeof structured.proposal === "object") {
        proposal = structured.proposal as ProposalDetail;
      }
    }
  } catch {
    /* non-JSON error body */
  }
  if (res.status === 401 || res.status === 403) message = AUTH_REQUIRED_MESSAGE;
  return new ZtApprovalError(message, res.status, code, proposal);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!res.ok) throw await errorFrom(res);
  const contentType = res.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    // The SPA answered: the ZT routes are not registered on this server.
    throw new ZtApprovalError(`Expected JSON from ${path}, got ${contentType || "no content type"}`, 404, "routes_missing");
  }
  return (await res.json()) as T;
}

const proposalPath = (id: string) => `/zt/orders/proposals/${encodeURIComponent(id)}`;

export const ztApprovalsApi = {
  listProposals: (status?: ProposalStatus | "") =>
    request<ProposalList>(`/zt/orders/proposals${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  getProposal: (id: string) => request<ProposalDetail>(proposalPath(id)),
  approveProposal: (id: string, confirmHash: string) =>
    request<ProposalDetail>(`${proposalPath(id)}/approve`, {
      method: "POST",
      body: JSON.stringify({ confirm_hash: confirmHash }),
    }),
  rejectProposal: (id: string, reason: string) =>
    request<ProposalDetail>(`${proposalPath(id)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  paperAccount: () => request<PaperAccount>("/zt/paper/account"),
  resetPaper: (startingCash?: number) =>
    request<{ account: PaperAccount; rejected_proposals: string[] }>("/zt/paper/reset", {
      method: "POST",
      body: JSON.stringify(startingCash === undefined ? { confirm: "RESET" } : { confirm: "RESET", starting_cash: startingCash }),
    }),
};

/** Seconds left until an ISO-8601 instant (negative once past). */
export function secondsUntil(iso: string, nowMs: number = Date.now()): number {
  const at = Date.parse(iso);
  return Number.isFinite(at) ? Math.floor((at - nowMs) / 1000) : Number.NaN;
}

/** "14:05", "1:02:03", or null when the time is up or unknown. */
export function formatCountdown(seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** The last characters of a content hash, as the approver compares them. */
export function hashTail(hash: string | undefined | null, length = 12): string {
  return (hash ?? "").slice(-length);
}

/** One-line order text, e.g. "BUY 10 AAPL @ 201 LMT". */
export function describeOrder(order: ProposalOrder): string {
  const size = order.qty !== null && order.qty !== undefined ? formatNumber(order.qty) : `$${formatNumber(order.notional ?? 0)}`;
  const price = order.order_type === "limit" && order.limit_price !== null ? ` @ ${formatNumber(order.limit_price)} LMT` : " MKT";
  return `${order.side.toUpperCase()} ${size} ${order.symbol}${price}`;
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function formatPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}
