"""launch_api route discovery: allowlist, deterministic order, isolation of failures."""
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from conftest import EXTENSION, load_extension_module


@pytest.fixture()
def launcher():
    return load_extension_module("zt_dashboards_launch_api", "launch_api.py")


def make_ext(root: Path, name: str, routes: str, **siblings: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "api_routes.py").write_text(textwrap.dedent(routes))
    for filename, body in siblings.items():
        (folder / f"{filename}.py").write_text(textwrap.dedent(body))
    return folder


ROUTE = """
    def register(app):
        @app.get("/{path}")
        def endpoint():
            return {{"ext": "{name}"}}
    """


@pytest.fixture()
def extensions(tmp_path):
    root = tmp_path / "extensions"
    make_ext(root, "zt_alpha", ROUTE.format(path="zt/alpha", name="zt_alpha"))
    make_ext(root, "zt_broken", """
        def register(app):
            @app.get("/zt/half")
            def half():
                return {}
            raise RuntimeError("register blew up")
        """)
    make_ext(root, "zt_import_error", "import module_that_does_not_exist_zt\n")
    make_ext(root, "zt_exit", "raise SystemExit(3)\n")
    make_ext(root, "zt_no_register", "VALUE = 1\n")
    make_ext(root, "zt_relative", """
        from .helper import PATH

        def register(app):
            @app.get(PATH)
            def rel():
                return {"ext": "zt_relative"}
        """, helper='PATH = "/zt/relative"\n')
    make_ext(root, "zt_bare", """
        import zt_bare_helper_mod

        def register(app):
            @app.get(zt_bare_helper_mod.PATH)
            def bare():
                return {"ext": "zt_bare"}
        """, zt_bare_helper_mod='PATH = "/zt/bare"\n')
    make_ext(root, "other_ext", ROUTE.format(path="other", name="other_ext"))
    make_ext(root, "zt-dash", ROUTE.format(path="dash", name="zt-dash"))
    (root / "_private").mkdir()
    (root / "_private" / "api_routes.py").write_text("raise AssertionError('never loaded')\n")
    (root / "zt_empty").mkdir()
    yield root
    for name in list(sys.modules):
        if name.startswith(("vt_extensions.zt_", "zt_alpha", "zt_broken", "zt_relative", "zt_bare",
                            "zt_import_error", "zt_exit", "zt_no_register")):
            sys.modules.pop(name, None)


def test_allowlist_parsing(launcher, monkeypatch):
    monkeypatch.delenv(launcher.ALLOWLIST_ENV, raising=False)
    assert launcher.allowlist() == ("zt_*",)
    assert launcher.allowlist(" zt_dashboards ; zt_approvals ,") == ("zt_dashboards", "zt_approvals")
    assert launcher.allowlist("none") == ()
    monkeypatch.setenv(launcher.ALLOWLIST_ENV, "*")
    assert launcher.is_allowed("anything", launcher.allowlist())
    assert not launcher.is_allowed("other_ext", ("zt_*",))


def test_discovery_is_sorted_and_skips_private_and_empty_folders(launcher, extensions):
    names = [folder.name for folder in launcher.discover(extensions)]
    assert names == sorted(names)
    assert "_private" not in names and "zt_empty" not in names
    assert launcher.discover(extensions / "absent") == []


def test_register_extensions_isolates_failures(launcher, extensions):
    app = FastAPI()
    lines = []
    path_before = list(sys.path)
    results = launcher.register_extensions(app, extensions, ("zt_*",), log=lines.append)
    assert sys.path == path_before
    by_name = {r["name"]: r for r in results}
    assert [r["name"] for r in results] == sorted(by_name)
    assert by_name["zt_alpha"] == {"name": "zt_alpha", "status": "registered",
                                   "routes": ["/zt/alpha"], "reason": ""}
    assert by_name["zt_relative"]["routes"] == ["/zt/relative"]
    assert by_name["zt_bare"]["routes"] == ["/zt/bare"]
    assert by_name["zt_broken"]["status"] == "failed"
    assert "RuntimeError: register blew up" in by_name["zt_broken"]["reason"]
    assert by_name["zt_import_error"]["status"] == "failed"
    assert "ModuleNotFoundError" in by_name["zt_import_error"]["reason"]
    assert by_name["zt_exit"]["status"] == "failed"
    assert by_name["zt_no_register"] == {"name": "zt_no_register", "status": "skipped", "routes": [],
                                         "reason": "api_routes.py has no register(app)"}
    assert by_name["other_ext"]["status"] == "skipped" and "allowlist" in by_name["other_ext"]["reason"]
    assert by_name["zt-dash"]["status"] == "skipped"
    assert app.state.vt_extension_routes is results
    # One log line per folder, prefixed; failures say what happened.
    assert len(lines) == len(results)
    assert any(line.startswith("FAILED zt_broken: RuntimeError") for line in lines)
    assert any(line.startswith("registered zt_alpha: 1 route(s) /zt/alpha") for line in lines)

    paths = {getattr(route, "path", None) for route in app.router.routes}
    assert "/zt/half" not in paths  # the failed extension's partial route was removed
    dist = extensions.parent / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>")
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    client = TestClient(app)
    assert client.get("/zt/alpha").json() == {"ext": "zt_alpha"}
    assert client.get("/zt/relative").json() == {"ext": "zt_relative"}
    assert client.get("/zt/bare").json() == {"ext": "zt_bare"}
    assert client.get("/").text == "<html>spa</html>"


def test_an_idempotent_register_is_reported_as_already_registered(launcher, tmp_path):
    root = tmp_path / "ext2"
    make_ext(root, "zt_once", """
        def register(app):
            if any(getattr(r, "path", None) == "/zt/once" for r in app.router.routes):
                return []
            app.add_api_route("/zt/once", lambda: {"ok": True}, methods=["GET"])
        """)
    app = FastAPI()
    try:
        first = launcher.register_extensions(app, root, ("zt_*",), log=lambda _: None)
        again = launcher.register_extensions(app, root, ("zt_*",), log=lambda _: None)
    finally:
        sys.modules.pop("zt_once_api_routes", None)
        sys.modules.pop("vt_extensions.zt_once.api_routes", None)
    assert first[0]["routes"] == ["/zt/once"]
    assert again[0] == {"name": "zt_once", "status": "registered", "routes": [],
                        "reason": "routes were already registered"}
    assert [getattr(r, "path", None) for r in app.router.routes].count("/zt/once") == 1


def test_real_extensions_register_the_zt_dashboards(launcher, project):
    app = FastAPI()
    lines = []
    results = launcher.register_extensions(app, launcher.EXTENSIONS_DIR, log=lines.append)
    dashboards = next(r for r in results if r["name"] == "zt_dashboards")
    assert dashboards["status"] == "registered"
    assert dashboards["routes"] == sorted(["/zt/reports", "/zt/reports/{report_id:path}",
                                           "/zt/snapshot/{date}", "/zt/preflight"])
    assert all(r["status"] != "failed" for r in results), lines
    # Folders without api_routes.py are not listed at all.
    assert {r["name"] for r in results} <= {p.name for p in EXTENSION.parent.iterdir()}
    assert "pit_actor_sim" not in {r["name"] for r in results}


def test_logs_go_to_stderr_with_a_prefix(launcher, extensions, capsys):
    launcher.register_extensions(FastAPI(), extensions, ("zt_alpha",))
    err = capsys.readouterr().err
    assert "[vt-extensions] registered zt_alpha" in err
    assert "[vt-extensions] skipped zt_bare: not in the allowlist" in err
