"""ZT add-on: the DSR/PBO/CPCV/FDR validation gate and the trial ledger.

The gate reads a variant family from the hash-chained trial ledger
(``backtest.variants``). Families here are built directly from return
matrices, except the end-to-end test, which drives ``runner.main``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.validation import run_validation
from backtest.variants import (
    VARIANTS_CONFIG_KEY,
    expand_param_grid,
    family_variants,
    ledger_trials,
    question_key,
)
from src.governance.ledger import LedgerCorruptionError

N_OBS = 756  # three years of daily bars


def _returns(matrix: np.ndarray) -> pd.DataFrame:
    index = pd.bdate_range("2023-01-02", periods=matrix.shape[0], name="trade_date")
    return pd.DataFrame(matrix, index=index,
                        columns=[f"v{i:03d}" for i in range(matrix.shape[1])])


def _family(tmp_path: Path, returns: pd.DataFrame, reported: int, *,
            codes=("SPY.US",), family: str | None = None, tag: str = "a") -> dict:
    ledger = tmp_path / "governance" / "trial_ledger.jsonl"
    written = ledger_trials(returns, [{"variant": i} for i in range(returns.shape[1])],
                            reported, ledger_path=ledger, codes=codes, family=family)
    path = tmp_path / f"variant_returns_{tag}.parquet"
    returns.to_parquet(path)
    return written.to_config(path)


def _equity(returns: pd.Series) -> pd.Series:
    """An equity curve whose pct_change reproduces ``returns`` exactly."""
    start = returns.index[0] - pd.tseries.offsets.BDay(1)
    levels = 1_000_000 * (1 + returns).cumprod()
    return pd.concat([pd.Series([1_000_000.0], index=[start]), levels])


def _gate(info: dict, equity: pd.Series, **checks) -> dict:
    config = {"validation": {"gate": True, **checks}, VARIANTS_CONFIG_KEY: info}
    return run_validation(config, equity, [], 1_000_000, 252)


def _noise(seed: int, n_variants: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, (N_OBS, n_variants))


def _planted(seed: int = 7, n_variants: int = 20) -> tuple[pd.DataFrame, int]:
    matrix = _noise(seed, n_variants)
    planted = n_variants - 1
    matrix[:, planted] = np.random.default_rng(seed + 1).normal(0.0025, 0.01, N_OBS)
    return _returns(matrix), planted


# ---------------------------------------------------------------------------
# The four required gate tests
# ---------------------------------------------------------------------------


def test_best_of_200_noise_strategies_fails_the_gate(tmp_path):
    returns = _returns(_noise(20260928, 200))
    reported = int(np.argmax(returns.mean() / returns.std()))  # the "discovered" winner
    info = _family(tmp_path, returns, reported)
    result = _gate(info, _equity(returns.iloc[:, reported]))

    assert result["overall"]["verdict"] == "FAIL"
    assert result["dsr"]["status"] == "FAIL"
    assert result["dsr"]["n_trials"] == 200
    assert result["dsr"]["deflated_sharpe_ratio"] < 0.95
    assert result["dsr"]["expected_max_sharpe_per_obs"] > 0
    assert result["fdr"]["status"] == "FAIL" and result["fdr"]["q_value"] > 0.10
    # The work bound is honoured and recorded rather than silently skipped.
    assert result["pbo"]["n_splits_requested"] == 16
    assert result["pbo"]["n_splits_used"] < 16 and result["pbo"]["budget_reduced"] is True
    assert result["cpcv"]["leakage"]["dirty_splits"] == 0


def test_planted_signal_passes_the_gate(tmp_path):
    returns, planted = _planted()
    info = _family(tmp_path, returns, planted)
    result = _gate(info, _equity(returns.iloc[:, planted]))

    assert result["overall"]["verdict"] == "PASS", result["overall"]
    assert result["dsr"]["deflated_sharpe_ratio"] >= 0.95
    assert result["pbo"]["pbo"] < 0.5
    assert result["cpcv"]["median_oos_sharpe_per_obs"] > 0
    assert result["cpcv"]["n_splits"] == 15 and result["cpcv"]["n_paths"] == 5
    assert result["fdr"]["q_value"] <= 0.10


def test_overlapping_labels_without_purging_fail_the_leakage_audit(tmp_path):
    returns, planted = _planted()
    info = _family(tmp_path, returns, planted)
    equity = _equity(returns.iloc[:, planted])

    leaky = _gate(info, equity, cpcv={"label_horizon": 5, "purge": False})
    assert leaky["cpcv"]["status"] == "FAIL"
    assert "boundary leakage" in leaky["cpcv"]["reason"]
    assert leaky["cpcv"]["leakage"]["dirty_splits"] > 0
    # Unpurged 5-bar labels leak both ways: training labels that run into a
    # test block, and training rows inside the true post-test embargo.
    examples = leaky["cpcv"]["leakage"]["examples"]
    assert any(e["overlapping"] > 0 for e in examples)
    assert any(e["embargo_violations"] > 0 for e in examples)
    assert leaky["overall"]["verdict"] == "FAIL"
    # The same labels, purged, audit clean and the planted signal still passes.
    purged = _gate(info, equity, cpcv={"label_horizon": 5, "purge": True})
    assert purged["cpcv"]["leakage"]["dirty_splits"] == 0
    assert purged["overall"]["verdict"] == "PASS"


def test_a_tampered_trial_ledger_blocks_validation(tmp_path):
    returns, planted = _planted()
    info = _family(tmp_path, returns, planted)
    equity = _equity(returns.iloc[:, planted])
    assert _gate(info, equity)["overall"]["verdict"] == "PASS"

    ledger = Path(info["ledger_path"])
    lines = ledger.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    record["sharpe_per_obs"] = 0.0  # quietly shrink one trial
    lines[2] = json.dumps(record)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    blocked = _gate(info, equity)
    assert blocked["overall"]["verdict"] == "FAIL"
    for check in ("dsr", "pbo", "cpcv", "fdr"):
        assert blocked[check]["status"] == "BLOCKED"
        assert "chain broken" in blocked[check]["reason"]
    # The ledger also refuses to grow on top of the tampered history.
    with pytest.raises(LedgerCorruptionError):
        _family(tmp_path, returns, planted, tag="b")


# ---------------------------------------------------------------------------
# Under-counting, tamper variants, and inconclusive cases
# ---------------------------------------------------------------------------


def test_trials_accumulate_per_universe_and_a_family_label_only_adds(tmp_path):
    first, _ = _planted(seed=1, n_variants=5)
    second, planted = _planted(seed=2, n_variants=7)
    _family(tmp_path, first, 0, tag="first")
    info = _family(tmp_path, second, planted, family="fresh-name", tag="second")
    equity = _equity(second.iloc[:, planted])
    fast = {"pbo": {"n_splits": 8}}
    assert _gate(info, equity, **fast)["dsr"]["n_trials"] == 12  # a new label does not reset it

    elsewhere, _ = _planted(seed=3, n_variants=3)
    _family(tmp_path, elsewhere, 0, codes=("QQQ.US",), family="fresh-name", tag="third")
    widened = _gate(info, equity, **fast)
    assert widened["dsr"]["n_trials"] == 15  # the declared family pulls those in too
    assert widened["fdr"]["n_hypotheses"] == 15
    assert question_key(["spy.us"]) == question_key(["SPY.US"])


def test_returns_that_differ_from_the_ledger_block(tmp_path):
    returns, planted = _planted()
    info = _family(tmp_path, returns, planted)
    swapped = returns.copy()
    swapped.iloc[:, [0, 1]] = swapped.iloc[:, [1, 0]].to_numpy()
    swapped.to_parquet(info["variant_returns_path"])
    result = _gate(info, _equity(returns.iloc[:, planted]))
    assert result["dsr"]["status"] == "BLOCKED"
    assert "differs from the ledgered trial" in result["dsr"]["reason"]


def test_trials_missing_from_the_ledger_block(tmp_path):
    returns, planted = _planted()
    info = _family(tmp_path, returns, planted)
    Path(info["ledger_path"]).unlink()
    result = _gate(info, _equity(returns.iloc[:, planted]))
    assert result["overall"]["verdict"] == "FAIL"
    assert "not in the trial ledger" in result["fdr"]["reason"]


def test_no_prepared_family_blocks_and_a_single_variant_is_inconclusive(tmp_path):
    returns, planted = _planted()
    equity = _equity(returns.iloc[:, planted])
    unprepared = run_validation({"validation": {"dsr": True}}, equity, [], 1_000_000, 252)
    assert unprepared["dsr"]["status"] == "BLOCKED"
    assert unprepared["overall"]["verdict"] == "FAIL"

    single = returns.iloc[:, [planted]]
    info = _family(tmp_path, single, 0, tag="single")
    result = _gate(info, equity)
    assert result["pbo"]["status"] == "INCONCLUSIVE"
    assert result["cpcv"]["selection"] == "single variant (no selection)"
    assert result["overall"]["verdict"] == "INCONCLUSIVE"


def test_unknown_gate_options_are_reported_not_ignored(tmp_path):
    returns, planted = _planted()
    info = _family(tmp_path, returns, planted)
    result = _gate(info, _equity(returns.iloc[:, planted]), pbo={"n_split": 8})
    assert result["pbo"]["status"] == "INCONCLUSIVE"
    assert "unknown option" in result["pbo"]["reason"]
    assert result["overall"]["verdict"] == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Variant expansion and the runner hook
# ---------------------------------------------------------------------------


def test_param_grid_expansion_and_the_reported_default():
    assert expand_param_grid({"b": [1, 2], "a": ["x"]}) == [{"a": "x", "b": 1}, {"a": "x", "b": 2}]
    assert expand_param_grid([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]
    with pytest.raises(ValueError):
        expand_param_grid({"a": []})

    class Engine:
        PARAM_GRID = {"window": [5, 20]}
        window = 10

        def generate(self, data_map):
            return {}

    variants, reported = family_variants(Engine)
    assert variants == [{"window": 10}, {"window": 5}, {"window": 20}] and reported == 0

    class Broken(Engine):
        PARAM_GRID = {"nope": [1]}

    with pytest.raises(ValueError, match="not attributes"):
        family_variants(Broken)


_SIGNAL_ENGINE = '''import pandas as pd


class SignalEngine:
    PARAM_GRID = {"window": [5, 10, 20]}
    window = 10

    def generate(self, data_map):
        out = {}
        for code, frame in data_map.items():
            average = frame["close"].rolling(self.window).mean()
            out[code] = (frame["close"] > average).astype(float) * 0.5
        return out
'''


def _bars(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, 320))
    index = pd.bdate_range("2024-01-02", periods=320, name="trade_date")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=index)


def test_runner_ledgers_the_family_and_accumulates_across_runs(monkeypatch, tmp_path):
    from backtest import runner
    from src.config.accessor import reset_env_config

    frames = {"SPY.US": _bars(1), "QQQ.US": _bars(2)}

    class _Offline:
        name = "yahoo"

        def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
            return {c: frames[c].copy() for c in codes if c in frames}

    runtime = tmp_path / "runtime"
    monkeypatch.setenv("VIBE_TRADING_HOME", str(runtime))
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setattr(runner, "_get_loader", lambda source: _Offline)
    reset_env_config()
    reports = []
    try:
        for name in ("first", "second"):
            run_dir = tmp_path / name
            (run_dir / "code").mkdir(parents=True)
            (run_dir / "code" / "signal_engine.py").write_text(_SIGNAL_ENGINE, encoding="utf-8")
            config = {"codes": ["SPY.US", "QQQ.US"], "start_date": "2024-01-02",
                      "end_date": "2025-03-31", "source": "yahoo", "initial_cash": 1_000_000,
                      "validation": {"gate": True, "pbo": {"n_splits": 8}},
                      # A smuggled family must never be trusted.
                      VARIANTS_CONFIG_KEY: {"status": "ok", "ledger_path": "/nonexistent"}}
            (run_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            runner.main(run_dir)
            reports.append(json.loads((run_dir / "artifacts" / "validation.json")
                                      .read_text(encoding="utf-8")))
    finally:
        reset_env_config()

    first, second = reports
    assert first["dsr"]["n_trials"] == 3 and second["dsr"]["n_trials"] == 6
    assert first["overall"]["verdict"] in {"PASS", "FAIL", "INCONCLUSIVE"}
    assert all(first[c]["status"] != "BLOCKED" for c in ("dsr", "pbo", "cpcv", "fdr"))
    ledger = runtime / "governance" / "trial_ledger.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 6 and sum(r["reported"] for r in records) == 2
    assert {r["params"]["window"] for r in records} == {5, 10, 20}
    card = json.loads((tmp_path / "second" / "run_card.json").read_text(encoding="utf-8"))
    assert any(a["path"] == "artifacts/variant_returns.parquet" for a in card["artifacts"])
    assert card["validation"]["overall"]["verdict"] == second["overall"]["verdict"]
