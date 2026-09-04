#!/usr/bin/env python3
"""Loopback-only Brain Dashboard HTTP foundation."""
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.content import MAX_CONTENT, ContentCatalog, ContentError
from dashboard.editing import EditingError, UserEditing
from lib.ops import (
    backup_action,
    backup_health,
    brain_mcp_action,
    brain_mcp_status,
    load_ops_config,
    service_action,
    system_status,
)


MAX_POST = 6 * MAX_CONTENT + 64 * 1024
STATIC_CSS = Path(__file__).with_name("static") / "app.css"
STATIC_JS = Path(__file__).with_name("static") / "app.js"
EVENT_FIELDS = (
    "time",
    "actor",
    "action",
    "target",
    "outcome",
    "latency_ms",
    "correlation_id",
)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _header(headers, name: str) -> str:
    return next(
        (str(value) for key, value in headers.items() if str(key).lower() == name.lower()),
        "",
    ).strip()


def success(result: dict) -> dict:
    return {"ok": True, "result": result}


def failure(error: str, message: str) -> dict:
    return {"ok": False, "error": error, "message": message}


def _normalized_origin(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid dashboard public origin") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid dashboard public origin")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def allowed_origins(config) -> frozenset[str]:
    origins = {
        f"http://127.0.0.1:{config.dashboard_port}",
        f"http://localhost:{config.dashboard_port}",
    }
    if config.dashboard_public_origin:
        origins.add(_normalized_origin(config.dashboard_public_origin))
    return frozenset(origins)


def allowed_hosts(config) -> frozenset[str]:
    return frozenset(urlsplit(origin).netloc for origin in allowed_origins(config))


def validate_headers(headers: dict, config, csrf_token: str, mutation: bool):
    if _header(headers, "Host").lower() not in allowed_hosts(config):
        return 403, failure("invalid_host", "Request Host is not allowed.")
    origin = _header(headers, "Origin")
    if origin and origin not in allowed_origins(config):
        return 403, failure("invalid_origin", "Request Origin is not allowed.")
    if mutation and not origin:
        return 403, failure("invalid_origin", "Request Origin is required.")
    if mutation and not secrets.compare_digest(_header(headers, "X-CSRF-Token"), csrf_token):
        return 403, failure("invalid_csrf", "CSRF token is invalid.")
    if mutation and _header(headers, "Transfer-Encoding"):
        return 400, failure(
            "unsupported_transfer_encoding",
            "Transfer-Encoding is not supported.",
        )
    content_type = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
    if mutation and content_type != "application/json":
        return 415, failure("unsupported_media_type", "Content-Type must be application/json.")
    return None


def open_repository_file(config, repository: str, relative: str):
    roots = {"user": config.user, "agents": config.agents, "engine": config.engine}
    if repository not in roots:
        raise ValueError("unknown repository")
    if (
        not relative
        or relative.startswith("/")
        or "\0" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("invalid repository-relative path")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(roots[repository], directory_flags)
        parts = relative.split("/")
        for component in parts[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("repository target is not a regular file")
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle
    except (OSError, ValueError) as exc:
        raise ValueError("unsafe repository path") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _status_badge(status: str) -> str:
    value = html.escape(status)
    return f'<span class="status {value}">{value}</span>'


def _brain_mcp_html(status: dict) -> str:
    if status["error"]:
        return (
            '<section><h3>Brain-mcp</h3><p role="alert">'
            f'{html.escape(status["error"])}</p></section>'
        )
    remote = status["remote"]
    clients = "".join(
        f"<li><strong>{html.escape(client['name'])}</strong> · "
        f"{html.escape(client['profile'])} · {html.escape(client['created_at'])} "
        f'<button type="button" data-mcp-revoke="{html.escape(client["name"], quote=True)}">'
        "Revoke</button></li>"
        for client in remote["clients"]
    ) or "<li>None</li>"
    add_form = ""
    if remote["configured"]:
        add_form = """<form id="brain-mcp-add">
<label>Unique name <input name="name" required></label>
<label>Client type <select name="profile">
<option value="chatgpt">ChatGPT</option><option value="claude">Claude</option>
<option value="hermes">Hermes</option></select></label>
<label>ChatGPT HTTPS callback <input name="redirect_uri" type="url"></label>
<button type="submit">Add client</button>
</form>"""
    resource = html.escape(remote["resource"] or "Not initialized")
    return f"""<section><h3>Brain-mcp</h3>
<article><h4>Remote</h4><p>Resource: <code>{resource}</code></p>
<h5>Clients</h5><ul>{clients}</ul>{add_form}
<pre id="brain-mcp-credentials" hidden></pre></article></section>"""


def render_html(csrf_token: str, status: dict) -> str:
    token = html.escape(csrf_token, quote=True)
    repositories = "".join(
        f"<article><h3>{html.escape(repo['name'])} {_status_badge(repo['status'])}</h3>"
        f"<p><code>{html.escape(repo['head'][:12] or 'unavailable')}</code> · "
        f"{html.escape(repo['reason'])}</p>"
        + (
            "<details><summary>Dirty paths</summary><ul>"
            + "".join(f"<li><code>{html.escape(path)}</code></li>" for path in repo["dirty_paths"])
            + "</ul></details>"
            if repo["dirty_paths"] else ""
        )
        + "</article>"
        for repo in status["repositories"]
    )
    proposal_items = "".join(
        f"<li>{html.escape(bucket['agent'])}: {bucket['count']}</li>"
        for bucket in status["proposals"]["buckets"]
    ) or "<li>None</li>"
    services = "".join(
        f'<article><h3>{html.escape(service["label"])} {_status_badge(service["status"])}</h3>'
        f'<p>{html.escape(service["reason"])} · installed {str(service["installed"]).lower()} · '
        f'enabled {str(service["enabled"]).lower()} · active {str(service["active"]).lower()}</p>'
        + "".join(
            f'<button type="button" data-service="{html.escape(service["id"], quote=True)}" '
            f'data-action="{html.escape(action, quote=True)}">{html.escape(action.title())}</button>'
            for action in service["actions"]
        )
        + "</article>"
        for service in status["services"]
    )
    backup = status["backup"]
    stage = backup["stage"]
    restored_paths = "".join(
        f"<li><code>{html.escape(path)}</code></li>" for path in stage["restored_paths"]
    ) or "<li>None</li>"
    stage_actions = (
        '<button type="button" data-backup-action="verify">Verify staged restore</button>'
        if stage["present"] else ""
    ) + (
        '<button type="button" data-backup-action="replace">'
        "Replace current data</button>"
        if stage["verified"] else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{token}">
<title>Brain Dashboard</title>
<link rel="stylesheet" href="/static/app.css">
<script src="/static/app.js" defer></script>
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
<header><h1>Brain Dashboard</h1></header>
<div class="shell">
<nav aria-label="Brain sections">
<h2>Library</h2>
<p id="catalog-status" aria-live="polite">Loading catalog status…</p>
<button id="catalog-rebuild" type="button">Rebuild catalog</button>
<p id="semantic-status" aria-live="polite">Loading semantic status…</p>
<div id="content-tree" aria-live="polite">Loading content…</div>
</nav>
<main id="main" tabindex="-1">
<section aria-labelledby="search-heading">
<h2 id="search-heading">Search</h2>
<form id="content-search">
<label for="search-query">Query</label>
<input id="search-query" name="query" type="search" required>
<label for="search-mode">Mode</label>
<select id="search-mode" name="mode">
<option value="lexical">Lexical</option>
<option value="semantic">Semantic</option>
</select>
<button type="submit">Search</button>
</form>
<p id="search-status" aria-live="polite"></p>
<ul id="search-results" class="result-list"></ul>
</section>
<article aria-labelledby="page-title">
<h2 id="page-title">Select a page</h2>
<div class="page-toolbar" role="group" aria-label="Page view">
<button id="show-rendered" type="button" aria-pressed="true">Rendered</button>
<button id="show-source" type="button" aria-pressed="false">Source</button>
<button id="page-edit" type="button" class="is-hidden">Edit</button>
<button id="page-new" type="button">New user file</button>
</div>
<p id="editor-status" aria-live="polite"></p>
<form id="page-editor-form" class="is-hidden">
<label for="editor-path">User path</label>
<input id="editor-path" name="path" required>
<label id="editor-content-label" for="page-editor">Content</label>
<textarea id="page-editor" name="content" rows="20"></textarea>
<div class="editor-buttons">
<button type="submit">Save and commit</button>
<button id="editor-cancel" type="button">Cancel</button>
</div>
</form>
<div id="page-actions" class="page-actions is-hidden">
<label for="destination-path">New path</label>
<input id="destination-path">
<button id="page-move" type="button">Move / rename</button>
<button id="page-archive" type="button">Archive</button>
<button id="page-delete" type="button">Delete</button>
<button id="page-undo" type="button">Undo last change</button>
</div>
<div id="page-rendered"></div>
<pre id="page-source" class="is-hidden" tabindex="0"></pre>
<section aria-labelledby="backlinks-heading">
<h3 id="backlinks-heading">Backlinks</h3>
<ul id="backlinks" class="result-list" aria-live="polite"></ul>
</section>
</article>
<section aria-labelledby="operations-heading">
<h2 id="operations-heading">Service and health</h2>
<p>Observed <time>{html.escape(status['observed_time'])}</time>.</p>
<section><h3>Repositories</h3><div class="cards">{repositories}</div></section>
<section><h3>Proposals {_status_badge(status['proposals']['status'])}</h3>
<p>{html.escape(status['proposals']['reason'])}</p><ul>{proposal_items}</ul></section>
<section><h3>Services</h3><div class="cards">{services}</div></section>
{_brain_mcp_html(status['brain_mcp'])}
<section><h3>Backup and restore {_status_badge(backup['status'])}</h3>
<p>{html.escape(backup['reason'])}</p>
<p>Last successful backup and check: <time>{html.escape(backup['last_success'] or 'Never')}</time>.<br>
Last manual restore test: <time>{html.escape(backup['manual_restore_test'] or 'Never')}</time>.</p>
<button type="button" data-backup-action="run">Run backup now</button>
<button type="button" data-backup-action="stage">Stage latest restore</button>
<article><h4>Staged restore {_status_badge('healthy' if stage['verified'] else 'unknown')}</h4>
<p>{'Verified ' + html.escape(stage['verified_at']) if stage['verified'] else ('Awaiting verification' if stage['present'] else 'None staged')}.</p>
<ul>{restored_paths}</ul>{stage_actions}</article>
</section>
<p id="operation-result" role="status" aria-live="polite"></p>
</section>
</main>
</div>
</body>
</html>
"""


def system_status_response(config, _context, _payload):
    return 200, success(system_status(config))


def backup_status_response(config, _context, _payload):
    return 200, success(backup_health(config))


def brain_mcp_status_response(_config, _context, _payload):
    return 200, success(brain_mcp_status())


def service_action_response(_config, _context, payload):
    try:
        result = service_action(payload["service"], payload["action"])
    except (KeyError, TypeError, ValueError):
        return 400, failure("invalid_operation", "Service action is not allowed.")
    if not result["command_ok"]:
        return 409, failure(
            "service_action_failed",
            f"Service action failed; observed state is {result['after']['reason']}.",
        )
    return 200, success(result)


def backup_action_response(config, _context, payload):
    try:
        result = backup_action(
            config,
            payload["action"],
            confirmed=payload.get("confirm") is True,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return 400, failure("invalid_operation", "Backup action is not allowed.")
    if not result["command_ok"]:
        return 409, failure(
            "backup_action_failed",
            "Backup action failed; current data was not replaced unless a confirmed replacement completed.",
        )
    return 200, success(result)


def brain_mcp_action_response(_config, _context, payload):
    try:
        result = brain_mcp_action(
            payload["action"],
            name=payload.get("name", ""),
            profile=payload.get("profile", ""),
            redirect_uri=payload.get("redirect_uri"),
            confirmed=payload.get("confirm") is True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return 400, failure(
            "invalid_operation",
            str(exc) if isinstance(exc, ValueError) else "Brain-mcp action is not allowed.",
        )
    except OSError:
        return 409, failure("brain_mcp_action_failed", "Brain-mcp action failed.")
    return 200, success(result)


def dashboard_status(config) -> dict:
    status = system_status(config)
    status["backup"] = backup_health(config)
    status["brain_mcp"] = brain_mcp_status()
    return status


def build_content_routes(catalog):
    def adapt(call, fields=()):
        def route(_config, _context, payload):
            if payload is None or any(not isinstance(payload.get(field), str) for field in fields):
                return 400, failure("invalid_request", "Request fields are invalid.")
            try:
                return 200, success(call(*(payload[field] for field in fields)))
            except ContentError as exc:
                return exc.status, failure(exc.code, exc.message)

        return route

    return {
        ("POST", "/api/content/tree"): adapt(catalog.list_tree),
        ("POST", "/api/content/page"): adapt(catalog.get_page, ("repository", "path")),
        ("POST", "/api/content/search"): adapt(catalog.search, ("query", "mode")),
        ("POST", "/api/content/backlinks"): adapt(catalog.backlinks, ("repository", "path")),
        ("POST", "/api/content/catalog/status"): adapt(catalog.status),
        ("POST", "/api/content/catalog/rebuild"): adapt(catalog.rebuild),
    }


def build_editing_routes(editing):
    def adapt(action, call, fields, optional=()):
        def route(_config, context, payload):
            if payload is None or any(not isinstance(payload.get(field), str) for field in fields):
                return 400, failure("invalid_request", "Request fields are invalid.")
            if any(field in payload and not isinstance(payload[field], str) for field in optional):
                return 400, failure("invalid_request", "Request fields are invalid.")
            if context is not None:
                context["action"] = action
                context["target"] = f"user/{payload.get('destination') or payload.get('path', '')}"
            try:
                arguments = [payload[field] for field in fields]
                arguments.extend(payload.get(field, "") for field in optional)
                return 200, success(call(*arguments))
            except EditingError as exc:
                problem = failure(exc.code, exc.message)
                if exc.result is not None:
                    problem["result"] = exc.result
                return exc.status, problem

        return route

    return {
        ("POST", "/api/editing/preview"): adapt("preview", editing.preview, ("content",)),
        ("POST", "/api/editing/create"): adapt("create", editing.create_page, ("path", "content")),
        ("POST", "/api/editing/edit"): adapt(
            "edit", editing.edit_page, ("path", "content", "expected_digest")
        ),
        ("POST", "/api/editing/rename"): adapt(
            "rename", editing.rename_page, ("path", "destination", "expected_digest")
        ),
        ("POST", "/api/editing/move"): adapt(
            "move", editing.move_page, ("path", "destination", "expected_digest")
        ),
        ("POST", "/api/editing/archive"): adapt(
            "archive", editing.archive_page, ("path", "expected_digest"), ("replacement",)
        ),
        ("POST", "/api/editing/delete"): adapt(
            "delete", editing.delete_page, ("path", "expected_digest")
        ),
        ("POST", "/api/editing/undo"): adapt(
            "undo", editing.undo_page, ("path", "expected_digest")
        ),
    }


def emit_event(event: dict) -> None:
    print(
        json.dumps(
            {field: event.get(field) for field in EVENT_FIELDS},
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )


class Handler(BaseHTTPRequestHandler):
    config = None
    csrf_token = ""
    routes = {
        ("GET", "/api/system/status"): system_status_response,
        ("POST", "/api/services/action"): service_action_response,
        ("GET", "/api/backup/status"): backup_status_response,
        ("POST", "/api/backup/action"): backup_action_response,
        ("GET", "/api/brain-mcp/status"): brain_mcp_status_response,
        ("POST", "/api/brain-mcp/action"): brain_mcp_action_response,
    }
    status_provider = staticmethod(dashboard_status)
    server_version = "BrainDashboard"
    sys_version = ""

    def _send(self, code: int, body: str | bytes, content_type: str, correlation_id: str) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Correlation-ID", correlation_id)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            return 411, failure("length_required", "Content-Length is required.")
        if len(lengths) != 1 or not lengths[0].isdecimal():
            return 400, failure("invalid_content_length", "Content-Length is invalid.")
        length = int(lengths[0])
        if length > MAX_POST:
            return 413, failure("request_too_large", "Request is too large.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, failure("invalid_json", "Request body is not valid JSON.")
        if not isinstance(payload, dict):
            return 400, failure("invalid_request", "Request body must be a JSON object.")
        return None, payload

    def _handle(self) -> None:
        started = time.monotonic()
        correlation_id = uuid.uuid4().hex
        method = self.command
        path = urlsplit(self.path).path
        event_action = method
        event_target = path
        actor = _header(self.headers, "Cf-Access-Authenticated-User-Email")
        outcome = "internal_error"
        try:
            problem = validate_headers(
                self.headers,
                self.config,
                self.csrf_token,
                mutation=method == "POST",
            )
            if problem:
                code, payload = problem
                outcome = payload["error"]
                self._send(code, json.dumps(payload), "application/json", correlation_id)
                return

            if method == "GET" and path == "/":
                outcome = "ok"
                self._send(
                    200,
                    render_html(self.csrf_token, self.status_provider(self.config)),
                    "text/html; charset=utf-8",
                    correlation_id,
                )
                return
            if method == "GET" and path == "/static/app.css":
                outcome = "ok"
                self._send(200, STATIC_CSS.read_bytes(), "text/css; charset=utf-8", correlation_id)
                return
            if method == "GET" and path == "/static/app.js":
                outcome = "ok"
                self._send(
                    200,
                    STATIC_JS.read_bytes(),
                    "text/javascript; charset=utf-8",
                    correlation_id,
                )
                return

            request_payload = None
            if method == "POST":
                problem, request_payload = self._read_json()
                if problem:
                    outcome = request_payload["error"]
                    self._send(problem, json.dumps(request_payload), "application/json", correlation_id)
                    return

            route = self.routes.get((method, path))
            if route is None:
                known_path = any(route_path == path for _, route_path in self.routes)
                code = 405 if known_path else 404
                outcome = "method_not_allowed" if known_path else "not_found"
                payload = failure(outcome, "Method not allowed." if known_path else "Not found.")
                self._send(code, json.dumps(payload), "application/json", correlation_id)
                return

            context = {
                "correlation_id": correlation_id,
                "actor": actor,
                "method": method,
                "path": path,
            }
            code, payload = route(self.config, context, request_payload)
            event_action = context.get("action", method)
            event_target = context.get("target", path)
            outcome = "ok" if payload.get("ok") else payload.get("error", "internal_error")
            self._send(code, json.dumps(payload), "application/json", correlation_id)
        except Exception:
            payload = failure("internal_error", "Internal server error.")
            outcome = payload["error"]
            self._send(500, json.dumps(payload), "application/json", correlation_id)
        finally:
            emit_event({
                "time": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "action": event_action,
                "target": event_target,
                "outcome": outcome,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "correlation_id": correlation_id,
            })

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def log_message(self, _format: str, *_args) -> None:
        pass


def fixture_check() -> None:
    cfg = load_ops_config(Path(__file__).resolve().parents[2] / "brain.config.toml")
    html_text = render_html('token"value', {
        "observed_time": "2026-08-23T00:00:00+00:00",
        "repositories": [{
            "name": "engine", "status": "healthy", "head": "abcdef123456",
            "reason": "clean", "dirty_paths": [],
        }],
        "proposals": {"status": "healthy", "reason": "0 pending", "buckets": []},
        "services": [{
            "id": "backup", "label": "Backup schedule", "status": "healthy",
            "reason": "active/waiting", "installed": True, "enabled": True,
            "active": True, "actions": ["restart"],
        }],
        "brain_mcp": {
            "error": "",
            "remote": {
                "configured": True, "resource": "https://brain.example/mcp",
                "clients": [{"name": "Claude", "profile": "claude", "created_at": "2026-08-23"}],
            },
        },
        "backup": {
            "status": "healthy", "reason": "backup and repository check succeeded within 36 hours",
            "last_success": "2026-08-23T00:00:00+00:00", "manual_restore_test": "",
            "stage": {"present": False, "verified": False, "verified_at": "", "restored_paths": []},
        },
    })
    assert success({}) == {"ok": True, "result": {}}
    assert failure("not_found", "Not found.")["error"] == "not_found"
    assert f"http://127.0.0.1:{cfg.dashboard_port}" in allowed_origins(cfg)
    assert "token&quot;value" in html_text
    assert all(word in html_text for word in (
        "Repositories", "Proposals", "Backup schedule", "Brain-mcp", "Backup and restore",
    ))
    assert set(build_content_routes(ContentCatalog(cfg, open_repository_file))) == {
        ("POST", f"/api/content/{path}")
        for path in ("tree", "page", "search", "backlinks", "catalog/status", "catalog/rebuild")
    }
    assert set(build_editing_routes(UserEditing(cfg, open_repository_file))) == {
        ("POST", f"/api/editing/{path}")
        for path in ("preview", "create", "edit", "rename", "move", "archive", "delete", "undo")
    }
    assert STATIC_CSS.is_file() and STATIC_JS.is_file()
    print("dashboard fixture check: PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    if args.once and args.fixture:
        fixture_check()
        return 0
    cfg = load_ops_config(args.config)
    host = args.host or cfg.dashboard_host
    port = args.port or cfg.dashboard_port
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("refusing non-localhost dashboard bind")
    allowed_origins(cfg)
    catalog = ContentCatalog(cfg, open_repository_file)
    editing = UserEditing(cfg, open_repository_file)
    try:
        catalog.rebuild()
    except ContentError:
        pass
    Handler.config = cfg
    Handler.csrf_token = secrets.token_urlsafe(32)
    Handler.routes = {
        **Handler.routes,
        **build_content_routes(catalog),
        **build_editing_routes(editing),
    }
    HTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
