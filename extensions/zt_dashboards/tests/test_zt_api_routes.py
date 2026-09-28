"""Web UI routes: VT auth reused, whitelist enforced, traversal refused, SPA never shadows."""
import hashlib
import sys

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from conftest import load_extension_module

KEY = "zt-test-key-0123456789abcdef0123456789abcdef"
AUTH = {"Authorization": f"Bearer {KEY}"}
REPORT = "ALPHA_MONITOR_TOP25.html"


@pytest.fixture(scope="module")
def api_server():
    import api_server as module

    return module


@pytest.fixture(scope="module")
def routes(api_server):
    module = load_extension_module("zt_dashboards_api_routes", "api_routes.py")
    module.register(api_server.app)
    return module


def _set_key(monkeypatch, api_server, value):
    from src.config.accessor import reset_env_config

    if value:
        monkeypatch.setenv("API_AUTH_KEY", value)
    else:
        monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", value)
    reset_env_config()


@pytest.fixture()
def keyed(monkeypatch, api_server, routes, project):
    _set_key(monkeypatch, api_server, KEY)
    yield TestClient(api_server.app, client=("127.0.0.1", 50000))
    from src.config.accessor import reset_env_config

    reset_env_config()


@pytest.fixture()
def keyless(monkeypatch, api_server, routes, project):
    _set_key(monkeypatch, api_server, "")
    yield api_server.app
    from src.config.accessor import reset_env_config

    reset_env_config()


def test_json_routes_require_vt_auth(keyed):
    assert keyed.get("/zt/reports").status_code == 401
    assert keyed.get("/zt/snapshot/latest").status_code == 401
    assert keyed.get("/zt/reports", headers={"Authorization": "Bearer wrong"}).status_code == 401
    listed = keyed.get("/zt/reports", headers=AUTH)
    assert listed.status_code == 200
    body = listed.json()
    assert body["tool"] == "project_reports" and body["pit_label"] == "NON_PIT"
    assert REPORT in {r["id"] for r in body["data"]["reports"]}


def test_snapshot_route(keyed):
    response = keyed.get("/zt/snapshot/2026-09-27", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["source_path"] == "implementation/exports/2026-09-27"
    assert body["data"]["portfolio_context"]["reconciliation_status"] == "UNRECONCILED"
    assert "1234567.89" not in response.text
    assert keyed.get("/zt/snapshot/latest", headers=AUTH).json()["data"]["resolved_date"] == "2026-09-27"
    for bad in ("2026-99-01", "yesterday", "..%2F..%2Fsecret"):
        assert keyed.get(f"/zt/snapshot/{bad}", headers=AUTH).status_code in {400, 404}


def test_report_is_served_with_its_own_isolating_headers(keyed, project):
    source = (project / REPORT).read_bytes()
    assert keyed.get(f"/zt/reports/{REPORT}").status_code == 401
    response = keyed.get(f"/zt/reports/{REPORT}", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp and "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp and "connect-src 'none'" in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-zt-source-sha256"] == hashlib.sha256(source).hexdigest()
    assert b"ZT add-on: VT viewer shim" in response.content


def test_iframe_ticket_is_vt_single_use_ticket(keyed):
    minted = keyed.post("/auth/sse-ticket", headers=AUTH)
    assert minted.status_code == 200
    ticket = minted.json()["ticket"]
    assert keyed.get(f"/zt/reports/{REPORT}?ticket={ticket}").status_code == 200
    assert keyed.get(f"/zt/reports/{REPORT}?ticket={ticket}").status_code == 401
    assert keyed.get(f"/zt/reports/{REPORT}?ticket=forged").status_code == 401
    # The long-lived key is never accepted in a URL.
    assert keyed.get(f"/zt/reports/{REPORT}?api_key={KEY}").status_code == 401
    assert keyed.get(f"/zt/reports?ticket={ticket}").status_code == 401


@pytest.mark.parametrize("path", [
    "AGENTS.md",
    "implementation/secret.html",
    "..%2FAGENTS.md",
    "%2e%2e/AGENTS.md",
    "%2E%2E%2F%2E%2E%2Fetc%2Fpasswd",
    "..%5CAGENTS.md",
    "%2Fetc%2Fpasswd",
    "CATALYST_DASHBOARD.html",
    "alpha_monitor_top25.html",
])
def test_whitelist_and_traversal(keyed, project, path):
    (project / "AGENTS.md").write_text("secret project rules")
    (project / "implementation" / "secret.html").write_text("<p>secret project page</p>")
    response = keyed.get(f"/zt/reports/{path}", headers=AUTH)
    assert response.status_code == 404
    assert b"secret project" not in response.content
    page = keyed.get(f"/zt/reports/{path}", headers={**AUTH, "Accept": "text/html"})
    assert page.status_code == 404 and page.headers["content-type"].startswith("text/html")
    assert "sandbox" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "SAMEORIGIN"
    assert b"secret project" not in page.content


def test_loopback_dev_mode_matches_vt(keyless):
    local = TestClient(keyless, client=("127.0.0.1", 50000))
    remote = TestClient(keyless, client=("203.0.113.10", 50000))
    assert local.get("/zt/reports").status_code == 200
    assert local.get(f"/zt/reports/{REPORT}").status_code == 200
    assert remote.get("/zt/reports").status_code == 403
    assert remote.get(f"/zt/reports/{REPORT}").status_code == 403
    assert remote.get("/zt/snapshot/latest").status_code == 403


def test_unconfigured_project_root_is_503(keyed, monkeypatch):
    monkeypatch.delenv("INVESTMENT_AI_PROJECT_ROOT")
    response = keyed.get("/zt/reports", headers=AUTH)
    assert response.status_code == 503
    assert "INVESTMENT_AI_PROJECT_ROOT" in response.json()["detail"]
    assert keyed.get(f"/zt/reports/{REPORT}", headers=AUTH).status_code == 503


def test_routes_are_placed_before_a_catch_all_mount(routes, project, tmp_path, monkeypatch):
    from src.config.accessor import reset_env_config

    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    reset_env_config()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>")
    app = FastAPI()
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    added = routes.register(app)
    assert {route.path for route in added} == set(routes.ROUTE_PATHS)
    paths = [getattr(route, "path", None) for route in app.router.routes]
    assert paths.index("/zt/reports") < paths.index("")
    assert routes.register(app) == []
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/zt/reports").json()["tool"] == "project_reports"
    assert client.get("/").text == "<html>spa</html>"


def test_launcher_runs_vt_serve_with_routes_ahead_of_the_spa(
        api_server, routes, keyed, project, tmp_path, monkeypatch):
    import uvicorn

    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>vt spa</html>")
    monkeypatch.setattr(api_server, "__file__", str(tmp_path / "agent" / "api_server.py"))
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))
    monkeypatch.setattr(sys, "path", list(sys.path))
    launcher = load_extension_module("zt_dashboards_launch_api", "launch_api.py")
    before = list(api_server.app.router.routes)
    try:
        assert launcher.main(["serve", "--host", "127.0.0.1", "--port", "8899"]) == 0
        assert captured["app"] is api_server.app
        assert (captured["host"], captured["port"]) == ("127.0.0.1", 8899)
        assert sys.modules["api_server"] is api_server
        paths = [getattr(route, "path", None) for route in api_server.app.router.routes]
        spa = next(i for i, route in enumerate(api_server.app.router.routes)
                   if route not in before)
        assert paths.index("/zt/reports") < spa
        assert keyed.get("/zt", headers={"Accept": "text/html"}).text == "<html>vt spa</html>"
        assert keyed.get("/zt/reports", headers=AUTH).json()["tool"] == "project_reports"
        report = keyed.get(f"/zt/reports/{REPORT}", headers=AUTH)
        assert report.status_code == 200 and b"viewer shim" in report.content
    finally:
        api_server.app.router.routes[:] = before
