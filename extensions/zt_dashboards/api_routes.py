"""ZT add-on: Web UI routes for ZT's read-only research dashboards.

``register(app)`` adds three GET routes to VT's FastAPI app:

    /zt/reports                 whitelisted report list        (require_auth)
    /zt/reports/{report_id}     one whitelisted HTML report    (require_event_stream_auth)
    /zt/snapshot/{date}         daily_snapshot envelope        (require_auth)

Authentication reuses VT's own dependencies. The report route uses the event
stream variant because a browser iframe, like EventSource, cannot send an
Authorization header: the page mints VT's short-lived single-use ticket
(POST /auth/sse-ticket) and loads ``?ticket=``; bearer headers still work and
loopback dev mode is unchanged. Reports are served with their own CSP
(sandboxed, no network access) and ``X-Frame-Options: SAMEORIGIN``.

The routes are placed ahead of any catch-all "/" mount, so the SPA cannot
shadow them whichever order the caller uses. No upstream file is modified.
"""
import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.routing import Mount

_HERE = Path(__file__).resolve().parent
_CORE_MODULE = "zt_dashboards_core"

ROUTE_PATHS = ("/zt/reports", "/zt/reports/{report_id:path}", "/zt/snapshot/{date}")


def _load_core():
    module = sys.modules.get(_CORE_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(_CORE_MODULE, _HERE / "zt_core.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_MODULE] = module
        spec.loader.exec_module(module)
    return module


core = _load_core()


def _unavailable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"ZT dashboards unavailable: {exc}")


def list_zt_reports() -> Any:
    """Whitelisted project reports with existence, size and sha256."""
    try:
        return core.project_reports()
    except core.ProjectRootError as exc:
        raise _unavailable(exc) from exc


def get_zt_report(report_id: str, request: Request) -> Any:
    """One whitelisted report, sandboxed, with its own CSP and SAMEORIGIN framing."""
    try:
        body, headers = core.render_report(report_id)
    except core.ProjectRootError as exc:
        raise _unavailable(exc) from exc
    except core.ReportNotFound as exc:
        if "text/html" in request.headers.get("accept", ""):
            return Response(core.not_found_page(str(exc)), status_code=404,
                            media_type="text/html; charset=utf-8",
                            headers=core.not_found_headers())
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return Response(content=body, media_type="text/html; charset=utf-8", headers=headers)


def get_zt_snapshot(date: str) -> Any:
    """The daily_snapshot envelope for 'latest', 'today' or YYYY-MM-DD."""
    try:
        return core.daily_snapshot(date)
    except core.ProjectRootError as exc:
        raise _unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _is_catch_all(route: Any) -> bool:
    return isinstance(route, Mount) and getattr(route, "path", None) in ("", "/")


def register(app: Any) -> list:
    """Register the ZT routes on a VT FastAPI app (idempotent).

    Returns the routes added by this call; an empty list when they already exist.
    """
    if any(getattr(route, "path", None) == ROUTE_PATHS[0] for route in app.router.routes):
        return []
    from src.api.security import require_auth, require_event_stream_auth

    before = list(app.router.routes)
    for path, endpoint, dependency in (
            (ROUTE_PATHS[0], list_zt_reports, require_auth),
            (ROUTE_PATHS[1], get_zt_report, require_event_stream_auth),
            (ROUTE_PATHS[2], get_zt_snapshot, require_auth)):
        app.add_api_route(path, endpoint, methods=["GET"], dependencies=[Depends(dependency)],
                          response_model=None, tags=["zt-dashboards"],
                          name=f"zt_dashboards_{endpoint.__name__}")
    added = [route for route in app.router.routes if route not in before]
    routes = app.router.routes
    first_mount = next((i for i, route in enumerate(routes) if _is_catch_all(route)), None)
    if first_mount is not None:
        rest = [route for route in routes if route not in added]
        position = rest.index(routes[first_mount])
        routes[:] = rest[:position] + added + rest[position:]
    return added
