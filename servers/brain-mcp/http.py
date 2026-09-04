#!/usr/bin/env python3
"""Loopback Streamable HTTP entrypoint for remote brain-mcp access."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


SERVER_DIRECTORY = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == SERVER_DIRECTORY:
    sys.path.pop(0)

import uvicorn


ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE))

from lib.brain_mcp import load_config
from lib.brain_mcp_remote import create_remote_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--port", type=int, default=8787)
    return parser


def build_server_config(*, config_path: str | None, port: int) -> uvicorn.Config:
    app = create_remote_app(load_config(config_path))
    return uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        proxy_headers=False,
        access_log=False,
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        uvicorn.Server(
            build_server_config(config_path=args.config, port=args.port)
        ).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
