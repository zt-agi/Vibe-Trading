"""Pre-flight checks: each OK / WARN / FAIL branch, the calendar, and the route.

Checks run against a throwaway VIBE_TRADING_HOME and a synthetic price lake;
Ollama is a fake ``http_get``. Nothing reaches the network.
"""
import json
import os
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import load_extension_module

KEY = "zt-test-key-0123456789abcdef0123456789abcdef"
AUTH = {"Authorization": f"Bearer {KEY}"}
# Tuesday 2026-09-29 15:00 UTC = 11:00 New York time.
NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
TABLES = ("dim_source", "dim_security", "dim_security_alias", "dim_series", "fact_price_eod",
          "fact_observation")


@pytest.fixture()
def pf():
    return load_extension_module("zt_dashboards_preflight", "zt_preflight.py")


def write_prices(lake: Path, days) -> Path:
    import duckdb

    path = lake / "fact_price_eod" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ", ".join(f"(DATE '{d}', {i})" for i, d in enumerate(days))
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT * FROM (VALUES {values}) AS t(event_date, sec_id)) "
                    f"TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return path


def signature(lake: Path, tables=TABLES) -> dict:
    files = {}
    for table in tables:
        try:
            stat = (lake / table / "data.parquet").stat()
            files[table] = [stat.st_size, stat.st_mtime_ns]
        except FileNotFoundError:
            files[table] = None
    return {"lake_root": str(lake.resolve()), "files": files}


@pytest.fixture()
def ctx(pf, tmp_path):
    runtime = tmp_path / "home"
    runtime.mkdir()
    project = tmp_path / "work" / "Investment-AI-Drive-Research"
    lake = project / "implementation" / "pit_warehouse" / "lake"
    write_prices(lake, ["2026-09-25", "2026-09-28"])
    settings = {"provider": "ollama", "model": "gpt-oss:20b", "token_threshold": 24000,
                "timeout_seconds": 600, "scheduler_enabled": False, "playbook_dir": "",
                "source": "test"}
    return pf.Context(now=NOW, environ={}, runtime_root=runtime, project_root=project,
                      vt_root=tmp_path, settings=settings, dotenv={}, dotenv_path=None,
                      windows=False)


def fake_ollama(models=("gpt-oss:20b", "qwen3:14b"), context=32768, fail=False, calls=None):
    def get(url, timeout):
        if calls is not None:
            calls.append((url, timeout))
        if fail:
            raise OSError("connection refused")
        if url.endswith("/api/version"):
            return {"version": "0.12.3"}
        if url.endswith("/api/tags"):
            return {"models": [{"name": name} for name in models]}
        if url.endswith("/api/ps"):
            return {"models": [{"name": models[0], "context_length": context}]} if context else {"models": []}
        raise AssertionError(url)
    return get


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_holiday_rules_match_vt_static_calendar(pf):
    from src.live.runtime.triggers import _US_EQUITY_HOLIDAYS

    for year in (2026, 2027):
        assert pf.us_market_holidays(year) == {d for d in _US_EQUITY_HOLIDAYS if d.year == year}
    assert date(2022, 12, 26) in pf.us_market_holidays(2022)      # Christmas on a Sunday
    assert date(2021, 12, 31) not in pf.us_market_holidays(2021)  # New Year on a Saturday: no Friday close
    assert date(2025, 1, 9) in pf.us_market_holidays(2025)        # national day of mourning


@pytest.mark.parametrize("now, closed, due", [
    # Tuesday 11:00 ET: Monday closed and is due (after 09:00 ET).
    (datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc), "2026-09-28", "2026-09-28"),
    # Tuesday 08:00 ET: Monday closed but the daily run window is not over.
    (datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc), "2026-09-28", "2026-09-25"),
    # Monday 17:00 ET: Monday closed today, due tomorrow.
    (datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc), "2026-09-28", "2026-09-25"),
    # Sunday: Friday's session is due since Saturday 09:00 ET.
    (datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc), "2026-09-25", "2026-09-25"),
    # Tuesday after Labor Day, 10:00 ET: Friday 2026-09-04 is the last session before the holiday.
    (datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc), "2026-09-04", "2026-09-04"),
])
def test_last_closed_and_due_sessions(pf, now, closed, due):
    assert pf.last_closed_session(now).isoformat() == closed
    assert pf.due_session(now).isoformat() == due


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def test_llm_provider(pf, ctx):
    assert pf.check_llm_provider(ctx).status == "OK"
    ctx.settings["model"] = ""
    missing = pf.check_llm_provider(ctx)
    assert missing.status == "FAIL" and "set_llm_default.ps1" in missing.fix
    ctx.settings.update(provider="openai", model="gpt-4o")
    paid = pf.check_llm_provider(ctx)
    assert paid.status == "FAIL" and "subscription" in paid.summary
    ctx.settings.update(provider="openai_codex", model="openai-codex/gpt-5.4")
    assert pf.check_llm_provider(ctx).status == "OK"
    ctx.dotenv = {"LANGCHAIN_PROVIDER": "ollama"}
    pending = pf.check_llm_provider(ctx)
    assert pending.status == "WARN" and "Restart VT" in pending.fix


def test_ollama_active_and_ready(pf, ctx):
    calls = []
    ctx.http_get = fake_ollama(calls=calls)
    check = pf.check_ollama(ctx)
    assert check.status == "OK", check.summary
    assert "32768-token context" in check.summary
    assert {url for url, _ in calls} == {"http://localhost:11434/api/version",
                                         "http://localhost:11434/api/tags",
                                         "http://localhost:11434/api/ps"}
    assert all(timeout <= 2 for _, timeout in calls)


def test_ollama_base_url_is_normalized(pf, ctx):
    calls = []
    ctx.environ = {"OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1/"}
    ctx.http_get = fake_ollama(calls=calls)
    assert pf.check_ollama(ctx).status == "OK"
    assert calls[0][0] == "http://127.0.0.1:11434/api/version"


@pytest.mark.parametrize("kwargs, threshold, status, needle", [
    ({"models": ("qwen3:14b",)}, 24000, "FAIL", "not pulled"),
    ({"fail": True}, 24000, "FAIL", "Not reachable"),
    ({"context": 4096}, 24000, "WARN", "4096-token context"),
    ({}, 40000, "WARN", "TOKEN_THRESHOLD=40000"),
    ({"context": None}, 40000, "WARN", "32768-token context"),
])
def test_ollama_problems(pf, ctx, kwargs, threshold, status, needle):
    ctx.http_get = fake_ollama(**kwargs)
    ctx.settings["token_threshold"] = threshold
    check = pf.check_ollama(ctx)
    assert check.status == status and needle in check.summary
    assert check.fix


def test_ollama_as_fallback_and_non_loopback(pf, ctx):
    ctx.settings.update(provider="openai-codex", model="openai-codex/gpt-5.4")
    ctx.http_get = fake_ollama(fail=True)
    assert pf.check_ollama(ctx).status == "OK"
    ctx.settings.update(provider="ollama", model="gpt-oss:20b")
    ctx.environ = {"OLLAMA_BASE_URL": "http://192.0.2.10:11434"}
    ctx.http_get = fake_ollama(calls=(calls := []))
    remote = pf.check_ollama(ctx)
    assert remote.status == "WARN" and "not probed" in remote.summary and calls == []


def test_openai_codex_login_never_leaks_the_token(pf, ctx):
    token = {"access": "ACCESS-SECRET-123", "refresh": "REFRESH-SECRET-456",
             "expires": 1790000000000, "account_id": "acct-SECRET"}
    auth = ctx.runtime_root / "auth"
    auth.mkdir()
    (auth / "openai-codex.json").write_text(json.dumps(token))
    check = pf.check_openai_codex(ctx)
    assert check.status == "OK"
    dumped = json.dumps(check.as_dict())
    assert "SECRET" not in dumped
    (auth / "openai-codex.json").unlink()
    ctx.settings.update(provider="openai-codex", model="openai-codex/gpt-5.4")
    missing = pf.check_openai_codex(ctx)
    assert missing.status == "FAIL" and "provider login openai-codex" in missing.fix
    ctx.settings.update(provider="ollama")
    assert pf.check_openai_codex(ctx).status == "OK"


# ---------------------------------------------------------------------------
# PIT
# ---------------------------------------------------------------------------


def write_receipt(ctx, name, **payload):
    (ctx.runtime_root / name).write_text(json.dumps(payload))


def test_audit_receipt(pf, ctx):
    lake = pf.lake_root(ctx)
    missing = pf.check_audit_receipt(ctx)
    assert missing.status == "WARN" and "--refresh-audit" in missing.fix
    fresh = (NOW - timedelta(minutes=20)).isoformat()
    write_receipt(ctx, "pit_audit_receipt.json", status="PASS", audited_at_utc=fresh, checks=12,
                  checks_passed=["A1", "A11"], signature=signature(lake))
    ok = pf.check_audit_receipt(ctx)
    assert ok.status == "OK" and "bound to the current lake" in ok.summary
    old = (NOW - timedelta(hours=5)).isoformat()
    write_receipt(ctx, "pit_audit_receipt.json", status="PASS", audited_at_utc=old, checks=12,
                  signature=signature(lake))
    assert "expired for simulations" in pf.check_audit_receipt(ctx).summary
    write_prices(lake, ["2026-09-25", "2026-09-28", "2026-09-29"])
    changed = pf.check_audit_receipt(ctx)
    assert changed.status == "WARN" and "fact_price_eod" in changed.summary
    write_receipt(ctx, "pit_audit_receipt.json", status="FAIL", audited_at_utc=fresh,
                  signature=signature(lake))
    assert pf.check_audit_receipt(ctx).status == "FAIL"


def test_index_receipt(pf, ctx, tmp_path):
    lake = pf.lake_root(ctx)
    index = tmp_path / "pit.duckdb"
    index.write_bytes(b"")
    ctx.environ = {"PITDB_INDEX": str(index)}
    assert pf.check_index_receipt(ctx).status == "WARN"
    write_receipt(ctx, "pit_index_receipt.json", refreshed_at_utc=(NOW - timedelta(hours=2)).isoformat(),
                  rows=1234, signature=signature(lake))
    ok = pf.check_index_receipt(ctx)
    assert ok.status == "OK" and "1234 rows" in ok.summary
    index.unlink()
    assert "is missing" in pf.check_index_receipt(ctx).summary
    index.write_bytes(b"")
    write_receipt(ctx, "pit_index_receipt.json", refreshed_at_utc=(NOW - timedelta(days=3)).isoformat(),
                  rows=1234, signature=signature(lake))
    assert "rebuilt 72 h ago" in pf.check_index_receipt(ctx).summary


@pytest.mark.parametrize("days, status, needle", [
    (["2026-09-25", "2026-09-28"], "OK", "Latest price 2026-09-28"),
    (["2026-09-24", "2026-09-25"], "WARN", "1 session(s) behind"),
    (["2026-09-21", "2026-09-22"], "FAIL", "4 session(s) behind"),
])
def test_price_lake_against_the_due_session(pf, ctx, days, status, needle):
    write_prices(pf.lake_root(ctx), days)
    check = pf.check_price_lake(ctx)
    assert check.status == status and needle in check.summary
    assert check.detail["due_session"] == "2026-09-28"


def test_price_lake_missing(pf, ctx):
    (pf.lake_root(ctx) / "fact_price_eod" / "data.parquet").unlink()
    assert pf.check_price_lake(ctx).status == "FAIL"
    ctx.project_root = None
    assert "INVESTMENT_AI_PROJECT_ROOT" in pf.check_price_lake(ctx).summary


# ---------------------------------------------------------------------------
# Scheduler, HALT, approval
# ---------------------------------------------------------------------------


def test_scheduler(pf, ctx):
    assert pf.check_scheduler(ctx).status == "OK"
    store = ctx.runtime_root / "scheduled_research" / "scheduled_research_jobs.json"
    store.parent.mkdir()
    store.write_text(json.dumps({"schema_version": 1, "jobs": [
        {"id": "a", "title": "ZT pre-market brief", "status": "pending", "consecutive_failures": 0},
        {"id": "b", "title": "old", "status": "cancelled"}]}))
    off = pf.check_scheduler(ctx)
    assert off.status == "WARN" and "1 active job(s) will not fire" in off.summary
    ctx.settings["scheduler_enabled"] = True
    assert pf.check_scheduler(ctx).status == "OK"
    store.write_text("{not json")
    assert pf.check_scheduler(ctx).status == "WARN"


def test_halt(pf, ctx):
    assert pf.check_halt(ctx).status == "OK"
    live = ctx.runtime_root / "live"
    live.mkdir()
    (live / "HALT").write_text(json.dumps({"tripped_at": "2026-09-29T14:00:00+00:00", "by": "cli",
                                           "reason": "end of test"}))
    check = pf.check_halt(ctx)
    assert check.status == "WARN" and "all brokers" in check.summary
    assert check.detail["reason"] == "end of test"
    (live / "HALT").unlink()
    (live / "ibkr").mkdir()
    (live / "ibkr" / "HALT").write_text("")
    assert "ibkr" in pf.check_halt(ctx).summary


def test_order_approval(pf, ctx):
    assert pf.check_order_approval(ctx).status == "WARN"
    ctx.environ = {"VIBE_ORDER_APPROVAL": "required"}
    assert pf.check_order_approval(ctx).status == "OK"
    ctx.dotenv = {"VIBE_ORDER_APPROVAL": "off"}
    assert pf.check_order_approval(ctx).status == "WARN"
    ctx.environ = {"VIBE_ORDER_APPROVAL": "off"}
    assert pf.check_order_approval(ctx).status == "FAIL"


# ---------------------------------------------------------------------------
# MCP, playbooks, disk, version, extension routes
# ---------------------------------------------------------------------------


def test_mcp_servers(pf, ctx, tmp_path):
    assert pf.check_mcp_servers(ctx).status == "WARN"
    script = tmp_path / "server.py"
    script.write_text("")
    config = {"mcpServers": {
        "zt-dashboards": {"command": os.sys.executable, "args": [str(script)],
                          "enabledTools": ["daily_snapshot", "project_reports"]},
        "pit-actor-sim": {"command": os.sys.executable, "args": [str(script)]},
    }}
    (ctx.runtime_root / "agent.json").write_text(json.dumps(config))
    ok = pf.check_mcp_servers(ctx)
    assert ok.status == "OK"
    assert "zt-dashboards (2)" in ok.summary and "pit-actor-sim (all)" in ok.summary
    config["mcpServers"]["broken"] = {"command": str(tmp_path / "missing.exe")}
    (ctx.runtime_root / "agent.json").write_text(json.dumps(config))
    assert "missing for broken" in pf.check_mcp_servers(ctx).summary
    (ctx.runtime_root / "agent.json").write_text("{ nope")
    assert pf.check_mcp_servers(ctx).status == "FAIL"


def test_playbooks(pf, ctx, tmp_path):
    from conftest import EXTENSION

    assert pf.check_playbooks(ctx).status == "WARN"
    ctx.settings["playbook_dir"] = str(EXTENSION / "playbooks")
    ok = pf.check_playbooks(ctx)
    assert ok.status == "OK" and "3 playbook(s)" in ok.summary
    assert set(ok.detail["loadable"]) == {"alpha-monitor-brief", "asm-state", "pit-health"}
    ctx.settings["playbook_dir"] = str(tmp_path / "absent")
    assert pf.check_playbooks(ctx).status == "FAIL"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "bad.md").write_text("no front matter at all")
    ctx.settings["playbook_dir"] = str(broken)
    assert pf.check_playbooks(ctx).status == "FAIL"


def test_disk(pf, ctx, monkeypatch):
    usage = types.SimpleNamespace(total=500 * 1024 ** 3, used=0, free=100 * 1024 ** 3)
    monkeypatch.setattr(pf, "shutil", types.SimpleNamespace(disk_usage=lambda _: usage))
    assert pf.check_disk(ctx).status == "OK"
    usage.free = 10 * 1024 ** 3
    assert pf.check_disk(ctx).status == "WARN"
    usage.free = 2 * 1024 ** 3
    assert pf.check_disk(ctx).status == "FAIL"
    ctx.windows, ctx.runtime_root = True, Path("C:\\vt\\home")
    refused = pf.check_disk(ctx)
    assert refused.status == "FAIL" and "nothing on C: or D:" in refused.summary


def test_version(pf, ctx, monkeypatch):
    monkeypatch.setattr(pf, "REPO_ROOT", ctx.vt_root / "src")
    missing = pf.check_version(ctx)
    assert missing.status == "WARN" and "sync_src.ps1" in missing.fix
    (ctx.vt_root / "src_sync_receipt.json").write_text(json.dumps(
        {"source_commit": "d71eeac9f00d1234567890", "synced_at_utc": "2026-09-29T04:00:00Z"}))
    ok = pf.check_version(ctx)
    assert ok.status == "OK" and "d71eeac9f00d" in ok.summary and "2026-09-29T04:00:00Z" in ok.summary


def test_extension_routes(pf, ctx):
    assert pf.check_extension_routes(ctx).status == "WARN"
    ctx.app_state = types.SimpleNamespace(vt_extension_routes=[
        {"name": "zt_dashboards", "status": "registered", "routes": ["/zt/reports"] * 4},
        {"name": "zt_approvals", "status": "failed", "reason": "ImportError: x"}])
    failed = pf.check_extension_routes(ctx)
    assert failed.status == "FAIL" and "zt_approvals" in failed.summary
    ctx.app_state.vt_extension_routes.pop()
    assert pf.check_extension_routes(ctx).summary == "Registered: zt_dashboards (4)."


def test_run_preflight_isolates_a_crashing_check(pf, ctx, monkeypatch):
    def explode(_ctx):
        raise RuntimeError("boom")

    ctx.http_get = fake_ollama()
    monkeypatch.setattr(pf, "CHECKS", (pf.check_llm_provider, explode, pf.check_halt))
    result = pf.run_preflight(ctx)
    assert [c["status"] for c in result["checks"]] == ["OK", "FAIL", "OK"]
    assert "RuntimeError: boom" in result["checks"][1]["summary"]
    assert result["overall"] == "FAIL" and result["counts"] == {"OK": 2, "WARN": 0, "FAIL": 1}
    assert result["checks"][0]["fix"] == ""


def test_dotenv_parser(pf):
    text = ("# comment\nexport API_AUTH_KEY='abc def'\nLANGCHAIN_PROVIDER=ollama  # inline\n"
            'VIBE_TRADING_PLAYBOOK_DIR="G:\\\\x"\nBROKEN\n=novalue\n')
    assert pf.parse_dotenv(text) == {"API_AUTH_KEY": "abc def", "LANGCHAIN_PROVIDER": "ollama",
                                     "VIBE_TRADING_PLAYBOOK_DIR": "G:\\\\x"}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def test_preflight_route_requires_auth_and_never_returns_the_key(monkeypatch, project):
    from fastapi.testclient import TestClient

    import api_server
    from src.config.accessor import reset_env_config

    routes = load_extension_module("zt_dashboards_api_routes", "api_routes.py")
    routes.register(api_server.app)
    monkeypatch.setenv("API_AUTH_KEY", KEY)
    monkeypatch.setattr(api_server, "_API_KEY", KEY)
    reset_env_config()
    try:
        client = TestClient(api_server.app, client=("127.0.0.1", 50000))
        assert client.get("/zt/preflight").status_code == 401
        response = client.get("/zt/preflight", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["tool"] == "zt_preflight" and body["overall"] in {"OK", "WARN", "FAIL"}
        ids = [c["id"] for c in body["checks"]]
        assert ids == ["llm.provider", "llm.ollama", "llm.openai_codex", "pit.audit_receipt",
                       "pit.index_receipt", "pit.price_lake", "scheduler", "live.halt",
                       "orders.approval", "mcp.servers", "playbooks", "disk.runtime", "vt.version",
                       "routes.extensions"]
        assert KEY not in response.text
        assert all(c["status"] in {"OK", "WARN", "FAIL"} for c in body["checks"])
    finally:
        reset_env_config()
