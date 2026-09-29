"""PEAD forecast-ledger hook (ZT add-on): mechanical rows, staging, publication, resolution.

The draft is published with ZT's own forecast ledger (extensions/pit_actor_sim/
forecast_ledger.py) into a temporary project, so every row passes the ledger's
v2/v2.1 validators and verifies as a hash-chained shard. Needs
INVESTMENT_AI_PROJECT_ROOT for the store (see conftest.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import conftest as C
import core
import pead_ledger as H
from src.quantlib import event_study as es


#: The scan publishes half an hour after its as-of (the ledger refuses late forecasts).
PUBLISHED = datetime(2026, 7, 29, 22, 30, tzinfo=timezone.utc)


def published_clock():
    return PUBLISHED


@pytest.fixture
def receipt(tmp_path):
    path = tmp_path / "pit_audit_receipt.json"
    path.write_text(json.dumps({"status": "PASS", "audited_at_utc": "2026-07-29T21:30:00+00:00",
                                "checks_passed": ["A1", "A11"]}), encoding="utf-8")
    return H.ledger_module().pit_audit_receipt_entry(path)


@pytest.fixture
def draft(installed, receipt):
    with core.store() as (con, provenance):
        return H.build_draft(con, provenance, asof=core.asof_utc(C.ASOF_ENTRY), pit_audit_receipt=receipt,
                             min_trials=10)


def test_rows_are_beta_binomial_posterior_means_from_counts(draft):
    rows = {r["decision_link"]["ticker"]: r for r in draft["rows"]}
    assert set(rows) == {"AAA", "BBB", "CCC", "DDD"}
    table = {r["group"]: r for r in draft["hit_table"]}
    for ticker, row in rows.items():
        bucket = row["decision_link"]["sue_bucket"]
        k, n = table[bucket]["successes"], table[bucket]["trials"]
        assert row["forecast"]["probability"] == pytest.approx((k + 1) / (n + 2))
        assert row["forecast"]["hit_counts"] == {"successes": k, "trials": n}
        pooled = table["POOLED"]
        assert row["forecast"]["baseline_forecast"]["probability"] == \
            pytest.approx((pooled["successes"] + 1) / (pooled["trials"] + 2))
        assert row["emitted_by"] == "mechanical-baseline"
        assert row["forecast"]["anchor_status"] == "base-rate-anchored"
        assert row["attribution"]["agent_model_id"] is None                # no model elicited anything
        spec = row["resolution_spec"]
        assert spec["origin_event_date"] == "2026-07-29" and spec["horizon_trading_days"] == 20
        assert spec["first_resolvable_utc"] == "2026-08-26T20:00:00Z"      # close of session +20
        assert all(kt <= C.ASOF_ENTRY for kt in row["evidence_lineage"]["knowledge_time"])
    pooled_trials = sum(r["trials"] for g, r in table.items() if g.startswith("Q"))
    assert table["POOLED"]["trials"] == pooled_trials
    skipped = {s["ticker"]: s["reason"] for s in draft["skipped"]}
    assert set(skipped) == {"EEE", "FFF"} and all("SUE" in v for v in skipped.values())


def test_the_draft_publishes_as_a_valid_verified_shard(draft, tmp_path):
    ledger = H.ledger_module()
    project = tmp_path / "Investment-AI-Drive-Research"
    project.mkdir()
    staged = H.stage(draft, root=tmp_path / "staging")
    assert staged["status"] == "STAGED" and staged["rows"] == 4
    receipt = H.publish(project, staged["path"], scratch_dir=tmp_path / "scratch", now=published_clock)
    assert receipt["status"] == "VALID" and receipt["pit_violations"] == []
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ledger.LatePredictionError):       # the same forecast a month later is refused
        H.publish(other, staged["path"], scratch_dir=tmp_path / "scratch2",
                  now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc))
    report = ledger.verify_project(project)
    assert report["ok"] and report["shards"] == 1 and report["rows"] == 4
    again = H.stage(draft, root=tmp_path / "staging")
    assert again["status"] == "NOTHING_STAGED" and again["already_staged"] == 4


def test_no_rows_once_the_drift_window_has_begun(installed, receipt):
    with core.store() as (con, provenance):
        late = H.build_draft(con, provenance, asof=core.asof_utc("2026-07-30T20:30:00Z"),
                             pit_audit_receipt=receipt, min_trials=10)
    assert late["rows"] == []
    reasons = {s["ticker"]: s["reason"] for s in late["skipped"]}
    assert "no longer a forecast" in reasons["AAA"]


def test_too_few_historical_events_means_no_row(installed, receipt):
    with core.store() as (con, provenance):
        thin = H.build_draft(con, provenance, asof=core.asof_utc(C.ASOF_ENTRY), pit_audit_receipt=receipt,
                             min_trials=10_000)
    assert thin["rows"] == [] and all("historical events" in s["reason"] or "SUE" in s["reason"]
                                      for s in thin["skipped"])


def test_resolution_recomputes_the_car_from_pitdb(draft, tmp_path):
    ledger = H.ledger_module()
    project = tmp_path / "Investment-AI-Drive-Research"
    project.mkdir()
    staged = H.stage(draft, root=tmp_path / "staging")
    H.publish(project, staged["path"], scratch_dir=tmp_path / "scratch", now=published_clock)
    asof = datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc)
    out = H.resolve(project, asof=asof, scratch_dir=tmp_path / "scratch")
    assert out["due"] == 4 and {o["status"] for o in out["outcomes"]} == {"RESOLVED"}
    loaded = ledger.load_project(project)
    resolutions = [row for _, row, _ in loaded.rows() if row["kind"] == "resolution"]
    targets = {row["id"]: row for _, row, _ in loaded.rows() if row["kind"] == "prediction"}
    with core.store() as (con, _):
        closes, _ = core.price_panel(con, core.asof_utc("2026-09-25T23:00:00Z"), ["AAA", "SPY"],
                                     pd.Timestamp("2025-05-01").date(), pd.Timestamp("2026-09-25").date())
        events = core.earnings_events(con, core.asof_utc("2026-09-25T23:00:00Z"), tickers=["AAA"])
    fresh = events[events["knowledge_time"] > pd.Timestamp("2026-07-01")].rename(columns={"accession": "event_id"})
    report = es.run_event_study(closes, fresh, benchmark="SPY", firm_col="ticker", windows=[(2, 20)], period_col=None)
    expected = float(report.events["car"].iloc[0])
    for res in resolutions:
        target = targets[res["target_id"]]
        if target["decision_link"]["ticker"] == "AAA":
            assert res["scoring"]["resolved_value"] == pytest.approx(expected, abs=1e-12)
            assert res["scoring"]["outcome"] == int(expected > 0)
            assert res["resolution"]["observation"]["rel_day"] == 20
    assert ledger.rescore_from_shards(project)["ok"]


def test_the_car_reader_leaves_other_sources_alone():
    ledger = H.ledger_module()
    with pytest.raises(ledger.UnsupportedSource):
        H.CarReader().observations("pitdb:obs/FRED:PAYEMS", "value_num", None, None,
                                   datetime(2026, 1, 1, tzinfo=timezone.utc))
