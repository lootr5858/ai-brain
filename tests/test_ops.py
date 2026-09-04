#!/usr/bin/env python3
"""Ops tests - plain asserts, run: python3 tests/test_ops.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ops import (
    backup_action,
    backup_health,
    backup_manifest,
    collect_status,
    content_digest,
    load_ops_config,
    proposal_summary,
    service_action,
    service_status,
    system_status,
    user_delete,
    user_move,
    user_write,
)


root = Path(tempfile.mkdtemp())
brain = root / "brain"
engine = brain / "engine"
agents = brain / "agents"
user = brain / "user"
for directory in (engine, agents, user):
    directory.mkdir(parents=True)
    subprocess.run(["git", "-C", str(directory), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(directory), "config", "user.name", "Ops Test"], check=True)
    subprocess.run(["git", "-C", str(directory), "config", "user.email", "ops@example.invalid"], check=True)
    (directory / "README.md").write_text("ok\n")
    subprocess.run(["git", "-C", str(directory), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(directory), "commit", "-qm", "init"], check=True)

cfg_path = brain / "brain.config.toml"
cfg_path.write_text(f"""
[paths]
brain = "{brain}"
engine = "{engine}"
agents = "{agents}"
user = "{user}"

[engine.ops.dashboard]
public_origin = "https://brain.example"
embedding_url = "http://127.0.0.1:1234/v1/embeddings"
embedding_model = "text-embedding-model"
""")

cfg = load_ops_config(cfg_path)
assert cfg.brain == brain
assert cfg.dashboard_host == "127.0.0.1"
assert cfg.dashboard_port == 8765
assert cfg.dashboard_public_origin == "https://brain.example"
assert cfg.dashboard_embedding_url == "http://127.0.0.1:1234/v1/embeddings"
assert cfg.dashboard_embedding_model == "text-embedding-model"
assert cfg.notify_command == ""
assert cfg.personal_sync_target == ""
try:
    load_ops_config(brain / "missing.config.toml")
except FileNotFoundError:
    pass
else:
    raise AssertionError("explicit missing config was accepted")
print("task1 ok")

proposals = agents / "homes" / "codex" / "proposals"
proposals.mkdir(parents=True)
(proposals / ".gitkeep").write_text("")
(proposals / "one.md").write_text("do not read this\n")
(proposals / "two.md").write_text("or this\n")
buckets = proposal_summary(agents)
assert len(buckets) == 1
assert buckets[0].agent == "codex" and buckets[0].count == 2 and buckets[0].oldest_mtime
print("task2 ok")

(user / "profile").mkdir()
(user / "profile" / "bio.md").write_text("bio\n")
(user / "profile" / "api-token.md").write_text("nope\n")
manifest = backup_manifest(cfg)
assert manifest.rejected_secret_paths == 1
text = str(manifest.as_dict()).lower()
assert "api-token" not in text and "bio.md" not in text
assert manifest.user_categories["profile"] == 1
status = collect_status(cfg)
assert set(status) == {"repos", "proposals", "backup", "user_revision"}
assert all("dirty_paths" in state for state in status["repos"].values())
print("task3 ok")

result = user_write("preferences.md", "x\n", user, "")
assert result.allowed and (user / "preferences.md").read_text() == "x\n"
bio_digest = content_digest(user / "profile" / "bio.md")
result = user_write("profile/bio.md", "new bio\n", user, bio_digest)
assert result.allowed and (user / "profile" / "bio.md").read_text() == "new bio\n"
stale = user_write("profile/bio.md", "stale overwrite\n", user, bio_digest)
assert not stale.allowed and "stale content" in stale.reason
assert (user / "profile" / "bio.md").read_text() == "new bio\n"

private = user_write("private/x.md", "x\n", user, "")
assert private.allowed and (user / "private" / "x.md").read_text() == "x\n"
routing = user_write("context-routing.md", "routes\n", user, "")
assert routing.allowed and (user / "context-routing.md").read_text() == "routes\n"
category = user_write("profile", "x\n", user, "")
assert not category.allowed and "not an ops-writable user path" in category.reason
outside = user_write(agents / "outside.md", "x\n", user, "")
assert not outside.allowed and not (agents / "outside.md").exists()

target = user / "profile" / "linked.md"
target.symlink_to(user / "profile" / "bio.md")
linked = user_write(target, "no\n", user, content_digest(user / "profile" / "bio.md"))
assert not linked.allowed and "symlink" in linked.reason
assert (user / "profile" / "bio.md").read_text() == "new bio\n"
dangling = user / "profile" / "dangling.md"
dangling.symlink_to(user / "profile" / "missing.md")
try:
    content_digest(dangling)
except ValueError:
    pass
else:
    raise AssertionError("dangling symlink digest should fail")

real_directory = user / "profile" / "real"
real_directory.mkdir()
alias_directory = user / "profile" / "alias"
alias_directory.symlink_to(real_directory, target_is_directory=True)
ancestor_link = user_write(alias_directory / "through-link.md", "no\n", user, "")
assert not ancestor_link.allowed and "symlink" in ancestor_link.reason
assert not (real_directory / "through-link.md").exists()

move_digest = content_digest(user / "private" / "x.md")
moved = user_move("private/x.md", "projects/archive/x.md", user, move_digest)
assert moved.allowed and not (user / "private" / "x.md").exists()
assert (user / "projects" / "archive" / "x.md").read_text() == "x\n"
deleted = user_delete("projects/archive/x.md", user, move_digest)
assert deleted.allowed and not (user / "projects" / "archive" / "x.md").exists()
print("task4 ok")


class FakeSystemctl:
    def __init__(self):
        self.active = True
        self.calls = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        if "show" in command:
            active = "active" if self.active else "inactive"
            substate = "waiting" if self.active else "dead"
            stdout = (
                "LoadState=loaded\nUnitFileState=enabled\n"
                f"ActiveState={active}\nSubState={substate}\n"
                "Result=success\nExecMainStatus=0\n"
                "StateChangeTimestamp=now\n"
            )
        else:
            if "stop" in command:
                self.active = False
            elif "start" in command or "restart" in command:
                self.active = True
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


fake_systemctl = FakeSystemctl()
backup_service = service_status("backup", run=fake_systemctl)
assert backup_service["status"] == "healthy"
assert backup_service["actions"] == ["start", "stop", "restart"]
assert service_status("brain_mcp", run=fake_systemctl)["actions"] == [
    "start", "stop", "restart",
]
assert service_status("dashboard", run=fake_systemctl)["actions"] == []

stopped = service_action("backup", "stop", run=fake_systemctl)
assert stopped["command_ok"]
assert stopped["before"]["active"] and not stopped["after"]["active"]
try:
    service_action("dashboard", "restart", run=fake_systemctl)
except ValueError:
    pass
else:
    raise AssertionError("dashboard action must be rejected")

health = system_status(cfg, run=fake_systemctl)
assert set(health) == {"observed_time", "repositories", "proposals", "services"}
assert [item["name"] for item in health["repositories"]] == ["engine", "agents", "user"]
assert [item["id"] for item in health["services"]] == [
    "dashboard", "brain_mcp", "backup",
]
assert sum(bucket["count"] for bucket in health["proposals"]["buckets"]) == 2
print("task5 ok")

cache = root / "cache"
cache.mkdir()
success_marker = cache / "backup.OK"
success_marker.write_text("ok\n")
now = datetime(2026, 8, 26, tzinfo=timezone.utc)
boundary = now.timestamp() - 36 * 60 * 60
os.utime(success_marker, (boundary, boundary))
assert backup_health(cfg, now=now, cache=cache)["status"] == "healthy"
assert backup_health(cfg, now=now.replace(microsecond=1), cache=cache)["status"] == "unhealthy"
failure_marker = cache / "backup.FAILED"
failure_marker.write_text("failed\n")
os.utime(failure_marker, (now.timestamp(), now.timestamp()))
assert backup_health(cfg, now=now, cache=cache)["reason"] == "last backup failed"

backup_calls = []


def fake_backup_action(command, **_kwargs):
    backup_calls.append(command)
    return subprocess.CompletedProcess(command, 0, "", "")


assert backup_action(cfg, "run", run=fake_backup_action)["command_ok"]
assert backup_calls[-1] == ["systemctl", "--user", "start", "brain-user-backup.service"]
assert backup_action(cfg, "stage", run=fake_backup_action)["command_ok"]
assert "systemd-run" in backup_calls[-1] and backup_calls[-1][-1] == "stage"
assert "--unit=brain-user-backup-control" in backup_calls[-1]
assert any(item.startswith("--setenv=PATH=") for item in backup_calls[-1])
try:
    backup_action(cfg, "replace", run=fake_backup_action)
except ValueError:
    pass
else:
    raise AssertionError("backup replacement must require confirmation")
assert backup_action(cfg, "replace", confirmed=True, run=fake_backup_action)["command_ok"]
assert backup_calls[-1][-1] == "--confirm-replacement"
print("task6 ok")

print("ops tests: PASS")
