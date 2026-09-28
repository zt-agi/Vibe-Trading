"""ZT add-on: read-only Vibe-Trading tools over ZT's research dashboards.

A stdio FastMCP server, registered in <VIBE_TRADING_HOME>/agent.json like the
pit-actor-sim extension. It reads the canonical project folder named by
INVESTMENT_AI_PROJECT_ROOT and never writes, fetches or trades. Every response
carries as_of (UTC), source_path, sha256, pit_label (NON_PIT: nothing here goes
through pitdb's as-of macros) and an honest status: FRESH, STALE or MISSING.
Portfolio exports are reduced to coverage and status; account numbers,
quantities and values are never returned.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastmcp import FastMCP

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import zt_core as core  # noqa: E402

mcp = FastMCP("zt-dashboards")

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
             "openWorldHint": False}


def daily_snapshot(date: str = "latest") -> dict:
    """Read one dated Alpha Monitor export folder (implementation/exports/<date>).

    date is 'latest' (default), 'today' (America/New_York) or YYYY-MM-DD.
    Returns alerts, source health, market and macro context, event guardrails,
    signal readiness, broker connectivity status and portfolio coverage/status
    only (never account numbers, quantities or values), plus the latest daily
    update run. Monitoring context, not an investment signal. A missing folder
    is reported as MISSING with the latest available date.
    """
    return core.daily_snapshot(date)


def source_families() -> dict:
    """Top-25 PIT-aware source families from the backfill coverage ledger.

    Also returns the pitdb catalog grouped by family (DATA_SOURCE_LEDGER.csv)
    and the Alpha Monitor source registry grouped by source type, each with its
    own provenance and status. A missing coverage ledger is reported as MISSING.
    """
    return core.source_families()


def signal_state() -> dict:
    """ASM phase-1 state read: one row per monitored signal (value, z, state,
    freshness, PIT class, falsifier). Signals are monitored, not admitted."""
    return core.signal_state()


def lane_status() -> dict:
    """ASM phase-2 lane board: per-lane freshness, collector status, schedule
    and B3 gate progress."""
    return core.lane_status()


def test_ledger() -> dict:
    """ASM multiple-testing ledger: cumulative test count and the admission
    hurdle max(3.0, 2.57 + 0.5 * log10(n)) on the primary pre-registered test."""
    return core.test_ledger()


def promotion_board() -> dict:
    """ASM phase-3 promotion board: eligibility by history length and the
    eight-gate battery per candidate, with recorded verdicts."""
    return core.promotion_board()


def pit_inventory() -> dict:
    """pitdb state: ledger summaries (sources, files, ingest, papers, inventory),
    the latest daily run with its 10 integrity checks and OVERALL result, and
    the E: index/audit receipts when VIBE_TRADING_HOME is set."""
    return core.pit_inventory()


def world_model() -> dict:
    """Machine-readable world-model state when an export exists; otherwise an
    honest NOT_AVAILABLE with the human-readable references. Never re-estimate
    or alter any probability it contains."""
    return core.world_model()


def project_reports() -> dict:
    """PROJECT_HUB.json items and root dashboards: title, path, existence,
    size, modification time and sha256; these are the ids the VT Web UI ZT
    page can display."""
    return core.project_reports()


TOOLS = (daily_snapshot, source_families, signal_state, lane_status, test_ledger,
         promotion_board, pit_inventory, world_model, project_reports)
TOOL_NAMES = tuple(tool.__name__ for tool in TOOLS)

for _tool in TOOLS:
    mcp.tool(_tool, annotations=READ_ONLY)


if __name__ == "__main__":
    mcp.run()
