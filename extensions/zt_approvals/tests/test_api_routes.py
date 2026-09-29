"""Web UI routes on VT's real api_server app: VT auth, approve/reject semantics,
410/409/422/423, the paper account and reset, and placement ahead of the SPA."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from conftest import load_extension_module

KEY = "zt-approvals-test-key-0123456789abcdef0123"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture(scope="module")
def api_server():
    import api_server as module

    return module


@pytest.fixture(scope="module")
def routes(api_server):
    module = load_extension_module("zt_approvals_api_routes", "api_routes.py")
    module.register(api_server.app)
    return module


@pytest.fixture()
def client(monkeypatch, api_server, routes, home):
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("API_AUTH_KEY", KEY)
    monkeypatch.setattr(api_server, "_API_KEY", KEY)
    reset_env_config()
    yield TestClient(api_server.app, client=("127.0.0.1", 50000))
    reset_env_config()


def _propose(core, **kwargs):
    kwargs.setdefault("orders", [{"symbol": "AAPL", "side": "buy", "qty": 10}])
    kwargs.setdefault("signals", [{"source": "analyst", "symbol": "AAPL", "direction": "long",
                                   "evidence_ids": ["E-1"]}])
    return core.create_proposal(broker="zt-paper", rationale="test", evidence_ids=["E-1"],
                                origin={"kind": "test", "actor": "agent"}, **kwargs)


def test_every_route_requires_vt_auth(client, core):
    proposal = _propose(core)
    pid = proposal["id"]
    for method, path, body in (("get", "/zt/orders/proposals", None), ("get", f"/zt/orders/proposals/{pid}", None),
                               ("post", f"/zt/orders/proposals/{pid}/approve", {"confirm_hash": proposal["content_hash"]}),
                               ("post", f"/zt/orders/proposals/{pid}/reject", {"reason": "no"}),
                               ("get", "/zt/paper/account", None), ("post", "/zt/paper/reset", {"confirm": "RESET"})):
        call = getattr(client, method)
        kwargs = {"json": body} if body is not None else {}
        assert call(path, **kwargs).status_code == 401, path
        assert call(path, headers={"Authorization": "Bearer wrong"}, **kwargs).status_code == 401, path
    assert core.load_proposal(pid)["status"] == core.PENDING
    assert not core.zt_paper_engine().account_path().exists()


def test_list_and_detail(client, core):
    proposal = _propose(core)
    listed = client.get("/zt/orders/proposals", headers=AUTH)
    assert listed.status_code == 200
    body = listed.json()
    assert body["approval_mode"] == "off" and body["ttl_minutes"] == 15.0 and body["ledger"]["ok"] is True
    assert [row["id"] for row in body["proposals"]] == [proposal["id"]]
    assert body["proposals"][0]["approvable"] is True and "content_hash" not in body["proposals"][0]
    assert client.get("/zt/orders/proposals?status=bogus", headers=AUTH).status_code == 400

    detail = client.get(f"/zt/orders/proposals/{proposal['id']}", headers=AUTH).json()
    assert detail["content_hash"] == proposal["content_hash"]
    assert detail["integrity"]["ok"] is True
    assert [e["to"] for e in detail["ledger_events"]] == ["PENDING"]
    assert detail["route"] == {"kind": "zt_paper", "target": "ZT-PAPER"}
    assert client.get("/zt/orders/proposals/op_" + "0" * 32, headers=AUTH).status_code == 404
    assert client.get("/zt/orders/proposals/..%2F..%2Fsecret", headers=AUTH).status_code == 404


def test_approve_fills_once_and_records_the_approver(client, core):
    proposal = _propose(core)
    url = f"/zt/orders/proposals/{proposal['id']}/approve"

    done = client.post(url, headers=AUTH, json={"confirm_hash": proposal["content_hash"]})

    assert done.status_code == 200, done.text
    assert done.json()["status"] == "FILLED"
    again = client.post(url, headers=AUTH, json={"confirm_hash": proposal["content_hash"]})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_pending"
    approved = next(e for e in core.ledger_events(proposal["id"]) if e["to"] == "APPROVED")
    assert approved["actor"] == "local-user"
    assert approved["detail"]["principal"]["subject"] == "shared-key-holder"
    account = client.get("/zt/paper/account", headers=AUTH).json()
    assert account["cash"] == 98_000.0 and account["positions"][0]["symbol"] == "AAPL"


def test_wrong_confirmation_hash_is_409(client, core):
    proposal = _propose(core)
    wrong = client.post(f"/zt/orders/proposals/{proposal['id']}/approve", headers=AUTH,
                        json={"confirm_hash": "sha256:" + "1" * 64})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "confirm_hash_mismatch"


def test_expired_is_410(client, core, monkeypatch):
    proposal = _propose(core)
    later = core._parse_iso(proposal["expires_utc"]) + timedelta(seconds=1)
    monkeypatch.setattr(core, "_now", lambda: later)
    response = client.post(f"/zt/orders/proposals/{proposal['id']}/approve", headers=AUTH,
                           json={"confirm_hash": proposal["content_hash"]})
    assert response.status_code == 410 and response.json()["detail"]["code"] == "expired"
    assert response.json()["detail"]["proposal"]["status"] == "EXPIRED"


def test_tampered_file_is_409(client, core):
    proposal = _propose(core)
    path = core.proposal_path(proposal["id"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orders"][0]["qty"] = 499.0
    path.write_text(json.dumps(data), encoding="utf-8")
    response = client.post(f"/zt/orders/proposals/{proposal['id']}/approve", headers=AUTH,
                           json={"confirm_hash": proposal["content_hash"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "tampered"
    detail = client.get(f"/zt/orders/proposals/{proposal['id']}", headers=AUTH).json()
    assert detail["integrity"]["ok"] is False
    assert not core.zt_paper_engine().account_path().exists()


def test_failed_validation_is_422(client, core):
    proposal = _propose(core, orders=[{"symbol": "NVDA", "side": "sell", "qty": 5}],
                        signals=[{"source": "analyst", "symbol": "NVDA", "direction": "short", "evidence_ids": ["E"]}])
    response = client.post(f"/zt/orders/proposals/{proposal['id']}/approve", headers=AUTH,
                           json={"confirm_hash": proposal["content_hash"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "validation_failed" and "direction_permission" in detail["message"]
    assert detail["proposal"]["status"] == "PENDING"


def test_halt_is_423_for_live_brokers(client, core, monkeypatch):
    from src.live.halt import trip_halt

    proposal = _propose(core)
    data = json.loads(core.proposal_path(proposal["id"]).read_text(encoding="utf-8"))
    assert data["broker"]["live"] is False
    trip_halt(by="file", reason="test")  # a SIMULATED account is not stopped by HALT
    response = client.post(f"/zt/orders/proposals/{proposal['id']}/approve", headers=AUTH,
                           json={"confirm_hash": proposal["content_hash"]})
    assert response.status_code == 200 and response.json()["status"] == "FILLED"

    live = dict(data, id=core.new_proposal_id(), broker={**data["broker"], "live": True, "key": "robinhood"},
                route={"kind": "mcp_guard"})
    live["content_hash"] = core.content_hash(live)
    live["transitions"] = []
    core._record_transition(live, "PENDING", actor="agent", reason="proposed", event="created")
    core.write_json_atomic(core.proposal_path(live["id"]), live)
    halted = client.post(f"/zt/orders/proposals/{live['id']}/approve", headers=AUTH,
                         json={"confirm_hash": live["content_hash"]})
    assert halted.status_code == 423 and halted.json()["detail"]["code"] == "halted"


def test_reject_needs_a_reason(client, core):
    proposal = _propose(core)
    url = f"/zt/orders/proposals/{proposal['id']}/reject"
    assert client.post(url, headers=AUTH, json={"reason": ""}).status_code == 422
    assert client.post(url, headers=AUTH, json={}).status_code == 422
    done = client.post(url, headers=AUTH, json={"reason": "not convinced"})
    assert done.status_code == 200 and done.json()["status"] == "REJECTED"
    assert client.post(url, headers=AUTH, json={"reason": "again"}).status_code == 409


def test_cross_site_post_is_refused(client, core):
    proposal = _propose(core)
    response = client.post(f"/zt/orders/proposals/{proposal['id']}/approve",
                           headers={**AUTH, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                           json={"confirm_hash": proposal["content_hash"]})
    assert response.status_code == 403
    assert core.load_proposal(proposal["id"])["status"] == core.PENDING


def test_paper_reset_needs_explicit_confirmation(client, core):
    pending = _propose(core)
    assert client.post("/zt/paper/reset", headers=AUTH, json={"confirm": "yes"}).status_code == 422
    assert client.post("/zt/paper/reset", headers=AUTH, json={"confirm": "RESET", "starting_cash": -5}).status_code == 422
    done = client.post("/zt/paper/reset", headers=AUTH, json={"confirm": "RESET", "starting_cash": 25_000})
    assert done.status_code == 200
    body = done.json()
    assert body["account"]["cash"] == 25_000.0 and body["account"]["label"] == "SIMULATED"
    assert body["rejected_proposals"] == [pending["id"]]


def test_routes_sit_ahead_of_the_spa_mount_and_register_once(api_server, routes):
    app = api_server.app
    assert routes.register(app) == []
    paths = [getattr(route, "path", None) for route in app.router.routes]
    for path in routes.ROUTE_PATHS:
        assert paths.count(path) == 1
    mounts = [i for i, route in enumerate(app.router.routes)
              if isinstance(route, Mount) and getattr(route, "path", None) in ("", "/")]
    if mounts:
        assert max(paths.index(p) for p in routes.ROUTE_PATHS) < mounts[0]
