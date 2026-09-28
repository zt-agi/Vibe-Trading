"""The stdio MCP server exposes exactly nine read-only tools returning envelopes."""
import asyncio
import os
import sys

import pytest

from conftest import EXTENSION, load_extension_module

EXPECTED_TOOLS = {"daily_snapshot", "source_families", "signal_state", "lane_status",
                  "test_ledger", "promotion_board", "pit_inventory", "world_model",
                  "project_reports"}


@pytest.fixture()
def server():
    return load_extension_module("zt_dashboards_server", "server.py")


def _payload(result):
    return result.structured_content or result.data


def test_exactly_the_read_only_tools_are_registered(server):
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == EXPECTED_TOOLS == set(server.TOOL_NAMES)
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.description
    assert list(tools["daily_snapshot"].parameters["properties"]) == ["date"]
    assert tools["daily_snapshot"].parameters["properties"]["date"]["default"] == "latest"
    for name in EXPECTED_TOOLS - {"daily_snapshot"}:
        assert not tools[name].parameters.get("properties")


def test_tools_answer_through_an_mcp_client(server, project):
    from fastmcp import Client

    async def run():
        async with Client(server.mcp) as client:
            snapshot = await client.call_tool("daily_snapshot", {"date": "2026-09-27"})
            ledger = await client.call_tool("test_ledger", {})
            world = await client.call_tool("world_model", {})
            return _payload(snapshot), _payload(ledger), _payload(world)

    snapshot, ledger, world = asyncio.run(run())
    assert snapshot["source_path"] == "implementation/exports/2026-09-27"
    assert snapshot["pit_label"] == "NON_PIT" and snapshot["status"] in {"FRESH", "STALE"}
    assert "gross_absolute_captured_value" not in str(snapshot)
    assert ledger["data"]["hurdle_t"] == 3.34
    assert world["data"]["availability"] == "NOT_AVAILABLE"


def test_bad_date_is_a_tool_error_not_a_crash(server, project):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async def run():
        async with Client(server.mcp) as client:
            await client.call_tool("daily_snapshot", {"date": "../../etc"})

    with pytest.raises(ToolError, match="latest"):
        asyncio.run(run())


def test_stdio_entrypoint_as_vibe_trading_runs_it(project):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    env = {key: value for key, value in os.environ.items()
           if key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")}
    env["INVESTMENT_AI_PROJECT_ROOT"] = str(project)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    transport = StdioTransport(command=sys.executable, args=[str(EXTENSION / "server.py")],
                               env=env, cwd=str(project.parent))

    async def run():
        async with Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
            reports = _payload(await client.call_tool("project_reports", {}))
            return names, reports

    names, reports = asyncio.run(run())
    assert names == EXPECTED_TOOLS
    assert reports["source_path"] == "PROJECT_HUB.json" and len(reports["sha256"]) == 64
