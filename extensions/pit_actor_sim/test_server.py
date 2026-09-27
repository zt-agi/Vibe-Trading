"""Focused regression checks for the extension's fail-closed inputs."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server


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
            (base / "scenario.yaml").write_text("scenario", encoding="utf-8")
            (root / "AGENTS.md").write_text("governance", encoding="utf-8")
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
                (base / name).write_text("fixture", encoding="utf-8")
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
            with patch.object(server, "project", return_value=root), \
                 patch.object(server, "runtime", return_value=Path(directory)), \
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
                (base / name).write_text("fixture", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()