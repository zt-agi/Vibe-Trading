"""ZT add-on: backtest.asof_guard -- knowledge times, checks, the strategy probe,
the runtime switch, and the guard inside ``runner.main``."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backtest import asof_guard as g
from backtest.asof_guard import (
    US_EQUITY,
    AsOfGuard,
    AsOfGuardConfigError,
    LookAheadError,
)
from backtest.loaders import pitdb_loader as pl
from tests.acceptance.timing_harness import (
    CODES,
    NY,
    SESSIONS,
    bars,
    install_source,
    memory_loader_class,
    run_config,
)

DAYS = pd.DatetimeIndex(["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"])  # across the DST switch


def _bars(index=DAYS) -> pd.DataFrame:
    steps = np.arange(len(index), dtype=float)
    return pd.DataFrame({"open": 10 + steps, "high": 11 + steps, "low": 9 + steps,
                         "close": 10.5 + steps, "volume": 1.0}, index=index)


def _pit_frame(index=DAYS, *, mode="formation", lag="36h", known=None) -> pd.DataFrame:
    frame = _bars(index)
    pit = {"mode": mode, "availability_lag": lag, "max_knowledge_time_utc": "2026-03-11T00:00:00Z"}
    if known is not None:
        pit.update(g.encode_row_knowledge(index, known))
    frame.attrs["pit"] = pit
    return frame


# ---------------------------------------------------------------------------
# Session clock and knowledge times
# ---------------------------------------------------------------------------


def test_us_session_times_follow_new_york_daylight_saving():
    closes = US_EQUITY.close_utc(DAYS)
    opens = US_EQUITY.open_utc(DAYS)
    assert [c.hour for c in closes] == [21, 21, 20, 20]  # EST, EST, EDT, EDT
    assert [o.strftime("%H:%M") for o in opens] == ["14:30", "14:30", "13:30", "13:30"]


def test_knowledge_time_precedence():
    column = _bars().assign(knowledge_time=pd.Timestamp("2026-03-12 00:00"))
    column.attrs["pit"] = {"mode": "formation", "availability_lag": "36h"}
    assert g.knowledge_times(column)[1] == "knowledge_time column"

    rows = DAYS + pd.Timedelta(hours=30)
    known, basis = g.knowledge_times(_pit_frame(known=rows))
    assert basis == "pitdb row knowledge time"
    assert list(known) == list(rows.tz_localize("UTC"))

    bound, basis = g.knowledge_times(_pit_frame())
    assert basis.startswith("pitdb formation bound")
    assert bound[0] == pd.Timestamp("2026-03-06 12:00", tz="UTC")

    snapshot, basis = g.knowledge_times(_pit_frame(mode="snapshot"))
    assert basis.startswith("pitdb latest knowledge time") and snapshot.nunique() == 1

    closes, basis = g.knowledge_times(_bars())
    assert basis == "session close of the bar" and closes[0] == pd.Timestamp("2026-03-05 21:00", tz="UTC")

    hourly = _bars(pd.date_range("2026-03-05 14:30", periods=4, freq="h"))
    ends, basis = g.knowledge_times(hourly, interval="1H")
    assert basis == "end of the bar" and ends[0] == pd.Timestamp("2026-03-05 15:30", tz="UTC")


def test_row_knowledge_survives_slicing_resampling_and_concat():
    rows = DAYS + pd.Timedelta(hours=30)
    frame = _pit_frame(known=rows)
    sliced = frame.iloc[2:]
    assert list(g.knowledge_times(sliced)[0]) == list(rows[2:].tz_localize("UTC"))
    both = pd.concat([frame.iloc[:2], frame.iloc[2:]])  # attrs compare equal: bytes
    assert both.attrs["pit"][g.PIT_ROW_KNOWLEDGE] == frame.attrs["pit"][g.PIT_ROW_KNOWLEDGE]
    from backtest.loaders.base import resample_bars
    weekly = resample_bars(frame, "1W")
    assert list(g.knowledge_times(weekly)[0]) == [rows[1].tz_localize("UTC"), rows[3].tz_localize("UTC")]


def test_pitdb_frames_carry_row_knowledge_times(monkeypatch):
    frames = {code: bars(code) for code in CODES}
    install_source(monkeypatch, "pitdb", frames)
    loader = pl.PitdbLoader()
    loader.bind_run_config(run_config("global_equity", "pitdb"))
    frame = loader.fetch(["AAA.US"], "2026-01-05", "2026-01-30")["AAA.US"]
    known, basis = g.knowledge_times(frame)
    assert basis == "pitdb row knowledge time"
    expected = US_EQUITY.close_utc(frame.index) + pd.Timedelta(minutes=45)
    assert list(known) == list(expected)
    assert g.PIT_ROW_KNOWLEDGE not in loader.run_provenance()["symbols"]["AAA.US"]  # not in the run card


# ---------------------------------------------------------------------------
# Row and fill-timing checks
# ---------------------------------------------------------------------------


def test_check_rows_refuses_missing_and_intraday_knowledge():
    events = pd.DataFrame({"knowledge_time": [pd.NaT]}, index=DAYS[:1])
    with pytest.raises(LookAheadError, match="not known by the decision cutoff"):
        g.check_rows(events, "2026-03-20")
    captured = _bars().assign(knowledge_time=US_EQUITY.close_utc(DAYS) - pd.Timedelta(hours=2))
    with pytest.raises(LookAheadError, match="unfinished bar"):
        g.check_rows(captured, "2026-03-20")


def test_execution_times_per_fill_timing():
    nxt = g.execution_times(DAYS, "next_open")
    assert nxt[0] == US_EQUITY.open_utc(DAYS[1])[0] and pd.isna(nxt[-1])
    assert g.execution_times(DAYS, "next_close")[2] == US_EQUITY.close_utc(DAYS[3])[0]
    assert g.execution_times(DAYS, "same_close")[3] == US_EQUITY.close_utc(DAYS[3])[0]
    weekly = g.execution_times(pd.DatetimeIndex(["2026-03-06", "2026-03-13"]), "next_open", interval="1W")
    assert weekly[0] == US_EQUITY.open_utc("2026-03-07")[0] and pd.isna(weekly[1])
    with pytest.raises(ValueError, match="fill_timing"):
        g.execution_times(DAYS, "at_will")


def test_formation_lag_must_fit_the_gap_to_the_next_open():
    g.check_fill_timing(_pit_frame(lag="36h"), "next_open", code="AAA.US")
    with pytest.raises(LookAheadError, match="fills at .* but may read data known"):
        g.check_fill_timing(_pit_frame(lag="2 days"), "next_open", code="AAA.US")
    # The next close is later: a 40h lag, too late for the next open, fits the options engine.
    g.check_fill_timing(_pit_frame(lag="40h"), "next_close", code="AAA.US")


def test_revised_older_row_counts_for_every_later_decision():
    """A decision on bar t may read every earlier row, so a late revision of one leaks forward."""
    known = DAYS + pd.Timedelta(hours=30)
    known = known.insert(0, pd.Timestamp("2026-03-09 16:00")).delete(1)  # row 0 revised later
    with pytest.raises(LookAheadError, match="bar 2026-03-05.*2 bar"):
        g.check_fill_timing(_pit_frame(known=known), "next_open", code="AAA.US")


def test_same_close_is_refused_and_24h_markets_fill_on_the_next_open():
    with pytest.raises(LookAheadError, match="same_day_fill"):
        g.check_fill_timing(_bars(), "same_close")
    crypto = g.clock_for("BTC-USDT")
    assert crypto.tz == "UTC" and crypto.latency == pd.Timedelta(0)
    g.check_fill_timing(_bars(pd.date_range("2026-03-05", periods=4)), "next_open", clock=crypto)


def test_decision_bars_place_events_on_the_first_bar_that_can_act():
    index = SESSIONS
    known = [
        pd.Timestamp("2026-01-22 16:30", tz=NY),  # after the close
        pd.Timestamp("2026-01-22 08:00", tz=NY),  # before the open
        pd.Timestamp("2026-01-22 11:00", tz=NY),  # during the session
        pd.Timestamp("2026-01-24 10:00", tz=NY),  # Saturday
        pd.Timestamp("2026-01-19 10:00", tz=NY),  # a holiday
        pd.Timestamp("2026-02-27 17:00", tz=NY),  # after the last bar's close
    ]
    def at(timing):
        return [None if pd.isna(x) else str(x.date()) for x in g.decision_bars(known, index, fill_timing=timing)]
    assert at("next_open") == ["2026-01-22", "2026-01-21", "2026-01-22", "2026-01-23", "2026-01-16", None]
    assert at("next_close") == ["2026-01-22", "2026-01-21", "2026-01-21", "2026-01-23", "2026-01-16", None]
    assert at("same_close") == ["2026-01-23", "2026-01-22", "2026-01-22", "2026-01-26", "2026-01-20", None]


# ---------------------------------------------------------------------------
# Unfinished bars
# ---------------------------------------------------------------------------


def test_drop_unfinished_bars_uses_each_market_close():
    day = pd.DatetimeIndex(["2026-03-09", "2026-03-10"])
    data = {"AAPL.US": _bars(day), "BTC-USDT": _bars(day), "600000.SH": _bars(day)}
    kept, notes = g.drop_unfinished_bars(data, "1D", now="2026-03-10 21:00Z")  # 17:00 NY, 05:00 Shanghai
    assert len(kept["AAPL.US"]) == 2  # closed at 20:00 UTC
    assert len(kept["BTC-USDT"]) == 1  # UTC day still running
    assert len(kept["600000.SH"]) == 2  # Shanghai day ended at 16:00 UTC
    assert notes == ["BTC-USDT: dropped 1 bar(s) whose session had not closed at "
                     "2026-03-10T21:00:00+00:00 (2026-03-10)"]
    pit = _pit_frame(day)
    assert len(g.drop_unfinished_bars({"AAPL.US": pit}, "1D", now="2026-03-10 12:00Z")[0]["AAPL.US"]) == 2
    assert g.drop_unfinished_bars(data, "1H", now="2026-03-10 12:00Z")[1] == []


# ---------------------------------------------------------------------------
# The loader proxy
# ---------------------------------------------------------------------------


def test_guard_proxy_is_transparent_and_checks_only_the_strategys_codes():
    frames = {code: bars(code) for code in CODES}
    loader = memory_loader_class(frames)()
    guard = AsOfGuard(loader, "2026-01-10T00:00:00Z", fill_timing="next_open", codes=["AAA.US"])
    assert guard.name == "yfinance" and guard.wrapped is loader
    served = guard.fetch(["BBB.US"], "2026-01-05", "2026-02-27")  # a benchmark: not checked
    assert len(served["BBB.US"]) == len(SESSIONS)
    with pytest.raises(LookAheadError, match="AAA.US"):
        guard.fetch(["AAA.US"], "2026-01-05", "2026-02-27")
    with pytest.raises(ValueError, match="fill_timing"):
        AsOfGuard(loader, None, fill_timing="later")


# ---------------------------------------------------------------------------
# The strategy probe
# ---------------------------------------------------------------------------


def _map():
    return {code: bars(code) for code in CODES}


class _Momentum:
    def generate(self, data_map):
        return {c: (f["close"] > f["close"].rolling(5).mean()).astype(float) for c, f in data_map.items()}


class _ZScore:
    """A whole-sample standardisation: every signal depends on the future."""

    def generate(self, data_map):
        return {c: ((f["close"] - f["close"].mean()) / f["close"].std()).clip(-1, 1)
                for c, f in data_map.items()}


class _Noisy:
    def generate(self, data_map):
        rng = np.random.default_rng()
        return {c: pd.Series(rng.random(len(f)), index=f.index) for c, f in data_map.items()}


class _NeedsHistory:
    def generate(self, data_map):
        if min(len(f) for f in data_map.values()) < 30:
            raise ValueError("needs 30 bars")
        return {c: pd.Series(0.5, index=f.index) for c, f in data_map.items()}


def test_probe_passes_a_causal_strategy_and_catches_whole_sample_statistics():
    assert g.probe_signal_lookahead(lambda m: _Momentum().generate(m), _map())["status"] == "passed"
    with pytest.raises(LookAheadError, match="whole-sample statistic"):
        g.probe_signal_lookahead(lambda m: _ZScore().generate(m), _map())


def test_probe_skips_what_it_cannot_judge():
    assert "not deterministic" in g.probe_signal_lookahead(lambda m: _Noisy().generate(m), _map())["status"]
    result = g.probe_signal_lookahead(lambda m: _NeedsHistory().generate(m), _map())
    # 50% is inside min_history; at 75% (29 bars) the strategy raises and is skipped.
    assert result["status"] == "passed" and len(result["cutoffs"]) == 1
    short = {c: f.iloc[:10] for c, f in _map().items()}
    assert g.probe_signal_lookahead(lambda m: _Momentum().generate(m), short)["status"] == "skipped: too few bars"


def test_probe_never_mutates_the_data():
    data = _map()
    before = {c: f.copy() for c, f in data.items()}

    class _Mutating:
        def generate(self, data_map):
            for frame in data_map.values():
                frame["ma"] = frame["close"].rolling(3).mean()
            return {c: (f["close"] > f["ma"]).astype(float) for c, f in data_map.items()}

    g.probe_signal_lookahead(lambda m: _Mutating().generate(m), data)
    for code, frame in data.items():
        pd.testing.assert_frame_equal(frame, before[code])


# ---------------------------------------------------------------------------
# The runtime switch
# ---------------------------------------------------------------------------


def test_switch_defaults_and_overrides(monkeypatch):
    assert g.guard_enabled({}) is False
    assert g.guard_enabled({"pit": {"claim": "research"}}) is False
    assert g.guard_enabled({"pit": {"claim": "tradeable"}}) is True
    assert g.guard_enabled({"asof_guard": True}) is True
    monkeypatch.setenv(g.ENV_SWITCH, "on")
    assert g.guard_enabled({}) is True
    assert g.guard_enabled({"asof_guard": False}) is False  # config wins
    monkeypatch.setenv(g.ENV_SWITCH, "maybe")
    with pytest.raises(AsOfGuardConfigError, match="on or off"):
        g.guard_enabled({})
    monkeypatch.setenv(g.ENV_SWITCH, "off")
    with pytest.raises(AsOfGuardConfigError, match="tradeable claim"):
        g.guard_enabled({"pit": {"claim": "tradeable"}})
    with pytest.raises(AsOfGuardConfigError, match="true or false"):
        g.guard_enabled({"asof_guard": "yes"})


def test_guard_run_is_a_no_op_when_off_and_wraps_when_on():
    data = _map()
    loader = memory_loader_class(data)()
    assert g.guard_run({"codes": list(CODES)}, data, loader, fill_timing="next_open") is loader
    config = {"codes": list(CODES), "asof_guard": True}
    wrapped = g.guard_run(config, data, loader, fill_timing="next_open", signal_factory=_Momentum)
    assert isinstance(wrapped, AsOfGuard) and wrapped.wrapped is loader
    with pytest.raises(LookAheadError):
        g.guard_run(config, data, loader, fill_timing="next_open", signal_factory=_ZScore)


# ---------------------------------------------------------------------------
# The guard inside runner.main
# ---------------------------------------------------------------------------

_CLEAN = (
    "import pandas as pd\n\n\n"
    "class SignalEngine:\n"
    "    def generate(self, data_map):\n"
    "        return {c: (df['close'] > df['close'].rolling(5).mean()).astype(float) * 0.4\n"
    "                for c, df in data_map.items()}\n"
)
_PEEK = (
    "import pandas as pd\n\n\n"
    "class SignalEngine:\n"
    "    def generate(self, data_map):\n"
    "        return {c: (df['close'].shift(-1) > df['close']).astype(float) * 0.4\n"
    "                for c, df in data_map.items()}\n"
)


def _run_main(monkeypatch, tmp_path, config, strategy):
    from backtest import runner
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    reset_env_config()
    run_dir = tmp_path / "run"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (run_dir / "code" / "signal_engine.py").write_text(strategy, encoding="utf-8")
    try:
        runner.main(run_dir)
    finally:
        reset_env_config()
    return run_dir


def test_runner_guards_a_tradeable_pitdb_run_and_records_it(monkeypatch, tmp_path, capsys):
    install_source(monkeypatch, "pitdb", {code: bars(code) for code in CODES})
    run_dir = _run_main(monkeypatch, tmp_path, run_config("global_equity", "pitdb"), _CLEAN)
    card = json.loads((run_dir / "run_card.json").read_text(encoding="utf-8"))
    assert card["pit"]["asof_guard"].startswith("passed: cutoff 2026-03-02T00:00:00+00:00, fill timing next_open")
    assert "strategy probe passed" in card["pit"]["asof_guard"]


def test_runner_refuses_a_peeking_strategy_under_a_tradeable_claim(monkeypatch, tmp_path, capsys):
    install_source(monkeypatch, "pitdb", {code: bars(code) for code in CODES})
    with pytest.raises(SystemExit) as stop:
        _run_main(monkeypatch, tmp_path, run_config("global_equity", "pitdb"), _PEEK)
    assert stop.value.code == 1
    error = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]
    assert error.startswith("as-of guard: ") and "reads data from after the bar" in error


def test_runner_refuses_switching_the_guard_off_for_a_tradeable_claim(monkeypatch, tmp_path, capsys):
    install_source(monkeypatch, "pitdb", {code: bars(code) for code in CODES})
    config = {**run_config("global_equity", "pitdb"), "asof_guard": False}
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, config, _CLEAN)
    assert "tradeable claim runs under the as-of guard" in capsys.readouterr().out


def test_runner_drops_todays_bar_and_says_so_on_the_run_card(monkeypatch, tmp_path):
    install_source(monkeypatch, "memory", {code: bars(code) for code in CODES})
    monkeypatch.setattr(g, "utc_now", lambda: pd.Timestamp("2026-02-27 16:00", tz="UTC"))
    config = run_config("global_equity", "memory", end="2026-02-27")
    run_dir = _run_main(monkeypatch, tmp_path, config, _CLEAN)
    card = json.loads((run_dir / "run_card.json").read_text(encoding="utf-8"))
    assert any(w.startswith("unfinished bars dropped: AAA.US: dropped 1 bar(s)") for w in card["warnings"])
    equity = pd.read_csv(run_dir / "artifacts" / "equity.csv")
    assert equity["timestamp"].iloc[-1].startswith("2026-02-26")


def test_runner_guards_the_options_engine_and_refuses_same_day_fill(monkeypatch, tmp_path, capsys):
    install_source(monkeypatch, "memory", {code: bars(code) for code in CODES})
    options = (
        "class SignalEngine:\n"
        "    def generate(self, data_map):\n"
        "        return [{'date': '2026-01-22', 'action': 'open', 'underlying': 'AAA.US',\n"
        "                 'legs': [{'type': 'call', 'strike': 110, 'expiry': '2026-06-19', 'qty': 1}]}]\n"
    )
    config = run_config("options", "memory", same_day_fill=True, asof_guard=True)
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, config, options)
    assert "same_day_fill" in capsys.readouterr().out
