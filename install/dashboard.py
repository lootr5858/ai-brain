#!/usr/bin/env python3
"""Install and verify Brain Dashboard."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install.brain_mcp import _systemd_exec_arg
from lib.brain_mcp_install import InstallError
from lib.config import CONFIG_ENV, config_path
from lib.ops import load_ops_config


ENGINE = Path(__file__).resolve().parents[1]
ACCESS_TOKEN_ENV = "BRAIN_DASHBOARD_ACCESS_TOKEN"


def _systemd_working_directory(path: Path) -> str:
    value = str(path.absolute())
    if any(character in value for character in "\0\n\r"):
        raise InstallError("systemd service paths cannot contain control characters")
    return value.replace("\\", "\\\\").replace(" ", "\\x20")


def install_service(args) -> int:
    selected_config = getattr(args, "config", None)
    config = load_ops_config(selected_config)
    python = ENGINE / ".venv/bin/python"
    server = ENGINE / "dashboard/server.py"
    template = ENGINE / "dashboard/brain-dashboard.service.in"
    for path in (python, server, template):
        if not path.is_file():
            raise InstallError(f"missing dashboard deployment file: {path}")
    rendered = template.read_text()
    for name, path in {
        "PYTHON": python,
        "SERVER": server,
        "USER": config.user,
    }.items():
        rendered = rendered.replace(
            f"@BRAIN_DASHBOARD_{name}@",
            _systemd_exec_arg(path),
        )
    rendered = rendered.replace(
        "@BRAIN_DASHBOARD_ENGINE@",
        _systemd_working_directory(ENGINE),
    )
    explicit = selected_config is not None or CONFIG_ENV in os.environ
    rendered = rendered.replace(
        "@BRAIN_DASHBOARD_CONFIG@",
        " --config " + _systemd_exec_arg(config_path(selected_config)) if explicit else "",
    )
    if "@BRAIN_DASHBOARD_" in rendered:
        raise InstallError("dashboard service template contains an unknown placeholder")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    destination = config_home / "systemd/user/brain-dashboard.service"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered)
    destination.chmod(0o644)
    print(f"Installed brain-dashboard.service: {destination}")
    return 0


def _request(origin: str, path: str, *, csrf: str = "", payload=None, token: str = ""):
    headers = {"Accept": "application/json" if payload is not None else "text/html"}
    if token:
        headers["Cookie"] = f"CF_Authorization={token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers.update({
            "Content-Type": "application/json",
            "Origin": origin,
            "X-CSRF-Token": csrf,
        })
    try:
        with urlopen(Request(origin + path, data=data, headers=headers), timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        raise InstallError(f"dashboard probe returned HTTP {exc.code}") from None
    except (OSError, URLError) as exc:
        raise InstallError(f"dashboard probe failed ({type(exc).__name__})") from None


def verify_origin(origin: str, *, remote: bool, token: str = "") -> list[str]:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != ("https" if remote else "http")
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (not remote and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise InstallError("dashboard verification origin is invalid")
    if remote and not token:
        raise InstallError(f"set {ACCESS_TOKEN_ENV} to the current Access application token")

    status, headers, body = _request(origin, "/", token=token)
    if status != 200 or b"<title>Brain Dashboard</title>" not in body:
        raise InstallError("dashboard shell verification failed")
    content_security_policy = next(
        (value for name, value in headers.items() if name.lower() == "content-security-policy"),
        "",
    )
    if "default-src 'none'" not in content_security_policy:
        raise InstallError("dashboard security headers are missing")
    match = re.search(br'<meta name="csrf-token" content="([^"<>]+)">', body)
    if not match:
        raise InstallError("dashboard CSRF token is missing")
    csrf = match.group(1).decode()

    probes = (
        ("content/tree", {}),
        ("content/search", {"query": "brain", "mode": "lexical"}),
        ("editing/preview", {"content": "# Dashboard verification\n"}),
        ("content/catalog/rebuild", {}),
    )
    checks = ["shell", "security_headers"]
    for endpoint, payload in probes:
        status, _headers, response = _request(
            origin,
            f"/api/{endpoint}",
            csrf=csrf,
            payload=payload,
            token=token,
        )
        try:
            result = json.loads(response)
        except json.JSONDecodeError as exc:
            raise InstallError(f"dashboard {endpoint} returned invalid JSON") from exc
        if status != 200 or result.get("ok") is not True:
            raise InstallError(f"dashboard {endpoint} verification failed")
        if (
            endpoint == "content/catalog/rebuild"
            and result.get("result", {}).get("available") is not True
        ):
            raise InstallError("dashboard catalog rebuild is unavailable")
        checks.append(endpoint)
    return checks


def verify_local(args) -> int:
    config = load_ops_config(getattr(args, "config", None))
    origin = f"http://127.0.0.1:{config.dashboard_port}"
    for check in verify_origin(origin, remote=False):
        print(f"VERIFY local {check}: PASS")
    return 0


def verify_remote(args) -> int:
    origin = load_ops_config(getattr(args, "config", None)).dashboard_public_origin.rstrip("/")
    for check in verify_origin(
        origin,
        remote=True,
        token=os.environ.get(ACCESS_TOKEN_ENV, ""),
    ):
        print(f"VERIFY remote {check}: PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install-service").set_defaults(handler=install_service)
    commands.add_parser("verify-local").set_defaults(handler=verify_local)
    commands.add_parser("verify-remote").set_defaults(handler=verify_remote)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (InstallError, OSError) as exc:
        print(f"REFUSE {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
