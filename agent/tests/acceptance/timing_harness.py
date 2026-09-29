"""ZT add-on: harness for the timing-contract acceptance suite.

Synthetic US-equity daily bars on the real 2026 NYSE calendar around two
holidays, served either by an in-memory loader or by an in-memory pitdb
warehouse (the pitdb loader tests' own schema and backend), fetched through
the runner's real path (``fetch_data_map``: loader binding, OHLC sanitizing,
the unfinished-bar trim), guarded with ``guard_run`` as ``runner.main`` does,
and run through a named engine. No network, no files outside ``tmp_path``.

Every bar price is unique (open = base + i, close = base + i + 0.5), so a fill
price names the bar and the field it came from.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import duckdb
import numpy as np
import pandas as pd

from backtest.asof_guard import US_EQUITY, decision_bars, guard_run
from backtest.engines.composite import CompositeEngine
from backtest.engines.global_equity import GlobalEquityEngine
from backtest.engines.options_portfolio import (
    historical_volatility,
    options_fill_timing,
    run_options_backtest,
)
from backtest.loaders import pitdb_loader as pl
from src.quantlib.options import bs_price
from tests.test_pitdb_loader import ALL_CHECKS, FIXTURE_SCHEMA, MemoryBackend

NY = "America/New_York"
START, END = "2026-01-05", "2026-02-27"
#: Martin Luther King Jr. Day and Washington's Birthday, NYSE holidays in 2026.
HOLIDAYS = (pd.Timestamp("2026-01-19"), pd.Timestamp("2026-02-16"))
SESSIONS = pd.bdate_range(START, END).difference(pd.DatetimeIndex(HOLIDAYS))
CODES = ("AAA.US", "BBB.US")
BASES = {"AAA.US": 100.0, "BBB.US": 300.0}
OPTION_EXPIRY = "2026-06-19"
RUN_ASOF = "2026-03-02T00:00:00Z"
#: Equity engines under the contract; "options" is VT's options engine.
EQUITY_ENGINES = ("global_equity", "composite")
ENGINES = EQUITY_ENGINES + ("options",)
SOURCES = ("memory", "pitdb")


def session(day: Any) -> pd.Timestamp:
    """A session stamp (asserts it is one)."""
    stamp = pd.Timestamp(day)
    assert stamp in SESSIONS, f"{stamp.date()} is not a session"
    return stamp


def next_session(day: Any) -> pd.Timestamp:
    """The first session strictly after ``day``."""
    return SESSIONS[SESSIONS.searchsorted(pd.Timestamp(day), side="right")]


def bars(code: str, sessions: pd.DatetimeIndex = SESSIONS) -> pd.DataFrame:
    """Unique-priced daily bars: open base+i, close base+i+0.5."""
    base = BASES[code]
    steps = np.arange(len(sessions), dtype="float64")
    frame = pd.DataFrame(
        {
            "open": base + steps,
            "high": base + steps + 0.75,
            "low": base + steps - 0.25,
            "close": base + steps + 0.5,
            "volume": 1_000_000.0,
        },
        index=pd.DatetimeIndex(sessions, name="trade_date"),
    )
    return frame


def price_source(code: str, price: float, frames: Mapping[str, pd.DataFrame]) -> str:
    """Which field of which bar a raw price is (``"open@2026-01-23"``)."""
    frame = frames[code]
    for column in ("open", "close"):
        hits = frame.index[np.isclose(frame[column].to_numpy(dtype=float), price, atol=1e-9)]
        if len(hits):
            return f"{column}@{hits[0].date()}"
    return f"unknown({price})"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def memory_loader_class(frames: Mapping[str, pd.DataFrame]):
    """A loader class serving ``frames`` as given (NaNs and all)."""

    class _MemoryLoader:
        name = "yfinance"
        markets = {"us_equity"}
        requires_auth = False

        def is_available(self) -> bool:
            return True

        def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
            lo = pd.Timestamp(start_date)
            hi = pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1)  # the whole end day
            return {
                code: frames[code].loc[(frames[code].index >= lo) & (frames[code].index < hi)].copy()
                for code in codes if code in frames
            }

    return _MemoryLoader


def pitdb_store(
    frames: Mapping[str, pd.DataFrame],
    *,
    capture: pd.Timedelta = pd.Timedelta(minutes=45),
    captured_at: Optional[Mapping[tuple, dt.datetime]] = None,
) -> duckdb.DuckDBPyConnection:
    """An in-memory pitdb holding ``frames``, each bar captured after its close.

    Args:
        frames: Bars per VT code (``AAA.US`` -> ticker ``AAA``).
        capture: Delay from the session close to the capture.
        captured_at: ``{(code, day): naive-UTC knowledge time}`` overrides.
    """
    con = duckdb.connect(":memory:")
    con.execute(FIXTURE_SCHEMA)
    con.execute("INSERT INTO dim_source (source_id, pit_class) VALUES ('yahoo_eod', 'OBSERVED_PIT')")
    for sec_id, (code, frame) in enumerate(frames.items(), start=1):
        ticker = pl.pitdb_ticker(code)
        con.execute(
            "INSERT INTO dim_security (sec_id, primary_ticker, exchange_mic, currency, asset_class) "
            "VALUES (?, ?, 'XNAS', 'USD', 'equity')", [sec_id, ticker])
        con.execute(
            "INSERT INTO dim_security_alias VALUES (?, 'ticker', ?, NULL, NULL, 'warehouse')",
            [sec_id, ticker])
        closes = US_EQUITY.close_utc(frame.index).tz_localize(None)
        for (day, row), close_at in zip(frame.iterrows(), closes):
            known = (captured_at or {}).get((code, day.date()), close_at + capture)
            con.execute(
                "INSERT INTO fact_price_eod (sec_id, event_date, knowledge_time, revision_seq, "
                "open, high, low, close, volume, currency, is_settled, source_id, ingest_run_id) "
                "VALUES (?,?,?,0,?,?,?,?,?,'USD',TRUE,'yahoo_eod',1)",
                [sec_id, day.date(), pd.Timestamp(known).to_pydatetime(), float(row["open"]),
                 float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"])])
    return con


def pit_block(**overrides: Any) -> Dict[str, Any]:
    block = {"mode": "formation", "run_asof_utc": RUN_ASOF, "availability_lag": "36h",
             "claim": "tradeable", "allow_reconstructed": False}
    block.update(overrides)
    return block


def run_config(
    engine: str,
    source: str,
    *,
    codes=CODES,
    start: str = START,
    end: str = END,
    mode: str = "hold",
    same_day_fill: bool = False,
    pit: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A backtest config for one engine and source (frictionless fills)."""
    config: Dict[str, Any] = {
        "codes": list(codes),
        "start_date": start,
        "end_date": end,
        "source": "pitdb" if source == "pitdb" else "yfinance",
        "interval": "1D",
        "engine": "options" if engine == "options" else "daily",
        "initial_cash": 100_000.0,
        "slippage_us": 0.0,
        "commission": 0.0,
        "position_adjustment": mode,
        "options_config": {
            "risk_free_rate": 0.0,
            "contract_multiplier": 1.0,
            "margin_enabled": False,
            "same_day_fill": same_day_fill,
        },
    }
    if source == "pitdb":
        config["pit"] = pit if pit is not None else pit_block()
    config.update(extra)
    return config


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class CloseTrigger:
    """Equity: weight ``w`` on every bar whose close is above ``levels[code]``."""

    def __init__(self, levels: Mapping[str, float], weight: float = 0.45) -> None:
        self.levels, self.weight = dict(levels), weight

    def generate(self, data_map):
        return {
            code: (frame["close"] > self.levels.get(code, np.inf)).astype(float) * self.weight
            for code, frame in data_map.items()
        }


class OptionsCloseTrigger:
    """Options: one call (struck at the level) opened on the first bar whose close is above it."""

    def __init__(self, levels: Mapping[str, float]) -> None:
        self.levels = dict(levels)

    def generate(self, data_map):
        signals = []
        for code, frame in data_map.items():
            level = self.levels.get(code, np.inf)
            hit = frame.index[frame["close"] > level]
            if len(hit):
                signals.append(_option_open(code, hit[0], level))
        return signals


class StampedSignals:
    """Equity: weight from each stamp onward, the stamp being any time (event-style).

    ``stamps`` maps code -> timestamp; the value is placed at that stamp (added
    to the bar index if it is not a bar) and held on every later bar.
    """

    def __init__(self, stamps: Mapping[str, Any], weight: float = 0.45) -> None:
        self.stamps, self.weight = dict(stamps), weight

    def generate(self, data_map):
        out = {}
        for code, frame in data_map.items():
            stamp = self.stamps.get(code)
            signal = pd.Series(0.0, index=frame.index)
            if stamp is not None and not pd.isna(stamp):
                signal[frame.index > stamp] = self.weight
                signal[pd.Timestamp(stamp)] = self.weight
                signal = signal.sort_index()
            out[code] = signal
        return out


class OptionsStamped:
    """Options: one call per code, dated at the given stamp (any time)."""

    def __init__(self, stamps: Mapping[str, Any]) -> None:
        self.stamps = dict(stamps)

    def generate(self, data_map):
        signals = []
        for code, frame in data_map.items():
            stamp = self.stamps.get(code)
            if stamp is None or pd.isna(stamp):
                continue
            known = frame.loc[frame.index <= pd.Timestamp(stamp).normalize(), "close"]
            strike = float(known.iloc[-1]) if len(known) else float(frame["close"].iloc[0])
            signals.append(_option_open(code, pd.Timestamp(stamp), strike))
        return signals


def _option_open(code: str, day: pd.Timestamp, strike: float) -> Dict[str, Any]:
    return {
        "date": str(day.date()) if day == day.normalize() else day.isoformat(),
        "action": "open",
        "underlying": code,
        "legs": [{"type": "call", "strike": round(strike), "expiry": OPTION_EXPIRY, "qty": 1}],
    }


def naive_event_bar(knowledge_time: pd.Timestamp) -> pd.Timestamp:
    """The naive alignment: an event goes on the bar of its New York calendar date."""
    return pd.Timestamp(knowledge_time).tz_convert(NY).tz_localize(None).normalize()


def aligned_event_bar(knowledge_time: pd.Timestamp, fill_timing: str) -> pd.Timestamp:
    """The contract's alignment (backtest.asof_guard.decision_bars)."""
    return decision_bars([knowledge_time], SESSIONS, fill_timing=fill_timing)[0]


# ---------------------------------------------------------------------------
# Running an engine the way runner.main does
# ---------------------------------------------------------------------------


@dataclass
class Fill:
    """A strategy fill (end-of-backtest liquidations excluded)."""

    code: str
    day: pd.Timestamp
    price: float
    at: pd.Timestamp  # UTC execution time under the engine's convention
    source: str  # for equity: "open@DATE"; for options: the spot's "close@DATE"
    quantity: float = 0.0


@dataclass
class RunResult:
    fills: List[Fill]
    equity: pd.Series
    metrics: Dict[str, Any]
    data_map: Dict[str, pd.DataFrame]
    fetch: Any = None
    engine: Any = None
    rejections: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def for_code(self, code: str) -> List[Fill]:
        return [f for f in self.fills if f.code == code]


def fill_timing_of(engine: str, config: Mapping[str, Any]) -> str:
    if engine == "options":
        return options_fill_timing(dict(config))
    return GlobalEquityEngine.FILL_TIMING if engine == "global_equity" else CompositeEngine.FILL_TIMING


def install_source(monkeypatch, source: str, frames: Mapping[str, pd.DataFrame], **store_kw):
    """Point the runner at ``frames``: a memory loader or an in-memory pitdb."""
    if source == "pitdb":
        store = pitdb_store(frames, **store_kw)
        monkeypatch.setattr(pl, "default_backend", lambda: MemoryBackend(store, ALL_CHECKS))
        return store
    loader_cls = memory_loader_class(frames)
    monkeypatch.setattr("backtest.runner._get_loader", lambda _source: loader_cls)
    return None


def make_engine(engine: str, config: Dict[str, Any], codes):
    if engine == "global_equity":
        return GlobalEquityEngine(config, market="us")
    if engine == "composite":
        return CompositeEngine(config, list(codes))
    raise ValueError(engine)


def run(
    engine: str,
    config: Dict[str, Any],
    strategy_factory: Callable[[], Any],
    tmp_path: Path,
    *,
    guard: bool = True,
    hold: Optional[Dict[str, Any]] = None,
) -> RunResult:
    """Fetch through ``fetch_data_map``, guard like ``runner.main``, run, collect fills.

    ``hold`` (a dict) receives the engine instance before it runs, so a test
    can inspect it after the run raises.
    """
    from backtest.runner import _AutoLoader, fetch_data_map

    fetched = fetch_data_map(config)
    config = {**config, "codes": fetched.codes}
    data_map = fetched.data_map
    loader: Any = _AutoLoader(data_map)
    engine_loader = getattr(fetched.loader, "engine_loader", None)
    if callable(engine_loader):
        loader = engine_loader(data_map)
    timing = fill_timing_of(engine, config)
    if guard:
        loader = guard_run(config, data_map, loader, fill_timing=timing, signal_factory=strategy_factory)
    run_dir = tmp_path / f"run-{engine}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if engine == "options":
        metrics = run_options_backtest(config, loader, strategy_factory(), run_dir, bars_per_year=252)
        return RunResult(
            fills=_option_fills(run_dir, data_map),
            equity=_option_equity(run_dir),
            metrics=metrics,
            data_map=data_map,
            fetch=fetched,
        )
    instance = make_engine(engine, config, fetched.codes)
    if hold is not None:
        hold["engine"] = instance
    metrics = instance.run_backtest(config, loader, strategy_factory(), run_dir, bars_per_year=252)
    return RunResult(
        fills=_equity_fills(instance, data_map),
        equity=pd.Series(
            [s.equity for s in instance.equity_snapshots],
            index=pd.DatetimeIndex([s.timestamp for s in instance.equity_snapshots]),
        ),
        metrics=metrics,
        data_map=data_map,
        fetch=fetched,
        engine=instance,
        rejections=metrics.get("unfilled_plan_rejections_by_symbol", {}),
    )


def _equity_fills(instance, data_map) -> List[Fill]:
    fills = []
    for record in instance.fill_records:
        if record.reason == "end_of_backtest":
            continue
        day = pd.Timestamp(record.timestamp)
        fills.append(Fill(
            code=record.symbol,
            day=day,
            price=float(record.execution_price),
            at=US_EQUITY.open_utc(day)[0],
            source=price_source(record.symbol, float(record.execution_price), data_map),
            quantity=float(record.signed_quantity),
        ))
    return fills


def option_premium(frame: pd.DataFrame, day: pd.Timestamp, strike: float) -> float:
    """What the options engine charges for the harness's call on ``day``."""
    iv = float(historical_volatility(frame["close"]).at[day])
    t = max((pd.Timestamp(OPTION_EXPIRY) - day).days / 365.0, 0.001)
    return bs_price(float(frame.at[day, "close"]), strike, t, 0.0, iv, "call")


def _option_fills(run_dir: Path, data_map) -> List[Fill]:
    trades = pd.read_csv(run_dir / "artifacts" / "trades.csv")
    fills = []
    for _, row in trades.iterrows():
        if row["side"] not in ("buy", "sell"):
            continue
        day = pd.Timestamp(row["timestamp"])
        code = str(row["code"])
        frame = data_map[code]
        source = "unknown"
        for candidate in frame.index[frame.index <= day]:
            if np.isclose(option_premium(frame, candidate, float(row["strike"])), float(row["price"]),
                          atol=5e-5):
                source = f"close@{candidate.date()}"
        fills.append(Fill(code=code, day=day, price=float(row["price"]),
                          at=US_EQUITY.close_utc(day)[0], source=source,
                          quantity=float(row["qty"])))
    return fills


def _option_equity(run_dir: Path) -> pd.Series:
    equity = pd.read_csv(run_dir / "artifacts" / "equity.csv")
    return pd.Series(equity["equity"].to_numpy(), index=pd.DatetimeIndex(equity["timestamp"]))


def trigger_level(code: str, day: Any, frames: Mapping[str, pd.DataFrame]) -> float:
    """A close level first crossed on ``day``'s close (the bar's close is the trigger)."""
    frame = frames[code]
    stamp = pd.Timestamp(day)
    previous = frame.loc[frame.index < stamp, "close"]
    below = float(previous.iloc[-1]) if len(previous) else float(frame.at[stamp, "close"]) - 1.0
    return (below + float(frame.at[stamp, "close"])) / 2.0


def trigger_strategy(engine: str, levels: Mapping[str, float]) -> Callable[[], Any]:
    if engine == "options":
        return lambda: OptionsCloseTrigger(levels)
    return lambda: CloseTrigger(levels)


def stamped_strategy(engine: str, stamps: Mapping[str, Any]) -> Callable[[], Any]:
    if engine == "options":
        return lambda: OptionsStamped(stamps)
    return lambda: StampedSignals(stamps)


def fill_field(engine: str) -> str:
    """The bar price an engine fills at: equity at the open, options at the close."""
    return "close" if engine == "options" else "open"


def with_defect(frame: pd.DataFrame, day: pd.Timestamp, defect: str, column: str) -> pd.DataFrame:
    """``frame`` with bar ``day`` missing, or ``column`` NaN / zero on it."""
    out = frame.copy()
    if defect == "missing":
        return out.drop(index=day)
    out.loc[day, column] = np.nan if defect == "nan" else 0.0
    return out
