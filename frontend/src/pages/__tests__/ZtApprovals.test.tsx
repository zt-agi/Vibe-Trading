import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { ZtApprovals } from "../ZtApprovals";
import { ZtApprovalError, type PaperAccount, type ProposalDetail, type ProposalList } from "@/lib/ztApprovalsApi";

const apiMock = vi.hoisted(() => ({
  listProposals: vi.fn(),
  getProposal: vi.fn(),
  approveProposal: vi.fn(),
  rejectProposal: vi.fn(),
  paperAccount: vi.fn(),
  resetPaper: vi.fn(),
}));

vi.mock("@/lib/ztApprovalsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ztApprovalsApi")>();
  return { ...actual, ztApprovalsApi: apiMock };
});

const ID = "op_0123456789abcdef0123456789abcdef";
const HASH = "sha256:" + "ab".repeat(26) + "c0ffee123456";

function isoIn(seconds: number): string {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

function summary(overrides: Partial<ProposalList["proposals"][number]> = {}) {
  return {
    id: ID,
    status: "PENDING" as const,
    created_utc: isoIn(-60),
    expires_utc: isoIn(14 * 60 + 30),
    expires_in_seconds: 870,
    broker: "zt-paper",
    environment: "simulated",
    account: "ZT-PAPER",
    orders: [{ symbol: "AAPL", side: "buy" as const, qty: 125, notional: null, order_type: "market" as const, limit_price: null, tif: "day" }],
    validation_ok: true,
    failed_checks: [],
    advisory_checks: [],
    origin: "mcp_propose",
    approvable: true,
    content_hash_tail: HASH.slice(-12),
    rationale: "PEAD drift after a beat",
    ...overrides,
  };
}

function list(rows = [summary()], mode: "required" | "off" = "required"): ProposalList {
  return {
    approval_mode: mode,
    ttl_minutes: 15,
    server_time_utc: new Date().toISOString(),
    policy: { max_position_pct: 0.25, source: "defaults", error: null },
    ledger: { ok: true, record_count: 7, first_break: null },
    proposals: rows,
  };
}

function detail(overrides: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    id: ID,
    status: "PENDING",
    created_utc: isoIn(-60),
    expires_utc: isoIn(14 * 60 + 30),
    ttl_minutes: 15,
    origin: { kind: "mcp_propose", tool: "propose_orders", actor: "agent" },
    broker: { key: "zt-paper", profile_id: "zt-paper", environment: "simulated", live: false, label: "SIMULATED" },
    account_scope: { account: "ZT-PAPER", symbols: ["AAPL"] },
    orders: [{ symbol: "AAPL", side: "buy", qty: 125, notional: null, order_type: "market", limit_price: null, tif: "day" }],
    decision_record: {
      rationale: "PEAD drift after a beat",
      evidence_ids: ["ev-8k-1"],
      signals: [{ source: "pead", symbol: "AAPL", direction: "long", rationale: "SUE +2.1", evidence_ids: ["ev-8k-1"], approach: "long_only" }],
      equity: 100000,
      equity_basis: "cash + positions at completed-session closes",
      current_weights: { NVDA: 0.05 },
      requested_targets: { AAPL: 0.3 },
      target_weights: { AAPL: 0.25 },
      projected_weights: { AAPL: 0.25, NVDA: 0.05 },
      clamp_events: [{ limit: "max_position_pct", symbol: "AAPL", before: 0.3, after: 0.25 }],
      direction_permissions: [{ symbol: "AAPL", current_qty: 0, projected_qty: 125, creates_or_increases_short: false, short_capable_sources: [], long_only_bearish_sources: [], status: "PASS" }],
      exposure_before: { gross: 0.05, net: 0.05, max_name: 0.05, max_name_symbol: "NVDA" },
      projected_exposure: { gross: 0.3, net: 0.3, max_name: 0.25, max_name_symbol: "AAPL" },
      orders_detail: [{ symbol: "AAPL", side: "buy", qty: 125, notional: null, order_type: "market", limit_price: null, tif: "day", reference_price: 200, reference_date: "2026-09-28", reference_source: "vt:yahoo", est_notional_usd: 25000 }],
    },
    validation: {
      ok: true,
      checked_utc: new Date().toISOString(),
      checks: [
        { name: "reference_prices", status: "PASS", detail: "AAPL=200@2026-09-28 (vt:yahoo)", blocking: true },
        { name: "direction_permission", status: "PASS", detail: "no order creates or increases a short", blocking: true },
        { name: "evidence_cited", status: "FAIL", detail: "no signal citing evidence for MSFT", blocking: false },
      ],
    },
    route: { kind: "zt_paper", target: "ZT-PAPER" },
    content_hash: HASH,
    content_hash_tail: HASH.slice(-12),
    expires_in_seconds: 870,
    approvable: true,
    transitions: [],
    approval: null,
    submission: null,
    integrity: { ok: true, reason: null },
    ledger_events: [{ seq: 3, at_utc: isoIn(-60), event: "created", from: null, to: "PENDING", actor: "agent", reason: "proposed", record_hash: "sha256:" + "9".repeat(64) }],
    ...overrides,
  };
}

const PAPER: PaperAccount = {
  account: "ZT-PAPER",
  label: "SIMULATED",
  currency: "USD",
  cash: 95000,
  equity: 100000,
  starting_cash: 100000,
  max_leverage: 1,
  gross_exposure: 5000,
  net_exposure: 5000,
  leverage: 0.05,
  realized_pnl: 0,
  positions: [{ symbol: "NVDA", qty: 50, avg_cost: 100, mark: 100, mark_date: "2026-09-28", mark_source: "vt:yahoo", market_value: 5000, unrealized_pnl: 0, realized_pnl: 0, weight: 0.05 }],
  unpriced: [],
  price_source: "vt",
  valuation: "latest completed-session close",
  updated_utc: new Date().toISOString(),
  fills_count: 1,
  recent_fills: [],
};

function renderPage(initial = `/zt/approvals?proposal=${ID}`) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ZtApprovals />
    </MemoryRouter>,
  );
}

describe("ZT order approvals page", () => {
  beforeEach(() => {
    apiMock.listProposals.mockReset().mockResolvedValue(list());
    apiMock.getProposal.mockReset().mockResolvedValue(detail());
    apiMock.approveProposal.mockReset();
    apiMock.rejectProposal.mockReset();
    apiMock.paperAccount.mockReset().mockResolvedValue(PAPER);
    apiMock.resetPaper.mockReset();
  });

  it("lists proposals with status chips, a live expiry countdown and the approval mode", async () => {
    renderPage("/zt/approvals");
    const row = await screen.findByRole("button", { name: `Open proposal ${ID}` });
    expect(within(row).getByTestId("status-chip")).toHaveTextContent("PENDING");
    expect(within(row).getByText(/expires in 14:[23]\d/)).toBeInTheDocument();
    expect(within(row).getByText("BUY 125 AAPL MKT")).toBeInTheDocument();
    expect(within(row).getByText("checks pass")).toBeInTheDocument();
    expect(screen.getByText(/Approval required for every order/)).toBeInTheDocument();
    expect(screen.getByText("Ledger intact (7 records)")).toBeInTheDocument();
    expect(screen.getByText("Select a proposal to review its orders, evidence and checks.")).toBeInTheDocument();
  });

  it("warns when approval is off and shows an empty list", async () => {
    apiMock.listProposals.mockResolvedValue(list([], "off"));
    renderPage("/zt/approvals");
    expect(await screen.findByText(/Approval is off: only zt-paper orders are held/)).toBeInTheDocument();
    expect(screen.getByText(/No proposals\./)).toBeInTheDocument();
  });

  it("opens the deep-linked proposal with its decision record, weights, checks and audit trail", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: ID })).toBeInTheDocument();
    expect(apiMock.getProposal).toHaveBeenCalledWith(ID);
    expect(screen.getAllByText("PEAD drift after a beat").length).toBeGreaterThan(0);
    expect(screen.getAllByText("ev-8k-1", { selector: "span" }).length).toBeGreaterThan(0);
    expect(screen.getByText("25,000")).toBeInTheDocument();
    expect(screen.getAllByText("2026-09-28", { selector: "span" }).length).toBeGreaterThan(0);
    expect(screen.getByText(/Clamped AAPL by max_position_pct: 30.00% → 25.00%/)).toBeInTheDocument();
    expect(screen.getByText(/out of scope, untouched/)).toBeInTheDocument();
    const checks = screen.getByRole("list", { name: "Validation checks" });
    expect(within(checks).getByText("reference_prices")).toBeInTheDocument();
    expect(within(checks).getByLabelText("FAIL (advisory)")).toBeInTheDocument();
    expect(screen.getByText("∅ → PENDING")).toBeInTheDocument();
    expect(screen.getByTestId("hash-tail")).toHaveTextContent(HASH.slice(-12));
  });

  it("approves only after the confirmation is ticked, sending the full content hash", async () => {
    const filled = detail({ status: "FILLED", submission: { submitted_utc: new Date().toISOString(), results: [{ symbol: "AAPL", side: "buy", qty: 125, notional: null, status: "filled", error: null, fill_price: 200 }] } });
    apiMock.approveProposal.mockResolvedValue(filled);
    renderPage();
    const approve = await screen.findByRole("button", { name: /Approve and submit once/ });
    expect(approve).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(approve).toBeEnabled();
    apiMock.getProposal.mockResolvedValue(filled);
    apiMock.listProposals.mockResolvedValue(list([summary({ status: "FILLED", approvable: false })]));
    fireEvent.click(approve);

    await waitFor(() => expect(apiMock.approveProposal).toHaveBeenCalledWith(ID, HASH));
    expect(await screen.findByText("filled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve and submit once/ })).not.toBeInTheDocument();
    expect(apiMock.paperAccount).toHaveBeenCalledTimes(2);
  });

  it("rejects only with a reason", async () => {
    apiMock.rejectProposal.mockResolvedValue(detail({ status: "REJECTED" }));
    renderPage();
    const reject = await screen.findByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason for rejecting"), { target: { value: "  thesis too thin  " } });
    fireEvent.click(reject);
    await waitFor(() => expect(apiMock.rejectProposal).toHaveBeenCalledWith(ID, "thesis too thin"));
  });

  it("explains an expired proposal (410) and a halted live broker (423)", async () => {
    apiMock.approveProposal.mockRejectedValueOnce(new ZtApprovalError("the proposal expired", 410, "expired"));
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /Approve and submit once/ }));
    expect(await screen.findByText(/expired before approval/)).toBeInTheDocument();

    apiMock.approveProposal.mockRejectedValueOnce(new ZtApprovalError("halted", 423, "halted"));
    fireEvent.click(screen.getByRole("button", { name: /Approve and submit once/ }));
    expect(await screen.findByText(/Live trading is halted/)).toBeInTheDocument();
  });

  it("shows the checks that fail when approval re-validates (422)", async () => {
    const revalidation = { ok: false, checked_utc: "", checks: [{ name: "orders_match_targets", status: "FAIL" as const, detail: "orders differ from the target diff", blocking: true }] };
    apiMock.approveProposal.mockRejectedValue(
      new ZtApprovalError("validation fails now: orders_match_targets", 422, "validation_failed", detail({ revalidation })),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /Approve and submit once/ }));
    expect(await screen.findByText("orders differ from the target diff")).toBeInTheDocument();
    expect(screen.getByText(/re-checked the proposal just now/)).toBeInTheDocument();
  });

  it("cannot approve once the window has closed", async () => {
    apiMock.getProposal.mockResolvedValue(detail({ expires_utc: isoIn(-5), expires_in_seconds: -5, approvable: false }));
    renderPage();
    expect(await screen.findByText("The approval window has closed.")).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: /Approve and submit once/ })).toBeDisabled();
  });

  it("warns before approving live-broker orders and on failed creation-time checks", async () => {
    apiMock.getProposal.mockResolvedValue(detail({
      broker: { key: "robinhood", profile_id: "robinhood-live-mcp", environment: "live", live: true },
      validation: { ok: false, checked_utc: "", checks: [{ name: "halt", status: "FAIL", detail: "live trading halted (HALT)", blocking: true }] },
    }));
    renderPage();
    expect(await screen.findByText(/LIVE broker: approving sends real orders/)).toBeInTheDocument();
    expect(screen.getByText(/Some checks failed when this was proposed/)).toBeInTheDocument();
  });

  it("shows the SIMULATED paper account and resets it only after RESET is typed", async () => {
    apiMock.resetPaper.mockResolvedValue({ account: { ...PAPER, cash: 50000, equity: 50000, positions: [] }, rejected_proposals: [ID] });
    renderPage("/zt/approvals");
    expect(await screen.findByText("SIMULATED")).toBeInTheDocument();
    expect(screen.getByText("95,000")).toBeInTheDocument();
    const paperTable = screen.getByRole("table");
    expect(within(paperTable).getByText("NVDA")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Reset paper account/ }));
    const confirm = screen.getByRole("button", { name: "Reset now" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Starting cash (USD)"), { target: { value: "50000" } });
    fireEvent.change(screen.getByLabelText("Type RESET to confirm"), { target: { value: "RESET" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(apiMock.resetPaper).toHaveBeenCalledWith(50000));
    expect(await screen.findByText(/1 pending paper proposal\(s\) rejected/)).toBeInTheDocument();
  });

  it("says when the approval routes are not registered", async () => {
    apiMock.listProposals.mockRejectedValue(new ZtApprovalError("Expected JSON", 404, "routes_missing"));
    renderPage("/zt/approvals");
    expect(await screen.findByText(/approval routes are not registered/)).toBeInTheDocument();
  });

  it("switches the list filter", async () => {
    renderPage("/zt/approvals");
    await screen.findByRole("button", { name: `Open proposal ${ID}` });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "FILLED" } });
    await waitFor(() => expect(apiMock.listProposals).toHaveBeenLastCalledWith("FILLED"));
  });

  it("refreshes the list on a timer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderPage("/zt/approvals");
      await screen.findByRole("button", { name: `Open proposal ${ID}` });
      const calls = apiMock.listProposals.mock.calls.length;
      await act(async () => {
        vi.advanceTimersByTime(15_000);
      });
      expect(apiMock.listProposals.mock.calls.length).toBeGreaterThan(calls);
    } finally {
      vi.useRealTimers();
    }
  });
});
