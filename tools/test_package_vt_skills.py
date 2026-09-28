"""Tests for tools/package_vt_skills.py (ZT add-on).

Run with::

    pytest tools/test_package_vt_skills.py -v

Optional checks against the real artifacts run when these are set:
``ZT_VT_SKILL_CONTRACTS`` (the contracts registry) and ``ZT_VT_USER_SKILLS``
(a packaged user-skills folder).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from package_vt_skills import (
    SIDECAR_NAME,
    PackagingError,
    _inside,
    contract_applies,
    drive_problem,
    keyword_overlap,
    load_contracts,
    main,
    package_skills,
    verify_packaged,
)
from src.agent.frontmatter import parse_frontmatter
from src.agent.skills import SkillsLoader
from src.governance.manifest import SkillRecord

BODY = (
    "\n# Demo skill\n\nKeep this body byte-identical, including trailing spaces   \n"
    "and a later fence that is not frontmatter:\n\n---\n\nSee [ref](references/ref.md).\n"
)

CONTRACT = {
    "canonical_path": "G:\\My Drive\\work\\Shared-AI-Workspace-Core\\skills\\demo-skill\\SKILL.md",
    "vt_category": "analysis",
    "contract_status": "DRAFT_PENDING_ZT_REVIEW",
    "vt_tools": {"available": ["quantlib_call"], "planned": []},
    "contract": {
        "is_a": "DemoSkill",
        "acts_on": ["InvestmentThesis"],
        "requires": [["ScenarioBasis", "ResearchPipelineIntake"]],
        "requires_context": ["OutcomeForecastRequested"],
        "used_by_role": ["Investor"],
        "produces": ["DemoArtifact"],
        "depends_on": [],
        "blocked_by": ["PITAuditFailed"],
    },
    "test_cases": {
        "paraphrase": [
            {"id": "p1", "prompt": "Forecast the oil thesis.", "expect": True,
             "situation": {"entities": ["InvestmentThesis"], "concepts": ["ScenarioBasis"],
                           "context": ["OutcomeForecastRequested"], "role": "Investor"}},
            {"id": "p2", "prompt": "What happens to crude under escalation?", "expect": True,
             "situation": {"entities": ["InvestmentThesis"], "concepts": ["ResearchPipelineIntake"],
                           "context": ["OutcomeForecastRequested"], "role": "Investor"}},
        ],
        "historical": [
            {"id": "h1", "prompt": "2026-08-28 pilot.", "expect": True,
             "situation": {"entities": ["InvestmentThesis"], "concepts": ["ScenarioBasis"],
                           "context": ["OutcomeForecastRequested"], "role": "Investor"}},
        ],
        "negative_control": [
            {"id": "n1", "prompt": "Forecast the thesis although the demo audit failed.", "expect": False,
             "situation": {"entities": ["InvestmentThesis"], "concepts": ["ScenarioBasis"],
                           "context": ["OutcomeForecastRequested", "PITAuditFailed"], "role": "Investor"}},
        ],
    },
}


def write_skill(root: Path, frontmatter: str, body: str = BODY, dirname: str = "demo-skill") -> Path:
    skill = root / dirname
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "__pycache__").mkdir()
    (skill / "SKILL.md").write_bytes(f"---\n{frontmatter}---\n{body}".encode("utf-8"))
    (skill / "references" / "ref.md").write_text("reference text\n", encoding="utf-8")
    (skill / "scripts" / "tool.py").write_text("print('run by subscription agents')\n", encoding="utf-8")
    (skill / "desktop.ini").write_text("[.ShellClassInfo]\n", encoding="utf-8")
    (skill / "__pycache__" / "tool.cpython-313.pyc").write_bytes(b"\x00")
    return skill


QUOTED = 'name: "demo-skill"\ndescription: "Demo pack: scenario control and research-pipeline intake."\n'
PLAIN = "name: demo-skill\ndescription: Demo pack for tests.\ncategory: analysis\n"


@pytest.fixture
def contracts():
    return {"demo-skill": copy.deepcopy(CONTRACT)}


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "profile" / ".vibe-trading" / "skills" / "user"


def empty_bundled(tmp_path: Path) -> Path:
    return tmp_path / "no-bundled"


def test_packaged_skill_loads_in_vt_with_its_body_unchanged(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", QUOTED)
    report = package_skills([source], dest, contracts)
    assert [s["action"] for s in report["skills"]] == ["create"]

    loader = SkillsLoader(skills_dir=empty_bundled(tmp_path), user_skills_dir=dest)
    (skill,) = loader.skills
    assert skill.name == "demo-skill"  # VT alone would have read '"demo-skill"'
    assert skill.category == "analysis"
    source_text = (source / "SKILL.md").read_text(encoding="utf-8")
    assert skill.body == parse_frontmatter(source_text)[1]
    packaged = (dest / "demo-skill" / "SKILL.md").read_bytes()
    assert packaged.endswith(f"---\n{BODY}".encode("utf-8"))  # raw body bytes, fence included
    assert loader.get_content("demo-skill").startswith('<skill name="demo-skill">')
    assert (dest / "demo-skill" / "references" / "ref.md").read_text(encoding="utf-8") == "reference text\n"
    assert not (dest / "demo-skill" / "desktop.ini").exists()
    assert not (dest / "demo-skill" / "__pycache__").exists()

    # The sidecar does not disturb VT's loader alongside the real bundled skills.
    bundled = SkillsLoader(user_skills_dir=dest)
    alone = SkillsLoader(user_skills_dir=empty_bundled(tmp_path))
    assert len(bundled.skills) == len(alone.skills) + 1
    assert "demo-skill" in {s.name for s in bundled.skills}
    assert "demo-skill" in bundled.get_descriptions()


def test_sidecar_carries_the_contract_and_verifiable_provenance(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", QUOTED)
    package_skills([source], dest, contracts)
    sidecar = json.loads((dest / "demo-skill" / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["contract"] == CONTRACT["contract"]
    assert sidecar["contract_status"] == "DRAFT_PENDING_ZT_REVIEW"
    prov = sidecar["provenance"]
    assert prov["source_sha256"] == "sha256:" + hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()
    assert prov["canonical_path"] == CONTRACT["canonical_path"]
    loaded = SkillsLoader(skills_dir=empty_bundled(tmp_path), user_skills_dir=dest).skills[0]
    assert prov["vt_skill_record"]["content_hash"] == SkillRecord.from_content("demo-skill", loaded.body).content_hash
    assert [c["key"] for c in prov["frontmatter_changes"]] == ["name", "category"]
    assert prov["frontmatter_changes"][0]["change"] == "unquoted"
    assert {f["path"] for f in prov["files"]} == {"SKILL.md", "references/ref.md", "scripts/tool.py"}
    # The quoted description is left exactly as written: only name/category are normalized.
    assert '"Demo pack: scenario control' in (dest / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert verify_packaged(dest, canonical_sources={"demo-skill": source / "SKILL.md"})["ok"]


def test_nothing_changes_when_nothing_is_missing(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", PLAIN)
    report = package_skills([source], dest, contracts)
    assert report["skills"][0]["frontmatter_changes"] == []
    assert (dest / "demo-skill" / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


def test_dry_run_writes_nothing(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", QUOTED)
    report = package_skills([source], dest, contracts, dry_run=True)
    assert report["dry_run"] is True
    assert report["skills"][0]["action"] == "create"
    assert "SKILL.md" in report["skills"][0]["files"] and SIDECAR_NAME in report["skills"][0]["files"]
    assert not dest.exists()
    assert main(["--dest", str(dest), "--contracts", str(write_contracts(tmp_path, contracts)),
                 "--skill", str(source), "--dry-run"]) == 0
    assert not dest.exists()


def write_contracts(tmp_path: Path, contracts: dict) -> Path:
    path = tmp_path / "VT_SKILL_CONTRACTS.json"
    path.write_text(json.dumps({"schema": "zt-vt-skill-contracts/1", "skills": contracts}), encoding="utf-8")
    return path


def test_rerun_is_idempotent_and_refresh_needs_replace(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", QUOTED)
    package_skills([source], dest, contracts)
    snapshot = dest / "demo-skill" / "SKILL.md"
    assert not os.stat(snapshot).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert package_skills([source], dest, contracts)["skills"][0]["action"] == "unchanged"
    (source / "SKILL.md").chmod(0o644)
    (source / "SKILL.md").write_bytes((source / "SKILL.md").read_bytes() + b"\nNew canonical line.\n")
    with pytest.raises(PackagingError, match="--replace"):
        package_skills([source], dest, contracts)
    report = package_skills([source], dest, contracts, replace=True)
    assert report["skills"][0]["action"] == "replace"
    assert snapshot.read_bytes().endswith(b"New canonical line.\n")
    assert verify_packaged(dest)["ok"]


def test_refuses_to_write_outside_the_destination(tmp_path, contracts, dest):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = write_skill(tmp_path / "src", QUOTED)
    dest.mkdir(parents=True)
    (dest / "demo-skill").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackagingError, match="outside the destination"):
        package_skills([source], dest, contracts, replace=True)
    assert list(outside.iterdir()) == []
    with pytest.raises(PackagingError, match="outside the destination"):
        _inside(dest.resolve(), dest / ".." / "escape")
    evil = write_skill(tmp_path / "src2", 'name: "../../evil"\ndescription: x\n', dirname="evil")
    with pytest.raises(PackagingError, match="slug"):
        package_skills([evil], tmp_path / "dest2", contracts)
    linked = write_skill(tmp_path / "src3", QUOTED)
    (linked / "references" / "leak.md").symlink_to(tmp_path / "outside")
    with pytest.raises(PackagingError, match="symlinked"):
        package_skills([linked], tmp_path / "dest3", contracts)
    assert not (tmp_path / "dest2").exists() and not (tmp_path / "dest3").exists()


def test_windows_drive_rule():
    assert drive_problem(r"C:\Users\zoe\.vibe-trading\skills\user", os_name="nt", system_drive="C:")
    assert drive_problem(r"D:\vibe\profile", os_name="nt", system_drive="C:")
    assert drive_problem(r"E:\codex-runtime\investment-ai\vibe-trading\profile\.vibe-trading\skills\user",
                         os_name="nt", system_drive="C:") is None
    assert drive_problem(r"C:\x", os_name="posix") is None


def test_contract_failures_and_vt_unreadable_frontmatter_block_packaging(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", QUOTED)
    broken = copy.deepcopy(contracts)
    broken["demo-skill"]["contract"]["blocked_by"] = []  # the negative control would now trigger
    with pytest.raises(PackagingError, match="contract test cases fail"):
        package_skills([source], dest, broken)
    missing = copy.deepcopy(contracts)
    del missing["demo-skill"]["contract"]["requires_context"]
    with pytest.raises(PackagingError, match="missing semantic fields"):
        load_contracts(write_contracts(tmp_path, missing))
    with pytest.raises(PackagingError, match="no semantic contract"):
        package_skills([source], dest, {})
    block = write_skill(tmp_path / "src4", "name: demo-skill\ndescription: >\n  folded text\n")
    with pytest.raises(PackagingError, match="block scalar|continuation"):
        package_skills([block], tmp_path / "dest4", contracts)
    nodesc = write_skill(tmp_path / "src5", "name: demo-skill\n")
    with pytest.raises(PackagingError, match="vt_description"):
        package_skills([nodesc], tmp_path / "dest5", contracts)
    assert not dest.exists()


def test_a_bundled_vt_skill_is_never_shadowed_silently(tmp_path, contracts):
    contracts["alpha-zoo"] = copy.deepcopy(CONTRACT)
    source = write_skill(tmp_path / "src", "name: alpha-zoo\ndescription: shadow\n", dirname="alpha-zoo")
    with pytest.raises(PackagingError, match="shadow a bundled VT skill"):
        package_skills([source], tmp_path / "dest", contracts)
    report = package_skills([source], tmp_path / "dest", contracts, allow_bundled_override=True)
    assert report["skills"][0]["action"] == "create"


def test_matching_reads_the_situation_never_the_wording():
    cases = [c for kind in ("paraphrase", "historical", "negative_control") for c in CONTRACT["test_cases"][kind]]
    prompts = [c["prompt"] for c in cases]
    for shift in range(len(cases)):
        for case, prompt in zip(cases, prompts[shift:] + prompts[:shift]):
            applies, _ = contract_applies(CONTRACT["contract"], {**case["situation"], "prompt": prompt})
            assert applies is case["expect"]
    applies, reasons = contract_applies(CONTRACT["contract"], {"entities": [], "concepts": [], "context": []})
    assert not applies and any("acts_on" in r for r in reasons)


def test_cli_package_then_verify(tmp_path, contracts, dest):
    source = write_skill(tmp_path / "src", QUOTED)
    path = write_contracts(tmp_path, contracts)
    assert main(["--dest", str(dest), "--contracts", str(path), "--skill", str(source)]) == 0
    assert main(["--verify", "--dest", str(dest)]) == 0
    (dest / "demo-skill" / "references" / "ref.md").chmod(0o644)
    (dest / "demo-skill" / "references" / "ref.md").write_text("tampered\n", encoding="utf-8")
    assert main(["--verify", "--dest", str(dest)]) == 1


# --- optional: the real registry and the real packaged folder -----------------


@pytest.mark.skipif(not os.environ.get("ZT_VT_SKILL_CONTRACTS"), reason="ZT_VT_SKILL_CONTRACTS not set")
def test_real_contract_registry_cases_pass():
    registry = load_contracts(os.environ["ZT_VT_SKILL_CONTRACTS"])
    assert set(registry) >= {"investment-mct-setup", "montecarlo-outcome-forecasting",
                             "alpha-signal-monitoring", "fidelity-broker-export"}
    for name, entry in registry.items():
        for kind in ("paraphrase", "historical", "negative_control"):
            for case in entry["test_cases"][kind]:
                applies, reasons = contract_applies(entry["contract"], case["situation"])
                assert applies is case["expect"], (name, case["id"], reasons)


@pytest.mark.skipif(not os.environ.get("ZT_VT_USER_SKILLS"), reason="ZT_VT_USER_SKILLS not set")
def test_real_packaged_skills_load_and_negative_controls_are_keyword_traps():
    dest = Path(os.environ["ZT_VT_USER_SKILLS"])
    report = verify_packaged(dest)
    assert report["ok"], report
    loader = SkillsLoader(skills_dir=dest / ".no-bundled-skills", user_skills_dir=dest)
    descriptions = {s.name: f"{s.name} {s.description}" for s in loader.skills}
    for sidecar_path in dest.glob(f"*/{SIDECAR_NAME}"):
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        for case in sidecar["test_cases"]["negative_control"]:
            # Each negative control would fool a keyword router ...
            assert keyword_overlap(case["prompt"], descriptions[sidecar["name"]]), case["id"]
            # ... and the contract still refuses it.
            assert contract_applies(sidecar["contract"], case["situation"])[0] is False
