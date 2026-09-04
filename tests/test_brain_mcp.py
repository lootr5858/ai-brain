#!/usr/bin/env python3
"""Read-only brain-mcp domain tests."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.brain_mcp import BrainMcpConfig, ToolError, call_tool, tools


def refuse(function, message: str) -> None:
    try:
        function()
    except ToolError as exc:
        assert message in str(exc), exc
    else:
        raise AssertionError(f"expected ToolError containing {message!r}")


root = Path(tempfile.mkdtemp(prefix="brain-mcp-domain-"))
brain = root / "brain"
engine = brain / "engine"
agents = brain / "agents"
user = brain / "user"
for repo in (engine, agents, user):
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)

(agents / "core").mkdir()
(agents / "BOOTSTRAP.md").write_text("# Agent bootstrap\n")
(agents / "core" / "behavior-rules.md").write_text("# Behavior rules\nAGENT_MINIMUM\n")
(agents / "rules" / "asic-rtl").mkdir(parents=True)
(agents / "rules" / "asic-rtl" / "scope.md").write_text("# ASIC scope\n")
(agents / "skills" / "coding").mkdir(parents=True)
(agents / "skills" / "coding" / "SKILL.md").write_text("# Coding skill\n")
(agents / "reference").mkdir()
(agents / "reference" / "glossary.md").write_text("synthesis\n")
(user / "profile").mkdir()
(user / "profile" / "hobbies.md").write_text("photography\n")
(user / "BOOTSTRAP.md").write_text("# User bootstrap\n")
(user / "preferences.md").write_text("USER_PREFERENCES_MINIMUM\n")
(user / "context-routing.md").write_text("USER_ROUTING_MINIMUM\n")
(user / "optional.md").write_text("OPTIONAL_USER_CONTEXT\n")
(user / "private").mkdir()
(user / "private" / "secret.md").write_text("TOPSECRET\n")
(engine / "README.md").write_text("engine public read\n")
for repo in (engine, agents, user):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

cfg = BrainMcpConfig(brain=brain, engine=engine, agents=agents, user=user)

assert tools() == sorted(tools())
assert "propose_change" not in tools()
assert call_tool("health_check", {}, cfg) == "pong from brain-mcp"
core = call_tool("get_core", {}, cfg)
assert "AGENT_MINIMUM" in core
assert "USER_PREFERENCES_MINIMUM" in core
assert "USER_ROUTING_MINIMUM" in core
assert "OPTIONAL_USER_CONTEXT" not in core
assert "agents/BOOTSTRAP.md" in core and "user/BOOTSTRAP.md" in core
assert "ASIC scope" in call_tool("get_discipline", {"name": "asic-rtl"}, cfg)
assert "Coding skill" in call_tool("get_skill", {"name": "coding"}, cfg)
assert "photography" in call_tool("get_user_context", {"field": "hobbies"}, cfg)
assert "synthesis" in call_tool("get_reference", {"path": "glossary.md"}, cfg)
assert "engine public read" in call_tool(
    "read_nonprivate",
    {"path": "engine/README.md"},
    cfg,
)
assert call_tool("search", {"query": "TOPSECRET"}, cfg).startswith("No matches")
refuse(
    lambda: call_tool(
        "read_nonprivate",
        {"path": "user/private/secret.md"},
        cfg,
    ),
    "not exposed",
)
refuse(lambda: call_tool("propose_change", {}, cfg), "unknown tool")
status = call_tool("status", {}, cfg)
assert '"transport": "stdio"' in status

print("brain-mcp tests: PASS")
