#!/usr/bin/env python3
"""Read-only Brain Dashboard content, catalog, and search domain."""
from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from markdown_it import MarkdownIt


USER_ROOT_FILES = {"README.md", "preferences.md", "context-routing.md"}
USER_RAW_DIRS = {"profile", "inventory", "journal", "knowledge", "private", "projects"}
USER_GENERATED_DIRS = {"reports"}
AGENT_ROOT_FILES = {"BOOTSTRAP.md", "CONVENTIONS.md", "README.md", "skills.toml"}
AGENT_DIRS = {"core", "governance", "rules", "skills", "reference"}
AGENT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".sh", ".toml", ".ts", ".txt", ".v", ".yaml"}
MAX_CONTENT = 1024 * 1024
SCHEMA_VERSION = "2"
EMBED_INPUT_BYTES = 1800
EMBED_OVERLAP_BYTES = 180
EMBED_BATCH_SIZE = 8
EMBED_TIMEOUT = 10
EMBED_WORK_SECONDS = 15
MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False})


class ContentError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _NoEmbeddingRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_path(path: str) -> PurePosixPath | None:
    item = PurePosixPath(path)
    if not path or item.is_absolute() or any(part in {"", ".", ".."} for part in item.parts):
        return None
    return item


def classify_path(repository: str, path: str) -> str | None:
    item = _safe_path(path)
    if item is None:
        return None
    if repository == "user":
        if item.suffix not in {".md", ".yaml"}:
            return None
        if path in USER_ROOT_FILES:
            return "raw"
        if item.parts[0] in USER_RAW_DIRS:
            return "raw"
        if item.parts[0] in USER_GENERATED_DIRS:
            return "generated"
        return None
    if repository == "agents":
        if item.suffix not in AGENT_SUFFIXES:
            return None
        if path in AGENT_ROOT_FILES or item.parts[0] in AGENT_DIRS:
            return "shared"
    return None


def _format(path: str) -> str:
    return "markdown" if path.endswith(".md") else Path(path).suffix.removeprefix(".")


def _title(path: str, text: str) -> str:
    if path.endswith(".md"):
        heading = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
        if heading:
            return heading.group(1).strip()
    return Path(path).stem.replace("-", " ").title()


class ContentCatalog:
    def __init__(self, config, open_file, database_path=None, embed=None):
        self.config = config
        self.open_file = open_file
        self.embed = embed
        state = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.database_path = Path(database_path or state / "brain-engine/dashboard/catalog.sqlite3")
        self._status = {
            "available": False,
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "rebuilt_at": "",
            "semantic_available": False,
            "semantic_reason": "unavailable",
        }

    def _root(self, repository: str) -> Path:
        return {"user": self.config.user, "agents": self.config.agents}[repository]

    def _dirty(self, repository: str, path: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self._root(repository)), "status", "--porcelain", "--", path],
            text=True,
            capture_output=True,
            check=False,
        )
        return bool(result.stdout)

    def get_page(self, repository: str, path: str) -> dict:
        kind = classify_path(repository, path)
        if kind is None:
            raise ContentError(400, "invalid_target", "Content target is not allowed.")
        target = self._root(repository) / path
        try:
            metadata = target.lstat()
        except FileNotFoundError as exc:
            raise ContentError(404, "not_found", "Content page was not found.") from exc
        if target.is_symlink() or not target.is_file():
            raise ContentError(400, "invalid_target", "Content target is not allowed.")
        if metadata.st_size > MAX_CONTENT:
            raise ContentError(413, "content_too_large", "Content page is too large.")
        try:
            with self.open_file(self.config, repository, path) as handle:
                data = handle.read(MAX_CONTENT + 1)
        except ValueError as exc:
            raise ContentError(400, "invalid_target", "Content target is not allowed.") from exc
        if len(data) > MAX_CONTENT:
            raise ContentError(413, "content_too_large", "Content page is too large.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContentError(422, "invalid_encoding", "Content page is not valid UTF-8.") from exc
        page_format = _format(path)
        return {
            "repository": repository,
            "path": path,
            "kind": kind,
            "format": page_format,
            "title": _title(path, text),
            "content": text,
            "rendered_html": MARKDOWN.render(text) if page_format == "markdown" else "",
            "digest": hashlib.sha256(data).hexdigest(),
            "dirty": self._dirty(repository, path),
        }

    def _user_paths(self):
        for path in sorted(USER_ROOT_FILES):
            target = self.config.user / path
            if target.exists() and not target.is_symlink():
                yield path
        for directory in sorted(USER_RAW_DIRS | USER_GENERATED_DIRS):
            root = self.config.user / directory
            if not root.is_dir() or root.is_symlink():
                continue
            for current, directories, files in os.walk(root, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    name for name in directories if not (current_path / name).is_symlink()
                ]
                for name in files:
                    target = current_path / name
                    if target.is_symlink():
                        continue
                    relative = target.relative_to(self.config.user).as_posix()
                    if classify_path("user", relative):
                        yield relative

    def _agent_paths(self):
        result = subprocess.run(
            ["git", "-C", str(self.config.agents), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            return
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            path = raw.decode("utf-8")
            target = self.config.agents / path
            if classify_path("agents", path) and target.is_file() and not target.is_symlink():
                yield path

    def _paths(self):
        for path in self._user_paths():
            yield "user", path
        for path in self._agent_paths():
            yield "agents", path

    def list_tree(self) -> dict:
        repositories = {"user": [], "agents": []}
        for repository, path in self._paths():
            try:
                page = self.get_page(repository, path)
            except ContentError:
                continue
            repositories[repository].append(
                {
                    "path": path,
                    "kind": page["kind"],
                    "format": page["format"],
                    "title": page["title"],
                }
            )
        return {"repositories": repositories}

    def _collect(self) -> tuple[list[dict], int]:
        pages = []
        errors = 0
        for repository, path in self._paths():
            try:
                pages.append(self.get_page(repository, path))
            except ContentError:
                errors += 1
        return pages, errors

    def _connection(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.exists():
            try:
                with sqlite3.connect(self.database_path) as connection:
                    has_meta = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_meta'"
                    ).fetchone()
                    version = (
                        connection.execute(
                            "SELECT value FROM catalog_meta WHERE key='schema_version'"
                        ).fetchone()
                        if has_meta
                        else None
                    )
            except sqlite3.DatabaseError:
                version = None
            if version and version[0] != SCHEMA_VERSION:
                self.database_path.unlink()
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
              id INTEGER PRIMARY KEY,
              repository TEXT NOT NULL,
              path TEXT NOT NULL,
              kind TEXT NOT NULL,
              format TEXT NOT NULL,
              title TEXT NOT NULL,
              digest TEXT NOT NULL,
              UNIQUE(repository, path)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
              USING fts5(page_id UNINDEXED, title, body);
            CREATE TABLE IF NOT EXISTS links (
              source_id INTEGER NOT NULL,
              target_repository TEXT NOT NULL,
              target_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS embeddings (
              digest TEXT NOT NULL,
              model TEXT NOT NULL,
              vector TEXT NOT NULL,
              PRIMARY KEY(digest, model)
            );
            CREATE TABLE IF NOT EXISTS catalog_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO catalog_meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        return connection

    @staticmethod
    def _frontmatter_relation(text: str) -> str | None:
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---", 4)
        if end < 0:
            return None
        metadata = text[4:end]
        if not re.search(r"^schema:\s*brain-user/v1\s*$", metadata, re.MULTILINE):
            return None
        match = re.search(r"^superseded_by:\s*([^#\n]+?)\s*$", metadata, re.MULTILINE)
        return match.group(1).strip(" '\"") if match else None

    @staticmethod
    def _resolve_link(repository: str, source: str, href: str):
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return None
        target_repository = repository
        target_path = parsed.path
        if target_path.startswith("user/") or target_path.startswith("agents/"):
            target_repository, target_path = target_path.split("/", 1)
        else:
            target_path = posixpath.normpath(posixpath.join(posixpath.dirname(source), target_path))
        if target_path.startswith("../") or target_path.startswith("/") or classify_path(
            target_repository, target_path
        ) is None:
            return None
        return target_repository, target_path

    def _page_links(self, page: dict, existing: set[tuple[str, str]]):
        targets = set()
        if page["format"] == "markdown":
            for token in MARKDOWN.parse(page["content"]):
                if token.type == "inline" and token.children:
                    for child in token.children:
                        if child.type != "link_open":
                            continue
                        target = self._resolve_link(
                            page["repository"], page["path"], child.attrGet("href") or ""
                        )
                        if target in existing:
                            targets.add(target)
            if page["repository"] == "user":
                relation = self._frontmatter_relation(page["content"])
                target = self._resolve_link("user", page["path"], f"user/{relation}") if relation else None
                if target in existing:
                    targets.add(target)
        return sorted(targets)

    def _replace(self, pages: list[dict], errors: int) -> dict:
        connection = self._connection()
        try:
            with connection:
                connection.execute("DELETE FROM pages_fts")
                connection.execute("DELETE FROM links")
                connection.execute("DELETE FROM pages")
                page_ids = {}
                for page in sorted(pages, key=lambda item: (item["repository"], item["path"])):
                    cursor = connection.execute(
                        """INSERT INTO pages(repository, path, kind, format, title, digest)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            page["repository"],
                            page["path"],
                            page["kind"],
                            page["format"],
                            page["title"],
                            page["digest"],
                        ),
                    )
                    page_id = cursor.lastrowid
                    page_ids[(page["repository"], page["path"])] = page_id
                    connection.execute(
                        "INSERT INTO pages_fts(page_id, title, body) VALUES (?, ?, ?)",
                        (page_id, page["title"], page["content"]),
                    )
                existing = set(page_ids)
                for page in pages:
                    source_id = page_ids[(page["repository"], page["path"])]
                    for target_repository, target_path in self._page_links(page, existing):
                        connection.execute(
                            "INSERT INTO links(source_id, target_repository, target_path) VALUES (?, ?, ?)",
                            (source_id, target_repository, target_path),
                        )
                rebuilt_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "INSERT OR REPLACE INTO catalog_meta(key, value) VALUES('rebuilt_at', ?)",
                    (rebuilt_at,),
                )
            self._status = {
                "available": True,
                "indexed": len(pages),
                "skipped": 0,
                "errors": errors,
                "rebuilt_at": rebuilt_at,
                "semantic_available": False,
                "semantic_reason": "unavailable",
            }
            return dict(self._status)
        finally:
            connection.close()

    def rebuild(self) -> dict:
        try:
            pages, errors = self._collect()
            self._replace(pages, errors)
            available = self._cache_embeddings(pages)
            self._status.update(semantic_available=available, semantic_reason="" if available else "unavailable")
            return self.status()
        except Exception as exc:
            self._status["available"] = False
            raise ContentError(503, "catalog_unavailable", "Content catalog is unavailable.") from exc

    def status(self) -> dict:
        return dict(self._status)

    def _ensure_fresh(self) -> list[dict]:
        try:
            pages, errors = self._collect()
            current = {(page["repository"], page["path"]): page["digest"] for page in pages}
            connection = self._connection()
            try:
                stored = {
                    (row["repository"], row["path"]): row["digest"]
                    for row in connection.execute("SELECT repository, path, digest FROM pages")
                }
            finally:
                connection.close()
            # ponytail: O(n) digest scan/full rebuild; add incremental indexing only when measured latency matters.
            if not self._status["available"] or current != stored:
                self._replace(pages, errors)
            return pages
        except ContentError:
            raise
        except Exception as exc:
            self._status["available"] = False
            raise ContentError(503, "catalog_unavailable", "Content catalog is unavailable.") from exc

    def _semantic_configured(self) -> bool:
        try:
            parsed = urlsplit(self.config.dashboard_embedding_url)
            parsed.port
        except ValueError:
            return False
        return bool(
            self.config.dashboard_embedding_model
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and parsed.path.endswith("/v1/embeddings")
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _validated_vectors(vectors, count: int) -> list[list[float]]:
        if not isinstance(vectors, list) or len(vectors) != count:
            raise ValueError("invalid embedding response")
        result = []
        dimension = None
        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                raise ValueError("invalid embedding response")
            values = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("invalid embedding response")
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("invalid embedding response")
                values.append(number)
            dimension = dimension or len(values)
            if len(values) != dimension:
                raise ValueError("invalid embedding response")
            norm = math.hypot(*values)
            if not norm or not math.isfinite(norm):
                raise ValueError("invalid embedding response")
            result.append(values)
        return result

    def _embedding_input(self, text: str, *, query: bool = False) -> str:
        if "nomic-embed-text" in self.config.dashboard_embedding_model.lower():
            return ("search_query: " if query else "search_document: ") + text
        return text

    def _chunks(self, page: dict):
        header = page["title"][:160] + "\n"
        limit = EMBED_INPUT_BYTES - len(self._embedding_input(header).encode("utf-8"))
        data = page["content"].encode("utf-8")
        start = 0
        while True:
            # ponytail: conservative byte bound for the installed Nomic model, not a universal tokenizer.
            body = data[start:start + limit].decode("utf-8", errors="ignore")
            if start + limit < len(data):
                boundary = body.rfind("\n\n")
                if boundary < len(body) // 2:
                    boundary = body.rfind(" ")
                if boundary >= len(body) // 2:
                    body = body[:boundary + 1]
            text = self._embedding_input(header + body)
            digest = hashlib.sha256(
                (self.config.dashboard_embedding_url + "\0" + text).encode("utf-8")
            ).hexdigest()
            yield digest, text, body
            body_bytes = body.encode("utf-8")
            if start + len(body_bytes) == len(data):
                break
            overlap = body_bytes[-EMBED_OVERLAP_BYTES:].decode("utf-8", errors="ignore")
            start += len(body_bytes) - len(overlap.encode("utf-8"))

    def _embed(self, texts: list[str], timeout: float = EMBED_TIMEOUT) -> list[list[float]]:
        if not self._semantic_configured():
            raise ValueError("semantic search unavailable")
        if any(not text or len(text.encode("utf-8")) > EMBED_INPUT_BYTES for text in texts):
            raise ValueError("embedding input is too large or empty")
        if self.embed:
            return self._validated_vectors(self.embed(texts), len(texts))
        request = Request(
            self.config.dashboard_embedding_url,
            data=json.dumps(
                {"model": self.config.dashboard_embedding_model, "input": texts},
                separators=(",", ":"),
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Local documents must not follow redirects or environment-configured proxies.
        opener = build_opener(ProxyHandler({}), _NoEmbeddingRedirect())
        with opener.open(request, timeout=timeout) as response:
            payload = json.load(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(data, list) or len(data) != len(texts)
            or any(not isinstance(item, dict) or type(item.get("index")) is not int for item in data)
            or sorted(item["index"] for item in data) != list(range(len(texts)))
        ):
            raise ValueError("invalid embedding response")
        return self._validated_vectors(
            [item.get("embedding") for item in sorted(data, key=lambda item: item["index"])], len(texts)
        )

    def _cache_embeddings(self, pages: list[dict]) -> bool:
        if not self._semantic_configured():
            return False
        connection = self._connection()
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT digest FROM embeddings WHERE model = ?",
                    (self.config.dashboard_embedding_model,),
                )
            }
            chunks = {digest: text for page in pages for digest, text, _body in self._chunks(page)}
            missing = [(digest, text) for digest, text in chunks.items() if digest not in existing]
            deadline = time.monotonic() + EMBED_WORK_SECONDS
            for start in range(0, len(missing), EMBED_BATCH_SIZE):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                batch = missing[start:start + EMBED_BATCH_SIZE]
                vectors = self._embed([text for _digest, text in batch], min(EMBED_TIMEOUT, remaining))
                with connection:
                    connection.executemany(
                        "INSERT OR REPLACE INTO embeddings(digest, model, vector) VALUES (?, ?, ?)",
                        [
                            (
                                digest,
                                self.config.dashboard_embedding_model,
                                json.dumps(vector, separators=(",", ":")),
                            )
                            for (digest, _text), vector in zip(batch, vectors)
                        ],
                    )
            with connection:
                connection.executemany(
                    "DELETE FROM embeddings WHERE digest = ? AND model = ?",
                    [(digest, self.config.dashboard_embedding_model) for digest in existing - chunks.keys()],
                )
            return True
        except Exception:
            return False
        finally:
            connection.close()

    def _lexical(self, query: str, mode: str = "lexical") -> dict:
        match = query
        if mode == "semantic":
            # Semantic queries are plain language, not FTS5 expressions.
            terms = re.findall(r"\w+", query, re.UNICODE)
            if not terms:
                return {"query": query, "mode": mode, "results": []}
            match = " OR ".join('"' + term + '"' for term in terms)
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT p.repository, p.path, p.kind, p.format, p.title,
                          snippet(pages_fts, 2, '', '', ' … ', 18) AS snippet,
                          bm25(pages_fts) AS score
                   FROM pages_fts
                   JOIN pages p ON p.id = pages_fts.page_id
                   WHERE pages_fts MATCH ?
                   ORDER BY score, p.repository, p.path
                   LIMIT 50""",
                (match,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ContentError(400, "invalid_query", "Search query is invalid.") from exc
        finally:
            connection.close()
        results = []
        for row in rows:
            results.append(
                {
                    "repository": row["repository"],
                    "path": row["path"],
                    "kind": row["kind"],
                    "format": row["format"],
                    "title": row["title"],
                    "snippet": row["snippet"],
                    "score": row["score"],
                }
            )
        return {"query": query, "mode": mode, "results": results}

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            raise ValueError("embedding dimensions differ")
        left_norm = math.hypot(*left)
        right_norm = math.hypot(*right)
        if not left_norm or not right_norm:
            raise ValueError("zero embedding")
        return math.fsum((a / left_norm) * (b / right_norm) for a, b in zip(left, right))

    def _semantic(self, query: str, pages: list[dict]) -> dict:
        query_vector = self._embed([self._embedding_input(query, query=True)])[0]
        connection = self._connection()
        try:
            vectors = dict(connection.execute(
                "SELECT digest, vector FROM embeddings WHERE model = ?",
                (self.config.dashboard_embedding_model,),
            ))
        finally:
            connection.close()
        results = []
        for page in pages:
            best = None
            for digest, _text, body in self._chunks(page):
                vector = self._validated_vectors([json.loads(vectors[digest])], 1)[0]
                score = self._cosine(query_vector, vector)
                if best is None or score > best["score"]:
                    best = {
                        **{key: page[key] for key in ("repository", "path", "kind", "format", "title")},
                        "snippet": body,
                        "score": score,
                    }
            results.append(best)
        results.sort(key=lambda item: (-item["score"], item["repository"], item["path"]))
        return {
            "query": query,
            "mode": "semantic",
            "semantic_available": True,
            "semantic_reason": "",
            "results": results[:50],
        }

    def search(self, query: str, mode: str = "lexical") -> dict:
        if mode not in {"lexical", "semantic"}:
            raise ContentError(400, "invalid_mode", "Search mode is invalid.")
        if not isinstance(query, str) or not query.strip():
            raise ContentError(400, "invalid_query", "Search query is invalid.")
        query = query.strip()
        pages = self._ensure_fresh()
        if mode == "lexical":
            return self._lexical(query)
        try:
            if not self._cache_embeddings(pages):
                raise ValueError("document embeddings unavailable")
            result = self._semantic(query, pages)
            self._status.update(semantic_available=True, semantic_reason="")
            return result
        except Exception:
            self._status.update(semantic_available=False, semantic_reason="unavailable")
            result = self._lexical(query, "semantic")
            result.update(semantic_available=False, semantic_reason="unavailable")
            return result

    def backlinks(self, repository: str, path: str) -> dict:
        if classify_path(repository, path) is None:
            raise ContentError(400, "invalid_target", "Content target is not allowed.")
        self._ensure_fresh()
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT p.repository, p.path, p.kind, p.format, p.title
                   FROM links l JOIN pages p ON p.id = l.source_id
                   WHERE l.target_repository = ? AND l.target_path = ?
                   ORDER BY p.repository, p.path""",
                (repository, path),
            ).fetchall()
        finally:
            connection.close()
        return {
            "repository": repository,
            "path": path,
            "results": [dict(row) for row in rows],
        }
