"""Statistical validation for backtest results.

Descriptive checks (each says what it measures -- ZT add-on relabel):
  - ``trade_order_permutation`` (was ``monte_carlo``): shuffles the order of the
    realised trade PnLs. It tests whether the PATH (sequencing, drawdown) is
    unusual for these trades, not whether the strategy has an edge: the sum of
    the PnLs never changes under a permutation.
  - ``bootstrap``: Sharpe confidence interval; iid by default, or a stationary
    block bootstrap (``block_len``) that keeps serial dependence.
  - ``sequential_windows_no_refit`` (was ``walk_forward``): the one equity curve
    cut into sequential windows. Nothing is refit per window, so it is a
    consistency check, not a walk-forward test.
The old keys stay accepted as deprecated aliases and are echoed in results.

Usage: called automatically by BaseEngine.run_backtest when config[\"validation\"]
is present, or invoked directly on backtest outputs.
"""

from __future__ import annotations

import json
import math
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from backtest.metrics import effective_bars_per_year
from backtest.models import TradeRecord

#: Deprecated validation keys -> the canonical names they alias (ZT add-on).
DEPRECATED_VALIDATION_KEYS = {
    "monte_carlo": "trade_order_permutation",
    "walk_forward": "sequential_windows_no_refit",
}


# ─── Trade-order permutation test (formerly "Monte Carlo") ───


def monte_carlo_test(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Deprecated alias of :func:`trade_order_permutation_test`.

    The name overstated the test: shuffling trade order tests the path, not
    the edge. Kept so existing callers and ``validation.json`` readers work.
    """
    return trade_order_permutation_test(*args, **kwargs)


def trade_order_permutation_test(
    trades: List[TradeRecord],
    initial_capital: float,
    n_simulations: int = 1000,
    seed: int = 42,
    bars_per_year: int = 252,
) -> Dict[str, Any]:
    """Shuffle trade PnL order to test path significance.

    Null hypothesis: the observed Sharpe / max-drawdown is no better than
    a random ordering of the same trades. The PnL total is invariant under
    the shuffle, so this measures sequencing (path, drawdown), not edge.

    Args:
        trades: Completed round-trip trades from backtest.
        initial_capital: Starting capital.
        n_simulations: Number of random permutations.
        seed: Random seed for reproducibility.
        bars_per_year: Annualisation factor (must match bootstrap_sharpe_ci
            and walk_forward_analysis so the report's Sharpe figures agree).

    Returns:
        Dict with actual_sharpe, p_value_sharpe, actual_max_dd,
        p_value_max_dd, simulated_sharpes (percentiles).
    """
    if isinstance(n_simulations, bool) or not isinstance(n_simulations, Integral) or n_simulations < 1:
        return {
            "error": f"n_simulations must be >= 1, got {n_simulations}",
            "p_value_sharpe": 1.0,
        }
    if isinstance(seed, bool) or not isinstance(seed, Integral) or seed < 0:
        return {"error": f"seed must be >= 0, got {seed}", "p_value_sharpe": 1.0}
    if len(trades) < 3:
        return {"error": "need at least 3 trades", "p_value_sharpe": 1.0}

    pnls = np.array([t.pnl for t in trades])
    actual = _path_metrics(pnls, initial_capital, bars_per_year)

    rng = np.random.default_rng(seed)
    sharpe_count = 0
    dd_count = 0
    sim_sharpes = []
    # The full path matrix feeds the fan-chart payload; skip it for
    # pathological sizes so a huge run cannot balloon memory or the JSON.
    keep_paths = n_simulations * len(pnls) <= 2_000_000
    sim_equities = np.empty((n_simulations, len(pnls))) if keep_paths else None

    for i in range(n_simulations):
        shuffled = rng.permutation(pnls)
        if sim_equities is not None:
            sim_equities[i] = initial_capital + np.cumsum(shuffled)
        sim = _path_metrics(shuffled, initial_capital, bars_per_year)
        sim_sharpes.append(sim["sharpe"])
        if sim["sharpe"] >= actual["sharpe"]:
            sharpe_count += 1
        if sim["max_dd"] >= actual["max_dd"]:  # less negative = "better"
            dd_count += 1

    sim_arr = np.array(sim_sharpes)
    result = {
        "test": "trade_order_permutation",
        "measures": "path ordering of the realised trades, not edge",
        "actual_sharpe": round(actual["sharpe"], 4),
        "actual_max_dd": round(actual["max_dd"], 4),
        "p_value_sharpe": round(sharpe_count / n_simulations, 4),
        "p_value_max_dd": round(dd_count / n_simulations, 4),
        "simulated_sharpe_mean": round(float(sim_arr.mean()), 4),
        "simulated_sharpe_std": round(float(sim_arr.std()), 4),
        "simulated_sharpe_p5": round(float(np.percentile(sim_arr, 5)), 4),
        "simulated_sharpe_p95": round(float(np.percentile(sim_arr, 95)), 4),
        "n_simulations": n_simulations,
        "n_trades": len(trades),
        "sharpe_samples": [round(float(s), 4) for s in sim_sharpes],
    }
    if sim_equities is not None:
        idx = np.unique(np.linspace(0, len(pnls) - 1, min(len(pnls), 400)).astype(int))
        sample_rows = np.unique(
            np.linspace(0, n_simulations - 1, min(30, n_simulations)).astype(int)
        )
        result["equity_paths"] = {
            "steps": (idx + 1).tolist(),
            "initial_capital": round(float(initial_capital), 2),
            "actual": np.round((initial_capital + np.cumsum(pnls))[idx], 2).tolist(),
            "band_p5": np.round(np.percentile(sim_equities[:, idx], 5, axis=0), 2).tolist(),
            "band_p25": np.round(np.percentile(sim_equities[:, idx], 25, axis=0), 2).tolist(),
            "band_p50": np.round(np.percentile(sim_equities[:, idx], 50, axis=0), 2).tolist(),
            "band_p75": np.round(np.percentile(sim_equities[:, idx], 75, axis=0), 2).tolist(),
            "band_p95": np.round(np.percentile(sim_equities[:, idx], 95, axis=0), 2).tolist(),
            "samples": np.round(sim_equities[np.ix_(sample_rows, idx)], 2).tolist(),
        }
    return result


def _path_metrics(
    pnls: np.ndarray, initial_capital: float, bars_per_year: int = 252
) -> Dict[str, float]:
    """Compute Sharpe and max drawdown from a PnL sequence."""
    equity = initial_capital + np.cumsum(pnls)
    if len(equity) > 1:
        prev = equity[:-1]
        diff = np.diff(equity)
        returns = np.where(prev != 0, diff / np.where(prev != 0, prev, 1.0), 0.0)
    else:
        returns = np.array([0.0])
    std = returns.std()
    sharpe = float(returns.mean() / (std + 1e-10) * np.sqrt(bars_per_year))
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.where(peak > 0, peak, 1.0)
    max_dd = float(dd.min())
    return {"sharpe": sharpe, "max_dd": max_dd}


# ─── Bootstrap Sharpe CI ───


def stationary_bootstrap_indices(
    n: int, mean_block_len: float, rng: np.random.Generator
) -> np.ndarray:
    """One Politis-Romano stationary-bootstrap resample of positions ``0..n-1``.

    Blocks start at uniform random positions, run with geometric lengths of
    mean ``mean_block_len`` and wrap around the sample end, so serial
    dependence inside a block survives the resample (an iid draw destroys it
    and understates the Sharpe's uncertainty for autocorrelated returns).
    """
    starts_new = rng.random(n) < 1.0 / mean_block_len
    starts_new[0] = True
    random_starts = rng.integers(0, n, size=n)
    block_id = np.cumsum(starts_new) - 1
    block_first = np.flatnonzero(starts_new)
    offsets = np.arange(n) - block_first[block_id]
    return (random_starts[block_first][block_id] + offsets) % n


def bootstrap_sharpe_ci(
    equity_curve: pd.Series,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    bars_per_year: int = 252,
    seed: int = 42,
    block_len: float | None = None,
) -> Dict[str, Any]:
    """Resample daily returns to estimate Sharpe confidence interval.

    Args:
        equity_curve: Equity time series.
        n_bootstrap: Number of bootstrap samples.
        confidence: Confidence level (e.g. 0.95 for 95% CI).
        bars_per_year: Annualisation factor.
        seed: Random seed.
        block_len: ``None`` (default) resamples iid, exactly as before; a
            number > 1 uses the stationary block bootstrap with that mean
            block length in bars (ZT add-on).

    Returns:
        Dict with observed_sharpe, ci_lower, ci_upper, median_sharpe,
        prob_positive (fraction of samples with Sharpe > 0), and ``method``.
    """
    if block_len is not None and (
        isinstance(block_len, bool)
        or not isinstance(block_len, Real)
        or not math.isfinite(float(block_len))
        or block_len < 1
    ):
        return {"error": f"block_len must be a number >= 1, got {block_len}"}
    stationary = block_len is not None and float(block_len) > 1.0
    if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, Integral) or n_bootstrap < 1:
        return {"error": f"n_bootstrap must be >= 1, got {n_bootstrap}"}
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, Real)
        or not math.isfinite(float(confidence))
        or not 0.0 < confidence < 1.0
    ):
        return {"error": f"confidence must be in (0, 1), got {confidence}"}
    if isinstance(seed, bool) or not isinstance(seed, Integral) or seed < 0:
        return {"error": f"seed must be >= 0, got {seed}"}

    returns = equity_curve.pct_change().replace([np.inf, -np.inf], 0.0).dropna().values
    if len(returns) < 5:
        return {"error": "need at least 5 return observations"}

    observed = _sharpe(returns, bars_per_year)

    rng = np.random.default_rng(seed)
    boot_sharpes = []
    for _ in range(n_bootstrap):
        if stationary:
            sample = returns[stationary_bootstrap_indices(len(returns), float(block_len), rng)]
        else:
            sample = rng.choice(returns, size=len(returns), replace=True)
        boot_sharpes.append(_sharpe(sample, bars_per_year))

    arr = np.array(boot_sharpes)
    alpha = (1 - confidence) / 2
    lower = float(np.percentile(arr, alpha * 100))
    upper = float(np.percentile(arr, (1 - alpha) * 100))
    prob_pos = float(np.mean(arr > 0))

    result = {
        "observed_sharpe": round(observed, 4),
        "ci_lower": round(lower, 4),
        "ci_upper": round(upper, 4),
        "median_sharpe": round(float(np.median(arr)), 4),
        "prob_positive": round(prob_pos, 4),
        "confidence": confidence,
        "n_bootstrap": n_bootstrap,
        "method": "stationary_block" if stationary else "iid",
    }
    if stationary:
        result["block_len"] = float(block_len)
    if n_bootstrap <= 20_000:
        result["sharpe_samples"] = [round(float(s), 4) for s in boot_sharpes]
    return result


def _sharpe(returns: np.ndarray, bars_per_year: int = 252) -> float:
    std = returns.std()
    return float(returns.mean() / (std + 1e-10) * np.sqrt(bars_per_year))


# ─── Sequential windows, no refit (formerly "Walk-Forward") ───


def walk_forward_analysis(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Deprecated alias of :func:`sequential_windows_no_refit`.

    Nothing is refit per window, so the old name promised a walk-forward
    test this never ran. Kept so existing callers and readers work.
    """
    return sequential_windows_no_refit(*args, **kwargs)


def sequential_windows_no_refit(
    equity_curve: pd.Series,
    trades: List[TradeRecord],
    n_windows: int = 5,
    bars_per_year: int = 252,
) -> Dict[str, Any]:
    """Split backtest into sequential windows, check consistency.

    Each window is evaluated independently (returns normalised to window start).
    The strategy is NOT refit per window: this is a consistency check of one
    fixed equity curve, not walk-forward optimisation.

    Args:
        equity_curve: Equity time series.
        trades: Completed trades.
        n_windows: Number of non-overlapping windows.
        bars_per_year: Annualisation factor.

    Returns:
        Dict with per_window stats, consistency metrics.
    """
    if isinstance(n_windows, bool) or not isinstance(n_windows, Integral) or n_windows < 1:
        return {"error": f"n_windows must be >= 1, got {n_windows}"}
    if len(equity_curve) < n_windows * 2:
        return {"error": f"need at least {n_windows * 2} bars for {n_windows} windows"}

    indices = equity_curve.index
    window_size = len(indices) // n_windows
    windows = []

    for i in range(n_windows):
        start_idx = i * window_size
        end_idx = (i + 1) * window_size if i < n_windows - 1 else len(indices)
        win_eq = equity_curve.iloc[start_idx:end_idx]
        win_start = indices[start_idx]
        win_end = indices[end_idx - 1]

        # Per-window trades
        win_trades = [t for t in trades if win_start <= t.entry_time <= win_end]

        # Per-window metrics
        ret = float(win_eq.iloc[-1] / win_eq.iloc[0] - 1) if win_eq.iloc[0] > 0 else 0.0
        win_returns = win_eq.pct_change().replace([np.inf, -np.inf], 0.0).dropna().values
        sharpe = _sharpe(win_returns, bars_per_year) if len(win_returns) > 1 else 0.0

        peak = win_eq.cummax()
        dd = (win_eq - peak) / peak.replace(0, 1)
        max_dd = float(dd.min())

        win_pnls = [t.pnl for t in win_trades]
        win_rate = len([p for p in win_pnls if p > 0]) / len(win_pnls) if win_pnls else 0.0

        windows.append(
            {
                "window": i + 1,
                "start": str(win_start.date()) if hasattr(win_start, "date") else str(win_start),
                "end": str(win_end.date()) if hasattr(win_end, "date") else str(win_end),
                "return": round(ret, 6),
                "sharpe": round(sharpe, 4),
                "max_dd": round(max_dd, 6),
                "trades": len(win_trades),
                "win_rate": round(win_rate, 4),
            }
        )

    # Consistency metrics
    returns_list = [w["return"] for w in windows]
    sharpes_list = [w["sharpe"] for w in windows]
    profitable_windows = sum(1 for r in returns_list if r > 0)

    return {
        "test": "sequential_windows_no_refit",
        "n_windows": n_windows,
        "windows": windows,
        "profitable_windows": profitable_windows,
        "consistency_rate": round(profitable_windows / n_windows, 4),
        "return_mean": round(float(np.mean(returns_list)), 6),
        "return_std": round(float(np.std(returns_list)), 6),
        "sharpe_mean": round(float(np.mean(sharpes_list)), 4),
        "sharpe_std": round(float(np.std(sharpes_list)), 4),
    }


# ─── Runner integration ───


def run_validation(
    config: Dict[str, Any],
    equity_curve: pd.Series,
    trades: List[TradeRecord],
    initial_capital: float,
    bars_per_year: int | None = 252,
) -> Dict[str, Any]:
    """Run configured validation checks.

    Reads from config["validation"]:
      - trade_order_permutation (deprecated alias: monte_carlo): {n_simulations, seed}
      - bootstrap: {n_bootstrap, confidence, seed, block_len}
      - sequential_windows_no_refit (deprecated alias: walk_forward): {n_windows}

    A permutation or window result is written under both its canonical key and
    the deprecated one, so existing ``validation.json`` readers keep working.

    Args:
        config: Backtest config (must contain "validation" key).
        equity_curve: Equity time series.
        trades: Completed trades.
        initial_capital: Starting capital.
        bars_per_year: Annualisation factor.

    Returns:
        Dict keyed by validation type with results.
    """
    v_cfg = config.get("validation", {})
    results: Dict[str, Any] = {}

    # Cross-market convention (runner.py passes bars_per_year=None): resolve
    # it through the shared span-derived factor — otherwise _sharpe's
    # np.sqrt(bars_per_year) raises TypeError for every validation-enabled
    # cross-market run.
    if bars_per_year is None:
        bars_per_year = effective_bars_per_year(equity_curve.index)

    mc_key = _requested_key(v_cfg, "trade_order_permutation")
    if mc_key is not None:
        mc_cfg = v_cfg[mc_key] if isinstance(v_cfg[mc_key], dict) else {}
        permutation = trade_order_permutation_test(
            trades,
            initial_capital,
            n_simulations=mc_cfg.get("n_simulations", 1000),
            seed=mc_cfg.get("seed", 42),
            bars_per_year=bars_per_year,
        )
        results["trade_order_permutation"] = permutation
        results["monte_carlo"] = permutation  # deprecated alias, same payload

    if "bootstrap" in v_cfg:
        bs_cfg = v_cfg["bootstrap"] if isinstance(v_cfg["bootstrap"], dict) else {}
        results["bootstrap"] = bootstrap_sharpe_ci(
            equity_curve,
            bars_per_year=bars_per_year,
            n_bootstrap=bs_cfg.get("n_bootstrap", 1000),
            confidence=bs_cfg.get("confidence", 0.95),
            seed=bs_cfg.get("seed", 42),
            block_len=bs_cfg.get("block_len"),
        )

    wf_key = _requested_key(v_cfg, "sequential_windows_no_refit")
    if wf_key is not None:
        wf_cfg = v_cfg[wf_key] if isinstance(v_cfg[wf_key], dict) else {}
        windows = sequential_windows_no_refit(
            equity_curve,
            trades,
            n_windows=wf_cfg.get("n_windows", 5),
            bars_per_year=bars_per_year,
        )
        results["sequential_windows_no_refit"] = windows
        results["walk_forward"] = windows  # deprecated alias, same payload

    used = sorted(k for k in DEPRECATED_VALIDATION_KEYS if isinstance(v_cfg, dict) and k in v_cfg)
    if used:
        results["deprecated_keys"] = {k: DEPRECATED_VALIDATION_KEYS[k] for k in used}

    return results


def _requested_key(v_cfg: Any, canonical: str) -> str | None:
    """The key a validation block uses for ``canonical``: itself or its deprecated alias."""
    if not isinstance(v_cfg, dict):
        return None
    if canonical in v_cfg:
        return canonical
    legacy = next((old for old, new in DEPRECATED_VALIDATION_KEYS.items() if new == canonical), None)
    return legacy if legacy in v_cfg else None


# ─── Standalone CLI ───


def _load_equity(run_dir: Path) -> pd.Series:
    """Load equity curve from artifacts/equity.csv."""
    path = run_dir / "artifacts" / "equity.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    for col in ("equity", "nav", "value"):
        if col in df.columns:
            return df[col]
    raise ValueError(
        f"equity.csv must contain an equity/nav/value column; got {list(df.columns)}"
    )


def _load_trades(run_dir: Path) -> List[TradeRecord]:
    """Load trades from artifacts/trades.csv and convert to TradeRecord list."""
    path = run_dir / "artifacts" / "trades.csv"
    df = pd.read_csv(path)
    if df.empty:
        return []

    # trades.csv has entry+exit row pairs; extract exit rows (they have pnl != 0)
    trades = []
    exit_rows = df[df["pnl"] != 0].reset_index(drop=True)
    for _, row in exit_rows.iterrows():
        hold = pd.to_numeric(row.get("holding_bars"), errors="coerce")
        if pd.isna(hold):
            hold = pd.to_numeric(row.get("holding_days", 0), errors="coerce")
        holding_bars = 0.0 if pd.isna(hold) else float(hold)
        trades.append(
            TradeRecord(
                symbol=str(row.get("code", "")),
                direction=1 if row.get("side") == "sell" else -1,
                entry_price=0.0,
                exit_price=float(row.get("price", 0)),
                entry_time=pd.Timestamp(row.get("timestamp", "2000-01-01")),
                exit_time=pd.Timestamp(row.get("timestamp", "2000-01-01")),
                size=float(row.get("qty", 0)),
                leverage=1.0,
                pnl=float(row.get("pnl", 0)),
                pnl_pct=float(row.get("return_pct", 0)),
                exit_reason=str(row.get("reason", "signal")),
                holding_bars=holding_bars,
                commission=0.0,
            )
        )
    return trades


def _parse_run_dir(argv: List[str]) -> Path:
    """Validate CLI input and return a usable run directory path."""
    if len(argv) < 2:
        raise SystemExit("Usage: python -m backtest.validation <run_dir>")

    raw_run_dir = argv[1]
    if not raw_run_dir.strip():
        raise SystemExit("run_dir must be a non-empty path")
    if "\0" in raw_run_dir:
        raise SystemExit("Invalid run_dir path: embedded NUL byte")

    try:
        run_dir = Path(raw_run_dir).expanduser()
        exists = run_dir.exists()
        is_dir = run_dir.is_dir() if exists else False
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Invalid run_dir path: {exc}") from exc

    if not exists:
        raise SystemExit(f"run_dir does not exist: {run_dir}")
    if not is_dir:
        raise SystemExit(f"run_dir is not a directory: {run_dir}")
    return run_dir


def _json_safe(value: Any) -> Any:
    """Return a JSON-strict copy of validation results."""
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_validation_json(path: Path, results: Dict[str, Any]) -> Dict[str, Any]:
    """Write validation results to ``path`` as strict, RFC-8259 JSON.

    A validation metric can be non-finite (e.g. a Sharpe computed from a path
    whose equity touches zero), and ``json.dumps`` emits bare ``NaN`` /
    ``Infinity`` tokens for those by default (``allow_nan=True``) — tokens that
    strict parsers reject. Sanitize with :func:`_json_safe` (non-finite → null)
    and serialize with ``allow_nan=False`` so every writer of
    ``artifacts/validation.json`` produces the same valid JSON. Returns the
    sanitized payload that was written.
    """
    safe_results = _json_safe(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(safe_results, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return safe_results


def main(run_dir: Path) -> Dict[str, Any]:
    """Run all three validations on existing backtest artifacts.

    Reads equity.csv, trades.csv, and config.json from run_dir.

    Args:
        run_dir: Directory with artifacts/ subdirectory.

    Returns:
        Validation results dict.
    """
    # Load config for initial_cash
    config_path = run_dir / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {}
    initial_capital = config.get("initial_cash", 1_000_000)

    equity = _load_equity(run_dir)
    trades = _load_trades(run_dir)

    permutation = monte_carlo_test(trades, initial_capital)
    windows = walk_forward_analysis(equity, trades)
    results = {
        "trade_order_permutation": permutation,
        "monte_carlo": permutation,  # deprecated alias, same payload
        "bootstrap": bootstrap_sharpe_ci(equity),
        "sequential_windows_no_refit": windows,
        "walk_forward": windows,  # deprecated alias, same payload
    }

    out = run_dir / "artifacts" / "validation.json"
    safe_results = write_validation_json(out, results)

    print(json.dumps(safe_results, indent=2, allow_nan=False))
    return safe_results


if __name__ == "__main__":
    import sys

    main(_parse_run_dir(sys.argv))
