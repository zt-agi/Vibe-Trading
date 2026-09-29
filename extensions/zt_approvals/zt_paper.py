"""zt-paper: a simulated US-equity account for real tests without a broker (ZT add-on).

Account ``ZT-PAPER`` (label ``SIMULATED``, USD) lives under
``<VIBE_TRADING_HOME>/live/zt-paper/``: ``account.json`` (cash, positions,
settings; written atomically under a lock), ``fills.jsonl`` (append-only fill
and reset log) and ``history/`` (the state before each reset).

Orders reach this account only through an approved order proposal
(``src/live/order_proposals.py``): the agent proposes, a human approves in the
Web UI, and :func:`fill_orders` runs once per proposal. Fills happen at the
latest completed-session close (ai-hedge-fund's rule: the current New York date
is excluded even after the close), priced by VT's own loader chain or, with
``ZT_PAPER_PRICE_SOURCE=pitdb``, by the pitdb warehouse through VT's
``source="pitdb"`` loader (``pitdb_then_vt`` tries pitdb first). A batch is
all-or-nothing: a missing, non-finite or non-positive price, a limit order that
is not marketable at that close, or a post-trade gross exposure above
``max_leverage`` x equity rejects every order in it and changes nothing.

Reset: ``POST /zt/paper/reset`` (Web UI) or
``python extensions/zt_approvals/zt_paper.py reset --starting-cash 100000``.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ACCOUNT = "ZT-PAPER"
LABEL = "SIMULATED"
CURRENCY = "USD"
DEFAULT_STARTING_CASH = 100_000.0
DEFAULT_MAX_LEVERAGE = 1.0
MAX_STARTING_CASH = 1_000_000_000.0
MAX_LEVERAGE_CAP = 4.0
PRICE_SOURCE_ENV = "ZT_PAPER_PRICE_SOURCE"
PRICE_SOURCES = ("vt", "pitdb", "pitdb_then_vt")
SCHEMA = "zt-paper/v1"
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(-[A-Z])?$")
_EPS = 1e-9


class PaperRejected(Exception):
    """The simulated broker refused a batch; nothing was filled."""


def _core():
    agent = Path(__file__).resolve().parents[2] / "agent"
    if str(agent) not in sys.path:
        sys.path.insert(0, str(agent))
    from src.live import order_proposals

    return order_proposals


def _now() -> datetime:
    return _core()._now()


def _iso(moment: datetime) -> str:
    return _core()._iso(moment)


def state_dir() -> Path:
    from src.live.paths import live_root

    return live_root() / "zt-paper"


def account_path() -> Path:
    return state_dir() / "account.json"


def fills_path() -> Path:
    return state_dir() / "fills.jsonl"


def _finite(value: Any) -> float | None:
    return _core()._finite(value)


# ---------------------------------------------------------------------------
# Symbols and prices
# ---------------------------------------------------------------------------


def normalize_symbol(symbol: Any) -> str:
    """Canonical ticker: ``aapl``/``AAPL.US``/``US.AAPL`` -> ``AAPL``; ``BRK.B`` -> ``BRK-B``."""
    token = str(symbol or "").strip().upper()
    if token.startswith("US."):
        token = token[3:]
    if token.endswith(".US"):
        token = token[:-3]
    token = re.sub(r"^([A-Z][A-Z0-9]{0,9})[./]([A-Z])$", r"\1-\2", token)
    if not _TICKER_RE.fullmatch(token):
        raise ValueError(f"zt-paper trades US-listed equities and ETFs by ticker (e.g. AAPL, BRK-B), not {symbol!r}")
    return token


def price_source_name() -> str:
    from src.config.accessor import get_env_value

    raw = get_env_value(PRICE_SOURCE_ENV, "").strip().lower() or "vt"
    return raw if raw in PRICE_SOURCES else "vt"


def _pitdb_close(symbol: str) -> Any:
    core = _core()
    try:
        from backtest.loaders.pitdb_loader import PitdbLoader
    except Exception:  # noqa: BLE001
        return None
    cutoff = core.completed_session_cutoff("us_equity")
    loader = PitdbLoader()
    try:
        if not loader.is_available():
            return None
        now = _now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        loader.bind_run_config({"pit": {"mode": "snapshot", "run_asof_utc": now, "availability_lag": "36h",
                                        "claim": "research"}})
        start = cutoff - timedelta(days=14)
        frames = loader.fetch([f"{symbol}.US"], start.isoformat(), cutoff.isoformat())
    except Exception:  # noqa: BLE001 - unavailable or refused: no price
        return None
    frame = frames.get(f"{symbol}.US")
    if frame is None or frame.empty:
        return None
    best = None
    for stamp, close in zip(frame.index, frame["close"].tolist()):
        value = _finite(close)
        bar_date = stamp.date()
        if value is None or value <= 0 or bar_date > cutoff:
            continue
        if best is None or bar_date >= best[0]:
            best = (bar_date, value)
    if best is None:
        return None
    return core.RefPrice(symbol=symbol, price=best[1], bar_date=best[0].isoformat(), source="pitdb:price_asof")


def reference_close(symbol: str) -> Any:
    """Latest completed-session close for a ticker, or ``None`` (fail closed)."""
    core = _core()
    ticker = normalize_symbol(symbol)
    source = price_source_name()
    if source in ("pitdb", "pitdb_then_vt"):
        ref = _pitdb_close(ticker)
        if ref is not None or source == "pitdb":
            return ref
    ref = core.vt_loader_close(ticker, market="us_equity")
    if ref is None:
        return None
    return core.RefPrice(symbol=ticker, price=ref.price, bar_date=ref.bar_date, source=ref.source)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _default_account(starting_cash: float = DEFAULT_STARTING_CASH,
                     max_leverage: float = DEFAULT_MAX_LEVERAGE) -> dict[str, Any]:
    now = _iso(_now())
    return {"schema": SCHEMA, "account": ACCOUNT, "label": LABEL, "currency": CURRENCY,
            "starting_cash": starting_cash, "cash": starting_cash, "max_leverage": max_leverage,
            "positions": {}, "realized_pnl": 0.0, "created_utc": now, "updated_utc": now,
            "fills_count": 0, "filled_proposals": [], "persisted": False}


def load_account() -> dict[str, Any]:
    """The account state (a fresh default, not yet written, when none exists)."""
    path = account_path()
    if not path.is_file():
        return _default_account()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise PaperRejected(f"{path} is not a zt-paper account file")
    payload["persisted"] = True
    return payload


def _save(account: dict[str, Any]) -> None:
    body = {key: value for key, value in account.items() if key != "persisted"}
    _core().write_json_atomic(account_path(), body)


def _append_log(lines: Sequence[Mapping[str, Any]]) -> None:
    path = fills_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()


def _lock():
    return _core().file_lock(state_dir() / ".lock", wait_seconds=30.0)


def fills(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first fill and reset log entries."""
    path = fills_path()
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


def book(price_lookup: Any = None) -> Any:
    """The account as a ``core.Book`` marked at completed-session closes."""
    core = _core()
    lookup = price_lookup or reference_close
    account = load_account()
    positions = {s: float(p["qty"]) for s, p in account["positions"].items() if abs(float(p["qty"])) > _EPS}
    marks: dict[str, float] = {}
    unpriced = []
    for symbol in sorted(positions):
        ref = lookup(symbol)
        if ref is None or not _finite(ref.price) or ref.price <= 0:
            unpriced.append(symbol)
        else:
            marks[symbol] = ref.price
    cash = float(account["cash"])
    equity = None if unpriced else cash + sum(qty * marks[s] for s, qty in positions.items())
    return core.Book(account=ACCOUNT, positions=positions, marks=marks, equity=equity, cash=cash,
                     source=f"zt-paper ({price_source_name()})",
                     error=("cannot value " + ", ".join(unpriced)) if unpriced else None,
                     equity_basis="cash + positions at completed-session closes")


def snapshot(price_lookup: Any = None) -> dict[str, Any]:
    """Account view for the Web UI, the MCP tool and the VT connector adapter."""
    core = _core()
    lookup = core.cached_lookup(price_lookup or reference_close)
    account = load_account()
    cash = float(account["cash"])
    rows: list[dict[str, Any]] = []
    values: list[float] = []
    unpriced: list[str] = []
    for symbol, position in sorted(account["positions"].items()):
        qty = float(position["qty"])
        if abs(qty) <= _EPS:
            continue
        ref = lookup(symbol)
        mark = ref.price if ref is not None else None
        value = qty * mark if mark is not None else None
        if value is None:
            unpriced.append(symbol)
        else:
            values.append(value)
        rows.append({"symbol": symbol, "qty": qty, "avg_cost": position.get("avg_cost"),
                     "mark": mark, "mark_date": ref.bar_date if ref else None,
                     "mark_source": ref.source if ref else None, "market_value": value,
                     "unrealized_pnl": (mark - float(position.get("avg_cost") or 0.0)) * qty if mark is not None else None,
                     "realized_pnl": position.get("realized_pnl", 0.0)})
    equity = None if unpriced else cash + sum(values)
    gross = sum(abs(v) for v in values)
    for row in rows:
        row["weight"] = (row["market_value"] / equity) if equity and row["market_value"] is not None else None
    return {
        "account": ACCOUNT, "label": LABEL, "currency": CURRENCY, "simulated": True,
        "cash": round(cash, 6), "equity": round(equity, 6) if equity is not None else None,
        "starting_cash": account["starting_cash"], "max_leverage": account["max_leverage"],
        "gross_exposure": round(gross, 6), "net_exposure": round(sum(values), 6),
        "leverage": round(gross / equity, 6) if equity else None,
        "realized_pnl": account.get("realized_pnl", 0.0), "positions": rows, "unpriced": unpriced,
        "price_source": price_source_name(),
        "valuation": "latest completed-session close (the current New York date is excluded)",
        "created_utc": account.get("created_utc"), "updated_utc": account.get("updated_utc"),
        "fills_count": account.get("fills_count", 0), "persisted": account.get("persisted", False),
        "recent_fills": fills(limit=20),
    }


# ---------------------------------------------------------------------------
# Fills and reset
# ---------------------------------------------------------------------------


def fill_orders(orders: Sequence[Mapping[str, Any]], *, proposal_id: str, price_lookup: Any = None) -> list[dict[str, Any]]:
    """Fill an approved proposal's orders at completed-session closes (all or nothing)."""
    core = _core()
    if not orders:
        raise PaperRejected("no orders to fill")
    lookup = core.cached_lookup(price_lookup or reference_close)
    with _lock():
        account = load_account()
        if proposal_id in account.get("filled_proposals", []):
            raise PaperRejected(f"proposal {proposal_id} was already filled")
        positions = {s: dict(p) for s, p in account["positions"].items()}
        cash = float(account["cash"])
        realized_total = float(account.get("realized_pnl", 0.0))
        now = _iso(_now())
        batch: list[dict[str, Any]] = []
        for index, order in enumerate(orders):
            symbol = normalize_symbol(order.get("symbol"))
            side = str(order.get("side") or "").lower()
            if side not in ("buy", "sell"):
                raise PaperRejected(f"order {index}: side must be buy or sell")
            ref = lookup(symbol)
            price = _finite(ref.price) if ref is not None else None
            if price is None or price <= 0:
                raise PaperRejected(f"{symbol}: no finite positive completed-session close; nothing was filled")
            if str(order.get("order_type") or "market").lower() == "limit":
                limit = _finite(order.get("limit_price"))
                if limit is None or limit <= 0:
                    raise PaperRejected(f"{symbol}: limit order without a finite positive limit price")
                if (side == "buy" and limit < price) or (side == "sell" and limit > price):
                    raise PaperRejected(f"{symbol}: limit {limit:g} is not marketable at the close {price:g}; "
                                        "the simulator has no resting book, so nothing was filled")
            qty = _finite(order.get("qty"))
            if qty is None:
                notional = _finite(order.get("notional"))
                qty = notional / price if notional else None
            if qty is None or qty <= 0:
                raise PaperRejected(f"{symbol}: quantity must be finite and positive")
            signed = qty if side == "buy" else -qty
            position = positions.get(symbol, {"qty": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0})
            old_qty = float(position["qty"])
            old_avg = float(position.get("avg_cost") or 0.0)
            new_qty = old_qty + signed
            realized = 0.0
            if abs(old_qty) <= _EPS or (old_qty > 0) == (signed > 0):
                avg = (abs(old_qty) * old_avg + qty * price) / abs(new_qty)
            else:
                closed = min(abs(signed), abs(old_qty))
                realized = closed * (price - old_avg) * (1.0 if old_qty > 0 else -1.0)
                if abs(new_qty) <= _EPS:
                    new_qty, avg = 0.0, 0.0
                elif (new_qty > 0) != (old_qty > 0):
                    avg = price
                else:
                    avg = old_avg
            cash -= signed * price
            realized_total += realized
            position = {"qty": round(new_qty, 8), "avg_cost": round(avg, 8),
                        "realized_pnl": round(float(position.get("realized_pnl", 0.0)) + realized, 6)}
            positions[symbol] = position
            batch.append({"event": "fill", "fill_id": f"zf_{proposal_id[3:15]}_{index}", "proposal_id": proposal_id,
                          "account": ACCOUNT, "label": LABEL, "symbol": symbol, "side": side, "qty": round(qty, 8),
                          "price": price, "price_date": ref.bar_date, "price_source": ref.source,
                          "notional": round(qty * price, 6), "realized_pnl": round(realized, 6),
                          "cash_after": round(cash, 6), "position_after": position["qty"], "at_utc": now})
        held = {s: p for s, p in positions.items() if abs(float(p["qty"])) > _EPS}
        marks = {}
        for symbol in held:
            ref = lookup(symbol)
            price = _finite(ref.price) if ref is not None else None
            if price is None or price <= 0:
                raise PaperRejected(f"cannot value {symbol} after the fill; nothing was filled")
            marks[symbol] = price
        gross = sum(abs(float(p["qty"])) * marks[s] for s, p in held.items())
        equity = cash + sum(float(p["qty"]) * marks[s] for s, p in held.items())
        if not math.isfinite(equity) or equity <= 0:
            raise PaperRejected(f"post-trade equity {equity:,.2f} is not positive; nothing was filled")
        max_leverage = float(account.get("max_leverage", DEFAULT_MAX_LEVERAGE))
        if gross / equity > max_leverage + _EPS:
            raise PaperRejected(f"leverage cap: post-trade gross {gross:,.2f} / equity {equity:,.2f} = "
                                f"{gross / equity:.4f} > {max_leverage:g}; nothing was filled")
        account["positions"] = held
        account["cash"] = round(cash, 6)
        account["realized_pnl"] = round(realized_total, 6)
        account["updated_utc"] = now
        account["fills_count"] = int(account.get("fills_count", 0)) + len(batch)
        account["filled_proposals"] = (list(account.get("filled_proposals", [])) + [proposal_id])[-1000:]
        account["last_batch"] = {"proposal_id": proposal_id, "fill_ids": [f["fill_id"] for f in batch], "at_utc": now}
        _save(account)
        _append_log(batch)
    return batch


def reset(*, starting_cash: float = DEFAULT_STARTING_CASH, max_leverage: float = DEFAULT_MAX_LEVERAGE,
          actor: str = "local-user", reason: str = "reset") -> dict[str, Any]:
    """Start a fresh ZT-PAPER account; the old state is archived under history/."""
    cash = _finite(starting_cash)
    leverage = _finite(max_leverage)
    if cash is None or not 0 < cash <= MAX_STARTING_CASH:
        raise ValueError(f"starting_cash must be finite, positive and at most {MAX_STARTING_CASH:,.0f}")
    if leverage is None or not 0 < leverage <= MAX_LEVERAGE_CAP:
        raise ValueError(f"max_leverage must be finite in (0, {MAX_LEVERAGE_CAP:g}]")
    with _lock():
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        if account_path().is_file():
            history = state_dir() / "history"
            history.mkdir(parents=True, exist_ok=True)
            (history / f"account-{stamp}.json").write_bytes(account_path().read_bytes())
        account = _default_account(cash, leverage)
        _save(account)
        _append_log([{"event": "reset", "account": ACCOUNT, "label": LABEL, "starting_cash": cash,
                      "max_leverage": leverage, "actor": actor, "reason": reason, "at_utc": account["created_utc"]}])
    return load_account()


def reject_pending_proposals(reason: str = "the zt-paper account was reset") -> list[str]:
    """Reject every PENDING zt-paper proposal (they were valued against the old book)."""
    core = _core()
    rejected = []
    for proposal in core.list_proposals(status=core.PENDING, limit=500):
        if (proposal.get("broker") or {}).get("key") != core.ZT_PAPER:
            continue
        try:
            core.reject_proposal(proposal["id"], reason=reason, actor="system")
        except core.ProposalError:
            continue
        rejected.append(proposal["id"])
    return rejected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="zt-paper simulated account (SIMULATED, ZT-PAPER)")
    sub = parser.add_subparsers(dest="command", required=True)
    reset_cmd = sub.add_parser("reset", help="start a fresh ZT-PAPER account (the old one is archived)")
    reset_cmd.add_argument("--starting-cash", type=float, default=DEFAULT_STARTING_CASH)
    reset_cmd.add_argument("--max-leverage", type=float, default=DEFAULT_MAX_LEVERAGE)
    reset_cmd.add_argument("--reason", default="reset from the command line")
    sub.add_parser("show", help="print the account (positions marked at completed-session closes)")
    args = parser.parse_args(argv)
    if args.command == "reset":
        account = reset(starting_cash=args.starting_cash, max_leverage=args.max_leverage,
                        actor="local-user (cli)", reason=args.reason)
        summary = {k: account[k] for k in ("account", "label", "cash", "max_leverage", "created_utc")}
        summary["rejected_pending_proposals"] = reject_pending_proposals()
        print(json.dumps(summary, indent=2))
        return 0
    print(json.dumps(snapshot(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
