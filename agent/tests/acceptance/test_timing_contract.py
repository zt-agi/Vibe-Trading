"""ZT add-on: the timing contract ("no look-ahead") over every VT engine that
can run a US-equity daily backtest, and over the pitdb loader.

Engines: ``GlobalEquityEngine`` (what every US-equity source routes to),
``CompositeEngine`` (the same codes inside a cross-market run) and the options
engine (``engine="options"`` on US underlyings). Sources: an in-memory loader
and an in-memory pitdb warehouse (formation mode, ``claim="tradeable"``, so
the runtime as-of guard is on for every pitdb case). Everything runs through
the runner's fetch path and ``guard_run``, as ``runner.main`` does.

The contract (``backtest/asof_guard.py`` states it):
(a) a signal computed from bar D's close never fills at a price from bar D;
(b) an event known only after D's close (an 8-K at 16:30 New York time) cannot
    affect D's fill -- a negative control against a run without the event;
(c) a missing / NaN / zero fill price for one basket name is never a partial
    silent fill: each engine's documented behavior is asserted;
(d) a decision before a holiday, or dated on a non-session day, fills on the
    first session after it;
(e) an unfinished "today" bar is never used;
(f) the as-of guard raises on a deliberate peek.

ai-hedge-fund's per-alpha engine fills a signal at its own day's close, so an
after-close 8-K is bought before it was public; VT's only way to do that is
the documented ``options_config.same_day_fill`` opt-in, reproduced below and
refused by the guard.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backtest import asof_guard
from backtest.asof_guard import US_EQUITY, AsOfGuard, LookAheadError
from backtest.engines.base import BaseEngine
from backtest.engines.options_portfolio import FILL_TIMING as OPTIONS_FILL_TIMING
from backtest.loaders import pitdb_loader as pl
from tests.acceptance.timing_harness import (
    CODES,
    ENGINES,
    EQUITY_ENGINES,
    NY,
    SESSIONS,
    SOURCES,
    aligned_event_bar,
    bars,
    fill_field,
    fill_timing_of,
    install_source,
    memory_loader_class,
    naive_event_bar,
    next_session,
    pit_block,
    run,
    run_config,
    session,
    stamped_strategy,
    trigger_level,
    trigger_strategy,
    with_defect,
)

ENGINE_SOURCE = [(engine, source) for engine in ENGINES for source in SOURCES]
#: Equity engines in both position-adjustment modes; options has no modes.
ENGINE_SOURCE_MODE = [
    (engine, source, mode)
    for engine in ENGINES
    for source in SOURCES
    for mode in (("hold", "rebalance") if engine in EQUITY_ENGINES else ("hold",))
]
DECISION = pd.Timestamp("2026-01-22")  # a Thursday; fills land on Friday 2026-01-23
AFTER_CLOSE_8K = pd.Timestamp("2026-01-22 16:30", tz=NY).tz_convert("UTC")


def _frames():
    return {code: bars(code) for code in CODES}


def _levels(frames, day):
    return {code: trigger_level(code, day, frames) for code in CODES}


# ---------------------------------------------------------------------------
# (a) a signal from bar D's close never fills at a price from bar D
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("engine", "source", "mode"), ENGINE_SOURCE_MODE)
def test_a_signal_from_the_close_of_d_fills_on_a_later_bar(engine, source, mode, monkeypatch, tmp_path):
    frames = _frames()
    install_source(monkeypatch, source, frames)
    d = session(DECISION)
    fill_day = next_session(d)

    result = run(engine, run_config(engine, source, mode=mode),
                 trigger_strategy(engine, _levels(frames, d)), tmp_path)

    for code in CODES:
        fills = result.for_code(code)
        assert fills, f"{code} never filled"
        assert all(f.day > d for f in fills), [(f.day, f.source) for f in fills]
        assert all(not f.source.endswith(f"@{d.date()}") for f in fills)
        first = fills[0]
        assert first.day == fill_day
        assert first.source == f"{fill_field(engine)}@{fill_day.date()}"
        assert first.at > US_EQUITY.close_utc(d)[0]


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
def test_a_moving_the_close_of_d_moves_no_fill_price(engine, source, monkeypatch, tmp_path):
    """Negative control: D's close decides the trade but never prices it."""
    d = session(DECISION)
    levels = _levels(_frames(), d)
    runs = []
    for bump in (0.0, 0.2):
        frames = _frames()
        for frame in frames.values():
            frame.loc[d, "close"] += bump
            frame.loc[d, "high"] = max(frame.at[d, "high"], frame.at[d, "close"])
        install_source(monkeypatch, source, frames)
        runs.append(run(engine, run_config(engine, source), trigger_strategy(engine, levels),
                        tmp_path / f"bump{bump}"))
    first = [(f.code, f.day, f.price, f.quantity) for f in runs[0].fills if f.day <= next_session(d)]
    moved = [(f.code, f.day, f.price, f.quantity) for f in runs[1].fills if f.day <= next_session(d)]
    assert first and first == moved


def test_a_declared_fill_timings_are_the_ones_the_engines_use():
    assert BaseEngine.FILL_TIMING == "next_open"
    assert OPTIONS_FILL_TIMING == "next_close"
    assert fill_timing_of("options", {"options_config": {"same_day_fill": True}}) == "same_close"


# ---------------------------------------------------------------------------
# (b) an event known after D's close cannot affect D's fill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
@pytest.mark.parametrize("alignment", ["naive", "decision_bars"])
def test_b_after_close_8k_cannot_affect_the_fill_on_d(engine, source, alignment, monkeypatch, tmp_path):
    frames = _frames()
    install_source(monkeypatch, source, frames)
    d = session(DECISION)
    config = run_config(engine, source)
    timing = fill_timing_of(engine, config)
    bar = (naive_event_bar(AFTER_CLOSE_8K) if alignment == "naive"
           else aligned_event_bar(AFTER_CLOSE_8K, timing))
    assert bar == d  # both alignments put an after-close 8-K on D for these engines

    with_event = run(engine, config, stamped_strategy(engine, {"AAA.US": bar}), tmp_path / "with")
    without = run(engine, config, stamped_strategy(engine, {}), tmp_path / "without")

    # Negative control: through D nothing differs -- no fill, same marks.
    assert [f for f in with_event.fills if f.day <= d] == []
    assert without.fills == []
    pd.testing.assert_series_equal(with_event.equity.loc[:d], without.equity.loc[:d])
    # The event does trade, and only once it was public.
    fills = with_event.for_code("AAA.US")
    assert fills and fills[0].day == next_session(d)
    assert all(f.at > AFTER_CLOSE_8K for f in fills)


@pytest.mark.parametrize("source", SOURCES)
def test_b_same_day_fill_buys_the_8k_before_it_was_public_and_the_guard_refuses_it(
        source, monkeypatch, tmp_path):
    """ai-hedge-fund's per-alpha bug, reproduced through VT's one opt-in."""
    frames = _frames()
    install_source(monkeypatch, source, frames)
    d = session(DECISION)
    research = pit_block(claim="research") if source == "pitdb" else None
    unguarded = run_config("options", source, same_day_fill=True, pit=research)
    stamps = {"AAA.US": naive_event_bar(AFTER_CLOSE_8K)}

    result = run("options", unguarded, stamped_strategy("options", stamps), tmp_path / "off")
    fills = result.for_code("AAA.US")
    assert fills[0].day == d and fills[0].at < AFTER_CLOSE_8K  # bought at 16:00, public at 16:30

    guarded = run_config("options", source, same_day_fill=True,
                         **({} if source == "pitdb" else {"asof_guard": True}))
    with pytest.raises(LookAheadError, match="same_day_fill"):
        run("options", guarded, stamped_strategy("options", stamps), tmp_path / "on")

    # Dated by decision_bars, even a same-close fill waits for the next close.
    aligned = {"AAA.US": aligned_event_bar(AFTER_CLOSE_8K, "same_close")}
    result = run("options", unguarded, stamped_strategy("options", aligned), tmp_path / "aligned")
    assert all(f.at > AFTER_CLOSE_8K for f in result.for_code("AAA.US"))


# ---------------------------------------------------------------------------
# (c) an unusable fill price for one basket name is never a partial silent fill
# ---------------------------------------------------------------------------


def _documented(engine: str, source: str, mode: str, defect: str) -> str:
    """Each engine's documented behavior for a bad fill price on one basket name.

    refused -- pitdb refuses the snapshot: a bad print raises (fail closed);
    abort   -- rebalance mode stops at the bar before any of its orders;
    skip    -- hold mode records an ``invalid_price`` plan rejection, then defers;
    defer   -- the name fills at its own next usable bar, the rest on schedule
               (a zero price never reaches an engine from the runner: the OHLC
               sanitizer drops the bar, and pitdb drops a NaN one). An equity
               deferral shows in fills.jsonl; no plan rejection is recorded.
    """
    if defect == "zero" and source == "pitdb":
        return "refused"
    if defect == "nan" and source == "memory" and engine in EQUITY_ENGINES:
        return "abort" if mode == "rebalance" else "skip"
    return "defer"


@pytest.mark.parametrize(("engine", "source", "mode"), ENGINE_SOURCE_MODE)
@pytest.mark.parametrize("defect", ["missing", "nan", "zero"])
def test_c_bad_fill_price_for_one_name_is_never_a_silent_partial_fill(
        engine, source, mode, defect, monkeypatch, tmp_path):
    frames = _frames()
    d = session(DECISION)
    levels = _levels(frames, d)
    f_day, g_day = next_session(d), next_session(next_session(d))
    column = fill_field(engine)
    frames["BBB.US"] = with_defect(frames["BBB.US"], f_day, defect, column)
    install_source(monkeypatch, source, frames)
    config = run_config(engine, source, mode=mode)
    expected = _documented(engine, source, mode, defect)

    if expected == "refused":
        with pytest.raises(pl.PitDataError, match="BBB.US.*OHLC"):
            run(engine, config, trigger_strategy(engine, levels), tmp_path)
        return
    if expected == "abort":
        hold = {}
        with pytest.raises(ValueError, match=rf"rebalance at {f_day.date()}.*BBB\.US.*positive execution price"):
            run(engine, config, trigger_strategy(engine, levels), tmp_path, hold=hold)
        assert [r for r in hold["engine"].fill_records if r.timestamp == f_day] == []
        return

    result = run(engine, config, trigger_strategy(engine, levels), tmp_path)
    aaa, bbb = result.for_code("AAA.US"), result.for_code("BBB.US")
    assert aaa[0].day == f_day and aaa[0].source == f"{column}@{f_day.date()}"
    assert bbb, "BBB.US never filled"
    assert all(f.day != f_day for f in bbb)
    assert bbb[0].day == g_day and bbb[0].source == f"{column}@{g_day.date()}"
    if engine in EQUITY_ENGINES:
        assert math.isfinite(result.engine.capital)
        assert all(math.isfinite(f.price) and math.isfinite(f.quantity) for f in result.fills)
        rejected = result.rejections.get("BBB.US", {})
        assert rejected == ({"invalid_price": 1} if expected == "skip" else {})


# ---------------------------------------------------------------------------
# (d) holidays roll to the next session
# ---------------------------------------------------------------------------

MLK_DAY = pd.Timestamp("2026-01-19")


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
def test_d_decision_before_a_holiday_fills_on_the_first_session_after_it(
        engine, source, monkeypatch, tmp_path):
    frames = _frames()
    install_source(monkeypatch, source, frames)
    friday = session("2026-01-16")
    assert MLK_DAY not in SESSIONS and next_session(friday) == pd.Timestamp("2026-01-20")

    result = run(engine, run_config(engine, source), trigger_strategy(engine, _levels(frames, friday)),
                 tmp_path)

    assert MLK_DAY not in result.equity.index
    for code in CODES:
        first = result.for_code(code)[0]
        assert first.day == pd.Timestamp("2026-01-20")
        assert first.source == f"{fill_field(engine)}@2026-01-20"


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
@pytest.mark.parametrize("stamp", ["2026-01-19", "2026-01-24", "2026-01-23 18:00"],
                         ids=["holiday", "saturday", "friday-after-close"])
def test_d_decision_dated_off_session_rolls_to_the_next_session(
        engine, source, stamp, monkeypatch, tmp_path):
    frames = _frames()
    install_source(monkeypatch, source, frames)
    when = pd.Timestamp(stamp)
    expected = next_session(when.normalize())

    result = run(engine, run_config(engine, source), stamped_strategy(engine, {"AAA.US": when}),
                 tmp_path)

    first = result.for_code("AAA.US")[0]
    assert first.day == expected
    assert first.source == f"{fill_field(engine)}@{expected.date()}"


@pytest.mark.parametrize("engine", EQUITY_ENGINES)
@pytest.mark.parametrize("source", SOURCES)
def test_d_holiday_decision_replaces_the_one_before_it(engine, source, monkeypatch, tmp_path):
    """ai-hedge-fund's refresh before execution: the latest decision drives the fill."""
    frames = _frames()
    install_source(monkeypatch, source, frames)
    tuesday = pd.Timestamp("2026-01-20")

    class _Refresh:
        def generate(self, data_map):
            out = {}
            for code, frame in data_map.items():
                signal = pd.Series(0.0, index=frame.index)
                if code == "AAA.US":
                    signal[frame.index > MLK_DAY] = 0.4
                    signal[pd.Timestamp("2026-01-16")] = 0.2  # Friday's decision
                    signal[MLK_DAY] = 0.4  # refreshed on the holiday
                out[code] = signal.sort_index()
            return out

    result = run(engine, run_config(engine, source), _Refresh, tmp_path)
    first = result.for_code("AAA.US")[0]
    assert first.day == tuesday
    assert first.quantity == pytest.approx(round(0.4 * 100_000.0 / frames["AAA.US"].at[tuesday, "open"], 2))


# ---------------------------------------------------------------------------
# (e) an unfinished "today" bar is never used
# ---------------------------------------------------------------------------

TODAY = pd.Timestamp("2026-02-27")  # a Friday session
MIDDAY = pd.Timestamp("2026-02-27 16:00", tz="UTC")  # 11:00 New York
EVENING = pd.Timestamp("2026-02-27 22:00", tz="UTC")  # 17:00 New York


def _today_setup(source, monkeypatch, *, closed: bool):
    """Today's bar exists; the clock (memory) or the capture time (pitdb) says if it is finished."""
    frames = _frames()
    if source == "pitdb":
        captured = (EVENING - pd.Timedelta(minutes=15)) if closed else MIDDAY
        install_source(monkeypatch, source, frames, captured_at={
            (code, TODAY.date()): captured.tz_localize(None).to_pydatetime() for code in CODES})
        asof = (EVENING if closed else MIDDAY + pd.Timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return frames, pit_block(run_asof_utc=asof)
    install_source(monkeypatch, source, frames)
    monkeypatch.setattr(asof_guard, "utc_now", lambda: EVENING if closed else MIDDAY)
    return frames, None


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
def test_e_unfinished_today_bar_is_never_used(engine, source, monkeypatch, tmp_path):
    frames, pit = _today_setup(source, monkeypatch, closed=False)
    yesterday = session("2026-02-26")
    config = run_config(engine, source, end=str(TODAY.date()), pit=pit)

    result = run(engine, config, trigger_strategy(engine, _levels(frames, yesterday)), tmp_path)

    for code in CODES:
        assert TODAY not in result.data_map[code].index
    assert result.equity.index.max() == yesterday
    assert all(f.day < TODAY for f in result.fills)
    assert result.fills == []  # yesterday's decision has no finished session to fill on: pending
    if source == "pitdb":
        provenance = result.fetch.pit["symbols"]
        assert all(provenance[code]["unfinished_bars"] == 1 for code in CODES)
    else:
        assert len(result.fetch.unfinished_notes) == len(CODES)
        assert all("2026-02-27" in note for note in result.fetch.unfinished_notes)


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
def test_e_the_same_bar_is_used_once_its_session_has_closed(engine, source, monkeypatch, tmp_path):
    frames, pit = _today_setup(source, monkeypatch, closed=True)
    yesterday = session("2026-02-26")
    config = run_config(engine, source, end=str(TODAY.date()), pit=pit)

    result = run(engine, config, trigger_strategy(engine, _levels(frames, yesterday)), tmp_path)

    for code in CODES:
        assert TODAY in result.data_map[code].index
        first = result.for_code(code)[0]
        assert first.day == TODAY and first.source == f"{fill_field(engine)}@{TODAY.date()}"


def test_e_forming_intraday_bar_is_never_used(monkeypatch):
    from backtest.runner import fetch_data_map

    hours = pd.date_range("2026-02-27 14:30", periods=3, freq="h")  # naive UTC: 09:30..11:30 NY
    frame = pd.DataFrame({"open": [1.0, 2.0, 3.0], "high": [1.5, 2.5, 3.5], "low": [0.5, 1.5, 2.5],
                          "close": [1.2, 2.2, 3.2], "volume": 1.0}, index=hours)
    frame.attrs.update(bar_timezone="UTC", bar_timestamp_convention="start")
    install_source(monkeypatch, "memory", {"AAA.US": frame})
    monkeypatch.setattr(asof_guard, "utc_now", lambda: pd.Timestamp("2026-02-27 17:00", tz="UTC"))
    served = fetch_data_map(run_config("global_equity", "memory", codes=["AAA.US"], end="2026-02-27",
                                       interval="1H")).data_map["AAA.US"]
    assert served.index.max() < pd.Timestamp("2026-02-27 16:30")  # the 16:30 bar is still forming


# ---------------------------------------------------------------------------
# (f) the as-of guard raises on a deliberate peek
# ---------------------------------------------------------------------------


def _source_loader(source, frames, monkeypatch):
    if source == "pitdb":
        install_source(monkeypatch, source, frames)
        loader = pl.PitdbLoader()
        loader.bind_run_config(run_config("global_equity", source))
        return loader
    return memory_loader_class(frames)()


@pytest.mark.parametrize("source", SOURCES)
def test_f_guarded_loader_raises_on_a_peek_past_the_decision_cutoff(source, monkeypatch):
    frames = _frames()
    loader = _source_loader(source, frames, monkeypatch)
    d = session(DECISION)
    guard = AsOfGuard(loader, US_EQUITY.close_utc(d)[0] + pd.Timedelta(hours=1))

    served = guard.fetch(list(CODES), "2026-01-05", str(d.date()))
    assert all(frame.index.max() == d for frame in served.values())
    peek = str(next_session(d).date())
    basis = "pitdb row knowledge time" if source == "pitdb" else "session close of the bar"
    with pytest.raises(LookAheadError, match=rf"AAA\.US: 1 row\(s\) not known.*{peek}.*{basis}"):
        guard.fetch(list(CODES), "2026-01-05", peek)


class _PeekNextClose:
    """Equity: long whenever tomorrow's close is higher -- a deliberate peek."""

    def generate(self, data_map):
        return {code: (frame["close"].shift(-1) > frame["close"]).astype(float) * 0.4
                for code, frame in data_map.items()}


class _OptionsPeek:
    """Options: buys a call whenever tomorrow's close is higher -- a deliberate peek."""

    def generate(self, data_map):
        signals = []
        for code, frame in data_map.items():
            rises = (frame["close"].shift(-1) > frame["close"]).to_numpy()
            for day in frame.index[rises]:
                signals.append({"date": str(day.date()), "action": "open", "underlying": code,
                                "legs": [{"type": "call", "strike": 100, "expiry": "2026-06-19", "qty": 1}]})
        return signals


@pytest.mark.parametrize(("engine", "source"), ENGINE_SOURCE)
def test_f_strategy_that_reads_the_next_bar_is_refused_at_run_time(engine, source, monkeypatch, tmp_path):
    frames = _frames()
    install_source(monkeypatch, source, frames)
    config = run_config(engine, source, **({} if source == "pitdb" else {"asof_guard": True}))
    peeker = _OptionsPeek if engine == "options" else _PeekNextClose

    with pytest.raises(LookAheadError, match="reads data from after the bar"):
        run(engine, config, peeker, tmp_path)
    # Off (a research run that does not switch it on), the same strategy runs.
    research = run_config(engine, source, pit=pit_block(claim="research") if source == "pitdb" else None)
    assert run(engine, research, peeker, tmp_path / "off").fills


def test_f_event_known_after_the_cutoff_raises_and_asof_rows_drops_it():
    events = pd.DataFrame(
        {"knowledge_time": [pd.Timestamp("2026-01-22 12:00", tz=NY), AFTER_CLOSE_8K], "sue": [1.2, -3.4]},
        index=pd.DatetimeIndex(["2026-01-22", "2026-01-22"]),
    )
    cutoff = US_EQUITY.close_utc(DECISION)[0]  # a decision at D's close
    with pytest.raises(LookAheadError, match="1 row\\(s\\) not known.*knowledge_time column"):
        asof_guard.check_rows(events, cutoff, code="AAA.US")
    assert asof_guard.asof_rows(events, cutoff)["sue"].tolist() == [1.2]
