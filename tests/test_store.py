"""Store: paths stay inside, text only, one commit per write, hand edits kept apart, versions restorable."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from brain.store import MAX_BYTES, MAX_HISTORY, MAX_HITS, Conflict, Store, StoreError, digest


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def repo(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "start")


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "user"
        (self.root / "inventory").mkdir(parents=True)
        (self.root / "README.md").write_text("# Guide\n\nName: Sam\n")
        (self.root / "inventory" / "hardware.yaml").write_text("- laptop\n- phone\n")
        repo(self.root)
        self.store = Store(self.root)

    def subjects(self) -> list[str]:
        return git(self.root, "log", "--format=%s").splitlines()


class PathTest(StoreCase):
    def test_outside_paths_refused(self):
        (self.base / "secret.md").write_text("secret\n")
        for rel in ("../secret.md", "/etc/passwd", "inventory/../../secret.md", "a\nb.md"):
            with self.subTest(rel=rel), self.assertRaises(StoreError):
                self.store.read(rel)

    def test_symlink_out_of_store_refused(self):
        (self.base / "secret.md").write_text("secret\n")
        (self.root / "link.md").symlink_to(self.base / "secret.md")
        with self.assertRaises(StoreError):
            self.store.read("link.md")

    def test_hidden_refused_even_through_a_symlink(self):
        for rel in (".git/config", "inventory/.hidden"):
            with self.subTest(rel=rel), self.assertRaises(StoreError):
                self.store.read(rel)
        (self.root / "link").symlink_to(self.root / ".git" / "config")
        with self.assertRaises(StoreError):
            self.store.read("link")

    def test_symlink_inside_store_followed(self):
        (self.root / "link.md").symlink_to(self.root / "README.md")
        self.assertIn("Sam", self.store.read("link.md"))


class ReadTest(StoreCase):
    def test_list_skips_git_and_hidden(self):
        (self.root / ".note").write_text("x")
        self.assertEqual([rel for rel, _ in self.store.list()], ["README.md", "inventory/hardware.yaml"])

    def test_list_folder_and_size(self):
        self.assertEqual(self.store.list("inventory"), [("inventory/hardware.yaml", 17)])

    def test_list_missing(self):
        with self.assertRaises(StoreError):
            self.store.list("nope")

    def test_binary_and_oversize_refused(self):
        (self.root / "photo.bin").write_bytes(b"\xff\xfe\x00")
        (self.root / "big.md").write_text("x" * (MAX_BYTES + 1))
        for rel in ("photo.bin", "big.md"):
            with self.subTest(rel=rel), self.assertRaises(StoreError):
                self.store.read(rel)

    def test_search_case_insensitive_with_line(self):
        self.assertEqual(self.store.search("LAPTOP"), ["inventory/hardware.yaml:1: - laptop"])

    def test_search_skips_binary_and_stops_at_cap(self):
        (self.root / "photo.bin").write_bytes(b"\xff\xfelaptop")
        (self.root / "many.md").write_text("laptop\n" * (MAX_HITS + 5))
        self.assertEqual(len(self.store.search("laptop")), MAX_HITS)

    def test_search_needs_two_characters(self):
        with self.assertRaises(StoreError):
            self.store.search(" a ")


class WriteTest(StoreCase):
    def test_edit_one_commit_for_the_path(self):
        commit = self.store.edit("README.md", "Sam", "Alex", "via claude.ai")
        self.assertIn("Alex", self.store.read("README.md"))
        self.assertEqual(git(self.root, "rev-parse", "--short", "HEAD"), commit)
        self.assertEqual(self.subjects(), ["edit README.md", "start"])
        self.assertEqual(git(self.root, "log", "-1", "--format=%b"), "via claude.ai")
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), "README.md")

    def test_edit_needs_exactly_one_match(self):
        (self.root / "two.md").write_text("same\nsame\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "two")
        for path, old in (("two.md", "same"), ("README.md", "absent"), ("README.md", "")):
            with self.subTest(old=old), self.assertRaises(StoreError):
                self.store.edit(path, old, "new")
        self.assertEqual(len(self.subjects()), 2)

    def test_edit_same_text_refused(self):
        with self.assertRaises(StoreError):
            self.store.edit("README.md", "Sam", "Sam")
        self.assertEqual(len(self.subjects()), 1)

    def test_hand_edits_recorded_first(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "Hand: yes\n")
        self.store.edit("README.md", "Sam", "Alex")
        self.assertEqual(self.subjects(), ["edit README.md", "record hand edits to README.md", "start"])
        self.assertNotIn("+Hand", git(self.root, "show", "HEAD"))
        self.assertIn("+Hand: yes", git(self.root, "show", "HEAD~1"))
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_other_dirty_paths_stay_out(self):
        other = self.root / "inventory" / "hardware.yaml"
        other.write_text(other.read_text() + "- tablet\n")
        self.store.edit("README.md", "Sam", "Alex")
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), "README.md")
        self.assertEqual(git(self.root, "status", "--porcelain"), "M inventory/hardware.yaml")

    def test_create_commits_new_file(self):
        self.store.create("inventory/books.yaml", "- one\n", "via chatgpt.com")
        self.assertEqual(self.subjects(), ["create inventory/books.yaml", "start"])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_create_refuses_existing_and_missing_folder(self):
        for rel in ("README.md", "new/file.md", "", ".hidden.md"):
            with self.subTest(rel=rel), self.assertRaises(StoreError):
                self.store.create(rel, "x\n")
        self.assertEqual(len(self.subjects()), 1)

    def test_oversize_write_refused(self):
        with self.assertRaises(StoreError):
            self.store.create("big.md", "x" * (MAX_BYTES + 1))
        with self.assertRaises(StoreError):
            self.store.edit("README.md", "Sam", "x" * (MAX_BYTES + 1))
        self.assertIn("Sam", self.store.read("README.md"))

    def test_magic_pathspec_taken_literally(self):
        self.store.create(":(glob)x.md", "x\n")
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), ":(glob)x.md")

    def test_failed_commit_restores_file(self):
        (self.root / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (self.root / ".git" / "hooks" / "pre-commit").chmod(0o755)
        before = self.store.read("README.md")
        with self.assertRaises(StoreError):
            self.store.edit("README.md", "Sam", "Alex")
        with self.assertRaises(StoreError):
            self.store.create("new.md", "x\n")
        self.assertEqual(self.store.read("README.md"), before)
        self.assertFalse((self.root / "new.md").exists())


class ReplaceTest(StoreCase):
    def opened(self, rel: str = "README.md") -> str:
        return digest(self.store.read(rel))

    def test_saves_whole_file_as_one_commit(self):
        commit = self.store.replace("README.md", "# New\n", self.opened(), "via dashboard")
        self.assertEqual(self.store.read("README.md"), "# New\n")
        self.assertEqual(git(self.root, "rev-parse", "--short", "HEAD"), commit)
        self.assertEqual(self.subjects(), ["edit README.md", "start"])
        self.assertEqual(git(self.root, "log", "-1", "--format=%b"), "via dashboard")

    def test_stale_base_is_a_conflict(self):
        base = self.opened()
        self.store.edit("README.md", "Sam", "Alex", "via claude.ai")
        with self.assertRaises(Conflict):
            self.store.replace("README.md", "# Mine\n", base)
        self.assertIn("Alex", self.store.read("README.md"))
        self.assertEqual(len(self.subjects()), 2)

    def test_hand_edit_after_opening_is_a_conflict(self):
        base = self.opened()
        path = self.root / "README.md"
        path.write_text(path.read_text() + "Hand: yes\n")
        with self.assertRaises(Conflict):
            self.store.replace("README.md", "# Mine\n", base)
        self.assertIn("Hand: yes", self.store.read("README.md"))

    def test_hand_edits_recorded_first(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "Hand: yes\n")
        self.store.replace("README.md", "# Mine\n", self.opened())
        self.assertEqual(self.subjects(), ["edit README.md", "record hand edits to README.md", "start"])
        self.assertIn("+Hand: yes", git(self.root, "show", "HEAD~1"))

    def test_no_change_oversize_and_missing_refused(self):
        for rel, text, base in (("README.md", self.store.read("README.md"), self.opened()),
                                ("README.md", "x" * (MAX_BYTES + 1), self.opened()),
                                ("absent.md", "x", "0")):
            with self.subTest(rel=rel, size=len(text)), self.assertRaises(StoreError):
                self.store.replace(rel, text, base)
        self.assertEqual(len(self.subjects()), 1)

    def test_failed_commit_restores_file(self):
        (self.root / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (self.root / ".git" / "hooks" / "pre-commit").chmod(0o755)
        before = self.store.read("README.md")
        with self.assertRaises(StoreError):
            self.store.replace("README.md", "# Mine\n", self.opened())
        self.assertEqual(self.store.read("README.md"), before)


class HistoryTest(StoreCase):
    def test_lists_this_files_commits_newest_first(self):
        self.store.edit("README.md", "Sam", "Alex", "via claude.ai")
        self.store.create("other.md", "x\n")
        rows = self.store.history("README.md")
        self.assertEqual([row["subject"] for row in rows], ["edit README.md", "start"])
        self.assertEqual([row["via"] for row in rows], ["claude.ai", ""])
        self.assertEqual(rows[0]["sha"], git(self.root, "rev-parse", "--short", "HEAD~1"))
        self.assertRegex(rows[0]["date"], r"^\d{4}-\d{2}-\d{2}T")

    def test_stops_at_the_cap(self):
        path = self.root / "README.md"
        for number in range(MAX_HISTORY + 1):
            path.write_text(f"{number}\n")
            git(self.root, "commit", "-qam", f"change {number}")
        self.assertEqual(len(self.store.history("README.md")), MAX_HISTORY)

    def test_diff_of_a_commit_covers_only_this_file(self):
        (self.root / "README.md").write_text("# New\n")
        (self.root / "other.md").write_text("x\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "both")
        diff = self.store.diff("README.md", git(self.root, "rev-parse", "--short", "HEAD"))
        self.assertIn("+# New", diff)
        self.assertNotIn("other.md", diff)

    def test_diff_without_a_commit_is_uncommitted(self):
        self.assertEqual(self.store.diff("README.md"), "")
        path = self.root / "README.md"
        path.write_text(path.read_text() + "Hand: yes\n")
        self.assertIn("+Hand: yes", self.store.diff("README.md"))

    def test_dirty_lists_changed_and_untracked(self):
        self.assertEqual(self.store.dirty(), set())
        # a first entry starting with git's status space must keep its whole path
        (self.root / "README.md").write_text("changed\n")
        (self.root / "inventory" / "new file.yaml").write_text("- x\n")
        self.assertEqual(self.store.dirty(), {"README.md", "inventory/new file.yaml"})


class RestoreTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.first = git(self.root, "rev-parse", "--short", "HEAD")
        self.store.edit("README.md", "Sam", "Alex", "via claude.ai")

    def test_restores_as_a_new_commit_of_one_file(self):
        (self.root / "other.md").write_text("x\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "other")
        commit = self.store.restore("README.md", self.first, "via dashboard")
        self.assertIn("Sam", self.store.read("README.md"))
        self.assertEqual(git(self.root, "rev-parse", "--short", "HEAD"), commit)
        self.assertEqual(self.subjects()[0], f"restore README.md to {self.first}")
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), "README.md")
        self.assertTrue((self.root / "other.md").exists())

    def test_restore_can_be_undone(self):
        latest = git(self.root, "rev-parse", "--short", "HEAD")
        self.store.restore("README.md", self.first)
        self.store.restore("README.md", latest)
        self.assertIn("Alex", self.store.read("README.md"))

    def test_hand_edits_recorded_first(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "Hand: yes\n")
        self.store.restore("README.md", self.first)
        self.assertEqual(self.subjects()[1], "record hand edits to README.md")
        self.assertIn("+Hand: yes", git(self.root, "show", "HEAD~1"))
        self.assertNotIn("Hand: yes", self.store.read("README.md"))

    def test_current_version_refused(self):
        latest = git(self.root, "rev-parse", "--short", "HEAD")
        with self.assertRaises(StoreError):
            self.store.restore("README.md", latest)
        self.assertEqual(len(self.subjects()), 2)

    def test_only_a_plain_commit_id_reaches_git(self):
        for sha in ("--output=x", "HEAD", "abc", "../x", "", self.first + "\n", "A" * 7):
            with self.subTest(sha=sha), self.assertRaises(StoreError):
                self.store.restore("README.md", sha)
        for sha in ("--output=x", "HEAD"):
            with self.subTest(sha=sha), self.assertRaises(StoreError):
                self.store.diff("README.md", sha)
        self.assertFalse((self.root / "x").exists())

    def test_file_absent_at_that_commit_refused(self):
        self.store.create("new.md", "x\n")
        with self.assertRaises(StoreError):
            self.store.restore("new.md", self.first)
        self.assertEqual(self.store.read("new.md"), "x\n")

    def test_binary_version_refused(self):
        path = self.root / "README.md"
        path.write_bytes(b"\xff\xfe")
        git(self.root, "commit", "-qam", "binary")
        binary = git(self.root, "rev-parse", "--short", "HEAD")
        path.write_text("text\n")
        git(self.root, "commit", "-qam", "text")
        with self.assertRaises(StoreError):
            self.store.restore("README.md", binary)
        self.assertEqual(self.store.read("README.md"), "text\n")


class MatchesTest(StoreCase):
    def test_tuples_under_a_path(self):
        self.assertEqual(self.store.matches("LAPTOP", "inventory"), [("inventory/hardware.yaml", 1, "- laptop")])
        self.assertEqual(self.store.matches("Sam", "inventory"), [])

    def test_same_root_shares_a_lock(self):
        self.assertIs(Store(self.root)._lock, self.store._lock)


class RepoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_writes_refused_without_a_repo(self):
        root = self.base / "user"
        root.mkdir()
        (root / "a.md").write_text("old\n")
        store = Store(root)
        with self.assertRaises(StoreError):
            store.edit("a.md", "old", "new")
        self.assertEqual((root / "a.md").read_text(), "old\n")

    def test_writes_refused_inside_an_enclosing_repo(self):
        (self.base / "keep.md").write_text("x\n")
        repo(self.base)
        root = self.base / "user"
        root.mkdir()
        (root / "a.md").write_text("old\n")
        with self.assertRaises(StoreError):
            Store(root).edit("a.md", "old", "new")
        self.assertEqual(git(self.base, "log", "--format=%s"), "start")


if __name__ == "__main__":
    unittest.main()
