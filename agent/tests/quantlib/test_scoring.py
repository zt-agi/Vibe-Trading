"""Tests for src.quantlib.scoring (ZT add-on).

The load-bearing properties: every score has its textbook value on a case that
can be worked by hand, incoherent forecasts are refused rather than scored, the
quantile-grid CRPS is exact for the distribution the grid implies, and PIT uses
the same distribution as CRPS.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from scipy.stats import norm

from src.quantlib import scoring
from src.quantlib.scoring import (
    brier_score,
    brier_score_multiclass,
    brier_skill_score,
    crps_from_quantiles,
    crps_normal,
    interval_coverage,
    interval_score,
    log_loss,
    log_loss_multiclass,
    mean_pinball_loss,
    pinball_loss,
    pit_histogram,
    pit_values,
    skill_score,
    tail_exceedance_rates,
    wilson_interval,
)

UNIFORM_LEVELS = [0.1, 0.5, 0.9]
UNIFORM_QUANTILES = [0.1, 0.5, 0.9]  # U(0, 1) read with linear tails
Q01_Q99 = [round(k / 100, 2) for k in range(1, 100)]


def uniform_crps(y: float) -> float:
    """Closed-form CRPS of U(0, 1)."""
    if y <= 0.0:
        return 1.0 / 3.0 + (0.0 - y)
    if y >= 1.0:
        return 1.0 / 3.0 + (y - 1.0)
    return (y**3 + (1.0 - y) ** 3) / 3.0


# --- binary and categorical scores -----------------------------------------


def test_brier_score_hand_value():
    assert brier_score([0.8, 0.3], [1, 0]) == pytest.approx((0.04 + 0.09) / 2)
    assert brier_score([0.5], [True]) == pytest.approx(0.25)


@pytest.mark.parametrize(
    "forecasts, outcomes",
    [([1.2], [1]), ([-0.1], [0]), ([0.5], [2]), ([0.5, 0.5], [1]), ([], []), ([float("nan")], [1])],
)
def test_brier_score_refuses_incoherent_inputs(forecasts, outcomes):
    with pytest.raises(ValueError):
        brier_score(forecasts, outcomes)


def test_multiclass_brier_matches_the_calibration_ledger_formula():
    # market_actor_sim/sim/calibration.py: sum_o (p_o - 1[o == realized])^2.
    dist = {"oil_down": 0.133365, "oil_range": 0.5765893333333333,
            "oil_spike_moderate": 0.26534430666666664, "oil_spike_severe": 0.024701360000000002}
    legacy = sum((p - (1.0 if o == "oil_range" else 0.0)) ** 2 for o, p in dist.items())
    assert brier_score_multiclass([dist], ["oil_range"]) == pytest.approx(legacy, abs=1e-15)
    assert brier_score_multiclass([{"a": 0.7, "b": 0.2, "c": 0.1}], ["a"]) == pytest.approx(0.14)


def test_multiclass_refuses_non_exhaustive_or_non_normalised_forecasts():
    with pytest.raises(ValueError, match="sum"):
        brier_score_multiclass([{"a": 0.5, "b": 0.3}], ["a"])
    with pytest.raises(ValueError, match="exhaustive"):
        brier_score_multiclass([{"a": 0.5, "b": 0.5}], ["c"])
    with pytest.raises(ValueError):
        brier_score_multiclass({"a": 1.0}, ["a"])


def test_log_loss_values_and_clipping():
    assert log_loss([0.8], [1]) == pytest.approx(-math.log(0.8))
    assert log_loss([0.8, 0.3], [0, 0]) == pytest.approx(-(math.log(0.2) + math.log(0.7)) / 2)
    # A confident miss is clipped by default, infinite when clipping is off.
    assert log_loss([0.0], [1]) == pytest.approx(-math.log(1e-12))
    assert math.isinf(log_loss([0.0], [1], eps=0.0))
    assert log_loss_multiclass([{"a": 0.25, "b": 0.75}], ["b"]) == pytest.approx(-math.log(0.75))
    assert math.isinf(log_loss_multiclass([{"a": 0.0, "b": 1.0}], ["a"], eps=0.0))


def test_skill_scores():
    assert skill_score(0.1, 0.2) == pytest.approx(0.5)
    assert skill_score(0.3, 0.2) == pytest.approx(-0.5)
    with pytest.raises(ValueError, match="undefined"):
        skill_score(0.1, 0.0)
    forecasts, outcomes = [0.9, 0.2, 0.7, 0.1], [1, 0, 1, 0]
    bs = brier_score(forecasts, outcomes)
    assert brier_skill_score(forecasts, outcomes, 0.5) == pytest.approx(1 - bs / 0.25)
    per_event = [0.6, 0.4, 0.5, 0.3]
    expected = 1 - bs / brier_score(per_event, outcomes)
    assert brier_skill_score(forecasts, outcomes, per_event) == pytest.approx(expected)
    with pytest.raises(ValueError):
        brier_skill_score(forecasts, outcomes, [0.5, 0.5])


def test_wilson_interval_reproduces_the_actor_simulator():
    # pilot_result.json (2026-08-28 Iran oil pilot) reports this half-width
    # for oil_down, computed with z = 1.96 at 300k rollouts.
    w = wilson_interval(0.13385333333333332, 300000, z=1.96)
    assert w.halfwidth == pytest.approx(0.0012184457383627872, abs=1e-15)
    exact = wilson_interval(0.13385333333333332, 300000)
    assert abs(exact.halfwidth - w.halfwidth) < 1e-7
    assert exact.lower < exact.estimate < exact.upper
    assert wilson_interval(0.0, 10).lower == 0.0
    assert wilson_interval(1.0, 10).upper == 1.0
    with pytest.raises(ValueError):
        wilson_interval(0.5, 0)


# --- quantile forecasts -----------------------------------------------------


def test_pinball_loss_hand_values():
    assert pinball_loss(10.0, 8.0, 0.9) == pytest.approx(1.8)
    assert pinball_loss(6.0, 8.0, 0.9) == pytest.approx(0.2)
    assert pinball_loss(8.0, 8.0, 0.3) == 0.0
    grid = mean_pinball_loss([0.25, 0.75], [1.0, 3.0], 2.0)
    assert grid == pytest.approx((0.25 * 1.0 + 0.25 * 1.0) / 2)
    with pytest.raises(ValueError):
        pinball_loss(1.0, 1.0, 1.0)


@pytest.mark.parametrize("y", [0.5, 0.3, 0.0, 1.0, 1.5, -0.25])
def test_crps_is_exact_for_the_grid_implied_distribution(y):
    # Linear interpolation plus linear tails of this grid IS U(0, 1).
    assert crps_from_quantiles(UNIFORM_LEVELS, UNIFORM_QUANTILES, y) == pytest.approx(
        uniform_crps(y), abs=1e-14
    )


def test_crps_of_a_point_forecast_is_absolute_error():
    assert crps_from_quantiles([0.1, 0.5, 0.9], [2.0, 2.0, 2.0], 3.5, tails="flat") == pytest.approx(1.5)
    assert crps_from_quantiles([0.1, 0.5, 0.9], [2.0, 2.0, 2.0], 0.5, tails="linear") == pytest.approx(1.5)


@pytest.mark.parametrize("y", [0.0, 0.7, -2.3, 4.0])
def test_crps_from_quantiles_converges_to_the_normal_closed_form(y):
    dense = [k / 1000 for k in range(1, 1000)]
    assert crps_from_quantiles(dense, norm.ppf(dense), y) == pytest.approx(crps_normal(0.0, 1.0, y), abs=5e-4)
    # The ledger's q01..q99 grid is within a few thousandths.
    assert crps_from_quantiles(Q01_Q99, norm.ppf(Q01_Q99), y) == pytest.approx(crps_normal(0.0, 1.0, y), abs=5e-3)


def test_crps_averages_over_forecasts_and_rewards_sharpness():
    sharp = norm.ppf(Q01_Q99, loc=0.0, scale=0.5)
    wide = norm.ppf(Q01_Q99, loc=0.0, scale=2.0)
    both = crps_from_quantiles(Q01_Q99, np.vstack([sharp, wide]), [0.1, 0.1])
    assert both == pytest.approx(
        (crps_from_quantiles(Q01_Q99, sharp, 0.1) + crps_from_quantiles(Q01_Q99, wide, 0.1)) / 2
    )
    assert crps_from_quantiles(Q01_Q99, sharp, 0.1) < crps_from_quantiles(Q01_Q99, wide, 0.1)


def test_quantile_inputs_are_validated():
    with pytest.raises(ValueError, match="increasing"):
        crps_from_quantiles([0.5, 0.1], [1.0, 2.0], 1.0)
    with pytest.raises(ValueError, match="non-decreasing"):
        crps_from_quantiles([0.1, 0.9], [2.0, 1.0], 1.0)
    with pytest.raises(ValueError, match="inside"):
        crps_from_quantiles([0.0, 0.9], [1.0, 2.0], 1.0)
    with pytest.raises(ValueError, match="columns"):
        crps_from_quantiles([0.1, 0.9], [[1.0, 2.0, 3.0]], [1.0])
    with pytest.raises(ValueError, match="realized"):
        pit_values(UNIFORM_LEVELS, UNIFORM_QUANTILES, [0.3, 0.4])
    with pytest.raises(ValueError, match="tails"):
        crps_from_quantiles(UNIFORM_LEVELS, UNIFORM_QUANTILES, 0.3, tails="normal")


def test_pit_uses_the_same_interpolated_distribution():
    assert pit_values(UNIFORM_LEVELS, UNIFORM_QUANTILES, 0.3) == pytest.approx(0.3)
    assert pit_values(UNIFORM_LEVELS, UNIFORM_QUANTILES, 0.05) == pytest.approx(0.05)
    assert pit_values(UNIFORM_LEVELS, UNIFORM_QUANTILES, 1.2) == 1.0
    assert pit_values(UNIFORM_LEVELS, UNIFORM_QUANTILES, -1.0) == 0.0
    ys = [0.0, 1.0, -1.5]
    grid = np.vstack([norm.ppf(Q01_Q99)] * 3)
    assert pit_values(Q01_Q99, grid, ys) == pytest.approx(norm.cdf(ys), abs=1e-3)


def test_pit_at_a_point_mass_is_the_mid_point_of_the_jump():
    # Flat tails put 0.1 of mass on each outer quantile.
    assert pit_values([0.1, 0.9], [1.0, 2.0], 1.0, tails="flat") == pytest.approx(0.05)
    assert pit_values([0.1, 0.9], [1.0, 2.0], 2.0, tails="flat") == pytest.approx(0.95)
    # An interior tie is an atom from 0.25 to 0.75.
    assert pit_values([0.25, 0.75], [5.0, 5.0], 5.0, tails="flat") == pytest.approx(0.5)


def test_pit_histogram_detects_miscalibration():
    flat = pit_histogram([(i + 0.5) / 100 for i in range(100)], bins=10)
    assert flat.counts == [10] * 10
    assert flat.chi2_statistic == pytest.approx(0.0)
    assert flat.chi2_p_value == pytest.approx(1.0)
    assert flat.chi2_valid is True
    overconfident = pit_histogram([0.01] * 40 + [0.99] * 40 + [0.5] * 20, bins=10)
    assert overconfident.chi2_p_value < 1e-6
    assert pit_histogram([1.0], bins=4).counts == [0, 0, 0, 1]
    with pytest.raises(ValueError):
        pit_histogram([1.2])


def test_interval_coverage_and_score():
    result = interval_coverage([0, 0, 0, 0], [1, 1, 1, 1], [0.5, 1.0, -0.1, 2.0], nominal=0.5)
    assert (result.hits, result.below, result.above) == (2, 1, 1)
    assert result.coverage == pytest.approx(0.5)
    assert result.binomial_p_value == pytest.approx(1.0)
    # Width 2 plus (2 / 0.1) * 0.5 for the one miss, averaged with a hit.
    assert interval_score([0, 0], [2, 2], [1.0, 2.5], alpha=0.1) == pytest.approx((2 + (2 + 10.0)) / 2)
    with pytest.raises(ValueError):
        interval_coverage([1], [0], [0.5])


def test_tail_exceedance_rates_on_calibrated_and_miscalibrated_forecasts():
    rng = np.random.default_rng(20260928)
    n = 20000
    y = rng.standard_normal(n)
    grid = np.tile(norm.ppf(Q01_Q99), (n, 1))
    calibrated = tail_exceedance_rates(Q01_Q99, grid, y)
    assert [t.level for t in calibrated] == [0.01, 0.05, 0.95, 0.99]
    assert [t.side for t in calibrated] == ["lower", "lower", "upper", "upper"]
    for tail in calibrated:
        assert tail.observed_rate == pytest.approx(tail.expected_rate, abs=0.01)
        assert tail.binomial_p_value > 1e-4
    too_narrow = tail_exceedance_rates(Q01_Q99, grid * 0.5, y, tail_levels=[0.05])
    assert too_narrow[0].observed_rate > 0.15
    assert too_narrow[0].binomial_p_value < 1e-10
    with pytest.raises(ValueError, match="not on the quantile grid"):
        tail_exceedance_rates(Q01_Q99, grid[:5], y[:5], tail_levels=[0.025])


# --- reachable through quantlib_call ----------------------------------------


def test_scoring_is_reachable_through_the_quantlib_tool():
    from src.tools.quantlib_tool import ALLOWED_MODULES, QuantlibCallTool

    assert ALLOWED_MODULES["scoring"] == "src.quantlib.scoring"
    tool = QuantlibCallTool()
    listed = json.loads(tool.execute(action="list", module="scoring"))
    names = {f["name"] for f in listed["functions"]}
    assert {"brier_score", "crps_from_quantiles", "pit_values", "wilson_interval"} <= names
    called = json.loads(tool.execute(action="call", module="scoring", function="brier_score",
                                     kwargs={"forecasts": [0.8, 0.3], "outcomes": [1, 0]}))
    assert called["ok"] is True and called["result"] == pytest.approx(0.065)
    wilson = json.loads(tool.execute(action="call", module="scoring", function="wilson_interval",
                                     kwargs={"estimate": 0.5, "n": 100}))
    assert wilson["ok"] is True and wilson["result"]["lower"] < 0.5 < wilson["result"]["upper"]


def test_public_surface_is_exported():
    for name in scoring.__all__:
        assert hasattr(scoring, name)
