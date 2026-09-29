"""pead_8k strategy template (ZT add-on): the run builder, the signal timing, VT's runner end to end.

The builder reads the fixture warehouse through zt_events' core (conftest.py);
the end-to-end test runs VT's own ``backtest.runner.main`` on the built run
directory with the pitdb loader's backend pointed at that same warehouse, so
prices, the benchmark and the events all come from one point-in-time store.
Store-backed tests need INVESTMENT_AI_PROJECT_ROOT (see conftest.py); the
timing tests run everywhere.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pandas as pd
import pytest

import core
from src.quantlib import event_study as es

STRATEGY = Path(__file__).resolve().parents[1] / "strategies" / "pead_8k"
RUN_ASOF = "2026-09-25T23:00:00Z"
SESSIONS = es.us_equity_sessions("2026-07-20", "2026-12-31")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_run = _load(STRATEGY / "build_run.py", "zt_pead_build_run")


def _vt_load(path: Path, name: str):
    """Load a signal engine exactly as VT's runner does (AST sandbox, then the interface check)."""
    from backtest.runner import _load_module_from_file, _validate_signal_engine_class

    module = _load_module_from_file(path, name)
    _validate_signal_engine_class(module.SignalEngine)
    return module


def _engine(tmp_path: Path, events, *, hold: int = 3, short=(), name: str = "engine"):
    path = tmp_path / f"{name}.py"
    path.write_text(build_run.render_engine(events, hold=hold, long_buckets=("Q5",), short_buckets=short,
                                            slot_weight=0.1), encoding="utf-8")
    return _vt_load(path, f"zt_pead_{name}_{tmp_path.name}")


def _event(ticker: str, accepted: str, known: str, bucket: str = "Q5") -> dict:
    return {"ticker": ticker, "accession": f"acc-{ticker}-{accepted[:10]}", "accepted_utc": accepted,
            "sue_known_utc": known, "sue": 1.5, "bucket": bucket}


def _held(signal: pd.Series) -> list[str]:
    return [str(day.date()) for day in signal.index[signal != 0]]


def _frames(*tickers: str) -> dict:
    frame = pd.DataFrame({"open": 1.0, "close": 1.0}, index=SESSIONS)
    return {f"{t}.US": frame for t in tickers}


# --------------------------------------------------------------------------- signal timing (no store)


def test_the_shipped_template_passes_vts_sandbox_and_trades_nothing():
    module = _vt_load(STRATEGY / "signal_engine.py", "zt_pead_template")
    assert module.EVENTS == () and module.HOLD_SESSIONS == 20 and module.LONG_BUCKETS == ("Q5",)
    out = module.SignalEngine().generate(_frames("NVDA"))
    assert not out["NVDA.US"].any()


def test_the_decision_bar_is_the_first_close_after_the_release_and_the_sue():
    events = [
        _event("AAA", "2026-07-28T20:05:00Z", "2026-07-28T21:05:00Z"),   # Tue 16:05 New York: after the close
        _event("BBB", "2026-07-29T11:00:00Z", "2026-07-29T11:30:00Z"),   # Wed 07:00: pre-market
        _event("CCC", "2026-07-29T17:00:00Z", "2026-07-30T12:00:00Z"),   # Wed mid-session, 10-Q Thu pre-market
        _event("DDD", "2026-11-27T17:30:00Z", "2026-11-27T17:45:00Z"),   # day after Thanksgiving, before 13:00
        _event("EEE", "2026-11-27T18:30:00Z", "2026-11-27T18:45:00Z"),   # ... after the 13:00 early close
    ]
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        module = _engine(Path(tmp), events)
        out = module.SignalEngine().generate(_frames("AAA", "BBB", "CCC", "DDD", "EEE", "ZZZ"))
    assert _held(out["AAA.US"]) == ["2026-07-29", "2026-07-30", "2026-07-31"]    # never Tuesday's own bar
    assert _held(out["BBB.US"]) == ["2026-07-29", "2026-07-30", "2026-07-31"]
    assert _held(out["CCC.US"]) == ["2026-07-30", "2026-07-31", "2026-08-03"]    # waits for the SUE
    assert _held(out["DDD.US"]) == ["2026-11-27", "2026-11-30", "2026-12-01"]
    assert _held(out["EEE.US"]) == ["2026-11-30", "2026-12-01", "2026-12-02"]
    assert _held(out["ZZZ.US"]) == []
    assert set(out["AAA.US"].unique()) == {0.0, 0.1}


def test_long_the_top_bucket_short_the_bottom_only_when_asked(tmp_path):
    events = [_event("AAA", "2026-08-03T20:05:00Z", "2026-08-03T20:30:00Z", "Q5"),
              _event("BBB", "2026-08-03T20:05:00Z", "2026-08-03T20:30:00Z", "Q1"),
              _event("CCC", "2026-08-03T20:05:00Z", "2026-08-03T20:30:00Z", "Q3")]
    data = _frames("AAA", "BBB", "CCC")
    long_only = _engine(tmp_path, events, hold=20, name="long").SignalEngine().generate(data)
    assert (long_only["AAA.US"] == 0.1).sum() == 20 and _held(long_only["AAA.US"])[0] == "2026-08-04"
    assert not long_only["BBB.US"].any() and not long_only["CCC.US"].any()
    both = _engine(tmp_path, events, hold=20, short=("Q1",), name="both").SignalEngine().generate(data)
    assert (both["BBB.US"] == -0.1).sum() == 20 and (both["BBB.US"] != 0).sum() == 20
    assert not both["CCC.US"].any()


def test_the_cli_maps_its_flags_onto_build(monkeypatch, capsys):
    seen = {}

    def fake_build(run_dir, **kwargs):
        seen.update(kwargs, run_dir=run_dir)
        return {"run_dir": run_dir}

    monkeypatch.setattr(build_run, "build", fake_build)
    assert build_run.main(["--run-dir", "runs/x", "--start", "2018-01-01", "--end", "2026-09-25",
                           "--run-asof", RUN_ASOF, "--tickers", "NVDA", "AAA", "--hold", "25",
                           "--short-bottom", "--no-sue-lag-limit", "--rebalance-mask", "W-FRI"]) == 0
    assert seen["tickers"] == ["NVDA", "AAA"] and seen["hold"] == 25 and seen["short_bottom"] is True
    assert seen["max_sue_lag_sessions"] is None and seen["rebalance_mask"] == "W-FRI"
    assert seen["pit_mode"] == "snapshot" and seen["claim"] == "research" and seen["slot_weight"] == 0.1
    assert json.loads(capsys.readouterr().out) == {"run_dir": "runs/x"}


# --------------------------------------------------------------------------- the builder (store-backed)


def test_the_builder_writes_a_run_dir_vt_accepts(installed, tmp_path):
    from backtest.loaders.pitdb_loader import parse_pit_block
    from backtest.runner import BacktestConfigSchema

    run_dir = tmp_path / "pead"
    summary = build_run.build(run_dir, start="2016-01-01", end="2026-09-25", run_asof=RUN_ASOF, core=core)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    BacktestConfigSchema(**config)
    assert parse_pit_block(config).mode == "snapshot"
    assert config["source"] == "pitdb" and config["zt_template"] == "pead_8k"
    assert config["pit"] == {"mode": "snapshot", "run_asof_utc": RUN_ASOF, "availability_lag": "36h",
                             "claim": "research", "allow_reconstructed": False}
    assert config["codes"] == summary["codes"] == ["AAA.US", "BBB.US", "CCC.US", "DDD.US", "NVDA.US"]
    assert config["pead"]["hold_sessions"] == 20 and config["pead"]["long_buckets"] == ["Q5"]
    assert config["pead"]["short_buckets"] == [] and config["pead"]["max_sue_lag_sessions"] == 1

    engine = _vt_load(run_dir / "code" / "signal_engine.py", "zt_pead_built")
    assert len(engine.EVENTS) == summary["events_traded"] == config["pead"]["events_traded"] > 0
    assert {e["bucket"] for e in engine.EVENTS} == {"Q5"}
    assert all(e["accepted_utc"] <= RUN_ASOF and e["sue_known_utc"] <= RUN_ASOF for e in engine.EVENTS)

    audit = json.loads((run_dir / "pead_events.json").read_text(encoding="utf-8"))
    assert audit["run_asof_utc"] == RUN_ASOF
    assert audit["pit_classes"] == {"events": ["TRUE_PIT"], "sue": ["TRUE_PIT"]}
    assert len(audit["used"]) == summary["events_used"] and len(audit["excluded"]) == summary["events_excluded"]
    reasons: dict = {}
    for row in audit["excluded"]:
        reasons.setdefault(row["ticker"], []).append(row["reason"])
    assert all(r.startswith("no SUE at the run as-of") for r in reasons["FFF"])        # no 10-Q EPS ever
    assert "SUE first known after the close of day +1" in reasons["EEE"]               # 10-Q 20 days later
    assert any(r.startswith("duplicate of") for r in reasons["AAA"])                   # AAA's 8-K/A
    assert any(r.startswith("duplicate of 0001045810-22-000133") for r in reasons["NVDA"])


def test_without_the_lag_limit_a_late_sue_is_entered_when_it_becomes_known(installed, tmp_path):
    run_dir = tmp_path / "late"
    summary = build_run.build(run_dir, start="2016-01-01", end="2026-09-25", run_asof=RUN_ASOF,
                              tickers=["EEE"], max_sue_lag_sessions=None, core=core)
    assert summary["codes"] == ["EEE.US"]
    engine = _vt_load(run_dir / "code" / "signal_engine.py", "zt_pead_late")
    for event in engine.EVENTS:
        lag = pd.Timestamp(event["sue_known_utc"]) - pd.Timestamp(event["accepted_utc"])
        assert lag == pd.Timedelta(days=20)
        assert engine.decision_instant(event) == pd.Timestamp(event["sue_known_utc"])
    with pytest.raises(ValueError, match="no event in a traded SUE bucket"):
        build_run.build(tmp_path / "strict", start="2016-01-01", end="2026-09-25", run_asof=RUN_ASOF,
                        tickers=["EEE", "FFF"], core=core)
    assert not (tmp_path / "strict").exists()


def test_the_builder_refuses_what_the_runner_would_get_wrong(installed, tmp_path):
    with pytest.raises(ValueError, match="shorter than the rebalance cadence"):
        build_run.build(tmp_path / "monthly", start="2016-01-01", end="2026-09-25", run_asof=RUN_ASOF,
                        hold=20, rebalance_mask="BME", core=core)       # month ends are up to 23 sessions apart
    with pytest.raises(ValueError, match="after the run as-of"):
        build_run.build(tmp_path / "future", start="2016-01-01", end="2026-09-28", run_asof=RUN_ASOF, core=core)
    with pytest.raises(ValueError, match="slot_weight"):
        build_run.build(tmp_path / "heavy", start="2016-01-01", end="2026-09-25", run_asof=RUN_ASOF,
                        slot_weight=1.5, core=core)
    assert not any(tmp_path.iterdir())
    summary = build_run.build(tmp_path / "weekly", start="2016-01-01", end="2026-09-25", run_asof=RUN_ASOF,
                              hold=20, rebalance_mask="W-FRI", core=core)
    config = json.loads((tmp_path / "weekly" / "config.json").read_text(encoding="utf-8"))
    assert summary["rebalance_cadence_sessions"] == config["pead"]["rebalance_cadence_sessions"] == 5
    assert config["position_adjustment"] == "rebalance" and config["rebalance_mask"] == "W-FRI"


# --------------------------------------------------------------------------- VT's runner end to end


class WarehouseBackend:
    """VT's pitdb backend protocol over the fixture warehouse, with a PASS audit receipt."""

    def __init__(self, con) -> None:
        self.con = con

    def availability(self, required_checks=()):
        return {"backend": "zt-events fixture", "lake_signature_sha256": "sha256:fixture",
                "audit": {"status": "PASS", "audited_at_utc": RUN_ASOF,
                          "checks_passed": [f"A{i}" for i in range(1, 12)]}}

    def connect(self):
        return self.con.cursor()


@pytest.fixture
def pitdb_only(monkeypatch):
    """Restore VT's loader registry afterwards; any non-pitdb loader fails the test if built."""
    from backtest.loaders.registry import LOADER_REGISTRY, _ensure_registered

    saved = dict(LOADER_REGISTRY)
    _ensure_registered()
    touched: list = []

    def network(name):
        class Network:
            markets = {"us_equity", "index"}

            def __init__(self, *args, **kwargs):
                touched.append(name)
                raise AssertionError(f"network loader {name} was constructed")

        Network.name = name
        return Network

    for name in list(LOADER_REGISTRY):
        if name != "pitdb":
            monkeypatch.setitem(LOADER_REGISTRY, name, network(name))

    def no_yfinance(*args, **kwargs):
        raise AssertionError("a pitdb run never fetches its benchmark from the network")

    monkeypatch.setattr("backtest.benchmark.YfinanceLoader", no_yfinance)
    yield touched
    LOADER_REGISTRY.clear()
    LOADER_REGISTRY.update(saved)


def test_vts_runner_backtests_the_built_run_from_pitdb(installed, tmp_path, monkeypatch, pitdb_only):
    from backtest import runner
    from backtest.loaders import pitdb_loader as pl
    from src.config.accessor import reset_env_config

    run_dir = tmp_path / "runs" / "pead_8k"
    summary = build_run.build(run_dir, start="2023-01-01", end="2026-09-25", run_asof=RUN_ASOF,
                              tickers=["AAA", "BBB", "DDD", "NVDA"], core=core)
    monkeypatch.setattr(pl, "default_backend", lambda: WarehouseBackend(installed))
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    reset_env_config()
    try:
        runner.main(run_dir)
    finally:
        reset_env_config()
    assert pitdb_only == []

    card = json.loads((run_dir / "run_card.json").read_text(encoding="utf-8"))
    assert card["data_sources"] == ["pitdb"] and card["pit"]["mode"] == "snapshot"
    assert card["pit"]["declared"]["run_asof_utc"] == RUN_ASOF
    assert card["pit"]["symbols"]["SPY.US"]["role"] == "benchmark"
    assert card["metrics"]["benchmark_ticker"] == "SPY.US"

    # Every fill is at the open after the decision session and every exit 20 sessions
    # later -- except a position still open on the last bar, which the run marks there.
    engine = _vt_load(run_dir / "code" / "signal_engine.py", "zt_pead_e2e")
    closes = es.session_close_times(es.us_equity_sessions("2022-12-01", "2026-12-31"))
    expected = set()
    for event in engine.EVENTS:
        decision = int(es.first_session_after([engine.decision_instant(event)], closes)[0])
        expected.add((f"{event['ticker']}.US", str(closes.index[decision + 1].date()),
                      min(str(closes.index[decision + 21].date()), "2026-09-25")))
    trades = pd.read_csv(run_dir / "artifacts" / "trades.csv")
    entries, exits = trades.iloc[0::2].reset_index(drop=True), trades.iloc[1::2].reset_index(drop=True)
    assert set(entries["side"]) == {"buy"} and set(exits["side"]) == {"sell"}
    got = {(code, entry, leave) for code, entry, leave in zip(entries["code"], entries["timestamp"],
                                                              exits["timestamp"])}
    assert got == expected and len(expected) == summary["events_traded"]
    complete = exits["timestamp"] < "2026-09-25"
    assert exits.loc[complete, "holding_bars"].round(6).eq(20.0).all()
    assert list(exits.loc[~complete, "reason"]) == ["end_of_backtest"]


@pytest.mark.skipif(not os.environ.get("ZT_STRATEGY_DEPLOY_DIR"),
                    reason="set ZT_STRATEGY_DEPLOY_DIR (vt_addons\\strategies) to compare the deployed copy")
@pytest.mark.parametrize("name", ["README.md", "build_run.py", "config.template.json", "signal_engine.py"])
def test_deployed_template_is_byte_identical(name):
    deployed = Path(os.environ["ZT_STRATEGY_DEPLOY_DIR"]) / "pead_8k" / name

    def lf(data: bytes) -> bytes:   # git autocrlf on the Windows checkout vs LF on G:
        return data.replace(b"\r\n", b"\n")

    assert lf(deployed.read_bytes()) == lf((STRATEGY / name).read_bytes())
