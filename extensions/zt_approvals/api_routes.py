"""ZT add-on: Web UI routes for order approvals and the zt-paper account.

``register(app)`` adds these routes to VT's FastAPI app, each behind VT's own
``require_auth`` (bearer API key; loopback dev mode when no key is set)::

    GET  /zt/orders/proposals                   list (?status=PENDING&limit=50)
    GET  /zt/orders/proposals/{id}              one proposal + its ledger trail
    POST /zt/orders/proposals/{id}/approve      {"confirm_hash": "sha256:..."}
    POST /zt/orders/proposals/{id}/reject       {"reason": "..."}
    GET  /zt/paper/account                      the SIMULATED ZT-PAPER account
    POST /zt/paper/reset                        {"confirm": "RESET", "starting_cash": 100000}

Approving is the human step of ``src/live/order_proposals.py``: it re-checks
expiry (410), the content hash against the file and the ledger (409), the hash
the approver confirmed (409), HALT for live brokers (423) and a fresh
validation (422), then submits the approved orders exactly once. No agent tool
reaches these routes. The routes are placed ahead of any catch-all "/" mount.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.routing import Mount

_AGENT = Path(__file__).resolve().parents[2] / "agent"

ROUTE_PATHS = (
    "/zt/orders/proposals",
    "/zt/orders/proposals/{proposal_id}",
    "/zt/orders/proposals/{proposal_id}/approve",
    "/zt/orders/proposals/{proposal_id}/reject",
    "/zt/paper/account",
    "/zt/paper/reset",
)


def _core():
    if str(_AGENT) not in sys.path:
        sys.path.append(str(_AGENT))
    from src.live import order_proposals

    return order_proposals


class ApproveBody(BaseModel):
    """The approver confirms the exact content hash they reviewed."""

    confirm_hash: str = Field(..., min_length=8, max_length=80)


class RejectBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ResetBody(BaseModel):
    confirm: str = Field(..., description='Must be "RESET"')
    starting_cash: Optional[float] = Field(None, gt=0)
    max_leverage: Optional[float] = Field(None, gt=0)


def _error(exc: Exception) -> HTTPException:
    core = _core()
    if isinstance(exc, core.ProposalError):
        return HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    return HTTPException(status_code=422, detail={"code": "invalid", "message": str(exc)})


def _principal(principal: Any) -> dict[str, Any]:
    method = getattr(principal, "auth_method", None)
    return {"subject": getattr(principal, "subject", None),
            "auth_method": getattr(method, "value", method),
            "attributable": getattr(principal, "attributable", None)}


def list_order_proposals(status: Optional[str] = Query(None, max_length=16),
                         limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Proposals newest first, plus the approval mode, TTL, limits and ledger health."""
    core = _core()
    if status and status.strip().upper() not in core.STATUSES:
        raise HTTPException(status_code=400, detail={"code": "invalid", "message": f"unknown status {status!r}"})
    policy = core.load_policy()
    return {
        "approval_mode": core.approval_mode(),
        "ttl_minutes": core.ttl_minutes(),
        "server_time_utc": core._iso(core._now()),
        "policy": {k: policy.get(k) for k in (*core._POSITIVE_POLICY_KEYS, "short_capable_sources", "source", "error")},
        "ledger": core.verify_ledger(),
        "proposals": [core.summary(p) for p in core.list_proposals(status=status, limit=limit)],
    }


def get_order_proposal(proposal_id: str) -> dict[str, Any]:
    """One proposal with its integrity status and ledger trail."""
    core = _core()
    try:
        proposal = core.expire_if_due(proposal_id)
    except core.ProposalError as exc:
        raise _error(exc) from exc
    view = core.public_view(proposal)
    view["integrity"] = core.integrity(proposal)
    view["ledger_events"] = [
        {k: event.get(k) for k in ("seq", "at_utc", "event", "from", "to", "actor", "reason", "record_hash")}
        for event in core.ledger_events(proposal_id)
    ]
    return view


def approve_order_proposal(proposal_id: str, body: ApproveBody, principal: Any) -> dict[str, Any]:
    core = _core()
    try:
        proposal = core.approve_proposal(proposal_id, confirm_hash=body.confirm_hash, actor=core.APPROVER,
                                         principal=_principal(principal))
    except core.ProposalError as exc:
        raise _error(exc) from exc
    return core.public_view(proposal)


def reject_order_proposal(proposal_id: str, body: RejectBody, principal: Any) -> dict[str, Any]:
    core = _core()
    try:
        proposal = core.reject_proposal(proposal_id, reason=body.reason, actor=core.APPROVER,
                                        principal=_principal(principal))
    except core.ProposalError as exc:
        raise _error(exc) from exc
    return core.public_view(proposal)


def paper_account() -> dict[str, Any]:
    """The SIMULATED ZT-PAPER account marked at completed-session closes."""
    core = _core()
    try:
        engine = core.zt_paper_engine()
        return engine.snapshot()
    except core.ProposalError as exc:
        raise _error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - e.g. an unreadable account file
        raise HTTPException(status_code=409, detail={"code": "paper_unavailable", "message": str(exc)}) from exc


def paper_reset(body: ResetBody, principal: Any) -> dict[str, Any]:
    """Start a fresh ZT-PAPER account; pending zt-paper proposals are rejected."""
    core = _core()
    if body.confirm != "RESET":
        raise HTTPException(status_code=422, detail={"code": "confirm_required",
                                                     "message": 'send {"confirm": "RESET"} to reset the paper account'})
    engine = core.zt_paper_engine()
    kwargs: dict[str, Any] = {"actor": core.APPROVER, "reason": f"reset from the Web UI ({_principal(principal)['subject']})"}
    if body.starting_cash is not None:
        kwargs["starting_cash"] = body.starting_cash
    if body.max_leverage is not None:
        kwargs["max_leverage"] = body.max_leverage
    try:
        engine.reset(**kwargs)
    except ValueError as exc:
        raise _error(exc) from exc
    rejected = engine.reject_pending_proposals()
    return {"account": engine.snapshot(), "rejected_proposals": rejected}


def _is_catch_all(route: Any) -> bool:
    return isinstance(route, Mount) and getattr(route, "path", None) in ("", "/")


def register(app: Any) -> list:
    """Register the routes on a VT FastAPI app (idempotent); returns the routes added."""
    if any(getattr(route, "path", None) == ROUTE_PATHS[0] for route in app.router.routes):
        return []
    from src.api.security import require_auth

    def approve(proposal_id: str, body: ApproveBody, principal: Any = Depends(require_auth)) -> dict[str, Any]:
        return approve_order_proposal(proposal_id, body, principal)

    def reject(proposal_id: str, body: RejectBody, principal: Any = Depends(require_auth)) -> dict[str, Any]:
        return reject_order_proposal(proposal_id, body, principal)

    def reset(body: ResetBody, principal: Any = Depends(require_auth)) -> dict[str, Any]:
        return paper_reset(body, principal)

    before = list(app.router.routes)
    auth = [Depends(require_auth)]
    for path, endpoint, methods, dependencies in (
            (ROUTE_PATHS[0], list_order_proposals, ["GET"], auth),
            (ROUTE_PATHS[1], get_order_proposal, ["GET"], auth),
            (ROUTE_PATHS[2], approve, ["POST"], []),
            (ROUTE_PATHS[3], reject, ["POST"], []),
            (ROUTE_PATHS[4], paper_account, ["GET"], auth),
            (ROUTE_PATHS[5], reset, ["POST"], [])):
        app.add_api_route(path, endpoint, methods=methods, dependencies=dependencies, response_model=None,
                          tags=["zt-approvals"], name=f"zt_approvals_{endpoint.__name__}")
    added = [route for route in app.router.routes if route not in before]
    routes = app.router.routes
    first_mount = next((i for i, route in enumerate(routes) if _is_catch_all(route)), None)
    if first_mount is not None:
        rest = [route for route in routes if route not in added]
        position = rest.index(routes[first_mount])
        routes[:] = rest[:position] + added + rest[position:]
    return added
