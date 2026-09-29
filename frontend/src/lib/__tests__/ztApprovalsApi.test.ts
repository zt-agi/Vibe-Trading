import { setApiAuthKey } from "@/lib/apiAuth";
import { isAuthRequiredError } from "@/lib/api";
import {
  ZtApprovalError,
  describeOrder,
  formatCountdown,
  formatPct,
  hashTail,
  secondsUntil,
  ztApprovalsApi,
} from "@/lib/ztApprovalsApi";

function jsonResponse(status: number, body: unknown, contentType = "application/json") {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "content-type": contentType },
  });
}

describe("ztApprovalsApi", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setApiAuthKey("");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setApiAuthKey("");
  });

  it("sends the approval with the confirm hash and VT's bearer key", async () => {
    setApiAuthKey("k-123");
    fetchMock.mockResolvedValue(jsonResponse(200, { id: "op_x", status: "FILLED" }));
    const out = await ztApprovalsApi.approveProposal("op_x", "sha256:abc");
    expect(out.status).toBe("FILLED");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/zt/orders/proposals/op_x/approve");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ confirm_hash: "sha256:abc" });
    expect(init.headers.Authorization).toBe("Bearer k-123");
  });

  it("builds the list, reject, paper and reset requests", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));
    await ztApprovalsApi.listProposals("PENDING");
    await ztApprovalsApi.listProposals("");
    await ztApprovalsApi.rejectProposal("op_y", "no");
    await ztApprovalsApi.paperAccount();
    await ztApprovalsApi.resetPaper(25000);
    await ztApprovalsApi.resetPaper();
    const calls = fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? "GET", init?.body ?? null]);
    expect(calls).toEqual([
      ["/zt/orders/proposals?status=PENDING", "GET", null],
      ["/zt/orders/proposals", "GET", null],
      ["/zt/orders/proposals/op_y/reject", "POST", JSON.stringify({ reason: "no" })],
      ["/zt/paper/account", "GET", null],
      ["/zt/paper/reset", "POST", JSON.stringify({ confirm: "RESET", starting_cash: 25000 })],
      ["/zt/paper/reset", "POST", JSON.stringify({ confirm: "RESET" })],
    ]);
  });

  it("turns structured errors into ZtApprovalError with code and proposal", async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, {
      detail: { code: "validation_failed", message: "validation fails now: gross_exposure", proposal: { id: "op_z", status: "PENDING" } },
    }));
    const error = await ztApprovalsApi.approveProposal("op_z", "h").catch((e) => e);
    expect(error).toBeInstanceOf(ZtApprovalError);
    expect([error.status, error.code, error.message]).toEqual([422, "validation_failed", "validation fails now: gross_exposure"]);
    expect(error.proposal.id).toBe("op_z");
  });

  it("maps 401 to VT's auth-required message", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Invalid or missing API key" }));
    const error = await ztApprovalsApi.paperAccount().catch((e) => e);
    expect(isAuthRequiredError(error)).toBe(true);
  });

  it("detects the SPA answering instead of the ZT routes", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, "<!doctype html>", "text/html"));
    const error = await ztApprovalsApi.listProposals().catch((e) => e);
    expect([error.status, error.code]).toEqual([404, "routes_missing"]);
  });
});

describe("approval helpers", () => {
  it("formats the countdown", () => {
    expect(formatCountdown(899)).toBe("14:59");
    expect(formatCountdown(3723)).toBe("1:02:03");
    expect(formatCountdown(0)).toBeNull();
    expect(formatCountdown(Number.NaN)).toBeNull();
    expect(secondsUntil(new Date(10_000).toISOString(), 4_000)).toBe(6);
  });

  it("shows hash tails, orders and percentages", () => {
    expect(hashTail("sha256:0123456789abcdef")).toBe("456789abcdef");
    expect(hashTail(undefined)).toBe("");
    expect(describeOrder({ symbol: "AAPL", side: "buy", qty: 10, notional: null, order_type: "limit", limit_price: 201.5, tif: "day" }))
      .toBe("BUY 10 AAPL @ 201.5 LMT");
    expect(describeOrder({ symbol: "MSFT", side: "sell", qty: null, notional: 1500, order_type: "market", limit_price: null, tif: "gtc" }))
      .toBe("SELL $1,500 MSFT MKT");
    expect(formatPct(0.2512, 2)).toBe("25.12%");
    expect(formatPct(null)).toBe("—");
  });
});
