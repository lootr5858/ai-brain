#!/usr/bin/env python3
"""Local brain-mcp installer tests."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install.brain_mcp as installer
import lib.brain_mcp_install as brain_mcp_install
from lib.brain_mcp_install import (
    InstallContext,
    InstallError,
    expected_stdio_command,
    stdio_connection,
    verify_stdio,
)


engine = Path(__file__).resolve().parents[1]
with mock.patch.object(
    brain_mcp_install.sys,
    "executable",
    "/outside/activated/environment/python",
):
    assert brain_mcp_install.discover_context().python == engine / ".venv/bin/python"

context = InstallContext(
    engine=engine,
    brain=engine.parent,
    python=Path(sys.executable),
)
command, args = expected_stdio_command(context)
assert command == sys.executable
assert args == (str(engine / "servers" / "brain-mcp" / "stdio.py"),)
assert stdio_connection(context) == {
    "transport": "stdio",
    "command": command,
    "args": list(args),
    "env": {},
}

parser = installer.build_parser()
assert parser.parse_args(["stdio-command"]).handler is installer.stdio_command
assert parser.parse_args(["verify-stdio"]).handler is installer.verify_stdio_command
with contextlib.redirect_stderr(io.StringIO()):
    try:
        parser.parse_args(["remote"])
    except SystemExit:
        pass
    else:
        raise AssertionError("network command remains")

without_site_packages = subprocess.run(
    [
        sys.executable,
        "-S",
        str(engine / "install" / "brain_mcp.py"),
        "stdio-command",
    ],
    capture_output=True,
    text=True,
    check=False,
)
assert without_site_packages.returncode == 0, without_site_packages.stderr
assert json.loads(without_site_packages.stdout)["transport"] == "stdio"

with mock.patch(
    "lib.brain_mcp_install._probe_stdio",
    side_effect=TimeoutError,
):
    try:
        verify_stdio(context, timeout_seconds=0)
    except InstallError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("non-positive timeout accepted")

print("brain-mcp installer tests: PASS")
