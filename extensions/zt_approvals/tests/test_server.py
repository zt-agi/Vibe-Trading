"""The stdio MCP server: the agent can propose and read, never approve."""
import asyncio
import os
import sys

import pytest

from conftest import EXTENSION, load_extension_module

EXPECTED = {"propose_orders", "list_order_proposals", "get_order_proposal", "paper_account"}


@pytest.fixture()
def server(home):
    return load_extension_module("zt_approvals_server", "server.py")


def _payload(result):
    return result.structured_content or result.data


def test_exactly_four_tools_and_none_approves(server):
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == EXPECTED == set(server.TOOL_NAMES)
    assert not any("approve" in name or "reject" in name or "reset" in name for name in tools)
    assert tools["propose_orders"].annotations.readOnlyHint is False
    assert tools["propose_orders"].annotations.destructiveHint is False
    for name in EXPECTED - {"propose_orders"}:
        assert tools[name].annotations.readOnlyHint is True
    params = tools["propose_orders"].parameters
    assert {"orders", "targets", "rationale", "evidence_ids", "broker"} <= set(params["properties"])
    assert params["properties"]["broker"]["default"] == "zt-paper"
    assert set(params["required"]) == {"rationale", "evidence_ids"}


def test_propose_list_get_through_an_mcp_client(server, core, engine):
    from fastmcp import Client

    async def run():
        async with Client(server.mcp) as client:
            proposed = _payload(await client.call_tool("propose_orders", {
                "targets": {"AAPL": 0.05}, "rationale": "PEAD drift after a beat", "evidence_ids": ["ev-8k-1"],
                "signals": [{"source": "pead", "symbol": "AAPL", "direction": "long", "evidence_ids": ["ev-8k-1"]}]}))
            listed = _payload(await client.call_tool("list_order_proposals", {"status": "PENDING"}))
            detail = _payload(await client.call_tool("get_order_proposal", {"proposal_id": proposed["proposal_id"]}))
            account = _payload(await client.call_tool("paper_account", {}))
            return proposed, listed, detail, account

    proposed, listed, detail, account = asyncio.run(run())
    assert proposed["status"] == "pending_approval" and proposed["proposal_status"] == "PENDING"
    assert proposed["orders"] == [{"symbol": "AAPL", "side": "buy", "qty": 25.0, "notional": None,
                                   "order_type": "market", "limit_price": None, "tif": "day"}]
    assert proposed["validation"]["ok"] is True
    assert proposed["decision_summary"]["target_weights"] == {"AAPL": 0.05}
    assert [row["id"] for row in listed["proposals"]] == [proposed["proposal_id"]]
    assert detail["decision_record"]["rationale"] == "PEAD drift after a beat"
    full_hash = core.load_proposal(proposed["proposal_id"])["content_hash"]
    for payload in (proposed, listed, detail):
        assert full_hash not in str(payload)  # only the tail: not enough to approve
    assert detail["content_hash_tail"] == full_hash[-12:]
    assert account["account"] == "ZT-PAPER" and account["cash"] == 100_000.0
    assert not engine.account_path().exists()  # proposing filled nothing


def test_bad_proposals_are_tool_errors(server):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async def run(arguments):
        async with Client(server.mcp) as client:
            await client.call_tool("propose_orders", arguments)

    for arguments in ({"rationale": "x", "evidence_ids": []},
                      {"rationale": "x", "evidence_ids": [], "orders": [{"symbol": "AAPL", "side": "buy", "qty": 1}],
                       "targets": {"AAPL": 0.1}},
                      {"rationale": "x", "evidence_ids": [], "orders": [{"symbol": "700.HK", "side": "buy", "qty": 1}]}):
        with pytest.raises(ToolError):
            asyncio.run(run(arguments))


def test_stdio_entrypoint_as_vibe_trading_runs_it(home):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    env = {key: value for key, value in os.environ.items()
           if key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")}
    env["VIBE_TRADING_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    transport = StdioTransport(command=sys.executable, args=[str(EXTENSION / "server.py")], env=env,
                               cwd=str(home))

    async def run():
        async with Client(transport) as client:
            tools = await client.list_tools()
            listed = await client.call_tool("list_order_proposals", {})
            return {tool.name for tool in tools}, _payload(listed)

    names, listed = asyncio.run(run())
    assert names == EXPECTED
    assert listed["proposals"] == [] and listed["approval_mode"] == "off"
