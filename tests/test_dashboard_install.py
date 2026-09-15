#!/usr/bin/env python3
"""Dashboard deployment tests - plain asserts, run directly."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install import dashboard
from lib.brain_mcp_install import InstallError


root = Path(tempfile.mkdtemp()).resolve()
config_home = root / "config"
with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
    assert dashboard.install_service(None) == 0
unit = (config_home / "systemd/user/brain-dashboard.service").read_text()
assert str(dashboard.ENGINE / ".venv/bin/python") in unit
assert str(dashboard.ENGINE / "dashboard/server.py") in unit
assert "CacheDirectory=brain-engine/dashboard" in unit
assert "StateDirectory=brain-engine/brain-mcp" in unit
assert "ProtectSystem=strict" in unit
assert "@BRAIN_DASHBOARD_" not in unit
assert dashboard._systemd_working_directory(Path("/tmp/brain dashboard")) == (
    "/tmp/brain\\x20dashboard"
)

selected_config = root / "selected config.toml"
selected_config.write_text("[paths]\n")
with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
    args = dashboard.build_parser().parse_args(
        ["--config", str(selected_config), "install-service"]
    )
    assert args.handler(args) == 0
unit = (config_home / "systemd/user/brain-dashboard.service").read_text()
assert f'--config "{selected_config}"' in unit


class Response:
    status = 200
    headers = {"content-security-policy": "default-src 'none'"}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return self.body


requests = []


def fake_urlopen(request, timeout):
    requests.append((request, timeout))
    if request.full_url.endswith("/"):
        return Response(
            b'<title>Brain Dashboard</title><meta name="csrf-token" content="csrf-token">'
        )
    return Response(json.dumps({"ok": True, "result": {"available": True}}).encode())


for origin, remote, token in (
    ("http://127.0.0.1:8765", False, ""),
    ("https://brain.example", True, "test-access-token"),
):
    requests.clear()
    with mock.patch.object(dashboard, "urlopen", fake_urlopen):
        checks = dashboard.verify_origin(origin, remote=remote, token=token)
    assert checks == [
        "shell",
        "security_headers",
        "content/tree",
        "content/search",
        "editing/preview",
        "content/catalog/rebuild",
    ]
    assert len(requests) == 5
    assert all(request.headers.get("X-csrf-token") == "csrf-token" for request, _ in requests[1:])
    assert all(request.headers.get("Origin") == origin for request, _ in requests[1:])
    assert all(request.headers.get("Cookie", "") == (f"CF_Authorization={token}" if remote else "")
               for request, _ in requests)
    assert requests[-1][0].full_url == origin + "/api/content/catalog/rebuild"
    assert json.loads(requests[-1][0].data) == {}
    assert all("brain-mcp" not in request.full_url for request, _ in requests)

def unavailable_catalog(request, timeout):
    if request.full_url.endswith("/api/content/catalog/rebuild"):
        return Response(json.dumps({"ok": True, "result": {"available": False}}).encode())
    return fake_urlopen(request, timeout)


with mock.patch.object(dashboard, "urlopen", unavailable_catalog):
    try:
        dashboard.verify_origin("http://127.0.0.1:8765", remote=False)
    except InstallError as exc:
        assert str(exc) == "dashboard catalog rebuild is unavailable"
    else:
        raise AssertionError("unavailable catalog rebuild must fail verification")

assert dashboard.build_parser().parse_args(["verify-local"]).handler is dashboard.verify_local
assert dashboard.build_parser().parse_args(["verify-remote"]).handler is dashboard.verify_remote
try:
    dashboard.verify_origin("https://brain.example", remote=True)
except InstallError as exc:
    assert dashboard.ACCESS_TOKEN_ENV in str(exc)
else:
    raise AssertionError("remote verification accepted no Access token")

with contextlib.redirect_stderr(io.StringIO()):
    try:
        dashboard.build_parser().parse_args([])
    except SystemExit:
        pass
    else:
        raise AssertionError("dashboard installer accepted no command")

print("dashboard deployment tests: PASS")
