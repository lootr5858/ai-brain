#!/usr/bin/env python3
"""Read-only brain-mcp SDK adapter tests."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import types

from lib.brain_mcp import BrainMcpConfig, tools
from lib.mcp_protocol import TOOL_INPUTS, create_mcp_server, tool_schema


root = Path(tempfile.mkdtemp(prefix="brain-mcp-protocol-"))
cfg = BrainMcpConfig(
    brain=root,
    engine=root / "engine",
    agents=root / "agents",
    user=root / "user",
)

assert set(TOOL_INPUTS) == set(tools())
for name in tools():
    schema = tool_schema(name)
    assert schema["inputSchema"]["additionalProperties"] is False
    assert schema["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


async def checks() -> None:
    server = create_mcp_server(cfg)
    list_handler = server.get_request_handler("tools/list")
    call_handler = server.get_request_handler("tools/call")
    assert list_handler is not None and call_handler is not None

    listed = await list_handler.handler(None, types.PaginatedRequestParams())
    assert {tool.name for tool in listed.tools} == set(tools())

    health = await call_handler.handler(
        None,
        types.CallToolRequestParams(name="health_check", arguments={}),
    )
    assert not health.is_error
    assert health.content[0].text == "pong from brain-mcp"

    rejected = await call_handler.handler(
        None,
        types.CallToolRequestParams(name="propose_change", arguments={}),
    )
    assert rejected.is_error
    assert "Unknown tool" in rejected.content[0].text


asyncio.run(checks())
print("brain-mcp protocol tests: PASS")
