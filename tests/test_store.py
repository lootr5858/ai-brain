"""Store: paths stay inside, text only, one commit per write, hand edits kept apart."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from brain.store import MAX_BYTES, MAX_HITS, Store, StoreError


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
