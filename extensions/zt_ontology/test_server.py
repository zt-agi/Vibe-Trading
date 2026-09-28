"""Tests for the read-only ontology tools (ZT add-on).

They exercise the real project engine (pitdb/ontology_query.py) on the
release's SYNTHETIC replay fixture, so they need the canonical project:

    set INVESTMENT_AI_PROJECT_ROOT=G:\\My Drive\\work\\Investment-AI-Drive-Research
    python -B -m unittest discover -s extensions/zt_ontology -p "test_*.py" -v

Without INVESTMENT_AI_PROJECT_ROOT the suite is skipped (VT's own CI has no
access to the private project).  Everything runs on in-memory DuckDB; the
optional lake-backed test needs ZT_ONTOLOGY_TEST_LAKE (a lake folder that
already holds the ontology tables, e.g. after bin/seed_ontology.py --apply).
"""
from __future__ import annotations

import ast
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402

ROOT = os.environ.get("INVESTMENT_AI_PROJECT_ROOT")
R0 = "2026-09-05T00:00:00Z"
R1 = "2026-09-21T00:00:00Z"
FIXED_NOW = datetime(2026, 9, 28, 12, 0, 0)
STATE = {}


def setUpModule():
    if not ROOT:
        raise unittest.SkipTest("INVESTMENT_AI_PROJECT_ROOT not set (private project needed)")
    L, Q = server.engine()
    release = server.project() / "ontology" / "finance_mvo"
    con, _ = Q.build_store(release, fixture_files=[release / "fixtures" / "replay_fixture.yaml"],
                           extra_releases=[release / "fixtures" / "fixture_module.yaml"], now=FIXED_NOW)
    STATE["con"] = con
    STATE["patch"] = patch.object(server, "store", return_value=con)
    STATE["patch"].start()


def tearDownModule():
    if "patch" in STATE:
        STATE["patch"].stop()
        STATE["con"].close()


def counts():
    return {t: STATE["con"].execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in server.ONTOLOGY_TABLES}


class GuardsTest(unittest.TestCase):
    def test_asof_needs_offset_and_past(self):
        with self.assertRaises(ValueError):
            server.onto_concept("position", "2026-09-21T00:00:00")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            server.onto_node("SEC:FX1", future)

    def test_limits_and_hops_are_capped(self):
        with self.assertRaises(ValueError):
            server.onto_neighbors("SEC:FX1", R1, hops=3)
        with self.assertRaises(ValueError):
            server.onto_competency("CQ-FIN-01", R1, limit=501)
        with self.assertRaises(ValueError):
            server.onto_actor_roster("fx_oil_shock", R1, limit=0)

    def test_params_must_be_simple(self):
        with self.assertRaises(ValueError):
            server.onto_competency("CQ-FIN-01", R1, params={"holder": {"$where": "1=1"}})

    def test_responses_are_capped_at_500(self):
        payload = server._cap({"rows": list(range(900)), "notes": [{"x": list(range(700))}]})
        self.assertEqual(len(payload["rows"]), 500)
        self.assertEqual(len(payload["notes"][0]["x"]), 500)


class ReadOnlyTest(unittest.TestCase):
    def test_tools_never_change_the_store(self):
        before = counts()
        server.onto_concept("holding", R1)
        server.onto_node("FXCHIP", R1)
        server.onto_neighbors("PORT:fx_book", R1, ["holds"], hops=2)
        server.onto_actor_roster("fx_oil_shock", R1)
        for i in range(1, 12):
            server.onto_competency(f"CQ-FIN-{i:02d}", R1)
        self.assertEqual(before, counts())

    def test_server_source_has_no_write_path(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        for forbidden in ("INSERT", "COPY ", "persist_ontology_to_lake", "append_graph(", "load_release(",
                          "set_release_status", "authorize_verb", "subprocess"):
            self.assertNotIn(forbidden, source, forbidden)


class ConceptToolTest(unittest.TestCase):
    def test_paraphrased_labels_resolve(self):
        for phrase in ("position", "holding", "stake", "持仓", "Portfolio-Position"):
            out = server.onto_concept(phrase, R1)
            self.assertEqual(out["concept"]["concept_id"], "FIN.POSITION", phrase)
        self.assertEqual(server.onto_concept("go long", R1)["concept"]["contract"]["execution_mode"],
                         "approval_only_proposal")

    def test_negative_controls(self):
        self.assertEqual(server.onto_concept("buy", R1)["resolution"]["status"], "AMBIGUOUS")
        self.assertEqual(server.onto_concept("moonshot vibes", R1)["resolution"]["status"], "UNRESOLVED")
        self.assertNotIn("concept", server.onto_concept("moonshot vibes", R1))

    def test_deprecated_alias_after_valid_to_resolves_to_successor(self):
        before = server.onto_concept("trade idea", "2026-06-01T00:00:00Z")
        after = server.onto_concept("trade idea", R1)
        self.assertEqual(before["concept"]["concept_id"], "FIXTURE.TRADE_IDEA")
        self.assertEqual(after["resolution"]["status"], "DEPRECATED_TO_SUCCESSOR")
        self.assertEqual(after["concept"]["concept_id"], "FIN.TRADE_CANDIDATE")

    def test_record_carries_versioning_and_justification(self):
        rec = server.onto_concept("KERNEL.ACTOR", R1)["concept"]
        for key in ("canonical_label", "definition", "domain", "valid_from", "valid_to", "labels_valid_at_asof",
                    "deprecated_or_inactive_labels", "successor_id", "predecessor_id", "relations", "justification"):
            self.assertIn(key, rec)
        self.assertIn("FIN.REL.HOLDS", rec["relations"])
        self.assertTrue(any(j.get("module_id") == "FIN" for j in rec["justification"]))


class NodeAndNeighborTest(unittest.TestCase):
    def test_ticker_is_time_scoped(self):
        self.assertEqual(server.onto_node("FXOLD", "2026-05-01T00:00:00Z")["node"]["node_id"], "SEC:FX9")
        self.assertEqual(server.onto_node("FXOLD", R1)["node"]["node_id"], "SEC:FX3")
        # FXFAB was not a ticker yet on 2026-05-01 (label window starts 2026-07-01) ...
        self.assertEqual(server.onto_node("FXFAB", "2026-05-01T00:00:00Z")["status"], "UNRESOLVED")
        # ... and the node itself only became knowable on 2026-07-01.
        self.assertEqual(server.onto_node("SEC:FX3", "2026-05-01T00:00:00Z")["status"], "UNKNOWN_AT_ASOF")

    def test_edge_known_at_t1_is_invisible_at_t0(self):
        t0 = server.onto_neighbors("Fixture Chip Co", "2026-09-09T23:00:00Z", ["business link"])
        t1 = server.onto_neighbors("Fixture Chip Co", "2026-09-10T00:00:00Z", ["business link"])
        self.assertEqual(t0["row_count"], 0)
        self.assertEqual(t0["edge_type_status"]["FIN.BUSINESS_RELATIONSHIP"]["status"], "UNKNOWN")
        self.assertEqual([r["hyperedge_id"] for r in t1["rows"]], ["HE:fx:biz:chip_foundry"])

    def test_missing_edge_is_unknown_and_closed_scope_is_none(self):
        petro = server.onto_neighbors("ACTOR:fx_petrostate", R1, ["holds"])
        self.assertEqual(petro["edge_type_status"]["FIN.REL.HOLDS"]["status"], "UNKNOWN")
        self.assertIn("UNKNOWN", petro["absent_edge_semantics"])
        before_census = server.onto_competency("CQ-FIN-01", R0, {"holder": "PORT:fx_book", "instrument": "SEC:FX3"})
        after_census = server.onto_competency("CQ-FIN-01", R1, {"holder": "PORT:fx_book", "instrument": "SEC:FX3"})
        self.assertEqual((before_census["status"], after_census["status"]), ("UNKNOWN", "NONE"))

    def test_two_hops_and_unresolved_edge_type(self):
        out = server.onto_neighbors("PORT:fx_book", R1, ["holds"], hops=2)
        self.assertEqual({r["hyperedge_id"] for r in out["rows"]},
                         {"HE:fx:holds:book_FX1", "HE:fx:holds:book_FX2", "HE:fx:holds:lsf_FX2"})
        self.assertEqual(server.onto_neighbors("PORT:fx_book", R1, ["sort of owns"])["status"], "UNRESOLVED_EDGE_TYPE")


class RosterTest(unittest.TestCase):
    def test_both_families_with_holdings_and_visibility(self):
        out = server.onto_actor_roster("fx_oil_shock", R1)
        fam = out["families"]
        self.assertEqual({a["actor"] for a in fam["real_economy"]}, {"ACTOR:fx_petrostate", "ENT:fx_chipco"})
        self.assertEqual({a["actor"] for a in fam["security_flow"]}, {"FUND:fx_ls_funds", "FUND:fx_levetf"})
        lsf = next(a for a in fam["security_flow"] if a["actor"] == "FUND:fx_ls_funds")
        self.assertEqual([(h["instrument"], h["quantity"], h["side"]) for h in lsf["holdings"]],
                         [("SEC:FX2", 1000, "short")])
        self.assertEqual(lsf["visibility"][0]["observables"], ["SRC:cftc_cot"])
        self.assertEqual(lsf["revealed_by"][0]["channel"], "reveals_action")
        petro = next(a for a in fam["real_economy"] if a["actor"] == "ACTOR:fx_petrostate")
        self.assertTrue(petro["holdings_status"].startswith("UNKNOWN (absence"))
        chipco = next(a for a in fam["real_economy"] if a["actor"] == "ENT:fx_chipco")
        self.assertEqual((chipco["holdings_status"], chipco["visibility"]), ("UNKNOWN", "UNKNOWN"))

    def test_unknown_scenario_and_before_it_existed(self):
        self.assertEqual(server.onto_actor_roster("no_such_scenario", R1)["status"], "UNRESOLVED_SCENARIO")
        self.assertEqual(server.onto_actor_roster("fx_oil_shock", "2026-08-01T00:00:00Z")["status"],
                         "UNKNOWN_AT_ASOF")


class CompetencyToolTest(unittest.TestCase):
    EXPECT_R1 = {
        "CQ-FIN-01": ("ANSWERED", 3), "CQ-FIN-02": ("ANSWERED", 4), "CQ-FIN-03": ("ANSWERED", 3),
        "CQ-FIN-04": ("ANSWERED", 2), "CQ-FIN-05": ("ANSWERED", 4), "CQ-FIN-06": ("ANSWERED", 3),
        "CQ-FIN-07": ("ANSWERED", 1), "CQ-FIN-08": ("ANSWERED", 1), "CQ-FIN-09": ("ANSWERED", 2),
        "CQ-FIN-10": ("ANSWERED", 3), "CQ-FIN-11": ("ANSWERED", 2),
    }

    def test_each_competency_question_is_answered_from_the_fixture(self):
        for cq, (status, n) in self.EXPECT_R1.items():
            out = server.onto_competency(cq, R1)
            self.assertEqual((out["status"], out["row_count"]), (status, n), cq)
            self.assertEqual(out["question_id"], cq)
            text = json.dumps(out).lower()
            self.assertNotIn('"probability"', text)
            self.assertNotIn('"confidence"', text)

    def test_specific_answers(self):
        cq3 = server.onto_competency("which actors are likely constrained?", R1)
        self.assertEqual(cq3["rows"][0]["actor"], "FUND:fx_ls_funds")
        cq6 = {r["assumption"]: r["stale"] for r in server.onto_competency("CQ-FIN-06", R1)["rows"]}
        self.assertEqual(cq6, {"CLM:fx_a1": True, "CLM:fx_a2": False, "CLM:fx_a3": True})
        cq11 = server.onto_competency("CQ-FIN-11", R1)
        self.assertEqual(cq11["rows"][0]["work_order"], "WO:fx_backtest_flows")
        self.assertEqual(cq11["notes"][0]["voi_method"], "STRUCTURAL_FANOUT_TRIAGE")
        cq1 = server.onto_competency("CQ-FIN-01", R1)
        self.assertNotIn("TC:fx_long_FX2", json.dumps(cq1))          # a proposal is never a position

    def test_as_of_changes_the_answer(self):
        self.assertEqual(server.onto_competency("CQ-FIN-05", R0)["status"], "UNKNOWN")
        self.assertEqual(server.onto_competency("CQ-FIN-09", R0)["status"], "UNKNOWN")

    def test_unknown_question(self):
        out = server.onto_competency("CQ-FIN-99", R1)
        self.assertEqual(out["status"], "UNKNOWN_QUESTION")
        self.assertEqual(len(out["available"]), 11)


@unittest.skipUnless(os.environ.get("ZT_ONTOLOGY_TEST_LAKE"), "set ZT_ONTOLOGY_TEST_LAKE to a seeded lake")
class LakeBackedTest(unittest.TestCase):
    def test_store_hydrates_from_lake_in_memory(self):
        with patch.dict(os.environ, {"PITDB_LAKE": os.environ["ZT_ONTOLOGY_TEST_LAKE"]}):
            STATE["patch"].stop()
            try:
                server._STORE.update(signature=None, con=None)
                con = server.store()
                self.assertGreater(con.execute("SELECT count(*) FROM dim_concept").fetchone()[0], 0)
                self.assertIs(server.store(), con)                 # cached while the lake is unchanged
                roster = server.onto_actor_roster("iran_oil_pilot", datetime.now(timezone.utc).isoformat())
                self.assertEqual(roster["status"], "OK")
                self.assertTrue(roster["families"]["real_economy"] and roster["families"]["security_flow"])
            finally:
                STATE["patch"].start()


class ToolSurfaceTest(unittest.TestCase):
    def test_exactly_the_five_read_only_tools(self):
        tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
        tools = sorted(f.name for f in tree.body if isinstance(f, ast.FunctionDef)
                       and any(getattr(d, "attr", None) == "tool" for d in f.decorator_list))
        self.assertEqual(tools, ["onto_actor_roster", "onto_competency", "onto_concept",
                                 "onto_neighbors", "onto_node"])


if __name__ == "__main__":
    unittest.main()
