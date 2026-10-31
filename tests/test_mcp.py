"""MCP tools through an in-memory client: discovery, hints, refusals, writes."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import Client

from brain import context, mcp as brain_mcp
from brain.store import MAX_BYTES
from test_store import git, repo

MANIFEST = "---\nsession:\n  - core/*.md\nsubagent:\n  - core/style.md\n---\n\n# Agents\n"
TONE = "---\nname: tone\ndescription: Write in the\n  owner's voice.\n---\n\n# Tone\n"
LINKED = "---\nname: linked\ndescription: >\n  Folded text.\n---\n\n# Linked\n"
TOOLS = {"get_core", "list_skills", "read_skill", "list", "read", "search", "edit", "create"}
WRITES = {"edit", "create"}


class McpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.brain = Path(tmp.name)
        agents, self.user = self.brain / "agents", self.brain / "user"
        for folder in ("core", "skills/tone/refs", "skills/.trash/old", "skills/synced/fetched",
                       "skills/empty", "plugins/p/skills/linked"):
            (agents / folder).mkdir(parents=True)
        self.user.mkdir()
        (agents / "README.md").write_text(MANIFEST)
        (agents / "core" / "style.md").write_text("Rule.\n" * 2000)
        (agents / "skills" / "tone" / "SKILL.md").write_text(TONE)
        (agents / "skills" / "tone" / "refs" / "more.md").write_text("Extra.\n")
        (agents / "skills" / ".trash" / "old" / "SKILL.md").write_text(TONE)
        (agents / "skills" / "synced" / "fetched" / "SKILL.md").write_text(TONE)
        (agents / "plugins" / "p" / "skills" / "linked" / "SKILL.md").write_text(LINKED)
        (agents / "skills" / "linked").symlink_to(agents / "plugins" / "p" / "skills" / "linked")
        (self.user / "README.md").write_text("# Guide\n")
        (self.user / "profile.md").write_text("Name: Sam\n")
        repo(self.user)
        self.server = brain_mcp.server(self.brain)

    async def call(self, client: Client, tool: str, **arguments) -> tuple[str, bool]:
        result = await client.call_tool(tool, arguments)
        return result.content[0].text, bool(result.is_error)

    async def test_tools_and_hints(self):
        async with Client(self.server) as client:
            tools = (await client.list_tools()).tools
        self.assertEqual({t.name for t in tools}, TOOLS)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.annotations.read_only_hint, tool.name not in WRITES)
                self.assertEqual(tool.annotations.destructive_hint, tool.name in WRITES)
                self.assertGreater(len(tool.description), 40)

    async def test_get_core_is_the_whole_set(self):
        async with Client(self.server) as client:
            text, error = await self.call(client, "get_core")
        self.assertFalse(error)
        self.assertEqual(text, context.text(self.brain / "agents", self.user, "session", budget=sys.maxsize))
        self.assertGreater(len(text), context.BUDGET)
        for label in ("agents/README.md", "agents/core/style.md", "user/README.md"):
            self.assertIn(f"# ===== {label} =====", text)

    async def test_skill_discovery(self):
        async with Client(self.server) as client:
            text, _ = await self.call(client, "list_skills")
        self.assertEqual(text.splitlines(), ["linked: Folded text.", "tone: Write in the owner's voice."])

    async def list_skills(self) -> str:
        async with Client(self.server) as client:
            return (await self.call(client, "list_skills"))[0]

    async def test_no_skills_folder(self):
        shutil.rmtree(self.brain / "agents" / "skills")
        self.assertEqual(await self.list_skills(), "No skills.")

    async def test_skill_without_frontmatter_has_no_description(self):
        (self.brain / "agents" / "skills" / "tone" / "SKILL.md").write_text("# Tone\n")
        self.assertIn("tone: ", (await self.list_skills()).splitlines())

    async def test_unreadable_skill_left_out(self):
        (self.brain / "agents" / "skills" / "tone" / "SKILL.md").write_text("x" * (MAX_BYTES + 1))
        self.assertEqual((await self.list_skills()).splitlines(), ["linked: Folded text."])

    async def test_read_skill_lists_supporting_files(self):
        async with Client(self.server) as client:
            text, _ = await self.call(client, "read_skill", name="tone")
            self.assertIn("# Tone", text)
            self.assertIn("- refs/more.md", text)
            text, _ = await self.call(client, "read_skill", name="tone", path="refs/more.md")
            self.assertEqual(text, "Extra.\n")
            text, _ = await self.call(client, "read_skill", name="linked")
            self.assertEqual(text, LINKED)

    async def test_read_skill_refusals_reach_the_model(self):
        async with Client(self.server) as client:
            for name, path in (("synced", "SKILL.md"), ("nope", "SKILL.md"), ("../core", "SKILL.md"), ("tone", "../../README.md")):
                with self.subTest(name=name, path=path):
                    text, error = await self.call(client, "read_skill", name=name, path=path)
                    self.assertTrue(error)
                    self.assertTrue("no skill named" in text or "not a path inside" in text, text)

    async def test_user_reads(self):
        async with Client(self.server) as client:
            text, _ = await self.call(client, "list")
            self.assertEqual(text.splitlines(), ["README.md (8 bytes)", "profile.md (10 bytes)"])
            self.assertEqual((await self.call(client, "read", path="profile.md"))[0], "Name: Sam\n")
            self.assertEqual((await self.call(client, "search", query="sam"))[0], "profile.md:1: Name: Sam")
            self.assertEqual((await self.call(client, "search", query="nothing here"))[0], "No matches.")

    async def test_user_store_reaches_no_agent_file(self):
        async with Client(self.server) as client:
            text, error = await self.call(client, "read", path="../agents/README.md")
        self.assertTrue(error)
        self.assertIn("not a path inside", text)

    async def test_writes_commit(self):
        async with Client(self.server) as client:
            text, error = await self.call(client, "edit", path="profile.md", old="Sam", new="Alex")
            self.assertFalse(error, text)
            self.assertIn(git(self.user, "rev-parse", "--short", "HEAD"), text)
            text, error = await self.call(client, "create", path="notes.md", content="New.\n")
            self.assertFalse(error, text)
        self.assertEqual(git(self.user, "log", "--format=%s").splitlines(), ["create notes.md", "edit profile.md", "start"])

    async def test_write_refusal_reaches_the_model(self):
        async with Client(self.server) as client:
            text, error = await self.call(client, "edit", path="profile.md", old="absent", new="x")
        self.assertTrue(error)
        self.assertIn("exactly once", text)


if __name__ == "__main__":
    unittest.main()
