"""Checks for the shared receipt guards (pit_guard.py) and the audit receipt fields."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pit_guard
import server

AUDIT_STDOUT = """
=== PIT INTEGRITY ===
   [PASS] A1 no fact precedes its own knowledge
   [PASS] A10 no negative or zero close
   [PASS] A11 formation view never returns a bar known after event_date + lag

   OVERALL: PASS
"""


def _home() -> str:
    home = os.environ.get("VIBE_TRADING_HOME", "").strip()
    if not home:
        raise unittest.SkipTest("VIBE_TRADING_HOME must point at the E: runtime for these tests")
    return home


class AuditReceiptTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=_home())
        self.runtime = Path(self.directory.name)
        self.signature = {"lake_root": "lake", "files": {"fact_price_eod": [1, 2]}}

    def tearDown(self):
        self.directory.cleanup()

    def write(self, **fields):
        receipt = {"signature": self.signature, "status": "PASS",
                   "audited_at_utc": datetime.now(timezone.utc).isoformat(),
                   "checks_passed": ["A1", "A11"], **fields}
        pit_guard.write_json_atomic(self.runtime / pit_guard.AUDIT_RECEIPT, receipt)

    def test_parse_audit_checks(self):
        parsed = pit_guard.parse_audit_checks(AUDIT_STDOUT + "   [FAIL] A7 orphan\n")
        self.assertEqual(parsed, {"passed": ["A1", "A10", "A11"], "failed": ["A7"]})

    def test_fresh_receipt_with_required_check(self):
        self.write()
        receipt = pit_guard.require_fresh_audit(self.runtime, lambda: self.signature,
                                                required_checks=("A11",))
        self.assertEqual(receipt["status"], "PASS")

    def test_receipt_without_required_check_is_refused(self):
        self.write(checks_passed=["A1"])
        with self.assertRaisesRegex(RuntimeError, "A11"):
            pit_guard.require_fresh_audit(self.runtime, lambda: self.signature,
                                          required_checks=("A11",))

    def test_expired_missing_and_changed_lake(self):
        with self.assertRaisesRegex(RuntimeError, "receipt missing"):
            pit_guard.require_fresh_audit(self.runtime, lambda: self.signature)
        self.write(audited_at_utc=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        with self.assertRaisesRegex(RuntimeError, "receipt expired"):
            pit_guard.require_fresh_audit(self.runtime, lambda: self.signature)
        self.write()
        with self.assertRaisesRegex(RuntimeError, "lake changed"):
            pit_guard.require_fresh_audit(self.runtime, lambda: {"lake_root": "other"})

    def test_signature_digest_is_stable(self):
        a = pit_guard.signature_digest({"b": 1, "a": [1, 2]})
        self.assertEqual(a, pit_guard.signature_digest({"a": [1, 2], "b": 1}))
        self.assertTrue(a.startswith("sha256:"))

    def test_refresh_audit_records_the_checks_it_saw(self):
        root = self.runtime / "work" / "Investment-AI-Drive-Research"
        root.mkdir(parents=True)
        with patch.object(server, "project", return_value=root), \
             patch.object(server, "runtime", return_value=self.runtime), \
             patch.object(server, "lake_signature", return_value=self.signature), \
             patch.object(server, "child_env", return_value={}), \
             patch.object(server.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=AUDIT_STDOUT, stderr="")):
            receipt = server.refresh_audit()
        self.assertEqual(receipt["checks_passed"], ["A1", "A10", "A11"])
        self.assertEqual(receipt["checks"], 3)
        stored = json.loads((self.runtime / pit_guard.AUDIT_RECEIPT).read_text(encoding="utf-8"))
        self.assertEqual(stored["checks_passed"], ["A1", "A10", "A11"])


if __name__ == "__main__":
    unittest.main()
