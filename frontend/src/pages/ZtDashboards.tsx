// ZT add-on: read-only research dashboards served by extensions/zt_dashboards.
// Labels use t(key, { defaultValue }) under a "zt." prefix, so no locale file
// changes are needed and the locale-parity test is unaffected.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import {
  AlertTriangle,
  ClipboardCheck,
  ExternalLink,
  FileText,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, ApiError, isAuthRequiredError, ztReportPath } from "@/lib/api";
import { withFreshTicket } from "@/lib/apiAuth";
import {
  fetchZtPreflight,
  formatDetailValue,
  problemsFirst,
  type ZtCheckStatus,
  type ZtPreflight,
  type ZtPreflightCheck,
} from "@/lib/ztPreflight";
import type {
  ZtEnvelope,
  ZtReportItem,
  ZtReportsData,
  ZtSnapshotData,
  ZtStatus,
} from "@/lib/ztTypes";
import { cn } from "@/lib/utils";

type Translate = (key: string, defaultValue: string, vars?: Record<string, string | number>) => string;

type LoadError = { kind: "routes" | "auth" | "config" | "other"; message: string };

type ViewerState = { item: ZtReportItem; src: string | null; error: string | null };

/** Sandbox flags for the report frame: scripts and popups, never same-origin. */
export const ZT_REPORT_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox";

/** Parse the shim's link message; anything else is ignored. */
export function parseReportLinkMessage(data: unknown): { id: string; href: string } | null {
  if (!data || typeof data !== "object") return null;
  const message = data as { type?: unknown; id?: unknown; href?: unknown };
  if (message.type !== "zt-report-link") return null;
  return {
    id: typeof message.id === "string" ? message.id : "",
    href: typeof message.href === "string" ? message.href : "",
  };
}

/** True inside the Vibe-Trading desktop shell (Electron preload bridge). */
export function isDesktopShell(): boolean {
  return typeof window !== "undefined" && Boolean(window.vibeDesktop?.isDesktop);
}

function useZt(): Translate {
  const { t } = useTranslation();
  return useCallback<Translate>(
    (key, defaultValue, vars) => t(`zt.${key}` as never, { defaultValue, ...vars }) as unknown as string,
    [t],
  );
}

function classify(error: unknown, tz: Translate): LoadError {
  if (error instanceof ApiError) {
    // 404, or HTML where JSON was expected (the SPA answered): routes absent.
    if (error.status === 404 || error.status === 200) {
      return {
        kind: "routes",
        message: tz(
          "routesMissing",
          "The ZT routes are not registered on this Vibe-Trading server. Start it with extensions/zt_dashboards/launch_api.py (same flags as vibe-trading serve).",
        ),
      };
    }
    if (isAuthRequiredError(error)) return { kind: "auth", message: error.message };
    if (error.status === 503) return { kind: "config", message: error.message };
    return { kind: "other", message: error.message };
  }
  return { kind: "other", message: error instanceof Error ? error.message : String(error) };
}

export function ZtDashboards() {
  const tz = useZt();
  const [searchParams, setSearchParams] = useSearchParams();
  const [reports, setReports] = useState<ZtEnvelope<ZtReportsData> | null>(null);
  const [reportsError, setReportsError] = useState<LoadError | null>(null);
  const [snapshotDate, setSnapshotDate] = useState("today");
  const [snapshot, setSnapshot] = useState<ZtEnvelope<ZtSnapshotData> | null>(null);
  const [snapshotError, setSnapshotError] = useState<LoadError | null>(null);
  const [preflight, setPreflight] = useState<ZtPreflight | null>(null);
  const [preflightError, setPreflightError] = useState<LoadError | null>(null);
  const [loading, setLoading] = useState(true);
  const [viewer, setViewer] = useState<ViewerState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const viewerRef = useRef<HTMLElement>(null);
  const openSeq = useRef(0);
  const deepLinkHandled = useRef(false);

  const loadReports = useCallback(async () => {
    try {
      setReports(await api.listZtReports());
      setReportsError(null);
    } catch (error) {
      setReports(null);
      setReportsError(classify(error, tz));
    }
  }, [tz]);

  const loadSnapshot = useCallback(
    async (date: string) => {
      try {
        setSnapshot(await api.getZtSnapshot(date));
        setSnapshotError(null);
      } catch (error) {
        setSnapshot(null);
        setSnapshotError(classify(error, tz));
      }
    },
    [tz],
  );

  const loadPreflight = useCallback(async () => {
    try {
      setPreflight(await fetchZtPreflight());
      setPreflightError(null);
    } catch (error) {
      setPreflight(null);
      setPreflightError(classify(error, tz));
    }
  }, [tz]);

  const refresh = useCallback(async () => {
    setLoading(true);
    await Promise.all([loadPreflight(), loadReports(), loadSnapshot(snapshotDate)]);
    setLoading(false);
  }, [loadPreflight, loadReports, loadSnapshot, snapshotDate]);

  useEffect(() => {
    // Load once on mount; later loads are explicit (refresh or date change).
    void refresh();
  }, []);

  const viewable = useMemo(() => {
    const map = new Map<string, ZtReportItem>();
    for (const item of reports?.data.reports ?? []) if (item.viewable) map.set(item.id, item);
    return map;
  }, [reports]);

  const openReport = useCallback(
    async (item: ZtReportItem) => {
      const seq = ++openSeq.current;
      setNotice(null);
      setViewer({ item, src: null, error: null });
      setSearchParams({ report: item.id }, { replace: true });
      try {
        const src = await api.ztReportUrl(item.id);
        if (seq === openSeq.current) setViewer({ item, src, error: null });
      } catch (error) {
        if (seq === openSeq.current) {
          setViewer({ item, src: null, error: error instanceof Error ? error.message : String(error) });
        }
      }
      viewerRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    },
    [setSearchParams],
  );

  const closeReport = useCallback(() => {
    openSeq.current += 1;
    setViewer(null);
    setNotice(null);
    setSearchParams({}, { replace: true });
  }, [setSearchParams]);

  const openInNewTab = useCallback(
    async (item: ZtReportItem) => {
      if (isDesktopShell()) {
        // The desktop shell refuses about:blank windows and hands http(s) URLs to
        // the system browser, which has no key: always send a fresh ticket.
        try {
          const url = new URL(await withFreshTicket(ztReportPath(item.id)), window.location.href);
          window.open(url.href, "_blank", "noopener,noreferrer");
        } catch (error) {
          setNotice(error instanceof Error ? error.message : String(error));
        }
        return;
      }
      // Open synchronously (keeps the click's user activation), then navigate
      // once a fresh single-use ticket is minted.
      const win = window.open("about:blank", "_blank");
      if (!win) return;
      win.opener = null;
      try {
        win.location.href = await api.ztReportUrl(item.id);
      } catch (error) {
        win.close();
        setNotice(error instanceof Error ? error.message : String(error));
      }
    },
    [],
  );

  // Deep link: /zt?report=<id> opens that report once the list has loaded.
  useEffect(() => {
    if (deepLinkHandled.current || !reports) return;
    deepLinkHandled.current = true;
    const wanted = searchParams.get("report");
    const item = wanted ? viewable.get(wanted) : undefined;
    if (item) void openReport(item);
  }, [reports, searchParams, viewable, openReport]);

  // Links clicked inside a report arrive from the viewer shim as messages; only
  // whitelisted ids are opened, each with a freshly minted ticket.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const link = parseReportLinkMessage(event.data);
      if (!link) return;
      const target = link.id ? viewable.get(link.id) : undefined;
      if (target) {
        void openReport(target);
      } else {
        setNotice(
          tz("linkBlocked", "Not opened: {{href}} is not a whitelisted ZT report.", {
            href: link.href || link.id,
          }),
        );
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [viewable, openReport, tz]);

  const onDateChange = (value: string) => {
    setSnapshotDate(value);
    void loadSnapshot(value);
  };

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <section className="flex flex-col gap-4 border-b pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3">
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <LayoutDashboard className="h-3.5 w-3.5" aria-hidden="true" />
              {tz("badge", "ZT add-on · read-only")}
            </div>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">
                {tz("title", "ZT research dashboards")}
              </h1>
              <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
                {tz(
                  "subtitle",
                  "Daily monitoring snapshot and project dashboards from Investment-AI-Drive-Research, with provenance and honest freshness. Monitoring context only: not an investment signal, and nothing here places orders.",
                )}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {tz("refresh", "Refresh")}
          </button>
        </section>

        <PreflightPanel tz={tz} report={preflight} error={preflightError} loading={loading && !preflight} />

        {viewer ? (
          <section ref={viewerRef} aria-label={tz("viewer", "Report viewer")} className="space-y-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <h2 className="truncate text-lg font-semibold">{viewer.item.title}</h2>
                <p className="truncate font-mono text-xs text-muted-foreground">
                  {viewer.item.path}
                  {viewer.item.sha256 ? ` · sha256 ${viewer.item.sha256.slice(0, 12)}` : ""}
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                <button
                  type="button"
                  onClick={() => void openInNewTab(viewer.item)}
                  className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
                >
                  <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                  {isDesktopShell() ? tz("openInBrowser", "Open in browser") : tz("newTab", "Open in new tab")}
                </button>
                <button
                  type="button"
                  onClick={closeReport}
                  className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                  {tz("close", "Close")}
                </button>
              </div>
            </div>
            {notice ? (
              <p role="status" className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
                {notice}
              </p>
            ) : null}
            {viewer.error ? (
              <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm">
                {viewer.error}
              </p>
            ) : viewer.src ? (
              <iframe
                ref={iframeRef}
                key={viewer.src}
                src={viewer.src}
                title={tz("viewerTitle", "Report: {{title}}", { title: viewer.item.title })}
                sandbox={ZT_REPORT_SANDBOX}
                referrerPolicy="no-referrer"
                className="h-[75vh] w-full rounded-md border bg-white"
              />
            ) : (
              <div className="flex h-40 items-center justify-center rounded-md border text-sm text-muted-foreground">
                <Loader2 className="me-2 h-4 w-4 animate-spin" />
                {tz("loadingReport", "Loading report…")}
              </div>
            )}
          </section>
        ) : null}

        <SnapshotSection
          tz={tz}
          envelope={snapshot}
          error={snapshotError}
          date={snapshotDate}
          onDateChange={onDateChange}
        />

        <ReportsSection
          tz={tz}
          envelope={reports}
          error={reportsError}
          activeId={viewer?.item.id ?? null}
          onOpen={(item) => void openReport(item)}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pre-flight panel
// ---------------------------------------------------------------------------

function PreflightPanel({
  tz,
  report,
  error,
  loading,
}: {
  tz: Translate;
  report: ZtPreflight | null;
  error: LoadError | null;
  loading: boolean;
}) {
  const [showOk, setShowOk] = useState(true);
  const checks = useMemo(() => problemsFirst(report?.checks ?? []), [report]);
  const visible = showOk ? checks : checks.filter((check) => check.status !== "OK");
  return (
    <section aria-labelledby="zt-preflight" data-testid="zt-preflight" className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h2 id="zt-preflight" className="flex items-center gap-2 text-lg font-semibold">
            <ClipboardCheck className="h-4 w-4 shrink-0" aria-hidden="true" />
            {tz("preflightTitle", "Pre-flight")}
            {report ? <CheckPill status={report.overall} /> : null}
          </h2>
          <p className="text-sm text-muted-foreground">
            {tz(
              "preflightSubtitle",
              "Is this server ready for a real test? LLM, PIT receipts and prices, scheduler, kill switch, order approval, MCP servers, playbooks, disk and version.",
            )}
          </p>
        </div>
        {report ? (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-mono" data-testid="zt-preflight-counts">
              {tz("preflightCounts", "{{ok}} OK · {{warn}} WARN · {{fail}} FAIL", {
                ok: report.counts.OK ?? 0,
                warn: report.counts.WARN ?? 0,
                fail: report.counts.FAIL ?? 0,
              })}
            </span>
            <span className="font-mono">{report.generated_at?.slice(0, 19).replace("T", " ")} UTC</span>
            <button
              type="button"
              onClick={() => setShowOk((value) => !value)}
              className="rounded-md border px-2 py-1 font-medium transition hover:bg-muted"
            >
              {showOk ? tz("hideOk", "Hide OK checks") : tz("showOk", "Show all checks")}
            </button>
          </div>
        ) : null}
      </div>

      {error ? <ErrorPanel error={error} tz={tz} /> : null}
      {!report && !error && loading ? (
        <div className="flex h-16 items-center justify-center rounded-md border text-sm text-muted-foreground">
          <Loader2 className="me-2 h-4 w-4 animate-spin" />
          {tz("preflightLoading", "Running checks…")}
        </div>
      ) : null}

      {report ? (
        <ul className="grid gap-2 lg:grid-cols-2" aria-label={tz("preflightList", "Pre-flight checks")}>
          {visible.map((check) => (
            <PreflightRow key={check.id} check={check} tz={tz} />
          ))}
          {visible.length === 0 ? (
            <li className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
              {tz("allOk", "Every check is OK.")}
            </li>
          ) : null}
        </ul>
      ) : null}
    </section>
  );
}

function PreflightRow({ check, tz }: { check: ZtPreflightCheck; tz: Translate }) {
  const details = Object.entries(check.detail ?? {});
  return (
    <li
      data-testid="zt-preflight-check"
      data-check-id={check.id}
      data-status={check.status}
      className={cn(
        "min-w-0 space-y-1.5 rounded-md border p-3 text-sm",
        check.status === "FAIL" && "border-danger/40 bg-danger/5",
        check.status === "WARN" && "border-warning/40 bg-warning/5",
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <CheckPill status={check.status} />
        <span className="font-medium">{check.label}</span>
      </div>
      <p className="break-words text-muted-foreground">{check.summary}</p>
      {check.fix && check.status !== "OK" ? (
        <p className="break-words text-xs">
          <span className="font-medium">{tz("howToFix", "How to fix:")}</span> {check.fix}
        </p>
      ) : null}
      {details.length ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">{tz("details", "Details")}</summary>
          <dl className="mt-1 grid grid-cols-1 gap-x-3 gap-y-0.5 sm:grid-cols-[auto_1fr]">
            {details.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="font-mono text-muted-foreground">{key}</dt>
                <dd className="break-all font-mono">{formatDetailValue(value)}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </li>
  );
}

function CheckPill({ status }: { status: ZtCheckStatus }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 rounded px-2 py-0.5 font-mono text-xs font-medium",
        status === "OK" && "bg-success/10 text-success",
        status === "WARN" && "bg-warning/10 text-warning",
        status === "FAIL" && "bg-danger/10 text-danger",
      )}
    >
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Snapshot cards
// ---------------------------------------------------------------------------

function SnapshotSection({
  tz,
  envelope,
  error,
  date,
  onDateChange,
}: {
  tz: Translate;
  envelope: ZtEnvelope<ZtSnapshotData> | null;
  error: LoadError | null;
  date: string;
  onDateChange: (value: string) => void;
}) {
  const data = envelope?.data;
  const dates = [...(data?.available_dates ?? [])].reverse();
  const health = data?.source_health;
  const alerts = data?.alerts;
  const readiness = data?.signal_readiness;
  const guardrails = data?.event_guardrails;
  const portfolio = data?.portfolio_context;
  const context = [...(data?.market_context?.rows ?? []), ...(data?.macro_context?.rows ?? [])];
  const daily = envelope?.supplementary?.daily_update;
  const dailyData = (daily?.data ?? null) as { status?: string; message?: string } | null;

  return (
    <section aria-labelledby="zt-snapshot" className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="zt-snapshot" className="text-lg font-semibold">
            {tz("snapshotTitle", "Daily snapshot")}
          </h2>
          <p className="text-sm text-muted-foreground">
            {tz("snapshotSubtitle", "Alpha Monitor export: source health, alerts, guardrails and readiness.")}
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">{tz("snapshotDate", "Export")}</span>
          <select
            value={date}
            onChange={(event) => onDateChange(event.target.value)}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            <option value="today">{tz("today", "Today")}</option>
            <option value="latest">{tz("latest", "Latest available")}</option>
            {dates.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? <ErrorPanel error={error} tz={tz} /> : null}

      {envelope && envelope.status === "MISSING" ? (
        <div className="rounded-md border border-dashed p-4 text-sm">
          <p>
            {tz("snapshotMissing", "No export folder for {{date}}.", {
              date: data?.resolved_date ?? data?.requested ?? date,
            })}{" "}
            {data?.latest_available
              ? tz("latestIs", "The latest available export is {{date}}.", { date: data.latest_available })
              : tz("noneAvailable", "No export is available.")}
          </p>
          {data?.latest_available ? (
            <button
              type="button"
              onClick={() => onDateChange("latest")}
              className="mt-3 inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
            >
              {tz("showLatest", "Show latest ({{date}})", { date: data.latest_available })}
            </button>
          ) : null}
        </div>
      ) : null}

      {envelope && envelope.status !== "MISSING" ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <Card title={tz("freshness", "Freshness")}>
            <div className="flex items-center gap-2">
              <StatusPill status={envelope.status} />
              <span className="font-mono text-xs text-muted-foreground">{data?.resolved_date}</span>
            </div>
            <Provenance envelope={envelope} tz={tz} />
            {data?.complete === false ? (
              <p className="text-xs text-warning">
                {tz("incomplete", "Incomplete export, missing: {{files}}", {
                  files: (data.missing_files ?? []).join(", "),
                })}
              </p>
            ) : null}
            {dailyData?.status ? (
              <p className="text-xs text-muted-foreground">
                {tz("dailyRun", "Daily update run: {{status}}", { status: dailyData.status })}
                {dailyData.message ? ` · ${dailyData.message}` : ""}
              </p>
            ) : null}
          </Card>

          <Card title={tz("sourceHealth", "Source health")}>
            <Counts counts={health?.by_status} />
            <ul className="space-y-1 text-xs">
              {(health?.rows ?? [])
                .filter((row) => row.status !== "FRESH")
                .map((row) => (
                  <li key={row.source_id} className="min-w-0">
                    <span className="block truncate">{row.label}</span>
                    <span className="block truncate font-mono text-muted-foreground">
                      {row.status} · {row.age_label}
                    </span>
                  </li>
                ))}
            </ul>
          </Card>

          <Card title={tz("alerts", "Alerts")}>
            <Counts counts={alerts?.by_severity} />
            <ul className="space-y-1 text-xs">
              {(alerts?.rows ?? []).slice(0, 6).map((row) => (
                <li key={row.alert_id} className="flex gap-2">
                  <span
                    className={cn(
                      "shrink-0 font-mono",
                      row.severity === "ERROR" ? "text-danger" : "text-warning",
                    )}
                  >
                    {row.severity}
                  </span>
                  <span className="min-w-0">{row.title}</span>
                </li>
              ))}
            </ul>
          </Card>

          <Card title={tz("readiness", "Signal readiness")}>
            <Counts counts={readiness?.by_readiness} />
            <p className="text-xs text-muted-foreground">
              {tz("alphaReady", "Alpha-ready signals: {{count}}", {
                count: readiness?.alpha_ready_count ?? 0,
              })}
            </p>
          </Card>

          <Card title={tz("guardrails", "Event guardrails")}>
            <ul className="space-y-1 text-xs">
              {(guardrails?.rows ?? []).map((row) => (
                <li key={row.category} className="flex justify-between gap-2">
                  <span className="truncate">{row.category}</span>
                  <span className="shrink-0 font-mono text-muted-foreground">{row.status}</span>
                </li>
              ))}
            </ul>
          </Card>

          <Card title={tz("portfolio", "Portfolio import")}>
            {portfolio && !portfolio.error ? (
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="text-muted-foreground">{tz("reconciliation", "Reconciliation")}</dt>
                <dd className="font-mono">{portfolio.reconciliation_status ?? "—"}</dd>
                <dt className="text-muted-foreground">{tz("pnlStatus", "P&L status")}</dt>
                <dd className="font-mono">{portfolio.pnl_status ?? "—"}</dd>
                <dt className="text-muted-foreground">{tz("coverage", "Value coverage")}</dt>
                <dd className="font-mono">
                  {portfolio.pnl_value_coverage_pct == null ? "—" : `${portfolio.pnl_value_coverage_pct}%`}
                </dd>
                <dt className="text-muted-foreground">{tz("capturedAsOf", "Captured")}</dt>
                <dd className="font-mono">{portfolio.as_of ?? "—"}</dd>
              </dl>
            ) : (
              <p className="text-xs text-muted-foreground">{portfolio?.error ?? tz("noData", "no data retrieved")}</p>
            )}
            <p className="text-[11px] text-muted-foreground">
              {tz("portfolioPolicy", "Coverage and status only; account numbers, quantities and values are withheld.")}
            </p>
          </Card>

          <Card title={tz("context", "Market and macro context")} className="md:col-span-2 xl:col-span-3">
            {context.length ? (
              <div className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
                {context.map((row) => (
                  <div key={`${row.symbol ?? row.series}`} className="flex justify-between gap-2">
                    <span className="font-mono">{row.symbol ?? row.series}</span>
                    <span className="font-mono text-muted-foreground">
                      {row.close ?? row.value ?? "—"} · {row.as_of} · {row.price_status ?? row.status}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">{tz("noData", "no data retrieved")}</p>
            )}
          </Card>
        </div>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Report list
// ---------------------------------------------------------------------------

function ReportsSection({
  tz,
  envelope,
  error,
  activeId,
  onOpen,
}: {
  tz: Translate;
  envelope: ZtEnvelope<ZtReportsData> | null;
  error: LoadError | null;
  activeId: string | null;
  onOpen: (item: ZtReportItem) => void;
}) {
  const reports = envelope?.data.reports ?? [];
  return (
    <section aria-labelledby="zt-reports" className="space-y-3">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="zt-reports" className="text-lg font-semibold">
            {tz("reportsTitle", "Project dashboards")}
          </h2>
          <p className="text-sm text-muted-foreground">
            {tz(
              "reportsSubtitle",
              "PROJECT_HUB.json entries and root dashboards. Reports open sandboxed, with no network access.",
            )}
          </p>
        </div>
        {envelope ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <StatusPill status={envelope.status} />
            {tz("hubAsOf", "hub as of {{date}}", { date: envelope.as_of?.slice(0, 10) ?? "—" })}
          </div>
        ) : null}
      </div>

      {error ? <ErrorPanel error={error} tz={tz} /> : null}

      {envelope && reports.length === 0 ? (
        <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
          {tz("noReports", "No dashboards found.")}
        </p>
      ) : null}

      <ul className="grid gap-2">
        {reports.map((item) => (
          <li
            key={item.id}
            className={cn(
              "flex flex-col gap-2 rounded-md border p-3 sm:flex-row sm:items-center sm:justify-between",
              item.id === activeId && "border-primary/60 bg-primary/5",
              !item.viewable && "opacity-70",
            )}
          >
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="truncate text-sm font-medium">{item.title}</span>
                {item.role ? <Tag>{item.role}</Tag> : null}
                {!item.exists ? <Tag tone="danger">{tz("missing", "missing")}</Tag> : null}
              </div>
              <p className="truncate font-mono text-xs text-muted-foreground">
                {item.path}
                {item.mtime_utc ? ` · ${item.mtime_utc.slice(0, 10)}` : ""}
                {item.bytes != null ? ` · ${formatBytes(item.bytes)}` : ""}
              </p>
            </div>
            <button
              type="button"
              disabled={!item.viewable}
              onClick={() => onOpen(item)}
              aria-label={tz("openReport", "Open {{title}}", { title: item.title })}
              className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground"
            >
              {tz("open", "Open")}
            </button>
          </li>
        ))}
      </ul>

      {envelope ? (
        <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
          {envelope.data.viewer_policy}
        </p>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------

function Card({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <article className={cn("space-y-2 rounded-md border p-4", className)}>
      <h3 className="text-sm font-medium">{title}</h3>
      {children}
    </article>
  );
}

function StatusPill({ status }: { status: ZtStatus }) {
  return (
    <span
      className={cn(
        "inline-flex rounded px-2 py-0.5 font-mono text-xs font-medium",
        status === "FRESH" && "bg-success/10 text-success",
        status === "STALE" && "bg-warning/10 text-warning",
        status === "MISSING" && "bg-danger/10 text-danger",
      )}
    >
      {status}
    </span>
  );
}

function Tag({ children, tone }: { children: ReactNode; tone?: "danger" }) {
  return (
    <span
      className={cn(
        "rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground",
        tone === "danger" && "border-danger/40 text-danger",
      )}
    >
      {children}
    </span>
  );
}

function Counts({ counts }: { counts?: Record<string, number> }) {
  const entries = Object.entries(counts ?? {});
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([key, value]) => (
        <span key={key} className="rounded border px-2 py-0.5 font-mono text-xs">
          {key} {value}
        </span>
      ))}
    </div>
  );
}

function Provenance<T>({ envelope, tz }: { envelope: ZtEnvelope<T>; tz: Translate }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
      <dt className="text-muted-foreground">{tz("asOf", "as of")}</dt>
      <dd className="font-mono">{envelope.as_of ?? "—"}</dd>
      <dt className="text-muted-foreground">{tz("source", "source")}</dt>
      <dd className="truncate font-mono">{envelope.source_path}</dd>
      <dt className="text-muted-foreground">sha256</dt>
      <dd className="font-mono">{envelope.sha256 ? envelope.sha256.slice(0, 12) : "—"}</dd>
      <dt className="text-muted-foreground">PIT</dt>
      <dd className="font-mono">{envelope.pit_label}</dd>
    </dl>
  );
}

function ErrorPanel({ error, tz }: { error: LoadError; tz: Translate }) {
  return (
    <div role="alert" className="flex gap-2 rounded-md border border-danger/40 bg-danger/5 p-3 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
      <div>
        <p>{error.message}</p>
        {error.kind === "auth" ? (
          <p className="mt-1 text-xs text-muted-foreground">
            {tz("authHint", "Paste the API key in Settings, then refresh.")}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
