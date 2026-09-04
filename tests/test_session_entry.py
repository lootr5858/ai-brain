#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from lib import session_entry as se


root = Path(tempfile.mkdtemp())
brain = root / "brain"
engine = brain / "engine"
agents = brain / "agents"
user = brain / "user"
for directory in (engine, agents, user):
    directory.mkdir(parents=True)
    (directory / "BOOTSTRAP.md").write_text(f"# {directory.name} bootstrap\n")
    (directory / "CONVENTIONS.md").write_text(f"# {directory.name} conventions\n")

bootstrap = se.root_bootstrap()
assert "engine/BOOTSTRAP.md" in bootstrap
assert "agents/BOOTSTRAP.md" in bootstrap
assert "user/BOOTSTRAP.md" in bootstrap
assert "## Always Load" in bootstrap
assert "Pick exactly one repository" not in bootstrap
assert "Do not bulk-load all repositories" in bootstrap
assert "/home/" not in bootstrap
print("task1 ok")

agents_txt = se.agents_adapter()
claude_txt = se.claude_adapter()
assert "BOOTSTRAP.md" in agents_txt and "@import" not in agents_txt
assert "@import ./BOOTSTRAP.md" in claude_txt
for text in (bootstrap, agents_txt, claude_txt):
    assert "CHANGE_ME" not in text
    assert (".private-" + "data") not in text
print("task2 ok")

starter = ENGINE / "templates"
starter_files = [
    starter / "agents" / "BOOTSTRAP.md",
    starter / "agents" / "core" / "behavior-rules.md",
    starter / "user" / "BOOTSTRAP.md",
    starter / "user" / "preferences.md",
    starter / "user" / "context-routing.md",
]
assert all(path.is_file() for path in starter_files)
assert all("/home/" not in path.read_text() for path in starter_files)
print("task2b ok")

r = subprocess.run(
    [sys.executable, str(ENGINE / "install" / "adapters.py"), "--brain", str(brain), "--dry-run"],
    text=True,
    capture_output=True,
)
assert r.returncode == 0 and "dry-run" in r.stdout
assert not (brain / "BOOTSTRAP.md").exists()
print("task3 ok")

r = subprocess.run(
    [sys.executable, str(ENGINE / "install" / "adapters.py"), "--brain", str(brain), "--apply"],
    text=True,
    capture_output=True,
)
assert r.returncode == 0, r.stderr + r.stdout
assert (brain / "BOOTSTRAP.md").read_text() == bootstrap
assert (brain / "AGENTS.md").read_text() == agents_txt
assert (brain / "CLAUDE.md").read_text() == claude_txt
print("task4 ok")

print("session-entry tests: PASS")
