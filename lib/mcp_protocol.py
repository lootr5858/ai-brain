#!/usr/bin/env python3
"""Read-only brain-mcp SDK adapter."""
from __future__ import annotations

from mcp import types
from mcp.server.lowlevel import Server

from lib.brain_mcp import BrainMcpConfig, ToolError, call_tool, tools


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _string(description: str, *, minimum: int | None = None) -> dict:
    schema: dict[str, object] = {"type": "string", "description": description}
    if minimum is not None:
        schema["minLength"] = minimum
    return schema


TOOL_INPUTS = {
    "get_core": ({}, []),
    "get_discipline": ({"name": _string("Discipline name.", minimum=1)}, ["name"]),
    "get_reference": ({"path": _string("Reference path.", minimum=1)}, ["path"]),
    "get_skill": ({"name": _string("Skill name.", minimum=1)}, ["name"]),
    "get_user_context": ({"field": _string("User context field.", minimum=1)}, ["field"]),
    "health_check": ({}, []),
    "list_context": ({}, []),
    "list_nonprivate": ({"path": _string("Brain path; defaults to root.")}, []),
    "read_nonprivate": ({"path": _string("Brain file path.", minimum=1)}, ["path"]),
    "search": ({"query": _string("Text query.", minimum=2)}, ["query"]),
    "status": ({}, []),
}


class InvalidParams(ValueError):
    pass


def tool_schema(name: str) -> dict:
    try:
        properties, required = TOOL_INPUTS[name]
    except KeyError:
        raise InvalidParams(f"Unknown tool: {name}") from None
    return {
        "name": name,
        "description": name.replace("_", " "),
        "inputSchema": {
            "$schema": JSON_SCHEMA_DIALECT,
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def _argument_error(name: str, arguments: dict) -> str | None:
    properties, required = TOOL_INPUTS[name]
    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        return "Additional properties are not allowed: " + ", ".join(unexpected)
    missing = [field for field in required if field not in arguments]
    if missing:
        return "Required properties are missing: " + ", ".join(missing)
    for field, value in arguments.items():
        expected = properties[field]
        if not isinstance(value, str):
            return f"{field} must be a string"
        minimum = expected.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return f"{field} must be at least {minimum} characters"
    return None


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


def create_mcp_server(config: BrainMcpConfig) -> Server[None]:
    async def list_tools(
        _context,
        _params: types.PaginatedRequestParams,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(tool_schema(name)) for name in tools()],
            ttl_ms=0,
            cache_scope="private",
        )

    async def invoke(
        _context,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        if name not in TOOL_INPUTS:
            return _error(f"Unknown tool: {name}")
        arguments = params.arguments or {}
        argument_error = _argument_error(name, arguments)
        if argument_error:
            return _error(f"Input validation error: {argument_error}")
        try:
            output = call_tool(name, arguments, config)
        except ToolError as exc:
            return _error(str(exc))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=output)]
        )

    return Server(
        "brain-mcp",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=invoke,
    )


__all__ = ["TOOL_INPUTS", "create_mcp_server", "tool_schema"]
