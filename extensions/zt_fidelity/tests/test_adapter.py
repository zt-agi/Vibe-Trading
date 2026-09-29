"""Unit tests for the zt-fidelity read-only connector adapter (synthetic data only)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from zt_fidelity_fixtures import *  # noqa: F403  (pytest fixtures)
from zt_fidelity_testkit import CONNECTOR_DIR, FIXTURE_CSV, RAW_ACCOUNTS, tree_state, write_export

UTC = timezone.utc


def dumps(payload) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# parsing primitives
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("$1,234.56", Decimal("1234.56")), ("-$1,234.56", Decimal("-1234.56")),
    ("$-12.00", Decimal("-12.00")), ("+$2.50", Decimal("2.50")), ("($3.00)", Decimal("-3.00")),
    ("+1.17%", Decimal("1.17")), ("-0.43%", Decimal("-0.43")), ("10,000", Decimal("10000")),
    ("12.345", Decimal("12.345")), ("--", None), ("", None), ("n/a", None), ("abc", None),
    (None, None),
])
def test_parse_number(adapter, raw, expected):
    assert adapter.parse_number(raw) == expected


@pytest.mark.parametrize("line, expected", [
    ("Date downloaded Sep-26-2026 5:15 p.m ET", datetime(2026, 9, 26, 21, 15, tzinfo=UTC)),
    ('"Date downloaded Sep-26-2026 10:15 a.m. ET"', datetime(2026, 9, 26, 14, 15, tzinfo=UTC)),
    ("Date downloaded 09/26/2026 5:15 PM ET", datetime(2026, 9, 26, 21, 15, tzinfo=UTC)),
    ("Date downloaded Dec-01-2026 4:00 p.m ET", datetime(2026, 12, 1, 21, 0, tzinfo=UTC)),  # EST
    ("Brokerage services are provided by ...", None),
])
def test_footer_time(adapter, line, expected):
    assert adapter.parse_footer_time(line) == expected


def test_account_mask(adapter):
    assert adapter.mask_account("X00001111") == "****1111"
    assert adapter.mask_account("2AB-06708") == "****6708"
    assert adapter.mask_account("") == "****"


def test_parse_fixture_rows_cash_pending_and_footer(adapter):
    parsed = adapter.parse_positions_csv(FIXTURE_CSV.read_bytes())
    assert parsed["counts"] == {"positions": 6, "cash": 2, "pending": 1, "skipped_no_quantity": 1,
                                "skipped_no_symbol": 0, "non_data_lines": 3, "repeated_header": 0}
    accounts = parsed["accounts"]
    assert sorted(accounts) == ["****1111", "****2222"]
    first, second = accounts["****1111"], accounts["****2222"]
    assert (first["value"], first["core_cash"], first["pending_activity"]) == (
        Decimal("4930.00"), Decimal("1250.00"), Decimal("-300.00"))
    assert (second["value"], second["core_cash"], second["value_complete"]) == (
        Decimal("16695.90"), Decimal("500.00"), False)          # the "--" QQQ row has no value
    assert parsed["footer_time"] == datetime(2026, 9, 26, 21, 15, tzinfo=UTC)
    assert parsed["_secrets"] == set(RAW_ACCOUNTS)


def test_merged_holdings_are_unique_and_priced_per_unit(adapter):
    parsed = adapter.parse_positions_csv(FIXTURE_CSV.read_bytes())
    holdings = {h["symbol"]: h for h in adapter.merge_holdings(parsed["lines"])}
    assert sorted(holdings) == ["-NVDA261218C200", "912797XX1", "AAPL", "FXAIX", "NVDA"]
    nvda = holdings["NVDA"]
    assert (nvda["quantity"], nvda["market_price"], nvda["market_value"], nvda["cost_price"],
            nvda["unrealized_pnl"]) == (30.0, 181.5, 5445.0, 100.0, 2445.0)
    assert nvda["accounts"] == ["****1111", "****2222"]
    assert nvda["account"] == "****1111, ****2222"
    assert nvda["held_in"] == ["Individual ****1111", "ROLLOVER IRA ****2222"]
    option = holdings["-NVDA261218C200"]
    assert (option["asset_type"], option["market_price"], option["price_multiplier"],
            option["cost_price"], option["last_price"]) == ("option", 450.0, 100.0, 500.0, 4.5)
    bond = holdings["912797XX1"]
    assert (bond["asset_type"], bond["market_price"], bond["price_multiplier"]) == ("bond", 0.985, 0.01)
    fund = holdings["FXAIX"]
    assert (fund["asset_type"], fund["cost_price"], fund["unrealized_pnl"]) == ("mutual_fund", None, None)
    assert "asset_type" not in holdings["AAPL"]            # VT's own default (stock) applies
    for h in holdings.values():                            # quantity x price == Fidelity's value
        assert abs(h["quantity"] * h["market_price"] - h["market_value"]) < 0.01, h["symbol"]
        assert h["currency"] == "USD"


def test_header_variants_bom_preamble_and_trailing_commas(adapter):
    text = FIXTURE_CSV.read_bytes().decode("utf-8-sig")
    header, rest = text.split("\r\n", 1)
    capitalised = ",".join(word.title() if i < 16 else word for i, word in enumerate(header.split(",")))
    variant = ("\r\n\r\n" + capitalised.replace("'S", "\u2019s") + ",\r\n" + rest).encode("utf-8-sig")
    parsed = adapter.parse_positions_csv(variant)
    assert parsed["counts"]["positions"] == 6
    assert parsed["counts"]["non_data_lines"] == 3


def test_cp1252_export_is_read(adapter):
    raw = FIXTURE_CSV.read_bytes().decode("utf-8-sig").replace("APPLE INC", "APPLE INC \u00a9")
    assert adapter.parse_positions_csv(raw.encode("cp1252"))["counts"]["positions"] == 6


@pytest.mark.parametrize("body, message", [
    (b"Symbol,Quantity\r\nNVDA,1\r\n", "no 'Account number' header"),
    (b"Account number,Account name,Symbol,Quantity\r\nX00001111,I,NVDA,1\r\n", "missing columns"),
])
def test_unexpected_files_fail_without_quoting_content(adapter, body, message):
    with pytest.raises(adapter.ExportError, match=message) as info:
        adapter.parse_positions_csv(body)
    assert "X00001111" not in str(info.value) and "NVDA" not in str(info.value)


# ---------------------------------------------------------------------------
# selection, as-of and staleness
# ---------------------------------------------------------------------------

def test_as_of_precedence_context_receipt_footer_mtime(adapter, broker_raw, plugin_config, now):
    context_at = now - timedelta(hours=2)
    receipt_at = now - timedelta(hours=3)
    path = write_export(broker_raw, "2026-09-26_portfolio_refresh", exported_at=context_at,
                        receipt_at=receipt_at)
    export = adapter.load_export(plugin_config, now=now)["export"]
    assert export["as_of"] == adapter._iso(context_at)
    assert export["as_of_source"].startswith("context:portfolio_export_context_")
    next(path.parent.glob("portfolio_export_context_*.json")).unlink()
    export = adapter.load_export(plugin_config, now=now)["export"]
    assert (export["as_of"], export["as_of_source"]) == (
        adapter._iso(receipt_at), "receipt:csv_download_receipts.jsonl:downloaded_at")
    (path.parent / "csv_download_receipts.jsonl").unlink()
    footer_now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    export = adapter.load_export(plugin_config, now=footer_now)["export"]
    assert (export["as_of"], export["as_of_source"]) == (
        "2026-09-26T21:15:00Z", "csv_footer:Date downloaded")
    body = FIXTURE_CSV.read_bytes().replace(b'"Date downloaded Sep-26-2026 5:15 p.m ET"\r\n', b"")
    path.write_bytes(body)
    stamp = (now - timedelta(minutes=10)).timestamp()
    os.utime(path, (stamp, stamp))
    export = adapter.load_export(plugin_config, now=now)["export"]
    assert export["as_of_source"] == "file_mtime"
    assert any("mtime" in w for w in export["warnings"])


def test_context_stamp_and_nested_keys(adapter, broker_raw, plugin_config):
    path = write_export(broker_raw, "2026-09-26_refresh")
    (path.parent / "portfolio_export_context_2026-09-26T17-15-30.json").write_text(
        json.dumps({"note": "no time fields"}), encoding="utf-8")
    now = datetime(2026, 9, 27, tzinfo=UTC)
    export = adapter.load_export(plugin_config, now=now)["export"]
    # the stamp has no zone -> New York wall time (EDT): 17:15:30 ET = 21:15:30Z
    assert (export["as_of"], export["as_of_source"]) == (
        "2026-09-26T21:15:30Z",
        "context:portfolio_export_context_2026-09-26T17-15-30.json:filename_stamp")


def test_future_context_time_is_ignored(adapter, broker_raw, plugin_config):
    now = datetime(2026, 9, 27, tzinfo=UTC)
    write_export(broker_raw, "2026-09-26_refresh", exported_at=now + timedelta(days=2))
    export = adapter.load_export(plugin_config, now=now)["export"]
    assert export["as_of_source"] == "csv_footer:Date downloaded"
    assert any("future" in w for w in export["warnings"])


def test_newest_export_wins_by_date_then_export_time(adapter, broker_raw, plugin_config, now):
    older = FIXTURE_CSV.read_bytes().replace(b"Sep-26-2026", b"Sep-25-2026")
    write_export(broker_raw, "2026-09-25_portfolio_refresh", csv_bytes=older,
                 name="Portfolio_Positions_Sep-25-2026.csv", exported_at=now - timedelta(hours=30))
    write_export(broker_raw, "2026-09-26_portfolio_refresh", exported_at=now - timedelta(hours=5),
                 context_stamp="20260926T120000Z")
    later = FIXTURE_CSV.read_bytes().replace(b"NVIDIA CORPORATION COM", b"NVIDIA CORP (LATER)")
    write_export(broker_raw, "2026-09-26_refresh", csv_bytes=later, exported_at=now - timedelta(hours=1))
    snapshot = adapter.load_export(plugin_config, now=now)
    assert snapshot["export"]["file"] == "2026-09-26_refresh/Portfolio_Positions_Sep-26-2026.csv"
    assert snapshot["export"]["candidates_considered"] == 3
    assert snapshot["parsed"]["lines"][0]["description"] == "NVIDIA CORP (LATER)"


def test_unreadable_newest_file_falls_back_and_says_so(adapter, broker_raw, plugin_config, now):
    write_export(broker_raw, "2026-09-25_refresh", name="Portfolio_Positions_Sep-25-2026.csv",
                 exported_at=now - timedelta(hours=20))
    write_export(broker_raw, "2026-09-26_refresh", csv_bytes=b"<html>session expired</html>")
    export = adapter.load_export(plugin_config, now=now)["export"]
    assert export["file"] == "2026-09-25_refresh/Portfolio_Positions_Sep-25-2026.csv"
    assert any("2026-09-26_refresh/Portfolio_Positions_Sep-26-2026.csv unreadable" in w
               for w in export["warnings"])


def test_stale_export_blocks_reads_unless_served(adapter, broker_raw, plugin_config, now, monkeypatch):
    write_export(broker_raw, "2026-09-26_refresh", exported_at=now - timedelta(days=4))
    monkeypatch.setattr(adapter, "_utcnow", lambda: now)
    status = adapter.check_status(credentials={}, config=plugin_config)
    assert status["status"] == "STALE" and "configured" not in status
    assert "STALE" in status["error"] and "4.0 days old; limit 3" in status["error"]
    account = adapter.get_account_snapshot(credentials={}, config=plugin_config)
    positions = adapter.get_positions(credentials={}, config=plugin_config)
    assert account["status"] == positions["status"] == "STALE"
    assert account["error"] == positions["error"] == status["error"]
    settings = Path(plugin_config["plugin_directory"]) / "settings.json"
    settings.write_text(json.dumps({"broker_raw_dir": str(broker_raw), "serve_stale": True}),
                        encoding="utf-8")
    served = adapter.get_positions(credentials={}, config=plugin_config)
    assert served["status"] == "ok" and served["export"]["stale"] is True
    assert adapter.check_status(credentials={}, config=plugin_config)["status"] == "STALE"
    settings.write_text(json.dumps({"broker_raw_dir": str(broker_raw), "stale_after_days": 7}),
                        encoding="utf-8")
    fresh = adapter.check_status(credentials={}, config=plugin_config)
    assert (fresh["status"], fresh["configured"], fresh["export"]["stale"]) == ("ok", True, False)


def test_fresh_export_payloads(adapter, broker_raw, plugin_config, now, monkeypatch):
    write_export(broker_raw, "2026-09-26_portfolio_refresh", exported_at=now - timedelta(hours=1))
    monkeypatch.setattr(adapter, "_utcnow", lambda: now)
    status = adapter.check_status(credentials={}, config=plugin_config)
    assert (status["status"], status["configured"], status["accounts"]) == (
        "ok", True, ["****1111", "****2222"])
    account = adapter.get_account_snapshot(credentials={}, config=plugin_config)
    assert account["status"] == "ok" and account["readonly"] is True
    export = account["export"]
    assert account["account"] == {"currency": "USD", "portfolio_value": 21625.9, "cash": 1450.0,
                                  "core_cash": 1750.0, "pending_activity": -300.0,
                                  "account_count": 2, "as_of": export["as_of"],
                                  "as_of_source": export["as_of_source"],
                                  "export_file": export["file"], "stale": False}
    assert export["as_of_source"].startswith("context:")
    assert account["accounts"] == ["****1111", "****2222"]
    assert [(a["account"], a["name"], a["total_value"]) for a in account["account_breakdown"]] == [
        ("****1111", "Individual", 4930.0), ("****2222", "ROLLOVER IRA", 16695.9)]
    positions = adapter.get_positions(credentials={}, config=plugin_config)
    assert positions["status"] == "ok"
    assert len(positions["positions"]) == len({p["symbol"] for p in positions["positions"]}) == 5
    assert positions["export"]["rows"]["positions"] == 6
    for payload in (status, account, positions):
        text = dumps(payload)
        for raw in RAW_ACCOUNTS:
            assert raw not in text


def test_account_numbers_in_free_text_are_masked(adapter, broker_raw, plugin_config, now, monkeypatch):
    body = FIXTURE_CSV.read_bytes().replace(b"APPLE INC", b"APPLE INC (JOURNALED FROM X00001111)")
    write_export(broker_raw, "2026-09-26_refresh", csv_bytes=body, exported_at=now)
    monkeypatch.setattr(adapter, "_utcnow", lambda: now)
    positions = adapter.get_positions(credentials={}, config=plugin_config)
    names = {p["symbol"]: p["name"] for p in positions["positions"]}
    assert names["AAPL"] == "APPLE INC (JOURNALED FROM ****1111)"
    assert "X00001111" not in dumps(positions)


def test_missing_folder_and_empty_tree_are_reported_not_raised(adapter, broker_raw, plugin_config, tmp_path):
    status = adapter.check_status(credentials={}, config=plugin_config)
    assert status["status"] == "error" and "no Portfolio_Positions_*.csv" in status["error"]
    assert status["export_found"] is False
    settings = Path(plugin_config["plugin_directory"]) / "settings.json"
    settings.write_text(json.dumps({"broker_raw_dir": str(tmp_path / "nowhere")}), encoding="utf-8")
    account = adapter.get_account_snapshot(credentials={}, config=plugin_config)
    assert account["status"] == "error" and "broker raw folder not found" in account["error"]


def test_settings_resolution_order(adapter, tmp_path, monkeypatch):
    plugin = tmp_path / "p"
    plugin.mkdir()
    config = {"plugin_directory": str(plugin)}
    assert adapter.load_settings(config)["broker_raw_dir"] == str(
        Path(adapter.DEFAULT_PROJECT_ROOT) / "broker" / "raw")
    monkeypatch.setenv("INVESTMENT_AI_PROJECT_ROOT", str(tmp_path / "proj"))
    assert adapter.load_settings(config)["broker_raw_dir"] == str(tmp_path / "proj" / "broker" / "raw")
    (plugin / "settings.json").write_text(json.dumps({"broker_raw_dir": "S:/x", "stale_after_days": 2}),
                                          encoding="utf-8")
    loaded = adapter.load_settings(config)
    assert (loaded["broker_raw_dir"], loaded["stale_after_days"], loaded["serve_stale"]) == ("S:/x", 2.0, False)
    monkeypatch.setenv("ZT_FIDELITY_BROKER_RAW", "T:/y")
    assert adapter.load_settings(config)["broker_raw_dir"] == "T:/y"
    (plugin / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(adapter.ExportError, match="not valid JSON"):
        adapter.load_settings(config)
    example = json.loads((CONNECTOR_DIR / "settings.example.json").read_text(encoding="utf-8"))
    assert set(example) == {"broker_raw_dir", "folder_glob", "file_glob", "stale_after_days", "serve_stale"}


def test_reads_never_write_to_broker_raw(adapter, broker_raw, plugin_config, now, monkeypatch):
    write_export(broker_raw, "2026-09-25_refresh", name="Portfolio_Positions_Sep-25-2026.csv",
                 exported_at=now - timedelta(days=1), receipt_at=now - timedelta(days=1))
    write_export(broker_raw, "2026-09-26_portfolio_refresh", exported_at=now, receipt_at=now)
    before = tree_state(broker_raw)
    monkeypatch.setattr(adapter, "_utcnow", lambda: now)
    for operation in (adapter.check_status, adapter.get_account_snapshot, adapter.get_positions):
        assert operation(credentials={}, config=plugin_config)["status"] == "ok"
    assert tree_state(broker_raw) == before
