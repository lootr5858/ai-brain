"""File access for one store: reads stay inside it, writes land as one git commit each."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path, PurePosixPath

MAX_BYTES = 200_000
MAX_HITS = 50
MAX_LINE = 300
MAX_HISTORY = 50
COMMIT_ID = re.compile(r"[0-9a-f]{7,40}")
_locks: dict[Path, threading.Lock] = {}  # one per store root: MCP and the dashboard share a repo


class StoreError(ValueError):
    """A refused request; the message is safe to show the caller."""


class Conflict(StoreError):
    """The file changed after the caller read it."""


def digest(text: str) -> str:
    """Name one version of a file's text, so a save can tell whether it is still current."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = _locks.setdefault(self.root, threading.Lock())

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

    def matches(self, query: str, under: str = "") -> list[tuple[str, int, str]]:
        """Return up to MAX_HITS (path, line, text) matches under a path, case-insensitive."""
        if len(query.strip()) < 2:
            raise StoreError("search needs at least 2 characters")
        needle = query.strip().casefold()
        hits: list[tuple[str, int, str]] = []
        for rel, size in self.list(under):
            if size > MAX_BYTES:
                continue
            try:
                text = self.read(rel)
            except StoreError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.casefold():
                    hits.append((rel, number, line.strip()[:MAX_LINE]))
                    if len(hits) == MAX_HITS:
                        return hits
        return hits

    def search(self, query: str) -> list[str]:
        """Return up to MAX_HITS `path:line: text` matches, case-insensitive."""
        return [f"{rel}:{number}: {text}" for rel, number, text in self.matches(query)]

    def history(self, rel: str) -> list[dict[str, str]]:
        """Return the commits that changed a file, newest first."""
        name = self._rel(self._path(rel))
        self._require_repo()
        log = self._git("log", f"-n{MAX_HISTORY}", "--format=%h%x00%aI%x00%s%x00%b%x1e", "--", name)
        rows = []
        for entry in filter(str.strip, log.split("\x1e")):
            sha, date, subject, body = entry.strip().split("\x00", 3)
            via = next((line[4:] for line in body.splitlines() if line.startswith("via ")), "")
            rows.append({"sha": sha, "date": date, "subject": subject, "via": via})
        return rows

    def diff(self, rel: str, sha: str = "") -> str:
        """Return one commit's change to a file, or its uncommitted change when no commit is named."""
        name = self._rel(self._path(rel))
        self._require_repo()
        if not sha:
            return self._git("diff", "--no-color", "HEAD", "--", name)
        self._check_commit(sha)
        return self._git("show", "--format=", "--no-color", sha, "--", name)

    def dirty(self) -> set[str]:
        """Return the paths with uncommitted changes; none when the store is not a repo."""
        try:
            self._require_repo()
            out = self._run("status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
        except StoreError:
            return set()
        return {entry[3:] for entry in out.decode(errors="replace").split("\0") if entry}

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
            return self._save(path, data, f"edit {self._rel(path)}", via)

    def replace(self, rel: str, text: str, base: str, via: str = "") -> str:
        """Save a whole file unless it changed since the `base` digest was read; return the commit id."""
        path = self._path(rel)
        data = text.encode()
        self._check_size(data)
        with self._lock:
            self._require_repo()
            current = self.read(rel)
            if digest(current) != base:
                raise Conflict(f"{rel} changed after it was opened")
            if text == current:
                raise StoreError(f"no changes to {rel}")
            return self._save(path, data, f"edit {self._rel(path)}", via)

    def restore(self, rel: str, sha: str, via: str = "") -> str:
        """Commit a file back to the version it had at `sha`; return the commit id."""
        path = self._path(rel)
        self._check_commit(sha)
        with self._lock:
            self._require_repo()
            name = self._rel(path)
            current = self.read(rel)
            data = self._run("show", f"{sha}:{name}")
            self._check_size(data)
            try:
                same = data.decode() == current
            except UnicodeDecodeError:
                raise StoreError(f"not a text file at {sha}: {rel}") from None
            if same:
                raise StoreError(f"{rel} is already this version")
            return self._save(path, data, f"restore {name} to {sha}", via)

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

    def _save(self, path: Path, data: bytes, subject: str, via: str) -> str:
        """Record any hand edits apart, then write and commit; the file is put back if the commit fails."""
        name = self._rel(path)
        if self._git("status", "--porcelain", "--", name):
            self._commit(name, f"record hand edits to {name}")
        before = path.read_bytes()
        path.write_bytes(data)
        try:
            return self._commit(name, subject, via)
        except StoreError:
            path.write_bytes(before)
            raise

    def _check_size(self, data: bytes) -> None:
        if len(data) > MAX_BYTES:
            raise StoreError(f"over the {MAX_BYTES}-byte limit")

    def _check_commit(self, sha: str) -> None:
        """A commit id reaches git as an argument, so only a plain hex id may pass."""
        if not COMMIT_ID.fullmatch(sha):
            raise StoreError(f"not a commit id: {sha!r}")

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
        return self._run(*args).decode(errors="replace").strip()

    def _run(self, *args: str) -> bytes:
        done = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(self.root), *args],
            capture_output=True, timeout=30,
        )
        if done.returncode:
            detail = (done.stderr or done.stdout).decode(errors="replace").strip()
            raise StoreError(f"git {args[0]} failed: {detail}")
        return done.stdout
