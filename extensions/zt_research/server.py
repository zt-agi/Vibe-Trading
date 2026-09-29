"""ZT add-on: research tools over ZT's PIT alpha ranker and Alpha Signal Monitor.

A stdio FastMCP server ("zt-research"), registered in <VIBE_TRADING_HOME>/agent.json
like the other ZT extensions.  Read-only by default:

* rank_pit_signals(config_name, asof, limit) -- runs the project's ranker
  (implementation/alpha_signal_pit_ranker, ``python -m pit_alpha_ranker run``)
  on a named input cut at ``asof`` and writes ONLY into an E: scratch run folder
  under VIBE_TRADING_HOME/zt_research/rank_runs/.  Returns a ranking summary and
  the manifest paths.
* asm_promotion_board() / hypothesis_test_ledger(limit) -- the ASM phase-3
  board and the multiple-testing ledger, through the same readers the
  zt-dashboards extension uses (extensions/zt_dashboards/zt_core.py).
* import_promotion_board_to_vt(apply=False) -- maps board rows to Vibe-Trading
  research hypotheses.  Default is a dry-run diff; ``apply=True`` writes only to
  VT's own hypotheses store (src.hypotheses.registry; VIBE_TRADING_HYPOTHESES_PATH).

Nothing here writes to the project, places or proposes an order, or sets a
probability.  Ranker statuses are research admission labels, not signals to trade.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import ntpath
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True   # never drop __pycache__ into the shared G: project

from fastmcp import FastMCP  # noqa: E402

# ZT add-on (Windows stdio fix, as in zt_ontology): DuckDB imports pandas/numpy
# lazily on its first result conversion; doing that inside FastMCP's worker
# thread deadlocked on PC1, so import them on the main thread at startup.
import duckdb  # noqa: E402
import numpy  # noqa: E402
import pandas as pd  # noqa: E402

HERE = Path(__file__).resolve().parent
EXTENSIONS = HERE.parent
FORK_AGENT = EXTENSIONS.parent / "agent"
DRIVER = HERE / "rank_driver.py"
RANK_CONFIGS = HERE / "rank_configs.json"
RANKER_REL = Path("implementation") / "alpha_signal_pit_ranker"
PITDB_REL = Path("implementation") / "pit_warehouse"
SCRATCH_REL = Path("zt_research") / "rank_runs"
REQUIRED_DRIVE = "E:"
DRIVE_RULE = "ZT 2026-09-28: everything on E:"
DEFAULT_TIMEOUT_S = 900
MAX_LIMIT = 200
AUTHORITY = ("READ_ONLY_RESEARCH: ranker statuses are research admission labels, not "
             "trading signals; no probabilities; no order path")
ASM_SOURCE = "implementation/alpha_signal_monitor_v01/data/promotion_board.csv"
ASM_MARKER = "asm:"
PROMOTION_GATES = ("G0_prereg", "A1_baseline", "A2_sample_split", "A3_t_ge_3",
                   "A4_materiality", "A5_robustness", "A6_replication", "A7_decay_watch")
_CORE_MODULE = "zt_dashboards_core"
_CONFIG_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

mcp = FastMCP("zt-research")


def _load_core():
    """The zt-dashboards readers (same module name its api_routes uses)."""
    module = sys.modules.get(_CORE_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            _CORE_MODULE, EXTENSIONS / "zt_dashboards" / "zt_core.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_MODULE] = module
        spec.loader.exec_module(module)
    return module


core = _load_core()


# ---------------------------------------------------------------------------
# Paths, drive rule, time
# ---------------------------------------------------------------------------

def windows_drive(path) -> str:
    drive = ntpath.splitdrive(str(path))[0].upper()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if drive.startswith(prefix):
            drive = drive[len(prefix):]
    return drive


def require_e_drive(path: Path, what: str) -> Path:
    """Fail closed unless ``path`` is on E: (Windows only)."""
    if os.name != "nt":
        return path
    drive = windows_drive(path)
    system = (os.environ.get("SystemDrive") or "C:").upper()
    if drive != REQUIRED_DRIVE or drive in ("C:", "D:", system):
        raise ValueError(f"{what} must be on {REQUIRED_DRIVE} ({DRIVE_RULE}); got {path}")
    return path


def project_root() -> Path:
    return core.project_root()


def runtime_root(project: Path) -> Path:
    raw = os.environ.get("VIBE_TRADING_HOME", "").strip()
    if not raw:
        raise RuntimeError("VIBE_TRADING_HOME is required for the ranker's scratch output")
    path = require_e_drive(Path(raw).resolve(), "Runtime (VIBE_TRADING_HOME)")
    if path == project or project in path.parents:
        raise ValueError("VIBE_TRADING_HOME may not be inside the shared project")
    return path


def inside(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"path escapes its root: {rel}")
    return target


def asof_utc(raw: str) -> datetime:
    """Explicit, past, offset-qualified instant (aware UTC)."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("asof is required, e.g. 2026-09-25T21:00:00Z")
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("asof requires a timezone offset, e.g. 2026-09-25T21:00:00Z")
    value = value.astimezone(timezone.utc)
    if value > datetime.now(timezone.utc):
        raise ValueError("asof cannot be in the future")
    return value


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _limit(value: int) -> int:
    value = int(value)
    if not 1 <= value <= MAX_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_LIMIT}")
    return value


def _jsonable(value: Any) -> Any:
    """Plain JSON types; NaN/inf become None, numpy scalars become Python numbers."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, numpy.generic):
        value = value.item()
    if isinstance(value, float):
        return value if numpy.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# rank_pit_signals
# ---------------------------------------------------------------------------

def rank_configs() -> dict[str, dict[str, Any]]:
    doc = json.loads(RANK_CONFIGS.read_text(encoding="utf-8"))
    return dict(doc.get("configs") or {})


def _rank_config(name: str) -> dict[str, Any]:
    configs = rank_configs()
    if not _CONFIG_NAME.fullmatch(str(name or "")) or name not in configs:
        raise ValueError(f"unknown config_name {name!r}; choose one of {sorted(configs)}")
    return configs[name]


def export_pitdb_eod(project: Path, at: datetime, spec: dict[str, Any], out_csv: Path) -> dict[str, Any]:
    """EOD bars as known at ``at`` via pitdb's price_asof macro, in an in-memory DuckDB.

    The project's schema.sql defines the macro; the lake parquet is read, never
    written, and DuckDB spilling is disabled so no temp file lands anywhere.
    """
    warehouse = project / PITDB_REL
    lake = Path(os.environ.get("PITDB_LAKE") or warehouse / "lake")
    files = {name: lake / name / "data.parquet" for name in ("fact_price_eod", "dim_security")}
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"pitdb lake table missing: {name}")
    lookback = int(spec.get("lookback_days") or 550)
    if not 30 <= lookback <= 3660:
        raise ValueError("lookback_days must be 30..3660")
    classes = [str(c) for c in spec.get("asset_classes") or ["equity", "etf"]]
    start = (at - timedelta(days=lookback)).date()
    con = duckdb.connect(":memory:")
    try:
        con.execute("SET temp_directory=''")
        con.execute((warehouse / "pitdb" / "schema.sql").read_text(encoding="utf-8"))
        for name, path in files.items():
            con.execute(f"INSERT INTO {name} SELECT * FROM read_parquet('{path.as_posix()}')")
        frame = con.execute("""
            SELECT s.primary_ticker AS ticker, p.event_date, p.open AS "Open", p.high AS "High",
                   p.low AS "Low", p.close AS "Close", p.volume AS "Volume",
                   p.knowledge_time, p.revision_seq
            FROM price_asof(?::TIMESTAMP) p JOIN dim_security s USING (sec_id)
            WHERE list_contains(?::VARCHAR[], s.asset_class)
              AND p.event_date BETWEEN ?::DATE AND ?::DATE
            ORDER BY ticker, p.event_date
        """, [at.replace(tzinfo=None), classes, start, at.date()]).df()
    finally:
        con.close()
    if frame.empty:
        raise ValueError("no pitdb EOD bars were known at asof for the configured universe")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    frame[["ticker", "event_date", "Open", "High", "Low", "Close", "Volume"]].to_csv(out_csv, index=False)
    return {
        "kind": "pitdb_eod",
        "macro": "price_asof(asof)",
        "asset_classes": classes,
        "lookback_days": lookback,
        "rows": int(len(frame)),
        "tickers": int(frame["ticker"].nunique()),
        "first_event_date": str(frame["event_date"].min())[:10],
        "last_event_date": str(frame["event_date"].max())[:10],
        "latest_knowledge_time": str(frame["knowledge_time"].max()),
        "revised_rows": int((frame["revision_seq"] > 0).sum()),
        "lake": {name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
                 for name, path in files.items()},
        "eod_csv": out_csv.name,
    }


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def _run_step(name: str, argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, out, err = -1, str(exc.stdout or ""), f"timed out after {timeout} s"
    return {"name": name, "argv": [str(a) for a in argv], "cwd": str(cwd), "returncode": code,
            "seconds": round(time.monotonic() - started, 2),
            "stdout_tail": _tail(out), "stderr_tail": _tail(err)}


def summarize_ranking(output_dir: Path, limit: int) -> dict[str, Any]:
    ranking_path = output_dir / "signal_ranking.csv"
    if not ranking_path.is_file() or ranking_path.stat().st_size == 0:
        return {"signals": 0, "groups": {}}
    try:
        frame = pd.read_csv(ranking_path)
    except pd.errors.EmptyDataError:
        return {"signals": 0, "groups": {}}
    columns = [c for c in ("rank_within_group", "signal_id", "source_id", "status", "pit_status",
                           "predictive_status", "combined_loss_ratio", "fdr_q_value",
                           "net_sharpe_annualized", "net_mean_return_per_period", "final_rows",
                           "valid_rows", "status_reason") if c in frame.columns]

    def counts(col: str) -> dict[str, int]:
        return {str(k): int(v) for k, v in frame[col].value_counts().sort_index().items()} \
            if col in frame.columns else {}
    groups = {}
    if "ranking_group" in frame.columns:
        for group, rows in frame.groupby("ranking_group", sort=True):
            groups[str(group)] = _jsonable(rows[columns].head(limit).to_dict("records"))
    return {"signals": int(len(frame)), "by_status": counts("status"),
            "by_pit_status": counts("pit_status"),
            "by_predictive_status": counts("predictive_status"), "groups": groups}


def rank_pit_signals(config_name: str = "rank01_market_prices", asof: str = "",
                     limit: int = 20) -> dict:
    """Rank candidate signals with the project's PIT alpha ranker as of an instant.

    config_name names an input in extensions/zt_research/rank_configs.json
    (rank01_market_prices: pitdb EOD features as known at asof;
    rank01_market_prices_retained: the retained Rank-01 panel).  asof is an
    explicit past instant with a timezone offset; only rows whose availability,
    decision and target times are all at or before asof are ranked.  Writes only
    to an E: scratch run folder under VIBE_TRADING_HOME/zt_research/rank_runs and
    returns the ranking summary (top ``limit`` rows per like-for-like group) plus
    the manifest paths.  Statuses such as ALPHA_CANDIDATE are research admission
    labels, never trading signals."""
    config = _rank_config(config_name)
    at = asof_utc(asof)
    limit = _limit(limit)
    project = project_root()
    ranker = project / RANKER_REL
    if not (ranker / "pit_alpha_ranker" / "cli.py").is_file():
        raise FileNotFoundError(f"ranker package not found under {RANKER_REL.as_posix()}")
    ranker_config = inside(ranker, config["ranker_config"])
    if not ranker_config.is_file():
        raise FileNotFoundError(f"ranker config not found: {config['ranker_config']}")
    home = runtime_root(project)
    started = datetime.now(timezone.utc)
    run_id = f"{started:%Y%m%dT%H%M%SZ}_{config_name}_{at:%Y%m%dT%H%M}_{uuid.uuid4().hex[:6]}"
    run_dir = require_e_drive(home / SCRATCH_REL / run_id, "rank scratch folder")
    (run_dir / "input").mkdir(parents=True)
    (run_dir / "tmp").mkdir()
    output_dir = run_dir / "output"
    budget = int(os.environ.get("ZT_RESEARCH_RANK_TIMEOUT") or DEFAULT_TIMEOUT_S)
    deadline = time.monotonic() + budget      # one budget for both child steps
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               TEMP=str(run_dir / "tmp"), TMP=str(run_dir / "tmp"), TMPDIR=str(run_dir / "tmp"))
    kind = config["input"]["kind"]
    if kind == "pitdb_eod":
        eod_csv = run_dir / "input" / "eod_prices_asof.csv"
        input_meta = export_pitdb_eod(project, at, config["input"], eod_csv)
        source_args = ["--eod-csv", str(eod_csv), "--adapter", config["input"]["adapter"]]
    elif kind == "observations_csv":
        source = inside(project, config["input"]["path"])
        if not source.is_file():
            raise FileNotFoundError(f"observations not found: {config['input']['path']}")
        input_meta = {"kind": kind, "path": config["input"]["path"],
                      "bytes": source.stat().st_size, "sha256": sha256_file(source)}
        source_args = ["--observations-csv", str(source)]
    else:
        raise ValueError(f"unsupported input kind {kind!r}")
    observations = run_dir / "input" / "observations_asof.csv"
    cut_report = run_dir / "input" / "pit_cut.json"
    steps = [_run_step("build_observations", [
        sys.executable, "-B", str(DRIVER), "--ranker-dir", str(ranker), "--asof", iso(at),
        "--out", str(observations), "--report", str(cut_report), *source_args],
        run_dir, env, max(1, int(deadline - time.monotonic())))]
    pit_cut = json.loads(cut_report.read_text(encoding="utf-8")) if cut_report.is_file() else None
    if steps[0]["returncode"] == 0:
        steps.append(_run_step("pit_alpha_ranker_run", [
            sys.executable, "-B", "-m", "pit_alpha_ranker", "run", "--input", str(observations),
            "--config", str(ranker_config), "--output", str(output_dir)], ranker, env,
            max(1, int(deadline - time.monotonic()))))
    status = "PASS" if all(step["returncode"] == 0 for step in steps) and len(steps) == 2 else "FAIL"
    outputs = {}
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        outputs[path.relative_to(run_dir).as_posix()] = {"bytes": path.stat().st_size,
                                                         "sha256": sha256_file(path)}
    ranker_manifest = output_dir / "run_manifest.json"
    manifest = {
        "schema": "zt-research-rank-run/1",
        "tool": "rank_pit_signals",
        "status": status,
        "config_name": config_name,
        "config": config,
        "config_sha256": _canonical_sha(config),
        "asof": iso(at),
        "started_at": iso(started),
        "finished_at": iso(datetime.now(timezone.utc)),
        "project_root": str(project),
        "run_dir": str(run_dir),
        "ranker": {
            "dir": RANKER_REL.as_posix(),
            "config": config["ranker_config"],
            "config_sha256": sha256_file(ranker_config),
            "package_sha256": {p.name: sha256_file(p) for p in
                               sorted((ranker / "pit_alpha_ranker").glob("*.py"))},
        },
        "driver_sha256": sha256_file(DRIVER),
        "input": input_meta,
        "pit_cut": pit_cut,
        "steps": steps,
        "outputs": outputs,
        "ranker_manifest": "output/run_manifest.json" if ranker_manifest.is_file() else None,
        "authority": AUTHORITY,
    }
    manifest_path = run_dir / "zt_research_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    result = {
        "tool": "rank_pit_signals",
        "status": status,
        "config_name": config_name,
        "asof": iso(at),
        "run_dir": str(run_dir),
        "manifest_path": str(manifest_path),
        "ranker_manifest_path": str(ranker_manifest) if ranker_manifest.is_file() else None,
        "input": {k: v for k, v in input_meta.items() if k != "lake"},
        "pit_cut": pit_cut,
        "summary": summarize_ranking(output_dir, limit) if status == "PASS" else None,
        "authority": AUTHORITY,
    }
    if status != "PASS":
        failed = next(step for step in steps if step["returncode"] != 0)
        result["error"] = f"{failed['name']} failed (exit {failed['returncode']}): " \
                          f"{failed['stderr_tail'][-600:]}"
    return result


# ---------------------------------------------------------------------------
# ASM readers
# ---------------------------------------------------------------------------

def asm_promotion_board() -> dict:
    """ASM phase-3 promotion board: every candidate with eligibility, status,
    verdict and the eight-gate battery (G0_prereg .. A7_decay_watch), in the
    zt-dashboards envelope (as_of, sha256, FRESH/STALE/MISSING).  Monitored
    candidates are not admitted signals."""
    return core.promotion_board(root=project_root())


def hypothesis_test_ledger(limit: int = 200) -> dict:
    """ASM multiple-testing ledger: cumulative test count, the rising t hurdle
    max(3.0, 2.57 + 0.5*log10(n)), counts by run date and role, duplicate test
    ids, and the most recent ``limit`` rows (1..200)."""
    limit = _limit(limit)
    root = project_root()
    envelope = core.test_ledger(root=root)
    blob = core.read_blob(root, core.TEST_LEDGER_REL)
    if blob is None or not isinstance(envelope.get("data"), dict) or "error" in envelope["data"]:
        return envelope
    raw = core.csv_rows(blob)
    seen: dict[str, int] = {}
    for row in raw:
        seen[row.get("test_id", "")] = seen.get(row.get("test_id", ""), 0) + 1
    envelope["data"]["duplicate_test_ids"] = sorted(k for k, v in seen.items() if v > 1)[:MAX_LIMIT]
    envelope["data"]["rows"] = [
        core.pick(row, ("test_id", "run_date", "monitor_version", "signal_id", "family",
                        "statistic", "statistic_value", "n", "role", "hlz_hurdle_applies", "note"),
                  numeric=("statistic_value", "n"))
        for row in raw[-limit:]]
    return envelope


# ---------------------------------------------------------------------------
# import_promotion_board_to_vt
# ---------------------------------------------------------------------------

_FAIL_WORDS = {"FAIL", "FAILED", "REJECT", "REJECTED", "DEMOTED", "RETIRED", "INVALID"}
_PASS_WORDS = {"PASS", "PASSED", "ADMITTED", "PROMOTED", "VALIDATED"}
_RUNNING_WORDS = {"IN_PROGRESS", "RUNNING", "TESTING", "STARTED", "OPEN"}
_NOT_RUN = {"", "NOT_RUN", "NOT RUN", "NA", "N/A", "--"}


def vt_status(row: dict[str, Any]) -> tuple[str, str]:
    """Board row -> (VT hypothesis status, reason).  Conservative: 'validated'
    needs a PASS verdict AND G0..A6 all PASS; 'monitoring' additionally needs the
    A7 decay watch running; any FAIL gate or fail verdict is 'rejected'."""
    gates = [str((row.get("gates") or {}).get(g, "") or "").strip().upper() for g in PROMOTION_GATES]
    verdict = str(row.get("verdict") or "").strip().upper()
    status = str(row.get("status") or "").strip().upper()
    failed = [g for g, v in zip(PROMOTION_GATES, gates, strict=True) if v.startswith("FAIL")]
    if failed or verdict in _FAIL_WORDS or status in _FAIL_WORDS:
        return "rejected", ("failed gate " + ", ".join(failed)) if failed else f"verdict/status {verdict or status}"
    if verdict in _PASS_WORDS or status in _PASS_WORDS:
        if all(v == "PASS" for v in gates[:7]):
            if gates[7] not in _NOT_RUN:
                return "monitoring", "all gates passed; A7 decay watch running"
            return "validated", "all gates G0..A6 passed"
        return "testing", "pass verdict recorded but gates G0..A6 are not all PASS"
    if any(v not in _NOT_RUN for v in gates) or status in _RUNNING_WORDS:
        run = sum(1 for v in gates if v not in _NOT_RUN)
        return "testing", f"{run} of 8 gates run"
    return "exploring", "no gate run yet"


def board_to_hypothesis(row: dict[str, Any]) -> dict[str, Any]:
    """The VT hypothesis fields the importer owns for one board row."""
    signal = str(row.get("signal_id") or "").strip()
    name = str(row.get("signal_name") or signal).strip()
    family, cadence = str(row.get("family") or "").strip(), str(row.get("cadence") or "").strip()
    status, reason = vt_status(row)
    gates = row.get("gates") or {}
    gate_text = ", ".join(f"{g}={gates.get(g) or 'NOT_RUN'}" for g in PROMOTION_GATES)
    verdict = str(row.get("verdict") or "").strip() or "none"
    verdict_date = str(row.get("verdict_date") or "").strip() or "none"
    return {
        "title": f"ASM {signal}: {name}"[:200],
        "thesis": (f"Alpha Signal Monitor candidate {signal} ({family}, {cadence}): {name}. "
                   "A monitored state, not an admitted signal: admission needs all eight "
                   "promotion gates, G0 pre-registration through the A7 decay watch."),
        "status": status,
        "signal_definition": (f"{name}. Board selection rule: {row.get('selection_rule') or 'n/a'}. "
                              f"Eligible: {row.get('eligible') or 'n/a'} (n_obs={row.get('n_obs')})."),
        "data_sources": [f"{ASM_MARKER}{signal}", f"asm_promotion_board:{ASM_SOURCE}"],
        "invalidation_notes": (f"ASM board status {row.get('status') or 'n/a'}; verdict {verdict} "
                               f"({verdict_date}); gates: {gate_text}. VT status '{status}' "
                               f"because {reason}."),
    }


def _hypotheses_path() -> Path:
    _ensure_vt_importable()
    from src.hypotheses.registry import default_hypotheses_path
    return default_hypotheses_path()


def _ensure_vt_importable() -> None:
    if "src" in sys.modules:
        return
    if (FORK_AGENT / "src" / "hypotheses" / "registry.py").is_file():
        if str(FORK_AGENT) in sys.path:
            sys.path.remove(str(FORK_AGENT))
        sys.path.insert(0, str(FORK_AGENT))


def _read_hypotheses(path: Path) -> list[dict[str, Any]]:
    """VT's store read without the registry (whose constructor creates folders)."""
    if not path.exists():
        return []
    _ensure_vt_importable()
    from src.hypotheses.registry import Hypothesis
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("hypotheses storage must contain a JSON list")
    return [Hypothesis.from_dict(item).to_dict() for item in raw if isinstance(item, dict)]


def plan_import(board_rows: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_marker: dict[str, list[dict[str, Any]]] = {}
    for hyp in existing:
        for source in hyp.get("data_sources") or []:
            if str(source).startswith(ASM_MARKER):
                by_marker.setdefault(str(source), []).append(hyp)
    plan, seen = [], set()
    for row in board_rows:
        signal = str(row.get("signal_id") or "").strip()
        if not signal:
            continue
        desired = board_to_hypothesis(row)
        marker = desired["data_sources"][0]
        seen.add(marker)
        matches = by_marker.get(marker, [])
        if len(matches) > 1:
            plan.append({"action": "conflict", "signal_id": signal,
                         "hypothesis_ids": [h["hypothesis_id"] for h in matches],
                         "reason": "more than one VT hypothesis carries this ASM marker"})
            continue
        if not matches:
            plan.append({"action": "create", "signal_id": signal, "fields": desired})
            continue
        current = matches[0]
        changes = {}
        for field in ("title", "status", "signal_definition", "invalidation_notes"):
            if current.get(field) != desired[field]:
                changes[field] = {"from": current.get(field), "to": desired[field]}
        sources = list(current.get("data_sources") or [])
        merged = sources + [s for s in desired["data_sources"] if s not in sources]
        if merged != sources:
            changes["data_sources"] = {"from": sources, "to": merged}
        plan.append({"action": "update" if changes else "unchanged", "signal_id": signal,
                     "hypothesis_id": current["hypothesis_id"], "changes": changes})
    for marker, hyps in sorted(by_marker.items()):
        if marker not in seen:
            for hyp in hyps:
                plan.append({"action": "orphan", "signal_id": marker[len(ASM_MARKER):],
                             "hypothesis_id": hyp["hypothesis_id"],
                             "reason": "no longer on the ASM board; left untouched"})
    return plan


def import_promotion_board_to_vt(apply: bool = False) -> dict:
    """Map ASM promotion-board rows to Vibe-Trading research hypotheses.

    Each board signal becomes one VT hypothesis marked with data source
    'asm:<signal_id>'.  Status is conservative: exploring (no gate run),
    testing (gates running), validated (PASS verdict and G0..A6 PASS),
    monitoring (plus A7 decay watch running), rejected (any FAIL).  The importer
    owns title, status, signal definition and invalidation notes; thesis,
    universe and skills are set on creation only, so VT-side edits survive.
    Default apply=False returns the diff and writes nothing; apply=True writes
    only to VT's hypotheses store.  Hypotheses whose signal left the board are
    reported as orphans, never deleted."""
    root = project_root()
    board = core.promotion_board(root=root)
    rows = (board.get("data") or {}).get("rows") if isinstance(board.get("data"), dict) else None
    store = _hypotheses_path()
    result: dict[str, Any] = {
        "tool": "import_promotion_board_to_vt",
        "mode": "apply" if apply else "dry_run",
        "store": str(store),
        "board": {k: board.get(k) for k in ("as_of", "sha256", "status", "source_path")},
        "authority": "Research bookkeeping only: a VT hypothesis status is not an admission "
                     "verdict and carries no trading authority.",
    }
    if not rows:
        result.update(summary={}, changes=[], note="promotion board missing or empty; nothing to import")
        return result
    plan = plan_import(rows, _read_hypotheses(store))
    summary: dict[str, int] = {}
    for item in plan:
        summary[item["action"]] = summary.get(item["action"], 0) + 1
    result["summary"] = dict(sorted(summary.items()))
    if apply and any(item["action"] in ("create", "update") for item in plan):
        require_e_drive(store, "VT hypotheses store")
        _ensure_vt_importable()
        from src.hypotheses.registry import HypothesisRegistry
        registry = HypothesisRegistry(store)
        for item in plan:
            if item["action"] == "create":
                fields = item["fields"]
                created = registry.create(title=fields["title"], thesis=fields["thesis"],
                                          status=fields["status"],
                                          signal_definition=fields["signal_definition"],
                                          data_sources=fields["data_sources"],
                                          invalidation_notes=fields["invalidation_notes"])
                item["hypothesis_id"] = created.hypothesis_id
            elif item["action"] == "update":
                registry.update(item["hypothesis_id"],
                                **{field: change["to"] for field, change in item["changes"].items()})
        result["applied"] = True
    else:
        result["applied"] = False
    result["changes"] = plan
    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
             "openWorldHint": False}
SCRATCH_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False}
VT_STORE_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                  "openWorldHint": False}

TOOLS = ((rank_pit_signals, SCRATCH_WRITE), (asm_promotion_board, READ_ONLY),
         (hypothesis_test_ledger, READ_ONLY), (import_promotion_board_to_vt, VT_STORE_WRITE))
TOOL_NAMES = tuple(tool.__name__ for tool, _ in TOOLS)

for _tool, _annotations in TOOLS:
    mcp.tool(_tool, annotations=_annotations)


if __name__ == "__main__":
    if "show_banner" in inspect.signature(mcp.run).parameters:
        mcp.run(show_banner=False)
    else:
        mcp.run()
