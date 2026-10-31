"""File access for one store: reads stay inside it, writes land as one git commit each."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path, PurePosixPath

MAX_BYTES = 200_000
MAX_HITS = 50
MAX_LINE = 300


class StoreError(ValueError):
    """A refused request; the message is safe to show the caller."""


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = threading.Lock()

    def _path(self, rel: str) -> Path:
        """Resolve a store-relative path; hidden segments and anything outside the store are refused."""
        given = PurePosixPath(rel)
        if given.is_absolute() or ".." in given.parts or any(ord(c) < 32 for c in rel):
            raise StoreError(f"not a path inside the store: {rel!r}")
        path = (self.root / given).resolve()
        if not path.is_relative_to(self.root) or any(p.startswith(".") for p in path.relative_to(self.root).parts):
            raise StoreError(f"not a path inside the store: {rel!r}")
        return path

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def list(self, rel: str = "") -> list[tuple[str, int]]:
        """Return (path, size) for every visible file under a path, sorted."""
        start = self._path(rel)
        prefix = PurePosixPath(rel)
        if start.is_file():
            return [(str(prefix), start.stat().st_size)]
        if not start.is_dir():
            raise StoreError(f"not found: {rel}")
        found = []
        for folder, dirs, names in os.walk(start):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                path = Path(folder) / name
                if not name.startswith(".") and path.is_file():
                    found.append((str(prefix / path.relative_to(start)), path.stat().st_size))
        return sorted(found)

    def read(self, rel: str) -> str:
        path = self._path(rel)
        if not path.is_file():
            raise StoreError(f"not found: {rel}")
        if path.stat().st_size > MAX_BYTES:
            raise StoreError(f"over the {MAX_BYTES}-byte limit: {rel}")
        try:
            return path.read_bytes().decode()
        except UnicodeDecodeError:
            raise StoreError(f"not a text file: {rel}") from None

    def search(self, query: str) -> list[str]:
        """Return up to MAX_HITS `path:line: text` matches, case-insensitive."""
        if len(query.strip()) < 2:
            raise StoreError("search needs at least 2 characters")
        needle = query.strip().casefold()
        hits: list[str] = []
        for rel, size in self.list():
            if size > MAX_BYTES:
                continue
            try:
                text = self.read(rel)
            except StoreError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.casefold():
                    hits.append(f"{rel}:{number}: {line.strip()[:MAX_LINE]}")
                    if len(hits) == MAX_HITS:
                        return hits
        return hits

    def edit(self, rel: str, old: str, new: str, via: str = "") -> str:
        """Replace `old`, which must occur exactly once, and commit; return the commit id."""
        path = self._path(rel)
        with self._lock:
            self._require_repo()
            if old == new:
                raise StoreError("`old` and `new` are the same")
            text = self.read(rel)
            count = text.count(old) if old else 0
            if count != 1:
                raise StoreError(f"`old` must occur exactly once in {rel}; found {count}. Read the file again.")
            data = text.replace(old, new).encode()
            self._check_size(data)
            name = self._rel(path)
            if self._git("status", "--porcelain", "--", name):
                self._commit(name, f"record hand edits to {name}")
            before = path.read_bytes()
            path.write_bytes(data)
            try:
                return self._commit(name, f"edit {name}", via)
            except StoreError:
                path.write_bytes(before)
                raise

    def create(self, rel: str, content: str, via: str = "") -> str:
        """Write a new file in an existing folder and commit; return the commit id."""
        path = self._path(rel)
        data = content.encode()
        self._check_size(data)
        with self._lock:
            self._require_repo()
            if not path.parent.is_dir():
                raise StoreError(f"folder does not exist: {rel}")
            try:
                with path.open("xb") as file:
                    file.write(data)
            except (FileExistsError, IsADirectoryError):
                raise StoreError(f"already exists: {rel}") from None
            try:
                return self._commit(self._rel(path), f"create {self._rel(path)}", via)
            except StoreError:
                path.unlink()
                raise

    def _check_size(self, data: bytes) -> None:
        if len(data) > MAX_BYTES:
            raise StoreError(f"over the {MAX_BYTES}-byte limit")

    def _require_repo(self) -> None:
        """The store must be its own repo root, so a write never lands in an enclosing repo."""
        try:
            top = self._git("rev-parse", "--show-toplevel")
        except StoreError:
            raise StoreError("writes need the store to be a git repo") from None
        if Path(top).resolve() != self.root:
            raise StoreError("writes need the store to be a git repo")

    def _commit(self, name: str, subject: str, via: str = "") -> str:
        """Commit one path only; anything else staged or dirty stays out of the commit."""
        self._git("add", "--", name)
        body = ["-m", via] if via else []
        self._git("commit", "-m", subject, *body, "--", name)
        return self._git("rev-parse", "--short", "HEAD")

    def _git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(self.root), *args],
            capture_output=True, text=True, timeout=30,
        )
        if done.returncode:
            raise StoreError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()}")
        return done.stdout.strip()
