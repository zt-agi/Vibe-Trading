"""ZT add-on: VIBE_ORDER_APPROVAL=required holds every VT order path.

Each test drives VT's own entry point with a fake broker and checks that no
order reaches the broker before a human approves, that approval replays the
exact call once through VT's own gate, and that approval off leaves VT's
behavior unchanged: ``service.place_order`` (paper and live direct-SDK),
``LiveOrderGuardTool`` (remote MCP live broker), the runner's halt sweep,
``wrap_live_broker_tools``, eToro position actions and the agent's
``trading_place_order`` tool.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import src.live.paths as paths
from src.live import order_guard
from src.live import order_proposals as core
from src.live.halt import clear_halt, trip_halt
from src.live.runtime.flatten import flatten_and_cancel
from src.trading import service
from src.trading.types import TradingProfile
from tests import robinhood_mcp_helpers as rh
from tests.test_mandate_enforcement import _mandate, _spec, _write_mandate

pytestmark = pytest.mark.unit

ACCOUNT = "5QR12345"
PAPER = TradingProfile(id="fake-paper-sdk", connector="fakebroker", label="Fake paper", environment="paper",
                       transport="broker_sdk", capabilities=("account.read", "positions.read", "orders.place"),
                       readonly=False, config={})
LIVE = replace(PAPER, id="fake-live-sdk", environment="live", label="Fake live")


def _price(symbol: str):
    prices = {"AAPL": 200.0, "MSFT": 400.0}
    ticker = str(symbol).upper().removesuffix(".US")
    return core.RefPrice(ticker, prices[ticker], "2026-09-28", "test:fixture") if ticker in prices else None


class FakeSdk:
    """A direct-SDK connector module that records every order."""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []

    def build_config(self, profile_config, overrides):
        return {"cfg": True}

    def place_order(self, config, **kwargs):
        self.orders.append(kwargs)
        return {"status": "ok", "order_id": f"fake-{len(self.orders)}", "order_status": "accepted"}

    def get_positions(self, config):
        return {"status": "ok", "positions": [{"symbol": "AAPL", "quantity": 5, "market_price": 200.0}]}

    def get_account_snapshot(self, config):
        return {"status": "ok", "account": {"equity": 50_000.0, "cash": 49_000.0}}

    def close_position(self, config, **kwargs):
        self.closes.append(kwargs)
        return {"status": "ok"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "get_runtime_root", lambda: tmp_path)
    monkeypatch.delenv(core.APPROVAL_ENV, raising=False)
    monkeypatch.delenv(core.TTL_ENV, raising=False)
    monkeypatch.setattr(core, "vt_loader_close", lambda symbol, **_: _price(symbol))
    monkeypatch.setattr(core.zt_paper_engine(), "reference_close", _price)
    return tmp_path


@pytest.fixture
def sdk(home, monkeypatch: pytest.MonkeyPatch) -> FakeSdk:
    fake = FakeSdk()
    profiles = {PAPER.id: PAPER, LIVE.id: LIVE}
    monkeypatch.setattr(service, "profile_by_id", lambda profile_id=None: profiles[profile_id])
    monkeypatch.setattr(service, "_sdk_module", lambda connector: fake)
    return fake


def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(core.APPROVAL_ENV, "required")


def _approve(proposal_id: str) -> dict:
    proposal = core.load_proposal(proposal_id)
    return core.approve_proposal(proposal_id, confirm_hash=proposal["content_hash"])


# --------------------------------------------------------------------------- #
# service.place_order: paper direct-SDK                                        #
# --------------------------------------------------------------------------- #


def test_approval_off_leaves_vt_paper_orders_unchanged(sdk) -> None:
    out = service.place_order("AAPL", PAPER.id, side="buy", quantity=3)
    assert out["status"] == "ok" and len(sdk.orders) == 1
    assert not core.proposals_dir().exists()


def test_required_holds_the_order_until_approved_then_replays_it_once(sdk, monkeypatch) -> None:
    _required(monkeypatch)

    held = service.place_order("aapl", PAPER.id, side="buy", quantity=3, order_type="limit", limit_price=201.0,
                               time_in_force="gtc", session_id="s-1")

    assert held["status"] == "pending_approval" and held["profile_id"] == PAPER.id
    assert sdk.orders == []  # no broker call before approval
    proposal = core.load_proposal(held["proposal_id"])
    assert proposal["route"]["kind"] == "vt_profile" and proposal["route"]["session_id"] == "s-1"
    assert proposal["decision_record"]["current_positions"] == {"AAPL": 5.0}
    assert proposal["validation"]["ok"] is True

    done = _approve(held["proposal_id"])

    assert done["status"] == core.SUBMITTED  # the broker accepted; fills are the broker's
    assert sdk.orders == [{"symbol": "AAPL", "side": "buy", "quantity": 3.0, "notional": None,
                           "order_type": "limit", "limit_price": 201.0, "time_in_force": "gtc"}]
    with pytest.raises(core.ProposalError):
        _approve(held["proposal_id"])
    assert len(sdk.orders) == 1


def test_a_changed_order_is_a_new_proposal_not_the_approved_one(sdk, monkeypatch) -> None:
    _required(monkeypatch)
    first = service.place_order("AAPL", PAPER.id, side="buy", quantity=3)
    second = service.place_order("AAPL", PAPER.id, side="buy", quantity=4)
    assert first["proposal_id"] != second["proposal_id"]
    assert sdk.orders == []
    _approve(first["proposal_id"])
    assert [o["quantity"] for o in sdk.orders] == [3.0]


def test_a_failed_proposal_record_never_falls_through_to_the_broker(sdk, monkeypatch) -> None:
    _required(monkeypatch)
    monkeypatch.setattr(core, "write_json_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = service.place_order("AAPL", PAPER.id, side="buy", quantity=1)
    assert out["status"] == "error" and "nothing was sent" in out["error"]
    assert sdk.orders == []


def test_agent_tool_returns_the_pending_proposal(sdk, monkeypatch) -> None:
    from src.tools.trading_connector_tool import TradingPlaceOrderTool

    _required(monkeypatch)
    out = json.loads(TradingPlaceOrderTool().execute(symbol="AAPL", side="buy", quantity=2, connection=PAPER.id))
    assert out["status"] == "pending_approval" and out["approval_url"].startswith("/zt/approvals?proposal=op_")
    assert "Do not resubmit" in out["message"]
    assert sdk.orders == []


# --------------------------------------------------------------------------- #
# service.place_order: live direct-SDK and the kill switch                     #
# --------------------------------------------------------------------------- #


def test_halt_denies_live_broker_approval(sdk, monkeypatch) -> None:
    _required(monkeypatch)
    held = service.place_order("AAPL", LIVE.id, side="buy", quantity=1)
    assert held["status"] == "pending_approval" and sdk.orders == []
    checks = {c["name"]: c["status"] for c in core.load_proposal(held["proposal_id"])["validation"]["checks"]}
    assert checks["mandate"] == "FAIL"  # no mandate on file for this live broker

    trip_halt(by="file", reason="test halt")
    with pytest.raises(core.ProposalError) as err:
        _approve(held["proposal_id"])
    assert (err.value.status_code, err.value.code) == (423, "halted")
    assert sdk.orders == []
    assert core.load_proposal(held["proposal_id"])["status"] == core.PENDING
    clear_halt()


def test_live_sdk_approval_still_passes_through_vts_mandate_gate(sdk, monkeypatch) -> None:
    from src.live import sdk_order_gate as gate

    _required(monkeypatch)
    held = service.place_order("AAPL", LIVE.id, side="buy", quantity=1)
    calls: list[dict] = []
    monkeypatch.setattr(core, "revalidate", lambda proposal, **_: {"ok": True, "checks": [], "checked_utc": ""})
    monkeypatch.setattr(gate, "execute_live_order", lambda **kwargs: calls.append(kwargs) or {"status": "blocked",
                                                                                              "reason": "no mandate"})
    done = _approve(held["proposal_id"])
    assert len(calls) == 1 and calls[0]["place_kwargs"]["quantity"] == 1.0
    assert done["status"] == core.FAILED
    assert done["submission"]["results"][0]["error"] == "no mandate"
    assert sdk.orders == []


# --------------------------------------------------------------------------- #
# eToro position actions (_route_sdk_write)                                    #
# --------------------------------------------------------------------------- #


def test_position_actions_are_held_too(sdk, monkeypatch) -> None:
    _required(monkeypatch)
    etoro = replace(PAPER, id="etoro-paper-fake", connector="etoro")
    monkeypatch.setattr(service, "profile_by_id", lambda profile_id=None: etoro)
    out = service.close_position(99, etoro.id, units_to_close=2.0)
    assert out["status"] == "pending_approval" and sdk.closes == []
    proposal = core.load_proposal(out["proposal_id"])
    assert proposal["route"]["kind"] == "vt_service" and proposal["route"]["remote_tool"] == "close_position"
    checks = {c["name"]: c["status"] for c in proposal["validation"]["checks"]}
    assert checks["reference_prices"] == "FAIL"  # position-level actions cannot be valued, so not approvable


def test_approval_off_leaves_position_actions_unchanged(sdk, monkeypatch) -> None:
    monkeypatch.setattr(service, "_close_etoro_position", lambda module, cfg, **kw: module.close_position(cfg, **kw))
    etoro = replace(PAPER, id="etoro-paper-fake", connector="etoro")
    monkeypatch.setattr(service, "profile_by_id", lambda profile_id=None: etoro)
    out = service.close_position(99, etoro.id, units_to_close=2.0)
    assert out["status"] == "ok" and len(sdk.closes) == 1


# --------------------------------------------------------------------------- #
# Remote MCP live broker (LiveOrderGuardTool)                                   #
# --------------------------------------------------------------------------- #


class GateBroker:
    """Answers the gate's reads and records forwarded orders."""

    def __init__(self) -> None:
        self.server_name = "robinhood"
        self.order_calls: list[dict[str, Any]] = []

    def call_tool(self, remote_name: str, arguments: dict, *, local_name: str | None = None) -> dict:
        if remote_name == "get_equity_positions":
            return rh.positions([])
        if remote_name == "get_portfolio":
            return rh.portfolio()
        if remote_name == "get_equity_quotes":
            return {"status": "error", "error": "quote shape unmapped"}
        self.order_calls.append(dict(arguments))
        return {"status": "ok", "order_id": "rh_1", "state": "accepted"}


def _bound_mandate():
    mandate = _mandate()
    return replace(mandate, consent=replace(mandate.consent, account_ref=ACCOUNT))


def test_mcp_guard_holds_an_allowed_order_and_replays_it_once(home, monkeypatch) -> None:
    _required(monkeypatch)
    _write_mandate(home, _bound_mandate())
    broker = GateBroker()
    guard = order_guard.LiveOrderGuardTool(broker, _spec(), broker="robinhood", session_id="s1")

    out = json.loads(guard.execute(symbol="AAPL", side="buy", instrument_type="equity", notional_usd=100.0))

    assert out["status"] == "pending_approval" and broker.order_calls == []
    proposal = core.load_proposal(out["proposal_id"])
    assert proposal["route"]["kind"] == "mcp_guard"
    assert proposal["route"]["arguments"]["account_number"] == ACCOUNT
    assert proposal["account_scope"]["account"] == ACCOUNT
    audit = [json.loads(line) for line in (home / "live" / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["gate_decision"]["decision"] == "held_for_approval"

    monkeypatch.setattr(core, "mcp_adapter", lambda server_name: broker)
    done = _approve(out["proposal_id"])

    assert done["status"] == core.SUBMITTED
    assert len(broker.order_calls) == 1 and broker.order_calls[0]["account_number"] == ACCOUNT
    with pytest.raises(core.ProposalError):
        _approve(out["proposal_id"])
    assert len(broker.order_calls) == 1


def test_mcp_guard_approval_is_denied_while_halted(home, monkeypatch) -> None:
    _required(monkeypatch)
    _write_mandate(home, _bound_mandate())
    broker = GateBroker()
    guard = order_guard.LiveOrderGuardTool(broker, _spec(), broker="robinhood", session_id="s1")
    out = json.loads(guard.execute(symbol="AAPL", side="buy", instrument_type="equity", notional_usd=100.0))
    monkeypatch.setattr(core, "mcp_adapter", lambda server_name: broker)

    trip_halt(by="frontend", reason="stop", broker="robinhood")
    with pytest.raises(core.ProposalError) as err:
        _approve(out["proposal_id"])

    assert err.value.status_code == 423 and broker.order_calls == []


def test_mcp_guard_unchanged_when_approval_is_off(home) -> None:
    _write_mandate(home, _bound_mandate())
    broker = GateBroker()
    guard = order_guard.LiveOrderGuardTool(broker, _spec(), broker="robinhood", session_id="s1")
    out = json.loads(guard.execute(symbol="AAPL", side="buy", instrument_type="equity", notional_usd=100.0))
    assert out["status"] == "ok" and len(broker.order_calls) == 1


def test_registry_tells_the_model_orders_become_proposals(home, monkeypatch) -> None:
    from src.live.registry import wrap_live_broker_tools
    from src.tools.mcp import MCPRemoteTool

    tool = MCPRemoteTool(GateBroker(), _spec())
    assert "approval" not in wrap_live_broker_tools("robinhood", [tool])[0].description.lower()
    _required(monkeypatch)
    wrapped = wrap_live_broker_tools("robinhood", [MCPRemoteTool(GateBroker(), _spec())])[0]
    assert isinstance(wrapped, order_guard.LiveOrderGuardTool)
    assert "Order approval is required" in wrapped.description


# --------------------------------------------------------------------------- #
# Runner halt sweep                                                            #
# --------------------------------------------------------------------------- #


def test_halt_sweep_cancels_but_holds_closing_orders(home, monkeypatch) -> None:
    _required(monkeypatch)
    _write_mandate(home, _bound_mandate())
    submitted: list[dict] = []

    def submit(request):
        submitted.append(request)
        return {"status": "ok"}

    report = flatten_and_cancel(
        "robinhood", submit,
        read_positions=lambda: [{"symbol": "AAPL", "qty": 3}, {"symbol": "MSFT", "qty": -1}],
        read_open_orders=lambda: [{"order_id": "o1"}],
        allow_flatten=True,
    )

    assert submitted == [{"action": "cancel", "order_id": "o1"}]  # cancels still run
    assert report["flatten_orders_submitted"] == []
    assert "held as proposal op_" in report["flatten_skipped_reason"]
    proposal_id = report["flatten_skipped_reason"].split("proposal ")[1].split(" ")[0]
    proposal = core.load_proposal(proposal_id)
    assert proposal["route"]["kind"] == "mcp_flatten"
    assert [(o["symbol"], o["side"], o["qty"]) for o in proposal["orders"]] == [("AAPL", "sell", 3.0), ("MSFT", "buy", 1.0)]


def test_halt_sweep_unchanged_when_approval_is_off(home) -> None:
    submitted: list[dict] = []
    report = flatten_and_cancel(
        "robinhood", lambda request: submitted.append(request) or {"status": "ok"},
        read_positions=lambda: [{"symbol": "AAPL", "qty": 3}], read_open_orders=lambda: [], allow_flatten=True,
    )
    assert [r["action"] for r in submitted] == ["close"]
    assert report["flatten_orders_submitted"][0]["symbol"] == "AAPL"


# --------------------------------------------------------------------------- #
# zt-paper is always approval-gated                                            #
# --------------------------------------------------------------------------- #


def test_zt_paper_orders_are_proposals_even_with_approval_off(home, monkeypatch) -> None:
    zt = TradingProfile(id="zt-paper", connector="zt-paper", label="ZT Paper", environment="paper",
                        transport="local_plugin", capabilities=("account.read", "positions.read"), readonly=True)
    monkeypatch.setattr(service, "profile_by_id", lambda profile_id=None: zt)
    held = service.place_order("AAPL", "zt-paper", side="buy", quantity=5)
    assert held["status"] == "pending_approval"
    engine = core.zt_paper_engine()
    assert not engine.account_path().exists()
    assert _approve(held["proposal_id"])["status"] == core.FILLED
    assert engine.snapshot()["cash"] == 99_000.0
