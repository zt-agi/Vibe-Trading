"""ZT add-on: a throwaway Vibe-Trading server for the UI E2E suite (container mode).

    python tools/e2e/fixture_server.py --port 8911 [--via launch_api|vt-zt]

Builds a private runtime under a temporary folder (VIBE_TRADING_HOME, HOME and
USERPROFILE all point there, so the real ~/.vibe-trading is never touched), seeds
it with fixtures, and serves the checkout's built SPA through the wrapper
backend:

* ``.env`` with API_AUTH_KEY = $VT_E2E_KEY (required), the Ollama provider
  default, VIBE_ORDER_APPROVAL=required and the ZT playbook folder;
* a fake local Ollama (/api/version, /api/tags, /api/ps) on a free loopback
  port, so the pre-flight Ollama probe runs end to end;
* agent.json with the zt-dashboards MCP server, PIT audit and index receipts
  bound to the project's lake, a src_sync_receipt.json, and one synthetic run
  so /runs/<id> has something to show.

INVESTMENT_AI_PROJECT_ROOT is $VT_E2E_PROJECT_ROOT when set (read-only use),
else a copy of the zt_dashboards test fixture project. ``--via vt-zt`` starts
the server the way the desktop shell does, through ``python -m vt_zt_launcher
serve`` ($VT_ZT_LAUNCHER_DIR must hold the package). Never used on PC1: there
the suite runs against the real server.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LAUNCH_API = REPO / "extensions" / "zt_dashboards" / "launch_api.py"
FIXTURE_PROJECT = REPO / "extensions" / "zt_dashboards" / "tests" / "fixtures" / "Investment-AI-Drive-Research"
PLAYBOOKS = REPO / "extensions" / "zt_dashboards" / "playbooks"
PIT_TABLES = ("dim_source", "dim_security", "dim_security_alias", "dim_series", "dim_entity",
              "dim_calendar", "fact_price_eod", "fact_price_intraday", "fact_corp_action",
              "fact_observation", "fact_position", "fact_flow", "fact_option_quote",
              "fact_event", "doc_document", "doc_chunk")
MODELS = ("gpt-oss:20b", "qwen3:14b")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOllama(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/api/version":
            body = {"version": "0.12.6-e2e"}
        elif self.path == "/api/tags":
            body = {"models": [{"name": name, "model": name} for name in MODELS]}
        elif self.path == "/api/ps":
            body = {"models": [{"name": MODELS[0], "model": MODELS[0], "context_length": 65536}]}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the server log readable
        pass


def signature(lake: Path, tables) -> dict:
    files = {}
    for table in tables:
        try:
            stat = (lake / table / "data.parquet").stat()
            files[table] = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            files[table] = None
    return {"lake_root": str(lake.resolve()), "files": files}


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def seed_run(runs: Path) -> None:
    run = runs / "20260925_160000_e2e"
    write_json(run / "state.json", {"status": "success"})
    write_json(run / "req.json", {"prompt": "E2E fixture: SPY buy-and-hold, 2026-08-01 to 2026-09-25"})
    rows = ["timestamp,ret,equity,drawdown,benchmark_equity,active_ret"]
    equity, day = 100000.0, datetime(2026, 8, 3)
    for i in range(38):
        ret = 0.002 if i % 3 else -0.001
        equity *= 1 + ret
        rows.append(f"{day:%Y-%m-%d},{ret},{equity:.2f},0.0,{100000 * (1 + 0.0015) ** i:.2f},0.0005")
        day += timedelta(days=1 if day.weekday() < 4 else 3)
    (run / "artifacts").mkdir(parents=True, exist_ok=True)
    (run / "artifacts" / "equity.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (run / "code").mkdir(parents=True, exist_ok=True)
    (run / "code" / "signal_engine.py").write_text(
        "# E2E fixture strategy\ndef signals(prices):\n    return prices * 0 + 1\n", encoding="utf-8")
    (run / "artifacts" / "metrics.csv").write_text(
        "final_value,total_return,annual_return,sharpe,max_drawdown,trade_count\n"
        f"{equity:.2f},{equity / 100000 - 1:.4f},0.12,1.4,-0.02,1\n", encoding="utf-8")


def build_runtime(base: Path, key: str, project: Path, ollama_port: int) -> dict[str, str]:
    profile = base / "profile"
    home = profile / ".vibe-trading"
    tmp = base / "tmp"
    for folder in (home, tmp, home / "live", home / "auth"):
        folder.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("\n".join([
        "# E2E fixture runtime (tools/e2e/fixture_server.py)",
        f"API_AUTH_KEY={key}",
        "LANGCHAIN_PROVIDER=ollama",
        "LANGCHAIN_MODEL_NAME=gpt-oss:20b",
        f"OLLAMA_BASE_URL=http://127.0.0.1:{ollama_port}",
        "TOKEN_THRESHOLD=28000",
        "TIMEOUT_SECONDS=600",
        "VIBE_ORDER_APPROVAL=required",
        f"VIBE_TRADING_PLAYBOOK_DIR='{PLAYBOOKS}'",
        "",
    ]), encoding="utf-8")
    write_json(home / "agent.json", {"mcpServers": {
        "zt-dashboards": {
            "command": sys.executable,
            "args": [str(REPO / "extensions" / "zt_dashboards" / "server.py")],
            "env": {"INVESTMENT_AI_PROJECT_ROOT": str(project)},
            "enabledTools": ["daily_snapshot", "project_reports", "pit_inventory"],
        }}})
    lake = project / "implementation" / "pit_warehouse" / "lake"
    now = datetime.now(timezone.utc)
    if (lake / "fact_price_eod" / "data.parquet").is_file():
        passed = [f"A{i}" for i in range(1, 13)]
        write_json(home / "pit_audit_receipt.json", {
            "signature": signature(lake, PIT_TABLES), "audited_at_utc": now.isoformat(),
            "status": "PASS", "checks": len(passed), "checks_passed": passed})
        write_json(home / "pit_index_receipt.json", {
            "signature": signature(lake, PIT_TABLES[:4] + ("fact_price_eod", "fact_observation")),
            "refreshed_at_utc": now.isoformat(), "index_path": str(base / "pit.duckdb"), "rows": 1})
        (base / "pit.duckdb").write_bytes(b"")
    write_json(home / "src_sync_receipt.json", {"source_commit": git_head() or "unknown",
                                                "synced_at_utc": now.isoformat()})
    seed_run(home / "runs")
    env = {key_: value for key_, value in os.environ.items()
           if key_ not in ("API_AUTH_KEY", "VIBE_TRADING_API_KEY", "VIBE_TRADING_ENABLE_SCHEDULER")}
    env.update({
        "HOME": str(profile), "USERPROFILE": str(profile), "VIBE_TRADING_HOME": str(home),
        "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp),
        "INVESTMENT_AI_PROJECT_ROOT": str(project), "PITDB_INDEX": str(base / "pit.duckdb"),
        "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8911)
    parser.add_argument("--via", choices=("launch_api", "vt-zt"), default="launch_api")
    parser.add_argument("--keep", action="store_true", help="keep the temporary runtime")
    args = parser.parse_args()
    key = os.environ.get("VT_E2E_KEY", "").strip()
    if len(key) < 16:
        print("fixture_server: set VT_E2E_KEY (16+ characters)", file=sys.stderr)
        return 2
    if not (REPO / "frontend" / "dist" / "index.html").is_file():
        print("fixture_server: build the SPA first (cd frontend && npm run build)", file=sys.stderr)
        return 2

    base = Path(tempfile.mkdtemp(prefix="vt-e2e-"))
    source = os.environ.get("VT_E2E_PROJECT_ROOT", "").strip()
    if source:
        project = Path(source).resolve()
    else:
        project = base / "work" / "Investment-AI-Drive-Research"
        shutil.copytree(FIXTURE_PROJECT, project)
    ollama = ThreadingHTTPServer(("127.0.0.1", free_port()), FakeOllama)
    threading.Thread(target=ollama.serve_forever, daemon=True).start()
    env = build_runtime(base, key, project, ollama.server_address[1])

    serve = ["serve", "--host", "127.0.0.1", "--port", str(args.port)]
    if args.via == "vt-zt":
        launcher_dir = os.environ.get("VT_ZT_LAUNCHER_DIR", "").strip()
        if not launcher_dir:
            print("fixture_server: --via vt-zt needs VT_ZT_LAUNCHER_DIR", file=sys.stderr)
            return 2
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [launcher_dir, str(REPO / "agent"),
                                                          env.get("PYTHONPATH", "")]))
        env["VT_ZT_LAUNCH_API"] = str(LAUNCH_API)
        command = [sys.executable, "-m", "vt_zt_launcher", *serve]
    else:
        command = [sys.executable, str(LAUNCH_API), *serve]
    print(f"fixture_server: runtime {base} ({args.via}); project {project}", flush=True)
    child = subprocess.Popen(command, env=env, cwd=str(env["VIBE_TRADING_HOME"]))

    def forward(signum, _frame):
        # Only forward: the main thread's child.wait() returns once the server exits.
        try:
            child.send_signal(signum)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        return child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        ollama.shutdown()
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
