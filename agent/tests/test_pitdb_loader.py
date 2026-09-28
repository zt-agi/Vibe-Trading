"""ZT add-on: the pitdb point-in-time loader (source="pitdb").

A synthetic, in-memory pitdb stands in for the warehouse: the tables and the
sanctioned macros below are copied verbatim from
``implementation/pit_warehouse/pitdb/schema.sql`` (``test_fixture_macros_match
_the_warehouse_schema`` compares them when that file is reachable). No test
touches the network: every other loader is patched to raise where the path
could reach one.

The ten negative look-ahead tests from the design (C-i) are marked ``LA1`` ..
``LA10`` in their docstrings.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from backtest.loaders import pitdb_loader as pl
from backtest.loaders.base import NoAvailableSourceError
from backtest.loaders.registry import (
    FALLBACK_CHAINS,
    LOADER_REGISTRY,
    VALID_SOURCES,
    _ensure_registered,
    is_no_network_fallback_source,
)

FIXTURE_SCHEMA = """
CREATE TABLE dim_source (
    source_id VARCHAR PRIMARY KEY, family VARCHAR, provider VARCHAR, label VARCHAR,
    endpoint VARCHAR, access VARCHAR, cost_usd_month DOUBLE DEFAULT 0, license VARCHAR,
    cadence VARCHAR, release_lag VARCHAR, pit_class VARCHAR, revises BOOLEAN DEFAULT FALSE,
    history_start DATE, actor_id VARCHAR, used_by VARCHAR, status VARCHAR,
    connector VARCHAR, notes VARCHAR, first_seen TIMESTAMP, last_reviewed DATE);
CREATE TABLE dim_security (
    sec_id BIGINT PRIMARY KEY, figi VARCHAR, isin VARCHAR, primary_ticker VARCHAR,
    exchange_mic VARCHAR, country VARCHAR, currency VARCHAR, asset_class VARCHAR,
    name VARCHAR, sector VARCHAR, is_active BOOLEAN DEFAULT TRUE, listed_from DATE,
    delisted_on DATE, created_at TIMESTAMP);
CREATE TABLE dim_security_alias (
    sec_id BIGINT, alias_type VARCHAR, alias_value VARCHAR, valid_from DATE,
    valid_to DATE, source_id VARCHAR);
CREATE TABLE fact_price_eod (
    sec_id BIGINT NOT NULL, event_date DATE NOT NULL, knowledge_time TIMESTAMP NOT NULL,
    revision_seq INTEGER NOT NULL DEFAULT 0, open DOUBLE, high DOUBLE, low DOUBLE,
    close DOUBLE, volume DOUBLE, vwap DOUBLE, trade_count BIGINT, currency VARCHAR,
    is_settled BOOLEAN DEFAULT TRUE, source_id VARCHAR, ingest_run_id BIGINT);
CREATE TABLE fact_corp_action (
    sec_id BIGINT, action_type VARCHAR, ex_date DATE, announce_time TIMESTAMP,
    knowledge_time TIMESTAMP, revision_seq INTEGER DEFAULT 0, ratio DOUBLE,
    amount DOUBLE, currency VARCHAR, payload JSON, source_id VARCHAR, ingest_run_id BIGINT);

CREATE OR REPLACE MACRO price_asof(kt) AS TABLE
SELECT sec_id, event_date, open, high, low, close, volume, vwap, currency,
       knowledge_time, revision_seq, source_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY sec_id, event_date
               ORDER BY knowledge_time DESC, revision_seq DESC) AS rn
    FROM fact_price_eod
    WHERE knowledge_time <= kt
) WHERE rn = 1;

CREATE OR REPLACE MACRO price_formation(lag, kt := NULL) AS TABLE
SELECT sec_id, event_date, open, high, low, close, volume, vwap, currency,
       knowledge_time, revision_seq, source_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY sec_id, event_date
               ORDER BY knowledge_time DESC, revision_seq DESC) AS rn
    FROM fact_price_eod
    WHERE knowledge_time <= event_date + lag
      AND (kt IS NULL OR knowledge_time <= kt)
) WHERE rn = 1;

CREATE OR REPLACE MACRO corp_action_asof(kt) AS TABLE
SELECT sec_id, action_type, ex_date, announce_time, ratio, amount, currency,
       knowledge_time, revision_seq, source_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY sec_id, action_type, ex_date
               ORDER BY knowledge_time DESC, revision_seq DESC) AS rn
    FROM fact_corp_action WHERE knowledge_time <= kt
) WHERE rn = 1;
"""

ALL_CHECKS = tuple(f"A{i}" for i in range(1, 12))


class MemoryBackend:
    """In-memory warehouse with a synthetic PASS audit receipt."""

    def __init__(self, con: duckdb.DuckDBPyConnection, checks=ALL_CHECKS) -> None:
        self.con = con
        self.checks = tuple(checks)
        self.connections = 0

    def availability(self, required_checks=()):
        missing = [c for c in required_checks if c not in self.checks]
        if missing:
            raise pl.PitdbUnavailable(f"audit receipt lacks {missing}")
        return {
            "backend": "memory",
            "lake_signature_sha256": "sha256:fixture",
            "audit": {"status": "PASS", "audited_at_utc": "2026-09-27T00:00:00Z",
                      "checks_passed": list(self.checks)},
        }

    def connect(self):
        self.connections += 1
        return self.con.cursor()


def _store() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(FIXTURE_SCHEMA)
    for source, pit_class in (("yahoo_eod", "OBSERVED_PIT"), ("vendor_restated", "NON_PIT"),
                              ("alfred", "RECONSTRUCTED_PIT"), ("exchange", "TRUE_PIT")):
        con.execute("INSERT INTO dim_source (source_id, pit_class) VALUES (?, ?)",
                    [source, pit_class])
    return con


def _security(con, sec_id: int, ticker: str, *, valid_from=None, valid_to=None,
              currency: str = "USD") -> None:
    con.execute(
        "INSERT INTO dim_security (sec_id, primary_ticker, exchange_mic, currency, "
        "asset_class) SELECT ?, ?, 'XNAS', ?, 'equity' WHERE NOT EXISTS "
        "(SELECT 1 FROM dim_security WHERE sec_id = ?)", [sec_id, ticker, currency, sec_id])
    con.execute("INSERT INTO dim_security_alias VALUES (?, 'ticker', ?, ?, ?, 'warehouse')",
                [sec_id, ticker, valid_from, valid_to])


def _bar(con, sec_id: int, day: dt.date, close: float, knowledge: dt.datetime, *,
         rev: int = 0, source: str = "yahoo_eod", currency: str = "USD",
         high: float | None = None, low: float | None = None) -> None:
    con.execute(
        "INSERT INTO fact_price_eod (sec_id, event_date, knowledge_time, revision_seq, "
        "open, high, low, close, volume, currency, is_settled, source_id, ingest_run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,TRUE,?,1)",
        [sec_id, day, knowledge, rev, close, high if high is not None else close * 1.01,
         low if low is not None else close * 0.99, close, 1_000.0, currency, source])


def _series(con, sec_id: int, days, closes, *, delay=dt.timedelta(hours=35),
            source: str = "yahoo_eod", knowledge: dt.datetime | None = None) -> None:
    for day, close in zip(days, closes):
        kt = knowledge or dt.datetime.combine(day, dt.time()) + delay
        _bar(con, sec_id, day, float(close), kt, source=source)


def _days(start: str, n: int) -> list[dt.date]:
    return [d.date() for d in pd.bdate_range(start, periods=n)]


def _config(**pit) -> dict:
    block = {"mode": "formation", "run_asof_utc": "2026-09-27T00:00:00Z",
             "availability_lag": "36h", "claim": "research", "allow_reconstructed": False}
    block.update(pit)
    return {"codes": ["NVDA.US"], "start_date": "2026-08-24", "end_date": "2026-09-25",
            "source": "pitdb", "pit": block}


def _loader(con, config=None, checks=ALL_CHECKS) -> pl.PitdbLoader:
    loader = pl.PitdbLoader(backend=MemoryBackend(con, checks))
    loader.bind_run_config(config or _config())
    return loader


BACKFILL_DAYS = [d.date() for d in pd.bdate_range("2025-01-02", "2026-08-21")]
LIVE_DAYS = [d.date() for d in pd.bdate_range("2026-08-24", periods=25)]


@pytest.fixture
def nvda_store():
    """NVDA like today's lake: a 2026-08-22 backfill, then five weeks of daily captures."""
    con = _store()
    _security(con, 1, "NVDA")
    _series(con, 1, BACKFILL_DAYS, np.linspace(100, 168, len(BACKFILL_DAYS)),
            knowledge=dt.datetime(2026, 8, 22, 23, 54))
    _series(con, 1, LIVE_DAYS, np.linspace(170, 180, len(LIVE_DAYS)))
    return con


@pytest.fixture
def clean_registry():
    """Restore the global loader registry: pitdb registers on explicit request."""
    saved = dict(LOADER_REGISTRY)
    yield
    LOADER_REGISTRY.clear()
    LOADER_REGISTRY.update(saved)


def _network_loaders_raise(monkeypatch) -> list[str]:
    """Replace every registered non-pitdb loader with one that fails the test if touched."""
    touched: list[str] = []
    _ensure_registered()

    def _raiser(name):
        class _Network:
            def __init__(self, *a, **k):
                touched.append(name)
                raise AssertionError(f"network loader {name} was constructed")
        _Network.name = name
        _Network.markets = {"us_equity", "index"}
        return _Network

    for name in list(LOADER_REGISTRY):
        if name != "pitdb":
            monkeypatch.setitem(LOADER_REGISTRY, name, _raiser(name))
    return touched


# ---------------------------------------------------------------------------
# The ten negative look-ahead tests
# ---------------------------------------------------------------------------


def test_la1_formation_uses_revision_zero_snapshot_uses_revision_one():
    """LA1: rev0 inside the lag feeds formation; a later snapshot sees rev1."""
    con = _store()
    _security(con, 1, "NVDA")
    day = dt.date(2026, 9, 1)
    _bar(con, 1, day, 100.0, dt.datetime(2026, 9, 1, 22, 0), rev=0)
    _bar(con, 1, day, 101.0, dt.datetime(2026, 9, 6, 10, 0), rev=1)
    _series(con, 1, _days("2026-09-02", 5), [100.5, 100.7, 100.9, 101.1, 101.3])
    window = {"start_date": "2026-09-01", "end_date": "2026-09-08"}

    formation = _loader(con, {**_config(), **window}).fetch(["NVDA.US"], **window)
    assert formation["NVDA.US"]["close"].iloc[0] == 100.0
    assert formation["NVDA.US"].attrs["pit"]["revised_after_formation"] == 1

    late = _loader(con, {**_config(mode="snapshot", run_asof_utc="2026-09-10T00:00:00Z"),
                         **window}).fetch(["NVDA.US"], **window)
    assert late["NVDA.US"]["close"].iloc[0] == 101.0

    early = _loader(con, {**_config(mode="snapshot", run_asof_utc="2026-09-03T00:00:00Z"),
                          **{"start_date": "2026-09-01", "end_date": "2026-09-01"}})
    assert early.fetch(["NVDA.US"], "2026-09-01", "2026-09-01")["NVDA.US"]["close"].iloc[0] == 100.0


@pytest.mark.parametrize("mode", ["snapshot", "formation"])
def test_la2_knowledge_after_run_asof_never_appears(mode):
    """LA2: nothing known after run_asof is served, in either mode."""
    con = _store()
    _security(con, 1, "NVDA")
    days = _days("2026-09-01", 15)
    for i, day in enumerate(days):
        known = dt.datetime.combine(day, dt.time(21))
        _bar(con, 1, day, 100.0 + i, known, rev=0)
        _bar(con, 1, day, 100.5 + i, known + dt.timedelta(hours=10), rev=1)
    asof = "2026-09-11T00:00:00Z"
    config = {**_config(mode=mode, run_asof_utc=asof, availability_lag="7 days"),
              "start_date": "2026-09-01", "end_date": "2026-09-10"}
    loader = _loader(con, config)
    frame = loader.fetch(["NVDA.US"], "2026-09-01", "2026-09-10")["NVDA.US"]
    cutoff = pd.Timestamp(asof)
    assert pd.Timestamp(frame.attrs["pit"]["max_knowledge_time_utc"]) <= cutoff
    # 2026-09-10's rev1 (known 09-11 07:00) is after the as-of: rev0 is served.
    assert frame.loc["2026-09-10", "close"] == 107.0
    assert frame.loc["2026-09-09", "close"] == 106.5
    assert pd.Timestamp(loader.run_provenance()["max_knowledge_time_utc"]) <= cutoff


def test_la3_formation_over_backfilled_period_raises(nvda_store):
    """LA3: formation over 2025 on a backfilled lake raises, naming the backfill."""
    config = {**_config(), "start_date": "2025-03-03", "end_date": "2025-06-30"}
    loader = _loader(nvda_store, config)
    with pytest.raises(pl.PitDataError, match=r"no eligible bars.*backfill kt 2026-08-22"):
        loader.fetch(["NVDA.US"], "2025-03-03", "2025-06-30")


def test_la4_non_pit_rows_refused_for_a_tradeable_claim():
    """LA4: NON_PIT and missing PIT class raise under claim=tradeable."""
    con = _store()
    _security(con, 1, "NVDA")
    _security(con, 2, "AMD")
    _security(con, 3, "AVGO")
    days = _days("2026-08-24", 20)
    _series(con, 1, days, np.linspace(170, 180, 20), source="vendor_restated")
    _series(con, 2, days, np.linspace(150, 155, 20), source="unregistered_feed")
    _series(con, 3, days, np.linspace(300, 310, 20), source="alfred")
    tradeable = _config(claim="tradeable")
    for code, pattern in (("NVDA.US", "NON_PIT"), ("AMD.US", "MISSING"),
                          ("AVGO.US", "allow_reconstructed")):
        with pytest.raises(pl.PitClaimError, match=pattern):
            _loader(con, tradeable).fetch([code], "2026-08-24", "2026-09-18")
    # Research claims keep the rows and record the class; reconstructed rows
    # pass a tradeable claim only with the explicit opt-in.
    research = _loader(con).fetch(["NVDA.US"], "2026-08-24", "2026-09-18")
    assert research["NVDA.US"].attrs["pit"]["pit_classes"] == ["NON_PIT"]
    opted = _loader(con, _config(claim="tradeable", allow_reconstructed=True))
    assert "AVGO.US" in opted.fetch(["AVGO.US"], "2026-08-24", "2026-09-18")
    with pytest.raises(pl.PitClaimError, match="research-only"):
        pl.parse_pit_block(_config(mode="snapshot", claim="tradeable"))


def test_la5_missing_symbol_raises_and_never_reaches_the_network(
        monkeypatch, nvda_store, clean_registry):
    """LA5: a missing symbol raises NoAvailableSourceError; network loaders untouched."""
    from backtest.runner import fetch_data_map

    touched = _network_loaders_raise(monkeypatch)
    monkeypatch.setattr(pl, "default_backend", lambda: MemoryBackend(nvda_store))
    config = {**_config(), "codes": ["NVDA.US", "NOPE.US"]}
    with pytest.raises(NoAvailableSourceError, match="NOPE.US"):
        fetch_data_map(config)
    assert touched == []


def test_la6_benchmark_is_served_from_pitdb_and_yfinance_is_never_called(
        monkeypatch, tmp_path, nvda_store, clean_registry):
    """LA6: an end-to-end pitdb run reads SPY.US from pitdb; yfinance never runs."""
    from backtest import runner
    from src.config.accessor import reset_env_config

    _security(nvda_store, 36, "SPY")
    _series(nvda_store, 36, _days("2026-08-24", 25), np.linspace(640, 660, 25))

    def _no_yfinance(*_a, **_k):
        raise AssertionError("yfinance must not be used for a pitdb benchmark")

    monkeypatch.setattr("backtest.benchmark.YfinanceLoader", _no_yfinance)
    touched = _network_loaders_raise(monkeypatch)
    monkeypatch.setattr(pl, "default_backend", lambda: MemoryBackend(nvda_store))
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    reset_env_config()
    try:
        run_dir = tmp_path / "run"
        (run_dir / "code").mkdir(parents=True)
        (run_dir / "config.json").write_text(json.dumps(
            {**_config(), "benchmark": "SPY.US", "initial_cash": 100_000}), encoding="utf-8")
        (run_dir / "code" / "signal_engine.py").write_text(
            "import pandas as pd\n\n\n"
            "class SignalEngine:\n"
            "    def generate(self, data_map):\n"
            "        return {c: pd.Series(0.5, index=df.index) for c, df in data_map.items()}\n",
            encoding="utf-8")
        runner.main(run_dir)
    finally:
        reset_env_config()

    card = json.loads((run_dir / "run_card.json").read_text(encoding="utf-8"))
    assert card["metrics"]["benchmark_ticker"] == "SPY.US"
    spy = card["pit"]["symbols"]["SPY.US"]
    assert spy["role"] == "benchmark" and spy["bars"] == 25
    assert card["pit"]["mode"] == "formation"
    assert card["pit"]["declared"]["run_asof_utc"] == "2026-09-27T00:00:00Z"
    assert card["data_sources"] == ["pitdb"]
    assert touched == []
    assert "## Point-in-time" in (run_dir / "run_card.md").read_text(encoding="utf-8")


def test_la7_cache_on_and_two_as_of_dates_give_different_frames(monkeypatch, tmp_path):
    """LA7: with the VT loader cache enabled, two as-of dates still differ; nothing is cached."""
    from src.config.accessor import reset_env_config

    con = _store()
    _security(con, 1, "NVDA")
    day = dt.date(2026, 9, 1)
    _bar(con, 1, day, 100.0, dt.datetime(2026, 9, 1, 22, 0), rev=0)
    _bar(con, 1, day, 150.0, dt.datetime(2026, 9, 4, 22, 0), rev=1)
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", "true")
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE_ROOT", str(cache_root))
    reset_env_config()
    try:
        from backtest.loaders.base import loader_cache_enabled

        assert loader_cache_enabled() is True
        frames = []
        for asof in ("2026-09-02T12:00:00Z", "2026-09-05T12:00:00Z"):
            config = {**_config(mode="snapshot", run_asof_utc=asof),
                      "start_date": "2026-09-01", "end_date": "2026-09-01"}
            frames.append(_loader(con, config).fetch(["NVDA.US"], "2026-09-01", "2026-09-01"))
    finally:
        reset_env_config()
    assert frames[0]["NVDA.US"]["close"].iloc[0] == 100.0
    assert frames[1]["NVDA.US"]["close"].iloc[0] == 150.0
    assert not cache_root.exists() or not any(cache_root.rglob("*"))


def test_la8_stale_audit_receipt_makes_the_loader_unavailable(
        monkeypatch, pit_project, clean_registry):
    """LA8: a stale receipt -> unavailable, and an explicit request never falls back."""
    from backtest.runner import fetch_data_map

    project, runtime = pit_project
    guard = pl.load_pit_guard()
    signature = guard.lake_signature(project)
    guard.write_json_atomic(runtime / guard.INDEX_RECEIPT,
                            {"signature": signature, "refreshed_at_utc": _now_iso()})
    stale = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=2)).isoformat()
    guard.write_json_atomic(runtime / guard.AUDIT_RECEIPT, {
        "signature": signature, "audited_at_utc": stale, "status": "PASS",
        "checks_passed": list(ALL_CHECKS)})

    loader = pl.PitdbLoader()
    assert loader.is_available() is False
    assert "expired" in (loader.unavailable_reason or "")

    touched = _network_loaders_raise(monkeypatch)
    with pytest.raises(NoAvailableSourceError, match="does not fall back"):
        fetch_data_map(_config())
    assert touched == []

    # Positive control: the same layout with a fresh receipt is available, and
    # a receipt that predates audit check A11 still refuses formation mode.
    guard.write_json_atomic(runtime / guard.AUDIT_RECEIPT, {
        "signature": signature, "audited_at_utc": _now_iso(), "status": "PASS",
        "checks_passed": list(ALL_CHECKS[:10])})
    fresh = pl.PitdbLoader()
    assert fresh.is_available() is True
    with pytest.raises(pl.PitdbUnavailable, match="A11"):
        fresh._get_backend().availability(("A11",))


def test_la9_reused_ticker_maps_to_the_security_of_the_window():
    """LA9: ticker reuse resolves by date; a window spanning the reuse raises."""
    con = _store()
    _security(con, 101, "ABC", valid_to=dt.date(2025, 1, 1))
    _security(con, 202, "ABC", valid_from=dt.date(2025, 1, 1))
    _series(con, 101, _days("2024-06-03", 60), np.full(60, 10.0))
    _series(con, 202, _days("2024-06-03", 250), np.full(250, 500.0))
    old = {**_config(mode="snapshot"), "start_date": "2024-07-01", "end_date": "2024-08-15"}
    new = {**_config(mode="snapshot"), "start_date": "2025-02-03", "end_date": "2025-03-31"}
    got_old = _loader(con, old).fetch(["ABC.US"], "2024-07-01", "2024-08-15")["ABC.US"]
    got_new = _loader(con, new).fetch(["ABC.US"], "2025-02-03", "2025-03-31")["ABC.US"]
    assert set(got_old["close"]) == {10.0} and got_old.attrs["pit"]["sec_id"] == 101
    assert set(got_new["close"]) == {500.0} and got_new.attrs["pit"]["sec_id"] == 202
    span = {**_config(mode="snapshot"), "start_date": "2024-12-02", "end_date": "2025-01-31"}
    with pytest.raises(pl.PitDataError, match="maps to 2 securities"):
        _loader(con, span).fetch(["ABC.US"], "2024-12-02", "2025-01-31")


def test_la10_large_unexplained_move_is_flagged():
    """LA10: a SOXS-style -94.6% print raises unless explained or vouched for."""
    con = _store()
    _security(con, 55, "SOXS")
    days = [dt.date(2026, 5, 20), dt.date(2026, 5, 21), dt.date(2026, 5, 22),
            dt.date(2026, 5, 26), dt.date(2026, 5, 27)]
    _series(con, 55, days, [1281.0, 1243.5, 1159.5, 62.9, 65.3])
    window = {"start_date": "2026-05-20", "end_date": "2026-05-27"}
    research = {**_config(mode="snapshot"), **window}
    with pytest.raises(pl.PitDataError, match=r"SOXS\.US.*-94\.58% on 2026-05-26"):
        _loader(con, research).fetch(["SOXS.US"], **window)

    vouched = {**_config(mode="snapshot", accepted_moves=[
        {"code": "SOXS.US", "date": "2026-05-26", "reason": "reviewed print"}]), **window}
    frame = _loader(con, vouched).fetch(["SOXS.US"], **window)["SOXS.US"]
    assert frame.attrs["pit"]["accepted_moves"][0]["reason"] == "reviewed print"

    con.execute(
        "INSERT INTO fact_corp_action (sec_id, action_type, ex_date, announce_time, "
        "knowledge_time, ratio, source_id) VALUES (55, 'split', DATE '2026-05-26', "
        "TIMESTAMP '2026-05-15 12:00:00', TIMESTAMP '2026-05-15 12:00:00', 0.05, 'exchange')")
    explained = _loader(con, research)
    frame = explained.fetch(["SOXS.US"], **window)["SOXS.US"]
    assert frame.attrs["pit"]["explained_moves"][0]["corp_actions"][0]["type"] == "split"
    assert any("unadjusted split" in w for w in explained.run_provenance()["warnings"])
    # The loader never adjusts, so a tradeable claim cannot span the action.
    with pytest.raises(pl.PitClaimError, match="unadjusted corporate action"):
        _loader(con, {**_config(claim="tradeable", availability_lag="3 days"), **window}).fetch(
            ["SOXS.US"], **window)
    # A corporate action recorded only after the as-of explains nothing.
    before = {**_config(mode="snapshot", run_asof_utc="2026-05-31T00:00:00Z"), **window}
    con.execute("UPDATE fact_corp_action SET knowledge_time = TIMESTAMP '2026-06-15 00:00:00'")
    with pytest.raises(pl.PitDataError, match="unexplained one-day move"):
        _loader(con, before).fetch(["SOXS.US"], **window)


# ---------------------------------------------------------------------------
# Binding, gating, identity and provenance
# ---------------------------------------------------------------------------


def test_unbound_loader_fails_closed(nvda_store):
    loader = pl.PitdbLoader(backend=MemoryBackend(nvda_store))
    with pytest.raises(pl.PitBindingError, match="unbound"):
        loader.fetch(["NVDA.US"], "2026-08-24", "2026-09-18")


@pytest.mark.parametrize(
    ("pit", "pattern"),
    [
        (None, "`pit` block"),
        ({"mode": "formation"}, "claim"),
        ({**_config()["pit"], "mode": "latest"}, "pit.mode"),
        ({**_config()["pit"], "run_asof_utc": "2026-09-27T00:00:00"}, "UTC offset"),
        ({**_config()["pit"], "run_asof_utc": "2200-01-01T00:00:00Z"}, "future"),
        ({**_config()["pit"], "availability_lag": "soon"}, "not a duration"),
        ({**_config()["pit"], "availability_lag": "90 days"}, r"\[0, 30 days\]"),
        ({**_config()["pit"], "allow_reconstructed": "yes"}, "true or false"),
        ({**_config()["pit"], "as_of": "x"}, "unknown `pit` keys"),
    ],
)
def test_pit_block_is_validated(pit, pattern):
    config = {**_config()}
    if pit is None:
        config.pop("pit")
    else:
        config["pit"] = pit
    with pytest.raises(pl.PitBindingError, match=pattern):
        pl.parse_pit_block(config)


def test_window_after_the_as_of_is_refused():
    config = {**_config(run_asof_utc="2026-09-10T00:00:00Z"), "end_date": "2026-09-25"}
    with pytest.raises(pl.PitBindingError, match="after pit.run_asof_utc"):
        pl.parse_pit_block(config)


def test_iso_durations_and_lag_units_parse():
    binding = pl.parse_pit_block(_config(availability_lag="P1DT12H"))
    assert binding.availability_lag == pd.Timedelta(hours=36)
    assert binding.to_record()["availability_lag_seconds"] == 36 * 3600


def test_pit_block_cannot_ride_on_a_source_that_does_not_bind_it(monkeypatch):
    from backtest.runner import fetch_data_map

    class _Plain:
        name = "yahoo"

        def fetch(self, *a, **k):
            raise AssertionError("must not fetch")

    monkeypatch.setattr("backtest.runner._get_loader", lambda source: _Plain)
    with pytest.raises(ValueError, match="needs a loader that binds it"):
        fetch_data_map({**_config(), "source": "yahoo"})
    with pytest.raises(ValueError, match="cannot honour a `pit` block"):
        fetch_data_map({**_config(), "source": "auto"})


def test_tradeable_window_must_start_on_formation_eligible_history(nvda_store):
    config = {**_config(claim="tradeable"), "start_date": "2026-07-01"}
    with pytest.raises(pl.PitClaimError, match="first formation-eligible bar is 2026-08-24"):
        _loader(nvda_store, config).fetch(["NVDA.US"], "2026-07-01", "2026-09-25")
    research = _loader(nvda_store, {**_config(), "start_date": "2026-07-01"})
    frame = research.fetch(["NVDA.US"], "2026-07-01", "2026-09-25")["NVDA.US"]
    assert frame.index[0] == pd.Timestamp("2026-08-24")
    assert frame.attrs["pit"]["ineligible_leading_bars"] > 0
    assert any("treated as unknown" in w for w in research.run_provenance()["warnings"])


def test_snapshot_records_backfill_share_and_frame_contract(nvda_store):
    config = {**_config(mode="snapshot"), "start_date": "2025-01-02", "end_date": "2026-09-25"}
    loader = _loader(nvda_store, config)
    frame = loader.fetch(["NVDA.US"], "2025-01-02", "2026-09-25")["NVDA.US"]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "trade_date" and frame.index.tz is None
    assert all(frame[c].dtype == "float64" for c in frame.columns)
    assert frame.attrs["quote_currency"] == "USD"
    pit = frame.attrs["pit"]
    total = len(BACKFILL_DAYS) + len(LIVE_DAYS)
    assert pit["bars"] == total and pit["backfill_bars"] == len(BACKFILL_DAYS)
    run = loader.run_provenance()
    assert run["backfill_share"] == pytest.approx(len(BACKFILL_DAYS) / total, rel=1e-5)
    assert run["pit_classes"] == ["OBSERVED_PIT"]
    assert run["warehouse"]["macros_sha256"]["price_formation"].startswith("sha256:")


def test_formation_mode_requires_audit_check_a11(nvda_store):
    loader = _loader(nvda_store, checks=ALL_CHECKS[:10])
    with pytest.raises(pl.PitdbUnavailable, match="A11"):
        loader.fetch(["NVDA.US"], "2026-08-24", "2026-09-25")


def test_bad_ohlc_bar_raises_instead_of_being_dropped():
    con = _store()
    _security(con, 1, "NVDA")
    _series(con, 1, _days("2026-08-24", 5), [170, 171, 172, 173, 174])
    _bar(con, 1, dt.date(2026, 8, 31), 175.0, dt.datetime(2026, 9, 1, 11), high=160.0)
    with pytest.raises(pl.PitDataError, match="violate OHLC"):
        _loader(con).fetch(["NVDA.US"], "2026-08-24", "2026-09-25")


def test_index_codes_and_intraday_requests(nvda_store):
    _security(nvda_store, 45, "^GSPC")
    _series(nvda_store, 45, _days("2026-08-24", 25), np.linspace(6400, 6500, 25))
    loader = _loader(nvda_store)
    assert "^GSPC" in loader.fetch(["^GSPC"], "2026-08-24", "2026-09-25")
    with pytest.raises(ValueError, match="end-of-day"):
        loader.fetch(["^GSPC"], "2026-08-24", "2026-09-25", interval="1H")


# ---------------------------------------------------------------------------
# The sanctioned-read gate (what a query reads, from DuckDB's parse tree)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM fact_price_eod",
        "select close from FACT_PRICE_EOD where sec_id = 1",
        "SELECT * FROM price_asof(now()) p JOIN fact_observation o ON true",
        "WITH x AS (SELECT * FROM fact_price_eod) SELECT * FROM x",
        "SELECT * FROM read_parquet('lake/fact_price_eod/data.parquet')",
        "SELECT * FROM 'lake/fact_price_eod/data.parquet'",
        "SELECT * FROM v_price_latest",
        "SELECT * FROM other.main.dim_security",
        "SELECT * FROM price_asof(now()); SELECT 1",
        "DELETE FROM dim_security",
    ],
)
def test_unsanctioned_reads_are_refused(sql):
    con = _store()
    with pytest.raises(pl.UnsanctionedQueryError):
        pl.require_sanctioned(con, sql)


def test_the_loaders_own_statements_are_sanctioned():
    con = _store()
    for sql in (pl._ALIAS_SQL, pl._SNAPSHOT_SQL, pl._FORMATION_SQL, pl._CORP_ACTION_SQL):
        pl.require_sanctioned(con, sql)
    tables, functions = pl.referenced_relations(con, pl._FORMATION_SQL)
    assert tables == {"dim_source"} and functions == {"price_formation"}
    # Reformatting a sanctioned query changes nothing: the check reads the tree.
    pl.require_sanctioned(con, "select  *  from\n PRICE_ASOF( now() )")


def test_fixture_macros_match_the_warehouse_schema():
    """Drift guard: compare the fixture's macros with the real schema.sql when reachable."""
    candidates = []
    if os.environ.get("PITDB_SCHEMA_SQL"):
        candidates.append(Path(os.environ["PITDB_SCHEMA_SQL"]))
    if os.environ.get("INVESTMENT_AI_PROJECT_ROOT"):
        candidates.append(Path(os.environ["INVESTMENT_AI_PROJECT_ROOT"])
                          / "implementation" / "pit_warehouse" / "pitdb" / "schema.sql")
    schema = next((p for p in candidates if p.is_file()), None)
    if schema is None:
        pytest.skip("warehouse schema.sql not reachable (set PITDB_SCHEMA_SQL)")

    def macros(sql: str) -> dict[str, str]:
        con = duckdb.connect(":memory:")
        con.execute(sql)
        rows = con.execute(pl._MACRO_CATALOG_SQL).fetchall()
        return {n: d for n, d in rows if n in pl.SANCTIONED_TABLE_FUNCTIONS}

    assert macros(schema.read_text(encoding="utf-8")) == macros(FIXTURE_SCHEMA)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registry_wiring_is_explicit_only(clean_registry, monkeypatch, nvda_store):
    from backtest.loaders.registry import get_loader_cls_with_fallback

    _ensure_registered()
    assert "pitdb" in VALID_SOURCES
    assert is_no_network_fallback_source("pitdb")
    assert all("pitdb" not in chain for chain in FALLBACK_CHAINS.values())
    # Importing the module does not register it: upstream counts are unchanged
    # until someone asks for source="pitdb".
    LOADER_REGISTRY.pop("pitdb", None)
    _ensure_registered()
    assert "pitdb" not in LOADER_REGISTRY
    monkeypatch.setattr(pl, "default_backend", lambda: MemoryBackend(nvda_store))
    assert get_loader_cls_with_fallback("pitdb") is pl.PitdbLoader
    assert LOADER_REGISTRY["pitdb"] is pl.PitdbLoader


def test_run_card_carries_the_pit_block(tmp_path):
    from backtest.run_card import write_run_card

    config = {**_config(), "_run_card_pit": {"mode": "formation", "bars": 3,
                                            "pit_classes": ["OBSERVED_PIT"],
                                            "symbols": {"NVDA.US": {"role": "strategy",
                                                                    "sec_id": 1, "bars": 3}},
                                            "warnings": ["w1"]}}
    card = write_run_card(tmp_path, config, {"total_return": 0.1}, data_sources=["pitdb"])
    assert card["pit"]["declared"]["claim"] == "research"
    assert card["pit"]["symbols"]["NVDA.US"]["sec_id"] == 1
    md = (tmp_path / "run_card.md").read_text(encoding="utf-8")
    assert "## Point-in-time" in md and "warning: w1" in md
    plain = write_run_card(tmp_path / "plain", {"source": "yahoo"}, {"total_return": 0.1})
    assert "pit" not in plain


# ---------------------------------------------------------------------------
# The production backend (receipts through the extension's pit_guard)
# ---------------------------------------------------------------------------

_FAKE_CONFIG = '''
import os, pathlib
PROJECT_ROOT = pathlib.Path(os.environ["INVESTMENT_PROJECT_ROOT"])
WAREHOUSE_ROOT = PROJECT_ROOT / "implementation" / "pit_warehouse"
LAKE_ROOT = WAREHOUSE_ROOT / "lake"
DB_PATH = pathlib.Path(os.environ["PITDB_INDEX"])
SCHEMA_SQL = pathlib.Path(__file__).with_name("schema.sql")
PERSISTED_TABLES = ["dim_source", "dim_security", "dim_security_alias", "fact_price_eod",
                    "fact_corp_action"]
'''


def _now_iso() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


@pytest.fixture
def pit_project(monkeypatch, tmp_path):
    """A canonical-looking project with a stand-in pitdb package and E: index."""
    import sys

    before = {name: mod for name, mod in sys.modules.items()
              if name == "pitdb" or name.startswith("pitdb.")}
    yield _project_layout(monkeypatch, tmp_path)
    for name in [m for m in sys.modules if m == "pitdb" or m.startswith("pitdb.")]:
        sys.modules.pop(name, None)
    sys.modules.update(before)


def _project_layout(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    import sys

    project = tmp_path / "work" / "Investment-AI-Drive-Research"
    package = project / "implementation" / "pit_warehouse" / "pitdb"
    package.mkdir(parents=True)
    (project / "AGENTS.md").write_text("governance", encoding="utf-8")
    (project / "market_actor_sim").mkdir()
    (project / "market_actor_sim" / "run_governed_pilot.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text('__version__ = "test"\n', encoding="utf-8")
    (package / "config.py").write_text(_FAKE_CONFIG, encoding="utf-8")
    (package / "schema.sql").write_text(FIXTURE_SCHEMA, encoding="utf-8")
    for table in ("dim_source", "fact_price_eod"):
        target = project / "implementation" / "pit_warehouse" / "lake" / table
        target.mkdir(parents=True)
        (target / "data.parquet").write_bytes(b"PAR1-fixture")
    index = tmp_path / "e" / "pitdb" / "pit.duckdb"
    index.parent.mkdir(parents=True)
    duckdb.connect(str(index)).close()
    runtime = tmp_path / "e" / "runtime"
    runtime.mkdir(parents=True)
    for key, value in (("INVESTMENT_AI_PROJECT_ROOT", project), ("INVESTMENT_PROJECT_ROOT", project),
                       ("PITDB_INDEX", index), ("VIBE_TRADING_HOME", runtime)):
        monkeypatch.setenv(key, str(value))
    for name in [m for m in sys.modules if m == "pitdb" or m.startswith("pitdb.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return project, runtime
