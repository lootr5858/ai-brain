#!/usr/bin/env python3
"""Shared configuration loader tests."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import tomllib
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import config
from lib.brain_mcp import load_config as load_mcp_config
from lib.brain_mcp_install import discover_context, expected_stdio_command, validate_context
from lib.ops import load_ops_config


example_root = Path(tempfile.mkdtemp(prefix="brain-config-example-"))
example = example_root / "brain.config.toml"
example.write_text((Path(__file__).resolve().parents[1] / "brain.config.toml.example").read_text())
example_paths = config.load_config(example)["paths"]
assert Path(example_paths["brain"]) == example_root
assert Path(example_paths["engine"]) == example_root / "engine"
assert Path(example_paths["agents"]) == example_root / "agents"
assert Path(example_paths["user"]) == example_root / "user"


root = Path(tempfile.mkdtemp(prefix="brain-config-"))
config_dir = root / "config dir"
config_dir.mkdir()
home = root / "home"
home.mkdir()
first = config_dir / "first.toml"
first.write_text(
    """
[paths]
brain = "../brain root"
engine = "../engine checkout"
agents = "../agent store"
user = "~/user store"

[engine.ops.dashboard]
port = 9000

[engine.fanout.targets]
relative = "../fanout target"
home = "~/fanout home"
"""
)
second = config_dir / "second.toml"
second.write_text('[paths]\nbrain = "../second brain"\n')

with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=True):
    loaded = config.load_config(first)
    assert Path(loaded["paths"]["brain"]) == root / "brain root"
    assert Path(loaded["paths"]["engine"]) == root / "engine checkout"
    assert Path(loaded["paths"]["agents"]) == root / "agent store"
    assert Path(loaded["paths"]["user"]) == home / "user store"
    assert Path(loaded["engine"]["fanout"]["targets"]["relative"]) == root / "fanout target"
    assert Path(loaded["engine"]["fanout"]["targets"]["home"]) == home / "fanout home"

    os.environ[config.CONFIG_ENV] = str(first)
    assert config.load_config()["paths"] == loaded["paths"]
    assert Path(config.load_config(second)["paths"]["brain"]) == root / "second brain"

    mcp = load_mcp_config()
    ops = load_ops_config()
    assert (mcp.brain, mcp.engine, mcp.agents, mcp.user) == (
        ops.brain,
        ops.engine,
        ops.agents,
        ops.user,
    )
    assert ops.dashboard_port == 9000

    (root / "agent store").mkdir()
    (home / "user store").mkdir()
    portable_engine = root / "engine checkout"
    (portable_engine / ".venv/bin").mkdir(parents=True)
    (portable_engine / ".venv/bin/python").symlink_to(sys.executable)
    (portable_engine / "servers/brain-mcp").mkdir(parents=True)
    (portable_engine / "servers/brain-mcp/stdio.py").write_text("")
    context = discover_context(first)
    validate_context(context)
    assert context.agents == root / "agent store"
    assert context.user == home / "user store"
    assert expected_stdio_command(context)[1][-2:] == ("--config", str(first))

for missing in (root / "missing.toml",):
    try:
        config.load_config(missing)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("explicit missing config was accepted")

invalid = root / "invalid.toml"
invalid.write_text("invalid = [")
try:
    config.load_config(invalid)
except tomllib.TOMLDecodeError:
    pass
else:
    raise AssertionError("invalid TOML was accepted")

compatibility = root / "compatibility"
(compatibility / "engine").mkdir(parents=True)
(compatibility / "brain.config.toml").write_text("[paths]\n")
with mock.patch.object(config, "ENGINE", compatibility / "engine"):
    assert config.config_path() == compatibility / "brain.config.toml"
    assert Path(config.load_config()["paths"]["brain"]) == compatibility

print("config tests: PASS")
