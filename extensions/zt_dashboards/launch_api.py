"""ZT add-on: start VT's normal server with the extension routes registered.

    python extensions/zt_dashboards/launch_api.py [serve] --host 127.0.0.1 --port 8899 [--dev]

The arguments are VT's own ``serve`` flags and reach ``api_server.serve_main``
unchanged (a leading ``serve`` token is accepted and dropped, so the launcher
can stand in for ``vibe-trading serve``). ``api_server`` is imported under its
own module name because VT's scheduled-research dispatcher looks it up in
``sys.modules``.

Route discovery. Every ``extensions/<name>/api_routes.py`` that exposes a
callable ``register(app)`` is registered on ``api_server.app`` before
``serve_main`` mounts the SPA at "/", in sorted folder-name order. Only
allowlisted folders are loaded: ``VT_EXTENSION_ROUTES`` holds comma-separated
names or fnmatch patterns (default ``zt_*``; ``*`` allows every folder, ``none``
loads nothing). One log line per folder goes to stderr (``[vt-extensions]``).
A folder whose module fails to import or whose ``register`` raises is logged and
skipped, and any route it added before failing is removed again, so one broken
extension never takes down the SPA or the other extensions. The outcome is also
kept on ``app.state.vt_extension_routes`` for the /zt pre-flight panel.

Module contract: ``api_routes.py`` is loaded by path as ``<name>_api_routes``
(also reachable as ``vt_extensions.<name>.api_routes``, so relative imports of
sibling files work). While it executes, its folder is on ``sys.path`` *after*
VT's own entries, so bare sibling imports resolve but can never shadow a VT
module. ``register(app)`` should be idempotent.

Environment: INVESTMENT_AI_PROJECT_ROOT names the canonical project folder the
ZT routes read. Nothing else changes: VT's .env, API key, CORS, CSP and scheduler
settings apply exactly as with ``vibe-trading serve``.
"""
from __future__ import annotations

import fnmatch
import importlib.machinery
import importlib.util
import os
import re
import sys
import types
from pathlib import Path
from typing import Any, Callable, Iterable

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
AGENT_DIR = REPO_ROOT / "agent"
EXTENSIONS_DIR = REPO_ROOT / "extensions"
ROUTES_MODULE = "zt_dashboards_api_routes"
ROUTES_FILE = "api_routes.py"
ALLOWLIST_ENV = "VT_EXTENSION_ROUTES"
DEFAULT_ALLOWLIST = ("zt_*",)
PACKAGE_ROOT = "vt_extensions"
LOG_PREFIX = "[vt-extensions]"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def _same_dir(entry: str) -> bool:
    try:
        return Path(entry or ".").resolve() == HERE
    except OSError:
        return False


def allowlist(raw: str | None = None) -> tuple[str, ...]:
    """Allowlisted folder names or patterns; ``raw`` defaults to the environment."""
    text = os.environ.get(ALLOWLIST_ENV, "") if raw is None else raw
    items = tuple(part.strip() for part in text.replace(";", ",").split(",") if part.strip())
    if not items:
        return DEFAULT_ALLOWLIST
    if any(item.lower() == "none" for item in items):
        return ()
    return items


def is_allowed(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def discover(extensions_dir: Path = EXTENSIONS_DIR) -> list[Path]:
    """Extension folders holding an api_routes.py, sorted by folder name."""
    base = Path(extensions_dir)
    if not base.is_dir():
        return []
    root = base.resolve()
    found = []
    for folder in sorted(base.iterdir(), key=lambda p: p.name):
        if folder.name.startswith((".", "_")) or not folder.is_dir():
            continue
        routes = folder / ROUTES_FILE
        try:
            inside = routes.resolve().parent.parent == root
        except OSError:
            inside = False
        if routes.is_file() and inside:
            found.append(folder)
    return found


def _package(name: str, folder: Path) -> str:
    """A synthetic package per extension so ``from . import x`` resolves siblings."""
    if PACKAGE_ROOT not in sys.modules:
        root = types.ModuleType(PACKAGE_ROOT)
        root.__path__ = []
        root.__spec__ = importlib.machinery.ModuleSpec(PACKAGE_ROOT, None, is_package=True)
        sys.modules[PACKAGE_ROOT] = root
    package_name = f"{PACKAGE_ROOT}.{name}"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(folder)]
        spec = importlib.machinery.ModuleSpec(package_name, None, is_package=True)
        spec.submodule_search_locations = [str(folder)]
        package.__spec__ = spec
        package.__package__ = package_name
        sys.modules[package_name] = package
    return package_name


def load_routes_module(name: str, path: Path) -> types.ModuleType:
    """Load ``extensions/<name>/api_routes.py`` once, by path."""
    alias = f"{name}_api_routes"
    existing = sys.modules.get(alias)
    if existing is not None:
        try:
            if Path(existing.__file__).resolve() == Path(path).resolve():
                return existing
        except (AttributeError, TypeError, OSError):
            pass
    full_name = f"{_package(name, Path(path).parent)}.api_routes"
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    folder = str(Path(path).parent)
    sys.modules[full_name] = module
    sys.modules[alias] = module
    appended = folder not in sys.path
    if appended:
        sys.path.append(folder)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(full_name, None)
        sys.modules.pop(alias, None)
        raise
    finally:
        if appended:
            try:
                sys.path.remove(folder)
            except ValueError:
                pass
    return module


def _describe(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:300]}" if text else type(exc).__name__


def register_extensions(app: Any, extensions_dir: Path = EXTENSIONS_DIR,
                        patterns: Iterable[str] | None = None,
                        log: Callable[[str], None] = _log) -> list[dict[str, Any]]:
    """Register every allowlisted extension's routes on ``app``; never raises."""
    allowed = tuple(allowlist() if patterns is None else patterns)
    results: list[dict[str, Any]] = []
    for folder in discover(extensions_dir):
        name = folder.name
        entry: dict[str, Any] = {"name": name, "status": "skipped", "routes": [], "reason": ""}
        results.append(entry)
        if not _NAME_RE.match(name):
            entry["reason"] = "folder name is not a valid module name"
            log(f"skipped {name}: {entry['reason']}")
            continue
        if not is_allowed(name, allowed):
            entry["reason"] = f"not in the allowlist ({ALLOWLIST_ENV}={','.join(allowed) or 'none'})"
            log(f"skipped {name}: {entry['reason']}")
            continue
        before = {id(route) for route in app.router.routes}
        try:
            module = load_routes_module(name, folder / ROUTES_FILE)
            register = getattr(module, "register", None)
            if not callable(register):
                entry["reason"] = f"{ROUTES_FILE} has no register(app)"
                log(f"skipped {name}: {entry['reason']}")
                continue
            register(app)
        except (Exception, SystemExit) as exc:  # one broken extension never stops the server
            app.router.routes[:] = [route for route in app.router.routes if id(route) in before]
            entry.update(status="failed", reason=_describe(exc))
            log(f"FAILED {name}: {entry['reason']} -- skipped; the SPA and the other "
                "extensions are unaffected")
            continue
        added = [route for route in app.router.routes if id(route) not in before]
        entry["routes"] = sorted({str(getattr(route, "path", "?")) for route in added})
        entry["status"] = "registered"
        if added:
            log(f"registered {name}: {len(added)} route(s) {', '.join(entry['routes'])}")
        else:
            entry["reason"] = "routes were already registered"
            log(f"registered {name}: routes were already registered")
    if not results:
        log(f"no extension routes found under {extensions_dir}")
    try:
        app.state.vt_extension_routes = results
    except Exception:  # an app without .state still serves
        pass
    return results


def prepare(extensions_dir: Path = EXTENSIONS_DIR):
    """Import this checkout's api_server and register the extension routes on its app."""
    # Running this file puts its folder first on sys.path; drop it so no
    # extension module can shadow a VT import, and put this checkout's agent/ first.
    sys.path[:] = [entry for entry in sys.path if not _same_dir(entry)]
    agent = str(AGENT_DIR)
    if agent in sys.path:
        sys.path.remove(agent)
    sys.path.insert(0, agent)
    import api_server

    register_extensions(api_server.app, extensions_dir)
    return api_server


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["serve"]:
        args = args[1:]
    api_server = prepare()
    return int(api_server.serve_main(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
