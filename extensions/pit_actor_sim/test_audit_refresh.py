"""Read-only audit admission and immutable old receipt on any failed refresh."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import server


@pytest.mark.parametrize("failure", [None, "missing", "failed", "exit", "changed", "path", "stale", "timeout"])
def test_audit_refresh_admission(tmp_path, failure):
    index = tmp_path / "index.duckdb"
    index.write_bytes(b"unchanged index")
    index_receipt = {"index_path": str(index), "signature": {"files": {}}}
    (tmp_path / server.pit_guard.INDEX_RECEIPT).write_text(json.dumps(index_receipt))
    old = tmp_path / server.pit_guard.AUDIT_RECEIPT
    old.write_text('"old receipt"')
    config = SimpleNamespace(DB_PATH=index if failure != "path" else tmp_path / "wrong", LAKE_ROOT=tmp_path)
    checks = range(1, 11 if failure == "missing" else 12)
    stdout = "\n".join(f"[PASS] A{i} check" for i in checks) + "\nOVERALL: PASS"
    if failure == "failed":
        stdout += "\n[FAIL] A1 failed"
    def run(*args, **kwargs):
        assert "audit_worker.py" in args[0][1]
        assert kwargs["env"]["PITDB_INDEX"] == str(index)
        assert kwargs["timeout"] == 60
        if failure == "changed":
            index.write_bytes(b"mutated")
        if failure == "timeout":
            raise server.subprocess.TimeoutExpired(args[0], 60)
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout=stdout, stderr="")
    with patch.object(server, "project", return_value=tmp_path), \
         patch.object(server, "runtime", return_value=tmp_path), \
         patch.object(server, "lake_signature", return_value={"files": {}}), \
         patch.object(server, "require_fresh_index", side_effect=RuntimeError("stale") if failure == "stale" else None), \
         patch.object(server, "index_path", return_value=index), \
         patch.object(server.pit_guard, "pitdb_config", return_value=config), \
         patch.object(server, "child_env", return_value={"PITDB_INDEX": str(index)}), \
         patch.object(server.subprocess, "run", side_effect=run):
        if failure:
            with pytest.raises(Exception):
                server.refresh_audit()
            assert old.read_text() == '"old receipt"'
        else:
            receipt = server.refresh_audit()
            assert receipt["checks"] == 11
            assert json.loads(old.read_text())["status"] == "PASS"
            assert index.read_bytes() == b"unchanged index"
