"""Regression checks for the zt-daily-report MCP server.

No network and no warehouse: the PIT layer and the CBOE fetch are replaced by
fakes. A sandbox project is built under VIBE_TRADING_HOME (never the system
temp folder, never the real project); the report package is copied into it from
ZT_DAILY_REPORT_PACKAGE_SOURCE, or from INVESTMENT_AI_PROJECT_ROOT when that is
set.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import random
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server

PROJECT_NAME = "Investment-AI-Drive-Research"
WATCHLIST = """schema: daily-brief-watchlist/1
purpose: research watchlist (not holdings)
tickers:
  - {symbol: DEMOA, theme: synthetic, pitdb: expected}
  - {symbol: DEMOB, theme: synthetic, pitdb: expected}
  - {symbol: DEMOC, theme: synthetic, pitdb: expected}
pending: []
"""


def setUpModule():
    # Fail loudly instead of letting tempfile fall back to the system TEMP on C:.
    home = os.environ.get("VIBE_TRADING_HOME", "").strip()
    if not home:
        raise RuntimeError("VIBE_TRADING_HOME must be set to the E: runtime before running these tests "
                           "(ZT 2026-09-28: everything on E:)")
    if os.name == "nt" and server.windows_drive(Path(home).resolve()) != "E:":
        raise RuntimeError(f"VIBE_TRADING_HOME must be on E: for these tests; got {home}")


def package_source() -> Path | None:
    raw = os.environ.get("ZT_DAILY_REPORT_PACKAGE_SOURCE", "").strip()
    if raw:
        return Path(raw)
    root = os.environ.get("INVESTMENT_AI_PROJECT_ROOT", "").strip()
    return Path(root) / "implementation" / "daily_research_report" if root else None


class Sandbox(unittest.TestCase):
    """A throw-away project and runtime under VIBE_TRADING_HOME."""

    def setUp(self):
        source = package_source()
        if source is None or not (source / "validate_report.py").is_file():
            self.skipTest("set ZT_DAILY_REPORT_PACKAGE_SOURCE (or INVESTMENT_AI_PROJECT_ROOT) to the report package")
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ["VIBE_TRADING_HOME"])
        base = Path(self._tmp.name)
        self.project = base / "work" / PROJECT_NAME
        self.home = base / "runtime"
        self.home.mkdir(parents=True)
        shutil.copytree(source, self.project / "implementation" / "daily_research_report",
                        ignore=shutil.ignore_patterns("__pycache__", "tests", "examples"))
        addons = self.project / "vt_addons"
        (addons / "config").mkdir(parents=True)
        (addons / "config" / "daily_brief_watchlist.yaml").write_text(WATCHLIST, encoding="utf-8")
        (addons / "swarm_presets").mkdir()
        (addons / "swarm_presets" / "premarket_brief_team.yaml").write_text("name: premarket_brief_team\n",
                                                                           encoding="utf-8")
        self.env = patch.dict(os.environ, {"INVESTMENT_AI_PROJECT_ROOT": str(self.project),
                                           "VIBE_TRADING_HOME": str(self.home), "ZT_DAILY_REPORT_PACKAGE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self._tmp.cleanup)
        fixture = source / "fixtures" / "deterministic_sample_v2.json"
        self.fixture = json.loads(fixture.read_text(encoding="utf-8"))
        # No PASS claim and no staged rows unless a test sets them up.
        self.fixture["run_metadata"]["pit_audit"] = {"status": "STALE", "audited_at_utc": None, "receipt_sha256": None}
        self.fixture["forecast_track"].update(recorded_total=0, recorded_this_run=0)

    def doc(self, **changes) -> dict:
        data = copy.deepcopy(self.fixture)
        data.update(changes)
        return data

    def published(self) -> Path:
        return self.project / "reports" / "daily" / "2026-09-28" / self.fixture["run_id"]


class ValidateAndPublishTests(Sandbox):
    def test_validate_passes_and_reports_server_checks(self):
        result = server.validate_daily_report(json.dumps(self.doc()))
        self.assertEqual(result["status"], "PASS", result["errors"])
        self.assertEqual(result["error_count"], 0)

    def test_invalid_json_is_a_failure_not_a_crash(self):
        result = server.validate_daily_report("{not json")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("not valid JSON", result["errors"][0])

    def test_publish_writes_run_folder_manifest_and_pointer(self):
        result = server.publish_daily_report(json.dumps(self.doc()))
        self.assertEqual(result["status"], "PUBLISHED", result)
        run = self.published()
        for name in ("report.html", "input.json", "validation.json", "manifest.json", "evidence/reader-a.txt"):
            self.assertTrue((run / name).is_file(), name)
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        text = json.dumps(manifest)
        self.assertNotIn(str(self.project), text)
        self.assertNotIn(str(self.home), text)
        self.assertEqual(manifest["publication_status"], "PUBLISHED")
        self.assertEqual(manifest["artifacts"]["report"],
                         {"root": "project", "path": f"reports/daily/2026-09-28/{self.fixture['run_id']}/report.html",
                          "sha256": hashlib.sha256((run / "report.html").read_bytes()).hexdigest(),
                          "bytes": (run / "report.html").stat().st_size})
        self.assertTrue(all(c["path"].startswith("implementation/daily_research_report/")
                            for c in manifest["artifacts"]["code"]))
        self.assertEqual(manifest["engine"]["server"], "zt-daily-report")
        presets = {p["name"]: p for p in manifest["presets"]}
        self.assertEqual(presets["premarket_brief_team"]["path"], "vt_addons/swarm_presets/premarket_brief_team.yaml")
        self.assertEqual(presets["premarket-brief"]["status"], "NOT_FOUND")
        validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))
        self.assertEqual(validation["status"], "PASS")
        pointer = (self.project / server.POINTER_NAME).read_text(encoding="utf-8")
        self.assertIn(f'url=reports/daily/2026-09-28/{self.fixture["run_id"]}/report.html', pointer)
        self.assertEqual(list((self.home / "daily_report" / "scratch").iterdir()), [])
        self.assertEqual([p.name for p in run.parent.iterdir()], [self.fixture["run_id"]])

    def test_publish_refuses_overwrite(self):
        self.assertEqual(server.publish_daily_report(json.dumps(self.doc()))["status"], "PUBLISHED")
        before = {p.name: p.read_bytes() for p in self.published().iterdir() if p.is_file()}
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            server.publish_daily_report(json.dumps(self.doc()))
        after = {p.name: p.read_bytes() for p in self.published().iterdir() if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn("already published", " ".join(server.validate_daily_report(json.dumps(self.doc()))["warnings"]))

    def test_publish_refuses_a_run_id_published_on_another_date(self):
        (self.project / "reports" / "daily" / "2026-09-25" / self.fixture["run_id"]).mkdir(parents=True)
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            server.publish_daily_report(json.dumps(self.doc()))

    def test_publish_rejects_invalid_input_without_writing(self):
        data = self.doc()
        data["current_drivers"][0]["counterpoint"] = "Investors should sell DEMOA into strength."
        result = server.publish_daily_report(json.dumps(data))
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["published"])
        self.assertFalse((self.project / "reports").exists())
        self.assertFalse((self.project / server.POINTER_NAME).exists())

    def test_non_watchlist_symbol_is_rejected(self):
        data = self.doc()
        data["watchlist"].append({"symbol": "OTHER", "sec_id": 5, "pitdb_status": "available", "research_only": True})
        data["verdict"].append({"ticker": "OTHER", "state": "QUIET", "reason": "Nothing new."})
        data["attribution"]["rows"].append({"ticker": "OTHER", "close_figure": None, "change_figure": None,
                                            "premarket_figure": None, "premarket_status": "missing",
                                            "linked_event_ids": []})
        result = server.validate_daily_report(json.dumps(data))
        self.assertIn("not on the research watchlist config", " ".join(result["errors"]))

    def test_pointer_never_moves_backwards(self):
        later = self.doc(cutoff="2026-09-29T09:25:00-04:00", run_id="later_run")
        self.assertTrue(server.update_pointer(self.project, "reports/daily/x/later_run/report.html", later, "a" * 64)[0])
        earlier = self.doc()
        moved, note = server.update_pointer(self.project, "reports/daily/y/report.html", earlier, "b" * 64)
        self.assertFalse(moved)
        self.assertIn("later cutoff", note)
        self.assertIn("later_run", (self.project / server.POINTER_NAME).read_text(encoding="utf-8"))

    def test_mcp_tool_surface(self):
        from fastmcp import Client

        async def call():
            async with Client(server.mcp) as client:
                names = sorted(t.name for t in await client.list_tools())
                result = await client.call_tool("validate_daily_report", {"input_json": json.dumps(self.doc())})
                return names, result.data
        names, data = asyncio.run(call())
        self.assertEqual(names, ["pit_audit_status", "price_figures", "publish_daily_report", "stage_forecast_rows",
                                 "validate_daily_report"])
        self.assertEqual(data["status"], "PASS")


class AuditTests(Sandbox):
    def write_receipt(self, minutes_ago: float, status: str = "PASS") -> str:
        audited = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        body = json.dumps({"signature": {"files": {}}, "audited_at_utc": audited.isoformat(), "status": status})
        (self.home / "pit_audit_receipt.json").write_text(body, encoding="utf-8")
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def test_states_are_reported_honestly(self):
        self.assertEqual(server.pit_audit_status()["status"], "MISSING")
        self.write_receipt(5, status="FAIL")
        self.assertEqual(server.pit_audit_status()["status"], "FAIL")
        self.write_receipt(90)
        self.assertEqual(server.pit_audit_status()["status"], "STALE")
        self.write_receipt(5)
        with patch.object(server, "lake_signature_matches", return_value=None):
            self.assertEqual(server.pit_audit_status()["status"], "UNVERIFIED")
        with patch.object(server, "lake_signature_matches", return_value=False):
            self.assertEqual(server.pit_audit_status()["status"], "STALE")
        with patch.object(server, "lake_signature_matches", return_value=True):
            result = server.pit_audit_status()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue((self.home / "daily_report" / "audit_receipts" / f"{result['receipt_sha256']}.json").is_file())

    def test_claimed_pass_must_have_been_observed_as_pass(self):
        claim = {"run_id": "fixture_run", "watchlist": [], "forecast_track": {},
                 "run_metadata": {"pit_audit": {"status": "PASS", "receipt_sha256": "e" * 64,
                                                "audited_at_utc": "2026-09-28T12:20:00+00:00"}}}
        self.assertIn("was not observed", " ".join(server.server_checks(claim)[0]))
        digest = self.write_receipt(5)
        with patch.object(server, "lake_signature_matches", return_value=False):
            observed = server.pit_audit_status()
        claim["run_metadata"]["pit_audit"].update(receipt_sha256=digest, audited_at_utc=observed["audited_at_utc"])
        self.assertIn("never observed that receipt as PASS", " ".join(server.server_checks(claim)[0]))
        with patch.object(server, "lake_signature_matches", return_value=True):
            server.pit_audit_status()
        self.assertEqual(server.server_checks(claim)[0], [])
        claim["run_metadata"]["pit_audit"]["audited_at_utc"] = "2026-01-01T00:00:00+00:00"
        self.assertIn("does not match the observed receipt", " ".join(server.server_checks(claim)[0]))


def last_closed_day(pkg, freeze: datetime) -> date:
    day = freeze.astimezone(pkg.trading_calendar.NEW_YORK).date()
    while not (pkg.trading_calendar.is_trading_day(day) and
               pkg.trading_calendar.bar_is_closed(day, freeze - timedelta(hours=2))):
        day -= timedelta(days=1)
    return day


def fake_pit(pkg, freeze: datetime, known: dict[str, int]):
    end = last_closed_day(pkg, freeze)

    def rows_for(sec_id: int) -> list[dict]:
        rng, price, rows, day = random.Random(sec_id), 100.0, [], end - timedelta(days=1100)
        while day <= end:
            if pkg.trading_calendar.is_trading_day(day):
                price *= math.exp(rng.gauss(0.0, 0.02))
                kt = pkg.trading_calendar.session_close_utc(day).astimezone(timezone.utc) + timedelta(minutes=30)
                rows.append({"event_date": day.isoformat(), "close": round(price, 4), "volume": 1_000_000,
                             "knowledge_time": kt.replace(tzinfo=None).isoformat(), "revision_seq": 0,
                             "source_id": "synthetic", "pit_class": "TRUE_PIT"})
            day += timedelta(days=1)
        return rows

    return SimpleNamespace(
        pit_security=lambda ticker, asof: {"securities": [{"sec_id": known[ticker]}] if ticker in known else []},
        pit_price_history=lambda sec_id, asof, start, end_, limit: {
            "rows": [r for r in rows_for(sec_id) if start <= r["event_date"] <= end_][-limit:]},
        lake_signature=lambda: {"files": {}})


def fake_chain_fetch(pkg, freeze: datetime):
    def fetch(ticker, dest_dir, timeout=30.0):
        ref = last_closed_day(pkg, freeze)
        options = []
        for expiry in (ref + timedelta(days=35), ref + timedelta(days=98)):
            t = pkg.trading_calendar.trading_days_between(ref, expiry)[0] / 252
            for strike in [100 * (0.6 + 0.025 * i) for i in range(33)]:
                d1 = (math.log(100 / strike) + 0.08 * t) / (0.4 * math.sqrt(t))
                call = pkg.forecast_baseline._N.cdf(d1)
                for kind, delta in (("C", call), ("P", call - 1)):
                    options.append({"option": f"{ticker}{expiry.strftime('%y%m%d')}{kind}{int(strike * 1000):08d}",
                                    "bid": 1.0, "ask": 1.1, "iv": 0.4, "delta": round(delta, 6)})
        payload = {"timestamp": (freeze - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                   "data": {"symbol": ticker, "current_price": 100.0, "options": options}}
        body = json.dumps(payload).encode("utf-8")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / f"{ticker}_test.json"
        path.write_bytes(body)
        return {"path": str(path), "url": "https://example.invalid/chain", "sha256": hashlib.sha256(body).hexdigest(),
                "fetched_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    return fetch


class StagingAndFigureTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.pkg = server.package()
        self.freeze = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
        self.sim = fake_pit(self.pkg, self.freeze, {"DEMOA": 9001, "DEMOB": 9002})

    def stage(self, tickers=("DEMOA", "DEMOB"), run_id="stage_run"):
        with patch.object(server, "pit_sim", return_value=self.sim), \
             patch.object(self.pkg.forecast_baseline, "fetch_cboe_chain", fake_chain_fetch(self.pkg, self.freeze)), \
             patch.object(server.time, "sleep", return_value=None):
            return server.stage_forecast_rows(list(tickers), self.freeze.isoformat(), run_id)

    def test_stage_records_rows_and_returns_counts_only(self):
        result = self.stage()
        self.assertEqual(result["status"], "STAGED", result)
        self.assertEqual(result["recorded_this_run"], 2 * 2 * 5)
        self.assertEqual(result["recorded_total"], 20)
        self.assertEqual(result["display_gate"], "CLOSED")
        self.assertTrue(all(t["implied"] and t["mechanical"] for t in result["tickers"].values()))
        text = json.dumps(result)
        for forbidden in ('"q05"', '"terminal"', '"price"', '"log_move"'):
            self.assertNotIn(forbidden, text)
        staged = self.home / result["staging_file"]
        rows = [json.loads(line) for line in staged.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 20)
        self.assertEqual({r["kind"] for r in rows}, {"distribution"})
        self.assertTrue(staged.with_suffix(".receipt.json").is_file())
        with self.assertRaisesRegex(FileExistsError, "already staged"):
            self.stage()

    def test_forecast_counts_in_a_report_must_match_staging(self):
        self.stage(run_id=self.fixture["run_id"])
        data = self.doc()
        errors = server.server_checks(data)[0]
        self.assertIn("rows are staged for this run", " ".join(errors))
        data["forecast_track"].update(recorded_this_run=20, recorded_total=20)
        self.assertEqual(server.server_checks(data)[0], [])
        data["forecast_track"]["recorded_total"] = 999
        self.assertIn("recorded_total must lie between", " ".join(server.server_checks(data)[0]))

    def test_staging_is_prospective_and_watchlist_only(self):
        with self.assertRaisesRegex(ValueError, "research watchlist"):
            self.stage(tickers=("SPY",))
        with patch.object(server, "pit_sim", return_value=self.sim):
            with self.assertRaisesRegex(ValueError, "prospective"):
                server.stage_forecast_rows(["DEMOA"], (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "x_run")
            with self.assertRaisesRegex(ValueError, "prospective"):
                server.stage_forecast_rows(["DEMOA"], (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "x_run")

    def test_price_figures_are_code_computed_with_refs(self):
        with patch.object(server, "pit_sim", return_value=self.sim):
            result = server.price_figures(["DEMOA", "DEMOC"], self.freeze.isoformat())
        ids = {f["figure_id"] for f in result["figures"]}
        self.assertLessEqual({"demoa_close", "demoa_chg1d", "demoa_chg5d", "demoa_mad20"}, ids)
        self.assertIsNotNone(result["tickers"]["DEMOC"]["error"])
        self.assertEqual(result["source"]["status"], "stale")
        self.assertIn("DEMOC", result["source"]["status_reason"])
        ref = next(f for f in result["figures"] if f["figure_id"] == "demoa_chg1d")["ref"]
        self.assertEqual((ref["tool"], ref["sec_id"]), ("mcp_zt_daily_report_price_figures", 9001))
        self.assertTrue(ref["knowledge_time"].endswith("Z"))


class PathPolicyTests(Sandbox):
    def test_project_root_must_be_the_canonical_folder(self):
        other = self.project.parent / "Somewhere-Else"
        other.mkdir()
        with patch.dict(os.environ, {"INVESTMENT_AI_PROJECT_ROOT": str(other)}):
            with self.assertRaisesRegex(ValueError, "canonical work project"):
                server.project()

    def test_runtime_may_not_live_in_the_project(self):
        inside = self.project / "runtime"
        inside.mkdir()
        with patch.dict(os.environ, {"VIBE_TRADING_HOME": str(inside)}):
            with self.assertRaisesRegex(ValueError, "shared project"):
                server.runtime()

    def test_runtime_is_required(self):
        with patch.dict(os.environ, {"VIBE_TRADING_HOME": ""}):
            with self.assertRaisesRegex(RuntimeError, "VIBE_TRADING_HOME is required"):
                server.runtime()


@unittest.skipUnless(os.name == "nt", "Windows drive policy (ZT 2026-09-28: everything on E:)")
class DrivePolicyTest(unittest.TestCase):
    def test_c_and_d_runtime_rejected(self):
        for raw in (r"C:\vibe-trading\home", r"D:\codex-runtime\home"):
            with self.assertRaisesRegex(ValueError, "must be on E:"):
                server.require_e_drive(Path(raw), "Runtime")

    def test_project_on_system_drive_rejected(self):
        with self.assertRaisesRegex(ValueError, "may not be on C:"):
            server.refuse_system_drives(Path(r"C:\Users\x\work\Investment-AI-Drive-Research"), "Project root")


if __name__ == "__main__":
    unittest.main()
