"""ZT add-on: start VT's normal server with the ZT dashboard routes registered.

    python extensions/zt_dashboards/launch_api.py [serve] --host 127.0.0.1 --port 8899 [--dev]

The arguments are VT's own ``serve`` flags and reach ``api_server.serve_main``
unchanged (a leading ``serve`` token is accepted and dropped, so the launcher
can stand in for ``vibe-trading serve``). The routes are registered on
``api_server.app`` before ``serve_main`` mounts the SPA at "/". ``api_server``
is imported under its own module name because VT's scheduled-research
dispatcher looks it up in ``sys.modules``.

Environment: INVESTMENT_AI_PROJECT_ROOT names the canonical project folder the
routes read. Nothing else changes: VT's .env, API key, CORS, CSP and scheduler
settings apply exactly as with ``vibe-trading serve``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
AGENT_DIR = REPO_ROOT / "agent"
ROUTES_MODULE = "zt_dashboards_api_routes"


def _same_dir(entry: str) -> bool:
    try:
        return Path(entry or ".").resolve() == HERE
    except OSError:
        return False


def _load_routes():
    module = sys.modules.get(ROUTES_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(ROUTES_MODULE, HERE / "api_routes.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[ROUTES_MODULE] = module
        spec.loader.exec_module(module)
    return module


def prepare():
    """Import this checkout's api_server and register the ZT routes on its app."""
    # Running this file puts its folder first on sys.path; drop it so no
    # extension module can shadow a VT import, and put this checkout's agent/ first.
    sys.path[:] = [entry for entry in sys.path if not _same_dir(entry)]
    agent = str(AGENT_DIR)
    if agent in sys.path:
        sys.path.remove(agent)
    sys.path.insert(0, agent)
    import api_server

    _load_routes().register(api_server.app)
    return api_server


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["serve"]:
        args = args[1:]
    api_server = prepare()
    return int(api_server.serve_main(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
