#!/usr/bin/env python3
"""Package ZT's canonical skills as Vibe-Trading user skills (ZT add-on).

VT loads user skills from ``~/.vibe-trading/skills/user/<name>/SKILL.md``
(``src.agent.skills.USER_SKILLS_DIR``; at runtime USERPROFILE is
``E:\\codex-runtime\\investment-ai\\vibe-trading\\profile``). This tool turns a
canonical skill folder into such a folder as a read-only snapshot:

* The SKILL.md **body is byte-identical** to the source. Only the frontmatter
  may change, and only where VT needs it: a VT-required field (``name``,
  ``description``, ``category``) is added when missing, and a ``name`` or
  ``category`` value that VT's line parser would read with its YAML quotes
  still attached (``name: "x"`` becomes the name ``"x"``, breaking lookup) is
  unquoted. Every change is recorded in the sidecar.
* Every other file of the skill (references/, scripts/, ...) is copied
  verbatim; junk (desktop.ini, __pycache__) is skipped, symlinks are refused.
* ``skill_contract.json`` is written beside SKILL.md: the semantic contract
  (is_a, acts_on, requires, requires_context, used_by_role, produces,
  depends_on, blocked_by) with its paraphrase / historical / negative-control
  test cases, plus provenance: source and canonical path, ``source_sha256``,
  body hash, the VT ``SkillRecord`` content hash a run manifest will record,
  and a per-file hash inventory. Output is deterministic (no timestamps).
* Before writing, the tool re-parses the packaged SKILL.md with VT's own
  frontmatter parser and refuses unless VT would load the intended name,
  description, category and the unchanged body; it also refuses a skill whose
  contract test cases fail, or whose name would shadow a bundled VT skill.
* Nothing is written outside ``--dest`` (every target is resolved and checked),
  and on Windows ``--dest`` may not be on C:, D: or the system drive.

Usage::

    python tools/package_vt_skills.py --dest <user-skills-dir> --contracts VT_SKILL_CONTRACTS.json \\
        --skill <skill-dir> [--skill <skill-dir> ...] [--dry-run] [--replace]
    python tools/package_vt_skills.py --verify --dest <user-skills-dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


def _ensure_vt_importable() -> None:
    try:
        import src.agent.frontmatter  # noqa: F401
    except ImportError:
        agent_dir = Path(__file__).resolve().parents[1] / "agent"
        if agent_dir.is_dir() and str(agent_dir) not in sys.path:
            sys.path.insert(0, str(agent_dir))


_ensure_vt_importable()

from src.agent.frontmatter import parse_frontmatter  # noqa: E402
from src.governance.manifest import SkillRecord  # noqa: E402

SIDECAR_NAME = "skill_contract.json"
SIDECAR_SCHEMA = "zt-vt-skill-contract/1"
CONTRACTS_SCHEMA = "zt-vt-skill-contracts/1"
CONTRACT_FIELDS = ("is_a", "acts_on", "requires", "requires_context", "used_by_role", "produces",
                   "depends_on", "blocked_by")
TEST_CASE_KINDS = ("paraphrase", "historical", "negative_control")
NORMALIZED_KEYS = ("name", "category")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SKIP_FILES = {"desktop.ini", "Thumbs.db", ".DS_Store"}
_SKIP_DIRS = {"__pycache__", ".git"}
_BLOCK_SCALARS = {"|", ">", "|-", ">-", "|+", ">+"}
_STOPWORDS = {"that", "this", "with", "from", "into", "your", "just", "what", "when", "then", "them", "they",
              "have", "will", "would", "should", "about", "every", "there", "their", "only", "does", "give",
              "gets", "make", "like", "than", "some", "such", "used", "uses"}
DEFAULT_BUNDLED_SKILLS = Path(__file__).resolve().parents[1] / "agent" / "src" / "skills"

__all__ = ["PackagingError", "load_contracts", "validate_contract", "contract_applies", "plan_skill",
           "package_skills", "verify_packaged", "keyword_overlap", "main"]


class PackagingError(RuntimeError):
    """A skill cannot be packaged without breaking one of the tool's guarantees."""


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Destination and storage rule
# ---------------------------------------------------------------------------


def drive_problem(path: Any, *, os_name: str | None = None, system_drive: str | None = None) -> str | None:
    """Why ``path`` breaks the storage rule on Windows (C:, D: or the system drive), else ``None``."""
    if (os_name or os.name) != "nt":
        return None
    drive = ntpath.splitdrive(str(path))[0].upper()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if drive.startswith(prefix):
            drive = drive[len(prefix):]
    system = (system_drive or os.environ.get("SystemDrive") or "C:").upper()
    if drive in ("C:", "D:", system):
        return (f"destination may not be on C:, D: or the system drive {system} "
                f"(ZT 2026-09-28: E: runtime); got {path}")
    return None


def _inside(root: Path, target: Path) -> Path:
    """Resolve ``target`` and refuse anything outside ``root`` (already resolved)."""
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise PackagingError(f"refusing to write outside the destination: {target} resolves to {resolved}")
    return resolved


def _safe_relpath(rel: str) -> str:
    parts = PurePosixPath(rel).parts
    if (not parts or PurePosixPath(rel).is_absolute() or any(p in ("", ".", "..") for p in parts)
            or ntpath.splitdrive(rel)[0] or "\\" in rel):
        raise PackagingError(f"unsafe relative path in skill folder: {rel!r}")
    return rel


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def _str_list(value: Any, where: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PackagingError(f"{where} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise PackagingError(f"{where} must not be empty")
    return value


def validate_contract(name: str, entry: Mapping[str, Any]) -> None:
    """Check one contracts-file entry (fields, types and test-case shape)."""
    where = f"contract {name}"
    contract = entry.get("contract")
    if not isinstance(contract, Mapping):
        raise PackagingError(f"{where}: missing 'contract'")
    missing = [f for f in CONTRACT_FIELDS if f not in contract]
    if missing:
        raise PackagingError(f"{where}: missing semantic fields {missing}")
    if not isinstance(contract["is_a"], str) or not contract["is_a"].strip():
        raise PackagingError(f"{where}.is_a must be a non-empty string")
    _str_list(contract["acts_on"], f"{where}.acts_on", allow_empty=False)
    for key in ("requires_context", "used_by_role", "produces", "depends_on", "blocked_by"):
        _str_list(contract[key], f"{where}.{key}")
    if not isinstance(contract["requires"], list):
        raise PackagingError(f"{where}.requires must be a list of clauses (lists of concepts)")
    for clause in contract["requires"]:
        _str_list(clause, f"{where}.requires clause", allow_empty=False)
    category = entry.get("vt_category")
    if not isinstance(category, str) or not _NAME_RE.match(category):
        raise PackagingError(f"{where}.vt_category must be a VT category slug")
    if not isinstance(entry.get("canonical_path"), str) or not entry["canonical_path"].strip():
        raise PackagingError(f"{where}.canonical_path is required")
    cases = entry.get("test_cases")
    if not isinstance(cases, Mapping):
        raise PackagingError(f"{where}: test_cases are required")
    for kind in TEST_CASE_KINDS:
        items = cases.get(kind)
        minimum = 2 if kind == "paraphrase" else 1
        if not isinstance(items, list) or len(items) < minimum:
            raise PackagingError(f"{where}: needs at least {minimum} {kind} case(s)")
        for case in items:
            if not isinstance(case, Mapping) or not {"id", "prompt", "situation", "expect"} <= case.keys():
                raise PackagingError(f"{where}: each {kind} case needs id, prompt, situation, expect")
            if case["expect"] is not (kind != "negative_control"):
                raise PackagingError(f"{where}: {kind} case {case['id']} must expect "
                                     f"{kind != 'negative_control'}")


def load_contracts(path: Path | str) -> dict[str, dict]:
    """Load and validate a ``zt-vt-skill-contracts/1`` registry."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != CONTRACTS_SCHEMA or not isinstance(data.get("skills"), Mapping):
        raise PackagingError(f"{path}: expected schema {CONTRACTS_SCHEMA!r} with a 'skills' object")
    for name, entry in data["skills"].items():
        if not _NAME_RE.match(name):
            raise PackagingError(f"contract name {name!r} is not a VT skill slug")
        validate_contract(name, entry)
    return dict(data["skills"])


def contract_applies(contract: Mapping[str, Any], situation: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Deterministic trigger check of a contract against an extracted situation.

    ``situation`` = ``{"entities": [...], "concepts": [...], "context": [...],
    "role": str | None}``. Prompt text is deliberately not an input: discovery is
    keyed on graph concepts, so a paraphrase cannot change the answer and a
    keyword match alone cannot trigger a skill.

    Returns:
        ``(applies, reasons_it_does_not)``.
    """
    entities = set(situation.get("entities") or [])
    present = set(situation.get("concepts") or []) | entities
    context = set(situation.get("context") or [])
    reasons = []
    if not set(contract["acts_on"]) & entities:
        reasons.append(f"acts_on: none of {sorted(contract['acts_on'])} present")
    for clause in contract["requires"]:
        if not set(clause) & present:
            reasons.append(f"requires one of {sorted(clause)}")
    for flag in contract["requires_context"]:
        if flag not in context:
            reasons.append(f"requires_context {flag}")
    role = situation.get("role")
    if role is not None and contract["used_by_role"] and role not in contract["used_by_role"]:
        reasons.append(f"role {role} not in used_by_role")
    blocked = sorted(set(contract["blocked_by"]) & (present | context))
    if blocked:
        reasons.append(f"blocked_by {blocked}")
    return not reasons, reasons


def run_contract_cases(name: str, entry: Mapping[str, Any]) -> list[dict]:
    """Evaluate every test case of a contract; return the failures."""
    failures = []
    for kind in TEST_CASE_KINDS:
        for case in entry["test_cases"][kind]:
            applies, reasons = contract_applies(entry["contract"], case["situation"])
            if applies is not case["expect"]:
                failures.append({"skill": name, "kind": kind, "id": case["id"], "applies": applies,
                                 "reasons": reasons})
    return failures


def keyword_overlap(prompt: str, text: str) -> set[str]:
    """Words of four or more letters shared by ``prompt`` and ``text`` (stopwords removed)."""
    def words(value: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{4,}", value.lower()) if w not in _STOPWORDS}
    return words(prompt) & words(text)


# ---------------------------------------------------------------------------
# SKILL.md frontmatter
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[str, list[str], str]:
    """``(newline, frontmatter_lines, body_verbatim)`` using VT's fence rules."""
    first_break = text.find("\n")
    if first_break < 0 or text[:first_break].rstrip("\r").rstrip(" \t") != "---":
        raise PackagingError("SKILL.md does not open with a '---' frontmatter fence")
    newline = "\r\n" if text[:first_break].endswith("\r") else "\n"
    position = first_break + 1
    lines = []
    while True:
        end = text.find("\n", position)
        line = text[position:end if end >= 0 else len(text)].rstrip("\r")
        if line.rstrip(" \t") == "---":
            return newline, lines, text[end + 1:] if end >= 0 else ""
        if end < 0:
            raise PackagingError("SKILL.md frontmatter is not closed by a '---' fence")
        lines.append(line)
        position = end + 1


def _yaml_scalar(raw: str) -> Any:
    """What a YAML reader means by a one-line value (quotes removed), for the normalized keys."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise PackagingError(f"unsupported double-quoted frontmatter value {raw!r}") from exc
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _vt_value(key: str, raw: str) -> Any:
    """The value VT's own parser assigns to ``key: raw``."""
    meta, _ = parse_frontmatter(f"---\n{key}: {raw}\n---\n")
    return meta.get(key)


def build_skill_md(source_text: str, *, dir_name: str, entry: Mapping[str, Any]) -> tuple[str, list[dict]]:
    """Return the packaged SKILL.md text and the list of frontmatter changes."""
    newline, lines, body = _split_frontmatter(source_text)
    keys: dict[str, int] = {}
    out_lines = list(lines)
    changes: list[dict] = []
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            raise PackagingError(f"frontmatter line {index + 2} is an indented continuation; VT reads one line per key")
        if ":" not in line:
            raise PackagingError(f"frontmatter line {index + 2} has no key; VT cannot read it")
        key, raw = (part.strip() for part in line.split(":", 1))
        if raw in _BLOCK_SCALARS:
            raise PackagingError(f"frontmatter key {key!r} uses a block scalar; VT reads one line per key")
        keys[key] = index
        if key in NORMALIZED_KEYS:
            intended = _yaml_scalar(raw)
            if _vt_value(key, raw) != intended:
                if not isinstance(intended, str) or not _NAME_RE.match(intended):
                    raise PackagingError(f"frontmatter {key}={raw!r} cannot be represented for VT's parser")
                out_lines[index] = f"{key}: {intended}"
                changes.append({"key": key, "change": "unquoted", "before": raw, "after": intended,
                                "reason": "VT's line parser keeps YAML quotes, which would break lookup/grouping"})
    additions = []
    if "name" not in keys:
        additions.append(("name", dir_name))
    if "description" not in keys:
        description = entry.get("vt_description")
        if not isinstance(description, str) or not description.strip() or "\n" in description:
            raise PackagingError("SKILL.md has no description and the contract supplies no one-line vt_description")
        additions.append(("description", description.strip()))
    if "category" not in keys:
        additions.append(("category", entry["vt_category"]))
    for key, value in additions:
        out_lines.append(f"{key}: {value}")
        changes.append({"key": key, "change": "added", "before": None, "after": value,
                        "reason": "VT-required frontmatter field missing from the source"})
    packaged = newline.join(["---", *out_lines, "---"]) + newline + body
    return packaged, changes


# ---------------------------------------------------------------------------
# Planning and writing
# ---------------------------------------------------------------------------


def _source_files(skill_dir: Path) -> list[tuple[str, bytes]]:
    """Every file of the skill except SKILL.md, verbatim, in sorted order."""
    files = []
    for current, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        base = Path(current)
        for dirname in list(dirnames):
            if (base / dirname).is_symlink():
                raise PackagingError(f"refusing symlinked directory {base / dirname}")
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            path = base / filename
            if filename in _SKIP_FILES or filename.endswith(".pyc"):
                continue
            if path.is_symlink():
                raise PackagingError(f"refusing symlinked file {path}")
            rel = path.relative_to(skill_dir).as_posix()
            if rel == "SKILL.md":
                continue
            if rel == SIDECAR_NAME:
                raise PackagingError(f"source already carries {SIDECAR_NAME}; refusing to overwrite it")
            files.append((_safe_relpath(rel), path.read_bytes()))
    return files


def _packager_sha256() -> str:
    return _sha256(Path(__file__).resolve().read_bytes())


def plan_skill(skill_dir: Path | str, contracts: Mapping[str, dict], *,
               bundled_skills_dir: Path | None = DEFAULT_BUNDLED_SKILLS,
               allow_bundled_override: bool = False) -> dict:
    """Build everything that would be written for one skill (no writes)."""
    skill_dir = Path(skill_dir).resolve()
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise PackagingError(f"{skill_dir} has no SKILL.md")
    source_bytes = skill_md.read_bytes()
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagingError(f"{skill_md} is not UTF-8") from exc
    source_meta, source_body = parse_frontmatter(source_text)
    name = _yaml_scalar(str(source_meta.get("name", skill_dir.name)))
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PackagingError(f"skill name {name!r} is not a VT skill slug")
    if name not in contracts:
        raise PackagingError(f"no semantic contract for {name!r} in the contracts file")
    entry = contracts[name]
    failures = run_contract_cases(name, entry)
    if failures:
        raise PackagingError(f"contract test cases fail for {name}: {failures}")
    if bundled_skills_dir is not None and (Path(bundled_skills_dir) / name).is_dir() and not allow_bundled_override:
        raise PackagingError(f"{name!r} would shadow a bundled VT skill; pass --allow-bundled-override to do that")

    packaged_text, changes = build_skill_md(source_text, dir_name=skill_dir.name, entry=entry)
    meta, body = parse_frontmatter(packaged_text)
    _, _, source_raw_body = _split_frontmatter(source_text)
    _, _, packaged_raw_body = _split_frontmatter(packaged_text)
    if packaged_raw_body != source_raw_body or body != source_body:
        raise PackagingError(f"{name}: packaged body differs from the source body")
    expected_category = source_meta.get("category") if "category" in source_meta else entry["vt_category"]
    expected_category = _yaml_scalar(str(expected_category))
    if meta.get("name") != name or not meta.get("description") or meta.get("category") != expected_category:
        raise PackagingError(f"{name}: VT's parser would not read the intended name/description/category")

    files = [("SKILL.md", packaged_text.encode("utf-8"))] + _source_files(skill_dir)
    inventory = [{"path": rel, "sha256": _sha256(data), "bytes": len(data)} for rel, data in files]
    record = SkillRecord.from_content(name, body)
    sidecar = {
        "schema": SIDECAR_SCHEMA,
        "name": name,
        "vt_category": meta["category"],
        "contract_status": entry.get("contract_status", "DRAFT_PENDING_ZT_REVIEW"),
        "contract": {key: entry["contract"][key] for key in CONTRACT_FIELDS},
        "test_cases": entry["test_cases"],
        "vt_tools": entry.get("vt_tools"),
        "vt_notes": entry.get("vt_notes"),
        "provenance": {
            "snapshot_source": str(skill_md),
            "canonical_path": entry["canonical_path"],
            "source_sha256": _sha256(source_bytes),
            "body_sha256": _sha256(source_raw_body.encode("utf-8")),
            "vt_skill_record": {"name": record.name, "content_hash": record.content_hash},
            "frontmatter_changes": changes,
            "files": inventory,
            "packager": "tools/package_vt_skills.py",
            "packager_sha256": _packager_sha256(),
        },
    }
    sidecar_bytes = (json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return {"name": name, "source": str(skill_dir), "files": files, "sidecar": sidecar,
            "sidecar_bytes": sidecar_bytes, "changes": changes}


def _existing_tree(skill_dir: Path) -> dict[str, bytes]:
    tree = {}
    for current, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        dirnames.sort()
        for filename in filenames:
            path = Path(current) / filename
            tree[path.relative_to(skill_dir).as_posix()] = path.read_bytes()
    return tree


def _make_writable(tree_root: Path) -> None:
    for current, dirnames, filenames in os.walk(tree_root, followlinks=False):
        os.chmod(current, stat.S_IRWXU)
        for filename in filenames:
            os.chmod(Path(current) / filename, stat.S_IRUSR | stat.S_IWUSR)


def package_skills(skill_dirs: Iterable[Path | str], dest: Path | str, contracts: Mapping[str, dict], *,
                   dry_run: bool = False, replace: bool = False,
                   bundled_skills_dir: Path | None = DEFAULT_BUNDLED_SKILLS,
                   allow_bundled_override: bool = False) -> dict:
    """Package skills into ``dest`` (VT's user-skills folder). Returns a report."""
    dest = Path(dest)
    problem = drive_problem(dest.absolute())
    if problem:
        raise PackagingError(problem)
    plans = [plan_skill(d, contracts, bundled_skills_dir=bundled_skills_dir,
                        allow_bundled_override=allow_bundled_override) for d in skill_dirs]
    names = [p["name"] for p in plans]
    if len(names) != len(set(names)):
        raise PackagingError(f"two sources resolve to the same skill name: {names}")
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    report = {"dest": str(root), "dry_run": dry_run, "skills": []}
    for plan in plans:
        target = root / plan["name"]
        _inside(root, target)
        wanted = {rel: data for rel, data in plan["files"]}
        wanted[SIDECAR_NAME] = plan["sidecar_bytes"]
        action = "create"
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise PackagingError(f"{target} exists and is not a plain directory")
            if _existing_tree(target) == wanted:
                action = "unchanged"
            elif not replace:
                raise PackagingError(f"{target} exists and differs; rerun with --replace to refresh the snapshot")
            else:
                action = "replace"
        entry = {"name": plan["name"], "source": plan["source"], "target": str(target), "action": action,
                 "files": sorted(wanted), "frontmatter_changes": plan["changes"],
                 "source_sha256": plan["sidecar"]["provenance"]["source_sha256"],
                 "sidecar_sha256": _sha256(plan["sidecar_bytes"])}
        report["skills"].append(entry)
        if dry_run or action == "unchanged":
            continue
        if action == "replace":
            _make_writable(target)
            shutil.rmtree(target)
        target.mkdir()
        # SKILL.md goes last: VT ignores a folder without it, so an interrupted
        # run never exposes a partial skill.
        order = sorted(rel for rel in wanted if rel != "SKILL.md") + ["SKILL.md"]
        for rel in order:
            path = target / _safe_relpath(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            _inside(root, path.parent)
            if path.exists():
                raise PackagingError(f"refusing to overwrite {path}")
            path.write_bytes(wanted[rel])
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return report


def verify_packaged(dest: Path | str, *, bundled_skills_dir: Path | None = None,
                    canonical_sources: Mapping[str, Path | str] | None = None) -> dict:
    """Load ``dest`` with VT's own SkillsLoader and check every sidecar's hashes.

    Args:
        dest: The user-skills folder.
        bundled_skills_dir: Bundled skills to load alongside (``None``: an empty
            folder, so only packaged skills are seen).
        canonical_sources: Optional ``{name: SKILL.md path}`` to compare the
            snapshot against the current canonical source.
    """
    from src.agent.skills import SkillsLoader

    dest = Path(dest).resolve()
    empty = dest / ".no-bundled-skills"
    loader = SkillsLoader(skills_dir=Path(bundled_skills_dir) if bundled_skills_dir else empty,
                          user_skills_dir=dest)
    loaded = {skill.name: skill for skill in loader.skills}
    results, problems = [], []
    for sidecar_path in sorted(dest.glob(f"*/{SIDECAR_NAME}")):
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        name = sidecar["name"]
        skill_dir = sidecar_path.parent
        prov = sidecar["provenance"]
        checks = {"loaded_by_vt": name in loaded}
        if name in loaded:
            skill = loaded[name]
            checks["category"] = skill.category == sidecar["vt_category"]
            checks["vt_skill_record"] = SkillRecord.from_content(name, skill.body).content_hash == \
                prov["vt_skill_record"]["content_hash"]
            _, _, raw_body = _split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
            checks["body_sha256"] = _sha256(raw_body.encode("utf-8")) == prov["body_sha256"]
            checks["get_content"] = loader.get_content(name).startswith(f'<skill name="{name}">')
        checks["files"] = all(
            (skill_dir / item["path"]).is_file()
            and _sha256((skill_dir / item["path"]).read_bytes()) == item["sha256"] for item in prov["files"])
        extra = set(_existing_tree(skill_dir)) - {i["path"] for i in prov["files"]} - {SIDECAR_NAME}
        checks["no_extra_files"] = not extra
        if canonical_sources and name in canonical_sources:
            checks["matches_canonical_source"] = _sha256(Path(canonical_sources[name]).read_bytes()) == \
                prov["source_sha256"]
        ok = all(checks.values())
        results.append({"name": name, "ok": ok, "checks": checks})
        if not ok:
            problems.append(name)
    return {"dest": str(dest), "ok": not problems and bool(results), "skills": results,
            "loaded_names": sorted(loaded), "problems": problems}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package ZT's skills as read-only VT user skills.")
    parser.add_argument("--dest", required=True, help="VT user-skills folder (…/.vibe-trading/skills/user)")
    parser.add_argument("--contracts", help="zt-vt-skill-contracts/1 JSON (G:\\My Drive\\work\\_infra\\VT_SKILL_CONTRACTS.json)")
    parser.add_argument("--skill", action="append", default=[], help="canonical skill folder (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="plan and print; write nothing")
    parser.add_argument("--replace", action="store_true", help="refresh an existing, differing snapshot")
    parser.add_argument("--allow-bundled-override", action="store_true")
    parser.add_argument("--verify", action="store_true", help="load --dest with VT's loader and check sidecars")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            report = verify_packaged(args.dest)
        else:
            if not args.contracts or not args.skill:
                parser.error("--contracts and at least one --skill are required unless --verify")
            report = package_skills(args.skill, args.dest, load_contracts(args.contracts), dry_run=args.dry_run,
                                    replace=args.replace, allow_bundled_override=args.allow_bundled_override)
    except PackagingError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
