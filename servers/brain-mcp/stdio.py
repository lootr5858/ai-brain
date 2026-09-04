#!/usr/bin/env python3
"""Local brain-mcp STDIO entrypoint."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


SERVER_DIRECTORY = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == SERVER_DIRECTORY:
    sys.path.pop(0)

import anyio
from mcp.server.stdio import stdio_server


ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE))

from lib.brain_mcp import load_config
from lib.mcp_protocol import create_mcp_server


class _NativeInput:
    """Line iterator that avoids AnyIO's blocked AsyncFile stdin on Python 3.13."""

    def __init__(self) -> None:
        self.buffer = b""

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while b"\n" not in self.buffer:
            await anyio.wait_readable(0)
            chunk = os.read(0, 65_536)
            if not chunk:
                if not self.buffer:
                    raise StopAsyncIteration
                break
            self.buffer += chunk
        line, separator, self.buffer = self.buffer.partition(b"\n")
        return (line + separator).decode("utf-8", errors="replace")


class _NativeOutput:
    async def write(self, text: str) -> None:
        data = memoryview(text.encode())
        while data:
            written = os.write(1, data)
            data = data[written:]

    async def flush(self) -> None:
        return None


async def serve(config) -> None:
    server = create_mcp_server(config)
    async with stdio_server(
        stdin=_NativeInput(),
        stdout=_NativeOutput(),
    ) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    anyio.run(serve, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
