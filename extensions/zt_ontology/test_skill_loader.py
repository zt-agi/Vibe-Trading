"""VT's own SkillsLoader loads the ontology-hypergraph user skill (ZT add-on).

The skill lives in the E: runtime profile, not in this repository:
    E:\\codex-runtime\\investment-ai\\vibe-trading\\profile\\.vibe-trading\\skills\\user\\ontology-hypergraph
Point ZT_ONTOLOGY_SKILL_DIR at it (or run VT with that profile as HOME) and:
    python -B -m unittest discover -s extensions/zt_ontology -p "test_skill_loader.py" -v
The test is skipped when the skill folder is absent.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "agent"))

SKILL_DIR = Path(os.environ.get("ZT_ONTOLOGY_SKILL_DIR",
                                Path.home() / ".vibe-trading" / "skills" / "user" / "ontology-hypergraph"))


@unittest.skipUnless((SKILL_DIR / "SKILL.md").is_file(), f"skill not installed at {SKILL_DIR}")
class SkillLoaderTest(unittest.TestCase):
    def loader(self, bundled=None):
        from src.agent.skills import SkillsLoader
        return SkillsLoader(skills_dir=bundled or (SKILL_DIR / "__no_bundled__"),
                            user_skills_dir=SKILL_DIR.parent)

    def test_vt_loader_reads_frontmatter(self):
        skills = {s.name: s for s in self.loader().skills}
        skill = skills["ontology-hypergraph"]
        self.assertEqual(skill.category, "research")
        self.assertIn("read-only", skill.description)
        self.assertNotIn("\n", skill.description)
        self.assertEqual(skill.dir_path, SKILL_DIR)

    def test_loads_next_to_bundled_skills_without_collision(self):
        from src.agent.skills import SkillsLoader
        bundled = SkillsLoader(user_skills_dir=SKILL_DIR / "__no_user__").skills
        combined = self.loader(bundled=REPO / "agent" / "src" / "skills").skills
        self.assertNotIn("ontology-hypergraph", {s.name for s in bundled})
        self.assertEqual(len(combined), len(bundled) + len([p for p in SKILL_DIR.parent.iterdir()
                                                          if (p / "SKILL.md").is_file()]))
        self.assertIn("ontology-hypergraph", self.loader(bundled=REPO / "agent" / "src" / "skills")
                      .get_descriptions())

    def test_load_skill_returns_body_naming_every_tool(self):
        body = self.loader().get_content("ontology-hypergraph")
        self.assertTrue(body.startswith('<skill name="ontology-hypergraph">'))
        for tool in ("onto_concept", "onto_node", "onto_neighbors", "onto_actor_roster", "onto_competency"):
            self.assertIn(tool, body)
        self.assertIn("UNKNOWN, not false", body)

    def test_contract_sidecar_matches_the_server(self):
        skill = {s.name: s for s in self.loader().skills}["ontology-hypergraph"]
        contract = json.loads(skill.load_support_file("skill_contract.json"))
        self.assertEqual(contract["skill"], "ontology-hypergraph")
        self.assertEqual(contract["effects"]["writes"], "none")
        tree = ast.parse((Path(__file__).with_name("server.py")).read_text(encoding="utf-8"))
        tools = sorted(f.name for f in tree.body if isinstance(f, ast.FunctionDef)
                       and any(getattr(d, "attr", None) == "tool" for d in f.decorator_list))
        self.assertEqual(sorted(contract["implementation"]["tools"]), tools)

    @unittest.skipUnless(os.environ.get("INVESTMENT_AI_PROJECT_ROOT"), "project root needed to check the pin")
    def test_contract_pins_the_current_release(self):
        contract = json.loads((SKILL_DIR / "skill_contract.json").read_text(encoding="utf-8"))
        root = Path(os.environ["INVESTMENT_AI_PROJECT_ROOT"])
        sys.path.insert(0, str(root / "implementation" / "pit_warehouse"))
        from pitdb import ontology_loader as L
        release = L.read_release(root / "ontology" / "finance_mvo")
        pin = contract["ontology_release"]
        self.assertEqual((pin["release_id"], pin["version"], pin["content_sha256"]),
                         (release.release_id, release.version, release.content_sha256))
        known = {r["concept_id"] for r in release.records}
        self.assertTrue(set(contract["applicability"]["acts_on"]) <= known)
        self.assertTrue(set(contract["applicability"]["answers"]) <= known)


if __name__ == "__main__":
    unittest.main()
