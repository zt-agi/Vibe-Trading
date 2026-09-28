// ZT add-on: response types for the read-only research dashboards served by
// extensions/zt_dashboards (GET /zt/reports, /zt/reports/{id}, /zt/snapshot/{date}).
// Every response is an envelope carrying provenance and an honest status.

export type ZtStatus = "FRESH" | "STALE" | "MISSING";

export interface ZtFileInfo {
  path: string;
  exists: boolean;
  bytes: number | null;
  mtime_utc: string | null;
  sha256: string | null;
}

export interface ZtSection<T = unknown> {
  source_path: string;
  sha256: string | null;
  as_of: string | null;
  status: ZtStatus;
  data: T | null;
}

/** Every ZT response: provenance, point-in-time label and honest freshness. */
export interface ZtEnvelope<T> {
  tool: string;
  as_of: string | null;
  as_of_basis: string | null;
  retrieved_at: string;
  source_path: string;
  sha256: string | null;
  pit_label: string;
  status: ZtStatus;
  status_rule: string;
  age_hours: number | null;
  authority: string;
  data: T;
  files?: ZtFileInfo[];
  notes?: string[];
  supplementary?: Record<string, ZtSection | null | undefined>;
}

export interface ZtReportItem {
  id: string;
  title: string;
  path: string;
  origin: "root" | "hub" | "hub+root";
  section: string | null;
  role: string | null;
  state: string | null;
  hub_as_of: string | null;
  note: string | null;
  exists: boolean;
  viewable: boolean;
  bytes: number | null;
  mtime_utc: string | null;
  sha256: string | null;
}

export interface ZtReportsData {
  reports: ZtReportItem[];
  counts: { reports: number; viewable: number; missing: number; hub_items: number };
  hub_status: { label?: string; state?: string; note?: string; asOf?: string } | null;
  viewer_policy: string;
}

interface ZtRows<T> {
  count: number;
  rows: T[];
  by_status?: Record<string, number>;
  error?: string;
}

export interface ZtAlert {
  alert_id: string;
  category: string;
  severity: string;
  title: string;
  summary: string;
  evidence_id: string;
  as_of: string;
}

export interface ZtSourceHealth {
  source_id: string;
  label: string;
  status: string;
  observation_date: string;
  age_label: string;
  cadence: string;
}

export interface ZtContextRow {
  symbol?: string;
  series?: string;
  label?: string;
  close?: number | null;
  value?: number | null;
  as_of: string;
  price_status?: string;
  status?: string;
}

export interface ZtGuardrail {
  category: string;
  status: string;
  source: string;
  as_of: string;
  note: string;
}

export interface ZtSnapshotData {
  requested: string;
  resolved_date: string | null;
  latest_available: string | null;
  available_dates: string[];
  complete?: boolean;
  missing_files?: string[];
  alerts?: (ZtRows<ZtAlert> & { by_severity?: Record<string, number> }) | null;
  source_health?: ZtRows<ZtSourceHealth> | null;
  market_context?: ZtRows<ZtContextRow> | null;
  macro_context?: ZtRows<ZtContextRow> | null;
  event_guardrails?: ZtRows<ZtGuardrail> | null;
  signal_readiness?:
    | (ZtRows<{ rank: number; name: string; readiness_status: string }> & {
        by_readiness?: Record<string, number>;
        alpha_ready_count?: number;
      })
    | null;
  portfolio_context?: {
    as_of: string | null;
    reconciliation_status: string | null;
    pnl_status: string | null;
    pnl_value_coverage_pct: number | null;
    policy: string;
    error?: string;
  } | null;
  broker_connectivity?: { status: string | null; order_capability: string | null; error?: string } | null;
}
