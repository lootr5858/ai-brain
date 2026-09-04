#!/usr/bin/env python3
"""Dashboard editing tests - plain asserts, run directly."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.editing import EditingError, UserEditing
from dashboard.server import open_repository_file
from lib.ops import OpsConfig, content_digest


ROOT = Path(tempfile.mkdtemp())
USER = ROOT / "user"
ENGINE = ROOT / "engine"
AGENTS = ROOT / "agents"
for directory in (USER, ENGINE, AGENTS):
    directory.mkdir()
subprocess.run(["git", "-C", str(USER), "init", "-q"], check=True)
subprocess.run(["git", "-C", str(USER), "config", "user.name", "Editing Test"], check=True)
subprocess.run(
    ["git", "-C", str(USER), "config", "user.email", "editing@example.invalid"], check=True
)


def write(path: str, content: str) -> None:
    target = USER / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


write("profile/edit.md", "# Edit\n\nBefore.\n")
write("profile/dirty.md", "clean\n")
write("profile/rename.md", "rename\n")
write("knowledge/move.md", "move\n")
write(
    "knowledge/archive.md",
    "---\nschema: brain-user/v1\nsensitivity: personal\nload: on-context\nstatus: active\n---\n# Archive\n",
)
write("knowledge/delete.md", "delete\n")
write("inventory/data.yaml", "name: value\n")
write("reports/app/generated.md", "generated\n")
write("profile/staged.md", "base staged\n")
write("profile/unrelated.md", "base dirty\n")
subprocess.run(["git", "-C", str(USER), "add", "."], check=True)
subprocess.run(["git", "-C", str(USER), "commit", "-qm", "fixtures"], check=True)

write("profile/staged.md", "staged elsewhere\n")
subprocess.run(["git", "-C", str(USER), "add", "profile/staged.md"], check=True)
write("profile/unrelated.md", "dirty elsewhere\n")
write("profile/dirty.md", "dirty target\n")

CONFIG = OpsConfig(brain=ROOT, engine=ENGINE, agents=AGENTS, user=USER)
editing = UserEditing(CONFIG, open_repository_file)


def error(call) -> EditingError:
    try:
        call()
    except EditingError as exc:
        return exc
    raise AssertionError("expected EditingError")


def assert_commit(message: str, paths: set[str]) -> str:
    commit = subprocess.check_output(
        ["git", "-C", str(USER), "rev-parse", "HEAD"], text=True
    ).strip()
    assert subprocess.check_output(
        ["git", "-C", str(USER), "show", "-s", "--format=%s", commit], text=True
    ).strip() == message
    changed = set(subprocess.check_output(
        ["git", "-C", str(USER), "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        text=True,
    ).splitlines())
    assert changed == paths
    assert subprocess.check_output(
        ["git", "-C", str(USER), "diff", "--cached", "--name-only"], text=True
    ).splitlines() == ["profile/staged.md"]
    assert (USER / "profile/unrelated.md").read_text() == "dirty elsewhere\n"
    return commit


preview = editing.preview("# Safe\n\n<script>alert(1)</script>\n")
assert "<h1>Safe</h1>" in preview["rendered_html"]
assert "&lt;script&gt;" in preview["rendered_html"]
assert error(lambda: editing.preview("bad\0content")).code == "invalid_content"

stale = error(lambda: editing.edit_page("profile/edit.md", "changed\n", "0" * 64))
assert stale.code == "conflict" and stale.result["partial"] is False
dirty = error(lambda: editing.edit_page("profile/dirty.md", "changed\n", content_digest(USER / "profile/dirty.md")))
assert dirty.code == "dirty_target"
assert error(lambda: editing.create_page("reports/app/new.md", "no\n")).code == "invalid_target"
assert error(lambda: editing.create_page("profile/bad.txt", "no\n")).code == "invalid_target"

before = content_digest(USER / "profile/edit.md")
result = editing.edit_page("profile/edit.md", "# Edit\n\nAfter.\n", before)
edit_commit = assert_commit("edit: user/profile/edit.md", {"profile/edit.md"})
assert result["commit"] == edit_commit and result["partial"] is False
undo = editing.undo_page("profile/edit.md", result["digest"])
assert (USER / "profile/edit.md").read_text() == "# Edit\n\nBefore.\n"
assert undo["undone_commit"] == edit_commit
assert_commit("edit: user/profile/edit.md", {"profile/edit.md"})

created = editing.create_page("private/notes/new.md", "# New\n")
create_commit = assert_commit("create: user/private/notes/new.md", {"private/notes/new.md"})
assert created["commit"] == create_commit
editing.undo_page("private/notes/new.md", created["digest"])
assert not (USER / "private/notes/new.md").exists()
assert_commit("delete: user/private/notes/new.md", {"private/notes/new.md"})

renamed = editing.rename_page(
    "profile/rename.md", "profile/renamed.md", content_digest(USER / "profile/rename.md")
)
rename_commit = assert_commit(
    "rename: user/profile/renamed.md", {"profile/rename.md", "profile/renamed.md"}
)
assert renamed["commit"] == rename_commit
restored = editing.undo_page("profile/renamed.md", renamed["digest"])
assert restored["path"] == "profile/rename.md" and (USER / "profile/rename.md").exists()
assert_commit("rename: user/profile/rename.md", {"profile/rename.md", "profile/renamed.md"})

moved = editing.move_page(
    "knowledge/move.md", "projects/demo/move.md", content_digest(USER / "knowledge/move.md")
)
move_commit = assert_commit(
    "move: user/projects/demo/move.md", {"knowledge/move.md", "projects/demo/move.md"}
)
assert moved["commit"] == move_commit
restored = editing.undo_page("projects/demo/move.md", moved["digest"])
assert restored["path"] == "knowledge/move.md" and (USER / "knowledge/move.md").exists()
assert_commit("move: user/knowledge/move.md", {"knowledge/move.md", "projects/demo/move.md"})

archive_digest = content_digest(USER / "knowledge/archive.md")
archived = editing.archive_page("knowledge/archive.md", archive_digest, "profile/edit.md")
archive_commit = assert_commit("archive: user/knowledge/archive.md", {"knowledge/archive.md"})
archive_text = (USER / "knowledge/archive.md").read_text()
assert "status: superseded" in archive_text
assert "superseded_by: profile/edit.md" in archive_text
assert archived["commit"] == archive_commit
editing.undo_page("knowledge/archive.md", archived["digest"])
assert "status: active" in (USER / "knowledge/archive.md").read_text()
assert_commit("edit: user/knowledge/archive.md", {"knowledge/archive.md"})
assert error(lambda: editing.archive_page(
    "inventory/data.yaml", content_digest(USER / "inventory/data.yaml")
)).code == "archive_unsupported"
assert error(lambda: editing.archive_page(
    "knowledge/archive.md", content_digest(USER / "knowledge/archive.md"), "knowledge/archive.md"
)).code == "invalid_replacement"

delete_digest = content_digest(USER / "knowledge/delete.md")
deleted = editing.delete_page("knowledge/delete.md", delete_digest)
delete_commit = assert_commit("delete: user/knowledge/delete.md", {"knowledge/delete.md"})
assert deleted["commit"] == delete_commit and not (USER / "knowledge/delete.md").exists()
restored = editing.undo_page("knowledge/delete.md", "")
assert restored["path"] == "knowledge/delete.md" and (USER / "knowledge/delete.md").read_text() == "delete\n"
assert_commit("create: user/knowledge/delete.md", {"knowledge/delete.md"})

hook = USER / ".git" / "hooks" / "pre-commit"
hook.write_text("#!/bin/sh\nexit 1\n")
hook.chmod(0o755)
failed_digest = content_digest(USER / "inventory/data.yaml")
failed = error(lambda: editing.edit_page("inventory/data.yaml", "name: changed\n", failed_digest))
assert failed.code == "commit_failed" and failed.result == {
    "partial": True,
    "paths": ["inventory/data.yaml"],
    "commit": "",
}
assert (USER / "inventory/data.yaml").read_text() == "name: changed\n"
assert "inventory/data.yaml" in subprocess.check_output(
    ["git", "-C", str(USER), "status", "--porcelain"], text=True
)
hook.unlink()

linked = USER / "profile" / "linked.md"
linked.symlink_to(USER / "profile" / "edit.md")
assert error(lambda: editing.edit_page("profile/linked.md", "no\n", "")).code in {
    "dirty_target", "invalid_target"
}

print("dashboard editing tests: PASS")
