"""ZT add-on: order proposals — a human approves every order.

Covers ``src/live/order_proposals.py`` against the zt-paper simulated account:
nothing is submitted before approval, expiry (410), single use (409), the
confirmation hash, tamper detection against the file and the hash-chained
ledger, the ported ai-hedge-fund checks (long-only sources cannot create
shorts, caps, collar, reference prices, orders == target diff), account scope
(holdings outside it are never touched) and the approver in the ledger.
Prices come from a fixture lookup; nothing touches the network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import src.live.paths as paths
from src.live import order_proposals as core

pytestmark = pytest.mark.unit

PRICES: dict[str, float | None] = {}
BASE_PRICES = {"AAPL": 200.0, "MSFT": 400.0, "NVDA": 100.0, "TSLA": 250.0, "AMZN": 125.0}


def _lookup(symbol: str):
    ticker = str(symbol).upper().removesuffix(".US")
    price = PRICES.get(ticker)
    return core.RefPrice(ticker, price, "2026-09-28", "test:fixture") if price else None


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "get_runtime_root", lambda: tmp_path)
    for key in ("VIBE_ORDER_APPROVAL", "VIBE_ORDER_APPROVAL_TTL_MIN", "ZT_PAPER_PRICE_SOURCE"):
        monkeypatch.delenv(key, raising=False)
    PRICES.clear()
    PRICES.update(BASE_PRICES)
    monkeypatch.setattr(core.zt_paper_engine(), "reference_close", _lookup)
    monkeypatch.setattr(core, "vt_loader_close", lambda symbol, **_: _lookup(symbol))
    return tmp_path


@pytest.fixture
def engine(home):
    return core.zt_paper_engine()


def _signal(symbol: str, direction: str, source: str = "analyst", evidence: str = "E-1") -> dict:
    return {"source": source, "symbol": symbol, "direction": direction, "rationale": "fixture",
            "evidence_ids": [evidence]}


def _propose(**kwargs):
    kwargs.setdefault("rationale", "fixture decision")
    kwargs.setdefault("evidence_ids", ["E-1"])
    kwargs.setdefault("origin", {"kind": "test", "actor": "agent"})
    return core.create_proposal(broker="zt-paper", **kwargs)


def _checks(proposal: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in proposal["validation"]["checks"]}


def _approve(proposal: dict, **kwargs):
    return core.approve_proposal(proposal["id"], confirm_hash=proposal["content_hash"], **kwargs)


# --------------------------------------------------------------------------- #
# Mode and TTL                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("value", "mode"), [(None, "off"), ("", "off"), ("off", "off"), ("false", "off"),
                                             ("required", "required"), (" REQUIRED ", "required"),
                                             ("true", "required"), ("reqired-typo", "required")])
def test_approval_mode_fails_closed(monkeypatch: pytest.MonkeyPatch, value, mode) -> None:
    if value is None:
        monkeypatch.delenv(core.APPROVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(core.APPROVAL_ENV, value)
    assert core.approval_mode() == mode


@pytest.mark.parametrize(("value", "minutes"), [(None, 15.0), ("30", 30.0), ("0", 1.0), ("99999", 1440.0),
                                                ("abc", 15.0), ("nan", 15.0)])
def test_ttl_default_and_bounds(monkeypatch: pytest.MonkeyPatch, value, minutes) -> None:
    if value is None:
        monkeypatch.delenv(core.TTL_ENV, raising=False)
    else:
        monkeypatch.setenv(core.TTL_ENV, value)
    assert core.ttl_minutes() == minutes


# --------------------------------------------------------------------------- #
# Lifecycle                                                                   #
# --------------------------------------------------------------------------- #


def test_nothing_is_filled_before_approval_then_exactly_once(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "aapl.us", "side": "buy", "qty": 10}],
                        signals=[_signal("AAPL", "long")])

    assert proposal["status"] == core.PENDING
    assert proposal["id"].startswith("op_") and len(proposal["id"]) == 35
    assert proposal["orders"][0]["symbol"] == "AAPL"
    assert proposal["validation"]["ok"] is True
    assert proposal["content_hash"] == core.content_hash(proposal)
    stored = json.loads(core.proposal_path(proposal["id"]).read_text(encoding="utf-8"))
    assert stored["content_hash"] == proposal["content_hash"]
    assert not engine.account_path().exists()  # nothing touched the account
    assert engine.snapshot()["cash"] == 100_000.0

    done = _approve(proposal)

    assert done["status"] == core.FILLED
    assert [t["to"] for t in done["transitions"]] == ["PENDING", "APPROVED", "SUBMITTED", "FILLED"]
    result = done["submission"]["results"][0]
    assert result["status"] == "filled" and result["fill_price"] == 200.0
    snap = engine.snapshot()
    assert snap["cash"] == 98_000.0
    assert [(p["symbol"], p["qty"]) for p in snap["positions"]] == [("AAPL", 10.0)]
    assert snap["label"] == "SIMULATED" and snap["account"] == "ZT-PAPER"


def test_double_approve_is_refused_and_fills_once(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 5}], signals=[_signal("AAPL", "long")])
    _approve(proposal)

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)

    assert err.value.status_code == 409
    assert engine.snapshot()["fills_count"] == 1
    assert engine.snapshot()["cash"] == 99_000.0


def test_claim_file_makes_approval_single_use_even_if_status_is_stale(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 5}], signals=[_signal("AAPL", "long")])
    core._claim(proposal["id"])  # a crashed earlier approval left its claim

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)

    assert err.value.code == "not_pending"
    assert not engine.account_path().exists()


def test_expired_proposal_is_410_and_recorded(engine, home, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 5}], signals=[_signal("AAPL", "long")])
    later = core._parse_iso(proposal["created_utc"]) + timedelta(minutes=15, seconds=1)
    monkeypatch.setattr(core, "_now", lambda: later)

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)

    assert err.value.status_code == 410
    assert core.load_proposal(proposal["id"])["status"] == core.EXPIRED
    last = core.ledger_events(proposal["id"])[-1]
    assert (last["to"], last["actor"]) == ("EXPIRED", "system")
    assert not engine.account_path().exists()
    with pytest.raises(core.ProposalError) as again:
        _approve(proposal)
    assert again.value.status_code == 410


def test_ttl_comes_from_the_environment(engine, home, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(core.TTL_ENV, "2")
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    created = core._parse_iso(proposal["created_utc"])
    assert core._parse_iso(proposal["expires_utc"]) - created == timedelta(minutes=2)
    assert proposal["ttl_minutes"] == 2.0


def test_confirm_hash_must_match_what_the_human_saw(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])

    with pytest.raises(core.ProposalError) as err:
        core.approve_proposal(proposal["id"], confirm_hash="sha256:" + "0" * 64)
    assert (err.value.status_code, err.value.code) == (409, "confirm_hash_mismatch")
    assert core.load_proposal(proposal["id"])["status"] == core.PENDING

    bare = proposal["content_hash"].removeprefix("sha256:")
    assert core.approve_proposal(proposal["id"], confirm_hash=bare)["status"] == core.FILLED


def test_tampered_order_in_the_file_is_refused(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    path = core.proposal_path(proposal["id"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orders"][0]["qty"] = 400.0
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)

    assert (err.value.status_code, err.value.code) == (409, "tampered")
    assert any(e["event"] == "tamper_detected" for e in core.ledger_events(proposal["id"]))
    assert not engine.account_path().exists()


def test_tamper_with_a_recomputed_hash_is_caught_by_the_ledger(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    path = core.proposal_path(proposal["id"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orders"][0]["qty"] = 400.0
    data["content_hash"] = core.content_hash(data)
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(core.ProposalError) as err:
        core.approve_proposal(proposal["id"], confirm_hash=data["content_hash"])

    assert err.value.code == "tampered"
    assert "ledger" in str(err.value)
    assert not engine.account_path().exists()


def test_broken_ledger_chain_refuses_approval(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    ledger = core.ledger_path()
    lines = ledger.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["reason"] = "edited later"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)

    assert err.value.code == "ledger_broken"
    assert not engine.account_path().exists()


def test_ledger_records_approver_reason_and_hash(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 2}], signals=[_signal("AAPL", "long")])
    _approve(proposal, principal={"subject": "shared-key-holder", "auth_method": "shared_key"})

    events = core.ledger_events(proposal["id"])
    assert [e["event"] for e in events] == ["created", "transition", "transition", "transition"]
    assert (events[0]["from"], events[0]["to"], events[0]["actor"]) == (None, "PENDING", "agent")
    approved = events[1]
    assert approved["to"] == "APPROVED" and approved["actor"] == "local-user"
    assert approved["reason"] == "approved by the human operator"
    assert approved["content_hash"] == proposal["content_hash"]
    assert approved["detail"]["principal"]["subject"] == "shared-key-holder"
    assert all(e["record_hash"].startswith("sha256:") for e in events)
    assert core.verify_ledger()["ok"] is True
    assert core.integrity(core.load_proposal(proposal["id"]))["ok"] is True


def test_reject_needs_a_reason_and_is_final(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    with pytest.raises(core.ProposalError) as err:
        core.reject_proposal(proposal["id"], reason="  ")
    assert err.value.status_code == 422

    rejected = core.reject_proposal(proposal["id"], reason="thesis too thin")
    assert rejected["status"] == core.REJECTED
    assert core.ledger_events(proposal["id"])[-1]["reason"] == "thesis too thin"
    with pytest.raises(core.ProposalError) as again:
        _approve(proposal)
    assert again.value.status_code == 409
    assert not engine.account_path().exists()


def test_unknown_or_malformed_ids_are_404(home) -> None:
    for bad in ("op_123", "../../etc/passwd", "op_" + "g" * 32):
        with pytest.raises(core.ProposalError) as err:
            core.load_proposal(bad)
        assert err.value.status_code == 404
    with pytest.raises(core.ProposalError) as err:
        core.load_proposal("op_" + "0" * 32)
    assert err.value.status_code == 404


# --------------------------------------------------------------------------- #
# Validation (ported from ai-hedge-fund)                                       #
# --------------------------------------------------------------------------- #


def test_long_only_source_cannot_create_a_short(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "NVDA", "side": "sell", "qty": 10}],
                        signals=[_signal("NVDA", "short", source="value-analyst")])

    assert _checks(proposal)["direction_permission"] == "FAIL"
    assert proposal["validation"]["ok"] is False
    permission = proposal["decision_record"]["direction_permissions"][0]
    assert permission["creates_or_increases_short"] is True
    assert permission["long_only_bearish_sources"] == ["value-analyst"]
    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)
    assert (err.value.status_code, err.value.code) == (422, "validation_failed")
    assert not engine.account_path().exists()


def test_long_only_source_may_reduce_a_long(engine, home) -> None:
    _approve(_propose(orders=[{"symbol": "NVDA", "side": "buy", "qty": 20}], signals=[_signal("NVDA", "long")]))
    trim = _propose(orders=[{"symbol": "NVDA", "side": "sell", "qty": 20}],
                    signals=[_signal("NVDA", "short", source="value-analyst")])

    assert _checks(trim)["direction_permission"] == "PASS"
    assert _approve(trim)["status"] == core.FILLED
    assert engine.snapshot()["positions"] == []


def test_short_capable_source_from_the_policy_may_short(engine, home) -> None:
    core.policy_path().parent.mkdir(parents=True, exist_ok=True)
    core.policy_path().write_text(json.dumps({"short_capable_sources": ["pead"]}), encoding="utf-8")
    proposal = _propose(orders=[{"symbol": "NVDA", "side": "sell", "qty": 10}],
                        signals=[_signal("NVDA", "short", source="pead")])

    assert _checks(proposal)["direction_permission"] == "PASS"
    assert proposal["decision_record"]["signals"][0]["approach"] == "long_short"
    done = _approve(proposal)
    assert done["status"] == core.FILLED
    snap = engine.snapshot()
    assert snap["cash"] == 101_000.0 and snap["positions"][0]["qty"] == -10.0


def test_proposer_cannot_grant_itself_short_permission(engine, home) -> None:
    signal = {**_signal("NVDA", "short", source="agent"), "approach": "long_short"}
    proposal = _propose(orders=[{"symbol": "NVDA", "side": "sell", "qty": 1}], signals=[signal])
    assert proposal["decision_record"]["signals"][0]["approach"] == "long_only"
    assert _checks(proposal)["direction_permission"] == "FAIL"


def test_targets_are_clamped_and_orders_are_the_target_diff(engine, home) -> None:
    proposal = _propose(targets={"AAPL": 0.5, "MSFT": 0.1},
                        signals=[_signal("AAPL", "long"), _signal("MSFT", "long")])

    decision = proposal["decision_record"]
    assert decision["requested_targets"] == {"AAPL": 0.5, "MSFT": 0.1}
    assert decision["target_weights"] == {"AAPL": 0.25, "MSFT": 0.1}
    assert decision["clamp_events"] == [{"limit": "max_position_pct", "symbol": "AAPL", "before": 0.5, "after": 0.25}]
    assert [(o["symbol"], o["side"], o["qty"]) for o in proposal["orders"]] == [("AAPL", "buy", 125.0), ("MSFT", "buy", 25.0)]
    assert _checks(proposal)["orders_match_targets"] == "PASS"
    assert decision["projected_exposure"]["gross"] == pytest.approx(0.35)


def test_gross_cap_scales_every_target(engine, home) -> None:
    targets = {s: 0.25 for s in ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN")}
    proposal = _propose(targets=targets, signals=[_signal(s, "long") for s in targets])
    clamp = proposal["decision_record"]["clamp_events"][-1]
    assert clamp["limit"] == "max_gross_exposure" and clamp["before"] == pytest.approx(1.25)
    assert sum(proposal["decision_record"]["target_weights"].values()) == pytest.approx(1.0)


def test_orders_no_longer_matching_targets_fail_at_approval(engine, home) -> None:
    proposal = _propose(targets={"AAPL": 0.1}, signals=[_signal("AAPL", "long")])
    _approve(_propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 7}], signals=[_signal("AAPL", "long")]))

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)

    assert err.value.status_code == 422 and "orders_match_targets" in str(err.value)
    assert core.load_proposal(proposal["id"])["status"] == core.PENDING


def test_out_of_scope_holdings_are_never_touched(engine, home) -> None:
    _approve(_propose(orders=[{"symbol": "NVDA", "side": "buy", "qty": 50}], signals=[_signal("NVDA", "long")]))
    proposal = _propose(targets={"AAPL": 0.1}, signals=[_signal("AAPL", "long")])

    assert proposal["account_scope"] == {"account": "ZT-PAPER", "symbols": ["AAPL"]}
    assert [o["symbol"] for o in proposal["orders"]] == ["AAPL"]
    scope_check = next(c for c in proposal["validation"]["checks"] if c["name"] == "account_scope")
    assert "NVDA" in scope_check["detail"] and scope_check["status"] == "PASS"
    _approve(proposal)
    positions = {p["symbol"]: p["qty"] for p in engine.snapshot()["positions"]}
    assert positions == {"NVDA": 50.0, "AAPL": 50.0}


def test_every_order_needs_a_reference_price(engine, home) -> None:
    PRICES["ZZZZ"] = None
    proposal = _propose(orders=[{"symbol": "ZZZZ", "side": "buy", "qty": 1}], signals=[_signal("ZZZZ", "long")])
    assert _checks(proposal)["reference_prices"] == "FAIL"
    assert proposal["validation"]["ok"] is False


def test_non_finite_reference_price_is_refused(engine, home, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "reference_close",
                        lambda s: core.RefPrice("AAPL", float("nan"), "2026-09-28", "bad") if s == "AAPL" else _lookup(s))
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    assert _checks(proposal)["reference_prices"] == "FAIL"


def test_price_collar_and_order_notional_caps(engine, home) -> None:
    collar = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1, "order_type": "limit", "limit_price": 250}],
                      signals=[_signal("AAPL", "long")])
    assert _checks(collar)["price_collar"] == "FAIL"
    inside = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1, "order_type": "limit", "limit_price": 210}],
                      signals=[_signal("AAPL", "long")])
    assert _checks(inside)["price_collar"] == "PASS"
    big = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 150}], signals=[_signal("AAPL", "long")])
    assert _checks(big)["order_notional"] == "FAIL"  # 150 x 200 = 30,000 > 25,000
    assert _checks(big)["per_name_cap"] == "FAIL"    # 30% > 25%


def test_evidence_that_contradicts_the_order_fails(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "short")])
    assert _checks(proposal)["evidence_agrees"] == "FAIL"


def test_missing_evidence_is_advisory(engine, home) -> None:
    proposal = core.create_proposal(broker="zt-paper", orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}],
                                    rationale="", evidence_ids=[], origin={"kind": "test"})
    check = next(c for c in proposal["validation"]["checks"] if c["name"] == "evidence_cited")
    assert check["status"] == "FAIL" and check["blocking"] is False
    assert proposal["validation"]["ok"] is True


def test_invalid_policy_file_fails_closed(engine, home) -> None:
    core.policy_path().parent.mkdir(parents=True, exist_ok=True)
    core.policy_path().write_text(json.dumps({"max_position_pct": -1}), encoding="utf-8")
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    assert _checks(proposal)["policy"] == "FAIL"
    assert proposal["validation"]["ok"] is False


def test_price_moves_are_revalidated_at_approval(engine, home) -> None:
    proposal = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    PRICES["AAPL"] = None

    with pytest.raises(core.ProposalError) as err:
        _approve(proposal)
    assert err.value.status_code == 422
    assert not core._claim_path(proposal["id"]).exists()

    PRICES["AAPL"] = 200.0
    assert _approve(proposal)["status"] == core.FILLED


@pytest.mark.parametrize("orders", [
    [{"symbol": "AAPL", "side": "hold", "qty": 1}],
    [{"symbol": "AAPL", "side": "buy"}],
    [{"symbol": "AAPL", "side": "buy", "qty": 1, "notional": 100}],
    [{"symbol": "AAPL", "side": "buy", "qty": float("inf")}],
    [{"symbol": "AAPL", "side": "buy", "qty": -3}],
    [{"symbol": "AAPL", "side": "buy", "qty": 1, "order_type": "limit"}],
    [{"symbol": "AAPL", "side": "buy", "qty": 1}, {"symbol": "AAPL", "side": "sell", "qty": 1}],
    [{"symbol": "700.HK", "side": "buy", "qty": 1}],
    [],
])
def test_malformed_orders_are_refused_before_any_record(engine, home, orders) -> None:
    with pytest.raises(ValueError):
        _propose(orders=orders)
    assert not core.proposals_dir().exists() or not list(core.proposals_dir().glob("op_*.json"))


def test_list_summary_and_public_view(engine, home) -> None:
    first = _propose(orders=[{"symbol": "AAPL", "side": "buy", "qty": 1}], signals=[_signal("AAPL", "long")])
    second = _propose(orders=[{"symbol": "MSFT", "side": "buy", "qty": 1}], signals=[_signal("MSFT", "long")])
    core.reject_proposal(first["id"], reason="no")

    rows = core.list_proposals()
    assert [r["id"] for r in rows] == [second["id"], first["id"]]
    assert [r["id"] for r in core.list_proposals(status="pending")] == [second["id"]]
    summary = core.summary(second)
    assert summary["approvable"] is True and summary["content_hash_tail"] == second["content_hash"][-12:]
    view = core.public_view(second, full_hash=False)
    assert "content_hash" not in view and view["route"] == {"kind": "zt_paper", "target": "ZT-PAPER"}
