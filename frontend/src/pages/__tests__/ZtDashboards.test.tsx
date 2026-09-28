import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { ApiError, ztReportPath } from "@/lib/api";
import { ZT_REPORT_SANDBOX, ZtDashboards, parseReportLinkMessage } from "../ZtDashboards";

const apiMock = vi.hoisted(() => ({
  listZtReports: vi.fn(),
  getZtSnapshot: vi.fn(),
  ztReportUrl: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: apiMock };
});

function envelope<T>(tool: string, data: T, status: "FRESH" | "STALE" | "MISSING" = "FRESH") {
  return {
    tool,
    as_of: status === "MISSING" ? null : "2026-09-27T12:57:26Z",
    as_of_basis: "content:event_guardrails.as_of (instant)",
    retrieved_at: "2026-09-28T13:00:00Z",
    source_path: tool === "daily_snapshot" ? "implementation/exports/2026-09-27" : "PROJECT_HUB.json",
    sha256: status === "MISSING" ? null : "a".repeat(64),
    pit_label: "NON_PIT",
    status,
    status_rule: "FRESH when as_of is within 36h of retrieved_at",
    age_hours: 24,
    authority: "READ_ONLY_MONITORING_CONTEXT_NOT_AN_INVESTMENT_SIGNAL",
    data,
  };
}

const report = (id: string, title: string, exists = true) => ({
  id,
  title,
  path: id,
  origin: "hub+root",
  section: "artifacts",
  role: "key_dashboard",
  state: "current",
  hub_as_of: "2026-08-10",
  note: null,
  exists,
  viewable: exists,
  bytes: exists ? 39682 : null,
  mtime_utc: exists ? "2026-09-27T23:25:27Z" : null,
  sha256: exists ? "b".repeat(64) : null,
});

const REPORTS = envelope(
  "project_reports",
  {
    reports: [
      report("ALPHA_MONITOR_TOP25.html", "Alpha Monitor Top 25"),
      report("ASM_PHASE2_LANE_OPS.html", "Phase 2 lane ops"),
      report("CATALYST_DASHBOARD.html", "Catalyst Dashboard", false),
    ],
    counts: { reports: 3, viewable: 2, missing: 1, hub_items: 3 },
    hub_status: { label: "Gap-fill registered", asOf: "2026-08-15" },
    viewer_policy: "Only these ids are served, sandboxed.",
  },
  "STALE",
);

const SNAPSHOT = envelope("daily_snapshot", {
  requested: "today",
  resolved_date: "2026-09-27",
  latest_available: "2026-09-27",
  available_dates: ["2026-09-26", "2026-09-27"],
  complete: true,
  missing_files: [],
  alerts: {
    count: 2,
    by_severity: { ERROR: 1, WARNING: 1 },
    rows: [
      { alert_id: "A1", category: "PORTFOLIO_IMPORT", severity: "ERROR", title: "Rows do not reconcile", summary: "", evidence_id: "E1", as_of: "2026-07-19" },
      { alert_id: "A2", category: "SOURCE_HEALTH", severity: "WARNING", title: "Broker position captures is stale", summary: "", evidence_id: "E2", as_of: "2026-07-19" },
    ],
  },
  source_health: {
    count: 2,
    by_status: { FRESH: 1, STALE: 1 },
    rows: [
      { source_id: "prices_eod", label: "End-of-day market prices", status: "FRESH", observation_date: "2026-09-27", age_label: "0.0 hours", cadence: "daily" },
      { source_id: "broker_positions", label: "Broker position captures", status: "STALE", observation_date: "2026-07-19", age_label: "69.4 days", cadence: "daily" },
    ],
  },
  market_context: { count: 1, rows: [{ symbol: "QQQ", close: 744.5, as_of: "2026-09-25", price_status: "FRESH" }] },
  macro_context: { count: 1, rows: [{ series: "DGS10", value: 5.184, as_of: "2026-09-25", status: "PARTIAL" }] },
  event_guardrails: { count: 1, rows: [{ category: "EARNINGS", status: "UNAVAILABLE", source: "none", as_of: "", note: "" }] },
  signal_readiness: { count: 20, rows: [], by_readiness: { SEED_ONLY: 11, CONTEXT_ONLY: 7, BLOCKED: 2 }, alpha_ready_count: 0 },
  portfolio_context: {
    as_of: "2026-07-19",
    reconciliation_status: "UNRECONCILED",
    pnl_status: "PARTIAL",
    pnl_value_coverage_pct: 69.03,
    policy: "coverage and status only",
  },
  broker_connectivity: { status: "IBKR_NOT_CONNECTED", order_capability: "DISABLED" },
});

function renderPage(initial = "/zt") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ZtDashboards />
    </MemoryRouter>,
  );
}

describe("ZT dashboards page", () => {
  beforeEach(() => {
    apiMock.listZtReports.mockReset().mockResolvedValue(REPORTS);
    apiMock.getZtSnapshot.mockReset().mockResolvedValue(SNAPSHOT);
    apiMock.ztReportUrl
      .mockReset()
      .mockImplementation(async (id: string) => `${ztReportPath(id)}?ticket=t-${id}`);
  });

  it("shows today's snapshot cards with provenance and the report list", async () => {
    renderPage();
    expect(await screen.findByText("ZT research dashboards")).toBeInTheDocument();
    expect(apiMock.getZtSnapshot).toHaveBeenCalledWith("today");
    expect(await screen.findByText("Broker position captures")).toBeInTheDocument();
    expect(screen.getByText("implementation/exports/2026-09-27")).toBeInTheDocument();
    expect(screen.getByText("NON_PIT")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("Rows do not reconcile")).toBeInTheDocument();
    expect(screen.getByText("UNRECONCILED")).toBeInTheDocument();
    expect(screen.getByText("69.03%")).toBeInTheDocument();
    expect(screen.getByText("Alpha-ready signals: 0")).toBeInTheDocument();
    expect(screen.getByText("Alpha Monitor Top 25")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Catalyst Dashboard" })).toBeDisabled();
    expect(screen.getByText("missing")).toBeInTheDocument();
  });

  it("reports a missing export honestly and offers the latest one", async () => {
    apiMock.getZtSnapshot.mockImplementation(async (date: string) =>
      date === "today"
        ? envelope(
            "daily_snapshot",
            { requested: "today", resolved_date: "2026-09-28", latest_available: "2026-09-27", available_dates: ["2026-09-27"] },
            "MISSING",
          )
        : SNAPSHOT,
    );
    renderPage();
    expect(await screen.findByText(/No export folder for 2026-09-28\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show latest (2026-09-27)" }));
    await waitFor(() => expect(apiMock.getZtSnapshot).toHaveBeenLastCalledWith("latest"));
    expect(await screen.findByText("UNRECONCILED")).toBeInTheDocument();
  });

  it("opens a report in a sandboxed frame with a freshly minted ticket URL", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Open Alpha Monitor Top 25" }));
    const frame = await screen.findByTitle("Report: Alpha Monitor Top 25");
    expect(apiMock.ztReportUrl).toHaveBeenCalledWith("ALPHA_MONITOR_TOP25.html");
    expect(frame).toHaveAttribute("src", "/zt/reports/ALPHA_MONITOR_TOP25.html?ticket=t-ALPHA_MONITOR_TOP25.html");
    expect(frame).toHaveAttribute("sandbox", ZT_REPORT_SANDBOX);
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByTitle("Report: Alpha Monitor Top 25")).not.toBeInTheDocument();
  });

  it("follows links from the report only to whitelisted reports", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Open Alpha Monitor Top 25" }));
    const frame = (await screen.findByTitle("Report: Alpha Monitor Top 25")) as HTMLIFrameElement;

    // A message from any other window is ignored.
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "zt-report-link", id: "ASM_PHASE2_LANE_OPS.html" } }),
      );
    });
    expect(apiMock.ztReportUrl).toHaveBeenCalledTimes(1);

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "zt-report-link", id: "ASM_PHASE2_LANE_OPS.html", href: "ASM_PHASE2_LANE_OPS.html" },
          source: frame.contentWindow,
        }),
      );
    });
    expect(await screen.findByTitle("Report: Phase 2 lane ops")).toBeInTheDocument();
    expect(apiMock.ztReportUrl).toHaveBeenLastCalledWith("ASM_PHASE2_LANE_OPS.html");

    const next = screen.getByTitle("Report: Phase 2 lane ops") as HTMLIFrameElement;
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "zt-report-link", id: "", href: "implementation/config/source_registry.json" },
          source: next.contentWindow,
        }),
      );
    });
    const status = await screen.findByRole("status");
    expect(within(status).getByText(/source_registry\.json is not a whitelisted ZT report/)).toBeInTheDocument();
    expect(apiMock.ztReportUrl).toHaveBeenCalledTimes(2);
  });

  it("opens a deep-linked report once the list is loaded", async () => {
    renderPage("/zt?report=ASM_PHASE2_LANE_OPS.html");
    expect(await screen.findByTitle("Report: Phase 2 lane ops")).toBeInTheDocument();
  });

  it("explains when the ZT routes are not registered on the server", async () => {
    const spaFallback = new ApiError("Expected JSON from /zt/reports, got text/html", 200);
    apiMock.listZtReports.mockRejectedValue(spaFallback);
    apiMock.getZtSnapshot.mockRejectedValue(new ApiError("Not Found", 404));
    renderPage();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(2);
    expect(alerts[0]).toHaveTextContent("extensions/zt_dashboards/launch_api.py");
  });

  it("surfaces a server configuration error", async () => {
    apiMock.listZtReports.mockRejectedValue(
      new ApiError("ZT dashboards unavailable: INVESTMENT_AI_PROJECT_ROOT is required", 503),
    );
    renderPage();
    expect(await screen.findByText(/INVESTMENT_AI_PROJECT_ROOT is required/)).toBeInTheDocument();
  });
});

describe("ZT helpers", () => {
  it("parses only the shim's link messages", () => {
    expect(parseReportLinkMessage({ type: "zt-report-link", id: "A.html", href: "A.html" })).toEqual({
      id: "A.html",
      href: "A.html",
    });
    expect(parseReportLinkMessage({ type: "other", id: "A.html" })).toBeNull();
    expect(parseReportLinkMessage("zt-report-link")).toBeNull();
    expect(parseReportLinkMessage({ type: "zt-report-link", id: 7 })).toEqual({ id: "", href: "" });
  });

  it("encodes report ids segment by segment", () => {
    expect(ztReportPath("research/a b/Report #1.html")).toBe("/zt/reports/research/a%20b/Report%20%231.html");
    expect(ztReportPath("ALPHA_MONITOR_TOP25.html")).toBe("/zt/reports/ALPHA_MONITOR_TOP25.html");
  });
});
