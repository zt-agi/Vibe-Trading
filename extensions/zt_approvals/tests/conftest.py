"""Shared fixtures for the zt_approvals extension tests.

VT's runtime root and home point at a throwaway sandbox before any VT module
is imported, so no real API key, .env, proposal, ledger or paper account is
touched. Prices come from a fixture table (no network). On Windows the
temporary folder must be on E: (ZT 2026-09-28: nothing is written on C: or D:).
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EXTENSION = HERE.parent
REPO = EXTENSION.parents[1]
AGENT = REPO / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))


def _temp_base() -> Path:
    base = Path(tempfile.gettempdir()).resolve()
    if os.name == "nt" and base.drive.upper() != "E:":
        raise RuntimeError(
            "Point TEMP and TMP at an E: folder before running these tests "
            f"(ZT 2026-09-28: nothing is written on C: or D:); got {base}")
    return base


_SANDBOX = Path(tempfile.mkdtemp(prefix="zt-approvals-tests-", dir=_temp_base()))
os.environ["VIBE_TRADING_HOME"] = str(_SANDBOX / "vibe-trading-home")
os.environ["HOME"] = str(_SANDBOX)
os.environ["USERPROFILE"] = str(_SANDBOX)
for _key in ("API_AUTH_KEY", "VIBE_TRADING_API_KEY", "VIBE_ORDER_APPROVAL", "VIBE_ORDER_APPROVAL_TTL_MIN",
             "ZT_PAPER_PRICE_SOURCE"):
    os.environ.pop(_key, None)

PRICES: dict = {}
BASE_PRICES = {"AAPL": 200.0, "MSFT": 400.0, "NVDA": 100.0, "SPY": 500.0}


def fixture_close(symbol):
    from src.live import order_proposals as core

    ticker = str(symbol).upper().removesuffix(".US")
    price = PRICES.get(ticker)
    return core.RefPrice(ticker, price, "2026-09-28", "test:fixture") if price is not None else None


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
def home(tmp_path, monkeypatch):
    """A private VT runtime root per test, with fixture prices."""
    from src.live import order_proposals as core

    root = tmp_path / "vibe-trading-home"
    root.mkdir()
    monkeypatch.setenv("VIBE_TRADING_HOME", str(root))
    for key in ("VIBE_ORDER_APPROVAL", "VIBE_ORDER_APPROVAL_TTL_MIN", "ZT_PAPER_PRICE_SOURCE"):
        monkeypatch.delenv(key, raising=False)
    PRICES.clear()
    PRICES.update(BASE_PRICES)
    monkeypatch.setattr(core.zt_paper_engine(), "reference_close", fixture_close)
    monkeypatch.setattr(core, "vt_loader_close", lambda symbol, **_: fixture_close(symbol))
    return root


@pytest.fixture()
def core(home):
    from src.live import order_proposals

    return order_proposals


@pytest.fixture()
def engine(core):
    return core.zt_paper_engine()
