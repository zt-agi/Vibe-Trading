"""Fixtures for the zt_events extension tests (ZT add-on).

The store is an in-memory DuckDB built from the project's real
``pitdb/schema.sql`` and ``pitdb/events_schema.sql``:

* NVIDIA's real 8-K / 10-Q / 10-K filings and XBRL EPS, loaded through the
  real ``sec_8k_earnings`` / ``sec_xbrl_eps`` connectors from the saved
  data.sec.gov samples in the project's ``tests/fixtures/sec`` (no network);
* six synthetic issuers AAA..FFF with quarterly Item 2.02 releases, 10-Q EPS
  and closes whose drift over sessions +2..+20 follows the sign of the
  earnings surprise, so the study has an effect to find:
  EEE files its 10-Q twenty days after the release (SUE known late) and FFF
  files no EPS at all (SUE never known);
* closes for every ticker and SPY, knowable at 22:30 UTC on their date.

The pitdb package lives in ZT's private project, so the store tests need
``INVESTMENT_AI_PROJECT_ROOT`` (the canonical folder, or a copy of it) and
are skipped without it. Nothing is written outside pytest's tmp folders.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

EXT = Path(__file__).resolve().parents[1]
if str(EXT) not in sys.path:
    sys.path.insert(0, str(EXT))

import core  # noqa: E402
from src.quantlib import event_study as es  # noqa: E402

NY = ZoneInfo("America/New_York")
SYNTHETIC = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
FIRST_PERIOD, LAST_PERIOD = "2014-03-31", "2026-06-30"
PRICE_START, PRICE_END = "2013-06-01", "2026-09-25"
#: The fresh releases of the last quarter (period ending 2026-06-30), New York time.
FRESH = {
    "AAA": ("2026-07-28 16:05", 1.0 / 24, +0.45),    # after the close, 10-Q an hour later, big beat
    "BBB": ("2026-07-28 16:10", 1.0 / 24, -0.45),    # after the close, big miss
    "CCC": ("2026-07-29 07:00", 0.5 / 24, +0.01),    # pre-market, in line
    "DDD": ("2026-07-29 07:05", 0.5 / 24, +0.40),    # pre-market, beat
    "EEE": ("2026-07-28 16:15", 20.0, +0.40),        # 10-Q twenty days later
    "FFF": ("2026-07-28 16:20", None, +0.40),        # no EPS ever
}
ASOF_ENTRY = "2026-07-29T22:00:00Z"   # after Wednesday's close, before Thursday's open
#: The synthetic issuers' quarter plan (release, 10-Q, EPS, surprise), for assertions.
PLAN: dict = {}


def _utc(local: str) -> datetime:
    return pd.Timestamp(local, tz=NY).tz_convert("UTC").tz_localize(None).to_pydatetime()


def _project_or_skip() -> Path:
    if not os.environ.get("INVESTMENT_AI_PROJECT_ROOT"):
        pytest.skip("INVESTMENT_AI_PROJECT_ROOT not set (pitdb lives in the private project)")
    return core.project()


def _register(con) -> None:
    con.execute("INSERT INTO dim_source (source_id, pit_class, status) VALUES "
                "('yahoo_eod', 'OBSERVED_PIT', 'LIVE'), ('sec_8k_earnings', 'TRUE_PIT', 'LIVE'), "
                "('sec_xbrl_eps', 'TRUE_PIT', 'LIVE')")
    for sec_id, ticker in enumerate(("SPY", "NVDA") + SYNTHETIC, start=1):
        con.execute("INSERT INTO dim_security (sec_id, primary_ticker, exchange_mic, currency) "
                    "VALUES (?, ?, 'XNAS', 'USD')", [sec_id, ticker])
        con.execute("INSERT INTO dim_security_alias VALUES (?, 'ticker', ?, NULL, NULL, 'warehouse')",
                    [sec_id, ticker])


def _quarter_plan(rng) -> dict:
    """Release instants, 10-Q instants and EPS per synthetic issuer and quarter."""
    periods = pd.date_range(FIRST_PERIOD, LAST_PERIOD, freq="QE")
    plan = {}
    for k, ticker in enumerate(SYNTHETIC):
        rows, eps = [], {}
        for i, period in enumerate(periods):
            last = period == periods[-1]
            if last:
                release_text, q_lag_days, ue = FRESH[ticker]
                release = _utc(release_text)
            else:
                hour = "16:05" if i % 2 == 0 else "07:00"
                release = _utc(f"{(period + pd.Timedelta(days=24 + k)).date()} {hour}")
                q_lag_days = {"EEE": 20.0, "FFF": None}.get(ticker, 1.0 / 24)
                ue = float(rng.normal(0.0, 0.1))
            base = 1.0 + 0.1 * (i % 4)
            eps[i] = base if i < 4 else eps[i - 4] + ue
            ten_q = None if q_lag_days is None else release + timedelta(days=q_lag_days)
            rows.append({"period": period, "release": release, "ten_q": ten_q,
                         "eps": eps[i], "ue": ue if i >= 4 else 0.0})
        plan[ticker] = rows
    return plan


def _prices(rng, plan, nvda_events) -> pd.DataFrame:
    sessions = es.us_equity_sessions(PRICE_START, PRICE_END)
    closes = es.session_close_times(sessions)
    n = len(sessions)
    market = rng.normal(0.0004, 0.01, n)
    data = {"SPY": 400 * np.cumprod(1 + market)}
    for ticker in ("NVDA",) + SYNTHETIC:
        r = 0.0002 + 1.1 * market + rng.normal(0, 0.015, n)
        if ticker in plan:
            for q in plan[ticker]:
                day0 = int(es.first_session_after([q["release"]], closes)[0])
                if day0 < 0:
                    continue
                drift = 0.002 * float(np.clip(q["ue"] / 0.1, -2.5, 2.5))
                r[day0 + 2: day0 + 21] += drift
        data[ticker] = 50 * np.cumprod(1 + r)
    return pd.DataFrame(data, index=sessions)


def _write_prices(con, frame: pd.DataFrame) -> None:
    ids = dict(con.execute("SELECT primary_ticker, sec_id FROM dim_security").fetchall())
    long = frame.stack().rename("close").reset_index()
    long.columns = ["event_date", "ticker", "close"]
    long["sec_id"] = long["ticker"].map(ids)
    long["knowledge_time"] = long["event_date"] + pd.Timedelta(hours=22, minutes=30)
    long = long[["sec_id", "event_date", "knowledge_time", "close"]]
    con.register("_px", long)
    con.execute("INSERT INTO fact_price_eod (sec_id, event_date, knowledge_time, revision_seq, open, high, "
                "low, close, volume, currency, is_settled, source_id, ingest_run_id) SELECT sec_id, "
                "event_date::DATE, knowledge_time, 0, close, close, close, close, 1e6, 'USD', TRUE, "
                "'yahoo_eod', 1 FROM _px")
    con.unregister("_px")


def _write_synthetic_filings(con, plan, events_mod) -> None:
    from pitdb.identity import ensure_series
    from pitdb.writer import write_observations
    rows, obs = [], []
    for ticker, quarters in plan.items():
        for i, q in enumerate(quarters):
            accession = f"9{SYNTHETIC.index(ticker)}{i:02d}000000-26-{i:06d}"
            key = q["period"] + pd.Timedelta(days=15)
            payload = {"event_key": f"sec:{accession}", "ticker": ticker, "accession": accession,
                       "form": "8-K", "items": ["2.02", "9.01"], "cik": f"{i:010d}",
                       "filing_date": str(q["release"].date()), "report_date": str(q["release"].date()),
                       "acceptance_raw": q["release"].isoformat() + ".000Z",
                       "fiscal_period_end_est": str(q["period"].date()), "fiscal_period_basis": "projected",
                       "fiscal_period_key": f"{(key.replace(day=1) - pd.Timedelta(days=1)):%Y-%m}",
                       "url": f"https://example.invalid/{accession}", "pit_class": "TRUE_PIT"}
            rows.append({"event_type": "earnings_8k", "event_time": q["release"],
                         "knowledge_time": q["release"], "title": f"{ticker} 8-K", "payload": payload})
            if ticker == "AAA" and i == 20:     # an amendment in the same quarter, two days later
                amend = dict(payload, event_key=f"sec:{accession}A", accession=f"{accession}A", form="8-K/A")
                later = q["release"] + timedelta(days=2)
                rows.append({"event_type": "earnings_8k", "event_time": later, "knowledge_time": later,
                             "title": f"{ticker} 8-K/A", "payload": amend})
            if q["ten_q"] is not None:
                obs.append({"series_id": f"SEC:{ticker}:EarningsPerShareDiluted:USD/shares:3M",
                            "event_time": q["period"], "knowledge_time": q["ten_q"],
                            "value_num": round(q["eps"], 6)})
    events_mod.write_events(con, rows, "sec_8k_earnings", 1)
    frame = pd.DataFrame(obs)
    for series_id in frame["series_id"].unique():
        ensure_series(con, series_id, "sec_xbrl_eps", freq="Q", pit_class="TRUE_PIT")
    write_observations(con, frame, "sec_xbrl_eps", 1)


def _load_nvda(con, project: Path) -> None:
    fixtures = project / "implementation" / "pit_warehouse" / "tests" / "fixtures" / "sec"
    import json

    from pitdb.connectors import sec_8k_earnings as S
    subs = json.loads((fixtures / "nvda_submissions_2026-09-29.json").read_text(encoding="utf-8"))
    subs["filings"]["files"] = []
    facts = json.loads((fixtures / "nvda_companyfacts_eps_2026-09-29.json").read_text(encoding="utf-8"))

    def fetcher(url, **_):
        return types.SimpleNamespace(json=lambda: subs if "/submissions/" in url else facts)

    S._SUBMISSIONS.clear()
    original = S.PACER.wait
    S.PACER.wait = lambda: None
    try:
        for cls in (S.Sec8kEarnings, S.SecXbrlEps):
            connector = cls()
            connector.CIKS = {"NVDA": "0001045810"}
            connector.fetcher = fetcher
            connector.run(con, 2)
    finally:
        S.PACER.wait = original
        S._SUBMISSIONS.clear()


@pytest.fixture(scope="session")
def warehouse():
    project = _project_or_skip()
    import duckdb

    events_mod, _ = core.pitdb()
    from pitdb import identity
    identity._ENT_CACHE.clear()
    identity._SEC_CACHE.clear()
    con = duckdb.connect(":memory:")
    con.execute((project / "implementation" / "pit_warehouse" / "pitdb" / "schema.sql").read_text(encoding="utf-8"))
    events_mod.ensure_event_schema(con)
    rng = np.random.default_rng(20260929)
    plan = _quarter_plan(rng)
    _register(con)
    _load_nvda(con, project)
    _write_synthetic_filings(con, plan, events_mod)
    _write_prices(con, _prices(rng, plan, None))
    PLAN.clear()
    PLAN.update(plan)
    return con


@pytest.fixture
def installed(warehouse, monkeypatch):
    """Route every core.store() call to the fixture warehouse."""
    monkeypatch.setattr(core, "STORE_FACTORY", lambda: (warehouse, {"store": "test fixture"}))
    return warehouse
