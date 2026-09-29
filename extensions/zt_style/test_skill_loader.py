"""VT's own SkillsLoader loads the investor-style-checklists user skill (ZT add-on).

The skill lives in the E: runtime home, not in this repository:
    E:\\codex-runtime\\investment-ai\\vibe-trading\\home\\skills\\user\\investor-style-checklists
(the profile's .vibe-trading junction points at home). Point ZT_STYLE_SKILL_DIR at it, or
run with that profile as HOME, and:
    python -B -m unittest discover -s extensions/zt_style -p "test_skill_loader.py" -v
The test is skipped when the skill folder is absent.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(HERE))

import style_rules  # noqa: E402

SKILL_DIR = Path(os.environ.get("ZT_STYLE_SKILL_DIR",
                                Path.home() / ".vibe-trading" / "skills" / "user" / "investor-style-checklists"))


@unittest.skipUnless((SKILL_DIR / "SKILL.md").is_file(), f"skill not installed at {SKILL_DIR}")
class SkillLoaderTest(unittest.TestCase):
    def loader(self, bundled=None):
        from src.agent.skills import SkillsLoader
        return SkillsLoader(skills_dir=bundled or (SKILL_DIR / "__no_bundled__"), user_skills_dir=SKILL_DIR.parent)

    def test_vt_loader_reads_frontmatter(self):
        skill = {s.name: s for s in self.loader().skills}["investor-style-checklists"]
        self.assertEqual(skill.category, "analysis")
        self.assertIn("read-only", skill.description)
        self.assertIn("never a probability", skill.description)
        self.assertNotIn("\n", skill.description)
        self.assertEqual(skill.dir_path, SKILL_DIR)

    def test_loads_next_to_bundled_skills_without_collision(self):
        from src.agent.skills import SkillsLoader
        bundled = SkillsLoader(user_skills_dir=SKILL_DIR / "__no_user__").skills
        self.assertNotIn("investor-style-checklists", {s.name for s in bundled})
        combined = self.loader(bundled=REPO / "agent" / "src" / "skills")
        self.assertIn("investor-style-checklists", combined.get_descriptions())

    def test_body_names_the_tools_and_every_rule(self):
        body = self.loader().get_content("investor-style-checklists")
        self.assertTrue(body.startswith('<skill name="investor-style-checklists">'))
        for tool in ("mcp_zt_style_investor_style_scores", "freeze_evidence_packet", "score_contamination_guess",
                     "unseal_evidence_packet", "submit_role_fork"):
            self.assertIn(tool, body)
        for key in style_rules.RULES:
            family, component = key.split(".")
            self.assertIn(f"| {family} | {component} |", body)
        for template in ("value_checklist", "garp_growth", "macro_inflection"):
            self.assertIn(template, body)


if __name__ == "__main__":
    unittest.main()
