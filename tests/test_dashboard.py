#!/usr/bin/env python3
"""Dashboard foundation tests - plain asserts, run directly."""
from __future__ import annotations

import contextlib
import dataclasses
import http.client
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ops import OpsConfig


server_path = Path(__file__).resolve().parents[1] / "dashboard" / "server.py"
spec = importlib.util.spec_from_file_location("dashboard_server", server_path)
server = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(server)

refused_bind = subprocess.run(
    [sys.executable, str(server_path), "--host", "0.0.0.0", "--port", "8765"],
    text=True,
    capture_output=True,
)
assert refused_bind.returncode != 0
assert "refusing non-localhost dashboard bind" in refused_bind.stderr


def rejects(call) -> None:
    try:
        call()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def problem_code(problem) -> str:
    assert problem is not None
    return problem[1]["error"]


root = Path(tempfile.mkdtemp())
engine = root / "engine"
agents = root / "agents"
user = root / "user"
for directory in (engine, agents, user):
    directory.mkdir()

config = OpsConfig(
    brain=root,
    engine=engine,
    agents=agents,
    user=user,
    dashboard_port=8765,
    dashboard_public_origin="https://brain.example",
)

assert server.success({"ready": True}) == {"ok": True, "result": {"ready": True}}
assert server.failure("not_found", "Not found.") == {
    "ok": False,
    "error": "not_found",
    "message": "Not found.",
}
assert server.allowed_origins(config) == frozenset({
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "https://brain.example",
})
assert server.allowed_hosts(config) == frozenset({
    "127.0.0.1:8765",
    "localhost:8765",
    "brain.example",
})
assert "https://brain.example" in server.allowed_origins(
    dataclasses.replace(config, dashboard_public_origin="https://BRAIN.EXAMPLE/")
)
for invalid_origin in (
    "https://user:pass@brain.example",
    "ftp://brain.example",
    "https://brain.example/path",
    "https://brain.example?query",
    "https://brain.example#fragment",
):
    rejects(lambda value=invalid_origin: server.allowed_origins(
        dataclasses.replace(config, dashboard_public_origin=value)
    ))

csrf = "known-csrf-token"
host = {"Host": "localhost:8765"}
assert server.validate_headers(host, config, csrf, mutation=False) is None
assert problem_code(server.validate_headers({}, config, csrf, mutation=False)) == "invalid_host"
assert problem_code(server.validate_headers(
    {"Host": "attacker.example"}, config, csrf, mutation=False
)) == "invalid_host"
assert problem_code(server.validate_headers(
    {**host, "Origin": "https://attacker.example"}, config, csrf, mutation=False
)) == "invalid_origin"

mutation_headers = {
    **host,
    "Origin": "http://localhost:8765",
    "X-CSRF-Token": csrf,
    "Content-Type": "application/json; charset=utf-8",
}
assert server.validate_headers(mutation_headers, config, csrf, mutation=True) is None
assert problem_code(server.validate_headers(
    {key: value for key, value in mutation_headers.items() if key != "Origin"},
    config,
    csrf,
    mutation=True,
)) == "invalid_origin"
assert problem_code(server.validate_headers(
    {**mutation_headers, "Origin": "https://attacker.example"},
    config,
    csrf,
    mutation=True,
)) == "invalid_origin"
assert problem_code(server.validate_headers(
    {key: value for key, value in mutation_headers.items() if key != "X-CSRF-Token"},
    config,
    csrf,
    mutation=True,
)) == "invalid_csrf"
assert problem_code(server.validate_headers(
    {**mutation_headers, "X-CSRF-Token": "wrong"}, config, csrf, mutation=True
)) == "invalid_csrf"
assert problem_code(server.validate_headers(
    {**mutation_headers, "Content-Type": "text/plain"}, config, csrf, mutation=True
)) == "unsupported_media_type"
assert problem_code(server.validate_headers(
    {**mutation_headers, "Transfer-Encoding": "chunked"}, config, csrf, mutation=True
)) == "unsupported_transfer_encoding"

profile = user / "profile"
profile.mkdir()
(profile / "bio.md").write_text("bio\n")
with server.open_repository_file(config, "user", "profile/bio.md") as handle:
    assert handle.read() == b"bio\n"
rejects(lambda: server.open_repository_file(config, "unknown", "profile/bio.md"))
rejects(lambda: server.open_repository_file(config, "user", ""))
rejects(lambda: server.open_repository_file(config, "user", "/etc/passwd"))
rejects(lambda: server.open_repository_file(config, "user", "profile/../profile/bio.md"))
rejects(lambda: server.open_repository_file(config, "user", "profile/bio.md\0ignored"))
rejects(lambda: server.open_repository_file(config, "user", "profile"))

(profile / "linked.md").symlink_to(profile / "bio.md")
rejects(lambda: server.open_repository_file(config, "user", "profile/linked.md"))
real = profile / "real"
real.mkdir()
(real / "inside.md").write_text("inside\n")
(profile / "alias").symlink_to(real, target_is_directory=True)
rejects(lambda: server.open_repository_file(config, "user", "profile/alias/inside.md"))
assert (profile / "bio.md").read_text() == "bio\n"
assert (real / "inside.md").read_text() == "inside\n"

status_fixture = {
    "observed_time": "2026-08-23T00:00:00+00:00",
    "repositories": [{
        "name": "engine",
        "status": "healthy",
        "head": "abcdef1234567890",
        "reason": "1 dirty path(s)",
        "dirty_paths": ["dashboard/server.py"],
    }],
    "proposals": {
        "status": "healthy",
        "reason": "1 pending",
        "buckets": [{"agent": "codex", "count": 1, "oldest_mtime": 1.0}],
    },
    "services": [{
        "id": "backup",
        "label": "Backup schedule",
        "status": "healthy",
        "reason": "active/waiting",
        "installed": True,
        "enabled": True,
        "active": True,
        "actions": ["start", "stop", "restart"],
    }, {
        "id": "brain_mcp", "label": "Remote brain-mcp", "status": "healthy",
        "reason": "active/running", "installed": True, "enabled": True,
        "active": True, "actions": ["start", "stop", "restart"],
    }],
    "brain_mcp": {
        "error": "",
        "remote": {
            "configured": True,
            "resource": "https://mcp.example/mcp",
            "clients": [{
                "name": "Client <one>",
                "profile": "claude",
                "created_at": "2026-08-23",
            }],
        },
    },
    "backup": {
        "status": "healthy",
        "reason": "backup and repository check succeeded within 36 hours",
        "last_success": "2026-08-23T00:00:00+00:00",
        "last_failure": "",
        "manual_restore_test": "2026-08-22T00:00:00+00:00",
        "stage": {
            "present": True,
            "verified": True,
            "verified_at": "2026-08-22T00:00:00+00:00",
            "restored_paths": ["user", "brain.config.toml"],
            "file_count": 2,
        },
    },
}
html_text = server.render_html('token"value', status_fixture)

class FakeCatalog:
    def __init__(self):
        self.calls = []

    def list_tree(self):
        self.calls.append(("tree",))
        return {"repositories": {}}

    def get_page(self, repository, path):
        self.calls.append(("page", repository, path))
        if path == "missing.md":
            raise server.ContentError(404, "not_found", "Content page was not found.")
        return {"repository": repository, "path": path}

    def search(self, query, mode):
        self.calls.append(("search", query, mode))
        return {"query": query, "mode": mode, "results": []}

    def backlinks(self, repository, path):
        self.calls.append(("backlinks", repository, path))
        if path == "catalog-down.md":
            raise server.ContentError(503, "catalog_unavailable", "Content catalog is unavailable.")
        return {"repository": repository, "path": path, "results": []}

    def status(self):
        self.calls.append(("status",))
        return {"available": True}

    def rebuild(self):
        self.calls.append(("rebuild",))
        return {"available": True}


fake_catalog = FakeCatalog()
content_routes = server.build_content_routes(fake_catalog)
assert set(content_routes) == {
    ("POST", "/api/content/tree"),
    ("POST", "/api/content/page"),
    ("POST", "/api/content/search"),
    ("POST", "/api/content/backlinks"),
    ("POST", "/api/content/catalog/status"),
    ("POST", "/api/content/catalog/rebuild"),
}
assert content_routes[("POST", "/api/content/tree")](None, None, {}) == (
    200,
    server.success({"repositories": {}}),
)
assert content_routes[("POST", "/api/content/page")](
    None, None, {"repository": "user", "path": "profile/goals.md"}
)[0] == 200
assert content_routes[("POST", "/api/content/search")](
    None, None, {"query": "sleep", "mode": "lexical"}
)[0] == 200
assert content_routes[("POST", "/api/content/backlinks")](
    None, None, {"repository": "user", "path": "profile/goals.md"}
)[0] == 200
assert content_routes[("POST", "/api/content/catalog/status")](None, None, {})[0] == 200
assert content_routes[("POST", "/api/content/catalog/rebuild")](None, None, {})[0] == 200
calls_before_invalid = list(fake_catalog.calls)
for payload in (None, {}, {"repository": "user", "path": 1}):
    response = content_routes[("POST", "/api/content/page")](None, None, payload)
    assert response[0] == 400 and response[1]["error"] == "invalid_request"
assert fake_catalog.calls == calls_before_invalid
missing = content_routes[("POST", "/api/content/page")](
    None, None, {"repository": "user", "path": "missing.md"}
)
assert missing == (404, server.failure("not_found", "Content page was not found."))
degraded_target = {"repository": "user", "path": "catalog-down.md"}
assert content_routes[("POST", "/api/content/page")](None, None, degraded_target)[0] == 200
degraded_backlinks = content_routes[("POST", "/api/content/backlinks")](
    None, None, degraded_target
)
assert degraded_backlinks == (
    503,
    server.failure("catalog_unavailable", "Content catalog is unavailable."),
)


class FakeEditing:
    def __init__(self):
        self.calls = []

    def preview(self, content):
        self.calls.append(("preview", content))
        return {"rendered_html": "<p>safe</p>"}

    def create_page(self, path, content):
        self.calls.append(("create", path, content))
        return {"path": path}

    def edit_page(self, path, content, digest):
        self.calls.append(("edit", path, content, digest))
        if path == "profile/fail.md":
            raise server.EditingError(
                500, "commit_failed", "Commit failed.", {"partial": True, "paths": [path]}
            )
        return {"path": path}

    def rename_page(self, path, destination, digest):
        self.calls.append(("rename", path, destination, digest))
        return {"path": destination}

    def move_page(self, path, destination, digest):
        self.calls.append(("move", path, destination, digest))
        return {"path": destination}

    def archive_page(self, path, digest, replacement=""):
        self.calls.append(("archive", path, digest, replacement))
        return {"path": path}

    def delete_page(self, path, digest):
        self.calls.append(("delete", path, digest))
        return {"path": path}

    def undo_page(self, path, digest):
        self.calls.append(("undo", path, digest))
        return {"path": path}


fake_editing = FakeEditing()
editing_routes = server.build_editing_routes(fake_editing)
assert set(editing_routes) == {
    ("POST", f"/api/editing/{path}")
    for path in ("preview", "create", "edit", "rename", "move", "archive", "delete", "undo")
}
editing_context = {}
assert editing_routes[("POST", "/api/editing/edit")](None, editing_context, {
    "path": "profile/edit.md", "content": "draft", "expected_digest": "abc"
}) == (200, server.success({"path": "profile/edit.md"}))
assert editing_context == {"action": "edit", "target": "user/profile/edit.md"}
calls_before_invalid = list(fake_editing.calls)
assert editing_routes[("POST", "/api/editing/create")](
    None, None, {"path": "profile/new.md"}
)[0] == 400
assert fake_editing.calls == calls_before_invalid
partial = editing_routes[("POST", "/api/editing/edit")](None, None, {
    "path": "profile/fail.md", "content": "draft", "expected_digest": "abc"
})
assert partial == (
    500,
    {
        **server.failure("commit_failed", "Commit failed."),
        "result": {"partial": True, "paths": ["profile/fail.md"]},
    },
)

html_text = server.render_html('token"value', status_fixture)
for expected in (
    "<!doctype html>",
    "<title>Brain Dashboard</title>",
    "<h1>Brain Dashboard</h1>",
    'href="#main"',
    '<nav aria-label="Brain sections">',
    '<main id="main" tabindex="-1">',
    'content="token&quot;value"',
    "/static/app.css",
    "/static/app.js",
    "Service and health",
    "Repositories",
    "Proposals",
    "Backup schedule",
    "Brain-mcp",
    "Client &lt;one&gt;",
    "Add client",
    "Backup and restore",
    "Run backup now",
    "Stage latest restore",
    "Verify staged restore",
    "Replace current data",
    "brain.config.toml",
    "dashboard/server.py",
    'data-action="restart"',
    '<form id="content-search">',
    'id="search-mode"',
    'id="search-results"',
    'id="content-tree"',
    'id="page-title"',
    'id="page-rendered"',
    'id="page-source"',
    'id="backlinks"',
    'id="catalog-status"',
    'id="catalog-rebuild"',
    'id="semantic-status"',
    'id="page-edit"',
    'id="page-new"',
    'id="page-editor-form"',
    'id="page-editor"',
    'id="page-actions"',
    'id="page-move"',
    'id="page-archive"',
    'id="page-delete"',
    'id="page-undo"',
    'id="editor-status"',
    '<script src="/static/app.js" defer></script>',
):
    assert expected in html_text
for forbidden in (
    'token"value',
    "Client <one>",
    "Favorites",
    "Graph",
    "Local STDIO",
    "Verify local connection",
    "data-mcp-action",
):
    assert forbidden not in html_text

mcp_html = server._brain_mcp_html(status_fixture["brain_mcp"])
assert 'class="status ' not in mcp_html and "unknown" not in mcp_html
assert "Service:" not in mcp_html and "data-service" not in mcp_html
assert "Remote brain-mcp" in html_text
for action in ("start", "stop", "restart"):
    assert f'data-service="brain_mcp" data-action="{action}"' in html_text
assert 'data-mcp-revoke="Client &lt;one&gt;"' in mcp_html
assert '<pre id="brain-mcp-credentials" hidden></pre>' in mcp_html
uninitialized_html = server._brain_mcp_html({
    "error": "",
    "remote": {"configured": False, "resource": "", "clients": []},
})
assert "Not initialized" in uninitialized_html
assert "brain-mcp-add" not in uninitialized_html
unavailable_html = server.render_html('token', {
    **status_fixture,
    "brain_mcp": {"error": "remote state is unavailable <detail>"},
})
assert '<p role="alert">remote state is unavailable &lt;detail&gt;</p>' in unavailable_html
assert "brain-mcp-add" not in unavailable_html and "data-mcp-revoke" not in unavailable_html
assert "Backup schedule" in unavailable_html and "Backup and restore" in unavailable_html
assert 'data-service="brain_mcp" data-action="restart"' in unavailable_html

event = {
    "time": "2026-08-17T00:00:00+00:00",
    "actor": "owner@example.invalid",
    "action": "POST",
    "target": "/api/echo",
    "outcome": "ok",
    "latency_ms": 1.25,
    "correlation_id": "abc123",
    "body": "body-secret",
    "cookie": "cookie-secret",
    "authorization": "authorization-secret",
    "csrf": "csrf-secret",
}
event_output = io.StringIO()
with contextlib.redirect_stderr(event_output):
    server.emit_event(event)
logged = json.loads(event_output.getvalue())
assert logged == {key: event[key] for key in (
    "time",
    "actor",
    "action",
    "target",
    "outcome",
    "latency_ms",
    "correlation_id",
)}


calls = []


def echo(_config, _context, payload):
    calls.append(payload)
    return 200, server.success({"value": payload["value"]})


def fail_route(_config, _context, _payload):
    raise RuntimeError("route-secret")


backup_calls = []


def fake_backup_action(_config, action, confirmed=False):
    if action not in {"run", "stage", "verify", "replace"}:
        raise ValueError("unsupported")
    if action == "replace" and not confirmed:
        raise ValueError("confirmation required")
    backup_calls.append((action, confirmed))
    return {"action": action, "command_ok": True, "status": status_fixture["backup"]}


server.backup_action = fake_backup_action

mcp_calls = []
one_time_client_id = "one-time-" + "client-id"
one_time_client_secret = "one-time-" + "client-secret"


def fake_brain_mcp_status():
    return status_fixture["brain_mcp"]


def fake_brain_mcp_action(
    action,
    *,
    name="",
    profile="",
    redirect_uri=None,
    confirmed=False,
):
    mcp_calls.append((action, name, profile, redirect_uri, confirmed))
    if action == "add-client":
        return {
            "action": action,
            "resource": "https://mcp.example/mcp",
            "name": name,
            "profile": profile,
            "client_id": one_time_client_id,
            "client_secret": one_time_client_secret,
        }
    if action == "revoke-client" and confirmed:
        return {"action": action, "name": name}
    raise ValueError("unsupported brain-mcp action")


server.brain_mcp_status = fake_brain_mcp_status
server.brain_mcp_action = fake_brain_mcp_action


class TestHandler(server.Handler):
    config = config
    csrf_token = csrf
    routes = {
        **server.Handler.routes,
        ("POST", "/api/echo"): echo,
        ("POST", "/api/fail"): fail_route,
    }
    status_provider = staticmethod(lambda _config: status_fixture)


httpd = HTTPServer(("127.0.0.1", 0), TestHandler)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
port = httpd.server_address[1]
responses = []


def request(method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.putrequest(method, path, skip_host=True)
    for name, value in (headers or host).items():
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    responses.append(result)
    connection.close()
    return result


def json_response(response):
    return json.loads(response[2])


http_events = io.StringIO()
try:
    with contextlib.redirect_stderr(http_events):
        shell_response = request("GET", "/")
        assert shell_response[0] == 200 and b"Brain Dashboard" in shell_response[2]
        assert shell_response[1]["Content-Security-Policy"] == (
            "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        assert shell_response[1]["X-Content-Type-Options"] == "nosniff"
        assert shell_response[1]["Referrer-Policy"] == "no-referrer"
        assert shell_response[1]["Cache-Control"] == "no-store"
        assert shell_response[1]["X-Correlation-ID"]
        assert "Python" not in shell_response[1].get("Server", "")

        css_response = request("GET", "/static/app.css")
        assert css_response[0] == 200
        assert css_response[1]["Content-Type"].startswith("text/css")
        assert b"@media (min-width: 48rem)" in css_response[2]

        js_response = request("GET", "/static/app.js")
        assert js_response[0] == 200
        assert js_response[1]["Content-Type"].startswith("text/javascript")
        assert b"/api/services/action" in js_response[2]
        assert b"/api/backup/action" in js_response[2]
        assert b"/api/brain-mcp/action" in js_response[2]
        assert b"window.confirm" in js_response[2]
        assert b"data-mcp-action" not in js_response[2]
        assert b"Local verification" not in js_response[2]
        assert b"localStorage" not in js_response[2] and b"sessionStorage" not in js_response[2]
        assert b"Revoke ${name} and all of its active credentials?" in js_response[2]

        system_response = request("GET", "/api/system/status")
        assert system_response[0] == 200
        assert set(json_response(system_response)["result"]) == {
            "observed_time", "repositories", "proposals", "services",
        }

        backup_status_response = request("GET", "/api/backup/status")
        assert backup_status_response[0] == 200
        assert json_response(backup_status_response)["result"]["subsystem"] == "backup"

        mcp_status_response = request("GET", "/api/brain-mcp/status")
        assert mcp_status_response[0] == 200
        assert json_response(mcp_status_response)["result"]["remote"]["clients"] == (
            status_fixture["brain_mcp"]["remote"]["clients"]
        )
        assert "local" not in json_response(mcp_status_response)["result"]
        assert "status" not in json_response(mcp_status_response)["result"]
        assert b"document.createElement" in js_response[2]
        assert b"textContent" in js_response[2]
        assert js_response[2].count(b".innerHTML") == 1
        assert b"Promise.all" not in js_response[2]
        assert b'document.createElement("details")' in js_response[2]
        assert b'document.createElement("summary")' in js_response[2]
        assert b"Backlinks unavailable" in js_response[2]
        assert b"page.snippet" in js_response[2]
        assert b'api("/api/content/catalog/rebuild")' in js_response[2]
        assert b'api(`/api/editing/${route}`' in js_response[2]
        assert b'api("/api/editing/archive"' in js_response[2]
        assert b'api("/api/editing/delete"' in js_response[2]
        assert b'api("/api/editing/undo"' in js_response[2]
        assert b"window.confirm" in js_response[2]
        assert b"dirty" in js_response[2]
        assert b"Rebuilding catalog" in js_response[2]
        assert b"http://" not in js_response[2] and b"https://" not in js_response[2]

        body = json.dumps({"value": "body-secret-value"}).encode()
        valid_headers = {
            **mutation_headers,
            "Content-Length": str(len(body)),
            "Cookie": "cookie-secret-value",
            "Authorization": "authorization-secret-value",
            "Cf-Access-Authenticated-User-Email": "owner@example.invalid",
        }
        echo_response = request("POST", "/api/echo", body, valid_headers)
        assert echo_response[0] == 200
        assert json_response(echo_response) == server.success({"value": "body-secret-value"})
        assert calls == [{"value": "body-secret-value"}]

        missing_length = request("POST", "/api/echo", headers=mutation_headers)
        assert missing_length[0] == 411
        assert json_response(missing_length)["error"] == "length_required"

        invalid_length = request(
            "POST", "/api/echo", headers={**mutation_headers, "Content-Length": "nope"}
        )
        assert invalid_length[0] == 400
        assert json_response(invalid_length)["error"] == "invalid_content_length"

        negative_length = request(
            "POST", "/api/echo", headers={**mutation_headers, "Content-Length": "-1"}
        )
        assert negative_length[0] == 400
        assert json_response(negative_length)["error"] == "invalid_content_length"

        large_length = request(
            "POST",
            "/api/echo",
            headers={**mutation_headers, "Content-Length": str(server.MAX_POST + 1)},
        )
        assert large_length[0] == 413
        assert json_response(large_length)["error"] == "request_too_large"

        unknown_large = request(
            "POST",
            "/api/unknown",
            headers={**mutation_headers, "Content-Length": str(server.MAX_POST + 1)},
        )
        assert unknown_large[0] == 413
        assert json_response(unknown_large)["error"] == "request_too_large"

        malformed = b"{"
        malformed_response = request(
            "POST",
            "/api/echo",
            malformed,
            {**mutation_headers, "Content-Length": str(len(malformed))},
        )
        assert malformed_response[0] == 400
        assert json_response(malformed_response)["error"] == "invalid_json"

        array = b"[]"
        array_response = request(
            "POST",
            "/api/echo",
            array,
            {**mutation_headers, "Content-Length": str(len(array))},
        )
        assert array_response[0] == 400
        assert json_response(array_response)["error"] == "invalid_request"
        assert calls == [{"value": "body-secret-value"}]

        status_response = request("GET", "/api/status?query-secret")
        assert status_response[0] == 404
        assert json_response(status_response)["error"] == "not_found"

        empty = b"{}"
        write_response = request(
            "POST",
            "/api/user/write",
            empty,
            {**mutation_headers, "Content-Length": str(len(empty))},
        )
        assert write_response[0] == 404
        assert json_response(write_response)["error"] == "not_found"

        invalid_action = json.dumps({"service": "dashboard", "action": "restart"}).encode()
        invalid_action_response = request(
            "POST",
            "/api/services/action",
            invalid_action,
            {**mutation_headers, "Content-Length": str(len(invalid_action))},
        )
        assert invalid_action_response[0] == 400
        assert json_response(invalid_action_response)["error"] == "invalid_operation"

        stage_body = json.dumps({"action": "stage"}).encode()
        stage_response = request(
            "POST",
            "/api/backup/action",
            stage_body,
            {**mutation_headers, "Content-Length": str(len(stage_body))},
        )
        assert stage_response[0] == 200
        assert backup_calls[-1] == ("stage", False)

        replace_body = json.dumps({"action": "replace"}).encode()
        replace_response = request(
            "POST",
            "/api/backup/action",
            replace_body,
            {**mutation_headers, "Content-Length": str(len(replace_body))},
        )
        assert replace_response[0] == 400
        assert json_response(replace_response)["error"] == "invalid_operation"

        confirmed_body = json.dumps({"action": "replace", "confirm": True}).encode()
        confirmed_response = request(
            "POST",
            "/api/backup/action",
            confirmed_body,
            {**mutation_headers, "Content-Length": str(len(confirmed_body))},
        )
        assert confirmed_response[0] == 200
        assert backup_calls[-1] == ("replace", True)

        removed_action = json.dumps({"action": "verify-local"}).encode()
        removed_action_response = request(
            "POST",
            "/api/brain-mcp/action",
            removed_action,
            {**mutation_headers, "Content-Length": str(len(removed_action))},
        )
        assert removed_action_response[0] == 400
        assert json_response(removed_action_response)["error"] == "invalid_operation"

        add_client = json.dumps({
            "action": "add-client",
            "name": "New Claude",
            "profile": "claude",
        }).encode()
        add_client_response = request(
            "POST",
            "/api/brain-mcp/action",
            add_client,
            {**mutation_headers, "Content-Length": str(len(add_client))},
        )
        assert add_client_response[0] == 200
        assert json_response(add_client_response)["result"]["client_secret"] == (
            one_time_client_secret
        )

        revoke_client = json.dumps({
            "action": "revoke-client",
            "name": "Client <one>",
        }).encode()
        revoke_client_response = request(
            "POST",
            "/api/brain-mcp/action",
            revoke_client,
            {**mutation_headers, "Content-Length": str(len(revoke_client))},
        )
        assert revoke_client_response[0] == 400

        confirmed_revoke = json.dumps({
            "action": "revoke-client",
            "name": "Client <one>",
            "confirm": True,
        }).encode()
        confirmed_revoke_response = request(
            "POST",
            "/api/brain-mcp/action",
            confirmed_revoke,
            {**mutation_headers, "Content-Length": str(len(confirmed_revoke))},
        )
        assert confirmed_revoke_response[0] == 200
        assert mcp_calls[-1] == (
            "revoke-client", "Client <one>", "", None, True,
        )

        method_response = request("PUT", "/api/echo")
        assert method_response[0] == 405
        assert json_response(method_response)["error"] == "method_not_allowed"

        fail_response = request(
            "POST",
            "/api/fail",
            empty,
            {**mutation_headers, "Content-Length": str(len(empty))},
        )
        assert fail_response[0] == 500
        assert json_response(fail_response)["error"] == "internal_error"
        assert b"route-secret" not in fail_response[2]
finally:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)

lines = http_events.getvalue().splitlines()
assert len(lines) == len(responses)
events = [json.loads(line) for line in lines]
assert all(set(item) == {
    "time",
    "actor",
    "action",
    "target",
    "outcome",
    "latency_ms",
    "correlation_id",
} for item in events)
assert any(item["actor"] == "owner@example.invalid" for item in events)
assert any(item["target"] == "/api/status" for item in events)
event_text = http_events.getvalue()
for forbidden in (
    "query-secret",
    "body-secret-value",
    "cookie-secret-value",
    "authorization-secret-value",
    csrf,
    "route-secret",
    one_time_client_secret,
    one_time_client_id,
):
    assert forbidden not in event_text
assert len({headers["X-Correlation-ID"] for _, headers, _ in responses}) == len(responses)

print("dashboard foundation tests: PASS")
