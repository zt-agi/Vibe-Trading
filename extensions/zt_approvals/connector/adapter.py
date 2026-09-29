"""zt-paper read adapter for VT's local connector plugins (ZT add-on).

Installed the VT-native way (``vibe-trading connector install
extensions/zt_approvals/connector`` copies this folder to
``<VIBE_TRADING_HOME>/connectors/zt-paper``), it gives VT's connections, the
agent's ``trading_account`` / ``trading_positions`` / ``trading_quote`` tools and
``vibe-trading connector account zt-paper`` a read view of the SIMULATED
ZT-PAPER account. VT loads local plugins read-only by design; an order to this
profile (``trading_place_order`` with connection ``zt-paper``) becomes a
proposal a human approves (``src/live/order_proposals.py``).

The adapter runs inside VT's process and delegates to the engine in this
checkout's ``extensions/zt_approvals/zt_paper.py``, so the installed copy never
drifts from it. No credentials are used or stored.
"""
from __future__ import annotations

from typing import Any, Mapping


def _engine() -> Any:
    from src.live.order_proposals import zt_paper_engine

    return zt_paper_engine()


def check_status(*, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    engine = _engine()
    return {"status": "ok", "configured": True, "readonly": True, "simulated": True,
            "account": engine.ACCOUNT, "label": engine.LABEL, "price_source": engine.price_source_name()}


def get_account_snapshot(*, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    snap = _engine().snapshot()
    return {"status": "ok", "simulated": True, "label": snap["label"],
            "account": {"account_id": snap["account"], "currency": snap["currency"], "cash": snap["cash"],
                        "equity": snap["equity"], "buying_power": snap["cash"],
                        "gross_exposure": snap["gross_exposure"], "leverage": snap["leverage"],
                        "valuation": snap["valuation"], "price_source": snap["price_source"]}}


def get_positions(*, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    snap = _engine().snapshot()
    return {"status": "ok", "simulated": True, "positions": [
        {"symbol": row["symbol"], "quantity": row["qty"], "average_cost": row["avg_cost"],
         "market_price": row["mark"], "market_value": row["market_value"], "currency": snap["currency"],
         "asset_type": "equity", "price_date": row["mark_date"]}
        for row in snap["positions"]]}


def get_open_orders(*, credentials: Mapping[str, str], config: Mapping[str, Any],
                    include_executions: bool = False) -> dict[str, Any]:
    engine = _engine()
    result: dict[str, Any] = {"status": "ok", "simulated": True, "open_orders": [],
                              "note": "zt-paper fills approved proposals at once; nothing rests"}
    if include_executions:
        result["executions"] = [row for row in engine.fills(limit=50) if row.get("event") == "fill"]
    return result


def get_quote(symbol: str, *, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    engine = _engine()
    try:
        ticker = engine.normalize_symbol(symbol)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    ref = engine.reference_close(ticker)
    if ref is None:
        return {"status": "error", "error": f"no completed-session close for {ticker}"}
    return {"status": "ok", "symbol": ticker, "simulated": True,
            "quote": {"close": ref.price, "last": ref.price, "bar_date": ref.bar_date, "source": ref.source,
                      "basis": "latest completed-session close"}}
