"""ZT add-on tests: role-fork templates bound to scenario actors.

    python -m unittest test_role_templates     (VIBE_TRADING_HOME set, E: on Windows)

The mechanics run on two small fixture templates. With INVESTMENT_AI_PROJECT_ROOT
set, RealTemplatesTest also validates the three installed templates
(vt_addons/role_fork_templates/{value_checklist,garp_growth,macro_inflection}.yaml).
"""
from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import fork_rules
import role_templates
import server
from test_packets import PacketHarness, setUpModule  # noqa: F401  (setUpModule guards E:)

TEMPLATE_SCENARIO = """\
name: template_fixture
description: Issuer guidance decision, then a value-fund aggregate and a long/short aggregate respond.
headline_outcomes: [rerate_up]
rewards: {rerate_up: 1.0, flat: 0.5, derate_down: 0.0}
actor_roles:
  value_funds: {role_class: ActiveMutualFunds, sub_role: value, template: value_checklist}
  ls_books: {role_class: LongShortHedgeFunds, template: macro_inflection}
tree:
  actor: issuer_management
  actions:
    raise_guidance:
      child:
        actor: value_funds
        actions:
          accumulate:
            child:
              actor: ls_books
              actions:
                press_long: {outcome: rerate_up}
                initiate_short: {outcome: flat}
          hold_position: {outcome: flat}
          sell_short: {outcome: derate_down}
    hold_guidance: {outcome: flat}
"""
FIXTURE_TEMPLATES = {
    "value_checklist": """\
schema: vt.role_fork_template.v1
template_id: value_checklist
role_name: VALUE_CHECKLIST_ALLOCATOR
role_class: ActiveMutualFunds
sub_role: value
class_kind: Aggregate
investment_approach: long_only
forbidden_directions: [short, cover]
purpose: Fixture value-oriented aggregate.
checklist:
  - {id: V1, item: Price against demonstrated earnings, read: ["price rows and annual earnings rows"], required: true}
  - {id: V2, item: Financial strength, read: ["balance-sheet rows"], required: true}
  - {id: V3, item: Earnings stability, read: ["annual earnings rows"], required: false}
output_schema: {propensities: state-local, evidence: packet ids, memo: one checklist entry per item,
                forbidden_actions: the floor}
""",
    "macro_inflection": """\
schema: vt.role_fork_template.v1
template_id: macro_inflection
role_name: INFLECTION_SEEKING_LONG_SHORT_BOOK
role_class: LongShortHedgeFunds
class_kind: Aggregate
investment_approach: long_short
forbidden_directions: []
purpose: Fixture long/short aggregate.
checklist:
  - {id: M1, item: Direction of growth, read: ["revenue rows"], required: true}
  - {id: M2, item: Margin inflection, read: ["margin rows"], required: false}
  - {id: M3, item: What the price implies, read: ["price rows"], required: false}
output_schema: {propensities: state-local, evidence: packet ids, memo: one checklist entry per item,
                forbidden_actions: none}
""",
}


def analysis(actor: str, actions: list[str]) -> str:
    return (f"The {actor.replace('_', ' ')} aggregate weighs {', '.join(a.replace('_', ' ') for a in actions)} "
            "against its own incentives and constraints, reading only the frozen packet rows; the cited rows "
            "are observations of conditions, never copied into the vector.")


def template_fork(label: str, **changes) -> dict:
    fork = {
        "temperament": label,
        "memos": {
            "": {"actor": "issuer_management", "analysis": analysis("issuer_management",
                                                                    ["raise_guidance", "hold_guidance"]),
                 "evidence": ["E1"], "missing_observables": []},
            "raise_guidance": {
                "actor": "value_funds",
                "analysis": analysis("value_funds", ["accumulate", "hold_position", "sell_short"]),
                "evidence": ["E1", "P1"], "missing_observables": ["current ratio"],
                "checklist": {"V1": "P1 against E1: the price path and the frozen expectations row",
                              "V2": "MISSING: no balance-sheet row was frozen",
                              "V3": "MISSING: one period only"}},
            "raise_guidance/accumulate": {
                "actor": "ls_books", "analysis": analysis("ls_books", ["press_long", "initiate_short"]),
                "evidence": ["E2", "P1"], "missing_observables": [],
                "checklist": {"M1": "E2 moved against the older reading", "M2": "MISSING: no margin row",
                              "M3": "P1 closes the window at the t-0 level"}},
        },
        "propensities": {
            "": {"raise_guidance": 0.6, "hold_guidance": 0.4},
            "raise_guidance": {"accumulate": 0.55, "hold_position": 0.449999, "sell_short": 0.000001},
            "raise_guidance/accumulate": {"press_long": 0.6, "initiate_short": 0.4},
        },
    }
    for path, value in changes.items():
        target = fork
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        if value is None:
            target.pop(keys[-1], None)
        else:
            target[keys[-1]] = value
    return fork


class TemplateHarness(PacketHarness):
    def setUp(self):
        super().setUp()
        (self.sim / "config" / "scenario_templates.yaml").write_text(TEMPLATE_SCENARIO, encoding="utf-8",
                                                                      newline="\n")
        folder = self.root / "vt_addons" / "role_fork_templates"
        folder.mkdir(parents=True)
        for name, text in FIXTURE_TEMPLATES.items():
            (folder / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
        self.template_tree = fork_rules.parse_scenario(TEMPLATE_SCENARIO)

    def freeze_templates(self, **overrides):
        return self.freeze(scenario_id="templates", memos=[], excluded_or_missing=[], **overrides)

    def submit_fork(self, sha, fork):
        return server.submit_role_fork(sha, fork["temperament"], fork["memos"], fork["propensities"])


class ScenarioBindingTest(unittest.TestCase):
    def test_actor_roles_and_directions_parse_and_render(self):
        tree = fork_rules.parse_scenario(TEMPLATE_SCENARIO)
        self.assertEqual(tree.actor_roles["value_funds"],
                         {"role_class": "ActiveMutualFunds", "sub_role": "value", "template": "value_checklist"})
        view = {state["state_key"]: state for state in tree.fork_view()}
        self.assertEqual(view["raise_guidance"]["role_template"], "value_checklist")
        self.assertEqual(view["raise_guidance"]["role_class"], "ActiveMutualFunds")
        self.assertNotIn("role_class", view[""])
        declared = TEMPLATE_SCENARIO.replace("hold_position: {outcome: flat}",
                                             "hold_position: {outcome: flat, direction: neutral}")
        states = {s["state_key"]: s for s in fork_rules.parse_scenario(declared).fork_view()}
        self.assertEqual([a.get("direction") for a in states["raise_guidance"]["actions"]], [None, "neutral", None])

    def test_scenarios_without_bindings_render_exactly_as_before(self):
        from test_packets import SCENARIO_YAML
        for state in fork_rules.parse_scenario(SCENARIO_YAML).fork_view():
            self.assertEqual(set(state), {"state_key", "actor", "actions"})
            for action in state["actions"]:
                self.assertLessEqual(set(action), {"action", "leads_to_state", "terminal"})

    def test_bad_bindings_are_refused(self):
        for bad, message in (
                (TEMPLATE_SCENARIO.replace("  ls_books: {role_class", "  ghosts: {role_class"), "no actor of the tree"),
                (TEMPLATE_SCENARIO.replace("sub_role: value, ", "sub_role: value, weight: 2, "), "unknown keys"),
                (TEMPLATE_SCENARIO.replace("template: macro_inflection", "template: Macro Inflection"), "template id"),
                (TEMPLATE_SCENARIO.replace("press_long: {outcome: rerate_up}",
                                           "press_long: {outcome: rerate_up, direction: sideways}"), "direction must"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                fork_rules.parse_scenario(bad)

    def test_direction_inference(self):
        cases = {"sell_short": "short", "initiate_short": "short", "buy_to_cover": "cover", "trim_long": "reduce",
                 "accumulate": "long", "press_long": "long", "hold_position": "neutral", "fade_spike": "unspecified",
                 "risk_off_chase": "unspecified", "sellShort": "short"}
        for label, expected in cases.items():
            self.assertEqual(role_templates.action_direction(label, None), expected, label)
        self.assertEqual(role_templates.action_direction("fade_spike", "short"), "short")


class TemplateValidationTest(unittest.TestCase):
    def body(self, name="value_checklist", **changes):
        body = yaml.safe_load(FIXTURE_TEMPLATES[name])
        body.update(changes)
        return body

    def test_fixture_templates_validate(self):
        for name in FIXTURE_TEMPLATES:
            role_templates.validate_template(self.body(name), name)

    def test_persona_names_weights_years_and_mechanical_flows_are_refused(self):
        cases = [
            (self.body(purpose="Allocate the way Graham would."), "neutral role names"),
            (self.body(role_name="BUFFETT_STYLE_ALLOCATOR"), "neutral role names"),
            (self.body(checklist=[*self.body()["checklist"], {"id": "V4", "item": "moat", "read": ["rows"],
                                                              "required": False, "weight": 0.3}]), "coefficients"),
            (self.body(purpose="As in the 2008 crisis."), "calendar year"),
            (self.body(role_class="LeveragedETFRebalance"), "NatureNode"),
            (self.body(role_class="Hedgies"), "role_class must be"),
            (self.body(forbidden_directions=["short"]), "must forbid"),
            (self.body("macro_inflection", forbidden_directions=["short"]), "forbids no direction"),
            (self.body(checklist=self.body()["checklist"][:2]), "3 to 12 items"),
            (self.body(role_name="value allocator"), "upper-case"),
            (self.body(output_schema={"propensities": "x"}), "output_schema"),
            (self.body(template_id="other"), "file name"),
        ]
        for body, message in cases:
            with self.assertRaisesRegex(role_templates.TemplateError, message):
                role_templates.validate_template(body, "value_checklist")


class TemplateSubmissionTest(TemplateHarness):
    def test_freeze_records_template_hashes_and_the_fork_view_carries_the_contract(self):
        frozen = self.freeze_templates()
        self.assertEqual(frozen["counts"]["role_templates"], 2)
        packet = server.load_packet(frozen["packet_sha256"])
        folder = self.root / "vt_addons" / "role_fork_templates"
        self.assertEqual(packet["scenario"]["role_templates"]["value_checklist"]["sha256"],
                         server.sha256_hex((folder / "value_checklist.yaml").read_bytes()))
        view = server.inspect_evidence_packet(frozen["packet_sha256"], "role_fork")
        value = view["role_templates"]["value_checklist"]
        self.assertEqual(value["role_name"], "VALUE_CHECKLIST_ALLOCATOR")
        self.assertEqual(value["applies_to"][0]["forbidden_actions"], ["sell_short"])
        self.assertEqual(value["applies_to"][0]["action_directions"],
                         {"accumulate": "long", "hold_position": "neutral", "sell_short": "short"})
        self.assertEqual(view["role_templates"]["macro_inflection"]["applies_to"][0]["forbidden_actions"], [])
        self.assertIn("role_template_contract", view)
        for hidden in ("rerate_up", "derate_down", "rewards"):
            self.assertNotIn(hidden, json.dumps(view))

    def test_a_template_fork_is_accepted_and_keeps_its_checklist(self):
        sha = self.freeze_templates()["packet_sha256"]
        result = self.submit_fork(sha, template_fork("VALUE_TILTED"))
        self.assertTrue(result["accepted"])
        stored = server.accepted_forks(sha)[0]
        self.assertEqual(stored["memos"]["raise_guidance"]["checklist"]["V2"],
                         "MISSING: no balance-sheet row was frozen")
        self.assertNotIn("checklist", stored["memos"][""])

    def test_checklist_rules(self):
        sha = self.freeze_templates()["packet_sha256"]
        cases = [
            (template_fork("NO_LIST", **{"memos.raise_guidance.checklist": None}), "needs memo.checklist"),
            (template_fork("SHORT_LIST", **{"memos.raise_guidance.checklist": {"V1": "P1 shows it"}}),
             r"missing \['V2', 'V3'\]"),
            (template_fork("EXTRA", **{"memos.raise_guidance/accumulate.checklist.M9": "E1"}), "not in the template"),
            (template_fork("UNCITED", **{"memos.raise_guidance.checklist.V1": "the price looks rich"}),
             "cites no packet id"),
            (template_fork("UNLISTED", **{"memos.raise_guidance.checklist.V1": "E5 and P1 show it"}),
             r"cites \['E5'\]; list them in memo.evidence"),
            (template_fork("BARE_MISSING", **{"memos.raise_guidance.checklist.V2": "MISSING"}),
             "without naming what is absent"),
            (template_fork("OUTCOME", **{"memos.raise_guidance/accumulate.checklist.M1": "E2 says a rerate up"}),
             "terminal outcome"),
        ]
        for fork, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                self.submit_fork(sha, fork)
        self.assertEqual(server.accepted_forks(sha), [])

    def test_direction_permissions_of_a_long_only_role(self):
        sha = self.freeze_templates()["packet_sha256"]
        heavy = {"accumulate": 0.5, "hold_position": 0.45, "sell_short": 0.05}
        with self.assertRaisesRegex(ValueError, r"forbid mass on \['sell_short'\]"):
            self.submit_fork(sha, template_fork("SHORTER", **{"propensities.raise_guidance": heavy}))
        zero = {"accumulate": 0.55, "hold_position": 0.45, "sell_short": 0.0}
        with self.assertRaisesRegex(ValueError, "strictly between 0 and 1"):
            self.submit_fork(sha, template_fork("ZERO", **{"propensities.raise_guidance": zero}))
        # a long/short role may put real mass on its short action
        self.assertTrue(self.submit_fork(sha, template_fork("LS_SHORT", **{
            "propensities.raise_guidance/accumulate": {"press_long": 0.2, "initiate_short": 0.8}}))["accepted"])

    def test_no_role_evidence_means_near_uniform_over_the_permitted_actions(self):
        sha = self.freeze_templates()["packet_sha256"]
        all_missing = {"V1": "MISSING: no price row for this aggregate",
                       "V2": "MISSING: no balance-sheet row", "V3": "MISSING: one period only"}
        tilted = template_fork("TILTED", **{"memos.raise_guidance.checklist": all_missing,
                                            "propensities.raise_guidance": {"accumulate": 0.8,
                                                                            "hold_position": 0.199999,
                                                                            "sell_short": 0.000001}})
        with self.assertRaisesRegex(ValueError, "every required checklist item is MISSING"):
            self.submit_fork(sha, tilted)
        level = template_fork("LEVEL", **{"memos.raise_guidance.checklist": all_missing,
                                          "propensities.raise_guidance": {"accumulate": 0.55,
                                                                          "hold_position": 0.449999,
                                                                          "sell_short": 0.000001}})
        self.assertTrue(self.submit_fork(sha, level)["accepted"])
        # fork_rules' own uncited rule also measures uniform over the permitted actions
        uncited = template_fork("UNCITED_STATE", **{
            "memos.raise_guidance.evidence": [], "memos.raise_guidance.checklist": all_missing,
            "propensities.raise_guidance": {"accumulate": 0.5, "hold_position": 0.499999, "sell_short": 0.000001}})
        self.assertTrue(self.submit_fork(sha, uncited)["accepted"])

    def test_a_template_changed_after_the_freeze_is_refused(self):
        sha = self.freeze_templates()["packet_sha256"]
        path = self.root / "vt_addons" / "role_fork_templates" / "value_checklist.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("Fixture value", "Edited value"),
                        encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "changed after packet freeze"):
            self.submit_fork(sha, template_fork("LATE"))
        with self.assertRaisesRegex(ValueError, "changed after packet freeze"):
            server.inspect_evidence_packet(sha, "role_fork")

    def test_a_missing_template_blocks_the_freeze(self):
        (self.root / "vt_addons" / "role_fork_templates" / "macro_inflection.yaml").unlink()
        with self.assertRaisesRegex(ValueError, "not installed"):
            self.freeze_templates()
        self.assertFalse((self.home / "actor_packets").exists())

    def test_the_packet_run_revalidates_template_forks(self):
        sha = self.freeze_templates()["packet_sha256"]
        labels = ["VALUE_TILTED", "BASE_RATE", "STRESS"]
        for label in labels:
            self.submit_fork(sha, template_fork(label))
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            out = Path(args[args.index("--out-dir") + 1])
            (out / "pilot_result.json").write_text(json.dumps({
                "run_id": "t" * 24, "authority": "RESEARCH_PILOT_ONLY_UNCALIBRATED",
                "validation": {"crosscheck_status": "PASS"}, "ensemble_mc": {}, "mc_wilson_95": {},
                "limitations": []}), encoding="utf-8", newline="\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(server.subprocess, "run", side_effect=run):
            result = server.run_market_actor_sim(packet_sha256=sha, fork_order=labels, rollouts=1000)
        forks_file = json.loads(Path(calls[0][calls[0].index("--forks") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(forks_file["forks"][0]["memos"]["raise_guidance"]["checklist"]["V3"],
                         "MISSING: one period only")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["role_templates"]), ["macro_inflection", "value_checklist"])
        self.assertIn("role_templates.py", manifest["extension"])


def _templates_dir() -> Path | None:
    raw = os.environ.get("ZT_ROLE_TEMPLATES_DIR", "").strip()
    if raw:
        return Path(raw)
    root = os.environ.get("INVESTMENT_AI_PROJECT_ROOT", "").strip()
    folder = Path(root) / "vt_addons" / "role_fork_templates" if root else None
    return folder if folder and folder.is_dir() else None


@unittest.skipUnless(_templates_dir(), "needs the installed templates (INVESTMENT_AI_PROJECT_ROOT or "
                                       "ZT_ROLE_TEMPLATES_DIR)")
class RealTemplatesTest(unittest.TestCase):
    """The three templates ZT installs under vt_addons/role_fork_templates."""

    def test_the_three_templates_validate_and_map_to_her_role_classes(self):
        folder = _templates_dir()
        root = folder.parents[1]
        loaded = {t.template_id: t for t in role_templates.list_templates(root)} if folder.name == \
            "role_fork_templates" and folder.parent.name == "vt_addons" else {}
        self.assertTrue({"value_checklist", "garp_growth", "macro_inflection"} <= set(loaded))
        expected = {"value_checklist": ("ActiveMutualFunds", "value", "long_only"),
                    "garp_growth": ("ActiveMutualFunds", "garp", "long_only"),
                    "macro_inflection": ("LongShortHedgeFunds", None, "long_short")}
        for template_id, (role_class, sub_role, approach) in expected.items():
            body = loaded[template_id].body
            self.assertEqual((body["role_class"], body.get("sub_role"), body["investment_approach"]),
                             (role_class, sub_role, approach))
            text = (folder / f"{template_id}.yaml").read_text(encoding="utf-8").casefold()
            for name in role_templates.BANNED_NAMES:
                self.assertNotIn(name, text, f"{template_id} names {name}")
            self.assertNotIn("probabilit", text.split("\nschema:")[1])
        self.assertEqual(set(loaded["value_checklist"].forbidden_directions), {"short", "cover"})
        self.assertEqual(loaded["macro_inflection"].forbidden_directions, ())


if __name__ == "__main__":
    unittest.main()
