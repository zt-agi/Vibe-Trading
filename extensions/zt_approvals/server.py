"""ZT add-on: order-proposal tools for Vibe-Trading's agent (stdio FastMCP).

Registered in ``<VIBE_TRADING_HOME>/agent.json`` as ``zt-approvals``; the agent
sees ``mcp_zt_approvals_<tool>``. The agent can PROPOSE orders and read
proposals and the SIMULATED zt-paper account. It can never approve: approval is
a human action in the Web UI (``/zt/approvals``), backed by authenticated
routes this server does not expose. Responses carry only the tail of a
proposal's content hash, which is not enough to approve it.

Tools:
    propose_orders(orders | targets, rationale, evidence_ids, broker="zt-paper",
                   signals=None, scope_symbols=None)
    list_order_proposals(status="", limit=20)
    get_order_proposal(proposal_id)
    paper_account()
"""
from __future__ import annotations

import contextlib
import inspect
import sys
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_AGENT = _HERE.parents[1] / "agent"
# Running this file puts its folder first on sys.path; VT's agent/ goes first instead.
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != _HERE]
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))

from fastmcp import FastMCP  # noqa: E402

from src.live import order_proposals as core  # noqa: E402

mcp = FastMCP("zt-approvals")

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
PROPOSE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


def _view(proposal: dict[str, Any]) -> dict[str, Any]:
    view = core.public_view(proposal, full_hash=False)
    view.pop("approval", None)
    return view


def propose_orders(
    rationale: str,
    evidence_ids: list[str],
    orders: Optional[list[dict[str, Any]]] = None,
    targets: Optional[dict[str, float]] = None,
    broker: str = "zt-paper",
    signals: Optional[list[dict[str, Any]]] = None,
    scope_symbols: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Record an order proposal for a human to approve; nothing is submitted.

    Give exactly one of:
      orders:  [{symbol, side: buy|sell, qty (or notional), order_type: market|limit,
                 limit_price (limit only), tif: day|gtc}]
      targets: {symbol: weight}, weights as fractions of account equity (0.05 = 5%);
               clamped to the per-name and gross caps and diffed against the current
               book within the account scope (other holdings are never touched).
    rationale and evidence_ids explain the decision (cite evidence ids from your tools).
    signals (optional): [{source, symbol, direction: long|short|reduce|flat|neutral,
      rationale, evidence_ids}]; whether a source may create shorts is set by the
      user's policy, never by the proposer (long-only sources may only reduce longs).
    broker: "zt-paper" (SIMULATED account ZT-PAPER, default) or a VT trading profile id.
    scope_symbols (optional): the symbols this decision manages.
    Returns the proposal id, status PENDING, expiry, validation checks and the
    approval page. A human approves or rejects it in Vibe-Trading; do not resubmit.
    """
    try:
        proposal = core.create_proposal(
            broker=broker, orders=orders, targets=targets, rationale=rationale,
            evidence_ids=evidence_ids, signals=signals, scope_symbols=scope_symbols,
            origin={"kind": "mcp_propose", "tool": "propose_orders", "actor": "agent", "source": "agent"})
    except core.ProposalError as exc:
        raise ValueError(str(exc)) from exc
    result = core.hold_envelope(proposal)
    result["validation"] = proposal["validation"]
    result["decision_summary"] = {k: proposal["decision_record"].get(k) for k in (
        "equity", "current_weights", "target_weights", "projected_weights", "clamp_events",
        "projected_exposure")}
    return result


def list_order_proposals(status: str = "", limit: int = 20) -> dict[str, Any]:
    """List order proposals, newest first (status: PENDING, APPROVED, SUBMITTED,
    FILLED, FAILED, REJECTED, EXPIRED or empty for all)."""
    wanted = status.strip().upper()
    if wanted and wanted not in core.STATUSES:
        raise ValueError(f"status must be one of {', '.join(core.STATUSES)}")
    rows = core.list_proposals(status=wanted or None, limit=max(1, min(int(limit), 100)))
    return {"approval_mode": core.approval_mode(), "ttl_minutes": core.ttl_minutes(),
            "proposals": [core.summary(p) for p in rows]}


def get_order_proposal(proposal_id: str) -> dict[str, Any]:
    """One proposal: orders, decision record, validation checks, status and results."""
    try:
        return _view(core.expire_if_due(proposal_id.strip()))
    except core.ProposalError as exc:
        raise ValueError(str(exc)) from exc


def paper_account() -> dict[str, Any]:
    """The SIMULATED ZT-PAPER account: cash, equity, positions at completed-session
    closes, exposure and recent fills."""
    return core.zt_paper_engine().snapshot()


TOOLS = (propose_orders, list_order_proposals, get_order_proposal, paper_account)
TOOL_NAMES = tuple(tool.__name__ for tool in TOOLS)

mcp.tool(propose_orders, annotations=PROPOSE)
for _tool in TOOLS[1:]:
    mcp.tool(_tool, annotations=READ_ONLY)


def load_price_stack() -> None:
    """Load the numeric stack and VT's price loaders on the calling (main) thread.

    propose_orders reads the reference close through VT's loader chain
    (``order_proposals.vt_loader_close``), which imports pandas, numpy and every
    loader module. On Windows, loading numpy's OpenBLAS DLL for the first time
    inside FastMCP's worker thread deadlocks while the stdio reader thread is
    blocked on stdin (PC1, 2026-09-29: the first propose_orders never returned),
    so the stdio server loads them before it starts reading, as zt_events,
    zt_ontology and zt_research do. stdout carries the protocol, so anything a
    loader prints while importing goes to stderr.
    """
    with contextlib.redirect_stdout(sys.stderr):
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        from backtest.loaders.registry import _ensure_registered

        _ensure_registered()


if __name__ == "__main__":
    load_price_stack()
    # stdout carries the protocol; skip the startup banner where supported.
    if "show_banner" in inspect.signature(mcp.run).parameters:
        mcp.run(show_banner=False)
    else:
        mcp.run()
