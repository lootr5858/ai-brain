#!/usr/bin/env python3
"""Raw user-file editing with clean targets and exact Git commits."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import PurePosixPath

from dashboard.content import MARKDOWN, MAX_CONTENT, classify_path
from lib.ops import content_digest, user_delete, user_move, user_write


class EditingError(Exception):
    def __init__(self, status: int, code: str, message: str, result: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.result = result


class UserEditing:
    def __init__(self, config, open_file):
        self.config = config
        self.open_file = open_file

    def _git(self, *args: str, text: bool = True):
        result = subprocess.run(
            ["git", "-C", str(self.config.user), *args],
            text=text,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise EditingError(503, "git_unavailable", "User Git repository is unavailable.")
        return result.stdout

    @staticmethod
    def _path(path: str) -> str:
        if (
            classify_path("user", path) != "raw"
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise EditingError(400, "invalid_target", "Editing target is not allowed.")
        return path

    def _metadata(self, path: str):
        target = self.config.user / path
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            return None
        if target.is_symlink() or not target.is_file():
            raise EditingError(400, "invalid_target", "Editing target is not allowed.")
        return metadata

    def _clean(self, path: str) -> None:
        if self._git("status", "--porcelain=v1", "-z", "--", path):
            raise EditingError(
                409,
                "dirty_target",
                "Target has uncommitted changes; clean it outside the dashboard first.",
            )

    def _digest(self, path: str) -> str:
        try:
            return content_digest(self.config.user / path)
        except ValueError as exc:
            raise EditingError(400, "invalid_target", "Editing target is not allowed.") from exc

    def _expect(self, path: str, expected_digest: str, *, present: bool = True) -> str:
        metadata = self._metadata(path)
        if present and metadata is None:
            raise EditingError(404, "not_found", "Editing target was not found.")
        if not present and metadata is not None:
            raise EditingError(409, "destination_exists", "Destination already exists.")
        current = self._digest(path) if metadata else ""
        if current != expected_digest:
            raise EditingError(
                409,
                "conflict",
                "Target changed; reload it before trying again.",
                {"partial": False, "current_digest": current},
            )
        return current

    @staticmethod
    def _content(content: str) -> str:
        if "\0" in content:
            raise EditingError(422, "invalid_content", "Edited content contains an invalid character.")
        if len(content.encode("utf-8")) > MAX_CONTENT:
            raise EditingError(413, "content_too_large", "Edited content is too large.")
        return content

    def _read(self, path: str) -> str:
        try:
            with self.open_file(self.config, "user", path) as handle:
                data = handle.read(MAX_CONTENT + 1)
        except ValueError as exc:
            raise EditingError(400, "invalid_target", "Editing target is not allowed.") from exc
        if len(data) > MAX_CONTENT:
            raise EditingError(413, "content_too_large", "Edited content is too large.")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditingError(422, "invalid_encoding", "Editing target is not valid UTF-8.") from exc

    def _changed(self, paths: list[str], expected: dict[str, str]) -> None:
        if any(self._digest(path) != digest for path, digest in expected.items()):
            raise EditingError(
                409,
                "conflict",
                "Target changed during the operation; clean it outside the dashboard.",
                {"partial": True, "paths": paths},
            )

    def _commit(self, action: str, paths: list[str], message_path: str) -> str:
        message = f"{action}: user/{message_path}"
        for args in (("add", "-A", "--", *paths), ("commit", "--only", "-m", message, "--", *paths)):
            result = subprocess.run(
                ["git", "-C", str(self.config.user), *args],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise EditingError(
                    500,
                    "commit_failed",
                    "File changed, but its Git commit failed; clean it outside the dashboard.",
                    {"partial": True, "paths": paths, "commit": ""},
                )
        return self._git("rev-parse", "HEAD").strip()

    @staticmethod
    def _result(path: str, digest: str, commit: str, **extra) -> dict:
        return {"path": path, "digest": digest, "commit": commit, "partial": False, **extra}

    def preview(self, content: str) -> dict:
        return {"rendered_html": MARKDOWN.render(self._content(content))}

    def create_page(self, path: str, content: str) -> dict:
        path = self._path(path)
        content = self._content(content)
        self._clean(path)
        self._expect(path, "", present=False)
        written = user_write(path, content, self.config.user, "")
        if not written.allowed:
            code = "conflict" if "stale" in written.reason else "write_failed"
            raise EditingError(409 if code == "conflict" else 500, code, "File could not be created.")
        digest = hashlib.sha256(content.encode()).hexdigest()
        self._changed([path], {path: digest})
        return self._result(path, digest, self._commit("create", [path], path))

    def edit_page(self, path: str, content: str, expected_digest: str) -> dict:
        path = self._path(path)
        content = self._content(content)
        self._clean(path)
        current = self._expect(path, expected_digest)
        digest = hashlib.sha256(content.encode()).hexdigest()
        if digest == current:
            raise EditingError(400, "no_change", "Edited content has not changed.")
        written = user_write(path, content, self.config.user, current)
        if not written.allowed:
            code = "conflict" if "stale" in written.reason else "write_failed"
            raise EditingError(409 if code == "conflict" else 500, code, "File could not be edited.")
        self._changed([path], {path: digest})
        return self._result(path, digest, self._commit("edit", [path], path))

    def _relocate(self, action: str, path: str, destination: str, expected_digest: str) -> dict:
        path, destination = self._path(path), self._path(destination)
        same_parent = PurePosixPath(path).parent == PurePosixPath(destination).parent
        if path == destination or (action == "rename") != same_parent:
            raise EditingError(400, "invalid_destination", "Destination does not match the operation.")
        self._clean(path)
        self._clean(destination)
        current = self._expect(path, expected_digest)
        self._expect(destination, "", present=False)
        moved = user_move(path, destination, self.config.user, current)
        if not moved.allowed:
            code = "conflict" if "stale" in moved.reason else "move_failed"
            raise EditingError(409 if code == "conflict" else 500, code, "File could not be moved.")
        self._changed([path, destination], {path: "", destination: current})
        commit = self._commit(action, [path, destination], destination)
        return self._result(destination, current, commit, source=path)

    def rename_page(self, path: str, destination: str, expected_digest: str) -> dict:
        return self._relocate("rename", path, destination, expected_digest)

    def move_page(self, path: str, destination: str, expected_digest: str) -> dict:
        return self._relocate("move", path, destination, expected_digest)

    def archive_page(self, path: str, expected_digest: str, replacement: str = "") -> dict:
        path = self._path(path)
        if not path.endswith(".md"):
            raise EditingError(400, "archive_unsupported", "Only contract-backed Markdown can be archived.")
        self._clean(path)
        current = self._expect(path, expected_digest)
        content = self._read(path)
        if not content.startswith("---\n") or "\n---\n" not in content[4:]:
            raise EditingError(400, "archive_unsupported", "Markdown metadata does not support archive.")
        end = content.find("\n---\n", 4)
        metadata = content[4:end]
        if not re.search(r"^schema:\s*brain-user/v1\s*$", metadata, re.MULTILINE):
            raise EditingError(400, "archive_unsupported", "Markdown metadata does not support archive.")
        status = re.search(r"^status:\s*(\S+)\s*$", metadata, re.MULTILINE)
        if not status or status.group(1) != "active":
            raise EditingError(400, "archive_unsupported", "Markdown metadata does not support archive.")
        metadata = metadata[: status.start(1)] + "superseded" + metadata[status.end(1) :]
        if replacement:
            replacement = self._path(replacement)
            if replacement == path or self._metadata(replacement) is None:
                raise EditingError(400, "invalid_replacement", "Archive replacement was not found.")
            relation = re.search(r"^superseded_by:\s*.*$", metadata, re.MULTILINE)
            line = f"superseded_by: {replacement}"
            metadata = (
                metadata[: relation.start()] + line + metadata[relation.end() :]
                if relation
                else metadata.rstrip("\n") + "\n" + line + "\n"
            )
        archived = self._content(content[:4] + metadata + content[end:])
        digest = hashlib.sha256(archived.encode()).hexdigest()
        written = user_write(path, archived, self.config.user, current)
        if not written.allowed:
            code = "conflict" if "stale" in written.reason else "write_failed"
            raise EditingError(409 if code == "conflict" else 500, code, "File could not be archived.")
        self._changed([path], {path: digest})
        return self._result(path, digest, self._commit("archive", [path], path))

    def delete_page(self, path: str, expected_digest: str) -> dict:
        path = self._path(path)
        self._clean(path)
        current = self._expect(path, expected_digest)
        deleted = user_delete(path, self.config.user, current)
        if not deleted.allowed:
            code = "conflict" if "stale" in deleted.reason else "delete_failed"
            raise EditingError(409 if code == "conflict" else 500, code, "File could not be deleted.")
        self._changed([path], {path: ""})
        return self._result(path, "", self._commit("delete", [path], path))

    def _rename_pair(self, commit: str, path: str) -> tuple[str, str] | None:
        raw = self._git(
            "diff-tree", "--no-commit-id", "--name-status", "-r", "-M", "-z", f"{commit}^", commit,
            text=False,
        ).split(b"\0")
        index = 0
        while index < len(raw) and raw[index]:
            status = raw[index].decode("ascii")
            index += 1
            count = 2 if status.startswith("R") else 1
            names = [raw[index + offset].decode("utf-8") for offset in range(count)]
            index += count
            if count == 2 and path in names and all(classify_path("user", name) == "raw" for name in names):
                return names[0], names[1]
        return None

    def undo_page(self, path: str, expected_digest: str) -> dict:
        path = self._path(path)
        self._clean(path)
        current = self._expect(path, expected_digest, present=self._metadata(path) is not None)
        commit = self._git("log", "-1", "--format=%H", "--", path).strip()
        if not commit:
            raise EditingError(409, "undo_unavailable", "No previous Git version is available.")
        try:
            parent = self._git("rev-parse", f"{commit}^").strip()
        except EditingError as exc:
            raise EditingError(409, "undo_unavailable", "No previous Git version is available.") from exc

        pair = self._rename_pair(commit, path)
        if pair:
            old, new = pair
            self._clean(old)
            self._clean(new)
            existing = [name for name in pair if self._metadata(name) is not None]
            if len(existing) != 1 or path != existing[0]:
                raise EditingError(409, "undo_unavailable", "Move cannot be undone from the current state.")
            source = existing[0]
            destination = old if source == new else new
            digest = self._digest(source)
            moved = user_move(source, destination, self.config.user, digest)
            if not moved.allowed:
                raise EditingError(409, "conflict", "Move cannot be undone from the current state.")
            self._changed([old, new], {source: "", destination: digest})
            subject = self._git("show", "-s", "--format=%s", commit).strip()
            action = "rename" if subject.startswith("rename:") else "move"
            undo_commit = self._commit(action, [old, new], destination)
            return self._result(destination, digest, undo_commit, source=source, undone_commit=commit)

        probe = subprocess.run(
            ["git", "-C", str(self.config.user), "cat-file", "-e", f"{parent}:{path}"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            data = self._git("show", f"{parent}:{path}", text=False)
            if len(data) > MAX_CONTENT:
                raise EditingError(413, "content_too_large", "Previous Git version is too large.")
            try:
                previous = self._content(data.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise EditingError(422, "invalid_encoding", "Previous Git version is not valid UTF-8.") from exc
            digest = hashlib.sha256(data).hexdigest()
            written = user_write(path, previous, self.config.user, current)
            if not written.allowed:
                raise EditingError(409, "conflict", "Previous Git version could not be restored.")
            action = "create" if not current else "edit"
        else:
            if not current:
                raise EditingError(409, "undo_unavailable", "No previous Git version is available.")
            deleted = user_delete(path, self.config.user, current)
            if not deleted.allowed:
                raise EditingError(409, "conflict", "Previous Git version could not be restored.")
            digest = ""
            action = "delete"
        self._changed([path], {path: digest})
        undo_commit = self._commit(action, [path], path)
        return self._result(path, digest, undo_commit, undone_commit=commit)
