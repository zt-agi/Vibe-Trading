"""Optional: zt-research against the canonical project (read-only; about a minute).

Runs only with ZT_RESEARCH_REAL_PROJECT=1 and INVESTMENT_AI_PROJECT_ROOT set to
the canonical folder.  Scratch output goes to a pytest temporary folder (on E:
on PC1); the test asserts the project tree is unchanged afterwards.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zt_research_fixtures import *  # noqa: F403  (pytest fixtures)
from zt_research_testkit import tree_state

REAL_ROOT = os.environ.get("INVESTMENT_AI_PROJECT_ROOT", "")
pytestmark = pytest.mark.skipif(
    os.environ.get("ZT_RESEARCH_REAL_PROJECT") != "1" or not REAL_ROOT,
    reason="set ZT_RESEARCH_REAL_PROJECT=1 and INVESTMENT_AI_PROJECT_ROOT to run")


def _watched(root: Path) -> dict:
    state = {}
    for rel in ("implementation/alpha_signal_pit_ranker", "implementation/alpha_signal_monitor_v01",
                "implementation/pit_warehouse/lake/fact_price_eod",
                "implementation/pit_warehouse/lake/dim_security"):
        state[rel] = tree_state(root / rel)
    return state


def test_real_ranker_board_and_import_dry_run(server, tmp_path, monkeypatch):
    root = Path(REAL_ROOT)
    monkeypatch.setenv("INVESTMENT_AI_PROJECT_ROOT", str(root))
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "vt-home"))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "vt-home" / "hypotheses.json"))
    from src.config.accessor import reset_env_config
    reset_env_config()
    before = _watched(root)
    asof = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
    result = server.rank_pit_signals("rank01_market_prices", asof.isoformat().replace("+00:00", "Z"))
    assert result["status"] == "PASS", result.get("error")
    assert result["input"]["tickers"] > 10 and result["pit_cut"]["rows_kept"] > 1000
    assert result["summary"]["signals"] >= 1
    board = server.asm_promotion_board()
    assert board["data"]["count"] >= 1
    plan = server.import_promotion_board_to_vt()
    assert plan["applied"] is False and sum(plan["summary"].values()) >= board["data"]["count"]
    assert not (tmp_path / "vt-home" / "hypotheses.json").exists()
    assert _watched(root) == before
    reset_env_config()
