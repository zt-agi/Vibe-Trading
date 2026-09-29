"""Isolated paper browser fixture using real VT API routes and zt-paper engine.

Prices are deterministic TEST FIXTURES, never warehouse acceptance evidence.
Run with --base <fresh E: directory> --runtime <E: VT src> --port <private port>.
The JSON manifest contains proposal IDs and a disposable QA authentication key.
"""
import argparse
import importlib.util
import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    base, runtime = args.base.resolve(), args.runtime.resolve()
    if base.drive.upper() != "E:" or runtime.drive.upper() != "E:":
        raise RuntimeError("QA state and runtime must be on E:")
    if base.exists() or args.port == 8899 or not 1024 <= args.port <= 65535:
        raise RuntimeError("Use a fresh QA folder and a private non-production port")
    home = base / "home"
    (home / "live").mkdir(parents=True)
    (home / "live" / "HALT").write_text("Paper browser QA; live trading halted\n")
    key = secrets.token_hex(32)
    # Never inherit provider/broker credentials or production home configuration.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE",
               "NUMBER_OF_PROCESSORS", "PYTHONPATH", "PYTHONUTF8", "PYTHONUNBUFFERED",
               "PYTHONPYCACHEPREFIX", "INVESTMENT_AI_PROJECT_ROOT", "PITDB_INDEX",
               "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"}
    kept = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    os.environ.clear()
    os.environ.update(kept)
    os.environ.update(VIBE_TRADING_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
                      TEMP=str(base), TMP=str(base), TMPDIR=str(base), API_AUTH_KEY=key,
                      APPDATA=str(base / "AppData" / "Roaming"), LOCALAPPDATA=str(base / "AppData" / "Local"),
                      VIBE_ORDER_APPROVAL="required", VIBE_TRADING_ENABLE_SHELL_TOOLS="false",
                      VIBE_TRADING_ENABLE_SCHEDULER="false", VIBE_TRADING_CHANNELS_AUTO_START="false")
    (home / "agent.json").write_text('{"mcpServers": {}}')
    (home / ".env").write_text(f"API_AUTH_KEY={key}\nVIBE_ORDER_APPROVAL=required\n")
    sys.path.insert(0, str(runtime / "agent"))
    from src.live import order_proposals as core
    from src.config.accessor import reset_env_config
    reset_env_config()

    def fixture_close(symbol, **kwargs):
        ticker = str(symbol).upper().removesuffix(".US")
        return core.RefPrice(ticker, {"AAPL": 200.0, "MSFT": 400.0}[ticker], "2026-09-28", "TEST:browser-fixture")

    core.zt_paper_engine().reference_close = fixture_close
    core.vt_loader_close = fixture_close
    now = core._now

    def proposal(label, expired=False):
        if expired:
            core._now = lambda: now() - timedelta(hours=1)
        try:
            result = core.create_proposal(broker="zt-paper", orders=[{"symbol": "AAPL", "side": "buy", "qty": 10}],
                rationale=f"TEST FIXTURE {label}", evidence_ids=["TEST-PRICE-200"],
                signals=[{"source": "analyst", "symbol": "AAPL", "direction": "long", "evidence_ids": ["TEST-PRICE-200"]}],
                origin={"kind": "test", "actor": "paper-browser-fixture"})
            assert result["broker"]["live"] is False
            return result
        finally:
            core._now = now

    proposals = {name: proposal(name, name == "expired") for name in ("approve", "reject", "expired")}
    manifest = {"schema": "vt-paper-browser-fixture/1", "url": f"http://127.0.0.1:{args.port}",
                "key": key, "home": str(home), "halt": True, "prices": "TEST FIXTURE, no PIT acceptance",
                "proposals": {name: {"id": row["id"], "hash": row["content_hash"]} for name, row in proposals.items()}}
    (base / "fixture.json").write_text(json.dumps(manifest, indent=2))
    spec = importlib.util.spec_from_file_location("paper_fixture_launcher", runtime / "extensions" / "zt_dashboards" / "launch_api.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    api = launcher.prepare()
    api._API_KEY = key
    print(json.dumps({"fixture": str(base / "fixture.json"), "url": manifest["url"], "paper_only": True}), flush=True)
    return api.serve_main(["--host", "127.0.0.1", "--port", str(args.port)])


if __name__ == "__main__":
    raise SystemExit(main())
