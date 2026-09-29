"""Reader contract tests: provenance envelope, honest status, redaction, whitelist."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import NOW

READERS = ("daily_snapshot", "source_families", "signal_state", "lane_status", "test_ledger",
           "promotion_board", "pit_inventory", "world_model", "project_reports")
SYNTHETIC_PORTFOLIO_VALUES = ("1234567.89", "1,234,567", "234567.89", "123,456.78",
                              "123456.78", "11.11", "33.33", "55.55")


def _call(core, name, **kwargs):
    return getattr(core, name)(now=kwargs.pop("now", NOW), **kwargs)


def _tree_state(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


# --- project root -----------------------------------------------------------


def test_project_root_is_required(core, monkeypatch):
    monkeypatch.delenv("INVESTMENT_AI_PROJECT_ROOT", raising=False)
    with pytest.raises(core.ProjectRootError, match="required"):
        core.project_root()


def test_project_root_must_be_the_canonical_folder(core, tmp_path, monkeypatch):
    other = tmp_path / "work" / "Somewhere-Else"
    other.mkdir(parents=True)
    monkeypatch.setenv("INVESTMENT_AI_PROJECT_ROOT", str(other))
    with pytest.raises(core.ProjectRootError, match="canonical"):
        core.project_root()
    monkeypatch.setenv("INVESTMENT_AI_PROJECT_ROOT", str(tmp_path / "missing"))
    with pytest.raises(core.ProjectRootError, match="does not exist"):
        core.project_root()


def test_paths_cannot_escape_the_root(core, project):
    for bad in ("../AGENTS.md", "/etc/passwd", "a/../../x", "C:secret", "", "x\x00y"):
        with pytest.raises(ValueError):
            core.inside(project, bad)
    assert core.inside(project, "PROJECT_HUB.json") == project / "PROJECT_HUB.json"


# --- envelope contract --------------------------------------------------------


@pytest.mark.parametrize("name", READERS)
def test_every_reader_returns_the_provenance_envelope(core, project, name):
    out = _call(core, name)
    for key in ("tool", "as_of", "as_of_basis", "retrieved_at", "source_path", "sha256",
                "pit_label", "status", "status_rule", "authority", "data"):
        assert key in out, key
    assert out["tool"] == name
    assert out["pit_label"] == "NON_PIT"
    assert out["status"] in {"FRESH", "STALE", "MISSING"}
    assert out["retrieved_at"] == "2026-09-28T13:00:00Z"
    assert out["as_of"] is None or out["as_of"].endswith("Z")
    assert "NOT_AN_INVESTMENT_SIGNAL" in out["authority"]
    if out["status"] == "MISSING":
        assert out["sha256"] is None and out["as_of"] is None
    else:
        assert len(out["sha256"]) == 64
    json.dumps(out)  # JSON-serialisable end to end


def test_readers_never_write(core, project, monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "home"))
    before = _tree_state(project.parent.parent)
    for name in READERS:
        _call(core, name)
    for date in ("latest", "today", "2026-09-27"):
        core.daily_snapshot(date, now=NOW)
    core.render_report("ALPHA_MONITOR_TOP25.html")
    assert _tree_state(project.parent.parent) == before


# --- daily_snapshot -----------------------------------------------------------


def test_daily_snapshot_latest_is_fresh_with_manifest_digest(core, project):
    out = core.daily_snapshot("latest", now=NOW)
    data = out["data"]
    assert out["status"] == "FRESH"
    assert out["source_path"] == "implementation/exports/2026-09-27"
    assert out["as_of"] == "2026-09-27T12:57:26Z"
    assert out["as_of_basis"].startswith("content:event_guardrails.as_of")
    assert data["resolved_date"] == "2026-09-27" and data["complete"] is True
    lines = []
    for info in out["files"]:
        digest = hashlib.sha256((project / info["path"]).read_bytes()).hexdigest()
        assert info["sha256"] == digest
        lines.append(f"{info['path']}\t{digest}\n")
    assert out["sha256"] == hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()
    assert data["source_health"]["by_status"]["FRESH"] == 10
    assert data["alerts"]["by_severity"] == {"ERROR": 2, "WARNING": 8}
    assert data["signal_readiness"]["count"] == 20
    assert data["signal_readiness"]["alpha_ready_count"] == 0
    assert data["event_guardrails"]["by_status"] == {"UNAVAILABLE": 3}
    assert data["broker_connectivity"]["order_capability"] == "DISABLED"
    daily = out["supplementary"]["daily_update"]
    assert daily["data"]["status"] == "PARTIAL" and daily["data"]["matches_snapshot_date"]


def test_daily_snapshot_goes_stale_after_36_hours(core, project):
    later = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    assert core.daily_snapshot("2026-09-27", now=later)["status"] == "STALE"


def test_missing_day_is_reported_with_the_latest_available(core, project):
    out = core.daily_snapshot("today", now=NOW)
    assert out["status"] == "MISSING"
    assert out["source_path"] == "implementation/exports/2026-09-28"
    assert out["data"]["latest_available"] == "2026-09-27"
    assert "latest available is 2026-09-27" in out["notes"][0]


@pytest.mark.parametrize("value", ["../2026-09-27", "2026-13-01", "20260927", "2026-09-27/..",
                                   "2031-01-01"])
def test_daily_snapshot_rejects_bad_dates(core, project, value):
    with pytest.raises(ValueError):
        core.daily_snapshot(value, now=NOW)


def test_incomplete_export_folder_is_labelled(core, project):
    folder = project / "implementation" / "exports" / "2026-09-26"
    folder.mkdir()
    (folder / "alerts.csv").write_bytes(
        (project / "implementation/exports/2026-09-27/alerts.csv").read_bytes())
    out = core.daily_snapshot("2026-09-26", now=NOW)
    assert out["data"]["complete"] is False
    assert "source_health.csv" in out["data"]["missing_files"]
    assert out["as_of_basis"] == "folder date (date)"
    assert out["status"] == "STALE"  # date-only: 2026-09-26 00:00 ET is 57h before NOW
    assert any("incomplete" in note for note in out["notes"])


def test_portfolio_context_is_coverage_and_status_only(core, project):
    out = core.daily_snapshot("latest", now=NOW)
    portfolio = out["data"]["portfolio_context"]
    assert set(portfolio) == {"as_of", "source_id", "source_label", "reconciliation_status",
                              "pnl_status", "pnl_value_coverage_pct", "note", "policy"}
    assert portfolio["reconciliation_status"] == "UNRECONCILED"
    serialised = json.dumps(out)
    for value in SYNTHETIC_PORTFOLIO_VALUES:
        assert value not in serialised, value
    for key in ("gross_absolute_captured_value", "captured_total_gain", "position_count",
                "concentration_pct"):
        assert key not in serialised, key
    alert = next(a for a in out["data"]["alerts"]["rows"] if a["category"] == "PORTFOLIO_IMPORT")
    assert "[value withheld]" in alert["summary"] and "90.00%" in alert["summary"]


def test_redaction_of_free_text(core):
    text = ("Gap $1,234,567.89 (USD 5,000) on account X12345678 and Z12-3456789, id 123456789; "
            "coverage 93.62% as of 2026-09-27T08:57:26-04:00 in rows 4,111")
    redacted = core.redact_text(text)
    for secret in ("1,234,567.89", "5,000", "X12345678", "Z12-3456789", "123456789"):
        assert secret not in redacted, secret
    for kept in ("93.62%", "2026-09-27T08:57:26-04:00", "4,111"):
        assert kept in redacted, kept


# --- source_families ------------------------------------------------------------


def test_source_families_missing_ledger_keeps_other_sources_separate(core, project):
    out = core.source_families(now=NOW)
    assert out["status"] == "MISSING" and out["data"] is None
    assert out["source_path"].endswith("/top25_backfill_coverage.json")
    catalog = out["supplementary"]["pit_catalog_families"]
    assert catalog["status"] == "FRESH" and catalog["data"]["source_count"] == 124
    registry = out["supplementary"]["monitor_registry"]
    assert registry["data"]["source_count"] == 18 and registry["sha256"]


def test_source_families_reads_the_coverage_ledger(core, project):
    folder = project / "implementation" / "backfill" / "2025-08-06_to_2026-08-06"
    folder.mkdir(parents=True)
    families = [{"rank": i, "source_family_id": f"fam{i}", "coverage_status": "PARTIAL",
                 "pit_class": "OBSERVED_PIT", "record_count": 10, "file_count": 1, "bytes": 100,
                 "artifacts": [{"path": "x"}]} for i in range(1, 26)]
    (folder / "top25_backfill_coverage.json").write_text(
        json.dumps({"generated_at": "2026-09-20T12:00:00Z", "families": families}))
    out = core.source_families(now=NOW)
    assert out["status"] == "FRESH" and out["as_of"] == "2026-09-20T12:00:00Z"
    data = out["data"]
    assert data["family_count"] == 25 and data["record_count"] == 250
    assert data["by_coverage_status"] == {"PARTIAL": 25}
    assert "artifacts" not in data["families"][0]


# --- ASM boards --------------------------------------------------------------


def test_signal_state_is_stale_since_the_monitor_date(core, project):
    out = core.signal_state(now=NOW)
    assert out["status"] == "STALE"
    assert out["as_of"] == "2026-08-15T04:00:00Z"
    assert out["data"]["by_state"] == {"ELEVATED": 2, "QUIET": 1}
    assert out["data"]["rows"][0]["z"] == pytest.approx(1.8243729569643048)


def test_signal_state_missing_file(core, project):
    (project / "implementation/alpha_signal_monitor_v01/data/signal_state.csv").unlink()
    out = core.signal_state(now=NOW)
    assert out["status"] == "MISSING" and out["data"] is None


def test_lane_status_as_of_prefers_the_asm_build_log(core, project):
    lanes = project / "implementation/alpha_signal_monitor_v01/data/lane_status.csv"
    stamp = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc).timestamp()
    os.utime(lanes, (stamp, stamp))
    out = core.lane_status(now=NOW)
    assert out["as_of_basis"] == "file_mtime" and out["status"] == "FRESH"
    assert out["data"]["by_freshness"] == {"FRESH": 6, "STALE": 6}
    assert out["data"]["by_collector_status"] == {"LIVE": 6, "NOT_BUILT": 6}
    (lanes.parent / "dashboard_build_log.csv").write_text(
        "build_ts,asof,note,panels_live,signals,tests_in_ledger\n"
        "2026-08-15T21:10:00,2026-08-15,phase 1 build,3,12,35\n")
    out = core.lane_status(now=NOW)
    assert out["as_of_basis"].startswith("content:dashboard_build_log.asof")
    assert out["status"] == "STALE"
    assert out["supplementary"]["asm_build_log"]["sha256"]


def test_test_ledger_hurdle(core, project):
    out = core.test_ledger(now=NOW)
    data = out["data"]
    assert data["cumulative_tests"] == 35 and data["distinct_test_ids"] == 12
    assert data["hurdle_t"] == 3.34
    assert data["hurdle_rule"] == "max(3.0, 2.57 + 0.5 * log10(cumulative_tests))"
    assert out["status"] == "STALE"
    assert core.hlz_hurdle(0) == 3.0 and core.hlz_hurdle(1) == 3.0
    assert core.hlz_hurdle(1000) == pytest.approx(4.07)


def test_promotion_board(core, project):
    data = core.promotion_board(now=NOW)["data"]
    assert data["count"] == 12
    assert data["by_eligible"] == {"NO_INSUFFICIENT_HISTORY": 9, "YES": 3}
    assert data["gates_run"] == 0 and data["gates_total"] == 96
    assert data["rows"][0]["gates"]["A3_t_ge_3"] == "NOT_RUN"


# --- pit_inventory -------------------------------------------------------------


def test_pit_inventory_latest_run_and_ledgers(core, project, tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "empty-home"))
    out = core.pit_inventory(now=NOW)
    assert out["status"] == "FRESH" and out["as_of"] == "2026-09-27T14:38:00Z"
    latest = out["data"]["latest_run"]
    assert latest["overall"] == "PASS" and latest["checks_passed"] == 10
    assert latest["exit_code"] is None
    assert any("no exit= line" in note for note in out["notes"])
    assert [r["overall"] for r in out["data"]["recent_runs"]] == ["PASS", "PASS", "PASS"]
    assert out["data"]["recent_runs"][0]["exit_code"] == 0
    assert latest["facts"]["observations"] == 4729254
    ledgers = out["supplementary"]["ledgers"]
    assert ledgers["data_source_ledger"]["summary"]["rows"] == 124
    assert ledgers["ingest_ledger"]["summary"]["last_24h_by_status"] == {"ok": 11}
    assert ledgers["inventory"]["summary"]["prices"]["securities"] == 57
    receipts = out["supplementary"]["receipts"]
    assert receipts["audit"]["status"] == "MISSING" and receipts["index"]["status"] == "MISSING"


def test_pit_receipts_follow_the_one_hour_audit_window(core, project, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "pit_audit_receipt.json").write_text(json.dumps(
        {"audited_at_utc": (NOW - timedelta(minutes=30)).isoformat(), "status": "PASS",
         "checks": 10, "signature": {"files": {}}}))
    (home / "pit_index_receipt.json").write_text(json.dumps(
        {"refreshed_at_utc": (NOW - timedelta(hours=40)).isoformat(), "rows": 5}))
    monkeypatch.setenv("VIBE_TRADING_HOME", str(home))
    receipts = core.pit_inventory(now=NOW)["supplementary"]["receipts"]
    assert receipts["audit"]["status"] == "FRESH" and receipts["audit"]["receipt_status"] == "PASS"
    assert receipts["index"]["status"] == "STALE"
    later = core.pit_inventory(now=NOW + timedelta(hours=1))["supplementary"]["receipts"]
    assert later["audit"]["status"] == "STALE"


def test_pit_inventory_missing_log(core, project):
    (project / "implementation/pit_warehouse/logs/pitdb_daily.log").unlink()
    out = core.pit_inventory(now=NOW)
    assert out["status"] == "MISSING"
    assert out["supplementary"]["ledgers"]["paper_ledger"]["summary"]["rows"] == 13


# --- world_model ---------------------------------------------------------------


def test_world_model_is_honestly_not_available(core, project):
    out = core.world_model(now=NOW)
    assert out["status"] == "MISSING"
    assert out["data"]["availability"] == "NOT_AVAILABLE"
    refs = {r["path"]: r["exists"] for r in out["data"]["human_readable_references"]}
    assert refs == {"WORLD_MODEL_V06.html": True, "WORLD_MODEL_ENHANCEMENT_PLAN_2026-08-15.md": True}


def test_world_model_reads_a_declared_export(core, project, monkeypatch):
    target = project / "implementation" / "world_model"
    target.mkdir(parents=True)
    (target / "export.json").write_text(json.dumps({"generated_at": "2026-09-28T00:00:00Z",
                                                    "stories": [{"id": "S1"}]}))
    monkeypatch.setenv("ZT_WORLD_MODEL_PATH", "implementation/world_model/export.json")
    out = core.world_model(now=NOW)
    assert out["status"] == "FRESH" and out["data"]["availability"] == "AVAILABLE"
    assert out["data"]["state"]["stories"] == [{"id": "S1"}]
    for bad in ("../outside.json", "implementation/world_model/export.txt"):
        monkeypatch.setenv("ZT_WORLD_MODEL_PATH", bad)
        with pytest.raises(ValueError):
            core.world_model(now=NOW)


# --- project_reports and the viewer ----------------------------------------------


def test_project_reports_lists_hub_and_root_dashboards(core, project):
    assert core.warm_report_cache() == 5
    assert core.warm_report_cache() == 0  # cached: nothing is read twice
    out = core.project_reports(now=NOW)
    assert out["data"]["counts"]["digests_pending"] == 0
    assert out["status"] == "STALE"  # hub curation asOf 2026-08-15
    reports = {r["id"]: r for r in out["data"]["reports"]}
    top25 = reports["ALPHA_MONITOR_TOP25.html"]
    assert top25["origin"] == "hub+root" and top25["viewable"] is True
    assert top25["sha256"] == hashlib.sha256(
        (project / "ALPHA_MONITOR_TOP25.html").read_bytes()).hexdigest()
    assert reports["CATALYST_DASHBOARD.html"]["exists"] is False
    assert reports["CATALYST_DASHBOARD.html"]["viewable"] is False
    assert reports["ASM_PHASE2_LANE_OPS.html"]["title"] == "Phase 2 · lane ops"
    assert out["data"]["counts"]["viewable"] == 5
    other = {i["path"]: i["exists"] for i in out["data"]["other_hub_items"]}
    assert other["PROJECT_HUB.json"] is True and other["README.md"] is False


def test_project_reports_answers_from_stat_until_a_report_is_read(core, project, monkeypatch):
    """On Google Drive, reading every dashboard made the list take a minute on PC1:
    the list opens no report; the viewer and the warm-up pass fill digests and titles."""
    reads = []
    real_read_bytes = Path.read_bytes

    def spy(self):
        if self.suffix.lower() in (".html", ".htm"):
            reads.append(self.name)
        return real_read_bytes(self)

    real_open = Path.open

    def open_spy(self, *args, **kwargs):
        if self.suffix.lower() in (".html", ".htm"):
            reads.append(self.name)
        return real_open(self, *args, **kwargs)

    lookups = []
    real_stat, real_resolve = Path.stat, Path.resolve

    def stat_spy(self, *args, **kwargs):
        if self.suffix.lower() in (".html", ".htm"):
            lookups.append(self.name)
        return real_stat(self, *args, **kwargs)

    def resolve_spy(self, *args, **kwargs):
        if self.suffix.lower() in (".html", ".htm"):
            lookups.append(self.name)
        return real_resolve(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_bytes", spy)
        patched.setattr(Path, "open", open_spy)
        patched.setattr(Path, "stat", stat_spy)
        patched.setattr(Path, "resolve", resolve_spy)
        out = core.project_reports(now=NOW, warm=False)
    assert reads == []
    # Root dashboards come from one directory listing; only hub entries that are not
    # in the root folder (missing ones here) are looked up one by one.
    root_files = {p.name for p in project.iterdir() if p.suffix.lower() in (".html", ".htm")}
    assert root_files and not (set(lookups) & root_files)
    reports = {r["id"]: r for r in out["data"]["reports"]}
    top25, lane = reports["ALPHA_MONITOR_TOP25.html"], reports["ASM_PHASE2_LANE_OPS.html"]
    assert top25["sha256"] is None and top25["bytes"] > 0 and top25["viewable"] is True
    assert out["data"]["counts"]["digests_pending"] == 5
    source = (project / "ALPHA_MONITOR_TOP25.html").read_bytes()
    core.render_report("ALPHA_MONITOR_TOP25.html")
    out = core.project_reports(now=NOW, warm=False)
    reports = {r["id"]: r for r in out["data"]["reports"]}
    assert reports["ALPHA_MONITOR_TOP25.html"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert out["data"]["counts"]["digests_pending"] == 4
    # An edited report is read again: the cache key carries size and mtime.
    path = project / "ALPHA_MONITOR_TOP25.html"
    path.write_bytes(source + b"<!-- edited -->")
    out = core.project_reports(now=NOW, warm=False)
    reports = {r["id"]: r for r in out["data"]["reports"]}
    assert reports["ALPHA_MONITOR_TOP25.html"]["sha256"] is None
    assert lane["title"] in ("Phase 2 · lane ops", "ASM_PHASE2_LANE_OPS.html")


def test_project_reports_starts_one_background_warmup(core, project):
    out = core.project_reports(now=NOW)
    assert out["data"]["counts"]["digests_pending"] == 5
    thread = core._REPORT_WARMUP
    assert thread is not None and thread.name == "zt-report-warmup"
    thread.join(timeout=30)
    out = core.project_reports(now=NOW)
    assert out["data"]["counts"]["digests_pending"] == 0
    reports = {r["id"]: r for r in out["data"]["reports"]}
    assert reports["ASM_PHASE2_LANE_OPS.html"]["title"] == "Phase 2 · lane ops"


def test_render_report_injects_shim_and_sets_isolating_headers(core, project):
    source = (project / "ALPHA_MONITOR_TOP25.html").read_bytes()
    body, headers = core.render_report("ALPHA_MONITOR_TOP25.html")
    assert body.startswith(b"<!doctype html><html lang='en'><head><script>")
    assert body.replace(core.REPORT_SHIM, b"", 1) == source
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    csp = headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in csp and "connect-src 'none'" in csp
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp
    assert headers["X-ZT-Source-SHA256"] == hashlib.sha256(source).hexdigest()
    assert core.inject_shim(b"<p>x</p>") == core.REPORT_SHIM + b"<p>x</p>"


def test_render_report_enforces_the_whitelist(core, project):
    (project / "implementation" / "exports" / "2026-09-27" / "daily_brief.html").write_text("x")
    (project / "AGENTS.md").write_text("rules")
    for bad in ("../AGENTS.md", "AGENTS.md", "implementation/exports/2026-09-27/daily_brief.html",
                "CATALYST_DASHBOARD.html", "alpha_monitor_top25.html", "/etc/passwd", ""):
        with pytest.raises(core.ReportNotFound):
            core.render_report(bad)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs a privilege on Windows")
def test_symlinked_dashboard_leaving_the_root_is_not_whitelisted(core, project, tmp_path):
    outside = tmp_path / "outside.html"
    outside.write_text("<html>secret</html>")
    (project / "LEAK.html").symlink_to(outside)
    assert "LEAK.html" not in core.report_index(project)
    with pytest.raises(core.ReportNotFound):
        core.render_report("LEAK.html")


def test_unparseable_source_is_reported_not_raised(core, project):
    (project / "implementation/exports/2026-09-27/portfolio_context.json").write_text("{not json")
    out = core.daily_snapshot("latest", now=NOW)
    assert "could not be parsed" in out["data"]["portfolio_context"]["error"]
    folder = project / "implementation" / "backfill" / "2025-08-06_to_2026-08-06"
    folder.mkdir(parents=True)
    (folder / "top25_backfill_coverage.json").write_text("[{broken")
    out = core.source_families(now=NOW)
    assert out["status"] == "STALE" and out["as_of"] is None
    assert "could not be parsed" in out["data"]["error"] and len(out["sha256"]) == 64
