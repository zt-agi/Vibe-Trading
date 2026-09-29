"""zt-paper: fills at the completed-session close, cash math, fail-closed prices,
all-or-nothing batches, the leverage cap, single use per proposal and reset."""
import json
from datetime import datetime, timezone

import pytest

from conftest import PRICES


def _fill(engine, orders, proposal_id="op_" + "a" * 32):
    return engine.fill_orders(orders, proposal_id=proposal_id)


def _pid(n: int) -> str:
    return "op_" + f"{n:032x}"


@pytest.mark.parametrize(("raw", "ticker"), [("aapl", "AAPL"), ("AAPL.US", "AAPL"), ("US.MSFT", "MSFT"),
                                             ("BRK.B", "BRK-B"), ("brk/b", "BRK-B"), ("BF-B", "BF-B"),
                                             (" spy ", "SPY")])
def test_symbol_normalization(engine, raw, ticker):
    assert engine.normalize_symbol(raw) == ticker


@pytest.mark.parametrize("raw", ["700.HK", "600519.SH", "BTC-USDT", "", "AAPL; DROP", "TOOLONGTICKER1"])
def test_non_us_ticker_is_refused(engine, raw):
    with pytest.raises(ValueError):
        engine.normalize_symbol(raw)


def test_fresh_account_is_not_written_until_a_fill(engine, home):
    snap = engine.snapshot()
    assert (snap["account"], snap["label"], snap["currency"]) == ("ZT-PAPER", "SIMULATED", "USD")
    assert snap["cash"] == snap["equity"] == 100_000.0 and snap["persisted"] is False
    assert not engine.account_path().exists()


def test_fills_and_cash_math(engine, home):
    fills = _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 10}], _pid(1))
    assert fills[0]["price"] == 200.0 and fills[0]["notional"] == 2_000.0 and fills[0]["label"] == "SIMULATED"
    assert fills[0]["price_date"] == "2026-09-28" and fills[0]["cash_after"] == 98_000.0

    PRICES["AAPL"] = 210.0
    fills = _fill(engine, [{"symbol": "AAPL", "side": "sell", "qty": 4}], _pid(2))
    assert fills[0]["realized_pnl"] == pytest.approx(40.0)
    snap = engine.snapshot()
    assert snap["cash"] == pytest.approx(98_840.0)
    position = snap["positions"][0]
    assert (position["qty"], position["avg_cost"], position["mark"]) == (6.0, 200.0, 210.0)
    assert position["unrealized_pnl"] == pytest.approx(60.0)
    assert snap["equity"] == pytest.approx(98_840.0 + 6 * 210.0)
    assert snap["realized_pnl"] == pytest.approx(40.0)
    assert account_on_disk(engine)["fills_count"] == 2
    log = [json.loads(line) for line in engine.fills_path().read_text().splitlines()]
    assert [row["event"] for row in log] == ["fill", "fill"]


def account_on_disk(engine):
    return json.loads(engine.account_path().read_text(encoding="utf-8"))


def test_notional_orders_and_crossing_zero(engine, home):
    _fill(engine, [{"symbol": "MSFT", "side": "buy", "notional": 2_000}], _pid(1))
    assert engine.snapshot()["positions"][0]["qty"] == 5.0
    PRICES["MSFT"] = 380.0
    _fill(engine, [{"symbol": "MSFT", "side": "sell", "qty": 8}], _pid(2))
    position = engine.snapshot()["positions"][0]
    assert position["qty"] == -3.0 and position["avg_cost"] == 380.0
    assert position["realized_pnl"] == pytest.approx(5 * (380.0 - 400.0))
    assert engine.snapshot()["cash"] == pytest.approx(100_000 - 2_000 + 8 * 380.0)


def test_limit_orders_fill_only_when_marketable_at_the_close(engine, home):
    with pytest.raises(engine.PaperRejected, match="not marketable"):
        _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1, "order_type": "limit", "limit_price": 199}], _pid(1))
    fills = _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1, "order_type": "limit", "limit_price": 205}], _pid(2))
    assert fills[0]["price"] == 200.0
    with pytest.raises(engine.PaperRejected, match="not marketable"):
        _fill(engine, [{"symbol": "AAPL", "side": "sell", "qty": 1, "order_type": "limit", "limit_price": 201}], _pid(3))


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), 0.0, -5.0])
def test_non_finite_or_non_positive_prices_are_refused(engine, home, monkeypatch, bad):
    from src.live import order_proposals as core

    monkeypatch.setattr(engine, "reference_close",
                        lambda s: None if bad is None else core.RefPrice("AAPL", bad, "2026-09-28", "bad"))
    with pytest.raises(engine.PaperRejected, match="finite positive"):
        _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1}])
    assert not engine.account_path().exists()


def test_a_batch_is_all_or_nothing(engine, home):
    PRICES.pop("NVDA")
    with pytest.raises(engine.PaperRejected):
        _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1}, {"symbol": "NVDA", "side": "buy", "qty": 1}])
    assert not engine.account_path().exists()
    assert engine.snapshot()["cash"] == 100_000.0


def test_leverage_cap(engine, home):
    with pytest.raises(engine.PaperRejected, match="leverage cap"):
        _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 600}], _pid(1))  # 120,000 on 100,000
    assert not engine.account_path().exists()
    engine.reset(starting_cash=100_000, max_leverage=2.0)
    _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 600}], _pid(2))
    snap = engine.snapshot()
    assert snap["cash"] == -20_000.0 and snap["leverage"] == pytest.approx(1.2)


def test_short_exposure_counts_toward_the_leverage_cap(engine, home):
    with pytest.raises(engine.PaperRejected, match="leverage cap"):
        _fill(engine, [{"symbol": "NVDA", "side": "sell", "qty": 1_001}], _pid(1))  # |-100,100| > 100,000 equity


def test_one_fill_per_proposal(engine, home):
    _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1}], _pid(7))
    with pytest.raises(engine.PaperRejected, match="already filled"):
        _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1}], _pid(7))
    assert engine.snapshot()["positions"][0]["qty"] == 1.0


def test_reset_archives_the_old_account_and_rejects_pending_paper_proposals(engine, core, home):
    _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 3}], _pid(1))
    pending = core.create_proposal(broker="zt-paper", orders=[{"symbol": "MSFT", "side": "buy", "qty": 1}],
                                   rationale="r", evidence_ids=["E"], origin={"kind": "test"})

    account = engine.reset(starting_cash=50_000, reason="new test")
    rejected = engine.reject_pending_proposals()

    assert account["cash"] == 50_000.0 and account["positions"] == {}
    assert len(list((engine.state_dir() / "history").glob("account-*.json"))) == 1
    assert json.loads(engine.fills_path().read_text().splitlines()[-1])["event"] == "reset"
    assert rejected == [pending["id"]]
    assert core.load_proposal(pending["id"])["status"] == core.REJECTED


@pytest.mark.parametrize(("cash", "leverage"), [(0, 1), (-1, 1), (float("nan"), 1), (2e9, 1), (1000, 0), (1000, 9)])
def test_reset_validates_its_inputs(engine, home, cash, leverage):
    with pytest.raises(ValueError):
        engine.reset(starting_cash=cash, max_leverage=leverage)


def test_state_lives_under_the_vt_runtime_root(engine, home):
    _fill(engine, [{"symbol": "AAPL", "side": "buy", "qty": 1}], _pid(1))
    assert engine.account_path() == home / "live" / "zt-paper" / "account.json"
    assert engine.fills_path().parent == home / "live" / "zt-paper"


def test_command_line_reset_and_show(engine, home, capsys):
    assert engine.main(["reset", "--starting-cash", "25000"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cash"] == 25_000.0 and out["label"] == "SIMULATED"
    assert engine.main(["show"]) == 0
    assert json.loads(capsys.readouterr().out)["equity"] == 25_000.0


def test_price_source_selection(engine, core, home, monkeypatch):
    monkeypatch.undo()  # drop the fixture price table: exercise the real selection
    monkeypatch.setenv("VIBE_TRADING_HOME", str(home))
    calls = []
    monkeypatch.setattr(core, "vt_loader_close", lambda symbol, **kw: calls.append(("vt", symbol, kw)) or
                        core.RefPrice(symbol, 123.0, "2026-09-28", "vt:yahoo"))
    monkeypatch.setattr(engine, "_pitdb_close", lambda symbol: calls.append(("pitdb", symbol)) or None)

    monkeypatch.delenv("ZT_PAPER_PRICE_SOURCE", raising=False)
    assert engine.reference_close("aapl.us").source == "vt:yahoo"
    assert calls[-1] == ("vt", "AAPL", {"market": "us_equity"})

    monkeypatch.setenv("ZT_PAPER_PRICE_SOURCE", "pitdb")
    assert engine.reference_close("AAPL") is None  # pitdb only: no silent fallback
    monkeypatch.setenv("ZT_PAPER_PRICE_SOURCE", "pitdb_then_vt")
    assert engine.reference_close("AAPL").source == "vt:yahoo"
    assert [c[0] for c in calls[-2:]] == ["pitdb", "vt"]


def test_completed_session_rule_excludes_the_current_new_york_date(core):
    late_evening_ny = datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)  # 19:30 in New York
    assert core.completed_session_cutoff("us_equity", late_evening_ny).isoformat() == "2026-09-28"
    after_midnight_utc = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)  # still 29 Sep in New York
    assert core.completed_session_cutoff("us_equity", after_midnight_utc).isoformat() == "2026-09-28"
