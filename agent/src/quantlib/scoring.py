"""Proper scoring rules and calibration diagnostics for probabilistic forecasts.

ZT add-on: this module is the forecast-verification half of the research
stack's forecast ledger (``extensions/pit_actor_sim/forecast_ledger.py``). The
ledger records a forecast before its outcome is knowable; this module turns a
forecast plus its realised outcome into numbers that cannot be gamed by hedging.

Three forecast shapes, one family of functions each:

Binary probability ``p`` for an event that happens (1) or not (0)
    :func:`brier_score`, :func:`log_loss`, :func:`brier_skill_score`.

Categorical distribution over a mutually exclusive, collectively exhaustive
category set (for example an actor simulation's terminal outcome bins)
    :func:`brier_score_multiclass`, :func:`log_loss_multiclass`.

Quantile forecast: values ``q`` at probability levels ``tau`` (for example the
99-point grid q01..q99 of a price at a horizon)
    :func:`mean_pinball_loss`, :func:`crps_from_quantiles`, :func:`pit_values`,
    :func:`pit_histogram`, :func:`interval_coverage`, :func:`interval_score`,
    :func:`tail_exceedance_rates`.

Every score here is **negatively oriented: lower is better**, and 0 is perfect.
:func:`skill_score` converts a score and a reference score into the usual
"fraction of the reference's error removed" (1 perfect, 0 no better than the
reference, negative worse), which is what the ledger reports as incremental
skill over a predeclared baseline.

WHICH DISTRIBUTION A QUANTILE GRID MEANS
----------------------------------------
A finite grid does not define a distribution by itself, and CRPS and PIT both
need one. This module uses a single, documented reading for both, so the two
can never disagree about what was forecast:

* between grid points the quantile function is **linear** in ``tau``;
* beyond the outermost levels it is either extended with the slope of the end
  segment (``tails="linear"``, the default: a continuous distribution with
  bounded support) or held flat (``tails="flat"``: the probability outside the
  grid sits as a point mass on the outermost quantile).

Equal adjacent quantile values are allowed and mean a point mass. PIT at a
point mass is the mid-point of the jump (the deterministic "mid-PIT"), so a
calibrated forecast with atoms still has mean PIT 0.5.

CRPS is then computed **exactly** for that distribution through the quantile
decomposition ``CRPS(F, y) = 2 * integral_0^1 pinball_tau(y, F^-1(tau)) dtau``:
the integrand is piecewise quadratic, so Simpson's rule on each piece (split
where the quantile function crosses ``y``) is exact, not an approximation.

WILSON INTERVAL
---------------
:func:`wilson_interval` is here because a Monte Carlo outcome frequency is the
most common probability this stack records, and the ledger stores it with its
sampling interval. It is an interval for a *frequency estimate*, not a
calibration statement about the model that produced the frequency.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.stats import binomtest, chi2, norm

__all__ = [
    "WilsonInterval",
    "PitHistogram",
    "CoverageResult",
    "TailExceedance",
    "wilson_interval",
    "brier_score",
    "brier_score_multiclass",
    "log_loss",
    "log_loss_multiclass",
    "skill_score",
    "brier_skill_score",
    "pinball_loss",
    "mean_pinball_loss",
    "crps_from_quantiles",
    "crps_normal",
    "pit_values",
    "pit_histogram",
    "interval_coverage",
    "interval_score",
    "tail_exceedance_rates",
]

#: Tolerance on a categorical distribution summing to one. Monte Carlo
#: frequencies sum to exactly 1 and exact tree folds to within a few ulps; a
#: larger gap means the category set is not exhaustive or not exclusive.
SUM_TO_ONE_TOLERANCE: float = 1e-6

#: Default probability clip for the log loss. It matches the calibration
#: ledger this module replaces (market_actor_sim/sim/calibration.py), so scores
#: migrated from it reproduce. Pass ``eps=0`` for the unclipped proper score,
#: which is infinite when a realised outcome was given probability zero.
DEFAULT_LOG_EPS: float = 1e-12

_TAIL_MODES = ("linear", "flat")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WilsonInterval:
    """Wilson score interval for a binomial frequency.

    Attributes:
        estimate: The observed frequency the interval is built around.
        n: Number of trials (Monte Carlo rollouts).
        confidence: Two-sided confidence level.
        center: Wilson centre ``(p + z^2/2n) / (1 + z^2/n)``; not ``estimate``.
        halfwidth: Half the interval width.
        lower: ``max(0, center - halfwidth)``.
        upper: ``min(1, center + halfwidth)``.
    """

    estimate: float
    n: int
    confidence: float
    center: float
    halfwidth: float
    lower: float
    upper: float


@dataclass(frozen=True)
class PitHistogram:
    """Histogram of PIT values against the uniform a calibrated forecast gives.

    Attributes:
        n: Number of PIT values.
        edges: ``bins + 1`` bin edges on [0, 1].
        counts: Values per bin; the last bin is closed on the right.
        frequencies: ``counts / n``.
        expected_frequency: ``1 / bins``.
        chi2_statistic: Pearson statistic against the uniform.
        chi2_p_value: Upper-tail probability with ``bins - 1`` degrees of freedom.
        chi2_valid: ``False`` when an expected count is below 5, where the
            chi-square approximation is not to be trusted.
    """

    n: int
    edges: list[float]
    counts: list[int]
    frequencies: list[float]
    expected_frequency: float
    chi2_statistic: float
    chi2_p_value: float
    chi2_valid: bool


@dataclass(frozen=True)
class CoverageResult:
    """Empirical coverage of prediction intervals.

    Attributes:
        n: Number of intervals.
        hits: Outcomes inside ``[lower, upper]`` (both ends inclusive).
        below: Outcomes below ``lower``.
        above: Outcomes above ``upper``.
        coverage: ``hits / n``.
        nominal: Claimed coverage, when supplied.
        binomial_p_value: Two-sided exact binomial p-value of ``hits`` against
            ``nominal``; ``None`` without a nominal level.
    """

    n: int
    hits: int
    below: int
    above: int
    coverage: float
    nominal: float | None
    binomial_p_value: float | None


@dataclass(frozen=True)
class TailExceedance:
    """How often outcomes fell beyond one tail quantile.

    Attributes:
        level: Quantile level ``tau`` of the threshold.
        side: ``"lower"`` (outcome below ``q_tau``) for ``tau < 0.5``,
            ``"upper"`` (outcome above ``q_tau``) otherwise.
        n: Number of forecasts.
        exceedances: Outcomes strictly beyond the threshold.
        expected_rate: ``tau`` for the lower side, ``1 - tau`` for the upper.
        observed_rate: ``exceedances / n``.
        binomial_p_value: Two-sided exact binomial p-value.
    """

    level: float
    side: str
    n: int
    exceedances: int
    expected_rate: float
    observed_rate: float
    binomial_p_value: float


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _as_float_array(values, name: str, ndim: int | None = None) -> np.ndarray:
    """Convert to a float array and refuse non-finite entries."""
    array = np.asarray(values, dtype=float)
    if ndim is not None and array.ndim != ndim:
        raise ValueError(f"{name} must be {ndim}-dimensional, got shape {array.shape}")
    if array.size == 0:
        raise ValueError(f"{name} must not be empty")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite")
    return array


def _probabilities(values, name: str = "forecasts") -> np.ndarray:
    array = _as_float_array(values, name, ndim=1)
    if np.any(array < 0.0) or np.any(array > 1.0):
        raise ValueError(f"{name} must lie in [0, 1]")
    return array


def _binary_outcomes(values, n: int) -> np.ndarray:
    array = np.asarray(values)
    if array.ndim != 1 or array.shape[0] != n:
        raise ValueError(f"outcomes must be a 1-D sequence of length {n}")
    as_float = array.astype(float)
    if not np.all((as_float == 0.0) | (as_float == 1.0)):
        raise ValueError("binary outcomes must be 0 or 1 (or booleans)")
    return as_float


def _categorical(forecasts: Sequence[Mapping[str, float]], outcomes: Sequence[str]) -> list[tuple[dict, str]]:
    if isinstance(forecasts, Mapping):
        raise ValueError("forecasts must be a sequence of {category: probability} mappings")
    if isinstance(outcomes, str):
        raise ValueError("outcomes must be a sequence of realised category labels")
    if len(forecasts) == 0 or len(forecasts) != len(outcomes):
        raise ValueError("forecasts and outcomes must be non-empty and the same length")
    pairs = []
    for index, (forecast, realised) in enumerate(zip(forecasts, outcomes)):
        if not isinstance(forecast, Mapping) or not forecast:
            raise ValueError(f"forecast {index} must be a non-empty mapping")
        probabilities = {str(k): float(v) for k, v in forecast.items()}
        values = np.array(list(probabilities.values()))
        if not np.all(np.isfinite(values)) or np.any(values < 0.0) or np.any(values > 1.0):
            raise ValueError(f"forecast {index} has a probability outside [0, 1]")
        total = math.fsum(probabilities.values())
        if abs(total - 1.0) > SUM_TO_ONE_TOLERANCE:
            raise ValueError(
                f"forecast {index} sums to {total!r}; a categorical forecast must be "
                "mutually exclusive and collectively exhaustive (sum to 1)"
            )
        if str(realised) not in probabilities:
            raise ValueError(
                f"outcome {index} {realised!r} is not one of the forecast's categories "
                f"{sorted(probabilities)}; the category set is not exhaustive"
            )
        pairs.append((probabilities, str(realised)))
    return pairs


def _levels(levels) -> np.ndarray:
    tau = _as_float_array(levels, "levels", ndim=1)
    if np.any(tau <= 0.0) or np.any(tau >= 1.0):
        raise ValueError("quantile levels must lie strictly inside (0, 1)")
    if np.any(np.diff(tau) <= 0.0):
        raise ValueError("quantile levels must be strictly increasing")
    return tau


def _quantile_matrix(levels, quantiles, realized) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool]:
    """Normalise quantile inputs to ``(tau[K], q[N, K], y[N], scalar_input)``."""
    tau = _levels(levels)
    q = _as_float_array(quantiles, "quantiles")
    scalar = q.ndim == 1
    if scalar:
        q = q[np.newaxis, :]
    if q.ndim != 2 or q.shape[1] != tau.shape[0]:
        raise ValueError(
            f"quantiles must have {tau.shape[0]} columns (one per level), got shape {q.shape}"
        )
    if np.any(np.diff(q, axis=1) < 0.0):
        raise ValueError("quantile values must be non-decreasing in the level")
    y = _as_float_array(np.atleast_1d(realized), "realized", ndim=1)
    if y.shape[0] != q.shape[0]:
        raise ValueError(f"realized has {y.shape[0]} values for {q.shape[0]} forecasts")
    return tau, q, y, scalar


def _quantile_nodes(tau: np.ndarray, q: np.ndarray, tails: str) -> tuple[np.ndarray, np.ndarray]:
    """Node levels ``[0, tau..., 1]`` and node values of the implied quantile function."""
    if tails not in _TAIL_MODES:
        raise ValueError(f"tails must be one of {_TAIL_MODES}, got {tails!r}")
    k = tau.shape[0]
    if tails == "linear":
        if k < 2:
            raise ValueError("tails='linear' needs at least two quantile levels; use tails='flat'")
        slope_low = (q[:, 1] - q[:, 0]) / (tau[1] - tau[0])
        slope_high = (q[:, -1] - q[:, -2]) / (tau[-1] - tau[-2])
        q_zero = q[:, 0] - tau[0] * slope_low
        q_one = q[:, -1] + (1.0 - tau[-1]) * slope_high
    else:
        q_zero = q[:, 0]
        q_one = q[:, -1]
    node_tau = np.concatenate(([0.0], tau, [1.0]))
    node_q = np.column_stack([q_zero, q, q_one])
    return node_tau, node_q


# ---------------------------------------------------------------------------
# Binary and categorical scores
# ---------------------------------------------------------------------------


def wilson_interval(
    estimate: float, n: int, confidence: float = 0.95, z: float | None = None
) -> WilsonInterval:
    """Wilson score interval for an observed frequency.

    Args:
        estimate: Observed frequency in [0, 1], e.g. the Monte Carlo share of
            rollouts ending in one terminal outcome.
        n: Number of trials behind the frequency (>= 1).
        confidence: Two-sided confidence level in (0, 1).
        z: Explicit critical value overriding ``confidence``. The actor
            simulator reports its 95% half-widths with the rounded ``z = 1.96``;
            pass that to reproduce them exactly (the exact 97.5% normal
            quantile differs by about 2e-8 in the half-width at 300k rollouts).

    Returns:
        A :class:`WilsonInterval`.

    Raises:
        ValueError: On an estimate outside [0, 1], ``n < 1``, a confidence
            outside (0, 1) or a non-positive ``z``.
    """
    if not 0.0 <= float(estimate) <= 1.0 or not math.isfinite(float(estimate)):
        raise ValueError("estimate must lie in [0, 1]")
    if int(n) != n or int(n) < 1:
        raise ValueError("n must be a positive integer")
    if not 0.0 < float(confidence) < 1.0:
        raise ValueError("confidence must lie in (0, 1)")
    if z is not None and (not math.isfinite(float(z)) or float(z) <= 0.0):
        raise ValueError("z must be a positive finite number")
    p = float(estimate)
    n = int(n)
    z = float(z) if z is not None else float(norm.ppf(1.0 - (1.0 - float(confidence)) / 2.0))
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    halfwidth = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    # At p = 0 (p = 1) the lower (upper) bound is exactly 0 (1) algebraically;
    # pin it so rounding cannot report an interval that excludes the boundary.
    lower = 0.0 if p == 0.0 else max(0.0, center - halfwidth)
    upper = 1.0 if p == 1.0 else min(1.0, center + halfwidth)
    return WilsonInterval(
        estimate=p,
        n=n,
        confidence=float(confidence),
        center=center,
        halfwidth=halfwidth,
        lower=lower,
        upper=upper,
    )


def brier_score(forecasts: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean Brier score of binary probability forecasts.

    ``mean((p - o)^2)``: 0 is perfect, 0.25 is the always-0.5 forecast, 1 is a
    confident miss.

    Args:
        forecasts: Probabilities in [0, 1] that each event happens.
        outcomes: 1 when the event happened, else 0 (booleans accepted).

    Returns:
        The mean Brier score.
    """
    p = _probabilities(forecasts)
    o = _binary_outcomes(outcomes, p.shape[0])
    return float(np.mean((p - o) ** 2))


def brier_score_multiclass(
    forecasts: Sequence[Mapping[str, float]], outcomes: Sequence[str]
) -> float:
    """Mean multi-category Brier score.

    Per event ``sum_k (p_k - 1[k == realised])^2``, in [0, 2]. Each forecast
    must be a complete distribution over an exhaustive category set that
    contains the realised category; anything else is refused rather than
    scored, because a Brier number on an incoherent forecast is meaningless.

    Args:
        forecasts: One ``{category: probability}`` mapping per event.
        outcomes: The realised category of each event.

    Returns:
        The mean multi-category Brier score.
    """
    per_event = [
        math.fsum((prob - (1.0 if category == realised else 0.0)) ** 2 for category, prob in dist.items())
        for dist, realised in _categorical(forecasts, outcomes)
    ]
    return math.fsum(per_event) / len(per_event)


def log_loss(
    forecasts: Sequence[float], outcomes: Sequence[int], eps: float = DEFAULT_LOG_EPS
) -> float:
    """Mean negative log likelihood of binary forecasts (natural log).

    Args:
        forecasts: Probabilities in [0, 1].
        outcomes: 0/1 outcomes.
        eps: Probabilities are clipped to ``[eps, 1 - eps]``; ``0`` disables
            clipping (a zero-probability realised outcome then scores ``inf``).

    Returns:
        The mean log loss.
    """
    if not 0.0 <= eps < 0.5:
        raise ValueError("eps must lie in [0, 0.5)")
    p = _probabilities(forecasts)
    o = _binary_outcomes(outcomes, p.shape[0])
    p_realised = np.where(o == 1.0, p, 1.0 - p)
    if eps > 0.0:
        p_realised = np.clip(p_realised, eps, 1.0)
    with np.errstate(divide="ignore"):
        return float(-np.mean(np.log(p_realised)))


def log_loss_multiclass(
    forecasts: Sequence[Mapping[str, float]], outcomes: Sequence[str], eps: float = DEFAULT_LOG_EPS
) -> float:
    """Mean negative log probability assigned to the realised category.

    Args:
        forecasts: One complete ``{category: probability}`` mapping per event.
        outcomes: Realised categories.
        eps: Clip floor for the realised probability; ``0`` disables clipping.

    Returns:
        The mean log loss.
    """
    if not 0.0 <= eps < 0.5:
        raise ValueError("eps must lie in [0, 0.5)")
    losses = []
    for dist, realised in _categorical(forecasts, outcomes):
        prob = max(dist[realised], eps)
        losses.append(math.inf if prob <= 0.0 else -math.log(prob))
    return math.fsum(losses) / len(losses)


def skill_score(score: float, reference_score: float, perfect_score: float = 0.0) -> float:
    """Generic skill score ``(score - reference) / (perfect - reference)``.

    For a negatively oriented score with perfect value 0 this is
    ``1 - score / reference``: 1 is perfect, 0 matches the reference, negative
    is worse than the reference.

    Args:
        score: The forecast's score.
        reference_score: The reference (baseline) forecast's score on the
            same events.
        perfect_score: Score of a perfect forecast (0 for every score here).

    Returns:
        The skill score.

    Raises:
        ValueError: When the reference is itself perfect, where skill is
            undefined rather than zero or infinite.
    """
    values = (float(score), float(reference_score), float(perfect_score))
    if not all(math.isfinite(v) for v in values):
        raise ValueError("scores must be finite")
    if values[1] == values[2]:
        raise ValueError("reference score equals the perfect score; skill is undefined")
    return (values[0] - values[1]) / (values[2] - values[1])


def brier_skill_score(
    forecasts: Sequence[float],
    outcomes: Sequence[int],
    reference: float | Sequence[float],
) -> float:
    """Brier skill score against a reference forecast on the same events.

    Args:
        forecasts: Probabilities being evaluated.
        outcomes: 0/1 outcomes.
        reference: One probability used for every event (a base rate) or one
            reference probability per event (for example the market-implied
            probability recorded beside each forecast).

    Returns:
        ``1 - BS / BS_reference``.
    """
    p = _probabilities(forecasts)
    if np.ndim(reference) == 0:
        reference_forecasts = np.full(p.shape[0], float(reference))
    else:
        reference_forecasts = _probabilities(reference, "reference")
        if reference_forecasts.shape != p.shape:
            raise ValueError("reference must be a scalar or match forecasts in length")
    return skill_score(
        brier_score(p, outcomes), brier_score(reference_forecasts, outcomes)
    )


# ---------------------------------------------------------------------------
# Quantile forecasts
# ---------------------------------------------------------------------------


def pinball_loss(realized, quantile, level):
    """Pinball (quantile) loss ``rho_tau(y - q)``, vectorised.

    ``rho_tau(u) = tau * u`` when ``u >= 0`` and ``(tau - 1) * u`` otherwise.

    Args:
        realized: Outcome(s) ``y``.
        quantile: Forecast quantile(s) ``q``.
        level: Level(s) ``tau`` in (0, 1).

    Returns:
        A float for scalar inputs, else an array broadcast from the inputs.
    """
    y = np.asarray(realized, dtype=float)
    q = np.asarray(quantile, dtype=float)
    tau = np.asarray(level, dtype=float)
    if not (np.all(np.isfinite(y)) and np.all(np.isfinite(q)) and np.all(np.isfinite(tau))):
        raise ValueError("pinball_loss inputs must be finite")
    if np.any(tau <= 0.0) or np.any(tau >= 1.0):
        raise ValueError("levels must lie strictly inside (0, 1)")
    u = y - q
    loss = np.maximum(tau * u, (tau - 1.0) * u)
    return float(loss) if loss.ndim == 0 else loss


def mean_pinball_loss(levels: Sequence[float], quantiles, realized) -> float:
    """Pinball loss averaged over a quantile grid (and over forecasts).

    Args:
        levels: Strictly increasing levels in (0, 1), length ``K``.
        quantiles: ``K`` values for one forecast, or an ``N x K`` array.
        realized: One outcome, or ``N`` outcomes.

    Returns:
        The mean over every forecast and level.
    """
    tau, q, y, _ = _quantile_matrix(levels, quantiles, realized)
    return float(np.mean(pinball_loss(y[:, np.newaxis], q, tau[np.newaxis, :])))


def _piece_integral(a, b, qa, qb, y):
    """Exact integral of ``pinball_tau(y, Q(tau))`` over one monotone piece.

    ``Q`` is linear from ``qa`` at ``a`` to ``qb`` at ``b`` and does not cross
    ``y`` inside the piece, so the integrand is one quadratic in ``tau`` and
    Simpson's rule is exact.
    """
    mid = 0.5 * (a + b)
    q_mid = 0.5 * (qa + qb)
    above = (y < q_mid).astype(float)  # 1 where Q(tau) > y on this piece

    def g(t, q):
        return (y - q) * (t - above)

    return (b - a) / 6.0 * (g(a, qa) + 4.0 * g(mid, q_mid) + g(b, qb))


def crps_from_quantiles(
    levels: Sequence[float], quantiles, realized, tails: str = "linear"
) -> float:
    """Continuous ranked probability score of a quantile forecast.

    Computed exactly for the distribution the grid implies (see the module
    docstring): ``2 * integral_0^1 pinball_tau(y, Q(tau)) dtau`` with ``Q``
    piecewise linear through the grid and extended by ``tails``.

    Args:
        levels: Strictly increasing levels in (0, 1).
        quantiles: Values for one forecast, or an ``N x K`` array.
        realized: One outcome, or ``N`` outcomes.
        tails: ``"linear"`` (default) or ``"flat"``.

    Returns:
        The CRPS, averaged over forecasts when several are given. It is in the
        outcome's own unit.
    """
    tau, q, y, _ = _quantile_matrix(levels, quantiles, realized)
    node_tau, node_q = _quantile_nodes(tau, q, tails)
    a = node_tau[np.newaxis, :-1]
    b = node_tau[np.newaxis, 1:]
    qa = node_q[:, :-1]
    qb = node_q[:, 1:]
    yy = y[:, np.newaxis]
    crossing = (qa < yy) & (yy < qb)
    safe_rise = np.where(qb > qa, qb - qa, 1.0)
    cut = np.where(crossing, a + (yy - qa) / safe_rise * (b - a), b)
    q_cut = np.where(crossing, yy, qb)
    first = _piece_integral(a, cut, qa, q_cut, yy)
    second = np.where(crossing, _piece_integral(cut, b, q_cut, qb, yy), 0.0)
    per_forecast = 2.0 * np.sum(first + second, axis=1)
    return float(np.mean(per_forecast))


def crps_normal(mu, sigma, realized) -> float:
    """Closed-form CRPS of a normal forecast ``N(mu, sigma^2)``.

    ``sigma * [z (2 Phi(z) - 1) + 2 phi(z) - 1 / sqrt(pi)]`` with
    ``z = (y - mu) / sigma``; averaged when arrays are given.

    Args:
        mu: Mean(s).
        sigma: Standard deviation(s), strictly positive.
        realized: Outcome(s).

    Returns:
        The (mean) CRPS.
    """
    m = np.asarray(mu, dtype=float)
    s = np.asarray(sigma, dtype=float)
    y = np.asarray(realized, dtype=float)
    if not (np.all(np.isfinite(m)) and np.all(np.isfinite(s)) and np.all(np.isfinite(y))):
        raise ValueError("crps_normal inputs must be finite")
    if np.any(s <= 0.0):
        raise ValueError("sigma must be strictly positive")
    z = (y - m) / s
    value = s * (z * (2.0 * norm.cdf(z) - 1.0) + 2.0 * norm.pdf(z) - 1.0 / math.sqrt(math.pi))
    return float(np.mean(value))


def pit_values(levels: Sequence[float], quantiles, realized, tails: str = "linear"):
    """Probability integral transform ``F(y)`` of quantile forecasts.

    ``F`` is the CDF of the distribution the grid implies (linear
    interpolation between grid points, ``tails`` beyond them; see the module
    docstring). Where ``F`` jumps (a point mass) the mid-point of the jump is
    returned.

    Args:
        levels: Strictly increasing levels in (0, 1).
        quantiles: Values for one forecast, or an ``N x K`` array.
        realized: One outcome, or ``N`` outcomes.
        tails: ``"linear"`` (default) or ``"flat"``.

    Returns:
        A float for a single forecast, else a list of floats.
    """
    tau, q, y, scalar = _quantile_matrix(levels, quantiles, realized)
    node_tau, node_q = _quantile_nodes(tau, q, tails)
    out = []
    for values, outcome in zip(node_q, y):
        last = values.shape[0] - 1
        # Right limit F(y): the largest tau whose quantile is still <= y.
        i = int(np.searchsorted(values, outcome, side="right")) - 1
        if i < 0:
            right = 0.0
        elif i >= last:
            right = 1.0
        else:
            right = node_tau[i] + (outcome - values[i]) / (values[i + 1] - values[i]) * (
                node_tau[i + 1] - node_tau[i]
            )
        # Left limit F(y-): the smallest tau whose quantile is >= y.
        j = int(np.searchsorted(values, outcome, side="left"))
        if j > last:
            left = 1.0
        elif j == 0:
            left = 0.0
        else:
            left = node_tau[j - 1] + (outcome - values[j - 1]) / (values[j] - values[j - 1]) * (
                node_tau[j] - node_tau[j - 1]
            )
        out.append(float(min(1.0, max(0.0, 0.5 * (left + right)))))
    return out[0] if scalar else out


def pit_histogram(pit: Sequence[float], bins: int = 10) -> PitHistogram:
    """Histogram of PIT values with a chi-square test against uniformity.

    Args:
        pit: PIT values in [0, 1].
        bins: Number of equal-width bins (>= 2).

    Returns:
        A :class:`PitHistogram`.
    """
    values = _as_float_array(pit, "pit", ndim=1)
    if np.any(values < 0.0) or np.any(values > 1.0):
        raise ValueError("PIT values must lie in [0, 1]")
    if int(bins) != bins or int(bins) < 2:
        raise ValueError("bins must be an integer >= 2")
    bins = int(bins)
    counts, edges = np.histogram(values, bins=bins, range=(0.0, 1.0))
    n = int(values.shape[0])
    expected = n / bins
    statistic = float(np.sum((counts - expected) ** 2) / expected)
    return PitHistogram(
        n=n,
        edges=[float(e) for e in edges],
        counts=[int(c) for c in counts],
        frequencies=[float(c) / n for c in counts],
        expected_frequency=1.0 / bins,
        chi2_statistic=statistic,
        chi2_p_value=float(chi2.sf(statistic, bins - 1)),
        chi2_valid=bool(expected >= 5.0),
    )


def interval_coverage(
    lower: Sequence[float],
    upper: Sequence[float],
    realized: Sequence[float],
    nominal: float | None = None,
) -> CoverageResult:
    """Share of outcomes inside their prediction intervals.

    Args:
        lower: Interval lower bounds.
        upper: Interval upper bounds (>= lower).
        realized: Outcomes.
        nominal: Claimed coverage in (0, 1), e.g. 0.9 for a q05-q95 interval.

    Returns:
        A :class:`CoverageResult`.
    """
    lo = _as_float_array(np.atleast_1d(lower), "lower", ndim=1)
    hi = _as_float_array(np.atleast_1d(upper), "upper", ndim=1)
    y = _as_float_array(np.atleast_1d(realized), "realized", ndim=1)
    if not lo.shape == hi.shape == y.shape:
        raise ValueError("lower, upper and realized must have the same length")
    if np.any(hi < lo):
        raise ValueError("every upper bound must be >= its lower bound")
    below = int(np.sum(y < lo))
    above = int(np.sum(y > hi))
    n = int(y.shape[0])
    hits = n - below - above
    p_value = None
    if nominal is not None:
        if not 0.0 < float(nominal) < 1.0:
            raise ValueError("nominal must lie in (0, 1)")
        p_value = float(binomtest(hits, n, float(nominal)).pvalue)
    return CoverageResult(
        n=n,
        hits=hits,
        below=below,
        above=above,
        coverage=hits / n,
        nominal=None if nominal is None else float(nominal),
        binomial_p_value=p_value,
    )


def interval_score(
    lower: Sequence[float], upper: Sequence[float], realized: Sequence[float], alpha: float
) -> float:
    """Mean interval score of central ``(1 - alpha)`` prediction intervals.

    Gneiting and Raftery (2007): ``(u - l) + (2/alpha)(l - y)1[y < l] +
    (2/alpha)(y - u)1[y > u]``. Proper for the ``alpha/2`` and ``1 - alpha/2``
    quantiles; it rewards narrow intervals and charges for misses in
    proportion to their distance.

    Args:
        lower: Lower bounds.
        upper: Upper bounds.
        realized: Outcomes.
        alpha: Miss rate the interval claims, in (0, 1).

    Returns:
        The mean interval score.
    """
    if not 0.0 < float(alpha) < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    lo = _as_float_array(np.atleast_1d(lower), "lower", ndim=1)
    hi = _as_float_array(np.atleast_1d(upper), "upper", ndim=1)
    y = _as_float_array(np.atleast_1d(realized), "realized", ndim=1)
    if not lo.shape == hi.shape == y.shape:
        raise ValueError("lower, upper and realized must have the same length")
    if np.any(hi < lo):
        raise ValueError("every upper bound must be >= its lower bound")
    penalty = 2.0 / float(alpha)
    score = (hi - lo) + penalty * np.clip(lo - y, 0.0, None) + penalty * np.clip(y - hi, 0.0, None)
    return float(np.mean(score))


def tail_exceedance_rates(
    levels: Sequence[float],
    quantiles,
    realized,
    tail_levels: Sequence[float] | None = None,
) -> list[TailExceedance]:
    """Observed versus expected tail exceedance at grid levels.

    A level below 0.5 counts outcomes strictly below its quantile (expected
    rate ``tau``); a level at or above 0.5 counts outcomes strictly above it
    (expected rate ``1 - tau``). Only levels that are on the grid are
    accepted: a tail threshold interpolated between grid points would be a
    different forecast from the one recorded.

    Args:
        levels: Strictly increasing grid levels.
        quantiles: ``K`` values for one forecast, or ``N x K``.
        realized: Outcome(s).
        tail_levels: Levels to test; defaults to whichever of 0.01, 0.05,
            0.95 and 0.99 are on the grid.

    Returns:
        One :class:`TailExceedance` per tested level, in level order.
    """
    tau, q, y, _ = _quantile_matrix(levels, quantiles, realized)
    grid = [float(t) for t in tau]
    if tail_levels is None:
        wanted = [t for t in (0.01, 0.05, 0.95, 0.99) if any(math.isclose(t, g, abs_tol=1e-12) for g in grid)]
        if not wanted:
            raise ValueError("none of 0.01, 0.05, 0.95, 0.99 is on the grid; pass tail_levels")
    else:
        wanted = sorted(float(t) for t in tail_levels)
    results = []
    n = int(y.shape[0])
    for level in wanted:
        matches = [i for i, g in enumerate(grid) if math.isclose(level, g, abs_tol=1e-12)]
        if not matches:
            raise ValueError(f"tail level {level} is not on the quantile grid")
        column = q[:, matches[0]]
        if level < 0.5:
            side, count, expected = "lower", int(np.sum(y < column)), level
        else:
            side, count, expected = "upper", int(np.sum(y > column)), 1.0 - level
        results.append(
            TailExceedance(
                level=level,
                side=side,
                n=n,
                exceedances=count,
                expected_rate=expected,
                observed_rate=count / n,
                binomial_p_value=float(binomtest(count, n, expected).pvalue),
            )
        )
    return results
