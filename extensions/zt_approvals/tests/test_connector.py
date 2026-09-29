"""zt-paper registered the VT-native way: a local connector plugin (read side)
installed with VT's own validate/install, a connection without credentials,
VT's service reads, and orders to it held as proposals."""
import json
import shutil

import pytest

from conftest import EXTENSION

CONNECTOR = EXTENSION / "connector"


@pytest.fixture()
def installed(home):
    from src.trading.connections import ConnectionStore
    from src.trading.local_plugins import clear_plugin_cache
    from src.trading.plugin_scaffold import install_connector, validate_connector

    clear_plugin_cache()
    plugin = validate_connector(CONNECTOR)
    target = install_connector(CONNECTOR)
    ConnectionStore().ensure("zt-paper", "zt-paper", "ZT Paper")
    yield plugin, target
    clear_plugin_cache()


def test_manifest_is_a_valid_read_only_vt_connector(installed, home):
    plugin, target = installed
    profile = plugin.profile
    assert (profile.id, profile.connector, profile.environment, profile.transport) == (
        "zt-paper", "zt-paper", "paper", "local_plugin")
    assert profile.readonly is True and "orders.place" not in profile.capabilities
    assert plugin.credential_fields == ()
    assert target == home / "connectors" / "zt-paper"
    from src.trading.profiles import profile_by_id

    assert profile_by_id("zt-paper").label.startswith("ZT Paper")


def test_vt_reads_the_simulated_account(installed, engine):
    from src.trading import service

    engine.fill_orders([{"symbol": "AAPL", "side": "buy", "qty": 10}], proposal_id="op_" + "b" * 32)
    status = service.check_connection("zt-paper")
    assert status["status"] == "ok" and status["simulated"] is True
    account = service.get_account("zt-paper")
    assert account["account"]["account_id"] == "ZT-PAPER" and account["account"]["equity"] == 100_000.0
    positions = service.get_positions("zt-paper")["positions"]
    assert positions == [{"symbol": "AAPL", "quantity": 10.0, "average_cost": 200.0, "market_price": 200.0,
                          "market_value": 2_000.0, "currency": "USD", "asset_type": "equity",
                          "price_date": "2026-09-28"}]
    quote = service.get_quote("aapl", "zt-paper")
    assert quote["quote"]["close"] == 200.0 and quote["quote"]["basis"] == "latest completed-session close"
    assert service.get_open_orders("zt-paper")["open_orders"] == []


def test_vt_order_tool_to_zt_paper_becomes_a_proposal(installed, core, engine):
    from src.tools.trading_connector_tool import TradingPlaceOrderTool

    out = json.loads(TradingPlaceOrderTool().execute(symbol="MSFT", side="buy", quantity=3, connection="zt-paper"))

    assert out["status"] == "pending_approval" and out["connector"] == "zt-paper"
    assert not engine.account_path().exists()
    proposal = core.load_proposal(out["proposal_id"])
    assert proposal["broker"]["key"] == "zt-paper" and proposal["route"]["kind"] == "zt_paper"
    done = core.approve_proposal(proposal["id"], confirm_hash=proposal["content_hash"])
    assert done["status"] == "FILLED"
    assert engine.snapshot()["cash"] == 98_800.0


def test_installed_copy_delegates_to_the_checkout_engine(installed):
    _, target = installed
    shutil.rmtree(target / "__pycache__", ignore_errors=True)
    source = (CONNECTOR / "adapter.py").read_text(encoding="utf-8")
    assert (target / "adapter.py").read_text(encoding="utf-8") == source
    assert "zt_paper_engine" in source and "place_order" not in source.split('"""', 2)[2]
