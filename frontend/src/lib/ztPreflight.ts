// ZT add-on: client for GET /zt/preflight (extensions/zt_dashboards/zt_preflight.py).
// Kept apart from lib/api.ts so the ZT add-ons do not collide in one file.
import i18n from "@/i18n";
import { ApiError } from "@/lib/api";
import { authHeaders } from "@/lib/apiAuth";

export type ZtCheckStatus = "OK" | "WARN" | "FAIL";

export interface ZtPreflightCheck {
  id: string;
  label: string;
  status: ZtCheckStatus;
  summary: string;
  /** How to fix; empty for OK checks. */
  fix: string;
  detail: Record<string, unknown>;
}

export interface ZtPreflight {
  tool: "zt_preflight";
  generated_at: string;
  overall: ZtCheckStatus;
  counts: Record<ZtCheckStatus, number>;
  checks: ZtPreflightCheck[];
  note?: string;
}

export const ZT_PREFLIGHT_PATH = "/zt/preflight";

/** Fetch the pre-flight report; errors are ApiErrors so the page can classify them. */
export async function fetchZtPreflight(): Promise<ZtPreflight> {
  const res = await fetch(ZT_PREFLIGHT_PATH, {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body: unknown = await res.json();
      const raw = (body as { detail?: unknown } | null)?.detail;
      if (typeof raw === "string" && raw) detail = raw;
    } catch {
      /* not JSON */
    }
    if (res.status === 401 || res.status === 403) detail = i18n.t("agent.authRequired" as never);
    throw new ApiError(detail, res.status);
  }
  const type = res.headers.get("content-type") || "";
  if (!type.includes("application/json")) {
    // The SPA answered: the route is not registered on this server.
    throw new ApiError(`Expected JSON from ${ZT_PREFLIGHT_PATH}, got ${type || "unknown content type"}`, res.status);
  }
  return (await res.json()) as ZtPreflight;
}

/** One detail value as short display text (objects and arrays as compact JSON). */
export function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    const text = JSON.stringify(value);
    return text.length > 600 ? `${text.slice(0, 600)}…` : text;
  } catch {
    return String(value);
  }
}

/** Problems first (FAIL, WARN), otherwise the server's order. */
export function problemsFirst(checks: ZtPreflightCheck[]): ZtPreflightCheck[] {
  const rank: Record<ZtCheckStatus, number> = { FAIL: 0, WARN: 1, OK: 2 };
  return checks
    .map((check, index) => ({ check, index }))
    .sort((a, b) => rank[a.check.status] - rank[b.check.status] || a.index - b.index)
    .map(({ check }) => check);
}
