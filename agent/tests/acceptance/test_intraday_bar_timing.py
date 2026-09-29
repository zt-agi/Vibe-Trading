"""Physical intraday completion cannot be bypassed by a provider's knowledge field."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest import asof_guard as g
from backtest.loaders import base
from backtest.loaders.yfinance_loader import _normalize_frame


def _bars(index, *, zone=None, convention=None):
    frame = pd.DataFrame(
        {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 1.0},
        index=pd.DatetimeIndex(index),
    )
    if zone is not None:
        frame.attrs["bar_timezone"] = zone
    if convention is not None:
        frame.attrs["bar_timestamp_convention"] = convention
    return frame


@pytest.mark.parametrize(("day", "end"), [
    ("2026-02-27", "2026-02-27T17:30Z"),
    ("2026-07-17", "2026-07-17T16:30Z"),
])
def test_yfinance_preserves_absolute_ny_time_in_winter_and_summer(day, end):
    raw = _bars(pd.DatetimeIndex([f"{day} 11:30"], tz="America/New_York"))
    normalized = _normalize_frame(raw, "1H")
    known, _ = g.knowledge_times(normalized, interval="1H")
    assert known[0] == pd.Timestamp(end)
    assert normalized.index[0] == (pd.Timestamp(end) - pd.Timedelta(hours=1)).tz_localize(None)
    assert normalized.attrs["bar_timezone"] == "UTC"
    cutoff = pd.Timestamp(end) - pd.Timedelta(minutes=30)
    with pytest.raises(g.LookAheadError):
        g.check_rows(normalized, cutoff, interval="1H")
    assert g.drop_unfinished_bars({"AAA.US": normalized}, "1H", cutoff)[0]["AAA.US"].empty
    g.check_rows(normalized, end, interval="1H")


@pytest.mark.parametrize("interval", ["1H", "1h", "1h "])
def test_hour_aliases_trim_forming_utc_bar_and_accept_exact_boundary(interval):
    frame = _bars(pd.date_range("2026-02-27 14:30", periods=3, freq="h"), zone="UTC", convention="start")
    kept, notes = g.drop_unfinished_bars({"AAA.US": frame}, interval, "2026-02-27T17:00Z")
    assert list(kept["AAA.US"].index) == list(frame.index[:2])
    assert "unfinished intraday" in notes[0]
    assert len(g.drop_unfinished_bars({"AAA.US": frame}, interval, "2026-02-27T17:30Z")[0]["AAA.US"]) == 3
    g.check_rows(frame, "2026-02-27T17:30Z", interval=interval)


@pytest.mark.parametrize("cutoff", ["2026-02-27T17:00Z", "2026-02-27T18:00Z"])
def test_early_explicit_capture_never_becomes_a_complete_bar(cutoff):
    frame = _bars(["2026-02-27 16:30"], zone="UTC", convention="start")
    frame["knowledge_time"] = pd.Timestamp("2026-02-27T16:45Z")
    with pytest.raises(g.LookAheadError, match="unfinished bar"):
        g.check_rows(frame, cutoff, interval="1H")
    assert g.asof_rows(frame, cutoff, interval="1H").empty


def test_end_stamped_bar_is_complete_at_its_stamp():
    frame = _bars(["2026-02-27 16:30"], zone="UTC", convention="end")
    frame["knowledge_time"] = pd.Timestamp("2026-02-27T16:30Z")
    ends, basis = g.bar_end_times(frame, interval="1H")
    assert ends[0] == pd.Timestamp("2026-02-27T16:30Z") and "end stamp" in basis
    g.check_rows(frame, ends[0], interval="1H")
    assert len(g.drop_unfinished_bars({"AAA.US": frame}, "1H", ends[0])[0]["AAA.US"]) == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_unknown_naive_timezone_fails_closed_in_guarded_checks(explicit):
    frame = _bars(["2026-02-27 11:30"])
    if explicit:
        frame["knowledge_time"] = pd.Timestamp("2026-02-27T18:00Z")
    with pytest.raises(g.LookAheadError, match="unknown intraday timezone"):
        g.check_rows(frame, "2026-02-28T00:00Z", interval="1H")
    # Ordinary research remains accessible with an explicit unresolved-timing note.
    kept, notes = g.drop_unfinished_bars({"AAA.US": frame}, "1H", "2026-02-28T00:00Z")
    assert len(kept["AAA.US"]) == 1 and "timing unresolved" in notes[0]


def test_known_zone_without_stamp_convention_uses_labelled_conservative_start():
    frame = _bars(["2026-02-27 11:30"], zone="America/New_York")
    ends, basis = g.bar_end_times(frame, interval="1H")
    assert ends[0] == pd.Timestamp("2026-02-27T17:30Z")
    assert "assumed start conservatively" in basis


@pytest.mark.parametrize(("zone", "convention"), [
    ("provider-local", "start"), ("UTC", "provider-candle-time"),
])
def test_ambiguous_provider_metadata_is_refused(zone, convention):
    with pytest.raises(g.LookAheadError):
        g.bar_end_times(_bars(["2026-02-27 11:30"], zone=zone, convention=convention), interval="1H")


def test_dst_ambiguous_wall_time_is_refused():
    frame = _bars(["2026-11-01 01:30"], zone="America/New_York", convention="start")
    with pytest.raises(g.LookAheadError, match="unresolved intraday timezone"):
        g.bar_end_times(frame, interval="1H")


def test_resampled_4h_partial_bucket_is_removed():
    hourly = _bars(pd.date_range("2026-02-27", periods=6, freq="h", tz="UTC"))
    four_hour = _normalize_frame(hourly, "4h")
    assert len(four_hour) == 2
    kept, _ = g.drop_unfinished_bars({"BTC-USDT": four_hour}, "4h", "2026-02-27T06:00Z")
    assert list(kept["BTC-USDT"].index) == [pd.Timestamp("2026-02-27")]
    assert len(g.drop_unfinished_bars({"BTC-USDT": four_hour}, "4H", "2026-02-27T08:00Z")[0]["BTC-USDT"]) == 2


@pytest.mark.parametrize("cached", [False, True])
def test_ny_offset_4h_bucket_cannot_finish_before_its_last_hour(tmp_path, cached):
    hours = pd.DatetimeIndex(["2026-02-27 09:30", "2026-02-27 10:30"], tz="America/New_York")
    four_hour = _normalize_frame(_bars(hours), "4H")
    assert four_hour.index[0] == pd.Timestamp("2026-02-27 12:00")
    assert four_hour["bar_end_time"].iloc[0] == pd.Timestamp("2026-02-27T16:30Z")
    if cached:
        path = tmp_path / "offset_4h.parquet"
        base._write_loader_cache_frame(path, four_hour)
        four_hour = base._read_loader_cache_frame(path)
        assert four_hour is not None
    assert g.knowledge_times(four_hour, interval="4H")[0][0] == pd.Timestamp("2026-02-27T16:30Z")
    assert g.drop_unfinished_bars({"AAA.US": four_hour}, "4H", "2026-02-27T16:10Z")[0]["AAA.US"].empty
    with pytest.raises(g.LookAheadError):
        g.check_rows(four_hour, "2026-02-27T16:10Z", interval="4H")
    g.check_rows(four_hour, "2026-02-27T16:30Z", interval="4H")


def test_minute_and_month_tokens_remain_distinct_and_daily_unchanged():
    assert g.intraday_span("1m") == pd.Timedelta(minutes=1)
    assert g.intraday_span("1M") is None
    frame = _bars(["2026-02-27"])
    assert g.knowledge_times(frame, interval="1D")[0][0] == pd.Timestamp("2026-02-27T21:00Z")
    assert g.knowledge_times(frame, interval="1M")[0][0] == pd.Timestamp("2026-02-27T21:00Z")


def test_cache_roundtrip_preserves_timezone_convention_and_timing_basis(tmp_path):
    frame = _bars(["2026-02-27 11:30"], zone="America/New_York", convention="start")
    frame.attrs["bar_timing_basis"] = "verified exchange-local bar start"
    path = tmp_path / "bars.parquet"
    base._write_loader_cache_frame(path, frame)
    restored = base._read_loader_cache_frame(path)
    assert restored is not None and restored.attrs == frame.attrs
    assert g.knowledge_times(restored, interval="1H")[0][0] == pd.Timestamp("2026-02-27T17:30Z")
    with pytest.raises(g.LookAheadError):
        g.check_rows(restored, "2026-02-27T17:00Z", interval="1H")


def test_legacy_cache_without_timing_schema_is_invalidated(tmp_path):
    path = tmp_path / "legacy.parquet"
    base._write_loader_cache_frame(path, _bars(["2026-02-27 11:30"]))
    metadata_path = base._loader_cache_metadata_path(path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["version"] = base._LOADER_CACHE_VERSION - 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert base._read_loader_cache_frame(path) is None


def test_ambiguous_current_cache_is_still_guard_refused(tmp_path):
    path = tmp_path / "ambiguous.parquet"
    base._write_loader_cache_frame(path, _bars(["2026-02-27 11:30"]))
    restored = base._read_loader_cache_frame(path)
    assert restored is not None
    with pytest.raises(g.LookAheadError, match="unknown intraday timezone"):
        g.check_rows(restored, "2026-02-28T00:00Z", interval="1H")


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_timestamp_resolution_does_not_change_cutoff_comparison(unit):
    frame = _bars(pd.DatetimeIndex(["2026-02-27 16:30"]).as_unit(unit), zone="UTC", convention="start")
    frame["knowledge_time"] = pd.DatetimeIndex(["2026-02-27 16:45"], tz="UTC").as_unit(unit)
    with pytest.raises(g.LookAheadError, match="unfinished bar"):
        g.check_rows(frame, "2026-02-27T17:00Z", interval="1H")
    assert g.drop_unfinished_bars({"AAA.US": frame}, "1H", "2026-02-27T17:00Z")[0]["AAA.US"].empty
