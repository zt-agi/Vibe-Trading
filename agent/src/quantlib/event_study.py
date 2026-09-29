"""Knowledge-time event studies: abnormal returns around dated disclosures.

ZT add-on. :mod:`src.quantlib.eventstudy` answers "did the market react to
events anchored on these rows"; this module answers the question an earnings
study actually has to answer first -- *which session is the event's day 0*,
given the instant the market could first have known it -- and then measures
several windows per event with inference that survives what earnings seasons
do to a sample: events cluster on the same days and weeks, and their abnormal
returns are cross-correlated. It reuses that module's market-model fit and its
CAR standard error (prediction error included), so the two cannot disagree
about a single event.

DAY 0 IS THE FIRST SESSION WHOSE CLOSE IS AFTER THE KNOWLEDGE TIME
------------------------------------------------------------------
An 8-K accepted at 16:05 New York time is not in that day's close: its day 0
is the next session. One accepted at 07:30 is in that day's close, so day 0 is
the same session. :func:`first_session_after` implements exactly that rule
against session close instants (16:00 New York, 13:00 on NYSE early-close
days), and an acceptance at 16:00:00 sharp belongs to the next session. A
weekend or holiday filing rolls to the next session because only sessions
carry closes. Day ``k`` is then ``k`` sessions after day 0 on the benchmark's
calendar, so a halted stock cannot shift the clock.

THE ESTIMATION WINDOW
---------------------
The market model is OLS of the stock on the benchmark over relative sessions
``[-250, -11]`` by default. The firm's *other* events (including duplicates
dropped by :func:`dedupe_events`) are cut out of it, ``exclusion_window``
sessions around each, so a prior announcement cannot inflate the residual
variance the tests scale by. Missing prices simply leave the sample; an event
keeps its estimate only while ``min_estimation_days`` sessions survive.

WINDOWS, SKIPS AND DUPLICATES
----------------------------
Every window is inclusive, e.g. the announcement ``[0, +1]`` and the drifts
``[+2, +20]`` and ``[+2, +60]``. A window that runs past the last session, or
holds a missing price, is skipped *for that window only* and reported in
``skips`` with its reason; nothing is silently dropped and nothing is filled.
Several disclosures in one firm-period (an 8-K and its amendment, a
pre-announcement and the release) collapse to the earliest by knowledge time.

TESTS, AND WHICH ONE SURVIVES CLUSTERING
----------------------------------------
Per window and group the report carries: the cross-sectional t; a t clustered
by calendar period of day 0 (``cluster_freq``); Patell and BMP; both with the
Kolari-Pynnonen (2010) correction for cross-correlation; the Cowan generalized
sign test; the Kolari-Pynnonen (2011) generalized rank test (GRANK); and a
cluster-bootstrap confidence interval for the mean CAR. The plain t and the
unadjusted Patell/BMP assume independent events and over-reject when event
dates cluster; the clustered t, the K-P adjusted statistics and GRANK are the
ones built for that sample. Benjamini-Hochberg then controls the false
discovery rate across every window x group cell, through
:func:`src.quantlib.multipletesting.benjamini_hochberg`.

Kolari-Pynnonen with dispersed event dates: the correction needs the average
correlation of the standardized CARs. Two events' CARs share only the sessions
their windows share, so the pairwise correlation used is the estimation-period
residual correlation times the fraction of the window the two have in common.
With every event on the same day this is the original K-P statistic; with no
overlap at all it leaves BMP unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import beta as beta_distribution
from scipy.stats import norm, rankdata
from scipy.stats import t as student_t

from src.quantlib.eventstudy import MarketModelFit, _car_std_error, estimate_market_model
from src.quantlib.multipletesting import benjamini_hochberg

__all__ = [
    "DEFAULT_CAR_WINDOWS",
    "DEFAULT_ESTIMATION_WINDOW",
    "DEFAULT_EXCLUSION_WINDOW",
    "DEFAULT_MIN_ESTIMATION_DAYS",
    "FDR_TESTS",
    "EventStudyReport",
    "HitRate",
    "beta_binomial_hit_rate",
    "cluster_bootstrap_ci",
    "clustered_t_test",
    "dedupe_events",
    "first_session_after",
    "generalized_sign_test",
    "grank_test",
    "hit_rates",
    "kolari_pynnonen_factor",
    "nyse_early_closes",
    "nyse_holidays",
    "run_event_study",
    "session_close_times",
    "us_equity_sessions",
    "window_label",
]

#: Relative sessions of the market-model estimation window, inclusive.
DEFAULT_ESTIMATION_WINDOW: tuple[int, int] = (-250, -11)

#: Announcement reaction plus the two post-announcement drift horizons.
DEFAULT_CAR_WINDOWS: tuple[tuple[int, int], ...] = ((0, 1), (2, 20), (2, 60))

#: Sessions cut out of the estimation window around each of the firm's other
#: events (relative to that event's day 0).
DEFAULT_EXCLUSION_WINDOW: tuple[int, int] = (-1, 1)

#: Fewest estimation sessions an event keeps its market model with.
DEFAULT_MIN_ESTIMATION_DAYS: int = 120

#: Statistics Benjamini-Hochberg may be run on (see :func:`run_event_study`).
FDR_TESTS: tuple[str, ...] = (
    "t", "t_clustered", "patell", "patell_kp", "bmp", "bmp_kp", "sign", "grank",
)

_NEW_YORK = ZoneInfo("America/New_York")
_REGULAR_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)
#: Fewest common estimation sessions a residual correlation is computed on.
_MIN_PAIR_OVERLAP = 30
_ALL = "ALL"
_SIGNED = "SIGNED"
_CLUSTER_FREQS = ("D", "W", "M", "Q")

#: NYSE full-day closures that no holiday rule produces.
_SPECIAL_CLOSURES: frozenset[date] = frozenset({
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
    date(2004, 6, 11), date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30),
    date(2018, 12, 5), date(2025, 1, 9),
})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventStudyReport:
    """Everything a knowledge-time event study measured and refused to measure.

    Attributes:
        events: One row per measured (event, window): ids, firm, knowledge
            time, day-0 session, group, surprise sign, market-model fit, CAR,
            its standard error and the standardized CAR.
        aggregates: One row per (window, group) with every test statistic,
            its two-sided p-value, the cluster-bootstrap interval and the
            Benjamini-Hochberg adjusted p-value of ``fdr_test``.
        caar_path: Mean cumulative abnormal return by relative session and
            group, over events observed on every session of the path, so the
            tail is not an average of a shrinking sample.
        skips: ``(event_id, firm, window, reason)`` for every event or window
            that could not be measured. ``window`` is ``"*"`` for the event.
        duplicates: Events dropped by the firm-period de-duplication, with the
            id of the event kept instead.
        params: The parameters the study ran with.
    """

    events: pd.DataFrame
    aggregates: pd.DataFrame
    caar_path: pd.DataFrame
    skips: pd.DataFrame
    duplicates: pd.DataFrame
    params: dict


@dataclass(frozen=True)
class HitRate:
    """Beta-binomial posterior of a hit rate.

    Attributes:
        successes: Observed hits ``k``.
        trials: Observed events ``n``.
        prior_alpha: Beta prior ``alpha``.
        prior_beta: Beta prior ``beta``.
        posterior_alpha: ``prior_alpha + k``.
        posterior_beta: ``prior_beta + n - k``.
        probability: Posterior predictive probability that the next event is a
            hit, the posterior mean ``posterior_alpha / (posterior_alpha +
            posterior_beta)``.
        lower: Lower bound of the central credible interval.
        upper: Upper bound of the central credible interval.
        level: Credible level of ``[lower, upper]``.
    """

    successes: int
    trials: int
    prior_alpha: float
    prior_beta: float
    posterior_alpha: float
    posterior_beta: float
    probability: float
    lower: float
    upper: float
    level: float


# ---------------------------------------------------------------------------
# Calendar and alignment
# ---------------------------------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(day: date, *, saturday_to_friday: bool = True) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1) if saturday_to_friday else None
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    """NYSE full-day closures in ``year`` by the exchange's holiday rules.

    New Year's Day (a Saturday New Year is not observed on the Friday before),
    Martin Luther King Jr. Day, Washington's Birthday, Good Friday, Memorial
    Day, Juneteenth (from 2022), Independence Day, Labor Day, Thanksgiving and
    Christmas, with weekend observance, plus the unscheduled closures since
    2001 (9/11, two national days of mourning for former presidents in 2004
    and 2007 and two since, and Hurricane Sandy).

    Args:
        year: Calendar year.

    Returns:
        The set of closed weekdays.
    """
    days: set[date | None] = {
        _observed(date(year, 1, 1), saturday_to_friday=False),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    days |= {d for d in _SPECIAL_CLOSURES if d.year == year}
    return {d for d in days if d is not None and d.weekday() < 5}


def nyse_early_closes(year: int) -> set[date]:
    """Sessions in ``year`` on which the NYSE closes at 13:00 New York time.

    July 3 when it falls Monday to Thursday, the day after Thanksgiving, and
    Christmas Eve when it falls Monday to Thursday.

    Args:
        year: Calendar year.

    Returns:
        The set of early-close sessions.
    """
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    for day in (date(year, 7, 3), date(year, 12, 24)):
        if day.weekday() <= 3:
            out.add(day)
    return out - nyse_holidays(year)


def us_equity_sessions(start: Any, end: Any) -> pd.DatetimeIndex:
    """NYSE sessions between two dates, inclusive, from the holiday rules.

    Use it for sessions that have not traded yet; for the past, the
    benchmark's own bars are the calendar.

    Args:
        start: First date (anything ``pd.Timestamp`` accepts).
        end: Last date.

    Returns:
        A naive, normalized ``DatetimeIndex`` of sessions.
    """
    first, last = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if last < first:
        return pd.DatetimeIndex([])
    closed: set[date] = set()
    for year in range(first.year, last.year + 1):
        closed |= nyse_holidays(year)
    days = pd.bdate_range(first, last)
    return pd.DatetimeIndex([d for d in days if d.date() not in closed])


def session_close_times(
    sessions: Iterable[Any],
    *,
    early_closes: Iterable[Any] | None = None,
    tz: str | ZoneInfo = _NEW_YORK,
    close: time = _REGULAR_CLOSE,
    early_close: time = _EARLY_CLOSE,
) -> pd.Series:
    """The closing instant, in UTC, of each session.

    Args:
        sessions: Session dates.
        early_closes: Sessions closing at ``early_close``. ``None`` applies
            :func:`nyse_early_closes`; pass an empty list for none.
        tz: Exchange time zone.
        close: Regular closing time in ``tz``.
        early_close: Early closing time in ``tz``.

    Returns:
        Series indexed by the normalized session dates (sorted, unique), whose
        values are tz-aware UTC timestamps.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    days = pd.DatetimeIndex(pd.to_datetime(list(sessions))).normalize()
    if days.tz is not None:
        days = days.tz_localize(None)
    days = days.unique().sort_values()
    if early_closes is None:
        early: set[date] = set()
        for year in sorted({d.year for d in days}):
            early |= nyse_early_closes(year)
    else:
        early = {pd.Timestamp(d).date() for d in early_closes}
    stamps = [
        pd.Timestamp(datetime.combine(d.date(), early_close if d.date() in early else close),
                     tz=zone).tz_convert("UTC")
        for d in days
    ]
    return pd.Series(stamps, index=days, name="close_utc", dtype="datetime64[ns, UTC]")


def _as_utc_index(values: Any) -> pd.DatetimeIndex:
    """Instants as a UTC ``DatetimeIndex``; naive values are UTC (pitdb convention)."""
    stamps = pd.to_datetime(pd.Series(list(values) if not isinstance(values, pd.Series) else values),
                            utc=True, errors="coerce")
    return pd.DatetimeIndex(stamps)


def first_session_after(knowledge_times: Any, closes: pd.Series) -> np.ndarray:
    """Position of the first session whose close is strictly after each instant.

    Args:
        knowledge_times: Instants the market could first have known each
            event. Naive values are read as UTC.
        closes: Output of :func:`session_close_times` (sorted close instants).

    Returns:
        Integer positions into ``closes``; ``-1`` where the instant is missing
        or no session in ``closes`` closes after it.
    """
    kts = _as_utc_index(knowledge_times)
    close_index = pd.DatetimeIndex(pd.to_datetime(pd.Series(closes).reset_index(drop=True), utc=True))
    close_ns = close_index.as_unit("ns").asi8
    if np.any(np.diff(close_ns) <= 0):
        raise ValueError("session closes must be strictly increasing")
    out = np.full(len(kts), -1, dtype=np.int64)
    valid = ~kts.isna()
    if valid.any():
        pos = np.searchsorted(close_ns, kts[valid].as_unit("ns").asi8, side="right")
        pos = np.where(pos < len(close_ns), pos, -1)
        out[np.flatnonzero(valid)] = pos
    return out


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def window_label(window: Sequence[int]) -> str:
    """``(0, 1)`` -> ``"[0,+1]"``; ``(2, 20)`` -> ``"[+2,+20]"``; ``(-5, -1)`` -> ``"[-5,-1]"``."""
    def one(k: int) -> str:
        return "0" if k == 0 else f"{k:+d}"

    start, end = int(window[0]), int(window[1])
    return f"[{one(start)},{one(end)}]"


def dedupe_events(
    events: pd.DataFrame,
    *,
    firm_col: str = "firm",
    period_col: str = "period",
    time_col: str = "knowledge_time",
    id_col: str = "event_id",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep the earliest event per firm-period; report the rest.

    An 8-K and its amendment, or a pre-announcement and the release, are two
    disclosures of one quarter; counting both double-counts the firm-quarter.
    The earliest knowledge time wins (ties by ``id_col``). Rows without a
    period, or without a knowledge time, are never merged.

    Args:
        events: Event rows.
        firm_col: Firm identifier column.
        period_col: Fiscal-period key column.
        time_col: Knowledge-time column.
        id_col: Unique event id column.

    Returns:
        ``(kept, duplicates)``. ``duplicates`` carries ``kept_event_id``.
    """
    if events.empty or period_col not in events.columns:
        return events.copy(), events.iloc[0:0].assign(kept_event_id=pd.Series(dtype=object))
    order_col = "__dedupe_order_utc__"
    frame = events.copy()
    frame[order_col] = _as_utc_index(frame[time_col]).to_numpy()
    mergeable = frame[period_col].notna() & frame[order_col].notna()
    ranked = frame[mergeable].sort_values([order_col, id_col], kind="mergesort")
    first = ranked.groupby([firm_col, period_col], sort=False)[id_col].transform("first")
    is_dup = ranked[id_col] != first
    duplicates = ranked[is_dup].assign(kept_event_id=first[is_dup])
    kept = pd.concat([ranked[~is_dup], frame[~mergeable]])
    kept = kept.sort_values([order_col, id_col], kind="mergesort", na_position="last")
    return kept.drop(columns=order_col), duplicates.drop(columns=order_col)


# ---------------------------------------------------------------------------
# Test statistics
# ---------------------------------------------------------------------------


def _two_sided_normal(z: float) -> float:
    return float(2.0 * norm.sf(abs(z))) if math.isfinite(z) else float("nan")


def _two_sided_t(t_value: float, dof: float) -> float:
    if not math.isfinite(t_value) or not dof or dof <= 0:
        return float("nan")
    return float(2.0 * student_t.sf(abs(t_value), dof))


def clustered_t_test(values: Sequence[float], clusters: Sequence[Any]) -> tuple[float, float, int]:
    """t-test of a mean with a cluster-robust (CR1) standard error.

    Args:
        values: Observations (e.g. CARs).
        clusters: Cluster key of each observation (e.g. the week of day 0).

    Returns:
        ``(t, two-sided p, number of clusters)``; the reference distribution
        is Student t with ``G - 1`` degrees of freedom. NaN when fewer than
        two clusters.
    """
    x = np.asarray(values, dtype=float)
    keys = pd.Index(list(clusters))
    if x.size != len(keys):
        raise ValueError("values and clusters differ in length")
    n = x.size
    codes, uniques = pd.factorize(keys)
    groups = len(uniques)
    if n < 2 or groups < 2:
        return float("nan"), float("nan"), groups
    resid = x - x.mean()
    sums = np.bincount(codes, weights=resid, minlength=groups)
    variance = groups / (groups - 1) * float(np.sum(sums**2)) / n**2
    if variance <= 0:
        return float("nan"), float("nan"), groups
    t_value = float(x.mean() / math.sqrt(variance))
    return t_value, _two_sided_t(t_value, groups - 1), groups


def kolari_pynnonen_factor(n: int, rbar: float, *, statistic: str = "bmp") -> float:
    """Multiplier that corrects a standardized test for cross-correlation.

    Kolari and Pynnonen (2010): ``t_adj = t * sqrt((1 - r) / (1 + (n - 1) r))``
    for BMP, and ``z_adj = z * sqrt(1 / (1 + (n - 1) r))`` for Patell, where
    ``r`` is the average correlation of the standardized abnormal returns.

    Args:
        n: Number of events.
        rbar: Average pairwise correlation.
        statistic: ``"bmp"`` or ``"patell"``.

    Returns:
        The multiplier; NaN when the correction is undefined.
    """
    if statistic not in ("bmp", "patell"):
        raise ValueError("statistic must be 'bmp' or 'patell'")
    if not math.isfinite(rbar) or n < 1:
        return float("nan")
    denominator = 1.0 + (n - 1) * rbar
    numerator = 1.0 - rbar if statistic == "bmp" else 1.0
    if denominator <= 0 or numerator < 0:
        return float("nan")
    return math.sqrt(numerator / denominator)


def generalized_sign_test(cars: Sequence[float], baseline_positive: float) -> tuple[float, float]:
    """Cowan (1992) generalized sign test.

    Args:
        cars: Event CARs.
        baseline_positive: Fraction of positive abnormal returns expected
            under the null, estimated from the estimation windows.

    Returns:
        ``(z, two-sided p)``.
    """
    x = np.asarray(cars, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    p = float(baseline_positive)
    if n < 2 or not 0.0 < p < 1.0:
        return float("nan"), float("nan")
    wins = float(np.sum(x > 0))
    z = (wins - n * p) / math.sqrt(n * p * (1.0 - p))
    return float(z), _two_sided_normal(z)


def grank_test(
    estimation_sars: Sequence[Mapping[int, float] | tuple[Sequence[int], Sequence[float]]],
    event_scars: Sequence[float],
) -> tuple[float, float, int]:
    """Kolari and Pynnonen (2011) generalized rank test for a CAR.

    Each event contributes its standardized estimation-period abnormal returns
    (keyed by relative session) and one cumulated value: its standardized CAR
    divided by the cross-sectional standard deviation of those, which absorbs
    event-induced volatility. Within an event the ``T_i + 1`` values are
    ranked and demeaned, ``U = rank / (T_i + 2) - 1/2``; the statistic divides
    the mean event-day rank by the time-series dispersion of the mean ranks,
    which carries any cross-correlation, and is referred to Student t with
    ``T - 2`` degrees of freedom, ``T`` being the number of time points.

    Args:
        estimation_sars: Per event, the standardized abnormal returns of its
            estimation window, as ``{relative session: value}`` or as a pair
            ``(relative sessions, values)``.
        event_scars: Per event, the standardized CAR.

    Returns:
        ``(t_grank, two-sided p, T)``.
    """
    scars = np.asarray(event_scars, dtype=float)
    n = scars.size
    if n < 2 or len(estimation_sars) != n or not np.isfinite(scars).all():
        return float("nan"), float("nan"), 0
    spread = float(np.std(scars, ddof=1))
    if not spread > 0:
        return float("nan"), float("nan"), 0
    cumulated = scars / spread
    key_parts, u_parts = [], []
    event_ranks = np.empty(n)
    for i, sars in enumerate(estimation_sars):
        if isinstance(sars, Mapping):
            keys = np.fromiter(sars.keys(), dtype=np.int64, count=len(sars))
            values = np.fromiter(sars.values(), dtype=float, count=len(sars))
        else:
            keys = np.asarray(sars[0], dtype=np.int64)
            values = np.asarray(sars[1], dtype=float)
        ranks = rankdata(np.append(values, cumulated[i]))
        standardized = ranks / (ranks.size + 1.0) - 0.5
        key_parts.append(keys)
        u_parts.append(standardized[:-1])
        event_ranks[i] = standardized[-1]
    all_keys = np.concatenate(key_parts) if key_parts else np.array([], dtype=np.int64)
    all_u = np.concatenate(u_parts) if u_parts else np.array([])
    uniques, inverse = np.unique(all_keys, return_inverse=True)
    sums = np.bincount(inverse, weights=all_u, minlength=uniques.size)
    counts = np.bincount(inverse, minlength=uniques.size).astype(float)
    mean_event = float(event_ranks.mean())
    dispersion = float(np.sum(counts / n * (sums / np.maximum(counts, 1.0)) ** 2))
    dispersion += mean_event**2  # the event day, observed for all n events
    periods = int(uniques.size) + 1
    if periods < 3 or dispersion <= 0:
        return float("nan"), float("nan"), periods
    z = mean_event / math.sqrt(dispersion / periods)
    room = periods - 1 - z * z
    if room <= 0:
        return float("nan"), float("nan"), periods
    t_value = z * math.sqrt((periods - 2) / room)
    return float(t_value), _two_sided_t(t_value, periods - 2), periods


def cluster_bootstrap_ci(
    values: Sequence[float],
    clusters: Sequence[Any],
    *,
    n_boot: int = 2000,
    level: float = 0.95,
    seed: int | None = 20260929,
) -> tuple[float, float, int]:
    """Percentile interval for a mean, resampling whole clusters.

    Resampling events one by one treats a week of correlated announcements as
    independent draws and makes the interval too narrow; resampling clusters
    keeps each week's events together.

    Args:
        values: Observations.
        clusters: Cluster key per observation.
        n_boot: Bootstrap replicates.
        level: Central coverage.
        seed: Generator seed (the interval is reproducible for a fixed seed).

    Returns:
        ``(lower, upper, number of clusters)``; NaN bounds with fewer than two
        clusters.
    """
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie in (0, 1)")
    if n_boot < 100:
        raise ValueError("n_boot must be at least 100")
    x = np.asarray(values, dtype=float)
    codes, uniques = pd.factorize(pd.Index(list(clusters)))
    groups = len(uniques)
    if x.size != len(codes):
        raise ValueError("values and clusters differ in length")
    if groups < 2:
        return float("nan"), float("nan"), groups
    sums = np.bincount(codes, weights=x, minlength=groups)
    counts = np.bincount(codes, minlength=groups).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, groups, size=(n_boot, groups))
    means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    tail = (1.0 - level) / 2.0
    lower, upper = np.quantile(means, [tail, 1.0 - tail])
    return float(lower), float(upper), groups


def beta_binomial_hit_rate(
    successes: int,
    trials: int,
    *,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    level: float = 0.90,
) -> HitRate:
    """Posterior of a hit rate under a Beta prior and binomial sampling.

    A mechanical probability: counts in, probability out, nothing elicited.

    Args:
        successes: Observed hits.
        trials: Observed events.
        prior_alpha: Beta prior ``alpha`` (1 with ``prior_beta`` 1 is uniform).
        prior_beta: Beta prior ``beta``.
        level: Credible level of the reported interval.

    Returns:
        A :class:`HitRate`.
    """
    k, n = int(successes), int(trials)
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= successes <= trials, got {k} of {n}")
    if not (prior_alpha > 0 and prior_beta > 0):
        raise ValueError("prior parameters must be positive")
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie in (0, 1)")
    a, b = prior_alpha + k, prior_beta + n - k
    tail = (1.0 - level) / 2.0
    lower, upper = beta_distribution.ppf([tail, 1.0 - tail], a, b)
    return HitRate(k, n, float(prior_alpha), float(prior_beta), float(a), float(b),
                   float(a / (a + b)), float(lower), float(upper), float(level))


# ---------------------------------------------------------------------------
# The study
# ---------------------------------------------------------------------------


@dataclass
class _Measured:
    event_id: Any
    firm: str
    knowledge_time: pd.Timestamp
    day0_pos: int
    group: str | None
    sign: int
    cluster: Any
    fit: MarketModelFit
    est_pos: np.ndarray
    est_resid: np.ndarray
    key: int = 0
    path_ar: dict[int, float] = field(default_factory=dict)
    windows: dict[str, tuple[float, float, float]] = field(default_factory=dict)

    def standardized_estimation(self) -> tuple[np.ndarray, np.ndarray]:
        """Relative sessions and sign-adjusted standardized estimation residuals."""
        sigma = self.fit.residual_std
        if not sigma > 0:
            return np.array([], dtype=np.int64), np.array([])
        return self.est_pos - self.day0_pos, self.sign * self.est_resid / sigma


def _coerce_prices(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    frame.index = index.normalize()
    if frame.index.has_duplicates:
        raise ValueError("prices has duplicate session dates")
    frame = frame.sort_index()
    return frame.apply(pd.to_numeric, errors="coerce").astype(float)


def _cluster_key(day: pd.Timestamp, freq: str) -> Any:
    if freq == "D":
        return day.date()
    return str(pd.Period(day, freq=freq))


def _residual_correlation(mi: _Measured, mj: _Measured, cache: dict) -> float | None:
    """Estimation-residual correlation of two events on their common sessions (cached)."""
    key = (mi.key, mj.key) if mi.key <= mj.key else (mj.key, mi.key)
    if key in cache:
        return cache[key]
    _, ii, jj = np.intersect1d(mi.est_pos, mj.est_pos, assume_unique=True, return_indices=True)
    value: float | None = None
    if ii.size >= _MIN_PAIR_OVERLAP:
        ei = mi.est_resid[ii] - mi.est_resid[ii].mean()
        ej = mj.est_resid[jj] - mj.est_resid[jj].mean()
        norm_product = math.sqrt(float(ei @ ei) * float(ej @ ej))
        if norm_product > 0:
            value = float(ei @ ej) / norm_product
    cache[key] = value
    return value


def _kp_rbar(members: list[_Measured], starts: np.ndarray, length: int,
             cache: dict) -> tuple[float, int, int]:
    """Average pairwise correlation of the standardized CARs in one cell.

    Two CARs over windows sharing ``overlap`` of ``length`` sessions have
    correlation ``r * overlap / length``, ``r`` being the daily abnormal-return
    correlation estimated on the common estimation sessions. Pairs whose
    windows share nothing contribute zero, so only overlapping pairs are read.
    """
    n = len(members)
    if n < 2:
        return float("nan"), 0, 0
    order = np.argsort(starts, kind="mergesort")
    sorted_starts = starts[order]
    total, pairs, unestimated = 0.0, 0, 0
    for a_idx in range(n):
        stop = int(np.searchsorted(sorted_starts, sorted_starts[a_idx] + length, side="left"))
        mi = members[order[a_idx]]
        for b_idx in range(a_idx + 1, stop):
            mj = members[order[b_idx]]
            overlap = min(int(sorted_starts[a_idx] + length - sorted_starts[b_idx]), length)
            pairs += 1
            r = _residual_correlation(mi, mj, cache)
            if r is None:
                unestimated += 1
                continue
            total += r * mi.sign * mj.sign * overlap / length
    return 2.0 * total / (n * (n - 1)), pairs, unestimated


def _cell(members: list[_Measured], label: str, window: tuple[int, int], *,
          cluster_freq: str, n_boot: int, boot_level: float, seed: int | None,
          pair_cache: dict) -> dict:
    cars = np.array([m.sign * m.windows[label][0] for m in members], dtype=float)
    scars = np.array([m.sign * m.windows[label][2] for m in members], dtype=float)
    dofs = np.array([m.fit.observations - 2 for m in members], dtype=float)
    clusters = [m.cluster for m in members]
    n = cars.size
    row: dict[str, Any] = {"n": n}
    row["mean_car"] = float(cars.mean()) if n else float("nan")
    row["median_car"] = float(np.median(cars)) if n else float("nan")
    row["sd_car"] = float(cars.std(ddof=1)) if n > 1 else float("nan")
    row["positive_fraction"] = float(np.mean(cars > 0)) if n else float("nan")

    if n > 1 and row["sd_car"] > 0:
        t_value = row["mean_car"] / (row["sd_car"] / math.sqrt(n))
        row["t"], row["p_t"] = t_value, _two_sided_t(t_value, n - 1)
    else:
        row["t"] = row["p_t"] = float("nan")
    row["t_clustered"], row["p_t_clustered"], row["n_clusters"] = clustered_t_test(cars, clusters)

    finite = np.isfinite(scars)
    corrections = np.where(dofs > 2, dofs / np.maximum(dofs - 2, 1e-12), np.nan)
    variance = float(np.nansum(corrections[finite]))
    patell = float(scars[finite].sum() / math.sqrt(variance)) if finite.any() and variance > 0 else float("nan")
    row["patell_z"], row["p_patell"] = patell, _two_sided_normal(patell)
    if finite.sum() > 1 and np.std(scars[finite], ddof=1) > 0:
        s = scars[finite]
        bmp = float(s.mean() / (s.std(ddof=1) / math.sqrt(s.size)))
        row["bmp_t"], row["p_bmp"] = bmp, _two_sided_t(bmp, s.size - 1)
    else:
        bmp = float("nan")
        row["bmp_t"] = row["p_bmp"] = float("nan")

    starts = np.array([m.day0_pos + window[0] for m in members], dtype=np.int64)
    length = window[1] - window[0] + 1
    rbar, pairs, unestimated = _kp_rbar(members, starts, length, pair_cache)
    rbar_used = rbar if math.isfinite(rbar) else 0.0
    row["kp_rbar"], row["kp_pairs"], row["kp_pairs_unestimated"] = rbar_used, pairs, unestimated
    bmp_factor = kolari_pynnonen_factor(n, rbar_used, statistic="bmp")
    patell_factor = kolari_pynnonen_factor(n, rbar_used, statistic="patell")
    row["bmp_kp_t"] = bmp * bmp_factor if math.isfinite(bmp_factor) else float("nan")
    row["p_bmp_kp"] = _two_sided_t(row["bmp_kp_t"], n - 1)
    row["patell_kp_z"] = patell * patell_factor if math.isfinite(patell_factor) else float("nan")
    row["p_patell_kp"] = _two_sided_normal(row["patell_kp_z"])

    baseline = [float(np.mean(m.sign * m.est_resid > 0)) for m in members if m.est_resid.size]
    row["sign_baseline"] = float(np.mean(baseline)) if baseline else float("nan")
    row["sign_z"], row["p_sign"] = generalized_sign_test(cars, row["sign_baseline"])

    sars = [m.standardized_estimation() for m in members]
    row["grank_t"], row["p_grank"], row["grank_periods"] = grank_test(sars, scars)

    lower, upper, _ = cluster_bootstrap_ci(cars, clusters, n_boot=n_boot, level=boot_level, seed=seed)
    row["boot_lower"], row["boot_upper"], row["boot_level"] = lower, upper, boot_level
    return row


def run_event_study(
    prices: pd.DataFrame,
    events: pd.DataFrame,
    *,
    benchmark: str | pd.Series,
    windows: Sequence[Sequence[int]] = DEFAULT_CAR_WINDOWS,
    estimation_window: Sequence[int] = DEFAULT_ESTIMATION_WINDOW,
    min_estimation_days: int = DEFAULT_MIN_ESTIMATION_DAYS,
    exclusion_window: Sequence[int] | None = DEFAULT_EXCLUSION_WINDOW,
    firm_col: str = "firm",
    time_col: str = "knowledge_time",
    id_col: str = "event_id",
    period_col: str | None = "period",
    group_col: str | None = None,
    signed_col: str | None = None,
    session_closes: pd.Series | None = None,
    cluster_freq: str = "W",
    n_boot: int = 2000,
    boot_level: float = 0.95,
    seed: int | None = 20260929,
    fdr: float = 0.10,
    fdr_test: str = "bmp_kp",
) -> EventStudyReport:
    """Measure CARs for dated events whose day 0 comes from their knowledge time.

    Args:
        prices: Closing prices, one column per firm (and optionally the
            benchmark), indexed by session date. Never forward-filled: a
            missing close is missing.
        events: One row per event with ``id_col``, ``firm_col`` and
            ``time_col`` (the instant the market could first know it; naive
            values are UTC), plus optional ``period_col``, ``group_col`` and
            ``signed_col``.
        benchmark: Column of ``prices`` holding the benchmark, or a price
            Series. Its finite sessions are the event-time calendar.
        windows: Inclusive ``(start, end)`` relative-session windows. Each must
            start after the estimation window ends.
        estimation_window: Inclusive ``(start, end)`` relative sessions of the
            market-model fit, e.g. ``(-250, -11)``.
        min_estimation_days: Fewest usable estimation sessions (>= 30).
        exclusion_window: Sessions around each of the firm's *other* events
            removed from the estimation sample; ``None`` keeps them.
        firm_col: Firm column (must match a ``prices`` column).
        time_col: Knowledge-time column.
        id_col: Unique event id column (created from the row order if absent).
        period_col: Firm-period key for :func:`dedupe_events`; ``None`` or an
            absent column keeps every event.
        group_col: Optional grouping (e.g. a SUE bucket). ``"ALL"`` is always
            reported as well.
        signed_col: Optional surprise column; adds a ``"SIGNED"`` group whose
            abnormal returns are multiplied by the surprise sign (zero and
            missing surprises are left out of it).
        session_closes: Close instant per session (default
            :func:`session_close_times` of the calendar).
        cluster_freq: Calendar period that clusters day-0 dates for the
            clustered t and the bootstrap: ``"D"``, ``"W"``, ``"M"`` or ``"Q"``.
        n_boot: Cluster-bootstrap replicates.
        boot_level: Bootstrap interval coverage.
        seed: Bootstrap seed.
        fdr: Benjamini-Hochberg false-discovery rate.
        fdr_test: Statistic whose p-values enter Benjamini-Hochberg, one of
            :data:`FDR_TESTS`.

    Returns:
        An :class:`EventStudyReport`.

    Raises:
        ValueError: On inconsistent windows, an unknown option, a missing
            column, or an unusable benchmark. Unmeasurable events are never an
            error; they are reported in ``skips``.
    """
    est_lo, est_hi = (int(v) for v in estimation_window)
    wins = [(int(s), int(e)) for s, e in windows]
    if not wins:
        raise ValueError("windows is empty")
    if est_lo >= est_hi:
        raise ValueError(f"estimation_window must be (start, end) with start < end, got {estimation_window}")
    for s, e in wins:
        if s > e:
            raise ValueError(f"window {(s, e)} has start > end")
        if s <= est_hi:
            raise ValueError(f"window {(s, e)} overlaps the estimation window ending at {est_hi}")
    if len(set(wins)) != len(wins):
        raise ValueError("windows contains a duplicate")
    if min_estimation_days < 30:
        raise ValueError("min_estimation_days must be at least 30")
    if cluster_freq not in _CLUSTER_FREQS:
        raise ValueError(f"cluster_freq must be one of {_CLUSTER_FREQS}")
    if fdr_test not in FDR_TESTS:
        raise ValueError(f"fdr_test must be one of {FDR_TESTS}")
    if exclusion_window is not None:
        exc_lo, exc_hi = (int(v) for v in exclusion_window)
        if exc_lo > exc_hi:
            raise ValueError("exclusion_window must be (start, end) with start <= end")
    for column in (firm_col, time_col):
        if column not in events.columns:
            raise ValueError(f"events has no {column!r} column")
    for column in (group_col, signed_col):
        if column is not None and column not in events.columns:
            raise ValueError(f"events has no {column!r} column")

    frame = _coerce_prices(prices)
    if isinstance(benchmark, str):
        if benchmark not in frame.columns:
            raise ValueError(f"benchmark {benchmark!r} is not a prices column")
        bench = frame[benchmark]
    else:
        bench = _coerce_prices(benchmark.to_frame("__benchmark__"))["__benchmark__"]
    calendar = pd.DatetimeIndex(bench.index[np.isfinite(bench.to_numpy())])
    if len(calendar) < 3:
        raise ValueError("the benchmark has fewer than three finite closes")
    bench_px = bench.reindex(calendar).to_numpy(dtype=float)
    market = np.full(len(calendar), np.nan)
    market[1:] = bench_px[1:] / bench_px[:-1] - 1.0
    closes = session_close_times(calendar) if session_closes is None else session_closes
    closes = pd.Series(closes).reindex(calendar)
    if closes.isna().any():
        raise ValueError("session_closes does not cover every benchmark session")

    ev = events.copy().reset_index(drop=True)
    if id_col not in ev.columns:
        ev[id_col] = [f"e{i}" for i in range(len(ev))]
    if ev[id_col].duplicated().any():
        raise ValueError(f"{id_col} values must be unique")
    ev[firm_col] = ev[firm_col].astype(str)
    ev["_kt"] = _as_utc_index(ev[time_col]).to_numpy()

    skips: list[dict] = []
    missing_kt = ev["_kt"].isna()
    for _, row in ev[missing_kt].iterrows():
        skips.append({"event_id": row[id_col], "firm": row[firm_col], "window": "*",
                      "reason": "missing knowledge time"})
    ev = ev[~missing_kt]
    ev["_pos"] = first_session_after(ev["_kt"], closes)

    if period_col is not None and period_col in ev.columns:
        kept, duplicates = dedupe_events(ev, firm_col=firm_col, period_col=period_col,
                                         time_col="_kt", id_col=id_col)
    else:
        kept, duplicates = ev, ev.iloc[0:0].assign(kept_event_id=pd.Series(dtype=object))

    # Every known event date of a firm (duplicates included) is an information
    # day the estimation windows of its other events must not contain.
    firm_positions: dict[str, np.ndarray] = {
        firm: np.sort(group["_pos"][group["_pos"] >= 0].to_numpy(dtype=np.int64))
        for firm, group in ev.groupby(firm_col)
    }
    returns: dict[str, np.ndarray] = {}
    path_lo = min(s for s, _ in wins)
    path_hi = max(e for _, e in wins)
    measured: list[_Measured] = []
    n_sessions = len(calendar)

    for _, row in kept.iterrows():
        event_id, firm, pos = row[id_col], row[firm_col], int(row["_pos"])
        if firm not in frame.columns:
            skips.append({"event_id": event_id, "firm": firm, "window": "*",
                          "reason": "firm not in the price panel"})
            continue
        if pos < 0:
            skips.append({"event_id": event_id, "firm": firm, "window": "*",
                          "reason": "no session closes after the knowledge time in the panel"})
            continue
        if firm not in returns:
            px = frame[firm].reindex(calendar).to_numpy(dtype=float)
            r = np.full(n_sessions, np.nan)
            r[1:] = px[1:] / px[:-1] - 1.0
            returns[firm] = r
        r = returns[firm]
        lo, hi = pos + est_lo, pos + est_hi
        if hi < 1:
            skips.append({"event_id": event_id, "firm": firm, "window": "*",
                          "reason": "insufficient history before the estimation window"})
            continue
        est = np.arange(max(lo, 1), hi + 1)
        if exclusion_window is not None:
            others = firm_positions.get(firm, np.array([], dtype=np.int64))
            others = others[others != pos]
            if others.size:
                bad = (est[:, None] >= others[None, :] + exc_lo) & (est[:, None] <= others[None, :] + exc_hi)
                est = est[~bad.any(axis=1)]
        usable = est[np.isfinite(r[est]) & np.isfinite(market[est])]
        if usable.size < min_estimation_days:
            skips.append({"event_id": event_id, "firm": firm, "window": "*",
                          "reason": f"insufficient estimation data ({usable.size} sessions, "
                                    f"need {min_estimation_days})"})
            continue
        try:
            fit = estimate_market_model(r[usable], market[usable], model="market")
        except ValueError as exc:
            skips.append({"event_id": event_id, "firm": firm, "window": "*",
                          "reason": f"market model not estimable: {exc}"})
            continue
        resid = r[usable] - (fit.alpha + fit.beta * market[usable])
        sign = 0
        if signed_col is not None:
            value = pd.to_numeric(pd.Series([row[signed_col]]), errors="coerce").iloc[0]
            sign = int(np.sign(value)) if pd.notna(value) else 0
        group = None if group_col is None or pd.isna(row[group_col]) else str(row[group_col])
        item = _Measured(event_id=event_id, firm=firm, knowledge_time=pd.Timestamp(row["_kt"]),
                         day0_pos=pos, group=group, sign=sign,
                         cluster=_cluster_key(calendar[pos], cluster_freq), fit=fit,
                         est_pos=usable.astype(np.int64), est_resid=resid, key=len(measured))
        for k in range(path_lo, path_hi + 1):
            at = pos + k
            if at < n_sessions and np.isfinite(r[at]) and np.isfinite(market[at]):
                item.path_ar[k] = float(r[at] - (fit.alpha + fit.beta * market[at]))
        for s, e in wins:
            label = window_label((s, e))
            if pos + e >= n_sessions:
                skips.append({"event_id": event_id, "firm": firm, "window": label,
                              "reason": "window incomplete: it runs past the last session"})
                continue
            span = np.arange(pos + s, pos + e + 1)
            if not (np.isfinite(r[span]).all() and np.isfinite(market[span]).all()):
                skips.append({"event_id": event_id, "firm": firm, "window": label,
                              "reason": "missing price in the window"})
                continue
            ar = r[span] - (fit.alpha + fit.beta * market[span])
            car = float(ar.sum())
            se = _car_std_error(fit, market[span])
            item.windows[label] = (car, se, car / se if se > 0 else float("nan"))
        measured.append(item)

    event_rows = []
    for m in measured:
        for s, e in wins:
            label = window_label((s, e))
            if label not in m.windows:
                continue
            car, se, scar = m.windows[label]
            event_rows.append({
                "event_id": m.event_id, "firm": m.firm, "knowledge_time": m.knowledge_time,
                "day0": calendar[m.day0_pos], "window": label, "group": m.group,
                "surprise_sign": m.sign if signed_col is not None else None,
                "alpha": m.fit.alpha, "beta": m.fit.beta, "residual_std": m.fit.residual_std,
                "estimation_days": m.fit.observations, "car": car, "car_se": se, "scar": scar,
            })
    event_frame = pd.DataFrame(event_rows, columns=[
        "event_id", "firm", "knowledge_time", "day0", "window", "group", "surprise_sign",
        "alpha", "beta", "residual_std", "estimation_days", "car", "car_se", "scar"])

    group_names = [_ALL]
    if group_col is not None:
        group_names += sorted({m.group for m in measured if m.group is not None})
    if signed_col is not None:
        group_names.append(_SIGNED)

    def members_of(name: str, label: str) -> list[_Measured]:
        if name == _ALL:
            chosen = [m for m in measured if label in m.windows]
        elif name == _SIGNED:
            chosen = [m for m in measured if label in m.windows and m.sign != 0]
        else:
            chosen = [m for m in measured if label in m.windows and m.group == name]
        if name == _SIGNED:
            return chosen
        # Only the SIGNED cell flips abnormal returns; everywhere else sign = +1.
        return [_Measured(**{**m.__dict__, "sign": 1}) for m in chosen]

    agg_rows = []
    pair_cache: dict = {}
    for s, e in wins:
        label = window_label((s, e))
        for name in group_names:
            members = members_of(name, label)
            if not members:
                continue
            row = {"window": label, "window_start": s, "window_end": e, "group": name}
            row.update(_cell(members, label, (s, e), cluster_freq=cluster_freq, n_boot=n_boot,
                             boot_level=boot_level, seed=seed, pair_cache=pair_cache))
            agg_rows.append(row)
    aggregates = pd.DataFrame(agg_rows)
    if not aggregates.empty:
        column = {"t": "p_t", "t_clustered": "p_t_clustered", "patell": "p_patell",
                  "patell_kp": "p_patell_kp", "bmp": "p_bmp", "bmp_kp": "p_bmp_kp",
                  "sign": "p_sign", "grank": "p_grank"}[fdr_test]
        aggregates["p_fdr"] = np.nan
        aggregates["fdr_reject"] = False
        finite = aggregates[column].map(lambda v: isinstance(v, float) and math.isfinite(v))
        if finite.any():
            result = benjamini_hochberg(aggregates.loc[finite, column].to_numpy(dtype=float), fdr=fdr)
            aggregates.loc[finite, "p_fdr"] = result.adjusted_p_values
            aggregates.loc[finite, "fdr_reject"] = result.rejected
        aggregates["fdr_test"] = fdr_test

    path_rows = []
    days = list(range(path_lo, path_hi + 1))
    for name in group_names:
        members = [m for m in measured
                   if (name == _ALL or (name == _SIGNED and m.sign != 0) or m.group == name)
                   and all(k in m.path_ar for k in days)]
        if not members:
            continue
        mult = np.array([m.sign if name == _SIGNED else 1 for m in members], dtype=float)
        paths = np.array([[m.path_ar[k] for k in days] for m in members]) * mult[:, None]
        cumulative = np.cumsum(paths, axis=1)
        n = len(members)
        spread = cumulative.std(axis=0, ddof=1) / math.sqrt(n) if n > 1 else np.full(len(days), np.nan)
        for j, k in enumerate(days):
            path_rows.append({"group": name, "rel_day": k, "n": n, "aar": float(paths[:, j].mean()),
                              "caar": float(cumulative[:, j].mean()), "caar_se": float(spread[j])})
    caar_path = pd.DataFrame(path_rows, columns=["group", "rel_day", "n", "aar", "caar", "caar_se"])

    dup_frame = duplicates.rename(columns={id_col: "event_id", firm_col: "firm"})
    keep_cols = [c for c in ("event_id", "firm", period_col, "kept_event_id") if c and c in dup_frame.columns]
    dup_frame = dup_frame[keep_cols].reset_index(drop=True)
    skip_frame = pd.DataFrame(skips, columns=["event_id", "firm", "window", "reason"])
    params = {
        "windows": [window_label(w) for w in wins],
        "estimation_window": [est_lo, est_hi],
        "min_estimation_days": int(min_estimation_days),
        "exclusion_window": list(exclusion_window) if exclusion_window is not None else None,
        "calendar": {"first": str(calendar[0].date()), "last": str(calendar[-1].date()),
                     "sessions": int(len(calendar))},
        "day0_rule": "first session whose close is strictly after the knowledge time",
        "cluster_freq": cluster_freq, "n_boot": int(n_boot), "boot_level": float(boot_level),
        "seed": seed, "fdr": float(fdr), "fdr_test": fdr_test,
        "events_in": int(len(events)), "events_measured": int(len(measured)),
        "duplicates_dropped": int(len(dup_frame)),
    }
    return EventStudyReport(event_frame, aggregates, caar_path, skip_frame, dup_frame, params)


def hit_rates(
    report: EventStudyReport,
    window: Sequence[int] | str,
    *,
    threshold: float = 0.0,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    level: float = 0.90,
) -> pd.DataFrame:
    """Per-group Beta-binomial rate of ``CAR > threshold`` in one window.

    Args:
        report: A :func:`run_event_study` report.
        window: ``(start, end)`` or its label, e.g. ``"[+2,+20]"``.
        threshold: A hit is a CAR strictly above this.
        prior_alpha: Beta prior ``alpha``.
        prior_beta: Beta prior ``beta``.
        level: Credible level.

    Returns:
        One row per group (``"ALL"`` included): ``successes``, ``trials``,
        ``probability``, ``lower``, ``upper`` and the posterior parameters.
    """
    label = window if isinstance(window, str) else window_label(window)
    rows = report.events[report.events["window"] == label]
    out = []
    groups = [(_ALL, rows)]
    groups += [(str(g), part) for g, part in rows.dropna(subset=["group"]).groupby("group")]
    for name, part in groups:
        hits = int((part["car"] > threshold).sum())
        rate = beta_binomial_hit_rate(hits, len(part), prior_alpha=prior_alpha,
                                      prior_beta=prior_beta, level=level)
        out.append({"window": label, "group": name, "threshold": float(threshold),
                    **{k: getattr(rate, k) for k in HitRate.__dataclass_fields__}})
    return pd.DataFrame(out)
