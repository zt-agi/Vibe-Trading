"""pytest fixtures for the zt_fidelity tests (imported by each test module).

They live here rather than in a conftest.py so that this suite can run in one
pytest session with other extension suites that import their own conftest.

Every test works in a pytest temporary folder: a synthetic broker/raw tree
(fixtures/Portfolio_Positions_Sep-26-2026.csv holds invented accounts and
values, never real data) and, for the Vibe-Trading integration tests, a
throwaway VT runtime root.  The network is blocked.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zt_fidelity_testkit import CONNECTOR_DIR, load_adapter_module

__all__ = ["_temp_on_e_drive", "no_network", "_clear_env", "adapter", "broker_raw",
           "plugin_config", "now"]

@pytest.fixture(scope="session", autouse=True)
def _temp_on_e_drive(request):
    """ZT 2026-09-28: nothing is written on C: or D: -- on Windows the pytest
    temporary folder must be on E: (set TEMP/TMP or pass --basetemp)."""
    base = Path(str(request.config.option.basetemp or tempfile.gettempdir())).resolve()
    if os.name == "nt" and base.drive.upper() != "E:":
        pytest.exit(f"Point TEMP and TMP at an E: folder (or pass --basetemp on E:); got {base}",
                    returncode=4)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse every non-loopback connection (asyncio's self-pipe on Windows uses loopback)."""
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def loopback(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return not isinstance(address, tuple) or str(host) in ("127.0.0.1", "::1", "localhost")

    def connect(self, address):
        if not loopback(address):
            raise OSError("network access is disabled in these tests")
        return real_connect(self, address)

    def create_connection(address, *args, **kwargs):
        if not loopback(address):
            raise OSError("network access is disabled in these tests")
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in ("ZT_FIDELITY_BROKER_RAW", "INVESTMENT_AI_PROJECT_ROOT"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def adapter():
    return load_adapter_module()


@pytest.fixture()
def broker_raw(tmp_path):
    root = tmp_path / "project" / "broker" / "raw"
    root.mkdir(parents=True)
    return root


@pytest.fixture()
def plugin_config(tmp_path, broker_raw):
    """A plugin directory whose settings.json points at the synthetic tree."""
    plugin_dir = tmp_path / "plugin"
    shutil.copytree(CONNECTOR_DIR, plugin_dir)
    (plugin_dir / "settings.json").write_text(json.dumps({"broker_raw_dir": str(broker_raw)}),
                                              encoding="utf-8")
    return {"plugin_directory": str(plugin_dir)}


@pytest.fixture()
def now():
    return datetime.now(timezone.utc)
