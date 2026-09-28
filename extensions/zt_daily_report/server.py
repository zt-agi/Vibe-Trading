"""Opt-in Vibe-Trading tools that validate, publish and record ZT's daily research report.

Tools (stdio FastMCP server "zt-daily-report"):

* validate_daily_report(input_json) -- read-only; validator receipt plus the
  server-side checks publish will apply.
* publish_daily_report(input_json) -- validate, render, QA the rendered text,
  build the manifest in an E: scratch folder, then publish once to
  <project>/reports/daily/<YYYY-MM-DD>/<run_id>/ and move the
  DAILY_BRIEF_LATEST.html pointer forward. Refuses to overwrite.
* pit_audit_status() -- honest PASS/STALE/MISSING/FAIL/UNVERIFIED state of the
  PIT audit receipt; the observed receipt is archived so publish can verify a
  report's PASS claim.
* price_figures(tickers, freeze_time) -- script-generated report figures
  (reference close, session and five-session change, twenty-session noise band,
  volume ratio) from closed PIT bars, each with a complete reference.
* stage_forecast_rows(tickers, freeze_time, run_id) -- records mechanical and
  market-implied distributions for the forecast ledger (kind "distribution");
  returns counts only, never quantiles (display gate CLOSED until P2).

The report package is imported from INVESTMENT_AI_PROJECT_ROOT
(implementation/daily_research_report). Runtime state stays under
VIBE_TRADING_HOME, which must be on E: on Windows; nothing is written to C: or
D:. No broker, order or trading tool is exposed.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import ntpath
import os
import re
import shutil
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastmcp import FastMCP

sys.dont_write_bytecode = True  # never drop __pycache__ into the shared G: project

mcp = FastMCP("zt-daily-report")

SERVER_VERSION = "1.0.0"
REQUIRED_DRIVE = "E:"
DRIVE_RULE = "ZT 2026-09-28: everything on E:"
PACKAGE_REL = Path("implementation") / "daily_research_report"
WATCHLIST_REL = Path("vt_addons") / "config" / "daily_brief_watchlist.yaml"
PRESET_REL = Path("vt_addons") / "swarm_presets" / "premarket_brief_team.yaml"
PLAYBOOK_REL = Path("vt_addons") / "playbooks" / "premarket-brief.md"
REPORTS_REL = Path("reports") / "daily"
POINTER_NAME = "DAILY_BRIEF_LATEST.html"
PACKAGE_MODULES = ("text_rules", "trading_calendar", "validate_report", "render_report", "run_manifest",
                   "forecast_baseline", "report_figures")
MAX_INPUT_BYTES = 2_000_000
MAX_LISTED = 80
MAX_STAGE_TICKERS = 12
STAGING_MAX_LAG = timedelta(hours=6)
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{2,80}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_POINTER_CUTOFF = re.compile(r'<meta name="zt-daily-brief-cutoff" content="([^"]+)">')


# ---------------------------------------------------------------------------
# Paths and drive policy
# ---------------------------------------------------------------------------

def windows_drive(path) -> str:
    """Return the upper-case drive of a Windows path, ignoring a \\\\?\\ prefix."""
    drive = ntpath.splitdrive(str(path))[0].upper()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if drive.startswith(prefix):
            drive = drive[len(prefix):]
    return drive


def _system_drive() -> str:
    return (os.environ.get("SystemDrive") or "C:").upper()


def require_e_drive(path: Path, what: str) -> Path:
    """Fail closed unless ``path`` is on E: (Windows only)."""
    if os.name != "nt":
        return path
    drive = windows_drive(path)
    if drive != REQUIRED_DRIVE or drive in ("C:", "D:", _system_drive()):
        raise ValueError(f"{what} must be on {REQUIRED_DRIVE} ({DRIVE_RULE}); C:, D: and the system drive "
                         f"{_system_drive()} are refused; got {path}")
    return path


def refuse_system_drives(path: Path, what: str) -> Path:
    """Fail closed when ``path`` is on C:, D: or the system drive (Windows only)."""
    if os.name == "nt" and windows_drive(path) in ("C:", "D:", _system_drive()):
        raise ValueError(f"{what} may not be on C:, D: or the system drive ({DRIVE_RULE}); got {path}")
    return path


def project() -> Path:
    raw = os.environ.get("INVESTMENT_AI_PROJECT_ROOT")
    if not raw:
        raise RuntimeError("INVESTMENT_AI_PROJECT_ROOT is required")
    root = Path(raw).resolve(strict=True)
    if root.name != "Investment-AI-Drive-Research" or root.parent.name != "work":
        raise ValueError("Use the canonical work project folder")
    refuse_system_drives(root, "Project root")
    return root


def runtime() -> Path:
    raw = os.environ.get("VIBE_TRADING_HOME")
    if not raw:
        raise RuntimeError("VIBE_TRADING_HOME is required")
    path = require_e_drive(Path(raw).resolve(), "Runtime (VIBE_TRADING_HOME)")
    root = project()
    if root == path or root in path.parents:
        raise ValueError("Runtime may not be stored in the shared project")
    return path


def _rel(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


# ---------------------------------------------------------------------------
# Package, controls and sibling extension
# ---------------------------------------------------------------------------

_PACKAGES: dict[str, SimpleNamespace] = {}


def package() -> SimpleNamespace:
    """Import the report package from the project under a unique module name."""
    override = os.environ.get("ZT_DAILY_REPORT_PACKAGE", "").strip()
    pkg_dir = Path(override).resolve() if override else (project() / PACKAGE_REL).resolve()
    if not (pkg_dir / "validate_report.py").is_file():
        raise FileNotFoundError(f"report package not found under {PACKAGE_REL.as_posix()}")
    key = str(pkg_dir)
    if key not in _PACKAGES:
        name = "zt_drr_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        spec = importlib.util.spec_from_file_location(name, pkg_dir / "__init__.py",
                                                      submodule_search_locations=[str(pkg_dir)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        mods = {m: importlib.import_module(f"{name}.{m}") for m in PACKAGE_MODULES}
        _PACKAGES[key] = SimpleNamespace(dir=pkg_dir, **mods)
    return _PACKAGES[key]


def contracts_and_controls(pkg: SimpleNamespace) -> tuple[dict, dict]:
    return pkg.validate_report.load_contracts(), pkg.validate_report.load_controls()


def _yaml(path: Path) -> dict | None:
    if not path.is_file():
        return None
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def watchlist_config() -> dict | None:
    return _yaml(project() / WATCHLIST_REL)


def watchlist_symbols() -> set[str]:
    config = watchlist_config() or {}
    return {str(t.get("symbol")).upper() for t in config.get("tickers") or [] if isinstance(t, dict)}


_PIT_SIM = None


def pit_sim():
    """Load the sibling pit_actor_sim server module (the project's PIT access layer)."""
    global _PIT_SIM
    if _PIT_SIM is None:
        raw = os.environ.get("PIT_ACTOR_SIM_SERVER", "").strip()
        path = Path(raw) if raw else Path(__file__).resolve().parent.parent / "pit_actor_sim" / "server.py"
        spec = importlib.util.spec_from_file_location("zt_pit_actor_sim_server", path)
        module = importlib.util.module_from_spec(spec)
        folder = str(path.parent)
        added = folder not in sys.path
        if added:
            sys.path.insert(0, folder)
        try:
            spec.loader.exec_module(module)
        finally:
            if added:
                sys.path.remove(folder)
        _PIT_SIM = module
    return _PIT_SIM


# ---------------------------------------------------------------------------
# PIT audit observation
# ---------------------------------------------------------------------------

def _audit_archive() -> Path:
    return runtime() / "daily_report" / "audit_receipts"


def lake_signature_matches(signature) -> bool | None:
    """True/False when the lake signature can be compared, None when it cannot."""
    try:
        return pit_sim().lake_signature() == signature
    except Exception:
        return None


def observe_pit_audit(max_age_minutes: float) -> dict:
    """Read, judge and archive the pit_actor_sim audit receipt."""
    now = datetime.now(timezone.utc)
    observed = {"observed_at": now.replace(microsecond=0).isoformat(), "max_age_minutes": max_age_minutes}
    path = runtime() / "pit_audit_receipt.json"
    if not path.is_file():
        return {"status": "MISSING", "audited_at_utc": None, "age_minutes": None, "receipt_sha256": None,
                "signature_checked": False, "detail": "no receipt; run pit_actor_sim server.py --refresh-audit",
                **observed}
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        receipt = json.loads(raw.decode("utf-8"))
        audited = datetime.fromisoformat(str(receipt["audited_at_utc"]).replace("Z", "+00:00"))
        if audited.tzinfo is None:
            audited = audited.replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, UnicodeDecodeError):
        return {"status": "FAIL", "audited_at_utc": None, "age_minutes": None, "receipt_sha256": digest,
                "signature_checked": False, "detail": "receipt is unreadable", **observed}
    age = (now - audited).total_seconds() / 60
    result = {"audited_at_utc": audited.isoformat(), "age_minutes": round(age, 1), "receipt_sha256": digest,
              "signature_checked": False, **observed}
    archive = _audit_archive()
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"{digest}.json"
    if not target.exists():
        with open(target, "xb") as handle:
            handle.write(raw)
    if receipt.get("status") != "PASS":
        result.update(status="FAIL", detail="receipt status is not PASS")
    elif age < 0 or age > max_age_minutes:
        result.update(status="STALE", detail=f"receipt age exceeds {max_age_minutes} minutes")
    else:
        matches = lake_signature_matches(receipt.get("signature"))
        if matches is None:
            result.update(status="UNVERIFIED", detail="lake signature could not be checked")
        elif not matches:
            result.update(status="STALE", signature_checked=True, detail="lake changed since the audit")
        else:
            result.update(status="PASS", signature_checked=True, detail="fresh and bound to the current lake")
    with open(archive / "observations.jsonl", "a", encoding="utf-8") as log:  # append-only observation log
        log.write(json.dumps({"receipt_sha256": digest, "status": result["status"],
                              "audited_at_utc": result["audited_at_utc"], "observed_at": result["observed_at"]}) + "\n")
    return result


def observations(digest: str) -> list[dict]:
    """Every recorded observation of one receipt (append-only log)."""
    log = _audit_archive() / "observations.jsonl"
    if not log.is_file():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("receipt_sha256") == digest:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Forecast staging
# ---------------------------------------------------------------------------

def _staging_root() -> Path:
    return runtime() / "forecast_staging"


def staged_counts(run_id: str | None = None) -> tuple[int, int]:
    """Return (rows staged for ``run_id``, rows staged in total)."""
    total = this_run = 0
    root = _staging_root()
    if not root.is_dir():
        return 0, 0
    for path in root.glob("*/*.jsonl"):
        with open(path, encoding="utf-8") as handle:
            count = sum(1 for line in handle if line.strip())
        total += count
        if run_id and path.stem == run_id:
            this_run += count
    return this_run, total


def _parse_aware(value: str, label: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"{label} requires a timezone offset")
    return moment


def _price_rows(sim, sec_id: int, freeze: datetime) -> list[dict]:
    start = (freeze.date() - timedelta(days=1096 + 7)).isoformat()
    result = sim.pit_price_history(sec_id, freeze.isoformat(), start, freeze.date().isoformat(), 1000)
    return list(result.get("rows") or [])


# ---------------------------------------------------------------------------
# Validation shared by validate and publish
# ---------------------------------------------------------------------------

def _parse_input(input_json: str) -> tuple[dict | None, bytes, str | None]:
    raw = (input_json or "").encode("utf-8")
    if len(raw) > MAX_INPUT_BYTES:
        return None, raw, f"input_json exceeds {MAX_INPUT_BYTES} bytes"
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return None, raw, f"input_json is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, raw, "input_json must be a JSON object"
    return data, raw, None


def server_checks(data: dict) -> tuple[list[str], list[str]]:
    """Checks only the server can make: observed audit receipt, staged counts, watchlist config."""
    errors, warnings = [], []
    run_id = str(data.get("run_id", ""))
    audit = (data.get("run_metadata") or {}).get("pit_audit") or {}
    if audit.get("status") == "PASS":
        digest = str(audit.get("receipt_sha256", ""))
        archived = _audit_archive() / f"{digest}.json" if _SHA256.fullmatch(digest) else None
        if archived is None or not archived.is_file():
            errors.append("run_metadata.pit_audit claims PASS but that receipt was not observed by pit_audit_status")
        elif not any(o.get("status") == "PASS" for o in observations(digest)):
            errors.append("run_metadata.pit_audit claims PASS but pit_audit_status never observed that receipt as PASS")
        else:
            receipt = json.loads(archived.read_text(encoding="utf-8"))
            claimed = str(audit.get("audited_at_utc", "")).replace("Z", "+00:00")
            recorded = str(receipt.get("audited_at_utc", "")).replace("Z", "+00:00")
            try:
                same = datetime.fromisoformat(claimed) == datetime.fromisoformat(recorded)
            except ValueError:
                same = False
            if not same:
                errors.append("run_metadata.pit_audit audited_at_utc does not match the observed receipt")
    track = data.get("forecast_track") or {}
    this_run, total = staged_counts(run_id if _RUN_ID.fullmatch(run_id) else None)
    if isinstance(track.get("recorded_this_run"), int) and track["recorded_this_run"] != this_run:
        errors.append(f"forecast_track.recorded_this_run is {track['recorded_this_run']} but {this_run} rows "
                      f"are staged for this run")
    if isinstance(track.get("recorded_total"), int) and not this_run <= track["recorded_total"] <= total:
        errors.append(f"forecast_track.recorded_total must lie between {this_run} and {total} staged rows")
    symbols = watchlist_symbols()
    if not symbols:
        warnings.append(f"watchlist config {WATCHLIST_REL.as_posix()} not found; watchlist not cross-checked")
    else:
        extra = sorted({str(w.get("symbol", "")).upper() for w in data.get("watchlist") or []
                        if isinstance(w, dict)} - symbols)
        if extra:
            errors.append(f"watchlist symbols {extra} are not on the research watchlist config")
    if _RUN_ID.fullmatch(run_id) and _published_dirs(run_id):
        warnings.append(f"run_id {run_id} is already published; publishing it again will be refused")
    return errors, warnings


def _published_dirs(run_id: str) -> list[Path]:
    base = project() / REPORTS_REL
    return [p for p in base.glob(f"*/{run_id}") if p.is_dir()] if base.is_dir() else []


def _check(input_json: str) -> tuple[SimpleNamespace, dict | None, dict]:
    """Parse, normalize and validate; return (package, normalized input, receipt)."""
    pkg = package()
    contracts, controls = contracts_and_controls(pkg)
    data, raw, problem = _parse_input(input_json)
    if problem:
        return pkg, None, {"status": "FAIL", "errors": [problem], "warnings": [], "findings": [],
                           "input_sha256": hashlib.sha256(raw).hexdigest()}
    normalized, notes = pkg.validate_report.normalize_input(data)
    receipt = pkg.validate_report.receipt(normalized, "input.json", contracts=contracts, controls=controls,
                                          input_sha256=hashlib.sha256(raw).hexdigest(), normalization=notes)
    extra_errors, extra_warnings = server_checks(normalized)
    receipt["errors"] = list(dict.fromkeys(receipt["errors"] + extra_errors))
    receipt["warnings"] = list(dict.fromkeys(receipt["warnings"] + extra_warnings))
    receipt["server_checks"] = {"server": "zt-daily-report", "server_version": SERVER_VERSION,
                                "errors": extra_errors, "warnings": extra_warnings}
    receipt["status"] = "PASS" if not receipt["errors"] else "FAIL"
    return pkg, normalized, receipt


def _summary(receipt: dict) -> dict:
    errors = receipt.get("errors", [])
    return {"status": receipt.get("status"), "error_count": len(errors), "errors": errors[:MAX_LISTED],
            "warnings": receipt.get("warnings", [])[:MAX_LISTED],
            "failing_findings": [f for f in receipt.get("findings", []) if f.get("severity") == "error"][:MAX_LISTED],
            "evidence_triage": receipt.get("evidence_triage", []), "normalization": receipt.get("normalization", []),
            "input_sha256": receipt.get("input_sha256"), "validator_version": receipt.get("validator_version")}


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write(data)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _session_folder(data: dict) -> str:
    return date.fromisoformat(str(data["session_date"])).isoformat()


def _pointer_html(rel_report: str, data: dict, manifest_id: str) -> str:
    link = rel_report.replace('"', "")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"zt-daily-brief-cutoff\" content=\"{data['cutoff']}\">"
            f"<meta name=\"zt-daily-brief-run\" content=\"{data['run_id']}\">"
            f"<meta http-equiv=\"refresh\" content=\"0; url={link}\">"
            "<title>Daily Research Brief (latest)</title></head><body>"
            f"<p>Latest daily research brief: <a href=\"{link}\">{data['session_date']} · {data['run_id']}</a> "
            f"(manifest <code>{manifest_id[:16]}</code>).</p></body></html>\n")


def update_pointer(root: Path, rel_report: str, data: dict, manifest_id: str) -> tuple[bool, str]:
    """Move DAILY_BRIEF_LATEST.html forward; never back to an older cutoff."""
    pointer = root / POINTER_NAME
    new_cutoff = datetime.fromisoformat(str(data["cutoff"]).replace("Z", "+00:00"))
    if pointer.is_file():
        match = _POINTER_CUTOFF.search(pointer.read_text(encoding="utf-8", errors="replace"))
        if match:
            try:
                current = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            except ValueError:
                current = None
            if current is not None and current > new_cutoff:
                return False, "pointer already names a later cutoff"
    temporary = root / f".{POINTER_NAME}.{uuid.uuid4().hex[:8]}.tmp"
    temporary.write_text(_pointer_html(rel_report, data, manifest_id), encoding="utf-8")
    os.replace(temporary, pointer)
    return True, "updated"


def _hash_entries(pkg, root: Path, rt: Path, data: dict) -> tuple[list, list]:
    """Hash presets, playbook, watchlist and skills; paths stay relative to project or VT home."""
    roots = pkg.run_manifest.PathRoots([("project", root), ("vt_home", rt)])

    def one(name: str, path: Path | None) -> dict:
        try:
            return pkg.run_manifest.file_hashes([(name, path)], roots)[0]
        except ValueError:  # outside the declared roots: recorded by name only, never as an absolute path
            return {"name": name, "root": None, "path": None, "sha256": None, "bytes": None, "status": "OUTSIDE_ROOTS"}

    installed = Path.home() / ".vibe-trading" / "swarm" / "presets" / "premarket_brief_team.yaml"
    presets = [one("premarket_brief_team", root / PRESET_REL), one("premarket_brief_team (installed)", installed),
               one("premarket-brief", root / PLAYBOOK_REL), one("daily_brief_watchlist", root / WATCHLIST_REL)]
    skills_dir = os.environ.get("ZT_SKILLS_DIR", "").strip()
    names = [str(s) for s in (data.get("run_metadata") or {}).get("skills") or []]
    skills = [one(n, Path(skills_dir) / n / "SKILL.md" if skills_dir else None) for n in names]
    return skills, presets


def _vt_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("vibe-trading-ai")
    except Exception:
        return None


@mcp.tool
def validate_daily_report(input_json: str) -> dict:
    """Validate a daily-research-report-input/2 JSON document (read-only).

    Returns the validator status, errors, warnings, failing wording findings and
    evidence triage, plus the server-side checks publish_daily_report applies
    (observed PIT audit receipt, staged forecast counts, research watchlist).
    """
    _, _, receipt = _check(input_json)
    return _summary(receipt)


@mcp.tool
def publish_daily_report(input_json: str) -> dict:
    """Validate, render and publish one daily report; refuses to overwrite.

    Builds report.html, input.json, validation.json, manifest.json and the
    evidence excerpts in an E: scratch folder, runs a full-text QA of the
    rendered page, then publishes the folder once to
    reports/daily/<session_date>/<run_id>/ in the project and moves the
    DAILY_BRIEF_LATEST.html pointer forward.
    """
    root, rt = project(), runtime()
    pkg, data, receipt = _check(input_json)
    if receipt["status"] != "PASS" or data is None:
        return {**_summary(receipt), "status": "REJECTED", "published": False}
    run_id, folder = data["run_id"], _session_folder(data)
    rel_dir = (REPORTS_REL / folder / run_id).as_posix()
    dest = root / REPORTS_REL / folder / run_id
    if dest.exists() or _published_dirs(run_id):
        raise FileExistsError(f"refusing to overwrite a published run: {run_id}")
    contracts, _ = contracts_and_controls(pkg)
    scratch = rt / "daily_report" / "scratch" / f"{run_id}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True)
    _write_new(scratch / "input.json", _json_bytes(data))
    receipt["input_normalized_sha256"] = hashlib.sha256((scratch / "input.json").read_bytes()).hexdigest()
    _write_new(scratch / "validation.json", _json_bytes(receipt))
    evidence = {}
    for name, text in sorted((data.get("evidence_excerpts") or {}).items()):
        _write_new(scratch / name, str(text).encode("utf-8"))
        evidence[name] = scratch / name
    html = pkg.render_report.render(data, contracts=contracts, validation=receipt)
    qa = pkg.text_rules.rendered_text_findings(html)
    if qa:
        return {**_summary(receipt), "status": "REJECTED", "published": False,
                "errors": [f"rendered text QA: {f.rule} {f.snippet}" for f in qa][:MAX_LISTED],
                "scratch": _rel(scratch, rt)}
    _write_new(scratch / "report.html", html.encode("utf-8"))
    audit_claim = (data.get("run_metadata") or {}).get("pit_audit") or {}
    skills, presets = _hash_entries(pkg, root, rt, data)
    built = [scratch / n for n in ("input.json", "validation.json", "report.html")] + list(evidence.values())
    code = [pkg.dir / name for name in pkg.run_manifest.CODE_FILES if (pkg.dir / name).is_file()]
    manifest = pkg.run_manifest.build_manifest(
        scratch / "input.json", scratch / "report.html", code, project_root=root, run_root=scratch,
        published_paths={p: f"{rel_dir}/{_rel(p, scratch)}" for p in built},
        validation_path=scratch / "validation.json", extra_artifacts=evidence,
        pit_audit={k: audit_claim.get(k) for k in ("status", "receipt_sha256", "audited_at_utc", "observed_at")},
        skills=skills, presets=presets, model_config=(data.get("run_metadata") or {}).get("model_config") or {},
        engine_extra={"server": "zt-daily-report", "server_version": SERVER_VERSION, "vt_version": _vt_version()},
        publication_status="PUBLISHED")
    _write_new(scratch / "manifest.json", _json_bytes(manifest))
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    partial = parent / f".{run_id}.partial-{uuid.uuid4().hex[:8]}"
    shutil.copytree(scratch, partial)
    if dest.exists():
        shutil.rmtree(partial)
        raise FileExistsError(f"refusing to overwrite a published run: {run_id}")
    os.rename(partial, dest)  # one publish step; Windows refuses an existing target
    for name in ("input.json", "validation.json", "report.html", "manifest.json"):
        if (dest / name).read_bytes() != (scratch / name).read_bytes():
            raise RuntimeError(f"published copy of {name} differs from the scratch build")
    shutil.rmtree(scratch)
    pointer_updated, pointer_note = update_pointer(root, f"{rel_dir}/report.html", data, manifest["manifest_id"])
    return {"status": "PUBLISHED", "published": True, "run_dir": rel_dir, "report": f"{rel_dir}/report.html",
            "manifest_id": manifest["manifest_id"],
            "files": {k: v["sha256"] for k, v in manifest["artifacts"].items() if isinstance(v, dict)},
            "pointer": POINTER_NAME, "pointer_updated": pointer_updated, "pointer_note": pointer_note,
            "warnings": receipt["warnings"][:MAX_LISTED]}


@mcp.tool
def pit_audit_status() -> dict:
    """Report the PIT audit receipt state at this moment: PASS, STALE, MISSING, FAIL or UNVERIFIED.

    PASS needs a PASS receipt no older than the run-controls limit (60 minutes)
    that is bound to the current lake signature. The observed receipt is
    archived so a report's PASS claim can be verified at publish time.
    """
    _, controls = contracts_and_controls(package())
    limit = float(((controls.get("pit_audit") or {}).get("max_age_minutes")) or 60)
    return observe_pit_audit(limit)


@mcp.tool
def stage_forecast_rows(tickers: list[str], freeze_time: str, run_id: str, include_implied: bool = True) -> dict:
    """Record mechanical and market-implied distributions for the forecast ledger (counts only).

    Reads closed daily bars through the PIT warehouse with ``asof = freeze_time``
    and, when ``include_implied``, one free CBOE delayed chain per ticker. Rows
    (kind "distribution") are written once to VIBE_TRADING_HOME/forecast_staging
    for the forecast ledger to ingest. The result never contains quantiles:
    distributions are not displayed until the P2 gate passes.
    """
    rt, pkg = runtime(), package()
    _, controls = contracts_and_controls(pkg)
    limits = controls.get("limits") or {}
    if not _RUN_ID.fullmatch(run_id or ""):
        raise ValueError("run_id must be stable lowercase ASCII")
    freeze = _parse_aware(freeze_time, "freeze_time")
    now = datetime.now(timezone.utc)
    if freeze > now or now - freeze > STAGING_MAX_LAG:
        raise ValueError("freeze_time must be in the past and within six hours: staging is prospective only")
    wanted = _watchlist_tickers(tickers)
    session = freeze.astimezone(pkg.trading_calendar.NEW_YORK).date().isoformat()
    target = _staging_root() / session / f"{run_id}.jsonl"
    if target.exists():
        raise FileExistsError(f"rows for run {run_id} are already staged")
    audit = observe_pit_audit(float(((controls.get("pit_audit") or {}).get("max_age_minutes")) or 60))
    sim = pit_sim()
    max_fetch = int(limits.get("max_cboe_requests_per_run", 10))
    interval = float(limits.get("cboe_min_interval_seconds", 1.0))
    raw_dir = _staging_root() / "raw" / "cboe" / session
    rows, report, fetches = [], {}, []
    for ticker in wanted:
        entry = {"rows": 0, "mechanical": False, "implied": False, "skipped": [], "error": None}
        report[ticker] = entry
        try:
            securities = sim.pit_security(ticker, freeze.isoformat()).get("securities") or []
            if len(securities) != 1:
                raise ValueError(f"{len(securities)} securities resolve for {ticker} at freeze_time")
            sec_id = int(securities[0]["sec_id"])
            prices = _price_rows(sim, sec_id, freeze)
            chain = meta = None
            if include_implied and len(fetches) < max_fetch:
                if fetches:
                    time.sleep(interval)
                try:
                    meta = pkg.forecast_baseline.fetch_cboe_chain(ticker, raw_dir)
                    chain = pkg.forecast_baseline.parse_cboe_chain(meta.pop("payload"))
                    meta["path"] = _rel(Path(meta["path"]), rt)
                    fetches.append({"ticker": ticker, **meta})
                except Exception as exc:  # the free endpoint may be down; mechanical rows still record
                    entry["skipped"].append({"method": "MARKET_IMPLIED", "reason": f"chain fetch failed: {exc}"[:300]})
            built = pkg.forecast_baseline.build_distribution_rows(
                ticker=ticker, sec_id=sec_id, price_rows=prices, asof=freeze, issued_at=now, run_id=run_id,
                pit_audit_status=audit["status"], chain=chain, chain_meta=meta)
            rows.extend(built["rows"])
            entry["rows"] = len(built["rows"])
            entry["mechanical"] = any(r["method"] == "MECHANICAL_BASELINE" for r in built["rows"])
            entry["implied"] = any(r["method"] == "MARKET_IMPLIED" for r in built["rows"])
            entry["skipped"] += built["skipped"]
            entry["reference_close_date"] = built["reference_close"]["event_date"]
        except Exception as exc:
            entry["error"] = str(exc)[:300]
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode("utf-8")
    if rows:
        _write_new(target, body)
    receipt = {"run_id": run_id, "freeze_time": freeze.isoformat(), "staged_at": now.isoformat(),
               "rows": len(rows), "rows_sha256": hashlib.sha256(body).hexdigest() if rows else None,
               "pit_audit_status": audit["status"], "tickers": report, "fetches": fetches,
               "server_version": SERVER_VERSION}
    if rows:
        _write_new(target.with_suffix(".receipt.json"), _json_bytes(receipt))
    this_run, total = staged_counts(run_id)
    return {"status": "STAGED" if rows else "NOTHING_STAGED", "run_id": run_id, "recorded_this_run": this_run,
            "recorded_total": total, "scored_total": None, "display_gate": "CLOSED",
            "pit_audit_status": audit["status"], "tickers": report,
            "staging_file": _rel(target, rt) if rows else None, "rows_sha256": receipt["rows_sha256"],
            "note": "Distributions are recorded for the forecast ledger and never returned or displayed."}


def _watchlist_tickers(tickers) -> list[str]:
    wanted = [str(t).strip().upper() for t in (tickers or []) if str(t).strip()]
    if not wanted or len(wanted) > MAX_STAGE_TICKERS:
        raise ValueError(f"give 1..{MAX_STAGE_TICKERS} tickers")
    outside = sorted(set(wanted) - watchlist_symbols())
    if outside:
        raise ValueError(f"tickers {outside} are not on the research watchlist config")
    return wanted


@mcp.tool
def price_figures(tickers: list[str], freeze_time: str) -> dict:
    """Script-generated price figures with complete point-in-time references.

    For each research-watchlist ticker, reads closed daily bars through the PIT
    warehouse with ``asof = freeze_time`` and returns ready-to-use ``figures``
    entries: reference close, reference-session change, five-session change,
    twenty-session mean absolute daily move (the noise band) and volume versus
    its twenty-session average. Copy them into the report unchanged; also add
    the returned ``source`` entry to the report's source registry.
    """
    pkg, sim = package(), pit_sim()
    freeze = _parse_aware(freeze_time, "freeze_time")
    now = datetime.now(timezone.utc)
    if freeze > now:
        raise ValueError("freeze_time cannot be in the future")
    figures, report, digests, classes = [], {}, [], set()
    for ticker in _watchlist_tickers(tickers):
        entry = {"sec_id": None, "reference_close_date": None, "figure_ids": [], "error": None}
        report[ticker] = entry
        try:
            securities = sim.pit_security(ticker, freeze.isoformat()).get("securities") or []
            if len(securities) != 1:
                raise ValueError(f"{len(securities)} securities resolve for {ticker} at freeze_time")
            entry["sec_id"] = sec_id = int(securities[0]["sec_id"])
            start = (freeze.date() - timedelta(days=60)).isoformat()
            rows = sim.pit_price_history(sec_id, freeze.isoformat(), start, freeze.date().isoformat(), 250).get("rows")
            pack = pkg.report_figures.price_figures(ticker, sec_id, rows or [], freeze)
            figures += pack["figures"]
            entry["reference_close_date"] = pack["reference_close_date"]
            entry["figure_ids"] = [f["figure_id"] for f in pack["figures"]]
            digests.append(pack["rows_sha256"])
            classes.update(pack["pit_classes"])
        except Exception as exc:
            entry["error"] = str(exc)[:300]
    failed = sorted(t for t, e in report.items() if e["error"])
    status = "fresh" if not failed else ("missing" if len(failed) == len(report) else "stale")
    source = {"source_id": pkg.report_figures.DEFAULT_SOURCE_ID, "contract_id": "pitdb-prices", "status": status,
              "pit_class": min(classes or {"NON_PIT"}, key=lambda c: pkg.report_figures.PIT_ORDER.get(c, 0)),
              "captured_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "source_group": "pitdb",
              "evidence_hash": hashlib.sha256("|".join(sorted(digests)).encode()).hexdigest()}
    if failed:
        source["status_reason"] = f"no point-in-time prices for {failed}"
    return {"freeze_time": freeze.isoformat(), "figures": figures, "tickers": report, "source": source,
            "note": "Figures are computed by code from closed bars; do not edit values or refs."}


def prune_scratch(days: int | None = None) -> list[str]:
    """Delete E: scratch build folders older than the run-controls retention."""
    if days is None:
        _, controls = contracts_and_controls(package())
        days = int(((controls.get("retention_days") or {}).get("scratch_build")) or 7)
    base = runtime() / "daily_report" / "scratch"
    cutoff = time.time() - days * 86400
    removed = []
    for path in base.glob("*") if base.is_dir() else []:
        if path.is_dir() and path.stat().st_mtime < cutoff:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


if __name__ == "__main__":
    if sys.argv[1:] == ["--prune-scratch"]:
        print(json.dumps(prune_scratch(), indent=2))
    else:
        mcp.run()
