#!/usr/bin/env python3
"""Dashboard content tests - plain asserts, run directly."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ops import OpsConfig


ROOT = Path(tempfile.mkdtemp())
ENGINE = ROOT / "engine"
AGENTS = ROOT / "agents"
USER = ROOT / "user"
for repository in (ENGINE, AGENTS, USER):
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Content Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "content@example.invalid"],
        check=True,
    )
    (repository / "README.md").write_text("fixture\n")

(USER / "profile").mkdir()
(USER / "README.md").write_text("# Personal knowledge\n\nRouter for user records.\n")
(USER / "reports" / "app").mkdir(parents=True)
(USER / "preferences.md").write_text("# Preferences\n\nAlways concise.\n")
(USER / "profile" / "goals.md").write_text(
    "# Goals\n\nSleep well.\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n"
)
(USER / "reports" / "app" / "report.yaml").write_text("summary: generated\n")
(USER / "CONVENTIONS.md").write_text("excluded\n")
(USER / "profile" / "file.txt").write_text("unsupported\n")

(AGENTS / "core").mkdir()
(AGENTS / "homes" / "hermes").mkdir(parents=True)
(AGENTS / "core" / "behavior-rules.md").write_text("# Behavior rules\n\nBe precise.\n")
(AGENTS / "homes" / "hermes" / "SOUL.md").write_text("opaque\n")

for repository in (ENGINE, AGENTS, USER):
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixtures"], check=True)

(AGENTS / "core" / "draft.md").write_text("untracked\n")
(USER / "profile" / "goals.md").write_text(
    "# Goals\n\nSleep well.\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\nChanged.\n"
)
(USER / "profile" / "linked.md").symlink_to(USER / "profile" / "goals.md")
(USER / "profile" / "binary.md").write_bytes(b"\xff\xfe")
(USER / "profile" / "large.md").write_text("x" * (1024 * 1024 + 1))

CONFIG = OpsConfig(brain=ROOT, engine=ENGINE, agents=AGENTS, user=USER)

server_path = Path(__file__).resolve().parents[1] / "dashboard" / "server.py"
server_spec = importlib.util.spec_from_file_location("dashboard_server_for_content", server_path)
server = importlib.util.module_from_spec(server_spec)
assert server_spec and server_spec.loader
server_spec.loader.exec_module(server)

content_path = Path(__file__).resolve().parents[1] / "dashboard" / "content.py"
content_spec = importlib.util.spec_from_file_location("dashboard_content", content_path)
content = importlib.util.module_from_spec(content_spec)
assert content_spec and content_spec.loader
content_spec.loader.exec_module(content)


assert content.classify_path("user", "preferences.md") == "raw"
assert content.classify_path("user", "private/health/metrics.md") == "raw"
assert content.classify_path("user", "reports/app/report.yaml") == "generated"
assert content.classify_path("user", "CONVENTIONS.md") is None
assert content.classify_path("user", "profile/file.txt") is None
assert content.classify_path("agents", "core/behavior-rules.md") == "shared"
assert content.classify_path("agents", "homes/hermes/SOUL.md") is None
assert content.classify_path("engine", "lib/ops.py") is None
assert content.classify_path("user", "profile/../private/x.md") is None

catalog = content.ContentCatalog(
    CONFIG,
    server.open_repository_file,
    database_path=ROOT / "catalog.sqlite3",
)
tree = catalog.list_tree()["repositories"]
user_paths = {page["path"] for page in tree["user"]}
agent_paths = {page["path"] for page in tree["agents"]}
assert "preferences.md" in user_paths
assert "profile/goals.md" in user_paths
assert "reports/app/report.yaml" in user_paths
assert "CONVENTIONS.md" not in user_paths
assert "profile/file.txt" not in user_paths
assert "profile/linked.md" not in user_paths
assert "core/behavior-rules.md" in agent_paths
assert "core/draft.md" not in agent_paths
assert "homes/hermes/SOUL.md" not in agent_paths

page = catalog.get_page("user", "profile/goals.md")
assert set(page) == {
    "repository",
    "path",
    "kind",
    "format",
    "title",
    "content",
    "rendered_html",
    "digest",
    "dirty",
}
assert page["kind"] == "raw"
assert page["format"] == "markdown"
assert page["title"] == "Goals"
assert page["dirty"] is True
assert "<h1>Goals</h1>" in page["rendered_html"]
assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page["rendered_html"]
assert 'href="javascript:' not in page["rendered_html"]
assert len(page["digest"]) == 64

generated = catalog.get_page("user", "reports/app/report.yaml")
assert generated["kind"] == "generated"
assert generated["format"] == "yaml"
assert generated["rendered_html"] == ""


def error_code(call) -> tuple[int, str]:
    try:
        call()
    except content.ContentError as exc:
        return exc.status, exc.code
    raise AssertionError("expected ContentError")


assert error_code(lambda: catalog.get_page("engine", "lib/ops.py")) == (400, "invalid_target")
assert error_code(lambda: catalog.get_page("user", "CONVENTIONS.md")) == (400, "invalid_target")
assert error_code(lambda: catalog.get_page("user", "profile/../private/x.md")) == (
    400,
    "invalid_target",
)
assert error_code(lambda: catalog.get_page("user", "profile/linked.md")) == (400, "invalid_target")
assert error_code(lambda: catalog.get_page("user", "profile/missing.md")) == (404, "not_found")
assert error_code(lambda: catalog.get_page("user", "profile/binary.md")) == (
    422,
    "invalid_encoding",
)
assert error_code(lambda: catalog.get_page("user", "profile/large.md")) == (
    413,
    "content_too_large",
)

print("dashboard content task2: PASS")

(USER / "knowledge").mkdir()
(USER / "knowledge" / "alpha.md").write_text(
    "# Alpha\n\nA unique phrase lives here.\n\n"
    "[relative](../profile/goals.md)\n\n"
    "[canonical](user/profile/goals.md)\n\n"
    "[external](https://example.invalid/page)\n\n"
    "[fragment](#alpha)\n\n"
    "[missing](missing.md)\n"
)
(USER / "knowledge" / "beta.md").write_text("# Beta\n\nCompletely different words.\n")
(USER / "knowledge" / "replaced.md").write_text(
    "---\n"
    "schema: brain-user/v1\n"
    "status: superseded\n"
    "superseded_by: profile/goals.md\n"
    "---\n"
    "# Replaced\n"
)

rebuilt = catalog.rebuild()
assert rebuilt["indexed"] == 9
assert rebuilt["skipped"] == 0
assert rebuilt["errors"] == 2
assert catalog.status()["available"] is True

import sqlite3

with sqlite3.connect(catalog.database_path) as connection:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
assert {"pages", "pages_fts", "links", "embeddings", "catalog_meta"} <= tables

lexical = catalog.search("unique phrase", "lexical")
assert lexical["mode"] == "lexical"
assert lexical["results"][0]["path"] == "knowledge/alpha.md"
assert "unique phrase" in lexical["results"][0]["snippet"]
assert "<mark>" not in lexical["results"][0]["snippet"]
hostile_snippet = catalog.search("alert", "lexical")["results"][0]["snippet"]
assert "<script>" in hostile_snippet
assert "&lt;script&gt;" not in hostile_snippet
assert error_code(lambda: catalog.search("", "lexical")) == (400, "invalid_query")
assert error_code(lambda: catalog.search('"', "lexical")) == (400, "invalid_query")

backlinks = catalog.backlinks("user", "profile/goals.md")
backlink_paths = [result["path"] for result in backlinks["results"]]
assert backlink_paths == ["knowledge/alpha.md", "knowledge/replaced.md"]
assert all("example.invalid" not in str(result) for result in backlinks["results"])

(USER / "knowledge" / "alpha.md").write_text("# Alpha\n\nA refreshed needle appears.\n")
refreshed = catalog.search("refreshed needle", "lexical")
assert refreshed["results"][0]["path"] == "knowledge/alpha.md"


def failing_open(config, repository, path):
    if path == "knowledge/alpha.md":
        raise OSError("fixture read failure")
    return server.open_repository_file(config, repository, path)


broken = content.ContentCatalog(
    CONFIG,
    failing_open,
    database_path=ROOT / "broken-catalog.sqlite3",
)
assert error_code(broken.rebuild) == (503, "catalog_unavailable")
assert broken.status()["available"] is False
assert broken.get_page("user", "profile/goals.md")["title"] == "Goals"

print("dashboard content task3: PASS")

semantic_calls = []


def fake_embed(texts):
    semantic_calls.append(list(texts))
    return [[1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0] for text in texts]


SEMANTIC_CONFIG = replace(
    CONFIG,
    dashboard_embedding_url="http://127.0.0.1:1234/v1/embeddings",
    dashboard_embedding_model="text-embedding-nomic-embed-text-v1.5",
)
semantic_catalog = content.ContentCatalog(
    SEMANTIC_CONFIG,
    server.open_repository_file,
    database_path=ROOT / "semantic-catalog.sqlite3",
    embed=fake_embed,
)
semantic_catalog.rebuild()
document_batches = len(semantic_calls)
semantic_catalog.rebuild()
assert len(semantic_calls) == document_batches
assert all(text.startswith("search_document: ") for batch in semantic_calls for text in batch)
semantic = semantic_catalog.search("alpha intent", "semantic")
assert semantic["semantic_available"] is True
assert semantic["results"][0]["path"] == "knowledge/alpha.md"
assert semantic["results"][0]["score"] == 1.0
assert semantic_calls[-1] == ["search_query: alpha intent"]

fallback = catalog.search("alpha", "semantic")
assert fallback["results"][0]["path"] == "knowledge/alpha.md"
assert fallback["semantic_available"] is False
assert fallback["semantic_reason"] == "unavailable"


def failing_embed(_texts):
    raise OSError("provider detail must not escape")


failed_semantic = content.ContentCatalog(
    SEMANTIC_CONFIG,
    server.open_repository_file,
    database_path=ROOT / "failed-semantic.sqlite3",
    embed=failing_embed,
)
assert failed_semantic.rebuild()["available"] is True
failed_fallback = failed_semantic.search("alpha", "semantic")
assert failed_fallback["semantic_available"] is False
assert failed_fallback["semantic_reason"] == "unavailable"
assert "provider detail" not in str(failed_fallback)
failed_semantic.embed = fake_embed
assert failed_semantic.search("alpha intent", "semantic")["semantic_available"] is True
assert failed_semantic.status()["semantic_available"] is True
failed_semantic.embed = failing_embed
assert failed_semantic.search('alpha: "where is it?', "semantic")["results"][0]["path"] == "knowledge/alpha.md"
assert failed_semantic.search('"?!', "semantic")["results"] == []
failed_semantic.embed = fake_embed
assert failed_semantic.search("alpha intent", "semantic")["semantic_available"] is True

invalid_semantic = content.ContentCatalog(
    SEMANTIC_CONFIG,
    server.open_repository_file,
    database_path=ROOT / "invalid-semantic.sqlite3",
    embed=lambda texts: [[1.0], [1.0, 2.0]][: len(texts)],
)
assert invalid_semantic.rebuild()["semantic_available"] is False
assert invalid_semantic.search("alpha", "semantic")["semantic_available"] is False

unsafe_config = replace(SEMANTIC_CONFIG, dashboard_embedding_url="http://example.com/v1/embeddings")
unsafe_called = False


def unsafe_embed(_texts):
    global unsafe_called
    unsafe_called = True
    return [[1.0]]


unsafe_semantic = content.ContentCatalog(
    unsafe_config,
    server.open_repository_file,
    database_path=ROOT / "unsafe-semantic.sqlite3",
    embed=unsafe_embed,
)
assert unsafe_semantic.rebuild()["semantic_available"] is False
assert unsafe_called is False

# Long UTF-8 inputs retain every byte, overlap, and include the matching tail passage.
long_body = "\n\n".join(f"Section {number}: " + "雪🚲 cycle storage " * 80 for number in range(20))
long_body += "\n\nThe tail needle is here."
long_page = {"title": "Storage " * 80, "content": long_body}
chunks = list(semantic_catalog._chunks(long_page))
assert chunks == list(semantic_catalog._chunks(long_page))
assert len(chunks) > content.EMBED_BATCH_SIZE
assert all(len(text.encode("utf-8")) <= content.EMBED_INPUT_BYTES for _digest, text, _body in chunks)
covered = 0
for _digest, _text, body in chunks:
    start = long_body.find(body, max(0, covered - content.EMBED_OVERLAP_BYTES))
    assert 0 <= start <= covered
    covered = start + len(body)
assert covered == len(long_body)
assert "tail needle" in chunks[-1][2]
assert list(semantic_catalog._chunks({"title": "Empty", "content": ""}))[0][2] == ""
assert list(semantic_catalog._chunks({**long_page, "title": "Renamed"}))[0][0] != chunks[0][0]

(USER / "knowledge" / "long.md").write_text("# Storage\n" + long_body)

def passage_embed(texts):
    semantic_calls.append(list(texts))
    return [[1.0, 0.0] if "tail needle" in text else [0.0, 1.0] for text in texts]


# Save successful batches even if a later batch fails; retry only missing chunks.
partial = content.ContentCatalog(
    SEMANTIC_CONFIG, server.open_repository_file, ROOT / "partial.sqlite3", embed=passage_embed,
)
pages, errors = partial._collect()
partial._replace(pages, errors)
with patch.object(content.time, "monotonic", side_effect=[0, 0, content.EMBED_WORK_SECONDS + 1]):
    assert partial._cache_embeddings(pages) is False
with sqlite3.connect(partial.database_path) as connection:
    assert connection.execute("SELECT count(*) FROM embeddings").fetchone()[0] == content.EMBED_BATCH_SIZE
first_batch = semantic_calls[-1]
partial.embed = failing_embed
assert partial.search("tail needle", "semantic")["semantic_available"] is False
partial.embed = passage_embed
before_recovery = len(semantic_calls)
matched = partial.search("tail needle", "semantic")
assert matched["semantic_available"] is True
assert matched["results"][0]["path"] == "knowledge/long.md"
assert "tail needle" in matched["results"][0]["snippet"]
assert len({(row["repository"], row["path"]) for row in matched["results"]}) == len(matched["results"])
assert all(len(batch) <= content.EMBED_BATCH_SIZE for batch in semantic_calls[before_recovery:])
assert not set(first_batch).intersection(text for batch in semantic_calls[before_recovery:] for text in batch)

# Restarts reuse the cache; edits, title/path changes, provider/model changes do not reuse stale input.
before_restart = len(semantic_calls)
restarted = content.ContentCatalog(
    SEMANTIC_CONFIG, server.open_repository_file, partial.database_path, embed=passage_embed,
)
assert restarted.rebuild()["semantic_available"] is True
assert len(semantic_calls) == before_restart
(USER / "knowledge" / "long.md").write_text("# Storage\nReplacement note.")
assert restarted.search("Replacement note", "semantic")["semantic_available"] is True
assert any("Replacement note" in text for batch in semantic_calls[before_restart:] for text in batch)
with sqlite3.connect(restarted.database_path) as connection:
    assert connection.execute("SELECT count(*) FROM embeddings").fetchone()[0] == len(pages)
for change in ({"dashboard_embedding_model": "other-model"},
               {"dashboard_embedding_url": "http://localhost:1234/v1/embeddings"}):
    restarted.config = replace(SEMANTIC_CONFIG, **change)
    before_change = len(semantic_calls)
    assert restarted.search("tail needle", "semantic")["semantic_available"] is True
    assert len(semantic_calls) > before_change + 1

# Lexical refresh does not wait for a failed embedding provider.
with patch.object(restarted, "_embed", side_effect=AssertionError("lexical must not embed")) as embed_call:
    (USER / "knowledge" / "long.md").write_text("# Storage\nLexicalonly refresh.")
    assert restarted.search("Lexicalonly", "lexical")["results"][0]["path"] == "knowledge/long.md"
    embed_call.assert_not_called()

# A v1 disposable cache is rebuilt without touching source data.
with sqlite3.connect(restarted.database_path) as connection:
    connection.execute("UPDATE catalog_meta SET value='1' WHERE key='schema_version'")
assert restarted.search("Lexicalonly", "lexical")["results"]
with sqlite3.connect(restarted.database_path) as connection:
    assert connection.execute("SELECT value FROM catalog_meta WHERE key='schema_version'").fetchone()[0] == content.SCHEMA_VERSION

# Validate the real HTTP adapter, including response ordering and local-only transport.
http_catalog = content.ContentCatalog(SEMANTIC_CONFIG, server.open_repository_file, ROOT / "http.sqlite3")
payloads = [
    {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]},
    {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]},
    {"data": [{"index": True, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]},
    {"data": [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}]},
    {"data": [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}]},
    {"data": [{"index": 0, "embedding": [float("nan"), 1]}, {"index": 1, "embedding": [0, 1]}]},
]
for number, payload in enumerate(payloads):
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
    with patch.object(content, "build_opener", return_value=opener) as factory:
        try:
            vectors = http_catalog._embed(["one", "two"])
            assert number == 0 and vectors == [[1, 0], [0, 1]]
        except ValueError:
            assert number != 0
    assert opener.open.call_args.kwargs["timeout"] == content.EMBED_TIMEOUT
    assert json.loads(opener.open.call_args.args[0].data)["input"] == ["one", "two"]
    assert factory.call_args.args[0].proxies == {}
    assert factory.call_args.args[1].redirect_request(None, None, 302, None, None, "https://example.invalid") is None
with patch.object(content, "build_opener") as factory:
    try:
        http_catalog._embed(["x" * (content.EMBED_INPUT_BYTES + 1)])
    except ValueError:
        pass
    else:
        raise AssertionError("oversized input must not be silently truncated")
    factory.assert_not_called()

print("dashboard content tests: PASS")
