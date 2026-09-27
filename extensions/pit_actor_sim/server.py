"""Opt-in Vibe-Trading tools for the canonical PIT warehouse and actor simulator."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("pit-actor-sim")


def project() -> Path:
    raw = os.environ.get("INVESTMENT_AI_PROJECT_ROOT")
    if not raw:
        raise RuntimeError("INVESTMENT_AI_PROJECT_ROOT is required")
    root = Path(raw).resolve(strict=True)
    if root.name != "Investment-AI-Drive-Research" or root.parent.name != "work":
        raise ValueError("Use the canonical work project folder")
    for part in ("AGENTS.md", "implementation/pit_warehouse/pitdb",
                 "market_actor_sim/run_governed_pilot.py"):
        if not (root / part).exists():
            raise FileNotFoundError(part)
    return root


def runtime() -> Path:
    raw = os.environ.get("VIBE_TRADING_HOME")
    if not raw:
        raise RuntimeError("VIBE_TRADING_HOME is required")
    path = Path(raw).resolve()
    if os.name == "nt" and path.drive.upper() not in ("D:", "E:"):
        raise ValueError("Runtime must be on D: or E:")
    if project() == path or project() in path.parents:
        raise ValueError("Runtime may not be stored in the shared project")
    return path


def asof_utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("asof_utc requires a timezone offset")
    value = value.astimezone(timezone.utc)
    if value > datetime.now(timezone.utc):
        raise ValueError("asof_utc cannot be in the future")
    return value.replace(tzinfo=None)


def row_limit(value: int) -> int:
    if not 1 <= value <= 1000:
        raise ValueError("limit must be 1..1000")
    return value


_READ_TABLES = ("dim_source", "dim_security", "dim_security_alias",
                "dim_series", "fact_price_eod", "fact_observation")


def lake_signature(tables=None) -> dict:
    """Cheap freshness token for compact tables in the canonical Parquet lake."""
    sys.path.insert(0, str(project() / "implementation" / "pit_warehouse"))
    from pitdb import config as C
    if C.DB_PATH is None or C.DB_PATH.drive.upper() != "D:":
        raise RuntimeError("Set PITDB_INDEX=local for the disposable D: index")
    files = {}
    for table in (tables or C.PERSISTED_TABLES):
        path = C.LAKE_ROOT / table / "data.parquet"
        try:
            stat = path.stat()
            files[table] = [stat.st_size, stat.st_mtime_ns]
        except FileNotFoundError:
            files[table] = None
    return {"lake_root": str(C.LAKE_ROOT.resolve()), "files": files}


def require_fresh_index(full: bool = False) -> None:
    sys.path.insert(0, str(project() / "implementation" / "pit_warehouse"))
    from pitdb import config as C
    receipt_path = runtime() / "pit_index_receipt.json"
    if not C.DB_PATH or not C.DB_PATH.exists() or not receipt_path.exists():
        raise RuntimeError("PIT index unavailable; run server.py --refresh-index")
    recorded = json.loads(receipt_path.read_text(encoding="utf-8")).get("signature", {})
    current = lake_signature(None if full else _READ_TABLES)
    if current["lake_root"] != recorded.get("lake_root") or any(
        recorded.get("files", {}).get(table) != value
        for table, value in current["files"].items()
    ):
        raise RuntimeError("PIT index is stale; run server.py --refresh-index")


def refresh_index() -> dict:
    """Rebuild only the disposable local index, then record lake freshness."""
    before = lake_signature()
    from pitdb.db import connect, rebuild_from_lake
    con = connect(wait_minutes=1)
    try:
        counts = rebuild_from_lake(con)
    finally:
        con.close()
    after = lake_signature()
    if before != after:
        raise RuntimeError("Lake changed during rebuild; retry refresh")
    receipt = {"signature": after, "refreshed_at_utc": datetime.now(timezone.utc).isoformat(),
               "index_path": "D:\\pitdb\\pit.duckdb", "rows": sum(counts.values())}
    root = runtime()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "pit_index_receipt.json"
    temporary = root / "pit_index_receipt.json.tmp"
    temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    temporary.replace(target)
    return receipt

def refresh_audit() -> dict:
    """Run the PIT audit outside the MCP server and bind its receipt to the lake."""
    before = lake_signature()
    env = child_env()
    env["PITDB_INDEX"] = "memory"
    root = project()
    run = subprocess.run([sys.executable, "-m", "pitdb", "audit"],
                         cwd=root / "implementation" / "pit_warehouse",
                         env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    if run.returncode or "OVERALL: PASS" not in run.stdout:
        raise RuntimeError("PIT audit failed: " + (run.stdout + run.stderr)[-3000:])
    after = lake_signature()
    if before != after:
        raise RuntimeError("Lake changed during audit; retry preflight")
    receipt = {"signature": after,
               "audited_at_utc": datetime.now(timezone.utc).isoformat(),
               "status": "PASS", "checks": 10}
    path = runtime() / "pit_audit_receipt.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    temporary.replace(path)
    return receipt


def require_fresh_audit() -> dict:
    path = runtime() / "pit_audit_receipt.json"
    if not path.is_file():
        raise RuntimeError("PIT audit receipt missing; run server.py --refresh-audit")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    audited = datetime.fromisoformat(receipt["audited_at_utc"])
    age = datetime.now(timezone.utc) - audited
    if receipt.get("status") != "PASS" or age.total_seconds() < 0 or age.total_seconds() > 3600:
        raise RuntimeError("PIT audit receipt expired; run server.py --refresh-audit")
    if receipt.get("signature") != lake_signature():
        raise RuntimeError("PIT lake changed since audit; run server.py --refresh-audit")
    return receipt


def _trace(step: str) -> None:
    if os.environ.get("VIBE_EXTENSION_TRACE") == "1":
        path = Path(os.environ["VIBE_TRADING_HOME"]) / "pit_actor_trace.log"
        with path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now(timezone.utc).isoformat()} pid={os.getpid()} {step}\n")


def query(sql: str, args: list) -> list[dict]:
    if os.environ.get("VIBE_PIT_WORKER") != "1":
        payload = {"sql": sql, "args": [
            {"kind": "datetime", "value": arg.isoformat()} if isinstance(arg, datetime)
            else {"kind": "date", "value": arg.isoformat()} if isinstance(arg, date)
            else {"kind": "plain", "value": arg}
            for arg in args
        ]}
        env = child_env()
        env["VIBE_PIT_WORKER"] = "1"
        run = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("query_worker.py"))],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=240, cwd=Path(__file__).parent, env=env,
        )
        if run.returncode:
            raise RuntimeError("PIT query failed: " + run.stderr[-3000:])
        return json.loads(run.stdout)
    _trace("query_start")
    sys.path.insert(0, str(project() / "implementation" / "pit_warehouse"))
    from pitdb.db import connect
    _trace("imported")
    require_fresh_index()
    _trace("fresh")
    with redirect_stdout(sys.stderr):
        con = connect(read_only=True)
    _trace("connected")
    try:
        cursor = con.execute(sql, args)
        _trace("executed")
        fields = [field[0] for field in cursor.description]
        rows = [{key: value.isoformat() if isinstance(value, (date, datetime)) else value
                 for key, value in zip(fields, row)} for row in cursor.fetchall()]
        _trace("fetched")
        return rows
    finally:
        con.close()
        _trace("closed")


@mcp.tool
def pit_security(ticker: str, asof: str) -> dict:
    """Resolve a ticker to permanent security IDs using time-scoped aliases."""
    at = asof_utc(asof)
    if not ticker.strip():
        raise ValueError("ticker is required")
    rows = query("""
        SELECT DISTINCT s.sec_id, s.primary_ticker, s.name, s.exchange_mic,
               s.currency, a.valid_from, a.valid_to
        FROM dim_security_alias a JOIN dim_security s USING (sec_id)
        WHERE upper(a.alias_value) = upper(?) AND a.alias_type = 'ticker'
          AND (a.valid_from IS NULL OR a.valid_from <= ?::DATE)
          AND (a.valid_to IS NULL OR a.valid_to > ?::DATE)
        ORDER BY s.sec_id
    """, [ticker.strip(), at, at])
    return {"asof": asof, "ticker": ticker, "securities": rows}


@mcp.tool
def pit_price_history(sec_id: int, asof: str, start_date: str,
                      end_date: str, limit: int = 250) -> dict:
    """Read vintage-aware EOD prices through price_asof with provenance."""
    at = asof_utc(asof)
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if sec_id <= 0 or start > end:
        raise ValueError("invalid security ID or date range")
    rows = query("""
        SELECT p.sec_id, s.primary_ticker, p.event_date, p.open, p.high,
               p.low, p.close, p.volume, p.currency, p.knowledge_time,
               p.revision_seq, p.source_id, ds.pit_class
        FROM price_asof(?) p JOIN dim_security s USING (sec_id)
        LEFT JOIN dim_source ds ON p.source_id = ds.source_id
        WHERE p.sec_id = ? AND p.event_date BETWEEN ? AND ?
        ORDER BY p.event_date LIMIT ?
    """, [at, sec_id, start, end, row_limit(limit)])
    return {"asof": asof, "rows": rows, "row_count": len(rows),
            "contains_non_pit": any(r["pit_class"] in (None, "NON_PIT") for r in rows),
            "authority": "RESEARCH_ONLY_UNTIL_PIT_AUDIT_AND_BACKTEST_GATES_PASS"}


@mcp.tool
def pit_series_history(series_id: str, asof: str, start_date: str,
                       end_date: str, limit: int = 250) -> dict:
    """Read a series through obs_asof with source, unit, and revision data."""
    at = asof_utc(asof)
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if not series_id.strip() or start > end:
        raise ValueError("invalid series ID or date range")
    rows = query("""
        SELECT o.series_id, d.label, d.unit, o.event_time, o.value_num,
               o.value_str, o.quality, o.knowledge_time, o.revision_seq,
               o.source_id, COALESCE(d.pit_class, ds.pit_class) AS pit_class
        FROM obs_asof(?) o JOIN dim_series d USING (series_id)
        LEFT JOIN dim_source ds ON o.source_id = ds.source_id
        WHERE o.series_id = ? AND o.event_time::DATE BETWEEN ? AND ?
        ORDER BY o.event_time LIMIT ?
    """, [at, series_id.strip(), start, end, row_limit(limit)])
    return {"asof": asof, "rows": rows, "row_count": len(rows),
            "contains_non_pit": any(r["pit_class"] in (None, "NON_PIT") for r in rows),
            "authority": "RESEARCH_ONLY_UNTIL_PIT_AUDIT_AND_BACKTEST_GATES_PASS"}


def sim_input(relative: str) -> Path:
    base = (project() / "market_actor_sim").resolve(strict=True)
    path = (base / relative).resolve(strict=True)
    if base not in path.parents or not path.is_file():
        raise ValueError("input must be a file under market_actor_sim")
    return path


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, folder in (("TEMP", "tmp"), ("TMP", "tmp"),
                        ("PYTHONPYCACHEPREFIX", "pycache")):
        path = runtime() / folder
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env["PITDB_INDEX"] = "local"
    env["PYTHONPATH"] = str(project() / "implementation" / "pit_warehouse")
    return env


@mcp.tool
def run_market_actor_sim(scenario: str, forks: str, evidence: str,
                         seed: int = 20260828, rollouts: int = 300000) -> dict:
    """Run the existing evidence-frozen simulator after a fresh warehouse audit.

    Input paths are relative to market_actor_sim. Results are uncalibrated
    research and are saved in isolated D:/E: runtime state.
    """
    if not 1000 <= rollouts <= 300000:
        raise ValueError("rollouts must be 1000..300000")
    paths = [sim_input(value) for value in (scenario, forks, evidence)]
    snapshot = json.loads(paths[2].read_text(encoding="utf-8"))
    if snapshot.get("warehouse_audit", {}).get("status") != "PASS":
        raise ValueError("evidence snapshot lacks a passing PIT audit")
    root = project()
    require_fresh_index(full=True)
    require_fresh_audit()
    env = child_env()
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(f"{seed}:{rollouts}".encode("ascii"))
    run_key = digest.hexdigest()[:16]
    out = runtime() / "sim_runs" / run_key
    out.mkdir(parents=True, exist_ok=True)
    run = subprocess.run([
        sys.executable, str(root / "market_actor_sim" / "run_governed_pilot.py"),
        "--scenario", str(paths[0]), "--forks", str(paths[1]),
        "--evidence", str(paths[2]), "--out-dir", str(out),
        "--seed", str(seed), "--rollouts", str(rollouts)],
        cwd=root / "market_actor_sim", env=env, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=240)
    if run.returncode:
        raise RuntimeError("simulation failed: " + (run.stdout + run.stderr)[-3000:])
    result = json.loads((out / "pilot_result.json").read_text(encoding="utf-8"))
    return {"run_key": run_key, "run_id": result["run_id"],
            "authority": result["authority"], "validation": result["validation"],
            "ensemble_mc": result["ensemble_mc"], "mc_wilson_95": result["mc_wilson_95"],
            "limitations": result["limitations"],
            "result_path": str(out / "pilot_result.json")}


@mcp.tool
def inspect_market_actor_run(run_key: str) -> dict:
    """Read an isolated actor simulation by its exact run key."""
    if not re.fullmatch(r"[0-9a-f]{16}", run_key):
        raise ValueError("run_key must be 16 lowercase hex characters")
    path = runtime() / "sim_runs" / run_key / "pilot_result.json"
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    if sys.argv[1:] == ["--refresh-index"]:
        print(json.dumps(refresh_index(), indent=2))
    elif sys.argv[1:] == ["--refresh-audit"]:
        print(json.dumps(refresh_audit(), indent=2))
    else:
        mcp.run()