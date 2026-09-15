#!/usr/bin/env python3
from __future__ import annotations

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
    (directory / "BOOTSTRAP.md").write_text(f"# {directory.name} bootstrap\n")
    (directory / "CONVENTIONS.md").write_text(f"# {directory.name} conventions\n")

bootstrap = se.root_bootstrap()
assert bootstrap == "Read `agents/BOOTSTRAP.md` and `user/BOOTSTRAP.md` relative to this file and follow them.\n"
print("task1 ok")

agents_txt = se.agents_adapter(agents, user)
claude_txt = se.claude_adapter(agents, user)
assert "BOOTSTRAP.md" in agents_txt and "@import" not in agents_txt
assert claude_txt == f"@{agents}/BOOTSTRAP.md\n@{user}/BOOTSTRAP.md\n"
assert agents_txt == f"Read and follow `{agents}/BOOTSTRAP.md` and `{user}/BOOTSTRAP.md`.\n"
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
    env=env,
)
assert r.returncode == 0 and "dry-run" in r.stdout
assert not (brain / "BOOTSTRAP.md").exists()
assert not home.exists()
assert str(codex / "AGENTS.md") in r.stdout
assert str(claude / "CLAUDE.md") in r.stdout
print("task3 ok")

r = subprocess.run(
    [sys.executable, str(ENGINE / "install" / "adapters.py"), "--brain", str(brain), "--apply"],
    text=True,
    capture_output=True,
    env=env,
)
assert r.returncode == 0, r.stderr + r.stdout
assert (brain / "BOOTSTRAP.md").read_text() == bootstrap
assert agents_txt in (codex / "AGENTS.md").read_text()
assert claude_txt in (claude / "CLAUDE.md").read_text()
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
    assert str(home / "moved-brain" / "agents" / "BOOTSTRAP.md") in text
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
assert claude_txt in (custom_claude / "CLAUDE.md").read_text()

# Client environment overrides are honored; flags take precedence.
custom_env = {**env, "CODEX_HOME": "$HOME/env-codex", "CLAUDE_CONFIG_DIR": "${HOME}/env-claude"}
r = run("--apply", environment=custom_env)
assert r.returncode == 0, r.stderr
assert (home / "env-codex" / "AGENTS.md").is_file()
assert (home / "env-claude" / "CLAUDE.md").is_file()
r = run("--codex-home", str(custom_codex), "--claude-home", str(custom_claude), "--apply", environment=custom_env)
assert r.returncode == 0 and r.stdout.strip() == "nothing to do", r.stderr + r.stdout

# Preserve the local router; load exactly one selected user or none.
local_bootstrap = "Read `agents/BOOTSTRAP.md` relative to this file and follow it.\n"
(brain / "BOOTSTRAP.md").write_text(local_bootstrap)
r = run("--no-user", "--apply")
assert r.returncode == 0, r.stderr
assert (brain / "BOOTSTRAP.md").read_text() == local_bootstrap
assert se.agents_adapter(agents) in (codex / "AGENTS.md").read_text()
assert str(user) not in (claude / "CLAUDE.md").read_text()
selected = root / "users" / "selected"
selected.mkdir(parents=True)
(selected / "BOOTSTRAP.md").write_text("Selected user\n")
other = root / "users" / "other"
other.mkdir()
(other / "BOOTSTRAP.md").write_text("Other user\n")
r = run("--user-root", str(selected), "--agents-root", str(agents), "--apply")
assert r.returncode == 0, r.stderr
for path in (codex / "AGENTS.md", claude / "CLAUDE.md"):
    assert str(selected) in path.read_text() and str(other) not in path.read_text()
r = run("--user-root", str(root / "missing-user"), "--apply")
assert r.returncode != 0 and "requires BOOTSTRAP.md" in r.stderr
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

# Malformed blocks fail before any writes, including the root bootstrap.
space_brain = root / "brain with spaces"
r = run("--brain", str(space_brain), "--apply")
assert r.returncode != 0 and "without whitespace" in r.stderr
assert not space_brain.exists()

(claude / "CLAUDE.md").write_text(se.START + "\nunfinished block\n")
missing_brain = root / "not-created"
r = run("--brain", str(missing_brain), "--apply")
assert r.returncode != 0 and "malformed" in r.stderr
assert not missing_brain.exists()
assert (codex / "AGENTS.md").read_bytes() == snapshot[codex / "AGENTS.md"]
print("global adapter regression tests: PASS")

print("session-entry tests: PASS")
