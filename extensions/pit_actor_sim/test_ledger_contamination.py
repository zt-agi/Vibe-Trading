"""ZT add-on tests: the forecast ledger reads blind-run contamination (backward compatible).

    cd extensions/pit_actor_sim && python -m unittest test_ledger_contamination
    (VIBE_TRADING_HOME set; VT's agent directory importable, as for test_forecast_ledger)
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
from datetime import datetime

import forecast_ledger as fl
from test_forecast_ledger import EVIDENCE, PILOT_RESULT, UTC, LedgerCase, setUpModule  # noqa: F401


def run_manifest(status: str, **extra) -> dict:
    manifest = {
        "schema": "vt.actor_run_manifest.v1", "run_id": PILOT_RESULT["run_id"],
        "blind": {"blind": status != "NOT_BLIND", "mode": "auto", "max_age_days": 7,
                  "blind_view_sha256": "a" * 64, "sealed_mapping_sha256": "b" * 64},
        "contamination": {"status": status, "skill_eligible": status != "CONTAMINATED",
                          "probes": [{"probe_label": "probe", "verdict": "IDENTIFIED" if status == "CONTAMINATED"
                                      else "NOT_IDENTIFIED", "hits": {"ticker": False, "company": False,
                                                                      "year": status == "CONTAMINATED"}}],
                          "fork_identity_mentions": {}},
    }
    manifest.update(extra)
    return manifest


class ContaminationLedgerTest(LedgerCase):
    def board_after_resolution(self, draft):
        self.publish(draft)
        self.clock.moment = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
        fl.resolve_due(self.project, reader=self.brent_reader(), scratch_dir=self.scratch, now=self.clock)
        board = json.loads((self.project / "_ledger" / "index" / "scoreboard.json").read_text(encoding="utf-8"))
        return next(g for g in board["groups"] if g["claim_type"] == "binary")

    def brent_reader(self):
        from test_forecast_ledger import BRENT, FakeReader
        return FakeReader({BRENT: [("2026-12-31", 101.5, "2026-12-31T22:30:00Z", 0, "TRUE_PIT")]})

    def test_a_contaminated_run_is_resolved_but_never_counted_as_skill(self):
        draft = fl.mct_draft(PILOT_RESULT, self.spec(), evidence=EVIDENCE, run_manifest=run_manifest("CONTAMINATED"))
        self.assertEqual(draft["run_manifest"]["contamination"]["status"], "CONTAMINATED")
        self.assertIs(draft["run_manifest"]["contamination"]["skill_eligible"], False)
        self.assertTrue(draft["run_manifest"]["extensions"]["blind"]["blind"])
        group = self.board_after_resolution(draft)
        self.assertEqual((group["n_scored"], group["n_excluded_contaminated"]), (0, 4))
        self.assertIsNone(group["aggregate_skill"])
        shards = (self.project / "_ledger" / "index" / "shards.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(shards[0])["contamination"], "CONTAMINATED")
        rows = [json.loads(line) for line in
                (self.project / "_ledger" / "index" / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(row["run_contaminated"] for row in rows if row["kind"] == "prediction"))
        self.assertTrue(fl.verify_project(self.project)["ok"])

    def test_an_uncontaminated_blind_run_counts(self):
        draft = fl.mct_draft(PILOT_RESULT, self.spec(), evidence=EVIDENCE,
                             run_manifest=run_manifest("NOT_IDENTIFIED"))
        group = self.board_after_resolution(draft)
        self.assertEqual((group["n_scored"], group["n_excluded_contaminated"]), (4, 0))

    def test_manifests_without_the_key_are_unchanged(self):
        draft = fl.mct_draft(PILOT_RESULT, self.spec(), evidence=EVIDENCE)
        self.assertNotIn("contamination", draft["run_manifest"])
        self.assertNotIn("blind", draft["run_manifest"]["extensions"])
        group = self.board_after_resolution(draft)
        self.assertEqual((group["n_scored"], group["n_excluded_contaminated"]), (4, 0))

    def test_the_spec_can_state_it_and_bad_values_are_refused(self):
        spec = self.spec(contamination={"status": "CONTAMINATED", "skill_eligible": False})
        draft = fl.mct_draft(PILOT_RESULT, spec, evidence=EVIDENCE, run_manifest=run_manifest("NOT_IDENTIFIED"))
        self.assertEqual(draft["run_manifest"]["contamination"]["source"], "spec")
        for bad in ({"status": "MAYBE", "skill_eligible": True},
                    {"status": "CONTAMINATED", "skill_eligible": True},
                    {"status": "NOT_PROBED", "skill_eligible": False}):
            with self.assertRaises(fl.SchemaError):
                fl.mct_draft(PILOT_RESULT, self.spec(contamination=bad), evidence=EVIDENCE)

    def test_a_blind_rendering_or_another_runs_manifest_is_refused(self):
        with self.assertRaisesRegex(fl.SchemaError, "blind rendering"):
            fl.mct_draft(PILOT_RESULT, self.spec(), evidence={"schema": fl.BLIND_VIEW_SCHEMA})
        with self.assertRaisesRegex(fl.SchemaError, "another simulator run"):
            fl.mct_draft(PILOT_RESULT, self.spec(), evidence=EVIDENCE,
                         run_manifest=run_manifest("NOT_IDENTIFIED", run_id="0" * 24))

    def test_packet_price_rows_keep_their_ticker_in_the_lineage(self):
        evidence = copy.deepcopy(EVIDENCE)
        price = evidence["admitted_prices"][0]
        price["primary_ticker"] = price.pop("ticker")
        draft = fl.mct_draft(PILOT_RESULT, self.spec(), evidence=evidence)
        self.assertIn("price:USO@2026-08-27", draft["rows"][0]["evidence_lineage"]["evidence_record_ids"])

    def test_record_mct_reads_the_sibling_run_manifest_by_default(self):
        run_dir = self.tmp / "sim_runs" / "0123456789abcdef"
        run_dir.mkdir(parents=True)
        (run_dir / "pilot_result.json").write_text(json.dumps(PILOT_RESULT), encoding="utf-8", newline="\n")
        (run_dir / "run_manifest.json").write_text(json.dumps(run_manifest("CONTAMINATED")), encoding="utf-8",
                                                   newline="\n")
        spec_path = self.tmp / "spec.json"
        spec_path.write_text(json.dumps(self.spec()), encoding="utf-8", newline="\n")
        evidence_path = self.tmp / "packet.json"
        evidence_path.write_text(json.dumps(EVIDENCE), encoding="utf-8", newline="\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fl.main(["record-mct", "--project", str(self.project), "--result",
                            str(run_dir / "pilot_result.json"), "--spec", str(spec_path), "--evidence",
                            str(evidence_path), "--dry-run"])
        self.assertEqual(code, 0)
        draft = json.loads(out.getvalue())["draft"]
        self.assertEqual(draft["run_manifest"]["contamination"]["status"], "CONTAMINATED")
        (run_dir / "run_manifest.json").write_text(json.dumps(run_manifest("CONTAMINATED", run_id="x" * 24)),
                                                   encoding="utf-8", newline="\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fl.main(["record-mct", "--project", str(self.project), "--result", str(run_dir / "pilot_result.json"),
                     "--spec", str(spec_path), "--evidence", str(evidence_path), "--dry-run"])
        self.assertNotIn("contamination", json.loads(out.getvalue())["draft"]["run_manifest"])


if __name__ == "__main__":
    unittest.main()
