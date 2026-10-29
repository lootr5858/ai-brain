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
(agents / "README.md").write_text("---\nsession:\n  - core/*.md\n---\n# Agent store\nAGENT_ENTRY\n")
(agents / "core" / "behavior-rules.md").write_text("# Behavior rules\nAGENT_MINIMUM\n")
(agents / "skills" / "coding").mkdir(parents=True)
(agents / "skills" / "coding" / "SKILL.md").write_text("# Coding skill\n")
(user / "inventory").mkdir()
(user / "inventory" / "hobbies.yaml").write_text("photography\n")
(user / "career.md").write_text("synthesis engineer\n")
(user / "README.md").write_text("USER_ENTRY_MINIMUM\n")
(user / "preferences.md").write_text("USER_PREFERENCES_LEGACY\n")
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
assert "AGENT_ENTRY" in core
assert "USER_ENTRY_MINIMUM" in core
assert "USER_PREFERENCES_LEGACY" not in core
assert "OPTIONAL_USER_CONTEXT" not in core
assert "agents/README.md" in core and "user/README.md" in core
assert "session:" not in core
assert core.index("AGENT_ENTRY") < core.index("AGENT_MINIMUM") < core.index("USER_ENTRY_MINIMUM")
assert "get_discipline" not in tools() and "get_reference" not in tools()
index = call_tool("list_context", {}, cfg)
assert "coding" in index and "inventory/hobbies.yaml" in index and "private" not in index
assert "Coding skill" in call_tool("get_skill", {"name": "coding"}, cfg)
assert "photography" in call_tool("get_user_context", {"field": "inventory/hobbies.yaml"}, cfg)
assert "synthesis" in call_tool("get_user_context", {"field": "career"}, cfg)
assert "career.md:1" in call_tool("search", {"query": "synthesis"}, cfg)
assert "behavior-rules.md:2" in call_tool("search", {"query": "AGENT_MINIMUM"}, cfg)
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
