"""zt-research MCP server: ranker wrapper, ASM readers and the VT hypothesis import.

Runs on a synthetic project (tests/fixtures + a generated three-security lake)
with a stub ``pit_alpha_ranker`` that has the real CLI; see
test_real_project.py for the optional run against the canonical project.
"""
from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from zt_research_fixtures import *  # noqa: F403  (pytest fixtures)
from zt_research_testkit import ASOF, tree_state

BOARD = "implementation/alpha_signal_monitor_v01/data/promotion_board.csv"


def _payload(result):
    return result.structured_content or result.data


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_tools_and_annotations(server):
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == set(server.TOOL_NAMES) == {
        "rank_pit_signals", "asm_promotion_board", "hypothesis_test_ledger",
        "import_promotion_board_to_vt"}
    assert tools["asm_promotion_board"].annotations.readOnlyHint is True
    assert tools["hypothesis_test_ledger"].annotations.readOnlyHint is True
    assert tools["rank_pit_signals"].annotations.readOnlyHint is False
    assert tools["import_promotion_board_to_vt"].annotations.readOnlyHint is False
    for tool in tools.values():
        assert tool.annotations.destructiveHint is False and tool.description
    assert tools["import_promotion_board_to_vt"].parameters["properties"]["apply"]["default"] is False
    assert set(server.rank_configs()) == {"rank01_market_prices", "rank01_market_prices_retained"}


# ---------------------------------------------------------------------------
# rank_pit_signals
# ---------------------------------------------------------------------------

def test_pitdb_export_is_the_as_of_view(server, project):
    out = project.parent.parent / "eod.csv"
    at = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    meta = server.export_pitdb_eod(project, at, {"lookback_days": 550,
                                                 "asset_classes": ["equity", "etf"]}, out)
    frame = pd.read_csv(out)
    assert list(frame.columns) == ["ticker", "event_date", "Open", "High", "Low", "Close", "Volume"]
    assert sorted(frame["ticker"].unique()) == ["AAA", "BBB"]            # the index is excluded
    assert frame["event_date"].max() == "2026-08-14"                      # later bars unknown yet
    revised = frame[(frame.ticker == "AAA") & (frame.event_date == "2026-07-01")]
    assert revised["Close"].tolist() != [999.0]                           # revision known later
    assert (meta["rows"], meta["tickers"], meta["revised_rows"]) == (len(frame), 2, 0)
    assert set(meta["lake"]) == {"fact_price_eod", "dim_security"}


def test_rank_run_writes_only_scratch_and_returns_summary(server, project, vt_home):
    before = tree_state(project)
    result = server.rank_pit_signals("rank01_market_prices", ASOF, limit=5)
    assert result["status"] == "PASS", result.get("error")
    run_dir = Path(result["run_dir"])
    assert run_dir.parent == vt_home / "zt_research" / "rank_runs"
    assert Path(result["manifest_path"]).is_file() and Path(result["ranker_manifest_path"]).is_file()
    assert tree_state(project) == before                  # nothing written into the project
    assert not list(project.rglob("__pycache__"))
    cut = result["pit_cut"]
    assert cut["rows_kept"] == cut["rows_in"] > 0 and cut["dropped_not_known_at_asof"] == 0
    assert cut["target_time_last"] <= ASOF
    summary = result["summary"]
    assert summary["signals"] == 1 and summary["by_status"] == {"RESEARCH_ONLY_PIT": 1}
    (row,) = summary["groups"]["stub_next_session_return"]
    assert row["signal_id"] == "stub_momentum_1" and row["fdr_q_value"] is None   # NaN -> null
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert [s["returncode"] for s in manifest["steps"]] == [0, 0]
    assert manifest["asof"] == ASOF and manifest["config_name"] == "rank01_market_prices"
    assert manifest["input"]["kind"] == "pitdb_eod" and len(manifest["config_sha256"]) == 64
    assert "output/signal_ranking.csv" in manifest["outputs"]
    assert "input/observations_asof.csv" in manifest["outputs"]
    assert set(manifest["ranker"]["package_sha256"]) >= {"cli.py", "adapters.py"}
    json.dumps(result)                                    # plain JSON for the MCP transport


def test_observations_input_is_cut_at_asof(server, project, vt_home):
    rel = server.rank_configs()["rank01_market_prices_retained"]["input"]["path"]
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    start = datetime(2026, 8, 3, 20, tzinfo=timezone.utc)
    for day in range(20):                                 # decisions 08-04 .. 08-23
        feature = start + timedelta(days=day)
        rows.append({"signal_id": "s1", "source_id": "src", "entity_id": "AAA",
                     "feature_time": feature.isoformat(),
                     "available_time": (feature + timedelta(hours=4)).isoformat(),
                     "decision_time": (feature + timedelta(hours=17)).isoformat(),
                     "target_time": (feature + timedelta(hours=24)).isoformat(),
                     "signal_value": day, "target_value": 0.001 * day,
                     "pit_class": "OBSERVED_PIT", "ranking_group": "g1"})
    pd.DataFrame(rows).to_csv(path, index=False)
    result = server.rank_pit_signals("rank01_market_prices_retained", ASOF)
    assert result["status"] == "PASS"
    cut = result["pit_cut"]
    # row d's target resolves 2026-08-(4+d) 20:00Z; by 08-15 12:00Z rows 0..10 have resolved
    assert (cut["rows_in"], cut["rows_kept"], cut["dropped_not_known_at_asof"]) == (20, 11, 9)
    kept = pd.read_csv(Path(result["run_dir"]) / "input" / "observations_asof.csv")
    assert pd.to_datetime(kept["target_time"], utc=True).max() <= pd.Timestamp(ASOF)
    assert result["input"]["sha256"] and result["input"]["path"] == rel


def test_ranker_failure_is_reported_with_a_manifest(server, project, vt_home, monkeypatch):
    monkeypatch.setenv("ZT_STUB_RANKER_FAIL", "1")
    result = server.rank_pit_signals("rank01_market_prices", ASOF)
    assert result["status"] == "FAIL" and result["summary"] is None
    assert "pit_alpha_ranker_run failed (exit 3)" in result["error"]
    assert "induced failure" in result["error"]
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "FAIL" and manifest["steps"][1]["returncode"] == 3


@pytest.mark.parametrize("config_name, asof, message", [
    ("nope", ASOF, "unknown config_name"),
    ("../x", ASOF, "unknown config_name"),
    ("rank01_market_prices", "2026-08-15T12:00:00", "timezone offset"),
    ("rank01_market_prices", "", "asof is required"),
    ("rank01_market_prices", "2999-01-01T00:00:00Z", "future"),
])
def test_bad_arguments_are_refused(server, project, vt_home, config_name, asof, message):
    with pytest.raises(ValueError, match=message):
        server.rank_pit_signals(config_name, asof)


def test_scratch_must_be_outside_the_project(server, project, vt_home, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(project / "scratch"))
    with pytest.raises(ValueError, match="inside the shared project"):
        server.rank_pit_signals("rank01_market_prices", ASOF)
    monkeypatch.delenv("VIBE_TRADING_HOME")
    with pytest.raises(RuntimeError, match="VIBE_TRADING_HOME is required"):
        server.rank_pit_signals("rank01_market_prices", ASOF)
    assert not (project / "scratch").exists()


def test_windows_drive_rule(server, monkeypatch):
    monkeypatch.setattr(server.os, "name", "nt")
    monkeypatch.setenv("SystemDrive", "C:")
    for bad in (r"C:\vt\home", r"D:\vt\home", r"G:\My Drive\x"):
        with pytest.raises(ValueError, match="must be on E:"):
            server.require_e_drive(Path(bad), "Runtime")
    assert server.require_e_drive(Path(r"E:\codex-runtime\vt\home"), "Runtime")


# ---------------------------------------------------------------------------
# ASM readers
# ---------------------------------------------------------------------------

def test_promotion_board_reader(server, project):
    board = server.asm_promotion_board()
    assert board["source_path"] == BOARD and board["status"] in {"FRESH", "STALE"}
    rows = board["data"]["rows"]
    assert [r["signal_id"] for r in rows] == [f"SIG-{c}" for c in "ABCDEFG"]
    assert rows[1]["gates"]["G0_prereg"] == "PASS" and board["data"]["gates_run"] == 2 + 4 + 7 + 8 + 6


def test_test_ledger_reader(server, project):
    ledger = server.hypothesis_test_ledger(limit=4)
    data = ledger["data"]
    assert data["cumulative_tests"] == 6 and data["distinct_test_ids"] == 5
    assert data["duplicate_test_ids"] == ["AM01-2026-09-24-SIG-A"]
    assert [r["test_id"] for r in data["rows"]] == [
        "AM01-2026-09-24-SIG-A", "AM01-2026-09-25-SIG-A", "AM01-2026-09-25-SIG-B",
        "AM01-2026-09-25-SIG-C"]
    assert data["hurdle_t"] == 3.0
    with pytest.raises(ValueError):
        server.hypothesis_test_ledger(limit=0)


def test_readers_answer_through_an_mcp_client(server, project):
    from fastmcp import Client

    async def run():
        async with Client(server.mcp) as client:
            board = await client.call_tool("asm_promotion_board", {})
            plan = await client.call_tool("import_promotion_board_to_vt", {})
            return _payload(board), _payload(plan)

    board, plan = asyncio.run(run())
    assert board["data"]["count"] == 7
    assert plan["mode"] == "dry_run" and plan["applied"] is False


# ---------------------------------------------------------------------------
# import_promotion_board_to_vt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal, status", [
    ("SIG-A", "exploring"), ("SIG-B", "testing"), ("SIG-C", "rejected"), ("SIG-D", "validated"),
    ("SIG-E", "monitoring"), ("SIG-F", "testing"), ("SIG-G", "exploring"),
])
def test_status_mapping_is_conservative(server, project, signal, status):
    rows = {r["signal_id"]: r for r in server.asm_promotion_board()["data"]["rows"]}
    assert server.vt_status(rows[signal])[0] == status


def _edit_board(project: Path, edit) -> None:
    path = project / BOARD
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows = edit(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_dry_run_apply_rerun_and_vt_side_edits(server, project, vt_home):
    store = vt_home / "hypotheses.json"
    plan = server.import_promotion_board_to_vt()
    assert plan["store"] == str(store) and plan["summary"] == {"create": 7}
    assert not store.exists()                              # a dry run writes nothing
    applied = server.import_promotion_board_to_vt(apply=True)
    assert applied["applied"] is True and store.exists()

    from src.hypotheses.registry import HypothesisRegistry
    registry = HypothesisRegistry(store)
    hyps = {h.data_sources[0]: h for h in registry.list()}
    assert set(hyps) == {f"asm:SIG-{c}" for c in "ABCDEFG"}
    assert {k: h.status for k, h in hyps.items()} == {
        "asm:SIG-A": "exploring", "asm:SIG-B": "testing", "asm:SIG-C": "rejected",
        "asm:SIG-D": "validated", "asm:SIG-E": "monitoring", "asm:SIG-F": "testing",
        "asm:SIG-G": "exploring"}
    assert hyps["asm:SIG-C"].title == "ASM SIG-C: Synthetic spread"
    assert "A3_t_ge_3=FAIL" in hyps["asm:SIG-C"].invalidation_notes
    assert server.import_promotion_board_to_vt()["summary"] == {"unchanged": 7}

    # a VT-side edit of thesis, skills and sources survives a board change
    a = hyps["asm:SIG-A"]
    registry.update(a.hypothesis_id, thesis="ZT's own thesis", skills=["ontology-hypergraph"],
                    data_sources=[*a.data_sources, "fred:DGS10"])

    def start_sig_a(rows):
        rows[0].update(status="IN_PROGRESS", G0_prereg="PASS")
        return [r for r in rows if r["signal_id"] != "SIG-G"]      # SIG-G leaves the board
    _edit_board(project, start_sig_a)
    plan = server.import_promotion_board_to_vt()
    assert plan["summary"] == {"orphan": 1, "unchanged": 5, "update": 1}
    update = next(c for c in plan["changes"] if c["action"] == "update")
    assert update["signal_id"] == "SIG-A"
    assert update["changes"]["status"] == {"from": "exploring", "to": "testing"}
    assert "data_sources" not in update["changes"]                 # markers already present
    orphan = next(c for c in plan["changes"] if c["action"] == "orphan")
    assert orphan["signal_id"] == "SIG-G"
    server.import_promotion_board_to_vt(apply=True)
    after = {h.hypothesis_id: h for h in HypothesisRegistry(store).list()}
    edited = after[a.hypothesis_id]
    assert (edited.status, edited.thesis, edited.skills) == ("testing", "ZT's own thesis",
                                                             ["ontology-hypergraph"])
    assert "fred:DGS10" in edited.data_sources
    assert len(after) == 7                                         # the orphan was not deleted


def test_duplicate_markers_are_a_conflict_not_a_guess(server, project, vt_home):
    server.import_promotion_board_to_vt(apply=True)
    from src.hypotheses.registry import HypothesisRegistry
    registry = HypothesisRegistry(vt_home / "hypotheses.json")
    registry.create(title="copy", thesis="a hand-made duplicate", data_sources=["asm:SIG-B"])
    plan = server.import_promotion_board_to_vt(apply=True)
    conflict = next(c for c in plan["changes"] if c["action"] == "conflict")
    assert conflict["signal_id"] == "SIG-B" and len(conflict["hypothesis_ids"]) == 2
    assert plan["summary"]["conflict"] == 1


def test_missing_board_imports_nothing(server, project, vt_home):
    (project / BOARD).unlink()
    plan = server.import_promotion_board_to_vt(apply=True)
    assert plan["changes"] == [] and plan["board"]["status"] == "MISSING"
    assert not (vt_home / "hypotheses.json").exists()
