"""ZT add-on: investor-style factor pack for Vibe-Trading (read-only, LLM-free).

Opt-in stdio MCP server. One tool, ``investor_style_scores(ticker, asof)``,
recomputes the numeric rules behind ai-hedge-fund's investor personas (MIT,
commit 5d2c7ca2) from ZT's point-in-time warehouse: defensive-value checks
(P/E against 15-20, current ratio >= 1.5, earnings positive every year, debt
against net current assets), the growth-at-a-reasonable-price tier and PEG,
quality (return-on-equity consistency, book value per share CAGR over real
period spacing), cash-flow trend, debt/equity and inflection (growth and
margin acceleration). Each rule reports its value, a pass/fail status, its
inputs with knowledge times and what was missing; a style label describes
which rule families pass. No LLM, no probability, no trade instruction.

Facts come only through the sanctioned as-of macros, via the sibling
pit_actor_sim extension's guarded query path (fresh-index receipt, approved
SQL, E: runtime): ``pit_security`` and ``pit_price_history`` (price_asof) and
``issuer_annual_facts`` (obs_asof over ``SEC:<TICKER>:*:FY``). Nothing is
written.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never write __pycache__ into the canonical tree

from fastmcp import FastMCP  # noqa: E402

def _style_rules():
    """The sibling style_rules module, however this file was loaded (script, package or by path)."""
    try:
        import style_rules as module
        if Path(module.__file__).resolve().parent == Path(__file__).resolve().parent:
            return module
    except ImportError:
        pass
    path = Path(__file__).resolve().with_name("style_rules.py")
    spec = importlib.util.spec_from_file_location("zt_style_rules", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zt_style_rules"] = module
    spec.loader.exec_module(module)
    return module


style_rules = _style_rules()

mcp = FastMCP("zt-style")

_PIT_SIM = None


def pit_sim():
    """The sibling pit_actor_sim server module: the project's PIT access layer."""
    global _PIT_SIM
    if _PIT_SIM is None:
        raw = os.environ.get("PIT_ACTOR_SIM_SERVER", "").strip()
        path = Path(raw) if raw else Path(__file__).resolve().parent.parent / "pit_actor_sim" / "server.py"
        spec = importlib.util.spec_from_file_location("zt_style_pit_actor_sim_server", path)
        module = importlib.util.module_from_spec(spec)
        folder = str(path.parent)
        added = folder not in sys.path
        if added:
            sys.path.insert(0, folder)
        try:
            spec.loader.exec_module(module)
        finally:
            if added:
                sys.path.remove(folder)
        _PIT_SIM = module
    return _PIT_SIM


def compute(ticker: str, asof: str) -> dict:
    """investor_style_scores without the MCP wrapper (tests and callers in-process)."""
    if not str(ticker or "").strip():
        raise ValueError("ticker is required")
    pit = pit_sim()
    return style_rules.score_issuer(
        ticker, asof, asof_utc=pit.asof_utc, pit_security=pit.pit_security,
        pit_price_history=pit.pit_price_history, issuer_annual_facts=pit.issuer_annual_facts)


@mcp.tool
def investor_style_scores(ticker: str, asof: str) -> dict:
    """Investor-style rule outcomes for one issuer as known at an explicit UTC instant.

    Args:
        ticker: The issuer's ticker as used in the warehouse's SEC XBRL series
            (``SEC:<TICKER>:<Concept>:<Unit>:FY``), e.g. NVDA.
        asof: UTC instant with an offset (e.g. 2026-08-28T12:00:00Z); only
            filings and closes known by then are read.

    Returns:
        Per family (graham, lynch, quality, cash_flow, leverage, inflection)
        each rule's value, status (PASS, PASS_LENIENT, FAIL, NOT_MEANINGFUL or
        UNKNOWN), rule text, input ids and missing concepts; the inputs with
        series ids, period ends, values and knowledge times; missing-data
        flags, warnings and a descriptive style label. Research only: no
        probabilities and no trade instructions.
    """
    return compute(ticker, asof)


if __name__ == "__main__":
    mcp.run()
