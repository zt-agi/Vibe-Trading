"""QA-only startup seam, copied into a fresh E: directory by desktop-ui-smoke.

No production setting is invented: replace the actual registry factory before
SessionService imports it. The native session, event stream and Ollama provider
remain real. This module is never installed on the ordinary runtime PYTHONPATH.
"""
import json
import os
from pathlib import Path

_destination = os.environ.get("VT_QA_NO_TOOLS_EVIDENCE")
if _destination:
    _evidence = Path(_destination).resolve()
    _home = Path(os.environ["VIBE_TRADING_HOME"]).resolve()
    if _evidence.drive.upper() != "E:" or _home.drive.upper() != "E:":
        os._exit(91)
    try:
        _config = json.loads((_home / "agent.json").read_text(encoding="utf-8"))
        assert _config.get("mcpServers", {}) == {}
        assert os.environ.get("VIBE_TRADING_ENABLE_SHELL_TOOLS") == "false"
        from src.agent.tools import ToolRegistry
        import src.tools

        def record(**row):
            with _evidence.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")

        class EmptyRegistry(ToolRegistry):
            def register(self, tool):
                raise RuntimeError("QA registry forbids registering any tool")

            def get_definitions(self):
                assert not self._tools
                record(phase="definitions", tools=0)
                return []

        def empty_registry(*args, **kwargs):
            record(phase="registry", tools=0)
            return EmptyRegistry()

        src.tools.build_registry = empty_registry
        record(phase="installed", mcp_servers=0, shell_enabled=False)
    except BaseException:
        # sitecustomize normally logs an error and continues: fail closed here.
        os._exit(92)
