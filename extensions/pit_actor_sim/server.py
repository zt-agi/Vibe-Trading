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

try:
    import pit_guard
except ImportError:  # imported as a package module rather than run as a script
    from . import pit_guard
try:
    import fork_rules
except ImportError:  # imported as a package module rather than run as a script
    from . import fork_rules

# Operator storage rule (ZT 2026-09-28: everything on E:). On Windows the
# runtime state and the disposable PIT index must live on E:; C:, D: and the
# system drive are refused outright. The guards live in pit_guard.py, shared
# with VT's source="pitdb" backtest loader; these names are re-exported.
from pit_guard import (  # noqa: E402,F401
    DEFAULT_INDEX,
    DRIVE_RULE,
    REQUIRED_DRIVE,
    require_e_drive,
    windows_drive,
)

mcp = FastMCP("pit-actor-sim")


def project() -> Path:
    return pit_guard.validate_project_root(os.environ.get("INVESTMENT_AI_PROJECT_ROOT"))


def runtime() -> Path:
    return pit_guard.validate_runtime_root(os.environ.get("VIBE_TRADING_HOME"), project)


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


_READ_TABLES = pit_guard.READ_TABLES


def lake_signature(tables=None) -> dict:
    """Cheap freshness token for compact tables in the canonical Parquet lake."""
    return pit_guard.lake_signature(project(), tables)


def require_fresh_index(full: bool = False) -> None:
    pit_guard.require_fresh_index(project(), runtime(),
                                  tables=None if full else _READ_TABLES,
                                  signature=lake_signature)


def refresh_index() -> dict:
    """Rebuild only the disposable local index, then record lake freshness."""
    before = lake_signature()
    from pitdb import config as C
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
               "index_path": str(C.DB_PATH), "rows": sum(counts.values())}
    root = runtime()
    root.mkdir(parents=True, exist_ok=True)
    pit_guard.write_json_atomic(root / pit_guard.INDEX_RECEIPT, receipt)
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
    checks = pit_guard.parse_audit_checks(run.stdout)
    if checks["failed"]:
        raise RuntimeError("PIT audit failed: " + ", ".join(checks["failed"]))
    after = lake_signature()
    if before != after:
        raise RuntimeError("Lake changed during audit; retry preflight")
    # checks_passed names every check the audit printed as PASS, so a consumer
    # can require one (VT's pitdb loader requires A11 for formation mode).
    receipt = {"signature": after,
               "audited_at_utc": datetime.now(timezone.utc).isoformat(),
               "status": "PASS", "checks": len(checks["passed"]),
               "checks_passed": checks["passed"]}
    pit_guard.write_json_atomic(runtime() / pit_guard.AUDIT_RECEIPT, receipt)
    return receipt


def require_fresh_audit() -> dict:
    return pit_guard.require_fresh_audit(runtime(), lake_signature)


def _trace(step: str) -> None:
    if os.environ.get("VIBE_EXTENSION_TRACE") == "1":
        path = runtime() / "pit_actor_trace.log"
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
    # ZT add-on: "actor_packets/<sha256>/..." names a frozen packet's file in
    # the E: runtime; everything else stays under the canonical market_actor_sim.
    if str(relative).replace("\\", "/").startswith(PACKETS_DIRNAME + "/"):
        return packet_file(relative)
    base = (project() / "market_actor_sim").resolve(strict=True)
    path = (base / relative).resolve(strict=True)
    if base not in path.parents or not path.is_file():
        raise ValueError("input must be a file under market_actor_sim")
    return path


def index_path() -> Path:
    """Validated disposable PIT index for child processes (default E:\\pitdb\\pit.duckdb)."""
    raw = os.environ.get("PITDB_INDEX", "").strip()
    if raw.lower() in ("", "local", "file", "disk"):
        raw = DEFAULT_INDEX
    elif raw.lower() in ("memory", ":memory:", "none"):
        raise RuntimeError(f"PITDB_INDEX={raw} is not supported here; set PITDB_INDEX={DEFAULT_INDEX}")
    return require_e_drive(Path(raw), "PIT index (PITDB_INDEX)")


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, folder in (("TEMP", "tmp"), ("TMP", "tmp"),
                        ("PYTHONPYCACHEPREFIX", "pycache")):
        path = runtime() / folder
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env["PITDB_INDEX"] = str(index_path())
    env["PYTHONPATH"] = str(project() / "implementation" / "pit_warehouse")
    return env


# --------------------------------------------------------------------------
# ZT add-on (2026-09-28): frozen evidence packets and role-fork admission.
#
# A packet is the only thing a role fork may read. It is content-addressed
# (sha256 of its canonical JSON) and written once under
# runtime()/actor_packets/<sha256>/, with the warehouse audit receipt hash and
# the lake signature it was frozen against. Its facts are re-read here through
# the PIT macros at the packet's as-of; an agent names series, it never types
# values into the packet. Forks are validated on submission (fork_rules) and
# again, as a set of at least three, before the simulator runs.
# --------------------------------------------------------------------------

PACKETS_DIRNAME = "actor_packets"
PACKET_SCHEMA = "vt.actor_packet.v1"
RUN_MANIFEST_SCHEMA = "vt.actor_run_manifest.v1"
ADMITTED_PIT_CLASSES = ("TRUE_PIT", "OBSERVED_PIT", "RECONSTRUCTED_PIT")
MAX_PACKET_SERIES = 50
MAX_PACKET_ROWS = 2000
MAX_PACKET_MEMOS = 20
MAX_MEMO_CHARS = 8000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SCENARIO_ID_RE = re.compile(r"[a-z0-9][a-z0-9_]{1,63}")
PACKET_LIMITS = (
    "Only rows re-read through obs_asof/price_asof at the packet as-of are admitted facts.",
    "Prediction-market prices are external market observations, not actor documents and "
    "not calibrated action propensities.",
    "Role forks score only their own actor's decision at each state and widen toward "
    "uniform when direct role evidence is absent.",
    "No agent may estimate or mention a terminal-outcome probability; outcome "
    "distributions come only from run_market_actor_sim.",
)


def canonical_json(value) -> bytes:
    """Stable bytes for content addressing (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def packets_root() -> Path:
    return runtime() / PACKETS_DIRNAME


def packet_dir(packet_sha256: str) -> Path:
    if not _SHA256_RE.fullmatch(str(packet_sha256 or "")):
        raise ValueError("packet_sha256 must be 64 lowercase hex characters")
    return packets_root() / packet_sha256


def packet_file(relative: str) -> Path:
    """Resolve ``actor_packets/<sha256>/<file>`` inside the E: runtime only."""
    parts = str(relative).replace("\\", "/").split("/")
    if len(parts) < 3 or parts[0] != PACKETS_DIRNAME or not _SHA256_RE.fullmatch(parts[1]):
        raise ValueError("packet input must be actor_packets/<sha256>/<file>")
    base = packets_root().resolve(strict=True)
    path = (base / "/".join(parts[1:])).resolve(strict=True)
    if base not in path.parents or not path.is_file():
        raise ValueError("packet input must be a file inside the runtime packet store")
    return path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_once(path: Path, text: str) -> bool:
    """Create ``path`` with ``text`` unless it exists; True when this call wrote it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.link(temporary, path)
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def scenario_source(scenario_id: str) -> tuple[str, Path]:
    """``iran_oil`` -> config/scenario_iran_oil.yaml under market_actor_sim."""
    raw = str(scenario_id or "").strip()
    relative = f"config/scenario_{raw}.yaml" if _SCENARIO_ID_RE.fullmatch(raw) else raw
    path = sim_input(relative)
    if path.suffix not in (".yaml", ".yml"):
        raise ValueError("scenario must be a YAML file under market_actor_sim")
    return relative.replace("\\", "/"), path


def audit_receipt_digest() -> tuple[dict, str]:
    """The fresh audit receipt (fail closed if stale) and its content hash."""
    receipt = require_fresh_audit()
    return receipt, sha256_hex(canonical_json(receipt))


def load_packet(packet_sha256: str) -> dict:
    """Read a frozen packet and prove its content still hashes to its name."""
    path = packet_dir(packet_sha256) / "packet.json"
    if not path.is_file():
        raise FileNotFoundError(f"no frozen packet {packet_sha256}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if sha256_hex(canonical_json(body)) != packet_sha256:
        raise RuntimeError(f"packet {packet_sha256} content no longer matches its hash")
    return body


def packet_tree(packet: dict) -> fork_rules.ScenarioTree:
    """The packet's scenario, refusing a file that changed after the freeze."""
    path = sim_input(packet["scenario"]["path"])
    data = path.read_bytes()
    if sha256_hex(data) != packet["scenario"]["sha256"]:
        raise RuntimeError(
            f"scenario {packet['scenario']['path']} changed after packet freeze; freeze a new packet")
    return fork_rules.parse_scenario(data.decode("utf-8"))


def _admit_rows(rows: list[dict], at: datetime, label: str, excluded: list[dict]) -> list[dict]:
    """Rows a packet may hold as facts; one exclusion note per refused class."""
    admitted, refused = [], {}
    for row in rows:
        raw = row.get("knowledge_time")
        if raw is None:
            refused["missing knowledge_time"] = refused.get("missing knowledge_time", 0) + 1
            continue
        known = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if known.tzinfo is not None:
            known = known.astimezone(timezone.utc).replace(tzinfo=None)
        if known > at:
            raise RuntimeError(f"PIT macro returned {label} with knowledge_time after the as-of")
        if row.get("pit_class") not in ADMITTED_PIT_CLASSES:
            reason = f"pit_class {row.get('pit_class')!r}"
            refused[reason] = refused.get(reason, 0) + 1
            continue
        admitted.append(row)
    for reason, count in sorted(refused.items()):
        excluded.append({"item": label,
                         "reason": f"{count} row(s) refused as packet facts: {reason}"})
    return admitted


@mcp.tool
def freeze_evidence_packet(scenario_id: str, run_asof: str, series: list[dict],
                           prices: list[dict] | None = None,
                           memos: list[dict] | None = None,
                           excluded_or_missing: list[dict] | None = None,
                           interpretation_limits: list[str] | None = None) -> dict:
    """Freeze a content-addressed evidence packet for role forks.

    Args:
        scenario_id: Scenario id (``iran_oil`` -> config/scenario_iran_oil.yaml)
            or a path relative to market_actor_sim.
        run_asof: UTC instant with offset; every fact must be known by then.
        series: ``[{"series_id", "start_date", "end_date", "limit"?}]``, re-read
            through obs_asof at run_asof.
        prices: ``[{"ticker", "start_date", "end_date", "limit"?}]``, re-read
            through price_asof at run_asof.
        memos: Evidence-worker memos ``[{"author", "text", "citations"}]``;
            each citation must name a series_id or evidence id admitted here.
        excluded_or_missing: ``[{"item", "reason"}]`` gaps role forks must see.
        interpretation_limits: Extra limits; the fixed limits are always added.

    Returns:
        The packet sha256, where it is stored, and what it admitted.
    """
    at = asof_utc(run_asof)
    run_asof_utc = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    scenario_rel, scenario_path = scenario_source(scenario_id)
    scenario_bytes = scenario_path.read_bytes()
    tree = fork_rules.parse_scenario(scenario_bytes.decode("utf-8"))
    series = list(series or [])
    prices = list(prices or [])
    if not series and not prices:
        raise ValueError("a packet needs at least one series or price request")
    if len(series) + len(prices) > MAX_PACKET_SERIES:
        raise ValueError(f"at most {MAX_PACKET_SERIES} series and price requests per packet")
    if len(memos or []) > MAX_PACKET_MEMOS:
        raise ValueError(f"at most {MAX_PACKET_MEMOS} memos per packet")
    receipt, receipt_sha = audit_receipt_digest()
    signature = lake_signature()
    excluded: list[dict] = []
    observations: list[dict] = []
    for request in series:
        found = pit_series_history(str(request["series_id"]), run_asof, str(request["start_date"]),
                                   str(request["end_date"]), int(request.get("limit", 50)))
        rows = _admit_rows(found["rows"], at, f"series {request['series_id']}", excluded)
        if not found["rows"]:
            excluded.append({"item": f"series {request['series_id']}",
                             "reason": "no row known at the packet as-of in the requested window"})
        observations.extend(rows)
    price_rows: list[dict] = []
    for request in prices:
        ticker = str(request["ticker"])
        securities = pit_security(ticker, run_asof)["securities"]
        sec_ids = sorted({row["sec_id"] for row in securities})
        if len(sec_ids) != 1:
            excluded.append({"item": f"prices {ticker}",
                             "reason": f"ticker resolved to {len(sec_ids)} securities at the as-of"})
            continue
        found = pit_price_history(sec_ids[0], run_asof, str(request["start_date"]),
                                  str(request["end_date"]), int(request.get("limit", 50)))
        price_rows.extend(_admit_rows(found["rows"], at, f"prices {ticker}", excluded))
    if len(observations) + len(price_rows) > MAX_PACKET_ROWS:
        raise ValueError(f"packet exceeds {MAX_PACKET_ROWS} admitted rows; narrow the windows")
    if not observations and not price_rows:
        raise ValueError("no admissible PIT row was known at the as-of; nothing to freeze")
    admitted_observations = [
        {"evidence_id": f"E{index}", **{key: row.get(key) for key in (
            "series_id", "label", "unit", "event_time", "value_num", "value_str", "quality",
            "knowledge_time", "revision_seq", "source_id", "pit_class")}}
        for index, row in enumerate(observations, start=1)
    ]
    admitted_prices = [
        {"evidence_id": f"P{index}", **{key: row.get(key) for key in (
            "sec_id", "primary_ticker", "event_date", "open", "high", "low", "close", "volume",
            "currency", "knowledge_time", "revision_seq", "source_id", "pit_class")}}
        for index, row in enumerate(price_rows, start=1)
    ]
    frozen_exclusions = [
        {"exclusion_id": f"X{index}", "item": str(item.get("item", ""))[:300],
         "reason": str(item.get("reason", ""))[:600]}
        for index, item in enumerate([*(excluded_or_missing or []), *excluded], start=1)
    ]
    draft = {"admitted_observations": admitted_observations, "admitted_prices": admitted_prices,
             "memos": []}
    ids = fork_rules.packet_citation_ids(draft)
    frozen_memos = []
    problems = []
    for index, memo in enumerate(memos or [], start=1):
        text = str(memo.get("text", ""))
        if not text.strip() or len(text) > MAX_MEMO_CHARS:
            problems.append(f"memo {index}: text must be 1..{MAX_MEMO_CHARS} characters")
        cited, unresolved = [], []
        for citation in memo.get("citations", []) or []:
            target = fork_rules.resolve_citation(str(citation), ids)
            (cited if target else unresolved).append(target or str(citation)[:80])
        if unresolved or not cited:
            problems.append(f"memo {index}: citations must name admitted series_id or evidence ids; "
                            f"unresolved {unresolved}")
        frozen_memos.append({"memo_id": f"M{index}", "author": str(memo.get("author", ""))[:80],
                             "text": text, "citations": sorted(set(cited))})
    if problems:
        raise ValueError("packet not frozen: " + "; ".join(problems))
    here = Path(__file__).resolve()
    body = {
        "schema": PACKET_SCHEMA,
        "authority": "RESEARCH_PILOT_ONLY",
        "run_asof_utc": run_asof_utc,
        "asof_utc_naive": at.isoformat(timespec="seconds"),
        "warehouse_contract": "Only obs_asof/price_asof outputs are admitted as factual inputs.",
        "scenario": {
            "id": str(scenario_id).strip(), "path": scenario_rel,
            "sha256": sha256_hex(scenario_bytes), "name": tree.name,
            "description": tree.description, "rewards": tree.rewards,
            "headline_outcomes": tree.headline_outcomes, "outcomes": tree.outcomes,
            "states": tree.fork_view(),
        },
        "warehouse_audit": {"status": receipt.get("status"), "checks": receipt.get("checks"),
                            "audited_at_utc": receipt.get("audited_at_utc"),
                            "receipt_sha256": receipt_sha},
        "lake_signature": signature,
        "lake_signature_sha256": sha256_hex(canonical_json(signature)),
        "admitted_observations": admitted_observations,
        "admitted_prices": admitted_prices,
        "memos": frozen_memos,
        "excluded_or_missing": frozen_exclusions,
        "interpretation_limits": [*PACKET_LIMITS, *[str(x)[:600] for x in (interpretation_limits or [])]],
        "extension": {"server_sha256": sha256_hex(here.read_bytes()),
                      "fork_rules_sha256": sha256_hex(here.with_name("fork_rules.py").read_bytes())},
    }
    if body["warehouse_audit"]["status"] != "PASS":
        raise RuntimeError("PIT audit receipt is not PASS; run server.py --refresh-audit")
    packet_sha = sha256_hex(canonical_json(body))
    folder = packet_dir(packet_sha)
    created = _write_once(folder / "packet.json", json.dumps(body, indent=2, ensure_ascii=False))
    if created:
        _atomic_write(folder / "receipt.json", json.dumps({
            "packet_sha256": packet_sha,
            "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "audit_receipt": receipt}, indent=2))
    load_packet(packet_sha)
    return {"packet_sha256": packet_sha, "created": created,
            "packet_path": f"{PACKETS_DIRNAME}/{packet_sha}/packet.json",
            "run_asof_utc": run_asof_utc, "scenario": body["scenario"]["path"],
            "counts": {"observations": len(admitted_observations), "prices": len(admitted_prices),
                       "memos": len(frozen_memos), "excluded_or_missing": len(frozen_exclusions),
                       "decision_states": len(tree.states)},
            "warehouse_audit": body["warehouse_audit"],
            "next": "Role forks call inspect_evidence_packet(packet_sha256, view='role_fork') "
                    "and submit_role_fork; then run_market_actor_sim(packet_sha256=...)."}


def accepted_forks(packet_sha256: str) -> list[dict]:
    """Forks accepted for a packet, each re-verified against its content hash."""
    forks = []
    for path in sorted((packet_dir(packet_sha256) / "forks").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        fork = {key: record[key] for key in ("temperament", "memos", "propensities")}
        if sha256_hex(canonical_json(fork)) != record.get("fork_sha256"):
            raise RuntimeError(f"fork {path.name} content no longer matches its hash")
        forks.append({**fork, "fork_sha256": record["fork_sha256"]})
    return forks


@mcp.tool
def inspect_evidence_packet(packet_sha256: str, view: str = "role_fork") -> dict:
    """Read a frozen packet. ``role_fork`` hides outcome labels, rewards and other forks."""
    packet = load_packet(packet_sha256)
    if view == "full":
        forks = accepted_forks(packet_sha256)
        return {**packet, "packet_sha256": packet_sha256,
                "accepted_forks": [{"temperament": f["temperament"], "fork_sha256": f["fork_sha256"]}
                                   for f in forks]}
    if view != "role_fork":
        raise ValueError("view must be 'role_fork' or 'full'")
    return {
        "packet_sha256": packet_sha256,
        "authority": packet["authority"],
        "run_asof_utc": packet["run_asof_utc"],
        "decision_states": packet["scenario"]["states"],
        "admitted_observations": packet["admitted_observations"],
        "admitted_prices": packet["admitted_prices"],
        "memos": packet["memos"],
        "excluded_or_missing": packet["excluded_or_missing"],
        "interpretation_limits": packet["interpretation_limits"],
        "submission_contract": {
            "tool": "submit_role_fork",
            "per_state": "memo {actor, analysis >= 120 chars discussing every action, evidence "
                         "[packet ids or series_id], missing_observables} and a propensity per "
                         "action, each strictly between 0 and 1, summing to 1",
            "uncited_state": f"name a missing observable and stay within "
                             f"{fork_rules.UNCITED_UNIFORM_TOLERANCE} of uniform",
            "forbidden": "any statement about how the scenario ends or its probability",
        },
    }


@mcp.tool
def submit_role_fork(packet_sha256: str, temperament: str, memos: dict,
                     propensities: dict) -> dict:
    """Submit one temperament fork (memos + state-local propensities) for a packet.

    Rejected forks are not stored; the error lists every failed rule.
    """
    packet = load_packet(packet_sha256)
    tree = packet_tree(packet)
    try:
        fork = fork_rules.validate_fork(
            {"temperament": temperament, "memos": memos, "propensities": propensities},
            tree, packet)
    except fork_rules.ForkRejected as rejected:
        raise ValueError("role fork rejected: " + " | ".join(rejected.problems)) from None
    fork_sha = sha256_hex(canonical_json(fork))
    record = {**fork, "fork_sha256": fork_sha,
              "submitted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    target = packet_dir(packet_sha256) / "forks" / f"{fork['temperament']}.json"
    created = _write_once(target, json.dumps(record, indent=2, ensure_ascii=False))
    if not created:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("fork_sha256") != fork_sha:
            raise ValueError(f"temperament {fork['temperament']!r} was already submitted with "
                             "different content; use a new temperament label")
    count = len(accepted_forks(packet_sha256))
    return {"accepted": True, "duplicate": not created, "packet_sha256": packet_sha256,
            "temperament": fork["temperament"], "fork_sha256": fork_sha,
            "forks_accepted": count, "forks_required": fork_rules.MIN_FORKS,
            "ready_to_simulate": count >= fork_rules.MIN_FORKS}


def packet_inputs(packet_sha256: str, fork_order: list[str] | None) -> tuple[Path, Path, Path, dict]:
    """Scenario, assembled forks file and packet file for a packet-driven run."""
    packet = load_packet(packet_sha256)
    tree = packet_tree(packet)
    forks = accepted_forks(packet_sha256)
    by_label = {fork["temperament"]: fork for fork in forks}
    if fork_order:
        order = [str(label) for label in fork_order]
        if sorted(order) != sorted(by_label):
            raise ValueError(f"fork_order must list exactly the accepted forks {sorted(by_label)}")
    else:
        order = sorted(by_label)
    ordered = [{key: by_label[label][key] for key in ("temperament", "memos", "propensities")}
               for label in order]
    try:
        validated = fork_rules.validate_fork_set(ordered, tree, packet)
    except fork_rules.ForkRejected as rejected:
        raise ValueError("fork set rejected: " + " | ".join(rejected.problems)) from None
    document = {"packet_sha256": packet_sha256, "forks": validated}
    text = json.dumps(document, indent=2, ensure_ascii=False)
    forks_path = packet_dir(packet_sha256) / "fork_sets" / f"{sha256_hex(text.encode('utf-8'))}.json"
    if not forks_path.exists():
        _atomic_write(forks_path, text)
    scenario_path = sim_input(packet["scenario"]["path"])
    packet_path = sim_input(f"{PACKETS_DIRNAME}/{packet_sha256}/packet.json")
    return scenario_path, forks_path, packet_path, {
        "packet": packet, "order": order,
        "fork_sha256": {label: by_label[label]["fork_sha256"] for label in order}}


def _file_hashes(folder: Path, pattern: str, limit: int = 60) -> dict[str, str]:
    if not folder.is_dir():
        return {}
    return {str(path.relative_to(folder)).replace("\\", "/"): sha256_hex(path.read_bytes())
            for path in sorted(folder.glob(pattern))[:limit] if path.is_file()}


def run_manifest(run_key: str, result: dict, paths: list[Path], seed: int, rollouts: int,
                 packet_info: dict | None) -> dict:
    """What a reader needs to reproduce or audit the run (ZT review carry-over)."""
    root = project() / "market_actor_sim"
    receipt, receipt_sha = audit_receipt_digest()
    signature = lake_signature()
    vt_home = Path.home() / ".vibe-trading"
    manifest = {
        "schema": RUN_MANIFEST_SCHEMA, "run_key": run_key, "run_id": result["run_id"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "authority": result["authority"], "anchor_status": "elicited-only",
        "inputs": {name: {"path": str(path), "sha256": sha256_hex(path.read_bytes())}
                   for name, path in zip(("scenario", "forks", "evidence"), paths)},
        "seed": seed, "rollouts": rollouts,
        "lake_signature_at_run": signature,
        "lake_signature_sha256_at_run": sha256_hex(canonical_json(signature)),
        "audit_receipt_sha256_at_run": receipt_sha,
        "engine": {"run_governed_pilot.py": sha256_hex((root / "run_governed_pilot.py").read_bytes()),
                   **{f"sim/{k}": v for k, v in _file_hashes(root / "sim", "*.py").items()}},
        "extension": {"server.py": sha256_hex(Path(__file__).read_bytes()),
                      "fork_rules.py": sha256_hex(Path(__file__).with_name("fork_rules.py").read_bytes())},
        "ontology_version": os.environ.get("INVESTMENT_ONTOLOGY_VERSION") or "UNVERSIONED_NO_ONTOLOGY_RELEASE",
        "graph_asof_utc": None,
        "presets": _file_hashes(vt_home / "swarm" / "presets", "*.yaml"),
        "skills": _file_hashes(vt_home / "skills" / "user", "*/SKILL.md"),
        "model_config": {
            "LANGCHAIN_PROVIDER": os.environ.get("LANGCHAIN_PROVIDER"),
            "LANGCHAIN_MODEL_NAME": os.environ.get("LANGCHAIN_MODEL_NAME"),
            "note": "inherited Vibe-Trading process environment; per-agent model_name "
                    "overrides are recorded in the swarm run's run.json, not visible here",
        },
    }
    if packet_info:
        packet = packet_info["packet"]
        manifest.update({
            "packet_sha256": paths[2].parent.name,
            "graph_asof_utc": packet["run_asof_utc"],
            "run_asof_utc": packet["run_asof_utc"],
            "lake_signature_at_freeze": packet["lake_signature"],
            "lake_signature_sha256_at_freeze": packet["lake_signature_sha256"],
            "audit_receipt_sha256_at_freeze": packet["warehouse_audit"]["receipt_sha256"],
            "scenario_sha256_at_freeze": packet["scenario"]["sha256"],
            "fork_order": packet_info["order"],
            "fork_sha256": packet_info["fork_sha256"],
        })
    return manifest


@mcp.tool
def run_market_actor_sim(scenario: str = "", forks: str = "", evidence: str = "",
                         seed: int = 20260828, rollouts: int = 300000,
                         packet_sha256: str = "", fork_order: list[str] | None = None) -> dict:
    """Run the existing evidence-frozen simulator after a fresh warehouse audit.

    Either pass ``packet_sha256`` of a frozen packet whose role forks were
    accepted with submit_role_fork (ZT add-on), or the legacy three input paths
    relative to market_actor_sim. Results are uncalibrated research, labelled
    anchor_status elicited-only, and saved in isolated E: runtime state with a
    run manifest.
    """
    if not 1000 <= rollouts <= 300000:
        raise ValueError("rollouts must be 1000..300000")
    packet_info = None
    if packet_sha256:
        if scenario or forks or evidence:
            raise ValueError("pass either packet_sha256 or scenario/forks/evidence paths, not both")
        *paths, packet_info = packet_inputs(packet_sha256, fork_order)
    else:
        if fork_order:
            raise ValueError("fork_order applies to packet runs only")
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
    manifest = run_manifest(run_key, result, paths, seed, rollouts, packet_info)
    _atomic_write(out / "run_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return {"run_key": run_key, "run_id": result["run_id"],
            "authority": result["authority"], "anchor_status": "elicited-only",
            "validation": result["validation"],
            "ensemble_mc": result["ensemble_mc"], "mc_wilson_95": result["mc_wilson_95"],
            "model_form_range": result.get("model_form_range"),
            "expected_reward": result.get("expected_reward"),
            "limitations": result["limitations"],
            "packet_sha256": packet_sha256 or None,
            "result_path": str(out / "pilot_result.json"),
            "manifest_path": str(out / "run_manifest.json")}


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