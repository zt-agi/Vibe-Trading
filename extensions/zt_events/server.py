"""ZT add-on: read-only earnings-event tools for Vibe-Trading (``zt-events``).

Opt-in stdio MCP server, registered in ``<VIBE_TRADING_HOME>/agent.json`` like
``pit-actor-sim``. Tools (VT names them ``mcp_zt_events_<tool>``):

* ``earnings_8k_events(tickers, start, asof)`` -- 8-K Item 2.02 releases
  knowable at ``asof``, with acceptance instants and firm-quarter duplicates;
* ``sue(ticker, asof)`` -- standardized unexpected earnings of the newest
  quarter known at ``asof``, with every input's knowledge time;
* ``event_study(tickers, windows, start, end, asof)`` -- CAR tables by SUE
  bucket with clustering-robust tests, Benjamini-Hochberg across windows x
  groups, and every skipped event;
* ``pead_candidates(asof)`` -- fresh events with SUE, entry timing and
  bucket history, plus exits due, each candidate and exit with ready-made
  order-PROPOSAL arguments (recorded by the approvals tool, approved by a
  human; nothing is proposed or placed here).

Every response carries ``asof``, knowledge times and PIT classes. Reads go
through pitdb's sanctioned macros on the read-only E: index; nothing is
written, fetched or traded, and no response contains an outcome probability
(hit rates are counts).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True   # never write __pycache__ into the canonical G: tree

from fastmcp import FastMCP  # noqa: E402

# ZT add-on (Windows stdio fix, as in zt_ontology): import the numeric stack on
# the main thread at startup, never first inside FastMCP's worker thread.
import duckdb  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import scipy.stats  # noqa: E402,F401

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import core  # noqa: E402

mcp = FastMCP("zt-events")

MAX_TICKERS = 60


def _tickers(raw, *, required: bool = False) -> list[str] | None:
    if raw in (None, "", []):
        if required:
            raise ValueError("tickers is required")
        return None
    items = [raw] if isinstance(raw, str) else list(raw)
    names = [str(t).strip().upper() for t in items if str(t).strip()]
    if not names or len(names) > MAX_TICKERS:
        raise ValueError(f"give 1..{MAX_TICKERS} tickers")
    for name in names:
        if len(name) > 12 or not all(c.isalnum() or c in ".-^" for c in name):
            raise ValueError(f"not a ticker: {name!r}")
    return names


def _day(raw, label: str):
    if raw in (None, ""):
        return None
    import datetime as _dt
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def earnings_8k_events(tickers: list[str] | None = None, start: str | None = None,
                       asof: str = "") -> dict:
    """8-K Item 2.02 (Results of Operations) filings knowable at ``asof``.

    ``asof`` is an ISO instant with an offset (e.g. 2026-09-29T12:00:00Z);
    ``start`` (YYYY-MM-DD) bounds the acceptance date. Each row: accession,
    form, items, acceptance instant (``knowledge_time`` = ``event_time``, UTC),
    the estimated fiscal quarter, ``duplicate_of`` for a second Item 2.02
    filing in the same firm-quarter (the earliest counts), the SEC URL and the
    source's PIT class.
    """
    at = core.asof_utc(asof)
    names = _tickers(tickers)
    begin = _day(start, "start")
    with core.store() as (con, provenance):
        frame = core.earnings_events(con, at, tickers=names, start=begin)
    return {"asof": core.iso(at), "authority": core.AUTHORITY, "provenance": provenance,
            "row_count": int(len(frame)), "truncated": len(frame) > core.MAX_ROWS,
            "rows": core.records(frame)}


def sue(ticker: str, asof: str) -> dict:
    """Standardized unexpected earnings of the newest quarter known at ``asof``.

    Seasonal random walk on quarterly diluted EPS from 10-Q/10-K XBRL (Q4 =
    fiscal year minus nine months), standardized by the standard deviation of
    the last 8 seasonal changes (at least 4). Values are rebased across stock
    splits detected in the diluted share count. ``knowledge_time`` is the
    acceptance of the filing that first reported the newest input; every input
    row carries its own knowledge time, revision and PIT class.
    """
    at = core.asof_utc(asof)
    name = (_tickers([ticker], required=True) or [""])[0]
    with core.store() as (con, provenance):
        _, sue_module = core.pitdb()
        result = sue_module.sue_asof(con, name, at)
    return {"asof": core.iso(at), "authority": core.AUTHORITY, "provenance": provenance,
            **{k: core._json_value(v) for k, v in result.items() if k != "asof"}}


def event_study(tickers: list[str] | None = None, windows: list[list[int]] | None = None,
                start: str | None = None, end: str | None = None, asof: str = "",
                benchmark: str = core.DEFAULT_BENCHMARK, cluster_freq: str = "W",
                fdr_test: str = "bmp_kp") -> dict:
    """CARs around 8-K earnings releases, by SUE bucket, as knowable at ``asof``.

    Day 0 is the first session whose close is after the 8-K acceptance (after
    the close -> next session). Market model on ``benchmark`` over sessions
    [-250, -11], cutting the firm's other events out. ``windows`` defaults to
    [[0,1],[2,20],[2,60]]. Per window x group (ALL, Q1..Q5, SIGNED): mean CAR,
    t, clustered t, Patell, BMP, Kolari-Pynnonen, sign, GRANK, a cluster
    bootstrap interval and the Benjamini-Hochberg adjusted p of ``fdr_test``;
    plus hit counts (CAR > 0, not probabilities) and every skipped event.
    """
    at = core.asof_utc(asof)
    names = _tickers(tickers)
    if cluster_freq not in ("D", "W", "M", "Q"):
        raise ValueError("cluster_freq must be D, W, M or Q")
    with core.store() as (con, provenance):
        result = core.run_study(con, provenance, tickers=names, windows=windows,
                                start=_day(start, "start"), end=_day(end, "end"), asof=at,
                                benchmark=str(benchmark).upper(), cluster_freq=cluster_freq,
                                fdr_test=fdr_test)
    return core.drop_private(result)


def pead_candidates(asof: str, tickers: list[str] | None = None, lookback_days: int = 10,
                    short_bottom: bool = False, hold_sessions: int = core.DEFAULT_HOLD_SESSIONS,
                    slot_weight: float = core.DEFAULT_SLOT_WEIGHT) -> dict:
    """Fresh 8-K Item 2.02 events with SUE, bucket, entry timing and history; exits due.

    The decision session is the first session whose close is after both the
    8-K acceptance and the SUE's knowledge time (the 10-Q/10-K); the pead_8k
    backtest fills at the next open and holds ``hold_sessions`` sessions.
    ``candidates`` are ENTRY_DUE or LATE events in the top SUE bucket (and the
    bottom bucket when ``short_bottom``); ``watchlist`` lists every other fresh
    event with its status (SUE_PENDING, SUE_LATE, AWAITING_DECISION_CLOSE,
    STALE, or a middle bucket); ``exits`` lists traded-bucket events whose
    hold ends at the next open (EXIT_DUE) or just did (EXIT_LATE). Each
    candidate and exit carries ``proposal``: order-proposal arguments (target
    weight +/-``slot_weight``, 0 for an exit) to forward unchanged to the
    approvals tool; a human approves every order. Hit counts are not
    probabilities.
    """
    at = core.asof_utc(asof)
    names = _tickers(tickers)
    if not 1 <= int(lookback_days) <= 30:
        raise ValueError("lookback_days must be 1..30")
    if not 1 <= int(hold_sessions) <= 120:
        raise ValueError("hold_sessions must be 1..120")
    if not 0.0 < float(slot_weight) <= 0.25:
        raise ValueError("slot_weight must lie in (0, 0.25]")
    with core.store() as (con, provenance):
        return core.pead_candidates(con, provenance, asof=at, tickers=names,
                                    lookback_days=int(lookback_days),
                                    short_bucket="Q1" if short_bottom else None,
                                    hold_sessions=int(hold_sessions), slot_weight=float(slot_weight))


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
             "openWorldHint": False}
TOOLS = (earnings_8k_events, sue, event_study, pead_candidates)
TOOL_NAMES = tuple(tool.__name__ for tool in TOOLS)
for _tool in TOOLS:
    mcp.tool(_tool, annotations=READ_ONLY)


if __name__ == "__main__":
    # stdout carries the protocol; skip the startup banner where supported.
    import inspect

    if "show_banner" in inspect.signature(mcp.run).parameters:
        mcp.run(show_banner=False)
    else:
        mcp.run()
