"""zt-fidelity through Vibe-Trading's own plugin, connection, trading and portfolio code.

The connector is validated and installed with ``src.trading.plugin_scaffold``
into a throwaway runtime root, reached through ``src.trading.service`` exactly
as the Web UI and CLI reach it, and aggregated by ``PortfolioService.refresh``
with a fixed FX rate.  Synthetic data only; no network, no keyring.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from zt_fidelity_fixtures import *  # noqa: F403  (pytest fixtures)
from zt_fidelity_testkit import CONNECTOR_DIR, RAW_ACCOUNTS, write_export

PROFILE = "zt-fidelity-live-readonly"


class _NoKeyring:
    """Keyring stand-in: the manifest declares no credential, so nothing is ever stored."""

    def get_password(self, service_name, username):
        return None

    def set_password(self, service_name, username, password):  # pragma: no cover - must not happen
        raise AssertionError("zt-fidelity must never store a credential")

    def delete_password(self, service_name, username):
        return None


@pytest.fixture()
def vt_runtime(tmp_path, monkeypatch, broker_raw):
    root = tmp_path / "vt-home"
    root.mkdir()
    monkeypatch.setenv("VIBE_TRADING_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("ZT_FIDELITY_BROKER_RAW", str(broker_raw))
    monkeypatch.setattr("src.trading.local_plugins.get_runtime_root", lambda: root)
    monkeypatch.setattr("src.trading.connections.get_runtime_root", lambda: root)
    monkeypatch.setattr("src.trading.credentials.CredentialStore.backend",
                        property(lambda self: _NoKeyring()))
    from src.trading.local_plugins import clear_plugin_cache
    clear_plugin_cache()
    yield root
    clear_plugin_cache()


def install(root: Path) -> Path:
    from src.trading.plugin_scaffold import install_connector, validate_connector
    plugin = validate_connector(CONNECTOR_DIR)
    assert plugin.profile.id == PROFILE
    assert plugin.profile.readonly is True
    assert set(plugin.profile.capabilities) == {"account.read", "positions.read"}
    assert plugin.credential_fields == ()
    target = install_connector(CONNECTOR_DIR)
    assert target == root / "connectors" / "zt-fidelity"
    return target


def test_manifest_is_a_valid_vt_read_only_plugin(vt_runtime):
    from src.trading.connections import readonly_profile_catalog
    from src.trading.local_plugins import discover_plugins, load_adapter
    install(vt_runtime)
    plugins, errors = discover_plugins()
    assert errors == []
    assert [p.profile.id for p in plugins] == [PROFILE]
    module = load_adapter(plugins[0])
    for name in ("check_status", "get_account_snapshot", "get_positions"):
        assert callable(getattr(module, name))
    for forbidden in ("place_order", "cancel_order", "submit_order", "transfer"):
        assert not hasattr(module, forbidden)
    row = next(r for r in readonly_profile_catalog() if r.get("id") == PROFILE)
    assert (row["local_plugin"], row["credential_fields"], row["connector"]) == (True, [], "zt-fidelity")


def test_trading_service_reads_through_the_single_connection(vt_runtime, broker_raw):
    from src.trading.connections import ConnectionStore
    from src.trading.service import check_connection, get_account, get_positions
    install(vt_runtime)
    write_export(broker_raw, "2026-09-26_portfolio_refresh",
                 exported_at=datetime.now(timezone.utc) - timedelta(hours=1))
    ConnectionStore().create("zt-fidelity-live", PROFILE, "Fidelity")
    status = check_connection(PROFILE)
    assert (status["status"], status["configured"], status["transport"]) == ("ok", True, "local_plugin")
    account = get_account(PROFILE)
    positions = get_positions(PROFILE)
    assert account["status"] == positions["status"] == "ok"
    assert account["account"]["portfolio_value"] == 21625.9
    assert {p["symbol"] for p in positions["positions"]} == {
        "NVDA", "AAPL", "-NVDA261218C200", "912797XX1", "FXAIX"}
    text = json.dumps([status, account, positions], default=str)
    assert not any(raw in text for raw in RAW_ACCOUNTS)


def _portfolio(root: Path, tmp_path: Path):
    from src.portfolio.config import PortfolioSettingsStore
    from src.portfolio.service import PortfolioService
    from src.portfolio.store import PortfolioStore
    from src.trading.connections import ConnectionStore
    ConnectionStore().create("zt-fidelity-live", PROFILE, "Fidelity")
    settings = PortfolioSettingsStore(root / "portfolio.json")
    settings.save({"display_currency": "USD",
                   "sources": [{"connection_id": "zt-fidelity-live", "label": "Fidelity", "order": 0}]})
    return PortfolioService(
        PortfolioStore(tmp_path / "portfolio.sqlite3"), settings_store=settings,
        fx_fetcher=lambda: (Decimal("7.2"), Decimal("7.8"), "2026-09-26T00:00:00+00:00"))


def test_portfolio_refresh_values_the_export_exactly(vt_runtime, broker_raw, tmp_path):
    install(vt_runtime)
    write_export(broker_raw, "2026-09-26_portfolio_refresh",
                 exported_at=datetime.now(timezone.utc) - timedelta(hours=1))
    snapshot = _portfolio(vt_runtime, tmp_path).refresh()
    assert snapshot["complete"] is True
    (account,) = snapshot["accounts"]
    assert (account["status"], account["broker"], account["label"]) == ("ok", "zt-fidelity", "Fidelity")
    assert account["total_usd"] == 21625.9
    assert account["cash_usd"] == 1450.0
    assert account["priced_value_usd"] == pytest.approx(20175.9)
    assert account["unpriced_or_other_usd"] == pytest.approx(0.0, abs=1e-6)
    assert account["position_count"] == account["priced_position_count"] == 5
    positions = {p["symbol"]: p for p in snapshot["positions"]}
    assert positions["NVDA"]["market_value_usd"] == 5445.0
    assert positions["NVDA"]["unrealized_pnl_usd"] == 2445.0
    assert positions["-NVDA261218C200"]["market_value_usd"] == 900.0
    assert positions["-NVDA261218C200"]["asset_type"] == "option"
    assert positions["912797XX1"]["market_value_usd"] == 9850.0
    assert positions["AAPL"]["asset_type"] == "stock"
    keys = [f"{p['source_id']}-{p['symbol']}" for p in snapshot["positions"]]
    assert len(keys) == len(set(keys))            # the Web UI keys holdings rows by source + symbol
    text = json.dumps(snapshot, default=str)
    assert not any(raw in text for raw in RAW_ACCOUNTS)


def test_stale_export_is_a_failed_source_with_the_reason(vt_runtime, broker_raw, tmp_path):
    install(vt_runtime)
    write_export(broker_raw, "2026-09-26_refresh",
                 exported_at=datetime.now(timezone.utc) - timedelta(days=5))
    snapshot = _portfolio(vt_runtime, tmp_path).refresh()
    assert snapshot["complete"] is False
    (account,) = snapshot["accounts"]
    assert account["status"] == "error"
    assert account["error"].startswith("Fidelity export is STALE")
    assert account["total_usd"] is None and snapshot["positions"] == []
    assert any("failed to refresh" in w for w in snapshot["warnings"])


def test_vt_cli_prints_the_connector_without_account_numbers(vt_runtime, broker_raw, capsys):
    from cli import _legacy as cli
    from src.trading.connections import ConnectionStore
    install(vt_runtime)
    write_export(broker_raw, "2026-09-26_portfolio_refresh",
                 exported_at=datetime.now(timezone.utc) - timedelta(hours=1))
    ConnectionStore().create("zt-fidelity-live", PROFILE, "Fidelity")
    assert cli.cmd_connector_check(PROFILE) == cli.EXIT_SUCCESS
    assert cli.cmd_connector_account(PROFILE) == cli.EXIT_SUCCESS
    assert cli.cmd_connector_positions(PROFILE) == cli.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "ready" in out and "portfolio_value" in out and "NVDA" in out and "****1111" in out
    assert "as_of_source" in out
    assert not any(raw in out for raw in RAW_ACCOUNTS)
