"""pytest fixtures for the zt_research tests (imported by each test module).

They live here rather than in a conftest.py so that this suite can run in one
pytest session with other extension suites that import their own conftest.

A synthetic project (ASM board and ledger, a stub pit_alpha_ranker package and
a three-security pitdb lake) is built in a pytest temporary folder; VT's runtime
root and hypotheses store point into the same folder.  The network is blocked.
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

from zt_research_testkit import build_project, load_server

__all__ = ["_temp_on_e_drive", "no_network", "project", "vt_home", "server"]

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


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = build_project(tmp_path)
    monkeypatch.setenv("INVESTMENT_AI_PROJECT_ROOT", str(root))
    monkeypatch.delenv("PITDB_LAKE", raising=False)
    monkeypatch.delenv("ZT_STUB_RANKER_FAIL", raising=False)
    return root


@pytest.fixture()
def vt_home(tmp_path, monkeypatch):
    home = tmp_path / "vt-home"
    home.mkdir()
    monkeypatch.setenv("VIBE_TRADING_HOME", str(home))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(home / "hypotheses.json"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    from src.config.accessor import reset_env_config
    reset_env_config()
    yield home
    reset_env_config()


@pytest.fixture()
def server():
    return load_server()
