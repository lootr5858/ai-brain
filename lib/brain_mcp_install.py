#!/usr/bin/env python3
"""Local brain-mcp connection and verification helpers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile

from lib.config import CONFIG_ENV, config_path, load_config
from lib.brain_mcp import READ_TOOLS


MCP_PROTOCOL_VERSION = "2026-07-28"


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallContext:
    engine: Path
    brain: Path
    python: Path
    agents: Path | None = None
    user: Path | None = None
    config: Path | None = None


def discover_context(selected_config: str | Path | None = None) -> InstallContext:
    data = load_config(selected_config)
    paths = data["paths"]
    engine = Path(paths["engine"])
    explicit = selected_config is not None or CONFIG_ENV in os.environ
    return InstallContext(
        engine=engine,
        brain=Path(paths["brain"]),
        python=engine / ".venv/bin/python",
        agents=Path(paths["agents"]),
        user=Path(paths["user"]),
        config=config_path(selected_config) if explicit else None,
    )


def validate_context(ctx: InstallContext) -> None:
    if sys.version_info < (3, 11):
        raise InstallError("brain-mcp requires Python 3.11+")
    required = (
        ctx.python,
        ctx.engine / "servers" / "brain-mcp" / "stdio.py",
        ctx.agents or ctx.brain / "agents",
        ctx.user or ctx.brain / "user",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise InstallError("missing required path(s): " + ", ".join(missing))


def expected_stdio_command(ctx: InstallContext) -> tuple[str, tuple[str, ...]]:
    arguments = [str(ctx.engine / "servers" / "brain-mcp" / "stdio.py")]
    if ctx.config is not None:
        arguments.extend(("--config", str(ctx.config)))
    return (
        str(ctx.python),
        tuple(arguments),
    )


def stdio_connection(ctx: InstallContext) -> dict[str, object]:
    command, args = expected_stdio_command(ctx)
    return {
        "transport": "stdio",
        "command": command,
        "args": list(args),
        "env": {},
    }


def _tool_text(result, label: str) -> str:
    content = getattr(result, "content", None)
    if (
        getattr(result, "is_error", False)
        or not isinstance(content, list)
        or not content
        or not isinstance(getattr(content[0], "text", None), str)
    ):
        raise InstallError(f"{label} returned an invalid result")
    return str(content[0].text)


async def _probe_stdio(ctx: InstallContext, timeout_seconds: float) -> list[str]:
    import anyio
    from mcp import StdioServerParameters
    from mcp.client.client import Client
    from mcp.client.stdio import stdio_client

    command, args = expected_stdio_command(ctx)
    environment = {
        name: os.environ[name]
        for name in (
            "PYTHONPATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONPYCACHEPREFIX",
        )
        if name in os.environ
    }
    parameters = StdioServerParameters(
        command=command,
        args=list(args),
        env=environment,
        cwd=ctx.engine,
    )
    try:
        with tempfile.TemporaryFile(mode="w+") as errors:
            with anyio.fail_after(timeout_seconds):
                async with Client(
                    stdio_client(parameters, errlog=errors),
                    mode="auto",
                    read_timeout_seconds=timeout_seconds,
                    cache=None,
                ) as client:
                    if client.protocol_version != MCP_PROTOCOL_VERSION:
                        raise InstallError(
                            "STDIO negotiated an unexpected protocol version"
                        )
                    listed = await client.list_tools()
                    names = {tool.name for tool in listed.tools}
                    if names != READ_TOOLS or len(names) != len(listed.tools):
                        raise InstallError("STDIO read capability scope is invalid")
                    health = await client.call_tool("health_check", {})
                    if _tool_text(health, "health_check") != "pong from brain-mcp":
                        raise InstallError("STDIO health_check failed")
                    status = await client.call_tool("status", {})
                    try:
                        body = json.loads(_tool_text(status, "status"))
                    except json.JSONDecodeError as exc:
                        raise InstallError("STDIO status returned invalid JSON") from exc
                    if body.get("transport") != "stdio" or set(body.get("tools", ())) != names:
                        raise InstallError("STDIO status verification failed")
                    return ["server/discover", "tools/list", "health_check", "status"]
    except TimeoutError as exc:
        raise InstallError("brain-mcp STDIO process timed out") from exc
    except InstallError:
        raise
    except Exception as exc:
        raise InstallError(f"STDIO probe failed ({type(exc).__name__})") from exc


def verify_stdio(
    ctx: InstallContext,
    *,
    timeout_seconds: float = 20,
) -> list[str]:
    import anyio

    validate_context(ctx)
    if timeout_seconds <= 0:
        raise InstallError("STDIO probe timeout must be positive")
    return anyio.run(lambda: _probe_stdio(ctx, timeout_seconds))


__all__ = [
    "InstallContext",
    "InstallError",
    "discover_context",
    "expected_stdio_command",
    "stdio_connection",
    "validate_context",
    "verify_stdio",
]
