"""Build a Vibe-Trading run directory for the ``pead_8k`` strategy (ZT add-on).

Reads 8-K Item 2.02 events and their SUE from ZT's pitdb warehouse exactly as
knowable at ``--run-asof`` (through the zt_events extension's guarded,
read-only store), and writes a run directory VT's runner executes unchanged::

    <run_dir>/config.json               source="pitdb" with a `pit` block
    <run_dir>/code/signal_engine.py     this folder's template, EVENTS filled in
    <run_dir>/pead_events.json          every event used or excluded, with reasons,
                                        knowledge times and PIT classes

Then, from VT's agent folder::

    python -m backtest.runner <run_dir>

Needs VT importable (``PYTHONPATH=<VT>/agent``), ``INVESTMENT_AI_PROJECT_ROOT``,
``VIBE_TRADING_HOME`` and ``PITDB_INDEX`` (the E: index), like the zt-events
server. The run directory must sit under one of VT's allowed run roots.

Price history in the lake is a backfill, so historical runs use
``--pit-mode snapshot --claim research`` (the default); ``formation`` /
``tradeable`` is for windows whose bars were captured daily. Holding windows
are in sessions and must be at least as long as a ``--rebalance-mask`` cadence,
or an event could open and close between two rebalance dates unseen.

    python build_run.py --run-dir <E:\\...\\runs\\pead_8k_2026-09-26> \\
        --start 2018-01-01 --end 2026-09-25 --run-asof 2026-09-26T00:00:00Z
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pprint
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.dont_write_bytecode = True

import pandas as pd  # noqa: E402

HERE = Path(__file__).resolve().parent
TEMPLATE_ENGINE = HERE / "signal_engine.py"
TEMPLATE_CONFIG = HERE / "config.template.json"
DEFAULT_EDGES = (-1.0, -0.25, 0.25, 1.0)


def vt_root() -> Path:
    """The Vibe-Trading checkout (``src`` must be importable: PYTHONPATH=<VT>/agent)."""
    import src  # noqa: PLC0415

    return Path(src.__file__).resolve().parents[2]


def load_core():
    """The zt_events extension's core module (loaded by path)."""
    cached = sys.modules.get("zt_events_core")
    if cached is not None:
        return cached
    path = vt_root() / "extensions" / "zt_events" / "core.py"
    spec = importlib.util.spec_from_file_location("zt_events_core", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zt_events_core"] = module
    spec.loader.exec_module(module)
    return module


def cadence_sessions(mask: str, start: Any, end: Any, core) -> int:
    """Longest gap, in sessions, between consecutive rebalance dates of ``mask``."""
    from backtest.rebalance_mask import resolve_rebalance_dates  # noqa: PLC0415

    sessions = core.es.us_equity_sessions(start, end)
    dates = sorted(resolve_rebalance_dates(mask, sessions) or [])
    if len(dates) < 2:
        raise ValueError(f"rebalance_mask {mask!r} yields fewer than two rebalance dates in the window")
    positions = sessions.get_indexer(pd.DatetimeIndex(dates))
    return int(max(b - a for a, b in zip(positions, positions[1:])))


def render_engine(events: Sequence[Mapping[str, Any]], *, hold: int, long_buckets: Sequence[str],
                  short_buckets: Sequence[str], slot_weight: float) -> str:
    """The template with its generated constants replaced by literals."""
    text = TEMPLATE_ENGINE.read_text(encoding="utf-8")
    values = {
        "EVENTS": pprint.pformat(tuple(dict(e) for e in events), width=110, sort_dicts=True),
        "HOLD_SESSIONS": repr(int(hold)),
        "LONG_BUCKETS": repr(tuple(long_buckets)),
        "SHORT_BUCKETS": repr(tuple(short_buckets)),
        "SLOT_WEIGHT": repr(float(slot_weight)),
    }
    for name, literal in values.items():
        pattern = re.compile(rf"^{name} = .*$", re.MULTILINE)
        if len(pattern.findall(text)) != 1:
            raise RuntimeError(f"template has no single '{name} = ...' line")
        text = pattern.sub(lambda _m, n=name, v=literal: f"{n} = {v}", text, count=1)
    return text


def build(run_dir: Path | str, *, start: str, end: str, run_asof: str,
          tickers: Sequence[str] | None = None, hold: int = 20, short_bottom: bool = False,
          slot_weight: float = 0.1, edges: Sequence[float] = DEFAULT_EDGES, pit_mode: str = "snapshot",
          claim: str = "research", availability_lag: str = "36h", rebalance_mask: str | None = None,
          benchmark: str = "SPY", initial_cash: float = 1_000_000, max_sue_lag_sessions: int | None = 1,
          core=None) -> dict:
    """Write the run directory and return a summary (counts, paths, exclusions).

    ``max_sue_lag_sessions`` (default 1) drops events whose SUE was first known
    after the close of day +N -- the cut-off the zt-events bucket history and
    the scan use -- so the backtest trades the drift the history measured, not a
    release weeks old; ``None`` keeps every event, entered whenever its SUE
    became known.
    """
    core = core or load_core()
    asof = core.asof_utc(run_asof)
    if pd.Timestamp(end) > pd.Timestamp(asof).normalize():
        raise ValueError(f"end {end} is after the run as-of {run_asof}")
    if hold < 1:
        raise ValueError("hold must be at least one session")
    if not 0.0 < slot_weight <= 1.0:
        raise ValueError("slot_weight must lie in (0, 1]")
    if max_sue_lag_sessions is not None and max_sue_lag_sessions < 0:
        raise ValueError("max_sue_lag_sessions must be >= 0 (or None for no limit)")
    edges = tuple(float(x) for x in edges)
    long_buckets = (f"Q{len(edges) + 1}",)
    short_buckets = ("Q1",) if short_bottom else ()
    cadence = None
    if rebalance_mask:
        cadence = cadence_sessions(rebalance_mask, start, end, core)
        if hold < cadence:
            raise ValueError(
                f"hold ({hold} sessions) is shorter than the rebalance cadence of {rebalance_mask!r} "
                f"({cadence} sessions): an event could open and close between two rebalances unseen")

    with core.store() as (con, provenance):
        events, notes = core.study_frame(con, asof, tickers, pd.Timestamp(start), pd.Timestamp(end),
                                         edges=edges, sue_known_by_day=max_sue_lag_sessions)
    used, excluded = [], []
    for row in events.itertuples(index=False):
        reason = None
        if row.duplicate_of is not None and not pd.isna(row.duplicate_of):
            reason = f"duplicate of {row.duplicate_of} (earliest Item 2.02 in the firm-quarter wins)"
        elif row.sue_status != "OK" or pd.isna(row.decision_time):
            reason = f"no SUE at the run as-of ({row.sue_status})"
        elif pd.isna(row.sue_bucket):
            reason = f"SUE first known after the close of day +{max_sue_lag_sessions}"
        if not reason and str(row.sue_bucket) in long_buckets + short_buckets and claim == "tradeable":
            pit_reason = core.entry_pit_reason(row._asdict())
            if pit_reason:
                raise ValueError(f"{row.accession}: {pit_reason}; use claim='research'")
        record = {"ticker": row.ticker, "accession": row.accession,
                  "accepted_utc": core.iso(row.knowledge_time),
                  "sue_known_utc": core.iso(row.sue_knowledge_time),
                  "sue": None if row.sue is None or pd.isna(row.sue) else round(float(row.sue), 6),
                  "bucket": None if pd.isna(row.sue_bucket) else str(row.sue_bucket)}
        record.update(pit_class=getattr(row, "pit_class", None),
                      sue_pit_class=getattr(row, "sue_pit_class", None))
        if reason:
            excluded.append({**record, "reason": reason})
        else:
            used.append({**record, "traded": record["bucket"] in long_buckets + short_buckets})
    traded = [{k: v for k, v in e.items() if k != "traded"} for e in used if e["traded"]]
    codes = sorted({f"{e['ticker']}.US" for e in traded})
    if not codes:
        raise ValueError("no event in a traded SUE bucket in the window; nothing to backtest")

    config = json.loads(TEMPLATE_CONFIG.read_text(encoding="utf-8"))
    config.update(codes=codes, start_date=str(pd.Timestamp(start).date()),
                  end_date=str(pd.Timestamp(end).date()), benchmark=f"{benchmark.upper()}.US",
                  initial_cash=float(initial_cash))
    config["pit"].update(mode=pit_mode, claim=claim, availability_lag=availability_lag,
                         run_asof_utc=core.iso(asof))
    config["pead"].update(hold_sessions=int(hold), long_buckets=list(long_buckets),
                          short_buckets=list(short_buckets), slot_weight=float(slot_weight),
                          sue_bucket_edges=list(edges), rebalance_cadence_sessions=cadence,
                          max_sue_lag_sessions=max_sue_lag_sessions, events_used=len(used),
                          events_traded=len(traded), events_excluded=len(excluded))
    if rebalance_mask:
        config.update(position_adjustment="rebalance", rebalance_mask=rebalance_mask)

    from backtest.loaders.pitdb_loader import parse_pit_block  # noqa: PLC0415
    from backtest.runner import BacktestConfigSchema, _validate_signal_engine_source  # noqa: PLC0415

    BacktestConfigSchema(**config)
    parse_pit_block(config)

    run_dir = Path(run_dir)
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    engine_path = run_dir / "code" / "signal_engine.py"
    engine_path.write_text(render_engine(traded, hold=hold, long_buckets=long_buckets,
                                         short_buckets=short_buckets, slot_weight=slot_weight),
                           encoding="utf-8", newline="\n")
    _validate_signal_engine_source(engine_path)
    (run_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
    audit = {"run_asof_utc": core.iso(asof), "provenance": provenance, "notes": notes,
             "pit_classes": {"events": sorted(set(events["pit_class"].fillna("MISSING").astype(str))),
                             "sue": sorted({c for c in events["sue_pit_class"].dropna()})},
             "used": used, "excluded": excluded}
    (run_dir / "pead_events.json").write_text(json.dumps(audit, indent=2, default=str) + "\n",
                                              encoding="utf-8", newline="\n")
    return {"run_dir": str(run_dir), "codes": codes, "events_used": len(used),
            "events_traded": len(traded), "events_excluded": len(excluded),
            "rebalance_cadence_sessions": cadence,
            "run": f"python -m backtest.runner {run_dir}"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_run", description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--run-asof", required=True, help="ISO instant with an offset, e.g. ...Z")
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--hold", type=int, default=20, help="holding window in sessions")
    parser.add_argument("--short-bottom", action="store_true", help="also short the bottom SUE bucket")
    parser.add_argument("--slot-weight", type=float, default=0.1)
    parser.add_argument("--edges", default=",".join(str(x) for x in DEFAULT_EDGES))
    parser.add_argument("--pit-mode", choices=("snapshot", "formation"), default="snapshot")
    parser.add_argument("--claim", choices=("research", "tradeable"), default="research")
    parser.add_argument("--availability-lag", default="36h")
    parser.add_argument("--rebalance-mask")
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--initial-cash", type=float, default=1_000_000)
    parser.add_argument("--max-sue-lag-sessions", type=int, default=1,
                        help="drop events whose SUE was first known after the close of day +N")
    parser.add_argument("--no-sue-lag-limit", action="store_true",
                        help="keep every event, entered whenever its SUE became known")
    args = parser.parse_args(argv)
    summary = build(args.run_dir, start=args.start, end=args.end, run_asof=args.run_asof,
                    tickers=args.tickers, hold=args.hold, short_bottom=args.short_bottom,
                    slot_weight=args.slot_weight, edges=[float(x) for x in args.edges.split(",")],
                    pit_mode=args.pit_mode, claim=args.claim, availability_lag=args.availability_lag,
                    rebalance_mask=args.rebalance_mask, benchmark=args.benchmark,
                    initial_cash=args.initial_cash,
                    max_sue_lag_sessions=None if args.no_sue_lag_limit else args.max_sue_lag_sessions)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
