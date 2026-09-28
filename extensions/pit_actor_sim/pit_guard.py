"""Shared fail-closed guards for the pitdb add-ons.

Two consumers enforce one contract with these helpers:

* ``server.py`` -- the pit-actor-sim MCP server (thin wrappers keep its
  ``project()`` / ``runtime()`` / ``lake_signature()`` names and messages);
* ``agent/backtest/loaders/pitdb_loader.py`` -- VT's ``source="pitdb"``
  backtest loader, which loads this file by path.

Everything here takes explicit inputs (project root, runtime root, a
signature callable), so neither consumer depends on the other's environment
handling and the receipt logic is testable without either.  The storage rule
(ZT 2026-09-28: everything on E:) and the receipt formats are unchanged from
the extension; ``checks_passed`` is new in audit receipts and lets a consumer
require a specific audit check (the loader's formation mode requires A11).
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

REQUIRED_DRIVE = "E:"
DRIVE_RULE = "ZT 2026-09-28: everything on E:"
DEFAULT_INDEX = r"E:\pitdb\pit.duckdb"

INDEX_RECEIPT = "pit_index_receipt.json"
AUDIT_RECEIPT = "pit_audit_receipt.json"
#: An audit receipt older than this is expired (one hour, as before).
AUDIT_MAX_AGE_SECONDS = 3600

#: Compact lake tables the MCP read tools depend on.
READ_TABLES = ("dim_source", "dim_security", "dim_security_alias",
               "dim_series", "fact_price_eod", "fact_observation")

PROJECT_MARKERS = ("AGENTS.md", "implementation/pit_warehouse/pitdb",
                   "market_actor_sim/run_governed_pilot.py")

_AUDIT_LINE = re.compile(r"^\s*\[(PASS|FAIL)\]\s+(A\d+)\b", re.M)


def windows_drive(path) -> str:
    """Return the upper-case drive of a Windows path, ignoring a \\\\?\\ prefix."""
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
        raise ValueError(
            f"{what} must be on {REQUIRED_DRIVE} ({DRIVE_RULE}); C:, D: and the "
            f"system drive {system} are refused; got {path}")
    return path


def validate_project_root(raw: str | None) -> Path:
    """The canonical Investment-AI-Drive-Research folder, or raise."""
    if not raw:
        raise RuntimeError("INVESTMENT_AI_PROJECT_ROOT is required")
    root = Path(raw).resolve(strict=True)
    if root.name != "Investment-AI-Drive-Research" or root.parent.name != "work":
        raise ValueError("Use the canonical work project folder")
    for part in PROJECT_MARKERS:
        if not (root / part).exists():
            raise FileNotFoundError(part)
    return root


def validate_runtime_root(raw: str | None, project: Callable[[], Path]) -> Path:
    """The isolated E: runtime folder, or raise.

    ``project`` is called only after ``raw`` is known to be set, so a missing
    VIBE_TRADING_HOME is reported as such rather than as a project error.
    """
    if not raw:
        raise RuntimeError("VIBE_TRADING_HOME is required")
    path = require_e_drive(Path(raw).resolve(), "Runtime (VIBE_TRADING_HOME)")
    root = project()
    if root == path or root in path.parents:
        raise ValueError("Runtime may not be stored in the shared project")
    return path


def pitdb_config(project_root: Path):
    """Import ``pitdb.config`` from the project's own warehouse package."""
    warehouse = str(Path(project_root) / "implementation" / "pit_warehouse")
    if warehouse not in sys.path:
        sys.path.insert(0, warehouse)
    from pitdb import config as C
    return C


def lake_signature(project_root: Path, tables: Iterable[str] | None = None) -> dict:
    """Cheap freshness token for compact tables in the canonical Parquet lake."""
    C = pitdb_config(project_root)
    if C.DB_PATH is None:
        raise RuntimeError(f"Set PITDB_INDEX={DEFAULT_INDEX} for the disposable E: index")
    require_e_drive(C.DB_PATH, "PIT index (pitdb DB_PATH)")
    files = {}
    for table in (tables or C.PERSISTED_TABLES):
        path = C.LAKE_ROOT / table / "data.parquet"
        try:
            stat = path.stat()
            files[table] = [stat.st_size, stat.st_mtime_ns]
        except FileNotFoundError:
            files[table] = None
    return {"lake_root": str(C.LAKE_ROOT.resolve()), "files": files}


def signature_digest(signature: dict) -> str:
    """Stable ``sha256:`` digest of a lake signature, for manifests and run cards."""
    blob = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def require_fresh_index(project_root: Path, runtime_root: Path, *,
                        tables: Sequence[str] | None = READ_TABLES,
                        signature: Callable[[Sequence[str] | None], dict] | None = None,
                        ) -> dict:
    """The index receipt, if the E: index still mirrors the lake for ``tables``.

    ``tables=None`` compares every persisted table.  ``signature`` computes the
    current lake signature for a table list (default :func:`lake_signature`).
    """
    C = pitdb_config(project_root)
    receipt_path = Path(runtime_root) / INDEX_RECEIPT
    if not C.DB_PATH or not C.DB_PATH.exists() or not receipt_path.exists():
        raise RuntimeError("PIT index unavailable; run server.py --refresh-index")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    recorded = receipt.get("signature", {})
    compute = signature or (lambda wanted: lake_signature(project_root, wanted))
    current = compute(tables)
    if current["lake_root"] != recorded.get("lake_root") or any(
        recorded.get("files", {}).get(table) != value
        for table, value in current["files"].items()
    ):
        raise RuntimeError("PIT index is stale; run server.py --refresh-index")
    return receipt


def require_fresh_audit(runtime_root: Path, signature: Callable[[], dict], *,
                        max_age_seconds: float = AUDIT_MAX_AGE_SECONDS,
                        required_checks: Sequence[str] = (),
                        now: datetime | None = None) -> dict:
    """The audit receipt, if it PASSed, is under an hour old and matches the lake.

    ``required_checks`` names audit checks (e.g. ``"A11"``) that must appear
    in the receipt's ``checks_passed``; a receipt written before that check
    existed is refused rather than trusted.
    """
    path = Path(runtime_root) / AUDIT_RECEIPT
    if not path.is_file():
        raise RuntimeError("PIT audit receipt missing; run server.py --refresh-audit")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    audited = datetime.fromisoformat(receipt["audited_at_utc"])
    age = (now or datetime.now(timezone.utc)) - audited
    if receipt.get("status") != "PASS" or age.total_seconds() < 0 \
            or age.total_seconds() > max_age_seconds:
        raise RuntimeError("PIT audit receipt expired; run server.py --refresh-audit")
    passed = set(receipt.get("checks_passed") or ())
    missing = [check for check in required_checks if check not in passed]
    if missing:
        raise RuntimeError(
            f"PIT audit receipt does not record {', '.join(missing)} as passed; "
            "update pitdb and run server.py --refresh-audit")
    if receipt.get("signature") != signature():
        raise RuntimeError("PIT lake changed since audit; run server.py --refresh-audit")
    return receipt


def parse_audit_checks(stdout: str) -> dict[str, list[str]]:
    """Check ids the pitdb audit printed as ``[PASS]`` / ``[FAIL]``."""
    passed, failed = [], []
    for match in _AUDIT_LINE.finditer(stdout or ""):
        (passed if match.group(1) == "PASS" else failed).append(match.group(2))
    return {"passed": passed, "failed": failed}


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write ``payload`` beside ``path`` and swap it in, so readers never see half a file."""
    temporary = Path(path).with_name(Path(path).name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
