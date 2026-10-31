"""Installer: entry blocks and hooks for Claude Code and Codex."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import install
from brain.__main__ import main

ENGINE = Path(__file__).resolve().parents[1]


def old_groups(engine: str) -> list[dict]:
    return [{"hooks": [{"type": "command", "command": f"{engine}/bin/brain-context session --chunk {n}"}]}
            for n in range(1, 5)]


class InstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.brain, self.claude, self.codex = root / "brain", root / "claude", root / "codex"
        (self.brain / "agents").mkdir(parents=True)
        (self.brain / "agents" / "README.md").write_text("---\nsession:\n---\n\nAgents entry\n")
        (self.brain / "user").mkdir()
        (self.brain / "user" / "README.md").write_text("User entry\n")

    def run_install(self, apply: bool = True) -> list[str]:
        return install.run(self.brain, self.claude, self.codex, apply)

    def hooks(self, path: Path) -> dict:
        return json.loads(path.read_text())["hooks"]

    def test_dry_run_writes_nothing(self):
        report = self.run_install(apply=False)
        self.assertIn("INFO dry-run only; pass --apply to write", report)
        self.assertEqual(sum(line.startswith("WRITE ") for line in report), 4)
        self.assertFalse(self.claude.exists() or self.codex.exists())

    def test_apply_writes_entry_and_one_hook_per_event(self):
        self.run_install()
        for text_file, hooks_file in ((self.claude / "CLAUDE.md", self.claude / "settings.json"),
                                      (self.codex / "AGENTS.md", self.codex / "hooks.json")):
            text = text_file.read_text()
            self.assertIn(install.START, text)
            self.assertIn(f"`{self.brain.resolve()}/agents/README.md` and `{self.brain.resolve()}/user/README.md`", text)
            hooks = self.hooks(hooks_file)
            self.assertEqual([len(hooks["SessionStart"]), len(hooks["SubagentStart"])], [1, 1])
            self.assertIn(" -P -m brain hook session --brain ", hooks["SessionStart"][0]["hooks"][0]["command"])
            self.assertIn(" -P -m brain hook subagent --brain ", hooks["SubagentStart"][0]["hooks"][0]["command"])

    def test_replaces_old_hooks_and_keeps_the_rest(self):
        other = {"hooks": [{"type": "command", "command": "echo mine"}]}
        self.claude.mkdir()
        (self.claude / "settings.json").write_text(json.dumps(
            {"model": "opus", "hooks": {"SessionStart": [other] + old_groups("/x/engine"),
                                        "SubagentStart": old_groups("/x/engine")}}))
        (self.claude / "CLAUDE.md").write_text(f"Mine\n\n{install.START}\nold entry\n{install.END}\nAfter\n")
        self.run_install()
        settings = json.loads((self.claude / "settings.json").read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["hooks"]["SessionStart"][0], other)
        self.assertEqual([len(settings["hooks"]["SessionStart"]), len(settings["hooks"]["SubagentStart"])], [2, 1])
        text = (self.claude / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("Mine\n") and text.endswith("After\n"))
        self.assertNotIn("old entry", text)

    def test_replaces_hooks_from_before_the_flag(self):
        before = {"hooks": [{"type": "command", "command": "/x/python -m brain hook session --brain /x"}]}
        self.claude.mkdir()
        (self.claude / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [before]}}))
        self.run_install()
        groups = self.hooks(self.claude / "settings.json")["SessionStart"]
        self.assertEqual(len(groups), 1)
        self.assertIn(" -P -m brain hook session --brain ", groups[0]["hooks"][0]["command"])

    def test_second_apply_is_a_no_op(self):
        self.run_install()
        self.assertEqual(self.run_install(), ["nothing to do"])

    def test_malformed_block_is_an_error(self):
        self.claude.mkdir()
        (self.claude / "CLAUDE.md").write_text(f"{install.START}\n{install.START}\n{install.END}\n")
        with self.assertRaises(ValueError):
            self.run_install()

    def test_no_user_store_leaves_user_out_of_entry(self):
        (self.brain / "user" / "README.md").unlink()
        self.run_install()
        self.assertNotIn("user/README.md", (self.claude / "CLAUDE.md").read_text())

    def test_missing_agents_store_is_an_error(self):
        (self.brain / "agents" / "README.md").unlink()
        with self.assertRaises(ValueError):
            self.run_install()

    def test_override_file_warns(self):
        self.codex.mkdir()
        (self.codex / "AGENTS.override.md").write_text("local override\n")
        self.assertTrue(any(line.startswith("WARN ") for line in self.run_install()))

    def test_installed_hook_command_runs(self):
        self.run_install()
        command = self.hooks(self.claude / "settings.json")["SessionStart"][0]["hooks"][0]["command"]
        out = self.run_hook(command, ENGINE)
        self.assertIn("Agents entry", out)
        self.assertIn("User entry", out)

    def test_hook_ignores_a_brain_package_in_the_working_directory(self):
        self.run_install()
        project = self.brain.parent / "project"
        (project / "brain").mkdir(parents=True)
        (project / "brain" / "__init__.py").write_text("")
        (project / "brain" / "__main__.py").write_text('print("project code ran")\n')
        command = self.hooks(self.claude / "settings.json")["SessionStart"][0]["hooks"][0]["command"]
        out = self.run_hook(command, project)
        self.assertNotIn("project code ran", out)
        self.assertIn("Agents entry", out)

    def run_hook(self, command: str, cwd: Path) -> str:
        env = dict(os.environ, PYTHONPATH=str(ENGINE))
        return subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


class UnitTest(unittest.TestCase):
    WEB = ("https://brain.example.com", "team.cloudflareaccess.com", "aud-tag", 9000)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.brain, self.claude, self.codex = self.root / "brain", self.root / "claude", self.root / "codex"
        (self.brain / "agents").mkdir(parents=True)
        (self.brain / "agents" / "README.md").write_text("# Agents\n")
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.root / "config" / "systemd" / "user" / "brain.service"

    def run_install(self, apply: bool, web=WEB) -> list[str]:
        return install.run(self.brain, self.claude, self.codex, apply, web)

    def test_no_unit_without_web_settings(self):
        self.run_install(True, None)
        self.assertFalse(self.path.exists())

    def test_dry_run_writes_nothing(self):
        report = self.run_install(False)
        self.assertIn(f"WRITE {self.path}", report)
        self.assertFalse(self.path.exists())

    def test_apply_writes_unit_and_says_how_to_start(self):
        report = self.run_install(True)
        text = self.path.read_text()
        self.assertIn(" -P -m brain serve --brain ", text)
        for value in ("--public-url https://brain.example.com", "--cf-team team.cloudflareaccess.com",
                      "--cf-aud aud-tag", "--port 9000"):
            self.assertIn(value, text)
        self.assertTrue(any("systemctl --user enable --now brain.service" in line for line in report))

    def test_rerun_changes_nothing(self):
        self.run_install(True)
        self.assertEqual(self.run_install(True), ["nothing to do"])

    def test_cli_needs_all_web_flags(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["install", "--brain", str(self.brain), "--claude-home", str(self.claude),
                  "--codex-home", str(self.codex), "--public-url", "https://brain.example.com"])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
