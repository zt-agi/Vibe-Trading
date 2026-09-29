"""Focused regression checks for the extension's fail-closed inputs."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server


def setUpModule():
    # Fail loudly instead of letting tempfile fall back to the system TEMP on C:.
    home = os.environ.get("VIBE_TRADING_HOME", "").strip()
    if not home:
        raise RuntimeError(
            "VIBE_TRADING_HOME must be set to the E: runtime before running these tests "
            "(ZT 2026-09-28: everything on E:)")
    if os.name == "nt" and server.windows_drive(Path(home).resolve()) != "E:":
        raise RuntimeError(f"VIBE_TRADING_HOME must be on E: for these tests; got {home}")


class ExtensionGuardsTest(unittest.TestCase):
    def test_asof_requires_explicit_past_offset(self):
        with self.assertRaises(ValueError):
            server.asof_utc("2026-01-01T12:00:00")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            server.asof_utc(future)
        self.assertEqual(
            server.asof_utc("2026-01-01T07:00:00-05:00"),
            datetime(2026, 1, 1, 12),
        )

    def test_simulation_path_cannot_escape_project(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("VIBE_TRADING_HOME")) as directory:
            root = Path(directory) / "work" / "Investment-AI-Drive-Research"
            base = root / "market_actor_sim"
            base.mkdir(parents=True)
            (base / "scenario.yaml").write_text("scenario", encoding="utf-8", newline="\n")
            (root / "AGENTS.md").write_text("governance", encoding="utf-8", newline="\n")
            with patch.object(server, "project", return_value=root):
                self.assertEqual(server.sim_input("scenario.yaml"), base / "scenario.yaml")
                with self.assertRaises(ValueError):
                    server.sim_input("../AGENTS.md")

    def test_invalid_evidence_stops_before_audit_or_run(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("VIBE_TRADING_HOME")) as directory:
            root = Path(directory) / "work" / "Investment-AI-Drive-Research"
            base = root / "market_actor_sim"
            base.mkdir(parents=True)
            for name in ("scenario.yaml", "forks.json"):
                (base / name).write_text("fixture", encoding="utf-8", newline="\n")
            (base / "evidence.json").write_text(
                json.dumps({"warehouse_audit": {"status": "FAIL"}}), encoding="utf-8"
            )
            with patch.object(server, "project", return_value=root), \
                 patch.object(server.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "passing PIT audit"):
                    server.run_market_actor_sim(
                        "scenario.yaml", "forks.json", "evidence.json"
                    )
                run.assert_not_called()

    def test_audit_fail_text_does_not_create_receipt(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("VIBE_TRADING_HOME")) as directory:
            root = Path(directory) / "work" / "Investment-AI-Drive-Research"
            root.mkdir(parents=True)
            index = Path(directory) / "index.duckdb"
            index.write_bytes(b"fixture")
            (Path(directory) / server.pit_guard.INDEX_RECEIPT).write_text(json.dumps({"index_path": str(index)}))
            with patch.object(server, "project", return_value=root), \
                 patch.object(server, "runtime", return_value=Path(directory)), \
                 patch.object(server, "require_fresh_index"), \
                 patch.object(server, "index_path", return_value=index), \
                 patch.object(server.pit_guard, "pitdb_config", return_value=SimpleNamespace(DB_PATH=index, LAKE_ROOT=root)), \
                 patch.object(server, "lake_signature", return_value={"files": {}}), \
                 patch.object(server, "child_env", return_value={}), \
                 patch.object(server.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout="OVERALL: FAIL", stderr="")):
                with self.assertRaisesRegex(RuntimeError, "PIT audit failed"):
                    server.refresh_audit()
                self.assertFalse((Path(directory) / "pit_audit_receipt.json").exists())

    def test_stale_audit_receipt_blocks_simulation(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("VIBE_TRADING_HOME")) as directory:
            root = Path(directory) / "work" / "Investment-AI-Drive-Research"
            base = root / "market_actor_sim"
            base.mkdir(parents=True)
            for name in ("scenario.yaml", "forks.json"):
                (base / name).write_text("fixture", encoding="utf-8", newline="\n")
            (base / "evidence.json").write_text(
                json.dumps({"warehouse_audit": {"status": "PASS"}}), encoding="utf-8"
            )
            receipt = {"signature": {"files": {}}, "status": "PASS",
                       "audited_at_utc": "2020-01-01T00:00:00+00:00"}
            (Path(directory) / "pit_audit_receipt.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            with patch.object(server, "project", return_value=root), \
                 patch.object(server, "runtime", return_value=Path(directory)), \
                 patch.object(server, "require_fresh_index", return_value=None), \
                 patch.object(server.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "receipt expired"):
                    server.run_market_actor_sim("scenario.yaml", "forks.json", "evidence.json")
                run.assert_not_called()


class RuntimeHomeTest(unittest.TestCase):
    def test_runtime_requires_vibe_trading_home(self):
        with patch.dict(os.environ, {"VIBE_TRADING_HOME": ""}):
            with self.assertRaisesRegex(RuntimeError, "VIBE_TRADING_HOME is required"):
                server.runtime()


@unittest.skipUnless(os.name == "nt", "Windows drive policy (ZT 2026-09-28: everything on E:)")
class DrivePolicyTest(unittest.TestCase):
    def fake_project(self):
        return (Path(os.environ["VIBE_TRADING_HOME"]) / "no-such-project"
                / "work" / "Investment-AI-Drive-Research")

    def runtime_for(self, home):
        with patch.dict(os.environ, {"VIBE_TRADING_HOME": home}), \
             patch.object(server, "project", return_value=self.fake_project()):
            return server.runtime()

    def test_c_drive_runtime_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be on E:"):
            self.runtime_for(r"C:\vibe-trading\home")

    def test_d_drive_runtime_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be on E:"):
            self.runtime_for(r"D:\codex-runtime\investment-ai\vibe-trading\home")

    def test_system_drive_runtime_rejected(self):
        with patch.dict(os.environ, {"SystemDrive": "E:"}):
            with self.assertRaisesRegex(ValueError, "system drive E:"):
                self.runtime_for(os.environ["VIBE_TRADING_HOME"])

    def test_e_drive_runtime_accepted(self):
        with tempfile.TemporaryDirectory(dir=os.environ["VIBE_TRADING_HOME"]) as directory:
            self.assertEqual(self.runtime_for(directory), Path(directory).resolve())

    def test_index_on_d_drive_rejected(self):
        config = types.ModuleType("pitdb.config")
        config.DB_PATH = Path(r"D:\pitdb\pit.duckdb")
        config.PERSISTED_TABLES = []
        config.LAKE_ROOT = self.fake_project()
        package = types.ModuleType("pitdb")
        package.config = config
        with patch.dict(sys.modules, {"pitdb": package, "pitdb.config": config}), \
             patch.object(server, "project", return_value=self.fake_project()):
            with self.assertRaisesRegex(ValueError, "must be on E:"):
                server.lake_signature()

    def test_child_env_passes_validated_e_index(self):
        with tempfile.TemporaryDirectory(dir=os.environ["VIBE_TRADING_HOME"]) as directory, \
             patch.object(server, "project", return_value=self.fake_project()), \
             patch.object(server, "runtime", return_value=Path(directory)):
            for value in ("", "local", server.DEFAULT_INDEX):
                with patch.dict(os.environ, {"PITDB_INDEX": value}):
                    self.assertEqual(server.child_env()["PITDB_INDEX"], server.DEFAULT_INDEX)
            with patch.dict(os.environ, {"PITDB_INDEX": r"D:\pitdb\pit.duckdb"}):
                with self.assertRaisesRegex(ValueError, "must be on E:"):
                    server.child_env()


if __name__ == "__main__":
    unittest.main()
