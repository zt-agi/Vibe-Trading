// ZT add-on: human approval of every order (extensions/zt_approvals).
// Lists order proposals with an expiry countdown, shows one proposal's
// decision record and validation checks, and lets the human approve (after
// ticking a confirmation, showing the content-hash tail) or reject (with a
// reason). Also shows the SIMULATED zt-paper account. Labels use
// t(key, { defaultValue }) under "ztApprovals.", so no locale file changes.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Clock,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { ApiError, isAuthRequiredError } from "@/lib/api";
import {
  PROPOSAL_STATUSES,
  ZtApprovalError,
  describeOrder,
  formatCountdown,
  formatNumber,
  formatPct,
  hashTail,
  secondsUntil,
  ztApprovalsApi,
  type OrderDetail,
  type PaperAccount,
  type ProposalDetail,
  type ProposalList,
  type ProposalStatus,
  type ProposalSummary,
  type Validation,
} from "@/lib/ztApprovalsApi";
import { cn } from "@/lib/utils";

type Translate = (key: string, defaultValue: string, vars?: Record<string, string | number>) => string;

type LoadError = { kind: "routes" | "auth" | "other"; message: string };

/** How often the list and the paper account refresh while the page is open. */
export const ZT_APPROVALS_POLL_MS = 15_000;

function useZt(): Translate {
  const { t } = useTranslation();
  return useCallback<Translate>(
    (key, defaultValue, vars) =>
      t(`ztApprovals.${key}` as never, { defaultValue, ...vars }) as unknown as string,
    [t],
  );
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function classify(error: unknown, tz: Translate): LoadError {
  if (error instanceof ApiError) {
    if (error.status === 404 || error.code === "routes_missing") {
      return {
        kind: "routes",
        message: tz(
          "routesMissing",
          "The approval routes are not registered on this Vibe-Trading server. Start it with the ZT launcher (extensions/zt_dashboards/launch_api.py), which registers extensions/zt_approvals.",
        ),
      };
    }
    if (isAuthRequiredError(error)) return { kind: "auth", message: error.message };
    return { kind: "other", message: error.message };
  }
  return { kind: "other", message: error instanceof Error ? error.message : String(error) };
}

const STATUS_STYLE: Record<ProposalStatus, string> = {
  PENDING: "border-warning/50 bg-warning/10 text-warning",
  APPROVED: "border-primary/40 bg-primary/10 text-primary",
  SUBMITTED: "border-primary/40 bg-primary/10 text-primary",
  FILLED: "border-success/40 bg-success/10 text-success",
  FAILED: "border-danger/40 bg-danger/10 text-danger",
  REJECTED: "border-border bg-muted text-muted-foreground",
  EXPIRED: "border-border bg-muted text-muted-foreground",
};

function StatusChip({ status, tz }: { status: ProposalStatus; tz: Translate }) {
  return (
    <span
      data-testid="status-chip"
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
        STATUS_STYLE[status] ?? "border-border",
      )}
    >
      {tz(`status.${status}`, status)}
    </span>
  );
}

function Countdown({ expiresUtc, now, tz }: { expiresUtc: string; now: number; tz: Translate }) {
  const text = formatCountdown(secondsUntil(expiresUtc, now));
  return (
    <span className={cn("inline-flex items-center gap-1 font-mono text-xs", text ? "text-warning" : "text-muted-foreground")}>
      <Clock className="h-3 w-3" aria-hidden="true" />
      {text ? tz("expiresIn", "expires in {{time}}", { time: text }) : tz("expired", "expired")}
    </span>
  );
}

function Card({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section className={cn("min-w-0 rounded-md border bg-card p-4", className)}>
      <h3 className="mb-3 text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function shortId(id: string): string {
  return `${id.slice(0, 7)}…${id.slice(-6)}`;
}

export function ZtApprovals() {
  const tz = useZt();
  const [searchParams, setSearchParams] = useSearchParams();
  const [list, setList] = useState<ProposalList | null>(null);
  const [listError, setListError] = useState<LoadError | null>(null);
  const [statusFilter, setStatusFilter] = useState<ProposalStatus | "">("");
  const [selectedId, setSelectedId] = useState<string | null>(() => searchParams.get("proposal"));
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [paper, setPaper] = useState<PaperAccount | null>(null);
  const [paperError, setPaperError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const detailSeq = useRef(0);

  const loadList = useCallback(async () => {
    try {
      setList(await ztApprovalsApi.listProposals(statusFilter));
      setListError(null);
    } catch (error) {
      setList(null);
      setListError(classify(error, tz));
    }
  }, [statusFilter, tz]);

  const loadPaper = useCallback(async () => {
    try {
      setPaper(await ztApprovalsApi.paperAccount());
      setPaperError(null);
    } catch (error) {
      setPaperError(classify(error, tz).message);
    }
  }, [tz]);

  const loadDetail = useCallback(async (id: string) => {
    const seq = ++detailSeq.current;
    try {
      const data = await ztApprovalsApi.getProposal(id);
      if (seq === detailSeq.current) {
        setDetail(data);
        setDetailError(null);
      }
    } catch (error) {
      if (seq === detailSeq.current) {
        setDetail(null);
        setDetailError(error instanceof Error ? error.message : String(error));
      }
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    await Promise.all([loadList(), loadPaper(), selectedId ? loadDetail(selectedId) : Promise.resolve()]);
    setLoading(false);
  }, [loadList, loadPaper, loadDetail, selectedId]);

  useEffect(() => {
    void refresh();
    // refresh is rebuilt when the filter or selection changes; that is intended.
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadList();
      void loadPaper();
    }, ZT_APPROVALS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadList, loadPaper]);

  const select = useCallback(
    (id: string) => {
      setSelectedId(id);
      setDetail(null);
      setDetailError(null);
      setSearchParams({ proposal: id }, { replace: true });
    },
    [setSearchParams],
  );

  const afterAction = useCallback(
    async (updated: ProposalDetail | null) => {
      if (updated) setDetail((current) => ({ ...current, ...updated }) as ProposalDetail);
      await Promise.all([loadList(), loadPaper(), selectedId ? loadDetail(selectedId) : Promise.resolve()]);
    },
    [loadList, loadPaper, loadDetail, selectedId],
  );

  const hasPending = (list?.proposals ?? []).some((row) => row.status === "PENDING") || detail?.status === "PENDING";
  const now = useNow(hasPending);

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">
        <section className="flex flex-col gap-4 border-b pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3">
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              {tz("badge", "ZT add-on · a human approves every order")}
            </div>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">{tz("title", "Order approvals")}</h1>
              <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
                {tz(
                  "subtitle",
                  "Orders from the agent, the live runner and the zt-paper account wait here as proposals. Nothing reaches a broker until you approve it; each approval submits the reviewed orders exactly once.",
                )}
              </p>
            </div>
            {list ? <ModeLine list={list} tz={tz} /> : null}
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex min-h-[44px] min-w-[44px] items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {tz("refresh", "Refresh")}
          </button>
        </section>

        {listError ? (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm">
            {listError.message}
          </p>
        ) : null}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <section aria-labelledby="zt-approvals-list" className="min-w-0 space-y-3 lg:col-span-5">
            <div className="flex items-end justify-between gap-2">
              <h2 id="zt-approvals-list" className="text-lg font-semibold">
                {tz("proposals", "Proposals")}
              </h2>
              <label className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">{tz("filter", "Status")}</span>
                <select
                  aria-label={tz("filter", "Status")}
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value as ProposalStatus | "")}
                  className="min-h-[44px] min-w-[44px] rounded-md border bg-background px-2 py-1.5 text-sm"
                >
                  <option value="">{tz("all", "All")}</option>
                  {PROPOSAL_STATUSES.map((status) => (
                    <option key={status} value={status}>
                      {tz(`status.${status}`, status)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <ProposalListView
              rows={list?.proposals ?? []}
              loaded={list !== null}
              selectedId={selectedId}
              onSelect={select}
              now={now}
              tz={tz}
            />
          </section>

          <section aria-label={tz("detailLabel", "Proposal detail")} className="min-w-0 lg:col-span-7">
            {selectedId ? (
              detailError ? (
                <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm">
                  {detailError}
                </p>
              ) : detail ? (
                <ProposalDetailView key={detail.id} proposal={detail} now={now} tz={tz} onDone={afterAction} />
              ) : (
                <div className="flex h-40 items-center justify-center rounded-md border text-sm text-muted-foreground">
                  <Loader2 className="me-2 h-4 w-4 animate-spin" />
                  {tz("loadingDetail", "Loading proposal…")}
                </div>
              )
            ) : (
              <div className="flex h-40 items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
                {tz("selectHint", "Select a proposal to review its orders, evidence and checks.")}
              </div>
            )}
          </section>
        </div>

        <PaperPanel paper={paper} error={paperError} tz={tz} onReset={() => afterAction(null)} />
      </div>
    </div>
  );
}

function ModeLine({ list, tz }: { list: ProposalList; tz: Translate }) {
  const required = list.approval_mode === "required";
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-md border px-2 py-1 font-medium",
          required ? "border-success/40 bg-success/10 text-success" : "border-warning/50 bg-warning/10 text-warning",
        )}
      >
        {required ? <ShieldCheck className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
        {required
          ? tz("modeRequired", "Approval required for every order (VIBE_ORDER_APPROVAL=required)")
          : tz("modeOff", "Approval is off: only zt-paper orders are held (set VIBE_ORDER_APPROVAL=required)")}
      </span>
      <span className="text-muted-foreground">
        {tz("ttl", "Approval window {{minutes}} min", { minutes: formatNumber(list.ttl_minutes, 1) })}
      </span>
      <span className={list.ledger.ok ? "text-muted-foreground" : "font-medium text-danger"}>
        {list.ledger.ok
          ? tz("ledgerOk", "Ledger intact ({{count}} records)", { count: list.ledger.record_count })
          : tz("ledgerBroken", "Ledger hash chain is broken: approvals are refused")}
      </span>
      {list.policy.error ? <span className="font-medium text-danger">{list.policy.error}</span> : null}
    </div>
  );
}

function ProposalListView({
  rows,
  loaded,
  selectedId,
  onSelect,
  now,
  tz,
}: {
  rows: ProposalSummary[];
  loaded: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  now: number;
  tz: Translate;
}) {
  if (!loaded) return null;
  if (rows.length === 0) {
    return (
      <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        {tz("empty", "No proposals. When the agent proposes orders they appear here.")}
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {rows.map((row) => (
        <li key={row.id}>
          <button
            type="button"
            onClick={() => onSelect(row.id)}
            aria-current={row.id === selectedId ? "true" : undefined}
            aria-label={tz("openProposal", "Open proposal {{id}}", { id: row.id })}
            className={cn(
              "min-h-[44px] min-w-[44px] w-full rounded-md border p-3 text-start transition hover:bg-muted/60",
              row.id === selectedId && "border-primary/60 bg-primary/5",
            )}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <StatusChip status={row.status} tz={tz} />
                <span className="font-mono text-xs">{shortId(row.id)}</span>
              </div>
              {row.status === "PENDING" ? <Countdown expiresUtc={row.expires_utc} now={now} tz={tz} /> : null}
            </div>
            <p className="mt-2 truncate text-sm font-medium">{row.orders.map(describeOrder).join(" · ") || "—"}</p>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>
                {row.broker} · {row.account}
              </span>
              <span className={row.validation_ok ? "text-success" : "text-danger"}>
                {row.validation_ok
                  ? tz("checksPass", "checks pass")
                  : tz("checksFail", "{{count}} failing check(s)", { count: row.failed_checks.length })}
              </span>
              <span>{new Date(row.created_utc).toLocaleString()}</span>
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}

function ChecksList({ validation, tz }: { validation: Validation; tz: Translate }) {
  return (
    <ul className="space-y-1.5" aria-label={tz("checks", "Validation checks")}>
      {validation.checks.map((check) => (
        <li key={check.name} className="flex items-start gap-2 text-sm">
          {check.status === "PASS" ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-label="PASS" />
          ) : check.blocking ? (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-label="FAIL" />
          ) : (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-label="FAIL (advisory)" />
          )}
          <span className="min-w-0">
            <span className="font-mono text-xs font-semibold">{check.name}</span>{" "}
            <span className="text-xs text-muted-foreground">
              {check.status}
              {check.blocking ? "" : ` · ${tz("advisory", "advisory")}`}
            </span>
            <span className="block break-words text-xs text-muted-foreground">{check.detail}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function WeightsTable({ proposal, tz }: { proposal: ProposalDetail; tz: Translate }) {
  const record = proposal.decision_record;
  const symbols = useMemo(() => {
    const set = new Set<string>([
      ...Object.keys(record.current_weights ?? {}),
      ...Object.keys(record.target_weights ?? {}),
      ...Object.keys(record.projected_weights ?? {}),
      ...proposal.orders.map((o) => o.symbol),
    ]);
    return [...set].sort();
  }, [record, proposal.orders]);
  if (symbols.length === 0) return null;
  const scope = new Set(proposal.account_scope.symbols);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr className="border-b text-start">
            <th className="py-1 pe-2 text-start font-medium">{tz("symbol", "Symbol")}</th>
            <th className="py-1 pe-2 text-end font-medium">{tz("current", "Current")}</th>
            <th className="py-1 pe-2 text-end font-medium">{tz("requested", "Requested")}</th>
            <th className="py-1 pe-2 text-end font-medium">{tz("target", "Target")}</th>
            <th className="py-1 text-end font-medium">{tz("projected", "Projected")}</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((symbol) => (
            <tr key={symbol} className="border-b last:border-0">
              <td className="py-1 pe-2 font-mono">
                {symbol}
                {scope.has(symbol) ? null : (
                  <span className="ms-1 text-muted-foreground">({tz("outOfScope", "out of scope, untouched")})</span>
                )}
              </td>
              <td className="py-1 pe-2 text-end font-mono">{formatPct(record.current_weights?.[symbol] ?? 0, 2)}</td>
              <td className="py-1 pe-2 text-end font-mono">
                {record.requested_targets ? formatPct(record.requested_targets[symbol], 2) : "—"}
              </td>
              <td className="py-1 pe-2 text-end font-mono">
                {record.target_weights ? formatPct(record.target_weights[symbol], 2) : "—"}
              </td>
              <td className="py-1 text-end font-mono">{formatPct(record.projected_weights?.[symbol] ?? 0, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProposalDetailView({
  proposal,
  now,
  tz,
  onDone,
}: {
  proposal: ProposalDetail;
  now: number;
  tz: Translate;
  onDone: (updated: ProposalDetail | null) => Promise<void>;
}) {
  const record = proposal.decision_record;
  const orders: OrderDetail[] = record.orders_detail?.length ? record.orders_detail : proposal.orders;
  const exposure = record.projected_exposure ?? {};
  const before = record.exposure_before ?? {};
  const [revalidation, setRevalidation] = useState<Validation | null>(proposal.revalidation ?? null);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="break-all font-mono text-sm font-semibold">{proposal.id}</h2>
            <StatusChip status={proposal.status} tz={tz} />
            {proposal.status === "PENDING" ? <Countdown expiresUtc={proposal.expires_utc} now={now} tz={tz} /> : null}
          </div>
          <p className="text-xs text-muted-foreground">
            {proposal.broker.label ?? proposal.broker.profile_id} · {proposal.broker.environment}
            {proposal.broker.live ? ` · ${tz("live", "LIVE")}` : ""} · {tz("account", "account")}{" "}
            {proposal.account_scope.account} · {tz("origin", "from")} {proposal.origin.kind}
            {proposal.origin.tool ? ` (${proposal.origin.tool})` : ""}
          </p>
          <p className="text-xs text-muted-foreground">
            {tz("created", "Created")} {new Date(proposal.created_utc).toLocaleString()} · {tz("expires", "expires")}{" "}
            {new Date(proposal.expires_utc).toLocaleString()}
          </p>
        </div>
      </div>

      {proposal.integrity && !proposal.integrity.ok ? (
        <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm">
          {tz("integrityFailed", "Integrity check failed: {{reason}}", { reason: proposal.integrity.reason ?? "" })}
        </p>
      ) : null}

      <Card title={tz("orders", "Orders")}>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted-foreground">
              <tr className="border-b">
                <th className="py-1 pe-2 text-start font-medium">{tz("symbol", "Symbol")}</th>
                <th className="py-1 pe-2 text-start font-medium">{tz("side", "Side")}</th>
                <th className="py-1 pe-2 text-end font-medium">{tz("qty", "Qty / notional")}</th>
                <th className="py-1 pe-2 text-start font-medium">{tz("type", "Type")}</th>
                <th className="py-1 pe-2 text-end font-medium">{tz("limit", "Limit")}</th>
                <th className="py-1 pe-2 text-start font-medium">TIF</th>
                <th className="py-1 pe-2 text-end font-medium">{tz("refClose", "Ref. close")}</th>
                <th className="py-1 text-end font-medium">{tz("estNotional", "Est. notional")}</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => {
                const extra = order;
                return (
                  <tr key={order.symbol} className="border-b last:border-0">
                    <td className="py-1 pe-2 font-mono font-semibold">{order.symbol}</td>
                    <td className={cn("py-1 pe-2 font-semibold uppercase", order.side === "buy" ? "text-success" : "text-danger")}>
                      {order.side}
                    </td>
                    <td className="py-1 pe-2 text-end font-mono">
                      {order.qty !== null && order.qty !== undefined ? formatNumber(order.qty, 4) : `$${formatNumber(order.notional)}`}
                    </td>
                    <td className="py-1 pe-2">{order.order_type}</td>
                    <td className="py-1 pe-2 text-end font-mono">{order.limit_price !== null ? formatNumber(order.limit_price) : "—"}</td>
                    <td className="py-1 pe-2">{order.tif}</td>
                    <td className="py-1 pe-2 text-end font-mono">
                      {formatNumber(extra.reference_price)}
                      {extra.reference_date ? <span className="block text-muted-foreground">{extra.reference_date}</span> : null}
                    </td>
                    <td className="py-1 text-end font-mono">{formatNumber(extra.est_notional_usd)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title={tz("decision", "Decision record")}>
          <div className="space-y-2 text-sm">
            <p className="whitespace-pre-wrap">{record.rationale || tz("noRationale", "No rationale recorded.")}</p>
            {record.evidence_ids?.length ? (
              <p className="text-xs text-muted-foreground">
                {tz("evidence", "Evidence")}: <span className="font-mono">{record.evidence_ids.join(", ")}</span>
              </p>
            ) : null}
            <ul className="space-y-1 text-xs">
              {(record.signals ?? []).map((signal, index) => (
                <li key={`${signal.source}-${signal.symbol}-${index}`} className="rounded border px-2 py-1">
                  <span className="font-semibold">{signal.source}</span> · {signal.symbol ?? tz("allSymbols", "all")} ·{" "}
                  <span className="font-mono">{signal.direction}</span> ·{" "}
                  <span className="text-muted-foreground">
                    {signal.approach === "long_short" ? tz("longShort", "may short") : tz("longOnly", "long-only")}
                  </span>
                  {signal.rationale ? <span className="block text-muted-foreground">{signal.rationale}</span> : null}
                  {signal.evidence_ids.length ? (
                    <span className="block font-mono text-muted-foreground">{signal.evidence_ids.join(", ")}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        </Card>
        <Card title={tz("exposure", "Exposure")}>
          <dl className="grid grid-cols-3 gap-x-2 gap-y-1 text-xs">
            <dt className="text-muted-foreground" />
            <dt className="text-end text-muted-foreground">{tz("now", "Now")}</dt>
            <dt className="text-end text-muted-foreground">{tz("after", "After")}</dt>
            <dt>{tz("gross", "Gross")}</dt>
            <dd className="text-end font-mono">{formatPct(before.gross)}</dd>
            <dd className="text-end font-mono">{formatPct(exposure.gross)}</dd>
            <dt>{tz("net", "Net")}</dt>
            <dd className="text-end font-mono">{formatPct(before.net)}</dd>
            <dd className="text-end font-mono">{formatPct(exposure.net)}</dd>
            <dt>{tz("maxName", "Largest name")}</dt>
            <dd className="text-end font-mono">{formatPct(before.max_name)}</dd>
            <dd className="text-end font-mono">
              {formatPct(exposure.max_name)} {exposure.max_name_symbol ?? ""}
            </dd>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            {tz("equity", "Equity")} {formatNumber(record.equity)} ({record.equity_basis ?? "—"})
          </p>
          {record.clamp_events?.length ? (
            <ul className="mt-2 space-y-1 text-xs" aria-label={tz("clamps", "Clamp events")}>
              {record.clamp_events.map((clamp, index) => (
                <li key={`${clamp.limit}-${clamp.symbol}-${index}`}>
                  {tz("clamped", "Clamped {{symbol}} by {{limit}}: {{before}} → {{after}}", {
                    symbol: clamp.symbol ?? tz("book", "book"),
                    limit: clamp.limit,
                    before: formatPct(clamp.before, 2),
                    after: formatPct(clamp.after, 2),
                  })}
                </li>
              ))}
            </ul>
          ) : null}
        </Card>
      </div>

      <Card title={tz("weights", "Target vs current weights")}>
        <WeightsTable proposal={proposal} tz={tz} />
        {record.direction_permissions?.length ? (
          <ul className="mt-3 space-y-1 text-xs" aria-label={tz("permissions", "Direction permissions")}>
            {record.direction_permissions.map((row) => (
              <li key={row.symbol} className={row.status === "PASS" ? "" : "font-medium text-danger"}>
                {row.symbol}: {formatNumber(row.current_qty, 4)} → {formatNumber(row.projected_qty, 4)}{" "}
                {row.creates_or_increases_short
                  ? row.status === "PASS"
                    ? tz("shortAllowed", "(short allowed by {{sources}})", { sources: row.short_capable_sources.join(", ") })
                    : tz("shortDenied", "(creates a short without short-capable evidence)")
                  : tz("noShort", "(no new short)")}
              </li>
            ))}
          </ul>
        ) : null}
      </Card>

      <Card title={tz("validation", "Validation")}>
        {revalidation ? (
          <div className="mb-3 space-y-2">
            <p role="alert" className="text-xs font-medium text-danger">
              {tz("revalidationFailed", "Approval re-checked the proposal just now and these checks fail:")}
            </p>
            <ChecksList validation={revalidation} tz={tz} />
            <hr />
            <p className="text-xs text-muted-foreground">{tz("whenProposed", "When proposed:")}</p>
          </div>
        ) : null}
        <ChecksList validation={proposal.validation} tz={tz} />
      </Card>

      {proposal.status === "PENDING" ? (
        <DecisionBox proposal={proposal} now={now} tz={tz} onDone={onDone} onRevalidation={setRevalidation} />
      ) : null}

      {proposal.submission ? (
        <Card title={tz("submission", "Submission")}>
          <ul className="space-y-1 text-xs">
            {proposal.submission.results.map((result, index) => (
              <li key={`${result.symbol}-${index}`}>
                <span className="font-mono font-semibold">{result.symbol}</span> {result.side} ·{" "}
                <span className={result.status === "failed" || result.status === "not_submitted" ? "text-danger" : "text-success"}>
                  {result.status}
                </span>
                {result.fill_price ? ` @ ${formatNumber(result.fill_price)}` : ""}
                {result.error ? <span className="block text-danger">{result.error}</span> : null}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <Card title={tz("audit", "Audit trail (hash-chained ledger)")}>
        <ol className="space-y-1 text-xs">
          {(proposal.ledger_events ?? []).map((event) => (
            <li key={event.seq} className="flex flex-wrap gap-x-2">
              <span className="font-mono text-muted-foreground">#{event.seq}</span>
              <span>{new Date(event.at_utc).toLocaleString()}</span>
              <span className="font-medium">
                {event.from ?? "∅"} → {event.to}
              </span>
              <span>{event.actor}</span>
              <span className="text-muted-foreground">{event.reason}</span>
              <span className="font-mono text-muted-foreground">…{hashTail(event.record_hash, 10)}</span>
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}

function DecisionBox({
  proposal,
  now,
  tz,
  onDone,
  onRevalidation,
}: {
  proposal: ProposalDetail;
  now: number;
  tz: Translate;
  onDone: (updated: ProposalDetail | null) => Promise<void>;
  onRevalidation: (validation: Validation | null) => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const expired = secondsUntil(proposal.expires_utc, now) <= 0;
  const fullHash = proposal.content_hash ?? "";
  const tail = hashTail(fullHash || proposal.content_hash_tail);

  const approve = async () => {
    setBusy("approve");
    setError(null);
    try {
      const updated = await ztApprovalsApi.approveProposal(proposal.id, fullHash);
      onRevalidation(null);
      await onDone(updated);
    } catch (err) {
      if (err instanceof ZtApprovalError && err.proposal?.revalidation) onRevalidation(err.proposal.revalidation);
      setError(explain(err, tz));
    } finally {
      setBusy(null);
    }
  };

  const reject = async () => {
    setBusy("reject");
    setError(null);
    try {
      await onDone(await ztApprovalsApi.rejectProposal(proposal.id, reason.trim()));
    } catch (err) {
      setError(explain(err, tz));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card title={tz("decide", "Your decision")} className="border-warning/50">
      {!proposal.validation.ok ? (
        <p className="mb-3 text-xs text-warning">
          {tz(
            "validationWarning",
            "Some checks failed when this was proposed. Approval re-checks everything now and is refused while a blocking check fails.",
          )}
        </p>
      ) : null}
      {proposal.broker.live ? (
        <p className="mb-3 text-xs font-medium text-danger">
          {tz("liveWarning", "LIVE broker: approving sends real orders through Vibe-Trading's mandate gate.")}
        </p>
      ) : null}
      <div className="space-y-4">
        <div className="space-y-2">
          <label className="flex min-h-[44px] min-w-[44px] items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              disabled={expired || busy !== null}
              className="mt-0.5"
            />
            <span>
              {tz(
                "confirmLabel",
                "I reviewed these orders, their evidence and checks, and approve submitting them exactly once.",
              )}
            </span>
          </label>
          <p className="text-xs text-muted-foreground">
            {tz("hashLabel", "Content hash ending")}{" "}
            <span data-testid="hash-tail" className="rounded bg-muted px-1.5 py-0.5 font-mono font-semibold text-foreground">
              {tail}
            </span>
          </p>
          <button
            type="button"
            onClick={() => void approve()}
            disabled={!confirmed || expired || busy !== null || !fullHash}
            className="inline-flex min-h-[44px] min-w-[44px] items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          >
            {busy === "approve" ? <Loader2 className="h-4 w-4 animate-spin" /> : <ClipboardCheck className="h-4 w-4" />}
            {tz("approve", "Approve and submit once")}
          </button>
        </div>
        <div className="space-y-2 border-t pt-3">
          <label className="block text-sm">
            <span className="text-muted-foreground">{tz("rejectReason", "Reason for rejecting")}</span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              rows={2}
              disabled={busy !== null}
              className="mt-1 min-h-[44px] min-w-[44px] w-full rounded-md border bg-background px-2 py-1.5 text-sm"
            />
          </label>
          <button
            type="button"
            onClick={() => void reject()}
            disabled={!reason.trim() || busy !== null}
            className="inline-flex min-h-[44px] min-w-[44px] items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:opacity-50"
          >
            {busy === "reject" ? <Loader2 className="h-4 w-4 animate-spin" /> : <XCircle className="h-4 w-4" />}
            {tz("reject", "Reject")}
          </button>
        </div>
        {expired ? <p className="text-xs text-muted-foreground">{tz("expiredNote", "The approval window has closed.")}</p> : null}
        {error ? (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm">
            {error}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function explain(error: unknown, tz: Translate): string {
  if (error instanceof ApiError) {
    if (error.status === 410) return tz("errExpired", "This proposal expired before approval; ask for a new one.");
    if (error.status === 423) return tz("errHalted", "Live trading is halted (HALT). Clear the kill switch before approving live orders.");
    if (error.status === 409 && error.code === "tampered") {
      return tz("errTampered", "Refused: the proposal changed after it was recorded ({{message}}).", { message: error.message });
    }
    return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

function PaperPanel({
  paper,
  error,
  tz,
  onReset,
}: {
  paper: PaperAccount | null;
  error: string | null;
  tz: Translate;
  onReset: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [cash, setCash] = useState("100000");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const reset = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const amount = Number(cash);
      const result = await ztApprovalsApi.resetPaper(Number.isFinite(amount) && amount > 0 ? amount : undefined);
      setMessage(
        tz("resetDone", "Paper account reset; {{count}} pending paper proposal(s) rejected.", {
          count: result.rejected_proposals.length,
        }),
      );
      setOpen(false);
      setConfirmText("");
      await onReset();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="zt-paper" className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="zt-paper" className="text-lg font-semibold">
            {tz("paperTitle", "zt-paper account")}{" "}
            <span className="ms-1 rounded border border-warning/50 bg-warning/10 px-1.5 py-0.5 text-[11px] font-semibold text-warning">
              {paper?.label ?? "SIMULATED"}
            </span>
          </h2>
          <p className="text-sm text-muted-foreground">
            {tz(
              "paperSubtitle",
              "No broker: approved zt-paper orders fill at the latest completed-session close ({{source}}).",
              { source: paper?.price_source ?? "vt" },
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="inline-flex min-h-[44px] min-w-[44px] items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
        >
          <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
          {tz("resetOpen", "Reset paper account…")}
        </button>
      </div>
      {open ? (
        <div className="flex flex-wrap items-end gap-3 rounded-md border border-warning/50 p-3 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">{tz("startingCash", "Starting cash (USD)")}</span>
            <input
              value={cash}
              onChange={(event) => setCash(event.target.value)}
              inputMode="decimal"
              className="min-h-[44px] min-w-[44px] w-36 rounded-md border bg-background px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">{tz("typeReset", "Type RESET to confirm")}</span>
            <input
              value={confirmText}
              onChange={(event) => setConfirmText(event.target.value)}
              className="min-h-[44px] min-w-[44px] w-36 rounded-md border bg-background px-2 py-1 font-mono"
            />
          </label>
          <button
            type="button"
            onClick={() => void reset()}
            disabled={confirmText !== "RESET" || busy}
            className="inline-flex min-h-[44px] min-w-[44px] items-center gap-1.5 rounded-md border border-danger/50 px-3 py-1.5 text-xs font-medium text-danger transition hover:bg-danger/10 disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            {tz("resetConfirm", "Reset now")}
          </button>
        </div>
      ) : null}
      {message ? (
        <p role="status" className="text-sm">
          {message}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm">
          {error}
        </p>
      ) : null}
      {paper ? (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-4">
            <Stat label={tz("cash", "Cash")} value={formatNumber(paper.cash)} />
            <Stat label={tz("equity", "Equity")} value={formatNumber(paper.equity)} />
            <Stat label={tz("grossExposure", "Gross exposure")} value={formatNumber(paper.gross_exposure)} />
            <Stat
              label={tz("leverage", "Leverage (cap)")}
              value={`${formatNumber(paper.leverage, 3)} (${formatNumber(paper.max_leverage, 2)})`}
            />
          </div>
          {paper.positions.length ? (
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b">
                    <th className="px-2 py-1 text-start font-medium">{tz("symbol", "Symbol")}</th>
                    <th className="px-2 py-1 text-end font-medium">{tz("qtyShort", "Qty")}</th>
                    <th className="px-2 py-1 text-end font-medium">{tz("avgCost", "Avg cost")}</th>
                    <th className="px-2 py-1 text-end font-medium">{tz("mark", "Mark")}</th>
                    <th className="px-2 py-1 text-end font-medium">{tz("value", "Value")}</th>
                    <th className="px-2 py-1 text-end font-medium">{tz("weight", "Weight")}</th>
                    <th className="px-2 py-1 text-end font-medium">{tz("unrealized", "Unrealized")}</th>
                  </tr>
                </thead>
                <tbody>
                  {paper.positions.map((row) => (
                    <tr key={row.symbol} className="border-b last:border-0">
                      <td className="px-2 py-1 font-mono font-semibold">{row.symbol}</td>
                      <td className="px-2 py-1 text-end font-mono">{formatNumber(row.qty, 4)}</td>
                      <td className="px-2 py-1 text-end font-mono">{formatNumber(row.avg_cost)}</td>
                      <td className="px-2 py-1 text-end font-mono">
                        {formatNumber(row.mark)}
                        {row.mark_date ? <span className="block text-muted-foreground">{row.mark_date}</span> : null}
                      </td>
                      <td className="px-2 py-1 text-end font-mono">{formatNumber(row.market_value)}</td>
                      <td className="px-2 py-1 text-end font-mono">{formatPct(row.weight)}</td>
                      <td className="px-2 py-1 text-end font-mono">{formatNumber(row.unrealized_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">{tz("noPositions", "No positions.")}</p>
          )}
          {paper.unpriced.length ? (
            <p className="text-xs text-danger">
              {tz("unpriced", "No completed-session close for {{symbols}}; equity is unknown.", {
                symbols: paper.unpriced.join(", "),
              })}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="font-mono text-sm font-semibold">{value}</p>
    </div>
  );
}
