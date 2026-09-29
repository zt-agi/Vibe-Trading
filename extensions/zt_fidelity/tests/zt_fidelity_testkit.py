"""Helpers shared by the zt_fidelity tests (imported by the fixtures module and the tests)."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONNECTOR_DIR = HERE.parent / "connector"
FIXTURE_CSV = HERE / "fixtures" / "Portfolio_Positions_Sep-26-2026.csv"
AGENT_DIR = HERE.parents[2] / "agent"
RAW_ACCOUNTS = ("X00001111", "Y00002222")   # the synthetic fixture's invented account ids

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def load_adapter_module(name: str = "zt_fidelity_adapter_under_test"):
    spec = importlib.util.spec_from_file_location(name, CONNECTOR_DIR / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_export(raw_dir: Path, folder: str, *, csv_bytes: bytes | None = None,
                 name: str = "Portfolio_Positions_Sep-26-2026.csv",
                 exported_at: datetime | None = None, receipt_at: datetime | None = None,
                 context_stamp: str = "20260926T211500Z") -> Path:
    """One export folder: the CSV plus, optionally, its context JSON and receipt."""
    target = raw_dir / folder
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(csv_bytes if csv_bytes is not None else FIXTURE_CSV.read_bytes())
    if exported_at is not None:
        (target / f"portfolio_export_context_{context_stamp}.json").write_text(json.dumps({
            "schema": "synthetic-test-context/1",
            "export": {"exported_at": iso(exported_at), "view": "Positions Overview",
                       "files": [name]},
        }), encoding="utf-8")
    if receipt_at is not None:
        with (target / "csv_download_receipts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"file": name, "downloaded_at": iso(receipt_at),
                                     "bytes": path.stat().st_size}) + "\n")
    return path


def tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}
