"""Context sets: manifest order, dedupe, notes, one output under the budget."""
from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from brain import context
from brain.__main__ import main

MANIFEST = """---
session:
  - core/*.md
  - skills/tone/SKILL.md
  - missing/*.md
subagent:
  - skills/tone/SKILL.md
---

# Agents
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def labels(text: str) -> list[str]:
    return re.findall(r"^# ===== (.+) =====$", text, re.M)


class ContextTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.brain = Path(tmp.name)
        self.agents, self.user = self.brain / "agents", self.brain / "user"
        write(self.agents / "README.md", MANIFEST)
        write(self.agents / "core" / "b.md", "B rules")
        write(self.agents / "core" / "a.md", "A rules")
        write(self.agents / "skills" / "tone" / "SKILL.md", "Tone skill")
        write(self.user / "README.md", "---\nname: x\n---\n\n# User\n")

    def test_session_order_and_frontmatter(self):
        out = context.text(self.agents, self.user, "session")
        self.assertEqual(labels(out), [
            "agents/README.md", "agents/core/a.md", "agents/core/b.md",
            "agents/skills/tone/SKILL.md", "user/README.md", "brain hook notes",
        ])
        self.assertNotIn("session:", out)
        self.assertNotIn("name: x", out)
        self.assertIn("no file matches `missing/*.md`", out)

    def test_subagent_set_has_only_manifest_files(self):
        out = context.text(self.agents, self.user, "subagent")
        self.assertEqual(labels(out), ["agents/skills/tone/SKILL.md"])

    def test_no_user_store(self):
        self.assertNotIn("user/README.md", labels(context.text(self.agents, None, "session")))

    def test_duplicate_matches_load_once(self):
        write(self.agents / "README.md", "---\nsession:\n  - core/a.md\n  - core/*.md\n---\n")
        out = context.text(self.agents, None, "session")
        self.assertEqual(labels(out), ["agents/README.md", "agents/core/a.md", "agents/core/b.md"])

    def test_pattern_outside_store_is_rejected(self):
        write(self.agents / "README.md", "---\nsession:\n  - ../user/README.md\n---\n")
        with self.assertRaises(ValueError):
            context.text(self.agents, None, "session")

    def test_over_budget_skips_and_names_files(self):
        write(self.agents / "core" / "a.md", "x" * context.BUDGET)
        out = context.text(self.agents, self.user, "session")
        self.assertLessEqual(len(out), context.BUDGET)
        self.assertNotIn("agents/core/a.md", labels(out))
        self.assertIn("agents/core/b.md", labels(out))
        self.assertIn("read these directly: `agents/core/a.md`", out)

    def run_hook(self, name: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["hook", name, "--brain", str(self.brain)]), 0)
        return buf.getvalue()

    def test_hook_session_prints_plain_text(self):
        out = self.run_hook("session")
        self.assertTrue(out.startswith("# ===== agents/README.md ====="))

    def test_hook_subagent_prints_hook_json(self):
        data = json.loads(self.run_hook("subagent"))["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], "SubagentStart")
        self.assertIn("Tone skill", data["additionalContext"])


if __name__ == "__main__":
    unittest.main()
