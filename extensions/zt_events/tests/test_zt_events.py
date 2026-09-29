"""zt-events MCP tools (ZT add-on): point-in-time reads, the study, PEAD candidates.

Store-backed tests need INVESTMENT_AI_PROJECT_ROOT (pitdb lives in ZT's private
project; see conftest.py). Guard and packaging tests run everywhere.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import conftest as C
import core
import server


# --------------------------------------------------------------------------- guards (no store)


def test_asof_needs_an_explicit_past_offset():
    with pytest.raises(ValueError, match="timezone offset"):
        core.asof_utc("2026-09-01T12:00:00")
    with pytest.raises(ValueError, match="future"):
        core.asof_utc((datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    with pytest.raises(ValueError, match="required"):
        core.asof_utc("")
    assert core.asof_utc("2026-09-01T08:00:00-04:00") == datetime(2026, 9, 1, 12)


def test_tool_arguments_are_validated_before_any_read(monkeypatch):
    def refuse():
        raise AssertionError("the store must not be opened for invalid arguments")

    monkeypatch.setattr(core, "STORE_FACTORY", refuse)
    with pytest.raises(ValueError, match="not a ticker"):
        server.earnings_8k_events(tickers=["NVDA; DROP TABLE"], asof="2026-09-01T00:00:00Z")
    with pytest.raises(ValueError, match="tickers"):
        server.event_study(tickers=[f"T{i}" for i in range(61)], asof="2026-09-01T00:00:00Z")
    with pytest.raises(ValueError, match="cluster_freq"):
        server.event_study(asof="2026-09-01T00:00:00Z", cluster_freq="Y")
    with pytest.raises(ValueError, match="lookback_days"):
        server.pead_candidates(asof="2026-09-01T00:00:00Z", lookback_days=90)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        server.earnings_8k_events(start="last week", asof="2026-09-01T00:00:00Z")


def test_bucket_edges():
    assert [core.bucket_of(v) for v in (-3, -1.0, -0.5, 0.0, 0.25, 0.9, 1.0, 5)] == \
        ["Q1", "Q2", "Q2", "Q3", "Q4", "Q4", "Q5", "Q5"]
    assert core.bucket_of(None) is None and core.bucket_of(float("nan")) is None
    with pytest.raises(ValueError):
        core.bucket_of(1.0, (1.0, 0.0))


def test_server_has_no_write_or_order_path():
    source = Path(server.__file__).read_text(encoding="utf-8") + Path(core.__file__).read_text(encoding="utf-8")
    for forbidden in ("INSERT", "COPY ", "write_events", "persist_to_lake", "publish_draft",
                      "propose_orders", "place_order", "subprocess", "requests."):
        assert forbidden not in source, forbidden
    assert "read_only=True" in source


def test_the_mcp_surface_is_the_four_read_only_tools():
    from fastmcp import Client

    async def listed():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(listed())
    assert sorted(t.name for t in tools) == sorted(server.TOOL_NAMES) == \
        ["earnings_8k_events", "event_study", "pead_candidates", "sue"]
    assert all(t.annotations.readOnlyHint for t in tools)


# --------------------------------------------------------------------------- store-backed


def test_earnings_events_are_point_in_time_with_duplicates_marked(installed):
    out = server.earnings_8k_events(tickers=["nvda"], asof="2024-08-28T20:21:16Z")
    accessions = [r["accession"] for r in out["rows"]]
    assert accessions[-1] == "0001045810-24-000262"          # visible at its own acceptance ...
    earlier = server.earnings_8k_events(tickers=["NVDA"], asof="2024-08-28T20:21:15Z")
    assert "0001045810-24-000262" not in [r["accession"] for r in earlier["rows"]]   # ... not a second before
    row = out["rows"][-1]
    assert row["knowledge_time"] == row["event_time"] == "2024-08-28T20:21:16Z"
    assert row["pit_class"] == "TRUE_PIT" and out["asof"] == "2024-08-28T20:21:16Z"
    dup = {r["accession"]: r["duplicate_of"] for r in out["rows"]}
    assert dup["0001045810-22-000136"] == "0001045810-22-000133"   # Aug 2022 pre-announcement wins
    assert dup["0001045810-22-000133"] is None
    json.dumps(out)


def test_sue_tool_carries_knowledge_times_and_pit_class(installed):
    at = server.sue("NVDA", "2024-08-28T21:05:08Z")
    assert (at["period_end"], at["status"], at["knowledge_time"]) == ("2024-07-28", "OK", "2024-08-28T21:05:08Z")
    assert at["pit_class"] == "TRUE_PIT" and at["asof"] == "2024-08-28T21:05:08Z"
    assert all(i["knowledge_time"] <= "2024-08-28T21:05:08Z" for i in at["inputs"])
    pending = server.sue("NVDA", "2024-08-28T20:30:00Z")               # after the 8-K, before the 10-Q
    assert pending["period_end"] == "2024-04-28"
    json.dumps(at)


def test_event_study_finds_the_surprise_signed_drift_and_reports_skips(installed):
    out = server.event_study(asof="2026-09-25T23:00:00Z", start="2016-01-01")
    json.dumps(out)
    assert out["status"] == "OK" and out["asof"] == "2026-09-25T23:00:00Z"
    agg = pd.DataFrame(out["aggregates"]).set_index(["window", "group"])
    assert agg.loc[("[+2,+20]", "Q5"), "mean_car"] > 0.02 and agg.loc[("[+2,+20]", "Q5"), "fdr_reject"]
    assert agg.loc[("[+2,+20]", "Q1"), "mean_car"] < -0.02 and agg.loc[("[+2,+20]", "Q1"), "fdr_reject"]
    assert agg.loc[("[+2,+20]", "SIGNED"), "p_bmp_kp"] < 1e-4
    assert not agg.loc[("[0,+1]", "ALL"), "fdr_reject"]                # no announcement effect injected
    assert out["duplicates_dropped"] >= 2                               # NVDA Aug 2022 + AAA's 8-K/A
    reasons = {s["reason"] for s in out["skips"]}
    assert "window incomplete: it runs past the last session" in reasons
    assert out["knowledge_times"]["last_price"] <= "2026-09-25T23:00:00Z"
    assert out["pit_classes"] == {"events": ["TRUE_PIT"], "sue": ["TRUE_PIT"], "prices": ["OBSERVED_PIT"]}
    counts = pd.DataFrame(out["hit_counts"])
    assert "probability" not in counts.columns                        # counts, never probabilities
    assert set(counts["group"]) >= {"ALL", "Q1", "Q5"}


def test_event_study_sees_nothing_after_its_asof(installed):
    early = server.event_study(tickers=["AAA", "BBB"], asof="2020-01-01T00:00:00Z", windows=[[0, 1]])
    assert early["knowledge_times"]["last_event"] < "2020-01-01T00:00:00Z"
    assert early["knowledge_times"]["last_price"] < "2020-01-01T00:00:00Z"
    later = server.event_study(tickers=["AAA", "BBB"], asof="2021-01-01T00:00:00Z", windows=[[0, 1]])
    assert later["events"] > early["events"]


def test_pead_candidates_statuses_and_timing(installed):
    out = server.pead_candidates(asof=C.ASOF_ENTRY, short_bottom=True)
    json.dumps(out)
    cands = {r["ticker"]: r for r in out["candidates"]}
    watch = {r["ticker"]: r for r in out["watchlist"]}
    assert set(cands) == {"AAA", "BBB", "DDD"}
    assert (cands["AAA"]["direction"], cands["BBB"]["direction"], cands["DDD"]["direction"]) == ("LONG", "SHORT", "LONG")
    for t in ("AAA", "BBB", "DDD"):
        assert cands[t]["status"] == "ENTRY_DUE"
        assert cands[t]["decision_session"] == "2026-07-29"
        assert cands[t]["entry_open_utc"] == "2026-07-30T13:30:00Z"
        assert cands[t]["knowledge_time"] <= C.ASOF_ENTRY and cands[t]["sue_knowledge_time"] <= C.ASOF_ENTRY
        assert cands[t]["pit_class"] == "TRUE_PIT" and cands[t]["sue_pit_class"] == "TRUE_PIT"
    # AAA's release was accepted after Tuesday's close: day 0 is Wednesday, never Tuesday.
    assert cands["AAA"]["day0"] == "2026-07-29"
    assert watch["CCC"]["direction"] == "NONE" and watch["CCC"]["sue_bucket"] == "Q3"
    assert watch["EEE"]["status"] == "SUE_PENDING" and watch["FFF"]["status"] == "SUE_PENDING"
    assert all("probability" not in json.dumps(r) for r in out["bucket_history"])
    # Proposal arguments are mechanical: +/- the slot weight, scoped to the one symbol.
    for ticker, weight in (("AAA", 0.1), ("BBB", -0.1), ("DDD", 0.1)):
        proposal = cands[ticker]["proposal"]
        assert proposal["targets"] == {ticker: weight} and proposal["scope_symbols"] == [ticker]
        assert proposal["rationale"].startswith(cands[ticker]["proposal_tag"] + ":")
        assert cands[ticker]["proposal_tag"] == f"zt-pead entry sec:{cands[ticker]['accession']}"
        assert proposal["evidence_ids"][0] == f"sec:{cands[ticker]['accession']}"
        assert proposal["signals"][0]["direction"] == cands[ticker]["direction"].lower()
    assert "proposal" not in watch["CCC"] or watch["CCC"]["proposal"] is None
    assert "probability" not in json.dumps(out["candidates"]) and out["exits"] == []
    no_short = server.pead_candidates(asof=C.ASOF_ENTRY)
    assert {r["ticker"] for r in no_short["candidates"]} == {"AAA", "DDD"}


def test_pead_candidates_before_the_close_and_after_the_open(installed):
    morning = server.pead_candidates(asof="2026-07-29T15:00:00Z", tickers=["AAA", "DDD"])
    states = {r["ticker"]: r["status"] for r in morning["candidates"] + morning["watchlist"]}
    assert states == {"AAA": "AWAITING_DECISION_CLOSE", "DDD": "AWAITING_DECISION_CLOSE"}
    late = server.pead_candidates(asof="2026-07-30T15:00:00Z", tickers=["AAA"])
    assert late["candidates"][0]["status"] == "LATE"
    assert late["candidates"][0]["proposal"]["rationale"].endswith("This entry is 1 session(s) late.")
    stale = server.pead_candidates(asof="2026-07-31T21:00:00Z", tickers=["AAA"])
    assert stale["watchlist"][0]["status"] == "STALE" and not stale["candidates"]


def test_exits_fall_due_at_the_open_that_ends_the_hold(installed):
    # Decision session Wed 2026-07-29, entry at Thursday's open, 20 sessions held:
    # the exit is the open of Thu 2026-08-27.
    due = server.pead_candidates(asof="2026-08-26T22:00:00Z", short_bottom=True)
    exits = {r["ticker"]: r for r in due["exits"]}
    assert set(exits) == {"AAA", "BBB", "DDD"}
    for ticker, row in exits.items():
        assert row["status"] == "EXIT_DUE" and row["exit_open_utc"] == "2026-08-27T13:30:00Z"
        assert row["entry_open_utc"] == "2026-07-30T13:30:00Z" and row["decision_session"] == "2026-07-29"
        assert row["proposal"]["targets"] == {ticker: 0.0} and row["proposal"]["scope_symbols"] == [ticker]
        assert row["proposal"]["signals"][0]["direction"] == "flat"
        assert row["entry_tag"] == f"zt-pead entry sec:{row['accession']}"
        assert row["proposal"]["rationale"].startswith(row["proposal_tag"] + ":")
        assert row["proposal_tag"] == f"zt-pead exit sec:{row['accession']}"
        assert row["knowledge_time"] <= "2026-08-26T22:00:00Z" and row["pit_class"] == "TRUE_PIT"
    late = server.pead_candidates(asof="2026-08-27T15:00:00Z")
    assert {r["ticker"]: r["status"] for r in late["exits"]} == {"AAA": "EXIT_LATE", "DDD": "EXIT_LATE"}
    assert server.pead_candidates(asof="2026-08-28T15:00:00Z")["exits"] == []
    with pytest.raises(ValueError, match="slot_weight"):
        server.pead_candidates(asof="2026-08-26T22:00:00Z", slot_weight=0.5)


def test_a_sue_first_known_after_day_one_is_late_for_the_scan(installed):
    # EEE files its 10-Q twenty days after the release: the SUE exists, but its bucket
    # history (sue_known_by_day=1) never held such an event, so it is no candidate.
    with core.store() as (con, provenance):
        out = core.pead_candidates(con, provenance, asof=core.asof_utc("2026-08-18T22:00:00Z"),
                                   tickers=["EEE"], lookback_days=25)
    assert out["candidates"] == [] and out["params"]["max_sue_lag_sessions"] == 1
    (row,) = out["watchlist"]
    assert row["status"] == "SUE_LATE" and row["sue_bucket"] is not None
    assert row["sue_knowledge_time"] <= "2026-08-18T22:00:00Z"


def test_a_stale_index_is_refused(monkeypatch, tmp_path):
    guard = core.load_pit_guard()
    monkeypatch.setattr(core, "STORE_FACTORY", None)
    monkeypatch.setattr(core, "project", lambda: tmp_path)
    monkeypatch.setattr(core, "runtime", lambda: tmp_path)

    def stale(*args, **kwargs):
        raise RuntimeError("PIT index is stale; run server.py --refresh-index")

    monkeypatch.setattr(guard, "pitdb_config", lambda root: type("C", (), {"DB_PATH": tmp_path / "x"}))
    monkeypatch.setattr(guard, "require_fresh_index", stale)
    with pytest.raises(RuntimeError, match="stale"):
        server.earnings_8k_events(asof="2026-09-01T00:00:00Z")
