"""ZT add-on: relabelled validation checks and the stationary block bootstrap.

``monte_carlo`` became ``trade_order_permutation`` and ``walk_forward`` became
``sequential_windows_no_refit``; the old keys stay accepted and are echoed with
the same payload so existing ``validation.json`` readers keep working.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.validation import (
    bootstrap_sharpe_ci,
    run_validation,
    stationary_bootstrap_indices,
)


def _equity(returns: pd.Series) -> pd.Series:
    start = returns.index[0] - pd.tseries.offsets.BDay(1)
    levels = 1_000_000 * (1 + returns).cumprod()
    return pd.concat([pd.Series([1_000_000.0], index=[start]), levels])


# ---------------------------------------------------------------------------
# Relabelled descriptive checks and the stationary bootstrap
# ---------------------------------------------------------------------------


def _equity_and_trades():
    from backtest.models import TradeRecord

    equity = _equity(pd.Series(np.random.default_rng(3).normal(0.0005, 0.01, 300),
                               index=pd.bdate_range("2024-01-02", periods=300)))
    trades = [
        TradeRecord(symbol="SPY.US", direction=1, entry_price=1.0, exit_price=1.0,
                    entry_time=equity.index[i * 20], exit_time=equity.index[i * 20 + 5],
                    size=1.0, leverage=1.0, pnl=pnl, pnl_pct=pnl / 1e6,
                    exit_reason="signal", holding_bars=5.0, commission=0.0)
        for i, pnl in enumerate([100, -50, 200, -30, 150, -80, 120, -40, 90, -20])
    ]
    return equity, trades


def test_canonical_names_and_deprecated_aliases_carry_the_same_payload():
    equity, trades = _equity_and_trades()
    new = run_validation({"validation": {
        "trade_order_permutation": {"n_simulations": 50},
        "sequential_windows_no_refit": {"n_windows": 3}}}, equity, trades, 1e6, 252)
    old = run_validation({"validation": {
        "monte_carlo": {"n_simulations": 50},
        "walk_forward": {"n_windows": 3}}}, equity, trades, 1e6, 252)
    for result in (new, old):
        assert result["monte_carlo"] is result["trade_order_permutation"]
        assert result["walk_forward"] is result["sequential_windows_no_refit"]
        assert result["trade_order_permutation"]["test"] == "trade_order_permutation"
        assert "not edge" in result["trade_order_permutation"]["measures"]
    assert "deprecated_keys" not in new
    assert old["deprecated_keys"] == {"monte_carlo": "trade_order_permutation",
                                      "walk_forward": "sequential_windows_no_refit"}
    assert new["trade_order_permutation"]["actual_sharpe"] == old["monte_carlo"]["actual_sharpe"]


def test_stationary_block_bootstrap_option():
    equity, _ = _equity_and_trades()
    iid = bootstrap_sharpe_ci(equity, n_bootstrap=200, seed=1)
    block = bootstrap_sharpe_ci(equity, n_bootstrap=200, seed=1, block_len=10)
    again = bootstrap_sharpe_ci(equity, n_bootstrap=200, seed=1, block_len=10)
    assert iid["method"] == "iid" and "block_len" not in iid
    assert block["method"] == "stationary_block" and block["block_len"] == 10.0
    assert block == again
    assert block["ci_lower"] <= block["observed_sharpe"] <= block["ci_upper"]
    assert "error" in bootstrap_sharpe_ci(equity, block_len=0.5)
    assert "error" in bootstrap_sharpe_ci(equity, block_len=True)


def test_stationary_indices_form_wrapping_blocks():
    idx = stationary_bootstrap_indices(50, 8.0, np.random.default_rng(0))
    assert idx.shape == (50,) and idx.min() >= 0 and idx.max() < 50
    steps = np.diff(idx)
    continuing = (steps == 1) | (steps == -49)
    assert continuing.mean() > 0.6  # mean block length 8: most steps continue a block
