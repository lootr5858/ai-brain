#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from lib import session_entry as se


root = Path(tempfile.mkdtemp()).resolve()
home = root / "home"
env = {**os.environ, "HOME": str(home)}
env.pop("CODEX_HOME", None)
env.pop("CLAUDE_CONFIG_DIR", None)
env.pop("BRAIN_CONFIG", None)
codex = home / ".codex"
claude = home / ".claude"
brain = root / "brain"
engine = brain / "engine"
agents = brain / "agents"
user = brain / "user"
for directory in (engine, agents, user):
    directory.mkdir(parents=True)
    (directory / "README.md").write_text(f"# {directory.name} store\n")
    (directory / "CONVENTIONS.md").write_text(f"# {directory.name} conventions\n")

bootstrap = se.root_bootstrap()
assert bootstrap == "Read `agents/README.md` and `user/README.md` relative to this file and follow them.\n"
print("task1 ok")

entry_txt = se.entry_adapter(agents, user)
assert entry_txt.startswith(f"Brain context: `{agents}/README.md` and `{user}/README.md`, plus")
assert "@" not in entry_txt
assert se.entry_adapter(agents).startswith(f"Brain context: `{agents}/README.md`, plus")
groups = se.hook_groups(ENGINE)
assert [len(groups[event]) for event in ("SessionStart", "SubagentStart")] == [4, 4]
assert groups["SessionStart"][0]["hooks"][0]["command"] == f"{ENGINE}/bin/brain-context session --chunk 1"
assert groups["SubagentStart"][3]["hooks"][0]["command"].endswith("subagent --chunk 4 --json SubagentStart")
for text in (bootstrap, entry_txt):
    assert "CHANGE_ME" not in text
    assert (".private-" + "data") not in text
print("task2 ok")

starter = ENGINE / "templates"
starter_files = [
    starter / "agents" / "README.md",
    starter / "agents" / "core" / "behavior-rules.md",
    starter / "user" / "README.md",
]
assert all(path.is_file() for path in starter_files)
assert all("/home/" not in path.read_text() for path in starter_files)
print("task2b ok")

r = subprocess.run(
    [sys.executable, str(ENGINE / "install" / "adapters.py"), "--brain", str(brain), "--dry-run"],
    text=True,
    capture_output=True,
    env=env,
)
assert r.returncode == 0 and "dry-run" in r.stdout
assert not (brain / "BOOTSTRAP.md").exists()
assert not home.exists()
assert str(codex / "AGENTS.md") in r.stdout
assert str(claude / "CLAUDE.md") in r.stdout
assert str(claude / "settings.json") in r.stdout and str(codex / "hooks.json") in r.stdout
print("task3 ok")

r = subprocess.run(
    [sys.executable, str(ENGINE / "install" / "adapters.py"), "--brain", str(brain), "--apply"],
    text=True,
    capture_output=True,
    env=env,
)
assert r.returncode == 0, r.stderr + r.stdout
assert (brain / "BOOTSTRAP.md").read_text() == bootstrap
assert entry_txt in (codex / "AGENTS.md").read_text()
assert entry_txt in (claude / "CLAUDE.md").read_text()
for path in (codex / "hooks.json", claude / "settings.json"):
    assert json.loads(path.read_text())["hooks"] == groups
assert "trust" in r.stdout
assert not (brain / "AGENTS.md").exists()
assert not (brain / "CLAUDE.md").exists()
print("task4 ok")


def run(*args, environment=env):
    return subprocess.run(
        [sys.executable, str(ENGINE / "install" / "adapters.py"), "--brain", str(brain), *args],
        text=True, capture_output=True, env=environment,
    )


r = run("--apply")
assert r.returncode == 0 and r.stdout.strip() == "nothing to do", r.stderr + r.stdout

# Keep personal text byte-for-byte and update only our block after a move.
for path in (codex / "AGENTS.md", claude / "CLAUDE.md"):
    path.write_text("Personal instructions.\n\n" + path.read_text() + "\nTrailing instructions.")
r = run("--brain", "$HOME/moved-brain", "--apply")
assert r.returncode == 0, r.stderr
for path in (codex / "AGENTS.md", claude / "CLAUDE.md"):
    text = path.read_text()
    assert text.startswith("Personal instructions.\n\n")
    assert text.endswith("\nTrailing instructions.")
    assert str(home / "moved-brain" / "agents" / "README.md") in text
    assert str(brain / "BOOTSTRAP.md") not in text
    assert text.count(se.START) == 1

# Explicit paths expand variables/tilde, even when the shell doesn't expand them.
custom_codex = home / "custom-codex"
custom_claude = home / "custom-claude"
custom_codex.mkdir()
(custom_codex / "AGENTS.md").write_text("Existing preferences without newline")
r = run("--codex-home", "$HOME/custom-codex", "--claude-home", "~/custom-claude", "--apply")
assert r.returncode == 0, r.stderr
assert (custom_codex / "AGENTS.md").read_text().startswith("Existing preferences without newline\n\n")
assert entry_txt in (custom_claude / "CLAUDE.md").read_text()

# Client environment overrides are honored; flags take precedence.
custom_env = {**env, "CODEX_HOME": "$HOME/env-codex", "CLAUDE_CONFIG_DIR": "${HOME}/env-claude"}
r = run("--apply", environment=custom_env)
assert r.returncode == 0, r.stderr
assert (home / "env-codex" / "AGENTS.md").is_file()
assert (home / "env-claude" / "CLAUDE.md").is_file()
r = run("--codex-home", str(custom_codex), "--claude-home", str(custom_claude), "--apply", environment=custom_env)
assert r.returncode == 0 and r.stdout.strip() == "nothing to do", r.stderr + r.stdout

# Preserve the local router; load exactly one selected user or none.
local_bootstrap = "Read `agents/README.md` relative to this file and follow it.\n"
(brain / "BOOTSTRAP.md").write_text(local_bootstrap)
r = run("--no-user", "--apply")
assert r.returncode == 0, r.stderr
assert (brain / "BOOTSTRAP.md").read_text() == local_bootstrap
assert se.entry_adapter(agents) in (codex / "AGENTS.md").read_text()
assert str(user) not in (claude / "CLAUDE.md").read_text()
selected = root / "users" / "selected"
selected.mkdir(parents=True)
(selected / "README.md").write_text("Selected user\n")
other = root / "users" / "other"
other.mkdir()
(other / "README.md").write_text("Other user\n")
r = run("--user-root", str(selected), "--agents-root", str(agents), "--apply")
assert r.returncode == 0, r.stderr
for path in (codex / "AGENTS.md", claude / "CLAUDE.md"):
    assert str(selected) in path.read_text() and str(other) not in path.read_text()
r = run("--user-root", str(root / "missing-user"), "--apply")
assert r.returncode != 0 and "requires README.md" in r.stderr
r = run("--apply")
assert r.returncode == 0, r.stderr

# --dry-run wins over --apply and leaves every planned destination untouched.
snapshot = {path: path.read_bytes() for path in (brain / "BOOTSTRAP.md", codex / "AGENTS.md", claude / "CLAUDE.md")}
r = run("--apply", "--dry-run")
assert r.returncode == 0 and "dry-run" in r.stdout
assert all(path.read_bytes() == content for path, content in snapshot.items())

(codex / "AGENTS.override.md").write_text("Override instructions\n")
r = run()
assert r.returncode == 0 and "takes precedence" in r.stdout

# Hook merging keeps unrelated settings and hooks, and never duplicates ours.
settings = claude / "settings.json"
data = json.loads(settings.read_text())
data["model"] = "kept"
data["hooks"]["SessionStart"].insert(0, {"hooks": [{"type": "command", "command": "echo personal"}]})
data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "echo stop"}]}]
data["hooks"]["SubagentStart"][0]["hooks"][0]["command"] = "/old/engine/bin/brain-context subagent --chunk 1"
settings.write_text(json.dumps(data))
r = run("--apply")
assert r.returncode == 0, r.stderr
data = json.loads(settings.read_text())
assert data["model"] == "kept" and data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo stop"
assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo personal"
assert data["hooks"]["SessionStart"][1:] == groups["SessionStart"]
assert data["hooks"]["SubagentStart"] == groups["SubagentStart"]

# Malformed blocks fail before any writes, including the root bootstrap.

(claude / "CLAUDE.md").write_text(se.START + "\nunfinished block\n")
missing_brain = root / "not-created"
r = run("--brain", str(missing_brain), "--apply")
assert r.returncode != 0 and "malformed" in r.stderr
assert not missing_brain.exists()
assert (codex / "AGENTS.md").read_bytes() == snapshot[codex / "AGENTS.md"]
print("global adapter regression tests: PASS")

print("session-entry tests: PASS")
