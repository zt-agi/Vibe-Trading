"""Helpers for the zt_research tests: a synthetic project and a stub ranker."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
EXTENSION = HERE.parent
FIXTURES = HERE / "fixtures"
AGENT_DIR = EXTENSION.parents[1] / "agent"
ASOF = "2026-08-15T12:00:00Z"
ASOF_NAIVE = datetime(2026, 8, 15, 12, 0)

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def load_server(name: str = "zt_research_server"):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, EXTENSION / "server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def business_days(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def build_project(root: Path) -> Path:
    """work/Investment-AI-Drive-Research with the ASM data, a stub ranker and a small lake.

    Bars: AAA (equity), BBB (etf), ^IDX (index) on 60 sessions from 2026-06-01,
    each known the same evening (22:00 UTC).  Two PIT traps: AAA's 2026-07-01
    bar is revised AFTER the as-of (the revision must be invisible), and every
    bar after 2026-08-14 is first known after the as-of.
    """
    project = root / "work" / "Investment-AI-Drive-Research"
    shutil.copytree(FIXTURES / "project", project)
    ranker = project / "implementation" / "alpha_signal_pit_ranker"
    shutil.copytree(FIXTURES / "stub_ranker", ranker)
    lake = project / "implementation" / "pit_warehouse" / "lake"
    schema = (project / "implementation" / "pit_warehouse" / "pitdb" / "schema.sql").read_text()
    con = duckdb.connect(":memory:")
    con.execute(schema)
    con.execute("INSERT INTO dim_security (sec_id, primary_ticker, exchange_mic, currency, asset_class,"
                " name) VALUES (1, 'AAA', 'XNAS', 'USD', 'equity', 'Alpha'),"
                " (2, 'BBB', 'ARCX', 'USD', 'etf', 'Beta'), (3, '^IDX', 'INDEX', 'USD', 'index', 'Idx')")
    rows = []
    for sec_id in (1, 2, 3):
        for i, day in enumerate(business_days(date(2026, 6, 1), 60)):
            close = 100.0 + sec_id * 10 + i * 0.5 + (1.5 if i % 3 == 0 else -0.7)
            known = datetime.combine(day, datetime.min.time()) + timedelta(hours=22)
            rows.append((sec_id, day, known, 0, close - 0.4, close + 1, close - 1, close, 1e6 + i))
    rows.append((1, date(2026, 7, 1), ASOF_NAIVE + timedelta(days=3), 1, 1.0, 1.0, 1.0, 999.0, 1.0))
    con.executemany("INSERT INTO fact_price_eod (sec_id, event_date, knowledge_time, revision_seq,"
                    " open, high, low, close, volume, currency, source_id, ingest_run_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', 'yahoo_eod', 1)", rows)
    for table in ("dim_security", "fact_price_eod"):
        (lake / table).mkdir(parents=True)
        con.execute(f"COPY {table} TO '{(lake / table / 'data.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()
    return project.resolve()


def tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}
