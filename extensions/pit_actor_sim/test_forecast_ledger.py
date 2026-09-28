"""Tests for the forecast ledger (ZT add-on; v2 + v2.1 chaining amendment).

Run from the repository root with VT's ``agent`` directory importable::

    VIBE_TRADING_HOME=<E: runtime> PYTHONPATH=agent python -m pytest extensions/pit_actor_sim/test_forecast_ledger.py
    (or: cd extensions/pit_actor_sim && python -m unittest test_forecast_ledger)

Temporary projects and scratch live under VIBE_TRADING_HOME, never the system
TEMP, so the suite honours the E:-only storage rule on Windows.
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import forecast_ledger as fl
from src.governance.ledger import GENESIS_PREV_HASH, verify_chain, verify_export
from src.quantlib import scoring

UTC = timezone.utc
PUBLISH_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
RUN_ASOF = "2026-08-28T12:00:00Z"
PROJECT_NAME = "Investment-AI-Drive-Research"
BRENT = "pitdb:obs/TEST:BRENT_SETTLE"
SPY = "pitdb:price/42"
BINS = {"oil_down": [None, 70], "oil_range": [70, 95], "oil_spike_moderate": [95, 120], "oil_spike_severe": [120, None]}

# Numbers copied from market_actor_sim/runs/iran_oil_pit_pilot_20260828/pilot_result.json
# (a historical run): the ledger must reproduce the simulator's own Wilson
# half-widths before it will record the frequencies.
PILOT_RESULT = {
    "run_id": "6f3f0ddc308e1129a02bb8a0",
    "authority": "RESEARCH_PILOT_ONLY_UNCALIBRATED",
    "run_stamp": {
        "scenario_sha256": "65b8abac322ecc65043bc062dace466755df0836f02c11c9dfbbaae35d0a8efb",
        "forks_sha256": "e7b9e3a365c6ca56e50ecb29a5f01d7bd05d425ff1bafd751ebdf1381687da76",
        "evidence_sha256": "905b3abd03d74521e449d2c32d558a3e6175b2876d278196486a7bffd7c6eda4",
        "seed": 20260828,
        "n_rollouts": 300000,
    },
    "validation": {"pit_audit": "PASS", "crosscheck_status": "PASS"},
    "ensemble_exact": {"oil_down": 0.133365, "oil_range": 0.5765893333333333,
                       "oil_spike_moderate": 0.26534430666666664, "oil_spike_severe": 0.024701360000000002},
    "ensemble_mc": {"oil_down": 0.13385333333333332, "oil_range": 0.5768133333333333,
                    "oil_spike_moderate": 0.26454333333333335, "oil_spike_severe": 0.02479},
    "mc_wilson_95": {
        "oil_down": {"p": 0.13385333333333332, "wilson_halfwidth_95": 0.0012184457383627872},
        "oil_range": {"p": 0.5768133333333333, "wilson_halfwidth_95": 0.0017679759946954672},
        "oil_spike_moderate": {"p": 0.26454333333333335, "wilson_halfwidth_95": 0.0015784121343444097},
        "oil_spike_severe": {"p": 0.02479, "wilson_halfwidth_95": 0.0005564241512653706},
    },
    "model_form_range": {"oil_down": {"min": 0.102411, "max": 0.174132}},
    "leverage_plus_0_05": [
        {"state_key": "", "actor": "us_administration", "action_plus_0_05": "strike_iran",
         "delta_expected_reward": 0.009210679284954837},
        {"state_key": "", "actor": "us_administration", "action_plus_0_05": "negotiate",
         "delta_expected_reward": -0.01173356533839398},
    ],
    "limitations": ["No resolved outcomes are available, so calibration and backtest gates remain open."],
}

EVIDENCE = {
    "snapshot_id": "iran_oil_pit_pilot_20260828T120000",
    "asof_utc_naive": "2026-08-28T12:00:00",
    "admitted_observations": [
        {"series_id": "POLY:us-x-iran-ceasefire-continues-through-september-30:Yes", "value_num": 0.795,
         "event_time": "2026-08-28T11:42:10.794152", "knowledge_time": "2026-08-28T11:42:10.794152",
         "pit_class": "OBSERVED_PIT", "source_id": "polymarket"},
    ],
    "admitted_prices": [
        {"ticker": "USO", "event_date": "2026-08-27", "knowledge_time": "2026-08-28T11:40:48.358918",
         "close": 130.009995, "source_id": "yahoo_eod", "pit_class": "OBSERVED_PIT"},
    ],
}


class Clock:
    """Injectable wall clock."""

    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class FakeReader:
    """In-memory as-of reader with the same contract as pitdb's obs_asof/price_asof."""

    name = "fake-asof/v1"

    def __init__(self, series: dict[str, list[tuple]]):
        # series: {source_id: [(event_date, value, knowledge_time_iso, revision_seq, pit_class), ...]}
        self.series = series
        self.calls = []

    def observations(self, source_or_series_id, field, start, end, asof_utc):
        self.calls.append((source_or_series_id, field, start, end, asof_utc))
        latest = {}
        for event, value, kt, rev, pit in self.series.get(source_or_series_id, []):
            day = date.fromisoformat(event)
            known = fl.parse_utc(kt)
            if not start <= day <= end or known > asof_utc:
                continue
            if day not in latest or (known, rev) > (latest[day][2], latest[day][3]):
                latest[day] = (day, value, known, rev, pit)
        return [fl.Observation(event_date=day, value=value, knowledge_time=known, revision_seq=rev,
                               source_id="test_source", pit_class=pit,
                               record={"event_date": day.isoformat(), field: value,
                                       "knowledge_time": fl.iso_utc(known), "revision_seq": rev})
                for day, value, known, rev, pit in sorted(latest.values(), key=lambda item: item[0])]


def setUpModule():
    home = os.environ.get("VIBE_TRADING_HOME", "").strip()
    if not home:
        raise RuntimeError("VIBE_TRADING_HOME must be set to the E: runtime before running these tests "
                           "(ZT 2026-09-28: everything on E:)")
    if os.name == "nt" and fl.drive_violation(Path(home).resolve(), "VIBE_TRADING_HOME", require="E:"):
        raise RuntimeError(f"VIBE_TRADING_HOME must be on E: for these tests; got {home}")


class LedgerCase(unittest.TestCase):
    """Fresh project, scratch and PIT receipt per test."""

    def setUp(self):
        base = Path(os.environ["VIBE_TRADING_HOME"])
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="ledger-test-", dir=base))
        self.project = self.tmp / "work" / PROJECT_NAME
        self.project.mkdir(parents=True)
        self.scratch = self.tmp / "scratch"
        receipt = self.tmp / "pit_audit_receipt.json"
        receipt.write_text(json.dumps({"status": "PASS", "audited_at_utc": "2026-08-28T11:55:00+00:00"}),
                           encoding="utf-8")
        self.receipt = fl.pit_audit_receipt_entry(receipt)
        self.clock = Clock(PUBLISH_AT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def spec(self, **overrides):
        estimand = {"event": "brent_front_month_settle_bin", "horizon": "2026-12-31", "unit": "USD/bbl",
                    "denominator": "probability", "run_asof_utc": RUN_ASOF}
        spec = {
            "project": PROJECT_NAME,
            "decision_id": "gate0:iran-oil-long-2026-08-28",
            "decision_link": {"decision_id": "gate0:iran-oil-long-2026-08-28",
                              "branch": "long-oil sizing by terminal crude regime"},
            "claim_subject": f"Brent front-month settlement ({BRENT}) on or before 2026-12-31",
            "resolution_spec": {
                "resolver": "pitdb-asof/v1", "source_or_series_id": BRENT, "field": "value_num",
                "operator": "in_set", "threshold_or_categories": {"categories": BINS}, "unit": "USD/bbl",
                "timezone": "America/New_York", "first_resolvable_utc": "2026-12-31T22:00:00Z",
                "vintage_policy": "first_release", "missing_policy": "void_with_reason", "grace_period": "P7D",
                "observation_rule": "last_on_or_before", "event_date": "2026-12-31",
            },
            "pit_audit_receipt": self.receipt,
            "action_set_hash": "sha256:" + "a" * 64,
            "attention_minutes": 12,
            "compute_seconds": 41.5,
            "estimated_cost_usd": 0,
            "claim_estimand": estimand,
            "market_anchor": {"estimand": dict(estimand), "instrument": "BZ options-implied bins (fixture)",
                              "observed_utc": "2026-08-28T11:40:00Z",
                              "probabilities": {"oil_down": 0.2, "oil_range": 0.5, "oil_spike_moderate": 0.25,
                                                "oil_spike_severe": 0.05}},
            "leverage_series_map": {
                "|us_administration|strike_iran": {
                    "resolution_spec": {
                        "resolver": "pitdb-asof/v1", "source_or_series_id": "pitdb:obs/TEST:POLY_STRIKE",
                        "field": "value_num", "operator": ">", "threshold_or_categories": 0.5, "unit": "probability",
                        "timezone": "UTC", "first_resolvable_utc": "2026-10-01T00:00:00Z",
                        "vintage_policy": "first_release", "missing_policy": "resolve_missing",
                        "grace_period": "P2D", "observation_rule": "last_on_or_before", "event_date": "2026-09-30"},
                    "monitor": {"monitorability": "READY", "series_or_query_id": "pitdb:obs/TEST:POLY_STRIKE",
                                "trigger": {"operator": ">", "threshold": 0.5}, "cadence": "daily",
                                "owner": "forecast_ledger.resolve_due"},
                },
            },
        }
        spec.update(overrides)
        return spec

    def mct_draft(self, **overrides):
        return fl.mct_draft(PILOT_RESULT, self.spec(**overrides), evidence=EVIDENCE)

    def publish(self, draft, **kwargs):
        kwargs.setdefault("scratch_dir", self.scratch)
        kwargs.setdefault("now", self.clock)
        return fl.publish_draft(self.project, draft, **kwargs)

    def distribution_draft(self, origin="2026-09-25", horizons=(2, 5), run_asof="2026-09-26T00:00:00Z"):
        levels = [k / 100 for k in range(1, 100)]
        z = [math.sqrt(2) * _erfinv(2 * t - 1) for t in levels]
        quantiles = {h: [500.0 * math.exp(0.01 * math.sqrt(h) * zi) for zi in z] for h in horizons}
        baseline = {h: {"kind": "random_walk_wide", "levels": levels,
                        "values": [500.0 * math.exp(0.03 * math.sqrt(h) * zi) for zi in z]} for h in horizons}
        rows = fl.distribution_rows(
            claim_subject="SPY close (sec_id 42)", source_or_series_id=SPY, field="close", unit="USD",
            origin_event_date=origin, levels=levels, quantiles_by_horizon=quantiles,
            emitted_by="mechanical-baseline", forecast_method_id="random_walk_lognormal_1pct",
            decision_link={"decision_id": "reference", "branch": "none: mechanical reference forecast"},
            dependence_group=f"spy-rw:{origin}", baseline_by_horizon=baseline,
            evidence_lineage={"knowledge_time": ["2026-09-25T21:05:00Z"], "pit_class": "OBSERVED_PIT"})
        manifest = {"project": PROJECT_NAME, "decision_id": "reference:spy-random-walk", "mode": "baseline",
                    "run_asof_utc": run_asof, "pit_audit_receipt": self.receipt,
                    "attention_minutes": 0, "compute_seconds": 0.2, "estimated_cost_usd": 0}
        return {"run_manifest": manifest, "rows": rows}

    def shard_paths(self):
        return sorted((self.project / "_ledger" / "runs").glob("*.jsonl"))


def _erfinv(y: float) -> float:
    """Inverse error function by Newton iterations (keeps the fixture scipy-free)."""
    x = 0.0
    for _ in range(100):
        err = math.erf(x) - y
        x -= err / (2 / math.sqrt(math.pi) * math.exp(-x * x))
    return x


class ChainAndPublicationTest(LedgerCase):
    def test_published_shard_verifies_with_vt_and_carries_the_mct_contract(self):
        receipt = self.publish(self.mct_draft())
        path = Path(receipt["path"])
        self.assertEqual(path.parent, self.project / "_ledger" / "runs")
        self.assertTrue(verify_chain(path).ok)  # VT's own verifier, unmodified
        shard = fl.verify_shard_file(path)
        self.assertEqual(shard.prev_shard_hash, GENESIS_PREV_HASH)
        self.assertEqual(shard.shard_seq, 1)
        self.assertEqual(shard.terminal_hash, receipt["terminal_hash"])
        self.assertEqual(receipt["status"], "VALID")
        predictions = [r for r in shard.rows if r["kind"] == "prediction"]
        indicators = [r for r in shard.rows if r["kind"] == "indicator"]
        self.assertEqual(len(predictions), 4)
        self.assertEqual(len(indicators), 1)
        unmapped = shard.manifest["extensions"]["leverage_nodes_unmapped"]
        self.assertEqual([u["node"] for u in unmapped], ["|us_administration|negotiate"])
        for row in predictions:
            forecast = row["forecast"]
            lo, hi = forecast["mc_wilson_95"]
            self.assertLess(lo, forecast["probability"])
            self.assertLess(forecast["probability"], hi)
            self.assertEqual(forecast["anchor_status"], "market-anchored")
            self.assertEqual(forecast["anchor_parity"], "PARITY_OK")
            self.assertEqual(forecast["baseline_forecast"]["kind"], "market_implied")
            self.assertAlmostEqual(forecast["edge_vs_anchor"],
                                   forecast["probability"] - forecast["baseline_forecast"]["probability"])
            self.assertEqual(row["dependence_group"], "mct:6f3f0ddc308e1129a02bb8a0")
        self.assertTrue(verify_export(Path(receipt["export_receipt"])).ok)
        self.assertEqual(shard.manifest["vt_run_manifest"]["extra"]["seed"], 20260828)
        self.assertTrue(fl.verify_project(self.project, self.scratch / "receipts" / PROJECT_NAME)["ok"])

    def test_anchor_mismatch_reports_no_spread(self):
        spec = self.spec()
        spec["market_anchor"]["estimand"]["horizon"] = "2026-11-30"
        draft = fl.mct_draft(PILOT_RESULT, spec, evidence=EVIDENCE)
        for row in (r for r in draft["rows"] if r["kind"] == "prediction"):
            self.assertEqual(row["forecast"]["anchor_parity"], "ANCHOR_MISMATCH")
            self.assertIsNone(row["forecast"]["edge_vs_anchor"])
            self.assertEqual(row["forecast"]["baseline_forecast"]["kind"], "uniform_prior")
            self.assertEqual(row["forecast"]["anchor_status"], "elicited-only")

    def test_irreproducible_wilson_interval_is_refused(self):
        result = copy.deepcopy(PILOT_RESULT)
        result["mc_wilson_95"]["oil_down"]["wilson_halfwidth_95"] = 0.002
        with self.assertRaisesRegex(fl.SchemaError, "does not reproduce"):
            fl.mct_draft(result, self.spec(), evidence=EVIDENCE)

    def test_next_shard_links_to_the_previous_terminal_hash(self):
        first = self.publish(self.mct_draft())
        self.clock.moment += timedelta(minutes=5)
        second = self.publish(self.distribution_draft())
        shard = fl.verify_shard_file(second["path"])
        self.assertEqual(shard.prev_shard_hash, first["terminal_hash"])
        self.assertEqual(shard.shard_seq, 2)
        ledger = fl.load_project(self.project)
        self.assertEqual([h.run_id for h in ledger.heads], [second["run_id"]])
        self.assertFalse(ledger.fork)

    def test_every_single_byte_edit_breaks_verification(self):
        draft = self.distribution_draft(horizons=(2,))
        draft["rows"][0]["forecast"]["numeric_distribution"]["levels"] = [0.05, 0.5, 0.95]
        draft["rows"][0]["forecast"]["numeric_distribution"]["values"] = [490.0, 500.0, 510.0]
        draft["rows"][0]["forecast"]["baseline_forecast"] = None
        path = Path(self.publish(draft)["path"])
        original = path.read_bytes()
        probe_dir = self.tmp / "probe"
        probe_dir.mkdir()
        probe = probe_dir / path.name
        survivors = []
        for position in range(len(original)):
            for replacement in {original[position] ^ 0x01, 0x20}:
                if replacement == original[position]:
                    continue
                edited = bytearray(original)
                edited[position] = replacement
                probe.write_bytes(bytes(edited))
                try:
                    fl.verify_shard_file(probe)
                except fl.ShardInvalid:
                    continue
                survivors.append(position)
        self.assertEqual(survivors, [], "a byte edit passed verification")
        probe.write_bytes(original)
        fl.verify_shard_file(probe)  # the unedited copy still verifies

    def test_whitespace_key_order_and_truncation_edits_are_detected(self):
        path = Path(self.publish(self.mct_draft())["path"])
        original = path.read_text(encoding="utf-8")
        lines = original.split("\n")[:-1]
        record = json.loads(lines[1])
        reordered = json.dumps(dict(reversed(list(record.items()))), ensure_ascii=False)
        edits = {
            "insert_space": original.replace('"kind": ', '"kind":  ', 1),
            "key_order": "\n".join([lines[0], reordered, *lines[2:]]) + "\n",
            "crlf": original.replace("\n", "\r\n"),
            "no_final_newline": original[:-1],
            "truncated_last_row": "\n".join(lines[:-1]) + "\n",
            "swapped_rows": "\n".join([lines[0], lines[2], lines[1], *lines[3:]]) + "\n",
            "escaped_unicode": original.replace("-", "\\u002d", 1),
            "trailing_blank_line": original + "\n",
        }
        for name, text in edits.items():
            with self.subTest(edit=name):
                self.assertNotEqual(text, original)
                path.write_bytes(text.encode("utf-8"))
                with self.assertRaises(fl.ShardInvalid):
                    fl.verify_shard_file(path)
                with self.assertRaises(fl.ChainError):
                    fl.load_project(self.project)
        path.write_text(original, encoding="utf-8")
        fl.load_project(self.project)

    def test_a_fully_rechained_edit_breaks_the_link_from_the_next_shard(self):
        first = Path(self.publish(self.mct_draft())["path"])
        self.clock.moment += timedelta(minutes=5)
        self.publish(self.distribution_draft())
        payloads = []
        for line in first.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            payloads.append({k: v for k, v in record.items() if k not in fl.CHAIN_FIELDS})
        payloads[1]["forecast"]["probability"] = 0.5  # rewrite history, then re-hash consistently
        lines, _ = fl.chain_payloads(payloads)
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
        fl.verify_shard_file(first)  # internally consistent on its own ...
        with self.assertRaisesRegex(fl.ChainError, "missing shard"):
            fl.load_project(self.project)  # ... but the next shard no longer links to it

    def test_run_id_and_path_collisions_are_hard_failures(self):
        draft = self.mct_draft()
        draft["run_manifest"]["run_id"] = "01TESTCOLLISION0000000000A"
        receipt = self.publish(draft)
        before = Path(receipt["path"]).read_bytes()
        self.clock.moment += timedelta(minutes=1)
        with self.assertRaises(fl.ShardCollisionError):
            self.publish(draft)
        self.assertEqual(Path(receipt["path"]).read_bytes(), before)
        planted = self.project / "_ledger" / "runs" / "01TESTPLANTED00000000000AB.jsonl"
        planted.write_text("not a shard\n", encoding="utf-8")
        other = self.mct_draft()
        other["run_manifest"]["run_id"] = "01TESTPLANTED00000000000AB"
        with self.assertRaises(fl.ShardCollisionError):
            self.publish(other)
        self.assertEqual(planted.read_text(encoding="utf-8"), "not a shard\n")
        with self.assertRaises(fl.SchemaError):
            bad = self.mct_draft()
            bad["run_manifest"]["run_id"] = "../escape"
            self.publish(bad)

    def test_prediction_written_after_its_resolution_date_is_rejected(self):
        self.clock.moment = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)  # after 2026-12-31T22:00Z
        with self.assertRaises(fl.LatePredictionError):
            self.publish(self.mct_draft())
        self.assertEqual(self.shard_paths(), [])
        self.assertFalse((self.scratch / PROJECT_NAME).exists())
        # A forged earlier emitted_utc does not help: the publication clock governs.
        draft = self.mct_draft()
        for row in draft["rows"]:
            row["emitted_utc"] = "2026-09-01T00:00:00Z"
        with self.assertRaises(fl.SchemaError):
            self.publish(draft)
        self.assertEqual(self.shard_paths(), [])

    def test_evidence_after_the_cutoff_marks_the_run_invalid_pit(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["admitted_observations"][0]["knowledge_time"] = "2026-08-28T12:00:01"
        receipt = self.publish(fl.mct_draft(PILOT_RESULT, self.spec(), evidence=evidence))
        self.assertEqual(receipt["status"], "INVALID_PIT")
        self.assertTrue(any("after run_asof_utc" in v["reason"] for v in receipt["pit_violations"]))
        self.clock.moment += timedelta(minutes=1)
        no_receipt = self.publish(self.mct_draft(pit_audit_receipt=None))
        self.assertEqual(no_receipt["status"], "INVALID_PIT")

    def test_contract_refusals(self):
        with self.assertRaisesRegex(fl.SchemaError, "gap or overlap"):
            bins = dict(BINS, oil_range=[71, 95])
            spec = self.spec()
            spec["resolution_spec"]["threshold_or_categories"] = {"categories": bins}
            fl.mct_draft(PILOT_RESULT, spec, evidence=EVIDENCE)
        draft = self.distribution_draft(horizons=(2,))
        draft["rows"][0]["scoring"]["rule"] = "brier_binary"
        with self.assertRaisesRegex(fl.SchemaError, "not valid for a continuous claim"):
            self.publish(draft)
        draft = self.mct_draft()
        draft["rows"][0]["id"] = "forged"
        with self.assertRaisesRegex(fl.SchemaError, "stamps it"):
            self.publish(draft)
        draft = self.mct_draft()
        draft["rows"].append({"kind": "resolution", "emitted_by": "human", "claim": "x", "claim_type": "binary",
                              "scoring": {"rule": "brier_binary"}, "attribution": {"forecast_method_id": "x"},
                              "resolution": {"status": "RESOLVED"}})
        with self.assertRaises(fl.SchemaError):
            self.publish(draft)
        draft = self.mct_draft()
        del draft["rows"][0]["resolution_spec"]["grace_period"]
        with self.assertRaisesRegex(fl.SchemaError, "grace_period"):
            self.publish(draft)
        self.assertEqual(self.shard_paths(), [])


class ResolutionTest(LedgerCase):
    def brent_reader(self, value=101.3, knowledge="2026-12-31T22:30:00Z", revision=0):
        return FakeReader({
            BRENT: [("2026-12-30", 99.0, "2026-12-30T22:30:00Z", 0, "OBSERVED_PIT"),
                    ("2026-12-31", value, knowledge, revision, "OBSERVED_PIT")],
            "pitdb:obs/TEST:POLY_STRIKE": [("2026-09-30", 0.31, "2026-09-30T23:00:00Z", 0, "OBSERVED_PIT")],
        })

    def test_scores_reproduce_from_the_shards_alone(self):
        self.publish(self.mct_draft())
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        report = fl.resolve_due(self.project, reader=self.brent_reader(), scratch_dir=self.scratch, now=self.clock)
        self.assertEqual({o["status"] for o in report["outcomes"]}, {"RESOLVED"})
        self.assertEqual(report["due"], 5)
        resolution_shard = fl.verify_shard_file(report["published"]["path"])
        self.assertEqual(resolution_shard.manifest["mode"], "resolution")
        self.assertEqual(resolution_shard.shard_seq, 2)

        # Independent recomputation straight from the JSONL files, no ledger code.
        rows = {}
        for path in self.shard_paths():
            for line in path.read_text(encoding="utf-8").splitlines()[1:]:
                row = json.loads(line)
                rows[row["id"]] = row
        resolutions = [r for r in rows.values() if r["kind"] == "resolution"]
        by_outcome = {}
        for res in resolutions:
            target = rows[res["target_id"]]
            self.assertEqual(res["target_record_hash"], target["record_hash"])
            if target["kind"] != "prediction":
                self.assertEqual(res["scoring"]["outcome"], 0)  # 0.31 is not > 0.5
                continue
            (name, (lo, hi)), = target["resolution_spec"]["threshold_or_categories"]["categories"].items()
            outcome = int((lo is None or 101.3 >= lo) and (hi is None or 101.3 < hi))
            p = target["forecast"]["probability"]
            p_market = target["forecast"]["baseline_forecast"]["probability"]
            self.assertEqual(res["scoring"]["outcome"], outcome)
            self.assertEqual(res["scoring"]["raw_score"], (p - outcome) ** 2)
            self.assertEqual(res["scoring"]["baseline_score"], (p_market - outcome) ** 2)
            self.assertAlmostEqual(res["scoring"]["skill_score"],
                                   1 - (p - outcome) ** 2 / (p_market - outcome) ** 2, places=12)
            by_outcome[name] = outcome
        self.assertEqual(by_outcome, {"oil_down": 0, "oil_range": 0, "oil_spike_moderate": 1, "oil_spike_severe": 0})
        rescored = fl.rescore_from_shards(self.project)
        self.assertTrue(rescored["ok"], rescored)
        self.assertEqual(rescored["checked"], 5)
        # The multi-category Brier of the terminal distribution equals the sum of its marginals.
        marginal_sum = sum(r["scoring"]["raw_score"] for r in resolutions if rows[r["target_id"]]["kind"] == "prediction")
        self.assertAlmostEqual(marginal_sum,
                               scoring.brier_score_multiclass([PILOT_RESULT["ensemble_mc"]], ["oil_spike_moderate"]))
        # A second pass finds nothing left to resolve.
        again = fl.resolve_due(self.project, reader=self.brent_reader(), scratch_dir=self.scratch, now=self.clock)
        self.assertEqual(again["due"], 0)
        self.assertIsNone(again["published"])

    def test_index_rebuild_is_byte_identical(self):
        self.publish(self.mct_draft())
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        fl.resolve_due(self.project, reader=self.brent_reader(), scratch_dir=self.scratch, now=self.clock)
        index_dir = self.project / "_ledger" / "index"
        first = {p.name: p.read_bytes() for p in sorted(index_dir.iterdir())}
        self.assertEqual(sorted(first), sorted(fl.INDEX_FILES))
        fl.rebuild_index(self.project)
        self.assertEqual({p.name: p.read_bytes() for p in sorted(index_dir.iterdir())}, first)
        shutil.rmtree(index_dir)
        fl.rebuild_index(self.project)
        self.assertEqual({p.name: p.read_bytes() for p in sorted(index_dir.iterdir())}, first)
        manifest = json.loads(first["INDEX_MANIFEST.json"])
        for name, digest in manifest["files"].items():
            self.assertEqual(fl.sha256_bytes(first[name]), digest)
        board = json.loads(first["scoreboard.json"])
        group = next(g for g in board["groups"] if g["claim_type"] == "binary")
        self.assertEqual((group["n_scored"], group["n_independent"]), (4, 1))
        self.assertEqual(group["display"], "UNSCORED_LT5_INDEPENDENT")
        opened = first["open.jsonl"].decode("utf-8")
        self.assertEqual(opened, "")

    def test_pending_then_missing_policy_after_grace(self):
        self.publish(self.mct_draft())
        empty = FakeReader({})
        # Predictions are inside their grace period (2026-12-31T22:00Z + P7D); the
        # indicator's grace ended 2026-10-03 and its policy is resolve_missing.
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        first = fl.resolve_due(self.project, reader=empty, scratch_dir=self.scratch, now=self.clock)
        statuses = {o["target_id"].rsplit("-", 1)[1]: o["status"] for o in first["outcomes"]}
        self.assertEqual(statuses, {"1": "PENDING", "2": "PENDING", "3": "PENDING", "4": "PENDING", "5": "MISSING"})
        missing = fl.verify_shard_file(first["published"]["path"]).rows
        self.assertEqual([r["resolution"]["status"] for r in missing], ["MISSING"])
        self.clock.moment = datetime(2027, 1, 8, 0, 0, tzinfo=UTC)
        late = fl.resolve_due(self.project, reader=empty, scratch_dir=self.scratch, now=self.clock)
        statuses = {o["target_id"].rsplit("-", 1)[1]: o["status"] for o in late["outcomes"]}
        self.assertEqual(statuses, {"1": "VOID", "2": "VOID", "3": "VOID", "4": "VOID"})  # void_with_reason
        shard = fl.verify_shard_file(late["published"]["path"])
        self.assertTrue(all(r["scoring"]["raw_score"] is None for r in shard.rows))
        board = json.loads((self.project / "_ledger" / "index" / "scoreboard.json").read_text(encoding="utf-8"))
        binary = next(g for g in board["groups"] if g["claim_type"] == "binary")
        self.assertEqual((binary["n_scored"], binary["n_void"]), (0, 4))

    def test_a_legacy_v1_row_is_resolved_by_a_v21_shard(self):
        v1_line = b'{"id": "01V1LEGACYROW-1", "kind": "prediction", "stated_probability": 0.8}'
        draft = {
            "run_manifest": {"project": PROJECT_NAME, "decision_id": "legacy:v1-migration", "mode": "resolution",
                             "run_asof_utc": "2026-09-28T00:00:00Z", "attention_minutes": 3,
                             "compute_seconds": 0, "estimated_cost_usd": 0},
            "rows": [{"kind": "resolution", "legacy_schema": "zt-forecast-ledger/1", "target_id": "01V1LEGACYROW-1",
                      "target_record_hash": fl.sha256_bytes(v1_line), "emitted_by": "human",
                      "claim": "Resolution of V1 row 01V1LEGACYROW-1", "claim_type": "binary",
                      "attribution": {"forecast_method_id": "legacy-v1"},
                      "resolution": {"status": "RESOLVED", "reason": None},
                      "scoring": {"rule": "brier_binary", "resolved_value": 1, "outcome": 1, "raw_score": 0.04,
                                  "baseline_score": None, "skill_score": None,
                                  "date_scored_utc": "2026-09-28T12:00:00Z"}}],
        }
        self.publish(draft)
        result = fl.rescore_from_shards(self.project)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["checked"], result["legacy_v1_skipped"]), (0, 1))

    def test_invalid_pit_runs_are_resolved_but_never_scored_as_skill(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["admitted_observations"][0]["knowledge_time"] = "2026-08-28T12:00:01"
        self.assertEqual(self.publish(fl.mct_draft(PILOT_RESULT, self.spec(), evidence=evidence))["status"],
                         "INVALID_PIT")
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        fl.resolve_due(self.project, reader=self.brent_reader(), scratch_dir=self.scratch, now=self.clock)
        board = json.loads((self.project / "_ledger" / "index" / "scoreboard.json").read_text(encoding="utf-8"))
        binary = next(g for g in board["groups"] if g["claim_type"] == "binary")
        self.assertEqual((binary["n_scored"], binary["n_excluded_run_status"]), (0, 4))
        self.assertIsNone(binary["aggregate_skill"])

    def test_superseded_first_release_and_forecast_after_outcome_are_voided(self):
        self.publish(self.mct_draft())
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        revised = self.brent_reader(revision=1)
        report = fl.resolve_due(self.project, reader=revised, scratch_dir=self.scratch, now=self.clock, publish=False)
        prediction_statuses = [o for o in report["outcomes"] if o["target_id"].endswith(("-1", "-2", "-3", "-4"))]
        self.assertTrue(all(o["status"] == "VOID" and "superseded" in o["reason"] for o in prediction_statuses))
        known_early = self.brent_reader(knowledge="2026-08-28T11:00:00Z")
        report = fl.resolve_due(self.project, reader=known_early, scratch_dir=self.scratch, now=self.clock,
                                publish=False)
        prediction_statuses = [o for o in report["outcomes"] if o["target_id"].endswith(("-1", "-2", "-3", "-4"))]
        self.assertTrue(all(o["status"] == "VOID" and "knowable" in o["reason"] for o in prediction_statuses))

    def test_a_reader_leaking_future_values_is_refused(self):
        self.publish(self.mct_draft())
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)

        class Leaky(FakeReader):
            def observations(self, *args):
                rows = super().observations(*args)
                return [fl.Observation(o.event_date, o.value, o.knowledge_time + timedelta(days=30), 0,
                                       o.source_id, o.pit_class, o.record) for o in rows]

        with self.assertRaisesRegex(fl.LedgerError, "known after the cutoff"):
            fl.resolve_due(self.project, reader=Leaky(self.brent_reader().series), scratch_dir=self.scratch,
                           now=self.clock, publish=False)

    def test_distribution_rows_score_with_crps_pit_and_coverage(self):
        self.clock.moment = datetime(2026, 9, 26, 1, 0, tzinfo=UTC)
        receipt = self.publish(self.distribution_draft(horizons=(1, 5)))
        shard = fl.verify_shard_file(receipt["path"])
        self.assertEqual([r["resolution_spec"]["horizon_trading_days"] for r in shard.rows], [1, 5])
        self.assertEqual([r["resolution_spec"]["first_resolvable_utc"] for r in shard.rows],
                         ["2026-09-28T00:00:00Z", "2026-10-02T00:00:00Z"])
        # Trading days are counted on the series: the weekend is skipped by construction.
        closes = [("2026-09-25", 500.0), ("2026-09-28", 503.0), ("2026-09-29", 498.0), ("2026-09-30", 501.0),
                  ("2026-10-01", 507.0), ("2026-10-02", 512.0)]
        reader = FakeReader({SPY: [(d, v, f"{d}T21:00:00Z", 0, "OBSERVED_PIT") for d, v in closes]})
        self.clock.moment = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        report = fl.resolve_due(self.project, reader=reader, scratch_dir=self.scratch, now=self.clock)
        self.assertEqual([o["status"] for o in report["outcomes"]], ["RESOLVED", "RESOLVED"])
        res = fl.verify_shard_file(report["published"]["path"]).rows
        h1, h5 = shard.rows
        self.assertEqual(res[0]["scoring"]["resolved_value"], 503.0)
        self.assertEqual(res[1]["scoring"]["resolved_value"], 512.0)
        for target, resolution in ((h1, res[0]), (h5, res[1])):
            grid = target["forecast"]["numeric_distribution"]
            y = resolution["scoring"]["resolved_value"]
            self.assertEqual(resolution["scoring"]["raw_score"],
                             scoring.crps_from_quantiles(grid["levels"], grid["values"], y))
            diagnostics = resolution["scoring"]["diagnostics"]
            self.assertEqual(diagnostics["pit"], scoring.pit_values(grid["levels"], grid["values"], y))
            self.assertIn("0.90", diagnostics["central_interval_hit"])
            self.assertIn("0.99", diagnostics["tail_exceedance"])
            self.assertIsNotNone(resolution["scoring"]["skill_score"])
        self.assertTrue(fl.rescore_from_shards(self.project)["ok"])


class PitdbReaderTest(unittest.TestCase):
    """The default reader maps the extension's own pit_* tool rows (no warehouse needed)."""

    def test_rows_from_the_approved_pit_tools_become_observations(self):
        import server
        from unittest.mock import patch

        calls = []

        def series(series_id, asof, start_date, end_date, limit=250):
            calls.append(("obs", series_id, asof, start_date, end_date, limit))
            return {"rows": [{"series_id": series_id, "event_time": "2026-12-31T00:00:00", "value_num": 101.3,
                              "value_str": None, "knowledge_time": "2026-12-31T22:30:00", "revision_seq": 0,
                              "source_id": "eia", "pit_class": "TRUE_PIT"}]}

        def price(sec_id, asof, start_date, end_date, limit=250):
            calls.append(("price", sec_id, asof, start_date, end_date, limit))
            return {"rows": [{"sec_id": sec_id, "event_date": "2026-09-28", "close": 503.0,
                              "knowledge_time": "2026-09-28T21:00:00", "revision_seq": 1,
                              "source_id": "yahoo_eod", "pit_class": "OBSERVED_PIT"}]}

        asof = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        reader = fl.PitdbReader()
        with patch.object(server, "pit_series_history", series), patch.object(server, "pit_price_history", price):
            (obs,) = reader.observations("pitdb:obs/EIA:BRENT", "value_num", date(2026, 12, 21), date(2026, 12, 31), asof)
            (bar,) = reader.observations("pitdb:price/42", "close", date(2026, 9, 26), date(2026, 10, 9), asof)
            with self.assertRaises(fl.UnsupportedSource):
                reader.observations("fred:DCOILBRENTEU", "value", date(2026, 1, 1), date(2026, 1, 2), asof)
            with self.assertRaises(fl.UnsupportedSource):
                reader.observations("pitdb:price/42", "vwap", date(2026, 9, 26), date(2026, 10, 9), asof)
        self.assertEqual(calls[0], ("obs", "EIA:BRENT", "2027-01-02T09:00:00Z", "2026-12-21", "2026-12-31", 1000))
        self.assertEqual(calls[1][:2], ("price", 42))
        self.assertEqual((obs.event_date, obs.value, obs.revision_seq, obs.pit_class),
                         (date(2026, 12, 31), 101.3, 0, "TRUE_PIT"))
        self.assertEqual(obs.knowledge_time, datetime(2026, 12, 31, 22, 30, tzinfo=UTC))  # naive = UTC
        self.assertEqual((bar.event_date, bar.value, bar.revision_seq), (date(2026, 9, 28), 503.0, 1))


class ConcurrencyTest(LedgerCase):
    def test_fork_blocks_publication_until_merged(self):
        first = self.publish(self.mct_draft())
        self.clock.moment += timedelta(minutes=1)
        self.publish(self.distribution_draft())
        # A second machine published against the same parent before Drive synced.
        manifest, rows = fl._prepare(
            {k: v for k, v in self.distribution_draft()["run_manifest"].items()}, self.distribution_draft()["rows"],
            run_id="01TESTFORKEDSHARD000000000", published=self.clock.moment,
            integrity={"hash_scheme": fl.HASH_SCHEME, "shard_seq": 2, "prev_shard_hash": first["terminal_hash"]})
        lines, _ = fl.chain_payloads([manifest, *rows])
        forked = self.project / "_ledger" / "runs" / "01TESTFORKEDSHARD000000000.jsonl"
        forked.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ledger = fl.load_project(self.project)
        self.assertTrue(ledger.fork)
        self.assertEqual(len(ledger.heads), 2)
        self.clock.moment += timedelta(minutes=1)
        with self.assertRaises(fl.ForkError):
            self.publish(self.distribution_draft(origin="2026-09-25"))
        merged = fl.merge_heads(self.project, scratch_dir=self.scratch, now=self.clock)
        self.assertEqual(merged["shard_seq"], 3)
        ledger = fl.load_project(self.project)
        self.assertFalse(ledger.fork)
        self.assertEqual(ledger.heads[0].run_id, merged["run_id"])
        index = json.loads((self.project / "_ledger" / "index" / "INDEX_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertFalse(index["fork"])
        self.clock.moment += timedelta(minutes=1)
        self.publish(self.distribution_draft())

    def test_writer_lease_excludes_a_second_writer(self):
        with fl.writer_lease(self.project, "other-agent", now=self.clock):
            with self.assertRaises(fl.LeaseHeldError):
                self.publish(self.mct_draft())
        self.assertEqual(self.shard_paths(), [])
        stale = {"owner": "crashed", "host": "pc2", "pid": 1, "token": "x",
                 "acquired_utc": "2026-09-01T00:00:00Z", "expires_utc": "2026-09-01T00:15:00Z"}
        lease = self.project / "_ledger" / "writer.lease"
        lease.write_text(json.dumps(stale), encoding="utf-8")
        with self.assertRaisesRegex(fl.LeaseHeldError, "break-stale-lease"):
            self.publish(self.mct_draft())
        self.publish(self.mct_draft(), break_stale_lease=True)
        self.assertFalse(lease.exists())

    def test_receipts_detect_a_deleted_last_shard(self):
        self.publish(self.mct_draft())
        self.clock.moment += timedelta(minutes=1)
        last = self.publish(self.distribution_draft())
        receipts = self.scratch / "receipts" / PROJECT_NAME
        self.assertTrue(fl.verify_project(self.project, receipts)["ok"])
        Path(last["path"]).unlink()
        self.assertTrue(fl.verify_project(self.project)["ok"])  # the chain alone cannot see a missing tail
        report = fl.verify_project(self.project, receipts)
        self.assertFalse(report["ok"])
        self.assertIn("missing", report["problems"][0]["problem"])


class GuardsAndCliTest(LedgerCase):
    def test_storage_rule_on_windows_paths(self):
        check = lambda p, **kw: fl.drive_violation(p, "x", os_name="nt", system_drive="C:", **kw)  # noqa: E731
        self.assertIsNotNone(check(r"C:\Users\zoe\ledger"))
        self.assertIsNotNone(check(r"D:\codex-runtime\scratch"))
        self.assertIsNone(check(r"G:\My Drive\work\Investment-AI-Drive-Research"))
        self.assertIsNone(check(r"E:\codex-runtime\investment-ai\vibe-trading\home\forecast_ledger", require="E:"))
        self.assertIsNotNone(check(r"G:\My Drive\scratch", require="E:"))
        self.assertIsNotNone(fl.drive_violation(r"E:\x", "x", os_name="nt", system_drive="E:"))
        self.assertIsNone(fl.drive_violation(r"C:\x", "x", os_name="posix"))

    def test_scratch_inside_the_project_is_refused(self):
        with self.assertRaisesRegex(fl.LedgerError, "inside the shared project"):
            self.publish(self.mct_draft(), scratch_dir=self.project / "_scratch")

    def test_cli_record_verify_rebuild_rescore_export(self):
        # The CLI publishes on the real clock, so resolution dates are set relative to it.
        horizon = (datetime.now(UTC) + timedelta(days=400)).date()
        spec = self.spec(leverage_series_map={})
        spec["resolution_spec"].update(event_date=horizon.isoformat(),
                                       first_resolvable_utc=f"{horizon.isoformat()}T22:00:00Z")
        result_path = self.tmp / "pilot_result.json"
        spec_path = self.tmp / "spec.json"
        evidence_path = self.tmp / "evidence.json"
        result_path.write_text(json.dumps(PILOT_RESULT), encoding="utf-8")
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        evidence_path.write_text(json.dumps(EVIDENCE), encoding="utf-8")
        common = ["--project", str(self.project)]
        self.assertEqual(fl.main(["record-mct", *common, "--result", str(result_path), "--spec", str(spec_path),
                                  "--evidence", str(evidence_path), "--scratch", str(self.scratch), "--dry-run"]), 0)
        self.assertEqual(self.shard_paths(), [])
        self.assertEqual(fl.main(["record-mct", *common, "--result", str(result_path), "--spec", str(spec_path),
                                  "--evidence", str(evidence_path), "--scratch", str(self.scratch)]), 0)
        run_id = self.shard_paths()[0].stem
        self.assertEqual(fl.main(["verify", *common]), 0)
        self.assertEqual(fl.main(["rebuild-index", *common]), 0)
        self.assertEqual(fl.main(["rescore", *common]), 0)
        out = self.tmp / "export.json"
        self.assertEqual(fl.main(["export", *common, "--run-id", run_id, "--out", str(out)]), 0)
        self.assertTrue(verify_export(out).ok)
        self.assertEqual(fl.main(["merge-heads", *common, "--scratch", str(self.scratch)]), 2)


if __name__ == "__main__":
    unittest.main()
