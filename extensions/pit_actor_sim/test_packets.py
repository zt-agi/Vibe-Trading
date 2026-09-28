"""ZT add-on tests: frozen evidence packets, role-fork admission, packet runs.

Run from this folder with VIBE_TRADING_HOME set to the E: runtime (any
writable folder off Windows), exactly like test_server.py:

    python -m unittest test_server test_packets

The replay test additionally needs INVESTMENT_AI_PROJECT_ROOT pointing at the
canonical project (it runs market_actor_sim/run_governed_pilot.py for real).
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fork_rules
import server

# The pilot scenario (market_actor_sim/config/scenario_iran_oil.yaml, sha256
# 65b8abac...), without its comment header.
SCENARIO_YAML = """\
name: iran_oil_pilot
description: >
  US administration decision -> Iranian leadership response -> (China response /
  L/S hedge-fund positioning) -> leveraged-ETF flow amplification -> quarterly
  crude regime bin. Terminal bins: severe spike (>120), moderate spike (95-120),
  range (70-95), down (<70).
headline_outcomes: [oil_spike_severe, oil_spike_moderate]
rewards:
  oil_spike_severe: 1.00
  oil_spike_moderate: 0.70
  oil_range: 0.35
  oil_down: 0.00
tree:
  actor: us_administration
  actions:
    strike_iran:
      child:
        actor: iran_leadership
        actions:
          close_strait:
            child:
              actor: ls_hedge_funds
              actions:
                risk_off_chase:
                  child:
                    actor: leveraged_etf_complex
                    actions:
                      inflows_amplify: {outcome: oil_spike_severe}
                      outflows_dampen: {outcome: oil_spike_moderate}
                fade_spike: {outcome: oil_spike_moderate}
          proxy_attacks:
            child:
              actor: ls_hedge_funds
              actions:
                risk_off_chase: {outcome: oil_spike_moderate}
                fade_spike: {outcome: oil_range}
          restraint: {outcome: oil_range}
    sanctions_max:
      child:
        actor: iran_leadership
        actions:
          enrich_escalate:
            child:
              actor: china_leadership
              actions:
                buy_discounted_oil: {outcome: oil_range}
                broker_deal: {outcome: oil_down}
                back_iran_hard: {outcome: oil_spike_moderate}
          negotiate_back: {outcome: oil_range}
          proxy_attacks: {outcome: oil_spike_moderate}
    negotiate:
      child:
        actor: iran_leadership
        actions:
          deal:
            child:
              actor: china_leadership
              actions:
                support_deal: {outcome: oil_down}
                undercut: {outcome: oil_range}
          stall: {outcome: oil_range}
"""

PILOT_TEMPERAMENTS = [
    "CAUTIOUS_INSTITUTIONAL_BASE_RATE",
    "ESCALATION_SENSITIVE_STRESS_CASE",
    "DE_ESCALATION_SENSITIVE_NEGOTIATION_FAVORING",
]

# The 2026-08-28 pilot's fork vectors. role_forks.json is not in the shared
# folder; these were reconstructed from pilot_result.json (per-action min /
# mean / max across the three forks, then the unique assignment whose exact
# tree recursion reproduces exact_by_temperament for all three temperaments).
PILOT_PROPENSITIES = {
    "CAUTIOUS_INSTITUTIONAL_BASE_RATE": {
        "": {"strike_iran": 0.30, "sanctions_max": 0.36, "negotiate": 0.34},
        "strike_iran": {"close_strait": 0.31, "proxy_attacks": 0.35, "restraint": 0.34},
        "strike_iran/close_strait": {"risk_off_chase": 0.52, "fade_spike": 0.48},
        "strike_iran/close_strait/risk_off_chase": {"inflows_amplify": 0.50, "outflows_dampen": 0.50},
        "strike_iran/proxy_attacks": {"risk_off_chase": 0.49, "fade_spike": 0.51},
        "sanctions_max": {"enrich_escalate": 0.32, "negotiate_back": 0.35, "proxy_attacks": 0.33},
        "sanctions_max/enrich_escalate": {"buy_discounted_oil": 0.34, "broker_deal": 0.35, "back_iran_hard": 0.31},
        "negotiate": {"deal": 0.48, "stall": 0.52},
        "negotiate/deal": {"support_deal": 0.51, "undercut": 0.49},
    },
    "ESCALATION_SENSITIVE_STRESS_CASE": {
        "": {"strike_iran": 0.34, "sanctions_max": 0.37, "negotiate": 0.29},
        "strike_iran": {"close_strait": 0.39, "proxy_attacks": 0.34, "restraint": 0.27},
        "strike_iran/close_strait": {"risk_off_chase": 0.54, "fade_spike": 0.46},
        "strike_iran/close_strait/risk_off_chase": {"inflows_amplify": 0.52, "outflows_dampen": 0.48},
        "strike_iran/proxy_attacks": {"risk_off_chase": 0.52, "fade_spike": 0.48},
        "sanctions_max": {"enrich_escalate": 0.37, "negotiate_back": 0.30, "proxy_attacks": 0.33},
        "sanctions_max/enrich_escalate": {"buy_discounted_oil": 0.34, "broker_deal": 0.31, "back_iran_hard": 0.35},
        "negotiate": {"deal": 0.44, "stall": 0.56},
        "negotiate/deal": {"support_deal": 0.47, "undercut": 0.53},
    },
    "DE_ESCALATION_SENSITIVE_NEGOTIATION_FAVORING": {
        "": {"strike_iran": 0.24, "sanctions_max": 0.32, "negotiate": 0.44},
        "strike_iran": {"close_strait": 0.25, "proxy_attacks": 0.32, "restraint": 0.43},
        "strike_iran/close_strait": {"risk_off_chase": 0.45, "fade_spike": 0.55},
        "strike_iran/close_strait/risk_off_chase": {"inflows_amplify": 0.47, "outflows_dampen": 0.53},
        "strike_iran/proxy_attacks": {"risk_off_chase": 0.44, "fade_spike": 0.56},
        "sanctions_max": {"enrich_escalate": 0.29, "negotiate_back": 0.43, "proxy_attacks": 0.28},
        "sanctions_max/enrich_escalate": {"buy_discounted_oil": 0.34, "broker_deal": 0.39, "back_iran_hard": 0.27},
        "negotiate": {"deal": 0.57, "stall": 0.43},
        "negotiate/deal": {"support_deal": 0.55, "undercut": 0.45},
    },
}

# The pilot's frozen snapshot (runs/iran_oil_pit_pilot_20260828/evidence_snapshot.json).
PILOT_ASOF = "2026-08-28T12:00:00+00:00"
PILOT_SERIES = {
    "POLY:us-x-iran-ceasefire-continues-through-september-30:Yes": 0.795,
    "POLY:us-x-iran-diplomatic-meeting-by-september-30-2026:Yes": 0.165,
    "POLY:will-no-qualifying-diplomatic-us-iran-meeting-occur-by-september-30-2026-2026062:Yes": 0.824,
    "POLY:will-the-us-invade-iran-before-2027:Yes": 0.135,
    "POLY:will-the-us-officially-declare-war-on-iran-by-december-31-2026-746:Yes": 0.0325,
    "POLY:strait-of-hormuz-traffic-returns-to-normal-by-december-31:Yes": 0.325,
}
PILOT_KT = "2026-08-28T11:42:10.794152"
USO_ROW = {"sec_id": 7, "primary_ticker": "USO", "event_date": "2026-08-27", "open": 128.149994,
           "high": 130.960007, "low": 127.480003, "close": 130.009995, "volume": 3043200.0,
           "currency": "USD", "knowledge_time": "2026-08-28T11:40:48.358918", "revision_seq": 0,
           "source_id": "yahoo_eod", "pit_class": "OBSERVED_PIT"}
RECEIPT = {"signature": {"lake_root": "lake", "files": {"fact_observation": [1, 2]}},
           "audited_at_utc": "2026-08-28T11:59:00+00:00", "status": "PASS", "checks": 10}
SIGNATURE = RECEIPT["signature"]

EVIDENCE_BY_ACTOR = {
    "us_administration": ["E1", "E4", "E5"],
    "iran_leadership": ["E1", "E2", "E3"],
    "ls_hedge_funds": ["P1", "E6"],
    "leveraged_etf_complex": ["P1"],
    "china_leadership": [],
}


def setUpModule():
    home = os.environ.get("VIBE_TRADING_HOME", "").strip()
    if not home:
        raise RuntimeError(
            "VIBE_TRADING_HOME must be set to the E: runtime before running these tests "
            "(ZT 2026-09-28: everything on E:)")
    if os.name == "nt" and server.windows_drive(Path(home).resolve()) != "E:":
        raise RuntimeError(f"VIBE_TRADING_HOME must be on E: for these tests; got {home}")


def series_rows(series_id, asof, start_date, end_date, limit=250):
    value = PILOT_SERIES[series_id]
    return {"asof": asof, "row_count": 1, "rows": [{
        "series_id": series_id, "label": series_id, "unit": "probability",
        "event_time": PILOT_KT, "value_num": value, "value_str": None, "quality": None,
        "knowledge_time": PILOT_KT, "revision_seq": 0, "source_id": "polymarket",
        "pit_class": "OBSERVED_PIT"}]}


def memo_text(actor: str, actions: list[str]) -> str:
    return (f"The {actor.replace('_', ' ')} weighs {', '.join(actions)} against its incentives, "
            "constraints and the information set frozen in the packet; the cited rows are noisy "
            "external observations of expectations and are not copied into the vector.")


def make_fork(temperament: str, tree: fork_rules.ScenarioTree, uncited: bool = True) -> dict:
    memos, props = {}, {}
    for key, spec in tree.states.items():
        cites = EVIDENCE_BY_ACTOR[spec.actor]
        memos[key] = {
            "actor": spec.actor,
            "analysis": memo_text(spec.actor, list(spec.actions)),
            "evidence": list(cites) if cites or not uncited else [],
            "missing_observables": [] if cites else ["China leadership primary-source bundle"],
        }
        props[key] = dict(PILOT_PROPENSITIES[temperament][key])
    return {"temperament": temperament, "memos": memos, "propensities": props}


class PacketHarness(unittest.TestCase):
    """A fake canonical project and E: runtime with the PIT layer mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("VIBE_TRADING_HOME"))
        base = Path(self._tmp.name)
        self.root = base / "work" / "Investment-AI-Drive-Research"
        self.sim = self.root / "market_actor_sim"
        (self.sim / "config").mkdir(parents=True)
        (self.sim / "sim").mkdir()
        (self.sim / "config" / "scenario_iran_oil.yaml").write_text(SCENARIO_YAML, encoding="utf-8")
        (self.sim / "run_governed_pilot.py").write_text("# engine fixture\n", encoding="utf-8")
        (self.sim / "sim" / "model.py").write_text("# engine fixture\n", encoding="utf-8")
        self.home = base / "runtime"
        self.home.mkdir()
        self.tree = fork_rules.parse_scenario(SCENARIO_YAML)
        patches = [
            patch.object(server, "project", return_value=self.root),
            patch.object(server, "runtime", return_value=self.home),
            patch.object(server, "require_fresh_audit", return_value=dict(RECEIPT)),
            patch.object(server, "lake_signature", return_value=dict(SIGNATURE)),
            patch.object(server, "require_fresh_index", return_value=None),
            patch.object(server, "pit_series_history", side_effect=series_rows),
            patch.object(server, "pit_security", return_value={"securities": [{"sec_id": 7}]}),
            patch.object(server, "pit_price_history", return_value={"rows": [dict(USO_ROW)]}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self._tmp.cleanup)

    def freeze(self, **overrides):
        arguments = dict(
            scenario_id="iran_oil", run_asof=PILOT_ASOF,
            series=[{"series_id": s, "start_date": "2026-08-28", "end_date": "2026-08-28"}
                    for s in PILOT_SERIES],
            prices=[{"ticker": "USO", "start_date": "2026-08-27", "end_date": "2026-08-27"}],
            memos=[{"author": "geopolitical_evidence",
                    "text": "Ceasefire continuation and no-meeting contracts are both priced high.",
                    "citations": [next(iter(PILOT_SERIES)), "E3"]}],
            excluded_or_missing=[{"item": "China leadership primary-source bundle",
                                  "reason": "Missing; China states widen toward uniform."}],
        )
        arguments.update(overrides)
        return server.freeze_evidence_packet(**arguments)

    def submit(self, sha, fork):
        return server.submit_role_fork(sha, fork["temperament"], fork["memos"], fork["propensities"])


class ForkRulesTest(unittest.TestCase):
    """The ported validate_forks rules, one failure each."""

    def setUp(self):
        self.tree = fork_rules.parse_scenario(SCENARIO_YAML)
        self.fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)

    def reject(self, fork, pattern):
        with self.assertRaisesRegex(fork_rules.ForkRejected, pattern):
            fork_rules.validate_fork(fork, self.tree)

    def test_valid_fork_is_returned_in_scenario_action_order(self):
        fork = copy.deepcopy(self.fork)
        fork["propensities"]["negotiate"] = {"stall": 0.52, "deal": 0.48}
        accepted = fork_rules.validate_fork(fork, self.tree)
        self.assertEqual(list(accepted["propensities"]["negotiate"]), ["deal", "stall"])

    def test_fewer_than_three_forks_and_duplicate_labels_are_rejected(self):
        two = [make_fork(t, self.tree) for t in PILOT_TEMPERAMENTS[:2]]
        with self.assertRaisesRegex(fork_rules.ForkRejected, "at least 3"):
            fork_rules.validate_fork_set(two, self.tree)
        dup = [make_fork(PILOT_TEMPERAMENTS[0], self.tree)] * 3
        with self.assertRaisesRegex(fork_rules.ForkRejected, "unique"):
            fork_rules.validate_fork_set(dup, self.tree)
        self.assertEqual(len(fork_rules.validate_fork_set(
            [make_fork(t, self.tree) for t in PILOT_TEMPERAMENTS], self.tree)), 3)

    def test_state_coverage_actor_and_memo_length(self):
        fork = copy.deepcopy(self.fork)
        del fork["memos"]["negotiate/deal"]
        self.reject(fork, "state coverage differs")
        fork = copy.deepcopy(self.fork)
        fork["memos"]["negotiate"]["actor"] = "china_leadership"
        self.reject(fork, "actor mismatch")
        fork = copy.deepcopy(self.fork)
        fork["memos"]["negotiate"]["analysis"] = "deal or stall"
        self.reject(fork, "content memo too short")

    def test_propensities_are_state_local_open_interval_and_sum_to_one(self):
        fork = copy.deepcopy(self.fork)
        fork["propensities"]["negotiate"]["oil_range"] = 0.1
        self.reject(fork, "action mismatch")
        for bad in (0.0, 1.0, -0.2, True):
            fork = copy.deepcopy(self.fork)
            fork["propensities"]["negotiate"] = {"deal": bad, "stall": 0.5}
            self.reject(fork, "strictly between 0 and 1")
        fork = copy.deepcopy(self.fork)
        fork["propensities"]["negotiate"] = {"deal": 0.5, "stall": 0.49}
        self.reject(fork, "do not sum to one")

    def test_memo_naming_a_terminal_outcome_is_rejected_literally_and_by_concept(self):
        for phrase in ("oil_spike_severe looks likely",
                       "a severe oil spike would follow",
                       "crude surging severely is what the funds fear",
                       "the chance that crude drops in the quarter"):
            fork = copy.deepcopy(self.fork)
            fork["memos"]["strike_iran"]["analysis"] += " " + phrase
            self.reject(fork, "terminal outcome mentioned")
        fork = copy.deepcopy(self.fork)
        fork["memos"]["strike_iran"]["missing_observables"] = ["the oil_range bin odds"]
        self.reject(fork, "terminal outcome mentioned")

    def test_memo_must_discuss_every_action(self):
        fork = copy.deepcopy(self.fork)
        fork["memos"]["negotiate"]["analysis"] = (
            "Iranian leadership considers the deal on offer and nothing else, reading the frozen "
            "packet's prediction-market rows as noisy external expectations only.")
        self.reject(fork, r"does not discuss action\(s\) \['stall'\]")

    def test_concept_matcher_ignores_action_words_without_the_outcome(self):
        self.assertEqual(fork_rules.outcome_mentions(
            "hedge funds may fade the spike or chase it", self.tree.outcomes), [])
        self.assertEqual(fork_rules.outcome_mentions(
            "China could buy discounted oil from Iran", self.tree.outcomes), [])


class PacketFreezeTest(PacketHarness):
    def test_packet_is_content_addressed_idempotent_and_tamper_evident(self):
        first = self.freeze()
        second = self.freeze()
        sha = first["packet_sha256"]
        self.assertEqual(second["packet_sha256"], sha)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        packet = server.load_packet(sha)
        self.assertEqual(packet["warehouse_audit"]["status"], "PASS")
        self.assertEqual(packet["warehouse_audit"]["receipt_sha256"],
                         server.sha256_hex(server.canonical_json(RECEIPT)))
        self.assertEqual(packet["lake_signature"], SIGNATURE)
        self.assertEqual(packet["scenario"]["sha256"],
                         server.sha256_hex(SCENARIO_YAML.encode("utf-8")))
        self.assertEqual([o["evidence_id"] for o in packet["admitted_observations"]],
                         ["E1", "E2", "E3", "E4", "E5", "E6"])
        self.assertEqual(packet["admitted_prices"][0]["evidence_id"], "P1")
        self.assertEqual(packet["memos"][0]["citations"], ["E1", "E3"])
        path = self.home / "actor_packets" / sha / "packet.json"
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["admitted_observations"][0]["value_num"] = 0.5
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            server.load_packet(sha)

    def test_future_knowledge_non_pit_rows_and_unresolved_memo_citations(self):
        def late(series_id, asof, *_args):
            rows = series_rows(series_id, asof, None, None)
            rows["rows"][0]["knowledge_time"] = "2026-08-28T12:00:01"
            return rows
        with patch.object(server, "pit_series_history", side_effect=late):
            with self.assertRaisesRegex(RuntimeError, "after the as-of"):
                self.freeze()
        with patch.object(server, "pit_price_history",
                          return_value={"rows": [dict(USO_ROW, pit_class="NON_PIT")]}):
            packet = server.load_packet(self.freeze()["packet_sha256"])
        self.assertEqual(packet["admitted_prices"], [])
        self.assertTrue(any("NON_PIT" in x["reason"] for x in packet["excluded_or_missing"]))
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.freeze(memos=[{"author": "a", "text": "t", "citations": ["FRED:DCOILWTICO"]}])

    def test_stale_audit_blocks_the_freeze_before_anything_is_written(self):
        with patch.object(server, "require_fresh_audit",
                          side_effect=RuntimeError("PIT audit receipt expired")):
            with self.assertRaisesRegex(RuntimeError, "expired"):
                self.freeze()
        self.assertFalse((self.home / "actor_packets").exists())

    def test_role_fork_view_hides_outcomes_rewards_and_other_forks(self):
        sha = self.freeze()["packet_sha256"]
        self.submit(sha, make_fork(PILOT_TEMPERAMENTS[0], self.tree))
        view = json.dumps(server.inspect_evidence_packet(sha, "role_fork"))
        for hidden in ("oil_spike", "oil_range", "oil_down", "rewards", "headline",
                       "Terminal bins", PILOT_TEMPERAMENTS[0]):
            self.assertNotIn(hidden, view)
        full = server.inspect_evidence_packet(sha, "full")
        self.assertIn("oil_down", full["scenario"]["outcomes"])
        self.assertEqual(full["accepted_forks"][0]["temperament"], PILOT_TEMPERAMENTS[0])

    def test_packet_paths_cannot_leave_the_runtime_store(self):
        sha = self.freeze()["packet_sha256"]
        self.assertTrue(server.sim_input(f"actor_packets/{sha}/packet.json").is_file())
        for bad in (f"actor_packets/{sha}/../../escape.json", "actor_packets/abc/packet.json",
                    f"actor_packets/{sha}"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                server.sim_input(bad)


class RoleForkSubmissionTest(PacketHarness):
    def test_accept_write_once_and_rejection_stores_nothing(self):
        sha = self.freeze()["packet_sha256"]
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        accepted = self.submit(sha, fork)
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["duplicate"])
        self.assertEqual(accepted["forks_accepted"], 1)
        self.assertFalse(accepted["ready_to_simulate"])
        self.assertTrue(self.submit(sha, fork)["duplicate"])
        changed = copy.deepcopy(fork)
        changed["propensities"]["negotiate"] = {"deal": 0.5, "stall": 0.5}
        with self.assertRaisesRegex(ValueError, "already submitted"):
            self.submit(sha, changed)
        bad = make_fork(PILOT_TEMPERAMENTS[1], self.tree)
        bad["memos"]["negotiate"]["analysis"] += " A severe oil spike is the base case."
        with self.assertRaisesRegex(ValueError, "terminal outcome mentioned"):
            self.submit(sha, bad)
        self.assertEqual(len(server.accepted_forks(sha)), 1)

    def test_citations_must_come_from_the_packet(self):
        sha = self.freeze()["packet_sha256"]
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        fork["memos"][""]["evidence"] = ["Reuters 2026-08-27 briefing"]
        with self.assertRaisesRegex(ValueError, "not in the frozen packet"):
            self.submit(sha, fork)
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        fork["memos"][""]["evidence"] = [
            "POLY:us-x-iran-ceasefire-continues-through-september-30:Yes = 0.795 at kt 11:42"]
        self.assertTrue(self.submit(sha, fork)["accepted"])

    def test_uncited_state_needs_missing_observable_and_near_uniform_vector(self):
        sha = self.freeze()["packet_sha256"]
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        fork["memos"]["negotiate/deal"]["missing_observables"] = []
        with self.assertRaisesRegex(ValueError, "name the missing observable"):
            self.submit(sha, fork)
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        fork["propensities"]["negotiate/deal"] = {"support_deal": 0.8, "undercut": 0.2}
        with self.assertRaisesRegex(ValueError, "within 0.1 of uniform"):
            self.submit(sha, fork)
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        fork["memos"]["negotiate/deal"]["evidence"] = ["X1"]
        self.assertTrue(self.submit(sha, fork)["accepted"])

    def test_changed_scenario_invalidates_the_packet(self):
        sha = self.freeze()["packet_sha256"]
        path = self.sim / "config" / "scenario_iran_oil.yaml"
        path.write_text(SCENARIO_YAML.replace("0.35", "0.40"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed after packet freeze"):
            self.submit(sha, make_fork(PILOT_TEMPERAMENTS[0], self.tree))


class PacketRunTest(PacketHarness):
    def fake_simulator(self, calls):
        def run(args, **kwargs):
            calls.append(args)
            out = Path(args[args.index("--out-dir") + 1])
            result = {"run_id": "r" * 24, "authority": "RESEARCH_PILOT_ONLY_UNCALIBRATED",
                      "validation": {"crosscheck_status": "PASS"},
                      "ensemble_mc": {"oil_range": 0.6}, "mc_wilson_95": {},
                      "model_form_range": {}, "expected_reward": {}, "limitations": []}
            (out / "pilot_result.json").write_text(json.dumps(result), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return run

    def test_packet_run_needs_three_forks_and_an_exact_fork_order(self):
        sha = self.freeze()["packet_sha256"]
        for temperament in PILOT_TEMPERAMENTS[:2]:
            self.submit(sha, make_fork(temperament, self.tree))
        with patch.object(server.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "at least 3"):
                server.run_market_actor_sim(packet_sha256=sha)
            self.submit(sha, make_fork(PILOT_TEMPERAMENTS[2], self.tree))
            with self.assertRaisesRegex(ValueError, "fork_order must list exactly"):
                server.run_market_actor_sim(packet_sha256=sha, fork_order=PILOT_TEMPERAMENTS[:2])
            with self.assertRaisesRegex(ValueError, "either packet_sha256"):
                server.run_market_actor_sim("a.yaml", packet_sha256=sha)
            run.assert_not_called()

    def test_packet_run_feeds_packet_inputs_and_writes_a_manifest(self):
        sha = self.freeze()["packet_sha256"]
        for temperament in PILOT_TEMPERAMENTS:
            self.submit(sha, make_fork(temperament, self.tree))
        calls = []
        with patch.object(server.subprocess, "run", side_effect=self.fake_simulator(calls)):
            result = server.run_market_actor_sim(packet_sha256=sha, fork_order=PILOT_TEMPERAMENTS,
                                                 rollouts=1000)
        args = calls[0]
        self.assertEqual(Path(args[args.index("--evidence") + 1]),
                         (self.home / "actor_packets" / sha / "packet.json").resolve())
        forks_file = Path(args[args.index("--forks") + 1])
        forks = json.loads(forks_file.read_text(encoding="utf-8"))
        self.assertEqual([f["temperament"] for f in forks["forks"]], PILOT_TEMPERAMENTS)
        self.assertEqual(result["anchor_status"], "elicited-only")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        for key in ("lake_signature_at_freeze", "lake_signature_at_run", "audit_receipt_sha256_at_freeze",
                    "audit_receipt_sha256_at_run", "engine", "extension", "ontology_version",
                    "graph_asof_utc", "presets", "skills", "model_config", "fork_sha256", "inputs"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["packet_sha256"], sha)
        self.assertEqual(manifest["graph_asof_utc"], "2026-08-28T12:00:00Z")
        self.assertEqual(manifest["fork_order"], PILOT_TEMPERAMENTS)
        self.assertIn("run_governed_pilot.py", manifest["engine"])


def _real_project():
    raw = os.environ.get("INVESTMENT_AI_PROJECT_ROOT", "").strip()
    if not raw:
        return None
    root = Path(raw)
    pilot = root / "market_actor_sim" / "runs" / "iran_oil_pit_pilot_20260828" / "pilot_result.json"
    return root if pilot.is_file() else None


@unittest.skipUnless(_real_project(), "needs INVESTMENT_AI_PROJECT_ROOT with the 2026-08-28 pilot")
class PilotReplayTest(PacketHarness):
    """Replay the 2026-08-28 pilot through freeze -> submit x3 -> packet run."""

    def test_replay_reproduces_the_pilot_ensemble_exactly(self):
        real = _real_project()
        (self.sim / "config" / "scenario_iran_oil.yaml").write_bytes(
            (real / "market_actor_sim" / "config" / "scenario_iran_oil.yaml").read_bytes())
        with patch.object(server, "project", return_value=real), \
             patch.object(server, "child_env", return_value={**os.environ, "PYTHONPATH": ""}):
            sha = self.freeze()["packet_sha256"]
            for temperament in PILOT_TEMPERAMENTS:
                self.submit(sha, make_fork(temperament, self.tree))
            result = server.run_market_actor_sim(packet_sha256=sha, fork_order=PILOT_TEMPERAMENTS,
                                                 seed=20260828, rollouts=300000)
        pilot = json.loads((real / "market_actor_sim" / "runs" / "iran_oil_pit_pilot_20260828"
                            / "pilot_result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["ensemble_mc"], pilot["ensemble_mc"])
        full = server.inspect_market_actor_run(result["run_key"])
        self.assertEqual(full["ensemble_exact"], pilot["ensemble_exact"])
        self.assertEqual(full["exact_by_temperament"], pilot["exact_by_temperament"])
        self.assertEqual(full["validation"]["crosscheck_status"], "PASS")


if __name__ == "__main__":
    unittest.main()
