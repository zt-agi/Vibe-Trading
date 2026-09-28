"""Shared fixtures for the ZT dashboards extension tests.

The tests never read G: or the E: runtime: the fixture project is copied into
a temporary work/Investment-AI-Drive-Research folder, and VT's runtime root and
home are pointed at a throwaway sandbox before api_server can be imported, so
no real API key, .env or runtime state is touched. On Windows the temporary
folder must be on E: (ZT 2026-09-28: nothing is written on C: or D:).

fixtures/Investment-AI-Drive-Research is a trimmed copy of the 2026-09-27
project files. Deliberate differences: every monetary value, concentration
percentage and position count in exports/2026-09-27/portfolio_context.json and
alerts.csv is synthetic (no real portfolio figure is committed, and the
redaction tests assert these synthetic values never appear in a response);
alpha_signal_monitor_v01/data/signal_state.csv is synthetic (three rows in the
layout monitor.py writes, because the copy lacked the file); INGEST_LEDGER.csv
keeps its newest 40 rows, INVENTORY.json the first three items of its long
lists, and pitdb_daily.log the last three daily runs. Everything else is
byte-identical to the project copy.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EXTENSION = HERE.parent
FIXTURE = HERE / "fixtures" / "Investment-AI-Drive-Research"
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)


def _temp_base() -> Path:
    base = Path(tempfile.gettempdir()).resolve()
    if os.name == "nt" and base.drive.upper() != "E:":
        raise RuntimeError(
            "Point TEMP and TMP at an E: folder before running these tests "
            f"(ZT 2026-09-28: nothing is written on C: or D:); got {base}")
    return base


_SANDBOX = Path(tempfile.mkdtemp(prefix="zt-dashboards-tests-", dir=_temp_base()))
os.environ["VIBE_TRADING_HOME"] = str(_SANDBOX / "vibe-trading-home")
os.environ["HOME"] = str(_SANDBOX)
os.environ["USERPROFILE"] = str(_SANDBOX)
for _key in ("API_AUTH_KEY", "VIBE_TRADING_API_KEY", "INVESTMENT_AI_PROJECT_ROOT",
             "ZT_WORLD_MODEL_PATH"):
    os.environ.pop(_key, None)


def load_extension_module(name: str, filename: str):
    """Load an extension file by path under a unique module name."""
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, EXTENSION / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


@pytest.fixture()
def core():
    return load_extension_module("zt_dashboards_core", "zt_core.py")


@pytest.fixture()
def project(tmp_path, monkeypatch) -> Path:
    """A private copy of the fixture project, named like the canonical folder."""
    root = tmp_path / "work" / "Investment-AI-Drive-Research"
    shutil.copytree(FIXTURE, root)
    monkeypatch.setenv("INVESTMENT_AI_PROJECT_ROOT", str(root))
    monkeypatch.delenv("ZT_WORLD_MODEL_PATH", raising=False)
    return root.resolve()
