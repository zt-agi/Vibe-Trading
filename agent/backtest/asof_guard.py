"""As-of guard: one executable definition of "no look-ahead" (ZT add-on).

The timing contract every VT backtest engine and the pitdb loader are held to
(``agent/tests/acceptance/test_timing_contract.py`` runs it against each):

(a) A signal dated on bar D fills only at a price from a later bar. The equity
    engines (``BaseEngine`` and every subclass) fill at the next bar's open
    (``"next_open"``); the options engine at the next bar's close
    (``"next_close"``). ``options_config.same_day_fill`` is the one documented
    opt-out (``"same_close"``); the guard refuses it.
(b) Information known at time k only drives a fill executed after k: an 8-K
    filed at 16:30 New York time on D cannot move a fill priced on D.
(c) A basket name without a usable price on its fill bar is never filled at a
    stale or made-up price. Equity engines defer it to its own next bar (hold
    mode also records an ``invalid_price`` plan rejection when the bar exists
    but its open is unusable) or, in rebalance mode, abort the bar before any
    order; the options engine defers the signal to the underlying's next
    valid close.
(d) A decision made before a market holiday executes at the first session
    after it; a signal dated on a non-session day rolls to the next session.
(e) A bar whose session has not closed is never used
    (:func:`drop_unfinished_bars` on every run; the pitdb loader drops its own
    by knowledge time).
(f) Reading data known after a decision cutoff through the guard raises
    :class:`LookAheadError`.

Knowledge time of a row, in order of precedence:

1. a ``knowledge_time`` column (tz-naive values are UTC, pitdb's convention);
2. pitdb provenance in ``frame.attrs["pit"]``: the knowledge time of each
   served revision, or, for frames without it, the formation bound
   ``event_date + availability_lag`` (snapshot mode: the frame's latest
   knowledge time);
3. otherwise the bar itself: a daily bar is known at its session close, an
   intraday bar at its end.

Runtime switch: ``config["asof_guard"]`` (true/false) or the environment
variable ``VIBE_TRADING_ASOF_GUARD`` (on/off). Off by default unless the run
declares ``pit.claim == "tradeable"``; a tradeable claim cannot turn it off.
When on, :func:`guard_run` checks the served snapshot against the run cutoff
and the engine's fill timing, probes the strategy for look-ahead, and wraps
the engine's loader in :class:`AsOfGuard`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

KNOWLEDGE_COLUMN = "knowledge_time"
CONFIG_SWITCH = "asof_guard"
ENV_SWITCH = "VIBE_TRADING_ASOF_GUARD"
FILL_TIMINGS = ("next_open", "next_close", "same_close")
#: Intervals whose bars are stamped with a calendar day (1W/1M are resampled
#: from daily bars and stamped with the last trading day of their period).
DAILY_INTERVALS = frozenset({"1D", "1W", "1M"})
#: Keys of the per-row knowledge record the pitdb loader puts in attrs["pit"].
PIT_ROW_DATES = "row_event_date_ns"
PIT_ROW_KNOWLEDGE = "row_knowledge_time_ns"

_NAT = np.iinfo(np.int64).min
_INTRADAY_SPANS = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1H": pd.Timedelta(hours=1),
    "4H": pd.Timedelta(hours=4),
}
_OHLC = ("open", "high", "low", "close")


class LookAheadError(ValueError):
    """Data known after a decision cutoff reached the decision it drives."""


class AsOfGuardConfigError(ValueError):
    """The guard's switch is malformed or contradicts the run's claim."""


# ---------------------------------------------------------------------------
# Session clocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionClock:
    """When a market's daily session opens and closes.

    Attributes:
        tz: IANA time zone the bar dates are local to.
        open: Session open, local time.
        close: Session close, local time; ``None`` means the session runs to
            local midnight (24-hour markets, and markets whose close is not
            modelled, where it makes the close conservative).
        latency: Shortest time between knowing something and trading on it.
            Zero for 24-hour markets, whose next bar opens at the instant the
            previous one closes.
    """

    tz: str
    open: dt.time
    close: Optional[dt.time]
    latency: pd.Timedelta = pd.Timedelta(minutes=1)

    def open_utc(self, days: Any) -> pd.DatetimeIndex:
        """Session open of each day, in UTC."""
        return _local_to_utc(calendar_days(days), self.open, self.tz)

    def close_utc(self, days: Any) -> pd.DatetimeIndex:
        """Session close of each day, in UTC (next local midnight when open-ended)."""
        dates = calendar_days(days)
        if self.close is None:
            return _local_to_utc(dates + pd.Timedelta(days=1), dt.time(0), self.tz)
        return _local_to_utc(dates, self.close, self.tz)


US_EQUITY = SessionClock("America/New_York", dt.time(9, 30), dt.time(16, 0))
CA_EQUITY = SessionClock("America/Toronto", dt.time(9, 30), dt.time(16, 0))
_MARKET_TZ = {
    "a_share": "Asia/Shanghai",
    "hk_equity": "Asia/Hong_Kong",
    "india_equity": "Asia/Kolkata",
    "kr_equity": "Asia/Seoul",
    "vietnam_equity": "Asia/Ho_Chi_Minh",
    "uk_equity": "Europe/London",
    "ar_equity": "America/Argentina/Buenos_Aires",
}


def clock_for(code: str) -> SessionClock:
    """The session clock a code's daily bars are dated in.

    US equities and indexes close at 16:00 New York time, Canadian equities at
    16:00 Toronto time. Every other market is treated as open until local
    midnight (UTC for crypto, FX, non-Chinese futures and unknown codes),
    which can only make a bar count as finished later than it really is.
    """
    from backtest.engines._market_hooks import (
        _detect_market,
        _is_china_futures,
        strip_local_prefix,
    )

    bare = strip_local_prefix(str(code))
    market = _detect_market(bare)
    if market in ("us_equity", "index"):
        return US_EQUITY
    if market == "ca_equity":
        return CA_EQUITY
    if market == "futures":
        tz = "Asia/Shanghai" if _is_china_futures(bare) else "UTC"
    else:
        tz = _MARKET_TZ.get(market, "UTC")
    return SessionClock(tz, dt.time(0), None, pd.Timedelta(0))


def calendar_days(days: Any) -> pd.DatetimeIndex:
    """Naive, midnight-normalized dates of a scalar, sequence or index.

    A tz-aware stamp keeps its local wall date.
    """
    if isinstance(days, (str, dt.date, np.datetime64)):
        days = [days]
    index = pd.DatetimeIndex(pd.to_datetime(days))
    if index.tz is not None:
        index = index.tz_localize(None)
    return index.normalize()


def _local_to_utc(dates: pd.DatetimeIndex, when: dt.time, tz: str) -> pd.DatetimeIndex:
    offset = pd.Timedelta(hours=when.hour, minutes=when.minute, seconds=when.second)
    local = (dates + offset).tz_localize(tz, ambiguous=False, nonexistent="shift_forward")
    return local.tz_convert("UTC")


def utc_now() -> pd.Timestamp:
    """Wall clock in UTC (patched in tests)."""
    return pd.Timestamp.now(tz="UTC")


def as_utc(value: Any) -> pd.Timestamp:
    """A timestamp in UTC; a tz-naive value is taken as UTC."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _utc_index(values: Any) -> pd.DatetimeIndex:
    """UTC DatetimeIndex of stamps; tz-naive ones are taken as UTC."""
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True))


def _fmt(stamp: Any) -> str:
    if stamp is None or pd.isna(stamp):
        return "n/a"
    return pd.Timestamp(stamp).isoformat()


# ---------------------------------------------------------------------------
# Knowledge time of a row
# ---------------------------------------------------------------------------


def encode_row_knowledge(event_dates: Any, knowledge_times: Any) -> Dict[str, bytes]:
    """Per-row knowledge record for ``attrs["pit"]`` (used by the pitdb loader).

    Packed as bytes: pandas deep-copies ``attrs`` on nearly every operation and
    compares them on ``concat``; bytes copy in O(1) and compare exactly, where
    a list, array or Series would slow every strategy step or break ``concat``.

    Args:
        event_dates: Bar dates (tz-naive).
        knowledge_times: Knowledge time of each bar (tz-naive UTC).
    """
    dates = calendar_days(event_dates).as_unit("ns")
    known = _utc_index(knowledge_times).as_unit("ns")
    if len(dates) != len(known):
        raise ValueError("event_dates and knowledge_times differ in length")
    return {
        PIT_ROW_DATES: np.asarray(dates.asi8, dtype="<i8").tobytes(),
        PIT_ROW_KNOWLEDGE: np.asarray(known.asi8, dtype="<i8").tobytes(),
    }


def decode_row_knowledge(pit: Mapping[str, Any]) -> Optional[pd.Series]:
    """Per-row knowledge times from ``attrs["pit"]``: UTC stamps keyed by bar date."""
    dates, known = pit.get(PIT_ROW_DATES), pit.get(PIT_ROW_KNOWLEDGE)
    if not isinstance(dates, (bytes, bytearray)) or not isinstance(known, (bytes, bytearray)):
        return None
    days = np.frombuffer(dates, dtype="<i8")
    stamps = np.frombuffer(known, dtype="<i8")
    if len(days) != len(stamps):
        return None
    return pd.Series(
        pd.DatetimeIndex(pd.to_datetime(stamps, utc=True)),
        index=pd.DatetimeIndex(pd.to_datetime(days)),
    )


def _is_daily(index: pd.DatetimeIndex, interval: str) -> bool:
    if interval in DAILY_INTERVALS:
        return True
    if interval in _INTRADAY_SPANS:
        return False
    naive = index.tz_localize(None) if index.tz is not None else index
    return bool(len(naive)) and bool((naive == naive.normalize()).all())


def knowledge_times(
    frame: pd.DataFrame,
    *,
    clock: SessionClock = US_EQUITY,
    interval: str = "1D",
) -> Tuple[pd.DatetimeIndex, str]:
    """When each row of ``frame`` became known (see the module docstring).

    Args:
        frame: Bars or events on a ``DatetimeIndex``.
        clock: Session clock for the bar-close fallback.
        interval: Bar interval, for the fallback on intraday bars.

    Returns:
        ``(times, basis)``: UTC knowledge times, one per row in row order
        (``NaT`` where a row has none), and where they came from.
    """
    index = pd.DatetimeIndex(frame.index)
    if KNOWLEDGE_COLUMN in frame.columns:
        return _utc_index(frame[KNOWLEDGE_COLUMN]), "knowledge_time column"

    pit = frame.attrs.get("pit")
    if isinstance(pit, Mapping) and len(index):
        days = calendar_days(index)
        bound, bound_basis = _pit_bound(pit, days)
        rows = decode_row_knowledge(pit)
        if rows is not None:
            known = pd.DatetimeIndex(rows.reindex(days).array)
            if not known.isna().any():
                return known, "pitdb row knowledge time"
            if bound is not None:
                filled = np.where(known.isna(), bound.asi8, known.asi8)
                return _utc_index(filled), f"pitdb row knowledge time, else {bound_basis}"
        if bound is not None:
            return bound, bound_basis

    if _is_daily(index, interval):
        return clock.close_utc(index), "session close of the bar"
    span = _INTRADAY_SPANS.get(interval, pd.Timedelta(0))
    stamps = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return stamps + span, "end of the bar"


def _pit_bound(
    pit: Mapping[str, Any], days: pd.DatetimeIndex
) -> Tuple[Optional[pd.DatetimeIndex], str]:
    """Knowledge bound implied by the pitdb binding when rows carry no time."""
    if pit.get("mode") == "formation":
        try:
            lag = pd.Timedelta(str(pit.get("availability_lag")))
        except (TypeError, ValueError):
            lag = None
        if lag is not None and not pd.isna(lag):
            return (days + lag).tz_localize("UTC"), "pitdb formation bound (event_date + lag)"
    latest = pit.get("max_knowledge_time_utc")
    if latest:
        return (
            pd.DatetimeIndex([as_utc(latest)] * len(days)),
            "pitdb latest knowledge time (snapshot mode)",
        )
    return None, ""


def _is_bar_frame(frame: pd.DataFrame) -> bool:
    return all(column in frame.columns for column in _OHLC)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_rows(
    frame: pd.DataFrame,
    cutoff: Any,
    *,
    code: str = "",
    clock: SessionClock = US_EQUITY,
    interval: str = "1D",
) -> None:
    """Raise if any row was known after ``cutoff``, or is an unfinished bar.

    Args:
        frame: Bars or events.
        cutoff: Decision cutoff; a tz-naive value is UTC.
        code: Symbol, for the message.
        clock: Session clock of the symbol.
        interval: Bar interval.

    Raises:
        LookAheadError: A row's knowledge time is after ``cutoff`` or missing,
            or a bar was captured before its own session closed.
    """
    if frame is None or len(frame) == 0:
        return
    name = code or "frame"
    limit = as_utc(cutoff)
    known, basis = knowledge_times(frame, clock=clock, interval=interval)
    stamps = known.asi8
    late = (stamps == _NAT) | (stamps > limit.value)
    if late.any():
        i = int(np.flatnonzero(late)[0])
        raise LookAheadError(
            f"{name}: {int(late.sum())} row(s) not known by the decision cutoff "
            f"{_fmt(limit)}; first: row {_fmt(frame.index[i])} known {_fmt(known[i])} ({basis})"
        )
    explicit = basis.startswith(("knowledge_time", "pitdb row"))
    if explicit and _is_bar_frame(frame) and _is_daily(pd.DatetimeIndex(frame.index), interval):
        closes = clock.close_utc(frame.index)
        partial = stamps < closes.asi8
        if partial.any():
            i = int(np.flatnonzero(partial)[0])
            raise LookAheadError(
                f"{name}: {int(partial.sum())} unfinished bar(s): row {_fmt(frame.index[i])} "
                f"was captured {_fmt(known[i])}, before its session closed at {_fmt(closes[i])}"
            )


def asof_rows(
    frame: pd.DataFrame,
    cutoff: Any,
    *,
    clock: SessionClock = US_EQUITY,
    interval: str = "1D",
) -> pd.DataFrame:
    """Only the rows of ``frame`` known by ``cutoff`` (a strategy helper)."""
    if frame is None or len(frame) == 0:
        return frame
    known, _ = knowledge_times(frame, clock=clock, interval=interval)
    stamps = known.asi8
    keep = (stamps != _NAT) & (stamps <= as_utc(cutoff).value)
    return frame.loc[keep]


def execution_times(
    index: Any,
    fill_timing: str,
    *,
    clock: SessionClock = US_EQUITY,
    interval: str = "1D",
) -> pd.DatetimeIndex:
    """When the fill driven by the signal on each bar executes (UTC).

    ``next_open`` / ``next_close``: the open / close of the next bar on the
    symbol's own calendar, ``NaT`` on the last bar (that signal is pending).
    ``same_close``: the close of the bar itself. Resampled bars (1W/1M) are
    stamped with the last day of their period, so their next open is taken as
    the calendar day after the stamp, never later than the real one.
    """
    if fill_timing not in FILL_TIMINGS:
        raise ValueError(f"fill_timing must be one of {FILL_TIMINGS}, got {fill_timing!r}")
    days = calendar_days(index)
    if fill_timing == "same_close":
        return clock.close_utc(days)
    if fill_timing == "next_open" and interval in ("1W", "1M"):
        stamps = clock.open_utc(days + pd.Timedelta(days=1)).asi8.copy()
    else:
        following = days[1:]
        nxt = clock.open_utc(following) if fill_timing == "next_open" else clock.close_utc(following)
        stamps = np.concatenate([nxt.asi8, np.array([_NAT], dtype=np.int64)])
    if len(stamps):
        stamps[-1] = _NAT
    return _utc_index(stamps)


def check_fill_timing(
    frame: pd.DataFrame,
    fill_timing: str,
    *,
    code: str = "",
    clock: SessionClock = US_EQUITY,
    interval: str = "1D",
) -> None:
    """Raise if a signal on some bar could read data known only after its fill.

    A signal on bar t may read every row up to t, so the latest knowledge time
    among those rows must precede the fill the signal drives by the clock's
    latency.

    Raises:
        LookAheadError: ``same_close`` (a close cannot be traded by a decision
            that saw it), or a row known too late for the fill its bar drives
            (e.g. a formation lag longer than the gap to the next open).
    """
    if frame is None or len(frame) == 0:
        return
    name = code or "frame"
    if fill_timing == "same_close":
        raise LookAheadError(
            f"{name}: fill timing 'same_close' (options_config.same_day_fill) prices a "
            "signal at the close of the bar it is dated on, which that signal already "
            "saw. Date the signal on the bar whose data it uses and drop same_day_fill."
        )
    known, basis = knowledge_times(frame, clock=clock, interval=interval)
    seen = np.maximum.accumulate(known.asi8)
    fills = execution_times(frame.index, fill_timing, clock=clock, interval=interval).asi8
    valid = fills != _NAT
    deadline = np.where(valid, fills - clock.latency.value, _NAT)
    bad = valid & (seen > deadline)
    if bad.any():
        i = int(np.flatnonzero(bad)[0])
        raise LookAheadError(
            f"{name}: the signal on bar {_fmt(frame.index[i])} fills at "
            f"{_fmt(pd.Timestamp(fills[i], tz='UTC'))} ({fill_timing}) but may read data known "
            f"at {_fmt(pd.Timestamp(seen[i], tz='UTC'))} ({basis}); {int(bad.sum())} bar(s) affected"
        )


def decision_bars(
    known_at: Any,
    bar_index: Any,
    *,
    fill_timing: str = "next_open",
    clock: SessionClock = US_EQUITY,
    interval: str = "1D",
) -> pd.DatetimeIndex:
    """The first bar whose signal may act on information known at each time.

    The strategy-side half of the contract: date an event-driven signal on the
    bar this returns and the engine fills it at the earliest moment that is
    not look-ahead. An 8-K known at 16:30 New York time on D goes on bar D for
    the next-open equity engines (filled at D+1's open) and for the next-close
    options engine (filled at D+1's close); a release before the open of D
    goes on bar D-1, since D's open already knew it.

    Args:
        known_at: Knowledge times (tz-naive values are UTC).
        bar_index: The symbol's bars.
        fill_timing: The engine's fill timing.
        clock: Session clock.
        interval: Bar interval.

    Returns:
        One bar stamp per knowledge time; ``NaT`` when no bar in
        ``bar_index`` can still act on it (the decision is pending).
    """
    bars = pd.DatetimeIndex(bar_index)
    known = _utc_index(known_at)
    if not len(bars):
        return pd.DatetimeIndex([pd.NaT] * len(known))
    fills = execution_times(bars, fill_timing, clock=clock, interval=interval).asi8
    valid = fills != _NAT
    deadline = fills[valid] - clock.latency.value
    candidates = bars[valid]
    positions = np.searchsorted(deadline, known.asi8, side="left")
    return pd.DatetimeIndex([
        candidates[pos] if stamp != _NAT and pos < len(deadline) else pd.NaT
        for pos, stamp in zip(positions, known.asi8)
    ])


# ---------------------------------------------------------------------------
# The loader proxy
# ---------------------------------------------------------------------------


class AsOfGuard:
    """A loader proxy that refuses rows known after its cutoff.

    Wraps any VT loader (``fetch(codes, start_date, end_date, ...)``) and
    checks every frame it returns with :func:`check_rows` against ``cutoff``
    and, for the codes whose bars drive fills, with :func:`check_fill_timing`.
    Every other attribute is the wrapped loader's.

    Args:
        loader: The loader to wrap.
        cutoff: Decision cutoff (tz-naive: UTC); ``None`` skips the row check.
        fill_timing: Engine fill timing; ``None`` skips the fill check.
        codes: Codes whose bars drive fills; ``None`` means every code. A code
            outside it (a benchmark) is not checked at all, since benchmark
            fetches swallow errors and nothing trades on it.
        interval: Bar interval.
        clock: Session clock for every code; ``None`` picks one per code with
            :func:`clock_for`.
    """

    def __init__(
        self,
        loader: Any,
        cutoff: Any = None,
        *,
        fill_timing: Optional[str] = None,
        codes: Optional[Sequence[str]] = None,
        interval: str = "1D",
        clock: Optional[SessionClock] = None,
    ) -> None:
        if fill_timing is not None and fill_timing not in FILL_TIMINGS:
            raise ValueError(f"fill_timing must be one of {FILL_TIMINGS}, got {fill_timing!r}")
        self.__dict__["_loader"] = loader
        self.cutoff = as_utc(cutoff) if cutoff is not None else None
        self.fill_timing = fill_timing
        self.codes = None if codes is None else {str(c) for c in codes}
        self.interval = interval
        self.clock = clock
        self.checked: List[str] = []

    @property
    def wrapped(self) -> Any:
        """The loader behind the guard."""
        return self.__dict__["_loader"]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or "_loader" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.__dict__["_loader"], name)

    def fetch(self, codes, start_date, end_date, *args, **kwargs):
        """Fetch through the wrapped loader and check what comes back."""
        frames = self.wrapped.fetch(codes, start_date, end_date, *args, **kwargs)
        interval = str(kwargs.get("interval") or self.interval)
        for code, frame in (frames or {}).items():
            if self.codes is None or str(code) in self.codes:
                self.check(code, frame, interval=interval)
        return frames

    def check(self, code: str, frame: pd.DataFrame, *, interval: Optional[str] = None) -> None:
        """Check one frame; raises :class:`LookAheadError`."""
        interval = interval or self.interval
        clock = self.clock or clock_for(code)
        if self.cutoff is not None:
            check_rows(frame, self.cutoff, code=code, clock=clock, interval=interval)
        if self.fill_timing is not None:
            check_fill_timing(frame, self.fill_timing, code=code, clock=clock, interval=interval)
        self.checked.append(str(code))


# ---------------------------------------------------------------------------
# Strategy probe
# ---------------------------------------------------------------------------


def _copy_map(data_map: Mapping[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    return {code: frame.copy() for code, frame in data_map.items()}


def _effective(values: Any) -> pd.Series:
    """What an equity engine does with a signal: NaN is flat, then clipped."""
    series = values if isinstance(values, pd.Series) else pd.Series(dtype="float64")
    numeric = pd.to_numeric(series, errors="coerce").astype("float64")
    return numeric.fillna(0.0).clip(-1.0, 1.0)


def _upto(series: pd.Series, cut: Optional[pd.Timestamp]) -> pd.Series:
    if cut is None:
        return series
    try:
        return series[pd.DatetimeIndex(series.index) <= cut]
    except (TypeError, ValueError):
        return series


def _signal_date(signal: Any) -> Optional[pd.Timestamp]:
    if not isinstance(signal, Mapping):
        return None
    try:
        stamp = pd.Timestamp(signal.get("date"))
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp


def _compare(full: Any, part: Any, cut: Optional[pd.Timestamp], *, rtol: float, atol: float) -> Optional[str]:
    """Why ``full`` and ``part`` disagree on bars up to ``cut`` (None: they agree)."""
    if isinstance(full, Mapping):
        part = part if isinstance(part, Mapping) else {}
        for code in sorted(set(full) | set(part), key=str):
            left = _effective(_upto(full.get(code, pd.Series(dtype="float64")), cut))
            right = _effective(_upto(part.get(code, pd.Series(dtype="float64")), cut))
            union = left.index.union(right.index)
            a = left.reindex(union).fillna(0.0).to_numpy()
            b = right.reindex(union).fillna(0.0).to_numpy()
            diff = ~np.isclose(a, b, rtol=rtol, atol=atol)
            if diff.any():
                i = int(np.flatnonzero(diff)[0])
                where = f"once bars after {_fmt(cut)} are removed" if cut is not None else "on a rerun"
                return f"{code}: the signal dated {_fmt(union[i])} is {a[i]:g} on the full data but {b[i]:g} {where}"
        return None
    if isinstance(full, (list, tuple)):
        def upto(signals: Any) -> List[str]:
            kept = []
            for signal in signals or []:
                when = _signal_date(signal)
                if when is not None and (cut is None or when <= cut):
                    kept.append(json.dumps(signal, sort_keys=True, default=str))
            return sorted(kept)

        left = upto(full)
        right = upto(part if isinstance(part, (list, tuple)) else [])
        if left != right:
            only = sorted(set(left) ^ set(right))
            return (
                f"the signals dated up to {_fmt(cut)} differ once later bars are removed "
                f"(e.g. {only[0] if only else 'their order'})"
            )
    return None


def probe_signal_lookahead(
    generate: Callable[[Dict[str, pd.DataFrame]], Any],
    data_map: Mapping[str, pd.DataFrame],
    *,
    fractions: Sequence[float] = (0.5, 0.75, 0.9),
    min_history: int = 20,
    rtol: float = 1e-9,
    atol: float = 1e-12,
) -> Dict[str, Any]:
    """Prefix probe: a signal dated t must not change when bars after t are removed.

    Runs ``generate`` on the full data and on the data truncated at a few
    cutoffs. A strategy that reads a later bar (``shift(-1)``, a centred
    window, a whole-sample mean or fit) gives a different signal on some bar
    the truncated run already covered. A cutoff on which the strategy cannot
    run at all (too little history for it) is skipped, not failed.

    It is a detector, not a proof: a peek that only changes a rare signal
    (one event a year) shows up only if a cutoff falls just before it.
    Whole-sample statistics and features built on later bars change signals
    everywhere and are caught at any cutoff.

    Args:
        generate: ``data_map -> signals`` (a fresh strategy per call is best).
        data_map: The run's bars; never mutated (each call gets copies).
        fractions: Where the cutoffs fall, as fractions of the bar calendar.
        min_history: Fewest bars before a cutoff.
        rtol: Relative tolerance.
        atol: Absolute tolerance.

    Returns:
        ``{"status", "cutoffs"}``; status ``"skipped: ..."`` when the strategy
        is not deterministic, the data is too short, or no cutoff could run.

    Raises:
        LookAheadError: A signal changed on a bar the truncated run covered.
    """
    full = generate(_copy_map(data_map))
    if _compare(full, generate(_copy_map(data_map)), None, rtol=rtol, atol=atol) is not None:
        logger.warning("as-of probe skipped: the strategy returns different signals on identical data")
        return {"status": "skipped: the strategy is not deterministic", "cutoffs": []}
    dates = pd.DatetimeIndex(sorted({stamp for frame in data_map.values() for stamp in frame.index}))
    last = len(dates) - 2
    picks = sorted({
        int(round(fraction * (len(dates) - 1))) for fraction in fractions
        if min_history <= int(round(fraction * (len(dates) - 1))) <= last
    })
    if not picks:
        return {"status": "skipped: too few bars", "cutoffs": []}
    probed: List[str] = []
    for cut in (dates[i] for i in picks):
        truncated = {
            code: frame.loc[pd.DatetimeIndex(frame.index) <= cut].copy()
            for code, frame in data_map.items()
        }
        truncated = {code: frame for code, frame in truncated.items() if len(frame)}
        try:
            part = generate(truncated)
        except Exception as exc:  # noqa: BLE001 - a prefix the strategy cannot run on proves nothing
            logger.warning("as-of probe: the strategy failed on bars up to %s (%s); cutoff skipped", cut, exc)
            continue
        reason = _compare(full, part, cut, rtol=rtol, atol=atol)
        if reason is not None:
            raise LookAheadError(
                f"{reason}: the strategy reads data from after the bar a signal is dated on "
                "(e.g. shift(-1), a centred window, or a whole-sample statistic)"
            )
        probed.append(_fmt(cut))
    if not probed:
        return {"status": "skipped: the strategy could not run on any prefix", "cutoffs": []}
    return {"status": "passed", "cutoffs": probed}


# ---------------------------------------------------------------------------
# Unfinished bars (default path, every run)
# ---------------------------------------------------------------------------


def drop_unfinished_bars(
    data_map: Mapping[str, pd.DataFrame],
    interval: str,
    now: Any = None,
) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
    """Drop daily bars whose session had not closed at ``now``.

    A loader asked for bars through today serves today's bar while the session
    is still running (yfinance does, with the last trade as its close); used
    as a finished bar it becomes a signal input and the final mark. Frames
    with pitdb provenance are left alone: the pitdb loader drops its own
    unfinished bars by knowledge time.

    Args:
        data_map: ``code -> frame`` as served.
        interval: Interval the loader was asked for; only daily bars are trimmed.
        now: Reference time (default: the wall clock).

    Returns:
        The trimmed map and one note per trimmed code.
    """
    if interval not in DAILY_INTERVALS:
        return dict(data_map), []
    reference = as_utc(now) if now is not None else utc_now()
    out: Dict[str, pd.DataFrame] = {}
    notes: List[str] = []
    for code, frame in data_map.items():
        if (
            frame is None
            or len(frame) == 0
            or not isinstance(frame.index, pd.DatetimeIndex)
            or isinstance(frame.attrs.get("pit"), Mapping)
        ):
            out[code] = frame
            continue
        unfinished = clock_for(code).close_utc(frame.index).asi8 > reference.value
        if unfinished.any():
            dropped = frame.index[unfinished]
            out[code] = frame.loc[~unfinished]
            notes.append(
                f"{code}: dropped {len(dropped)} bar(s) whose session had not closed at "
                f"{_fmt(reference)} ({', '.join(str(d.date()) for d in dropped[:3])})"
            )
        else:
            out[code] = frame
    for note in notes:
        logger.warning("unfinished bar: %s", note)
    return out, notes


# ---------------------------------------------------------------------------
# Runtime switch
# ---------------------------------------------------------------------------


def _declared_claim(config: Mapping[str, Any]) -> Optional[str]:
    pit = config.get("pit")
    return pit.get("claim") if isinstance(pit, Mapping) else None


def guard_enabled(config: Mapping[str, Any]) -> bool:
    """Whether the run executes under the as-of guard.

    ``config["asof_guard"]`` wins over ``VIBE_TRADING_ASOF_GUARD``; with
    neither, the guard is on only for ``pit.claim == "tradeable"``.

    Raises:
        AsOfGuardConfigError: A tradeable claim with the guard switched off,
            or an unreadable switch value.
    """
    tradeable = _declared_claim(config) == "tradeable"
    explicit = config.get(CONFIG_SWITCH)
    if explicit is not None and not isinstance(explicit, bool):
        raise AsOfGuardConfigError(f"{CONFIG_SWITCH} must be true or false, got {explicit!r}")
    if explicit is None:
        from src.config.accessor import get_env_value

        raw = get_env_value(ENV_SWITCH, "").strip().lower()
        if raw in ("1", "true", "on", "yes"):
            explicit = True
        elif raw in ("0", "false", "off", "no"):
            explicit = False
        elif raw:
            raise AsOfGuardConfigError(f"{ENV_SWITCH} must be on or off, got {raw!r}")
    if explicit is False and tradeable:
        raise AsOfGuardConfigError(
            "a tradeable claim runs under the as-of guard; remove the switch that turns "
            "it off, or declare claim='research'"
        )
    return tradeable if explicit is None else explicit


def run_cutoff(config: Mapping[str, Any]) -> pd.Timestamp:
    """What the run may know: ``pit.run_asof_utc``, else the wall clock."""
    pit = config.get("pit")
    if isinstance(pit, Mapping) and pit.get("run_asof_utc"):
        return as_utc(pit["run_asof_utc"])
    return utc_now()


def guard_run(
    config: Dict[str, Any],
    data_map: Mapping[str, pd.DataFrame],
    loader: Any,
    *,
    fill_timing: str,
    signal_factory: Optional[Callable[[], Any]] = None,
) -> Any:
    """Runner hook: check the served snapshot and return the engine's loader.

    Off (the loader comes back unchanged) unless :func:`guard_enabled`. On, it
    checks every served frame against the run cutoff and the fill timing, runs
    :func:`probe_signal_lookahead` on the strategy, records the outcome in the
    run card's ``pit`` block when there is one, and returns the loader wrapped
    in :class:`AsOfGuard`.

    Raises:
        LookAheadError: The snapshot or the strategy fails a check.
        AsOfGuardConfigError: The switch is malformed or contradicts the claim.
    """
    if not guard_enabled(config):
        return loader
    codes = [str(code) for code in (config.get("codes") or list(data_map))]
    interval = str(config.get("interval") or "1D")
    guard = AsOfGuard(
        loader, run_cutoff(config), fill_timing=fill_timing, codes=codes, interval=interval
    )
    for code, frame in data_map.items():
        guard.check(code, frame, interval=interval)
    probe: Dict[str, Any] = {"status": "not run", "cutoffs": []}
    if signal_factory is not None:
        probe = probe_signal_lookahead(lambda frames: signal_factory().generate(frames), data_map)
    summary = (
        f"passed: cutoff {_fmt(guard.cutoff)}, fill timing {fill_timing}, "
        f"{len(data_map)} symbol(s) checked, strategy probe {probe['status']}"
    )
    logger.info("as-of guard %s", summary)
    served = config.get("_run_card_pit")
    if isinstance(served, dict):
        served["asof_guard"] = summary
    return guard


__all__ = [
    "AsOfGuard",
    "AsOfGuardConfigError",
    "CA_EQUITY",
    "CONFIG_SWITCH",
    "ENV_SWITCH",
    "FILL_TIMINGS",
    "KNOWLEDGE_COLUMN",
    "LookAheadError",
    "SessionClock",
    "US_EQUITY",
    "as_utc",
    "asof_rows",
    "calendar_days",
    "check_fill_timing",
    "check_rows",
    "clock_for",
    "decision_bars",
    "decode_row_knowledge",
    "drop_unfinished_bars",
    "encode_row_knowledge",
    "execution_times",
    "guard_enabled",
    "guard_run",
    "knowledge_times",
    "probe_signal_lookahead",
    "run_cutoff",
    "utc_now",
]
