#!/usr/bin/env python3
"""Remote brain-mcp storage, OAuth, transport, and CLI tests."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import configparser
import contextlib
import hashlib
import io
import json
import logging
import anyio
import httpx2
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock
from urllib.parse import parse_qs, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install.brain_mcp as brain_mcp_installer
from lib.brain_mcp_remote_store import RemoteStore, StoreError, remote_state_path
from lib.brain_mcp_remote import (
    CLAUDE_CALLBACK,
    RemoteAccessError,
    client_redirect_uri,
    create_remote_app,
    normalize_public_url,
    remote_oauth_routes,
    redirect_uri_matches,
)
from lib.brain_mcp import BrainMcpConfig
from starlette.applications import Starlette


root = Path(tempfile.mkdtemp(prefix="brain-mcp-remote-"))
home = root / "home"
xdg = root / "state"

assert remote_state_path(env={}, home=home) == (
    home / ".local/state/brain-engine/brain-mcp/remote.sqlite3"
)
assert remote_state_path(env={"XDG_STATE_HOME": str(xdg)}, home=home) == (
    xdg / "brain-engine/brain-mcp/remote.sqlite3"
)

database = root / "runtime" / "remote.sqlite3"
store = RemoteStore(database)
assert store.path == database
assert database.parent.stat().st_mode & 0o777 == 0o700
assert database.stat().st_mode & 0o777 == 0o600

with sqlite3.connect(database) as connection:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
assert tables == {
    "access_tokens",
    "authorization_codes",
    "clients",
    "refresh_tokens",
    "settings",
}

unknown = root / "unknown.sqlite3"
with sqlite3.connect(unknown) as connection:
    connection.execute("PRAGMA user_version=99")
try:
    RemoteStore(unknown)
except StoreError as exc:
    assert "schema version 99" in str(exc)
else:
    raise AssertionError("unknown schema version accepted")

print("remote storage schema tests: PASS")


def refuse_remote(function, message: str) -> None:
    try:
        function()
    except RemoteAccessError as exc:
        assert message in str(exc), exc
    else:
        raise AssertionError(f"expected RemoteAccessError containing {message!r}")


assert normalize_public_url(" HTTPS://Example.COM:443/mcp/ ") == (
    "https://example.com/mcp"
)
assert normalize_public_url("https://example.com/") == "https://example.com/"
for invalid in (
    "http://example.com/mcp",
    "https://user@example.com/mcp",
    "https://example.com/mcp?",
    "https://example.com/mcp?query=yes",
    "https://example.com/mcp#",
    "https://example.com/mcp#fragment",
    "https:///mcp",
    "https://example.com/authorize",
    "https://example.com/%61uthorize",
    "https://example.com/token",
    "https://example.com/.well-known/oauth-authorization-server",
):
    refuse_remote(lambda value=invalid: normalize_public_url(value), "public HTTPS")

chatgpt_callback = "https://chatgpt.example/callback?registration=1"
assert client_redirect_uri("chatgpt", chatgpt_callback) == chatgpt_callback
assert redirect_uri_matches("chatgpt", chatgpt_callback, chatgpt_callback)
assert not redirect_uri_matches(
    "chatgpt",
    "https://chatgpt.example/other",
    chatgpt_callback,
)
refuse_remote(lambda: client_redirect_uri("chatgpt", None), "callback")
refuse_remote(
    lambda: client_redirect_uri("chatgpt", "http://chatgpt.example/callback"),
    "HTTPS",
)

assert client_redirect_uri("claude", None) == CLAUDE_CALLBACK
assert client_redirect_uri("claude", CLAUDE_CALLBACK) == CLAUDE_CALLBACK
assert redirect_uri_matches("claude", CLAUDE_CALLBACK, CLAUDE_CALLBACK)
refuse_remote(
    lambda: client_redirect_uri("claude", "https://claude.example/callback"),
    "fixed callback",
)

assert client_redirect_uri("hermes", None) is None
for callback in (
    "http://127.0.0.1:1024/callback",
    "http://127.0.0.1:65535/callback",
    "http://localhost:5555/callback",
):
    assert redirect_uri_matches("hermes", callback, None), callback
for callback in (
    "https://localhost:5555/callback",
    "http://127.0.0.1:1023/callback",
    "http://127.0.0.1:65536/callback",
    "http://127.0.0.1:5555/other",
    "http://127.0.0.1:5555/callback?",
    "http://127.0.0.1:5555/callback?query=yes",
    "http://127.0.0.1:5555/callback#",
    "http://user@127.0.0.1:5555/callback",
    "http://[::1]:5555/callback",
):
    assert not redirect_uri_matches("hermes", callback, None), callback

refuse_remote(lambda: client_redirect_uri("other", None), "profile")
print("remote URL and profile tests: PASS")


client_database = root / "clients" / "remote.sqlite3"
client_store = RemoteStore(client_database)
resource = "https://brain.example/mcp"
assert client_store.get_canonical_resource() is None
assert client_store.set_canonical_resource(resource, now=100) == resource
assert client_store.set_canonical_resource(resource, now=101) == resource
assert client_store.get_canonical_resource() == resource

created = client_store.create_client(
    unique_name="My ChatGPT",
    profile="chatgpt",
    redirect_uri=chatgpt_callback,
    now=102,
)
assert created.client.unique_name == "My ChatGPT"
assert created.client.profile == "chatgpt"
assert created.client.redirect_uri == chatgpt_callback
assert created.client.created_at == 102
assert len(created.client.client_id) >= 24
assert len(created.client_secret) >= 32
assert client_store.verify_client_secret(
    created.client.client_id,
    created.client_secret,
)
assert not client_store.verify_client_secret(created.client.client_id, "wrong")
assert not client_store.verify_client_secret("unknown", created.client_secret)
assert client_store.list_clients() == [created.client]

database_bytes = client_database.read_bytes()
assert created.client_secret.encode() not in database_bytes

try:
    client_store.create_client(
        unique_name="my chatgpt",
        profile="chatgpt",
        redirect_uri=chatgpt_callback,
        now=103,
    )
except StoreError as exc:
    assert "unique" in str(exc)
else:
    raise AssertionError("case-insensitive duplicate client name accepted")

try:
    client_store.set_canonical_resource("https://other.example/mcp", now=104)
except StoreError as exc:
    assert "remove" in str(exc)
else:
    raise AssertionError("canonical resource changed while a client exists")

assert client_store.remove_client(created.client.client_id) == "My ChatGPT"
assert client_store.list_clients() == []
assert not client_store.verify_client_secret(
    created.client.client_id,
    created.client_secret,
)
assert client_store.set_canonical_resource(
    "https://other.example/mcp",
    now=105,
) == "https://other.example/mcp"
print("remote client storage tests: PASS")


credential_database = root / "credentials" / "remote.sqlite3"
credential_store = RemoteStore(credential_database)
credential_store.set_canonical_resource(resource, now=200)
credential_client = credential_store.create_client(
    unique_name="Credential client",
    profile="claude",
    redirect_uri=CLAUDE_CALLBACK,
    now=201,
).client
challenge = "A" * 43
code = credential_store.create_authorization_code(
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    scope="offline_access",
    now=202,
)
assert code.encode() not in credential_database.read_bytes()
assert credential_store.consume_authorization_code(
    code,
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    now=203,
).scope == "offline_access"
assert credential_store.consume_authorization_code(
    code,
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    now=204,
) is None

expired_code = credential_store.create_authorization_code(
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    scope="",
    now=300,
)
assert credential_store.consume_authorization_code(
    expired_code,
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    now=601,
) is None

atomic_code = credential_store.create_authorization_code(
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    scope="offline_access",
    now=610,
)
with mock.patch(
    "lib.brain_mcp_remote_store.secrets.token_urlsafe",
    side_effect=RuntimeError("injected token generation failure"),
):
    try:
        credential_store.exchange_authorization_code(
            atomic_code,
            client_id=credential_client.client_id,
            redirect_uri=CLAUDE_CALLBACK,
            code_challenge=challenge,
            resource=resource,
            now=611,
        )
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("injected token-generation failure did not fail")
atomic_tokens = credential_store.exchange_authorization_code(
    atomic_code,
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    now=612,
)
assert atomic_tokens is not None
assert atomic_tokens.scope == "offline_access"
assert credential_store.exchange_authorization_code(
    atomic_code,
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    now=613,
) is None

tokens = credential_store.issue_tokens(
    client_id=credential_client.client_id,
    resource=resource,
    scope="offline_access",
    now=700,
)
database_bytes = credential_database.read_bytes()
assert tokens.access_token.encode() not in database_bytes
assert tokens.refresh_token.encode() not in database_bytes
access = credential_store.lookup_access_token(
    tokens.access_token,
    resource=resource,
    now=701,
)
assert access is not None
assert access.client_id == credential_client.client_id
assert access.scope == "offline_access"
assert credential_store.lookup_access_token(
    tokens.access_token,
    resource="https://wrong.example/mcp",
    now=701,
) is None

rotated = credential_store.rotate_refresh_token(
    tokens.refresh_token,
    client_id=credential_client.client_id,
    resource=resource,
    now=702,
)
assert rotated is not None
assert rotated.access_token != tokens.access_token
assert rotated.refresh_token != tokens.refresh_token
assert credential_store.rotate_refresh_token(
    tokens.refresh_token,
    client_id=credential_client.client_id,
    resource=resource,
    now=703,
) is None

expired_tokens = credential_store.issue_tokens(
    client_id=credential_client.client_id,
    resource=resource,
    scope="offline_access",
    now=800,
)
assert credential_store.lookup_access_token(
    expired_tokens.access_token,
    resource=resource,
    now=4401,
) is None
assert credential_store.rotate_refresh_token(
    expired_tokens.refresh_token,
    client_id=credential_client.client_id,
    resource=resource,
    now=800 + 30 * 24 * 60 * 60 + 1,
) is None

race_code = credential_store.create_authorization_code(
    client_id=credential_client.client_id,
    redirect_uri=CLAUDE_CALLBACK,
    code_challenge=challenge,
    resource=resource,
    scope="offline_access",
    now=900,
)
with ThreadPoolExecutor(max_workers=2) as executor:
    code_race_results = list(
        executor.map(
            lambda _index: credential_store.exchange_authorization_code(
                race_code,
                client_id=credential_client.client_id,
                redirect_uri=CLAUDE_CALLBACK,
                code_challenge=challenge,
                resource=resource,
                now=901,
            ),
            range(2),
        )
    )
assert sum(result is not None for result in code_race_results) == 1

race_tokens = credential_store.issue_tokens(
    client_id=credential_client.client_id,
    resource=resource,
    scope="offline_access",
    now=1000,
)
with ThreadPoolExecutor(max_workers=2) as executor:
    refresh_race_results = list(
        executor.map(
            lambda _index: credential_store.rotate_refresh_token(
                race_tokens.refresh_token,
                client_id=credential_client.client_id,
                resource=resource,
                now=1001,
            ),
            range(2),
        )
    )
assert sum(result is not None for result in refresh_race_results) == 1
assert credential_store.lookup_access_token(
    rotated.access_token,
    resource=resource,
    now=703,
) is not None

assert credential_store.remove_client(credential_client.client_id) == (
    "Credential client"
)
assert credential_store.lookup_access_token(
    tokens.access_token,
    resource=resource,
    now=704,
) is None
assert credential_store.lookup_access_token(
    rotated.access_token,
    resource=resource,
    now=704,
) is None
assert credential_store.rotate_refresh_token(
    rotated.refresh_token,
    client_id=credential_client.client_id,
    resource=resource,
    now=704,
) is None

with sqlite3.connect(credential_database) as connection:
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("authorization_codes", "access_tokens", "refresh_tokens")
    }
assert counts == {"authorization_codes": 0, "access_tokens": 0, "refresh_tokens": 0}
print("remote credential lifecycle tests: PASS")


engine = Path(__file__).resolve().parents[1]
cli_state = root / "cli-state"


def run_cli(arguments: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["XDG_STATE_HOME"] = str(cli_state)
    return subprocess.run(
        [sys.executable, str(engine / "install/brain_mcp.py"), *arguments],
        cwd=engine,
        env=environment,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=10,
    )


uninitialized = run_cli(["remote", "add-client"])
assert uninitialized.returncode == 1
assert "REFUSE run remote init" in uninitialized.stdout
assert "Traceback" not in uninitialized.stderr


class AsgiClient:
    """Synchronous facade over installed httpx2; Starlette TestClient deadlocks."""

    def __init__(self, app, *, base_url: str) -> None:
        self.app = app
        self.base_url = base_url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def request(self, method: str, url: str, **kwargs):
        async def send():
            transport = httpx2.ASGITransport(app=self.app)
            async with httpx2.AsyncClient(
                transport=transport,
                base_url=self.base_url,
            ) as client:
                return await client.request(method, url, **kwargs)

        return anyio.run(send)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


initialized = run_cli(["remote", "init"], f"{resource}\n")
assert initialized.returncode == 0, initialized.stdout + initialized.stderr
assert resource in initialized.stdout
same = run_cli(["remote", "init"], f"{resource}/\n")
assert same.returncode == 0, same.stdout + same.stderr

added = run_cli(
    ["remote", "add-client"],
    f"1\nPrimary ChatGPT\n{chatgpt_callback}\n",
)
assert added.returncode == 0, added.stdout + added.stderr
assert f"Brain-MCP URL: {resource}" in added.stdout
assert (
    added.stdout.index("Unique name:")
    < added.stdout.index(f"Brain-MCP URL: {resource}")
    < added.stdout.index("Exact ChatGPT HTTPS callback:")
    < added.stdout.index("OAuth client ID:")
    < added.stdout.index("OAuth client secret:")
), added.stdout
client_id_match = re.search(r"OAuth client ID: (\S+)", added.stdout)
secret_match = re.search(r"OAuth client secret: (\S+)", added.stdout)
assert client_id_match and secret_match, added.stdout
cli_client_id = client_id_match.group(1)
cli_secret = secret_match.group(1)
assert added.stdout.count(cli_secret) == 1
assert "cannot be displayed again" in added.stdout

listed = run_cli(["remote", "list-clients"])
assert listed.returncode == 0, listed.stdout + listed.stderr
assert "Primary ChatGPT" in listed.stdout
assert "chatgpt" in listed.stdout
assert cli_secret not in listed.stdout
assert cli_client_id not in listed.stdout

duplicate = run_cli(["remote", "add-client"], "2\nprimary chatgpt\n")
assert duplicate.returncode == 1
assert "REFUSE" in duplicate.stdout and "unique" in duplicate.stdout

changed = run_cli(
    ["remote", "init"],
    "https://changed.example/mcp\n",
)
assert changed.returncode == 1
assert "REFUSE" in changed.stdout and "remove" in changed.stdout

removed = run_cli(["remote", "remove-client"], "1\n")
assert removed.returncode == 0, removed.stdout + removed.stderr
assert "Primary ChatGPT" in removed.stdout
changed = run_cli(
    ["remote", "init"],
    "https://changed.example/mcp\n",
)
assert changed.returncode == 0, changed.stdout + changed.stderr

assert run_cli(
    ["remote", "add-client"],
    "2\nClaude One\n",
).returncode == 0
assert run_cli(
    ["remote", "add-client"],
    "3\nHermes One\n",
).returncode == 0
cancelled = run_cli(["remote", "remove-client"], "all\nno\n")
assert cancelled.returncode == 0
assert "Cancelled" in cancelled.stdout
assert "Claude One" in run_cli(["remote", "list-clients"]).stdout
removed_all = run_cli(["remote", "remove-client"], "all\nREMOVE ALL\n")
assert removed_all.returncode == 0, removed_all.stdout + removed_all.stderr
assert "Claude One" in removed_all.stdout and "Hermes One" in removed_all.stdout
assert "No remote clients" in run_cli(["remote", "list-clients"]).stdout
print("remote management CLI tests: PASS")


oauth_database = root / "oauth" / "remote.sqlite3"
oauth_store = RemoteStore(oauth_database)
oauth_store.set_canonical_resource(resource)
oauth_created = oauth_store.create_client(
    unique_name="OAuth ChatGPT",
    profile="chatgpt",
    redirect_uri=chatgpt_callback,
)
oauth_app = Starlette(routes=remote_oauth_routes(oauth_store))

with AsgiClient(oauth_app, base_url="https://brain.example") as client:
    metadata_response = client.get("/.well-known/oauth-authorization-server")
    assert metadata_response.status_code == 200, metadata_response.text
    metadata = metadata_response.json()
    assert metadata["issuer"] == "https://brain.example", metadata
    assert metadata["authorization_endpoint"] == "https://brain.example/authorize"
    assert metadata["token_endpoint"] == "https://brain.example/token"
    assert metadata["grant_types_supported"] == [
        "authorization_code",
        "refresh_token",
    ]
    assert metadata["token_endpoint_auth_methods_supported"] == [
        "client_secret_post",
        "client_secret_basic",
    ]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["scopes_supported"] == ["offline_access"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert "registration_endpoint" not in metadata
    assert "revocation_endpoint" not in metadata
    assert client.post("/register").status_code == 404
    assert client.post("/revoke").status_code == 404

    verifier = "v" * 43
    pkce_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    authorize_params = {
        "response_type": "code",
        "client_id": oauth_created.client.client_id,
        "redirect_uri": chatgpt_callback,
        "code_challenge": pkce_challenge,
        "code_challenge_method": "S256",
        "state": "state-value",
        "scope": "offline_access",
        "resource": resource,
    }
    missing_method = dict(authorize_params)
    missing_method.pop("code_challenge_method")
    missing_method_response = client.get(
        "/authorize",
        params=missing_method,
        follow_redirects=False,
    )
    assert missing_method_response.status_code == 302
    missing_method_error = parse_qs(
        urlsplit(missing_method_response.headers["location"]).query
    )
    assert missing_method_error["error"] == ["invalid_request"]
    assert missing_method_error["state"] == ["state-value"]
    assert missing_method_error["iss"] == ["https://brain.example"]
    wrong_resource = dict(authorize_params, resource="https://wrong.example/mcp")
    wrong_resource_response = client.get(
        "/authorize",
        params=wrong_resource,
        follow_redirects=False,
    )
    assert wrong_resource_response.status_code == 302
    wrong_resource_error = parse_qs(
        urlsplit(wrong_resource_response.headers["location"]).query
    )
    assert wrong_resource_error["error"] == ["invalid_target"]
    assert wrong_resource_error["state"] == ["state-value"]
    wrong_scope = dict(authorize_params, scope="brain.read")
    wrong_scope_response = client.get(
        "/authorize",
        params=wrong_scope,
        follow_redirects=False,
    )
    assert wrong_scope_response.status_code == 302
    assert parse_qs(
        urlsplit(wrong_scope_response.headers["location"]).query
    )["state"] == ["state-value"]
    invalid_challenge = dict(authorize_params, code_challenge="." * 43)
    invalid_challenge_response = client.get(
        "/authorize",
        params=invalid_challenge,
        follow_redirects=False,
    )
    assert invalid_challenge_response.status_code == 302
    assert parse_qs(
        urlsplit(invalid_challenge_response.headers["location"]).query
    )["error"] == ["invalid_request"]
    wrong_redirect = dict(
        authorize_params,
        redirect_uri="https://chatgpt.example/other",
    )
    assert client.get("/authorize", params=wrong_redirect).status_code == 400
    unknown_client = dict(authorize_params, client_id="unknown-client")
    unknown_client_response = client.get("/authorize", params=unknown_client)
    assert unknown_client_response.status_code == 400
    assert unknown_client_response.json()["error"] == "unauthorized_client"

    authorized = client.get(
        "/authorize",
        params=authorize_params,
        follow_redirects=False,
    )
    assert authorized.status_code == 302, authorized.text
    redirect = urlsplit(authorized.headers["location"])
    assert redirect.scheme + "://" + redirect.netloc + redirect.path == (
        chatgpt_callback.split("?", 1)[0]
    )
    redirect_query = parse_qs(redirect.query)
    assert redirect_query["registration"] == ["1"]
    assert redirect_query["state"] == ["state-value"]
    assert redirect_query["iss"] == ["https://brain.example"]
    authorization_code = redirect_query["code"][0]

    basic = base64.b64encode(
        f"{oauth_created.client.client_id}:{oauth_created.client_secret}".encode()
    ).decode()
    wrong_redirect_exchange = client.post(
        "/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": "https://chatgpt.example/other",
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert wrong_redirect_exchange.status_code == 400
    assert wrong_redirect_exchange.json()["error"] == "invalid_grant"
    wrong_verifier_exchange = client.post(
        "/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": chatgpt_callback,
            "code_verifier": "w" * 43,
            "resource": resource,
        },
    )
    assert wrong_verifier_exchange.status_code == 400
    assert wrong_verifier_exchange.json()["error"] == "invalid_grant"
    wrong_basic = base64.b64encode(
        f"{oauth_created.client.client_id}:wrong".encode()
    ).decode()
    wrong_basic_exchange = client.post(
        "/token",
        headers={"Authorization": f"Basic {wrong_basic}"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": chatgpt_callback,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert wrong_basic_exchange.status_code == 401
    assert wrong_basic_exchange.json()["error"] == "invalid_client"
    unsupported_grant = client.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": oauth_created.client.client_id,
            "client_secret": oauth_created.client_secret,
            "resource": resource,
        },
    )
    assert unsupported_grant.status_code == 400
    assert unsupported_grant.json()["error"] == "unsupported_grant_type"
    exchanged = client.post(
        "/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": chatgpt_callback,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    first_tokens = exchanged.json()
    assert first_tokens["token_type"] == "Bearer"
    assert first_tokens["expires_in"] == 3600
    assert first_tokens["scope"] == "offline_access"
    assert first_tokens["access_token"]
    assert first_tokens["refresh_token"]

    reused = client.post(
        "/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": chatgpt_callback,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first_tokens["refresh_token"],
            "client_id": oauth_created.client.client_id,
            "client_secret": oauth_created.client_secret,
            "resource": resource,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    second_tokens = refreshed.json()
    assert second_tokens["access_token"] != first_tokens["access_token"]
    assert second_tokens["refresh_token"] != first_tokens["refresh_token"]

    invalid_refresh_scope = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": second_tokens["refresh_token"],
            "client_id": oauth_created.client.client_id,
            "client_secret": oauth_created.client_secret,
            "resource": resource,
            "scope": " ",
        },
    )
    assert invalid_refresh_scope.status_code == 400
    assert invalid_refresh_scope.json()["error"] == "invalid_scope"

    refresh_wrong_resource = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": second_tokens["refresh_token"],
            "client_id": oauth_created.client.client_id,
            "client_secret": oauth_created.client_secret,
            "resource": "https://wrong.example/mcp",
        },
    )
    assert refresh_wrong_resource.status_code == 400
    assert refresh_wrong_resource.json()["error"] == "invalid_target"

    rotated_reuse = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first_tokens["refresh_token"],
            "client_id": oauth_created.client.client_id,
            "client_secret": oauth_created.client_secret,
            "resource": resource,
        },
    )
    assert rotated_reuse.status_code == 400
    assert rotated_reuse.json()["error"] == "invalid_grant"

    wrong_secret = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": second_tokens["refresh_token"],
            "client_id": oauth_created.client.client_id,
            "client_secret": "wrong",
            "resource": resource,
        },
    )
    assert wrong_secret.status_code == 401
    assert wrong_secret.json()["error"] == "invalid_client"

    json_request = client.post(
        "/token",
        json={"grant_type": "refresh_token"},
    )
    assert json_request.status_code == 415

    captured_logs = io.StringIO()
    log_handler = logging.StreamHandler(captured_logs)
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    sensitive_state = "sensitive-state-marker"
    sensitive_verifier = "s" * 43
    sensitive_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(sensitive_verifier.encode()).digest()
    ).decode().rstrip("=")
    try:
        client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": second_tokens["refresh_token"],
                "client_id": oauth_created.client.client_id,
                "client_secret": oauth_created.client_secret,
                "resource": resource,
            },
        )
        sensitive_authorization = client.get(
            "/authorize",
            params={
                **authorize_params,
                "state": sensitive_state,
                "code_challenge": sensitive_challenge,
            },
            follow_redirects=False,
        )
        sensitive_code = parse_qs(
            urlsplit(sensitive_authorization.headers["location"]).query
        )["code"][0]
        sensitive_bearer = f"Bearer {second_tokens['access_token']}"
        client.get("/missing", headers={"Authorization": sensitive_bearer})
    finally:
        root_logger.removeHandler(log_handler)
    logged = captured_logs.getvalue()
    for sensitive_value in (
        oauth_created.client_secret,
        authorization_code,
        sensitive_code,
        first_tokens["access_token"],
        second_tokens["access_token"],
        first_tokens["refresh_token"],
        second_tokens["refresh_token"],
        sensitive_bearer,
        sensitive_state,
        sensitive_challenge,
    ):
        assert sensitive_value not in logged

print("remote OAuth route tests: PASS")


exact_callback = "https://EXAMPLE.com/callback"
exact_store = RemoteStore(root / "exact-callback" / "remote.sqlite3")
exact_store.set_canonical_resource(resource)
exact_created = exact_store.create_client(
    unique_name="Exact callback",
    profile="chatgpt",
    redirect_uri=exact_callback,
)
exact_app = Starlette(routes=remote_oauth_routes(exact_store))
with AsgiClient(exact_app, base_url="https://brain.example") as client:
    verifier = "e" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": exact_created.client.client_id,
            "redirect_uri": exact_callback,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": exact_callback,
            "code_verifier": verifier,
            "resource": resource,
            "client_id": exact_created.client.client_id,
            "client_secret": exact_created.client_secret,
        },
    )
    assert response.status_code == 200, response.text
print("remote exact callback tests: PASS")


hermes_store = RemoteStore(root / "hermes-oauth" / "remote.sqlite3")
hermes_store.set_canonical_resource(resource)
hermes_created = hermes_store.create_client(
    unique_name="Hermes OAuth",
    profile="hermes",
    redirect_uri=None,
)
hermes_app = Starlette(routes=remote_oauth_routes(hermes_store))
with AsgiClient(hermes_app, base_url="https://brain.example") as client:
    verifier = "h" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    callback = "http://localhost:49152/callback"
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": hermes_created.client.client_id,
            "redirect_uri": callback,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
            "state": "hermes-state",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    redirect = urlsplit(response.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == callback
    redirect_values = parse_qs(redirect.query)
    assert redirect_values["state"] == ["hermes-state"]
    code = redirect_values["code"][0]
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback,
            "code_verifier": verifier,
            "resource": resource,
            "client_id": hermes_created.client.client_id,
            "client_secret": hermes_created.client_secret,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"]
print("remote Hermes OAuth tests: PASS")


transport_root = root / "transport-brain"
for repository in ("engine", "agents", "user"):
    (transport_root / repository).mkdir(parents=True)
transport_config = BrainMcpConfig(
    brain=transport_root,
    engine=transport_root / "engine",
    agents=transport_root / "agents",
    user=transport_root / "user",
)
transport_config_path = root / "transport-brain.config.toml"
transport_config_path.write_text(
    "[paths]\n"
    f'brain = "{transport_root}"\n'
    f'engine = "{transport_root / "engine"}"\n'
    f'agents = "{transport_root / "agents"}"\n'
    f'user = "{transport_root / "user"}"\n'
)
remote_app = create_remote_app(transport_config, oauth_store)
wrong_resource_tokens = oauth_store.issue_tokens(
    client_id=oauth_created.client.client_id,
    resource="https://wrong.example/mcp",
    scope="offline_access",
)
expired_http_tokens = oauth_store.issue_tokens(
    client_id=oauth_created.client.client_id,
    resource=resource,
    scope="offline_access",
    now=0,
)


async def remote_transport_checks() -> None:
    async with remote_app.router.lifespan_context(remote_app):
        transport = httpx2.ASGITransport(app=remote_app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="https://brain.example",
        ) as client:
            protected = await client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
            assert protected.status_code == 200, protected.text
            protected_metadata = protected.json()
            assert protected_metadata["resource"] == resource
            assert protected_metadata["authorization_servers"] == [
                "https://brain.example"
            ]
            assert protected_metadata["bearer_methods_supported"] == ["header"]
            assert "scopes_supported" not in protected_metadata

            unauthenticated = await client.post(
                "/mcp",
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
            )
            assert unauthenticated.status_code == 401
            challenge_header = unauthenticated.headers["www-authenticate"]
            assert "invalid_token" in challenge_header
            assert (
                'resource_metadata="https://brain.example/'
                '.well-known/oauth-protected-resource/mcp"'
            ) in challenge_header

            for rejected_authorization in (
                "Bearer unknown-token",
                "Basic malformed",
                f"Bearer {wrong_resource_tokens.access_token}",
                f"Bearer {expired_http_tokens.access_token}",
            ):
                rejected = await client.post(
                    "/mcp",
                    headers={
                        "Authorization": rejected_authorization,
                        "Content-Type": "application/json",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 34,
                        "method": "server/discover",
                    },
                )
                assert rejected.status_code == 401, rejected.text
                assert "resource_metadata=" in rejected.headers["www-authenticate"]

            bearer_headers = {
                "Authorization": f"Bearer {second_tokens['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2026-07-28",
            }
            invalid_origin = await client.post(
                "/mcp",
                headers={**bearer_headers, "Origin": "https://invalid.example"},
                json={"jsonrpc": "2.0", "id": 2, "method": "server/discover"},
            )
            assert invalid_origin.status_code == 403

            invalid_host = await client.post(
                "https://invalid.example/mcp",
                headers=bearer_headers,
                json={"jsonrpc": "2.0", "id": 3, "method": "server/discover"},
            )
            assert 400 <= invalid_host.status_code < 500

            explicit_default_port = await client.post(
                "/mcp",
                headers={
                    **bearer_headers,
                    "Host": "brain.example:443",
                    "Mcp-Method": "server/discover",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 33,
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {
                                "name": "default-port-test",
                                "version": "1",
                            },
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                },
            )
            assert explicit_default_port.status_code == 200, explicit_default_port.text

            query_bearer = await client.post(
                f"/mcp?access_token={second_tokens['access_token']}",
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 30, "method": "server/discover"},
            )
            assert query_bearer.status_code == 401

            modern_meta = {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "remote-test",
                    "version": "1",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            discovered = await client.post(
                "/mcp",
                headers={**bearer_headers, "Mcp-Method": "server/discover"},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "server/discover",
                    "params": {"_meta": modern_meta},
                },
            )
            assert discovered.status_code == 200, discovered.text
            discovery_result = discovered.json()["result"]
            assert discovery_result["supportedVersions"] == ["2026-07-28"]
            assert discovery_result["resultType"] == "complete"
            assert discovery_result["ttlMs"] == 0
            assert discovery_result["cacheScope"] == "private"

            listed = await client.post(
                "/mcp",
                headers={**bearer_headers, "Mcp-Method": "tools/list"},
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/list",
                    "params": {"_meta": modern_meta},
                },
            )
            assert listed.status_code == 200, listed.text
            list_result = listed.json()["result"]
            assert list_result["resultType"] == "complete"
            assert list_result["ttlMs"] == 0
            assert list_result["cacheScope"] == "private"
            names = {tool["name"] for tool in list_result["tools"]}
            assert "health_check" in names

            health = await client.post(
                "/mcp",
                headers={
                    **bearer_headers,
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "health_check",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "health_check",
                        "arguments": {},
                        "_meta": modern_meta,
                    },
                },
            )
            assert health.status_code == 200, health.text
            assert health.json()["result"]["content"][0]["text"] == (
                "pong from brain-mcp"
            )

            context_read = await client.post(
                "/mcp",
                headers={
                    **bearer_headers,
                    "Origin": "https://brain.example",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "list_context",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "tools/call",
                    "params": {
                        "name": "list_context",
                        "arguments": {},
                        "_meta": modern_meta,
                    },
                },
            )
            assert context_read.status_code == 200, context_read.text
            assert "# brain-mcp index" in (
                context_read.json()["result"]["content"][0]["text"]
            )

            local_requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 40,
                    "method": "server/discover",
                    "params": {"_meta": modern_meta},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 41,
                    "method": "tools/list",
                    "params": {"_meta": modern_meta},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/call",
                    "params": {
                        "name": "health_check",
                        "arguments": {},
                        "_meta": modern_meta,
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 43,
                    "method": "tools/call",
                    "params": {
                        "name": "list_context",
                        "arguments": {},
                        "_meta": modern_meta,
                    },
                },
            ]
            local = subprocess.run(
                [
                    sys.executable,
                    str(engine / "servers/brain-mcp/stdio.py"),
                    "--config",
                    str(transport_config_path),
                ],
                input="".join(
                    json.dumps(request, separators=(",", ":")) + "\n"
                    for request in local_requests
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert local.returncode == 0, local.stderr
            local_results = {
                row["id"]: row["result"]
                for row in map(json.loads, local.stdout.splitlines())
            }
            assert local_results[40] == discovery_result
            assert local_results[41]["tools"] == list_result["tools"]
            assert local_results[42]["content"] == health.json()["result"]["content"]
            assert local_results[43]["content"] == (
                context_read.json()["result"]["content"]
            )

            legacy_headers = {
                "Authorization": f"Bearer {second_tokens['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2025-11-25",
            }
            initialized = await client.post(
                "/mcp",
                headers=legacy_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy-remote", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text
            assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"

            legacy_listed = await client.post(
                "/mcp",
                headers=legacy_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert legacy_listed.status_code == 200, legacy_listed.text
            assert "health_check" in {
                tool["name"]
                for tool in legacy_listed.json()["result"]["tools"]
            }

            legacy_health = await client.post(
                "/mcp",
                headers=legacy_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {"name": "health_check", "arguments": {}},
                },
            )
            assert legacy_health.status_code == 200, legacy_health.text
            assert legacy_health.json()["result"]["content"][0]["text"] == (
                "pong from brain-mcp"
            )


anyio.run(remote_transport_checks)
print("remote Streamable HTTP tests: PASS")


http_state_home = root / "http-state"
http_database = remote_state_path(
    env={"XDG_STATE_HOME": str(http_state_home)},
    home=home,
)
http_store = RemoteStore(http_database)
http_store.set_canonical_resource(resource)
http_config = root / "http-brain.config.toml"
http_config.write_text(
    "[paths]\n"
    f'brain = "{transport_root}"\n'
    f'engine = "{transport_root / "engine"}"\n'
    f'agents = "{transport_root / "agents"}"\n'
    f'user = "{transport_root / "user"}"\n'
)
http_entrypoint = engine / "servers/brain-mcp/http.py"
http_help = subprocess.run(
    [sys.executable, str(http_entrypoint), "--help"],
    cwd=engine,
    capture_output=True,
    text=True,
    timeout=10,
)
assert http_help.returncode == 0, http_help.stdout + http_help.stderr
assert "--port" in http_help.stdout and "--config" in http_help.stdout
http_spec = importlib.util.spec_from_file_location("brain_mcp_http", http_entrypoint)
assert http_spec and http_spec.loader
http_module = importlib.util.module_from_spec(http_spec)
http_spec.loader.exec_module(http_module)
parsed = http_module.build_parser().parse_args(
    ["--config", str(http_config), "--port", "9123"]
)
assert parsed.config == str(http_config)
assert parsed.port == 9123
old_xdg_state_home = os.environ.get("XDG_STATE_HOME")
os.environ["XDG_STATE_HOME"] = str(http_state_home)
try:
    server_config = http_module.build_server_config(
        config_path=str(http_config),
        port=9123,
    )
finally:
    if old_xdg_state_home is None:
        os.environ.pop("XDG_STATE_HOME", None)
    else:
        os.environ["XDG_STATE_HOME"] = old_xdg_state_home
assert server_config.host == "127.0.0.1"
assert server_config.port == 9123
assert server_config.proxy_headers is False
assert server_config.access_log is False
old_xdg_state_home = os.environ.get("XDG_STATE_HOME")
os.environ["XDG_STATE_HOME"] = str(http_state_home)
try:
    with mock.patch.object(
        http_module.uvicorn.Server,
        "run",
        side_effect=KeyboardInterrupt,
    ):
        assert http_module.main(
            ["--config", str(http_config), "--port", "9123"]
        ) == 0
finally:
    if old_xdg_state_home is None:
        os.environ.pop("XDG_STATE_HOME", None)
    else:
        os.environ["XDG_STATE_HOME"] = old_xdg_state_home
print("remote HTTP entrypoint tests: PASS")


service_template = engine / "servers/brain-mcp/brain-mcp-remote.service.in"
portable_clone = root / 'portable clone %42 $repo \\backslash "quoted"'
portable_python = portable_clone / ".venv/bin/python"
portable_http = portable_clone / "servers/brain-mcp/http.py"
portable_template = portable_clone / "servers/brain-mcp/brain-mcp-remote.service.in"
portable_python.parent.mkdir(parents=True)
portable_http.parent.mkdir(parents=True)
portable_python.symlink_to(sys.executable)
portable_http.write_text("# remote entrypoint\n")
shutil.copy2(service_template, portable_template)
portable_config = root / "portable-config"
service_config = root / "selected config.toml"
service_config.write_text("[paths]\n")
service_stdout = io.StringIO()
with (
    mock.patch.object(
        brain_mcp_installer,
        "__file__",
        str(portable_clone / "install/brain_mcp.py"),
    ),
    mock.patch.dict(
        os.environ,
        {"XDG_CONFIG_HOME": str(portable_config)},
    ),
    contextlib.redirect_stdout(service_stdout),
):
    service_args = brain_mcp_installer.build_parser().parse_args(
        ["--config", str(service_config), "remote", "install-service"]
    )
    assert service_args.handler(service_args) == 0

installed_service = (
    portable_config / "systemd/user/brain-mcp-remote.service"
)
first_render = installed_service.read_text()
assert str(installed_service) in service_stdout.getvalue()
assert installed_service.stat().st_mode & 0o777 == 0o644
assert "@BRAIN_MCP_" not in first_render
assert "%h/brain" not in first_render
escaped_python = (
    str(portable_python)
    .replace("\\", "\\\\")
    .replace('"', '\\"')
    .replace("%", "%%")
)
escaped_http = (
    str(portable_http)
    .replace("\\", "\\\\")
    .replace('"', '\\"')
    .replace("%", "%%")
)
assert (
    f'ExecStart=:env "{escaped_python}" "{escaped_http}" --config "{service_config}" --port 8787'
) in first_render

systemd_analyze = shutil.which("systemd-analyze")
if sys.platform.startswith("linux"):
    assert systemd_analyze, "systemd-analyze is required for Linux service tests"
if systemd_analyze:
    verified_service = subprocess.run(
        [systemd_analyze, "verify", str(installed_service)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    verification_output = verified_service.stdout + verified_service.stderr
    sandbox_warnings = (
        "Failed to turn off SO_PASSRIGHTS",
        "Failed to enable SO_PASSCRED",
    )
    actionable_output = "\n".join(
        line
        for line in verification_output.splitlines()
        if not any(warning in line for warning in sandbox_warnings)
    )
    assert verified_service.returncode == 0 or not actionable_output, (
        verification_output
    )

with (
    mock.patch.object(
        brain_mcp_installer,
        "__file__",
        str(portable_clone / "install/brain_mcp.py"),
    ),
    mock.patch.dict(
        os.environ,
        {"XDG_CONFIG_HOME": str(portable_config)},
    ),
):
    assert service_args.handler(service_args) == 0
assert installed_service.read_text() == first_render

moved_clone = root / "moved-clone"
moved_python = moved_clone / ".venv/bin/python"
moved_http = moved_clone / "servers/brain-mcp/http.py"
moved_template = moved_clone / "servers/brain-mcp/brain-mcp-remote.service.in"
moved_python.parent.mkdir(parents=True)
moved_http.parent.mkdir(parents=True)
moved_python.symlink_to(sys.executable)
moved_http.write_text("# moved remote entrypoint\n")
shutil.copy2(service_template, moved_template)
with (
    mock.patch.object(
        brain_mcp_installer,
        "__file__",
        str(moved_clone / "install/brain_mcp.py"),
    ),
    mock.patch.dict(
        os.environ,
        {"XDG_CONFIG_HOME": str(portable_config)},
    ),
):
    assert service_args.handler(service_args) == 0
moved_render = installed_service.read_text()
assert str(portable_clone) not in moved_render
assert (
    f'ExecStart=:env "{moved_python}" "{moved_http}" --config "{service_config}" --port 8787'
    in moved_render
)

fallback_home = root / "fallback-home"
with (
    mock.patch.object(
        brain_mcp_installer,
        "__file__",
        str(moved_clone / "install/brain_mcp.py"),
    ),
    mock.patch.dict(os.environ, {"HOME": str(fallback_home)}, clear=True),
):
    assert service_args.handler(service_args) == 0
assert (
    fallback_home / ".config/systemd/user/brain-mcp-remote.service"
).is_file()

service = configparser.ConfigParser(interpolation=None)
assert service.read(installed_service) == [str(installed_service)]
assert service["Service"]["Type"] == "exec"
assert service["Service"]["Restart"] == "on-failure"
assert "ExecStop" not in service["Service"]
assert "PIDFile" not in service["Service"]
assert service["Install"]["WantedBy"] == "default.target"
print("remote systemd user-service tests: PASS")


isolation_database = root / "isolation" / "remote.sqlite3"
isolation_store = RemoteStore(isolation_database)
isolation_store.set_canonical_resource(resource)
client_a = isolation_store.create_client(
    unique_name="Client A",
    profile="claude",
    redirect_uri=CLAUDE_CALLBACK,
)
client_b = isolation_store.create_client(
    unique_name="Client B",
    profile="claude",
    redirect_uri=CLAUDE_CALLBACK,
)


async def authenticate_remote_client(http, created, marker: str) -> dict:
    verifier = marker * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": created.client.client_id,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": marker,
            "scope": "offline_access",
            "resource": resource,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    basic = base64.b64encode(
        f"{created.client.client_id}:{created.client_secret}".encode()
    ).decode()
    response = await http.post(
        "/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def invoke_remote_health(http, access: str, request_id: int):
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {
            "name": "isolation-test",
            "version": "1",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return await http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "health_check",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "health_check",
                "arguments": {},
                "_meta": metadata,
            },
        },
    )


async def multi_client_isolation_checks() -> None:
    first_app = create_remote_app(transport_config, isolation_store)
    async with first_app.router.lifespan_context(first_app):
        transport = httpx2.ASGITransport(app=first_app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="https://brain.example",
        ) as http:
            tokens_a = await authenticate_remote_client(http, client_a, "a")
            tokens_b = await authenticate_remote_client(http, client_b, "b")
            assert (await invoke_remote_health(http, tokens_a["access_token"], 20)).status_code == 200
            assert (await invoke_remote_health(http, tokens_b["access_token"], 21)).status_code == 200

            cross_client_refresh = await http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens_a["refresh_token"],
                    "client_id": client_b.client.client_id,
                    "client_secret": client_b.client_secret,
                    "resource": resource,
                },
            )
            assert cross_client_refresh.status_code == 400
            assert cross_client_refresh.json()["error"] == "invalid_grant"

            cross_verifier = "c" * 43
            cross_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(cross_verifier.encode()).digest()
            ).decode().rstrip("=")
            cross_authorize = await http.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_a.client.client_id,
                    "redirect_uri": CLAUDE_CALLBACK,
                    "code_challenge": cross_challenge,
                    "code_challenge_method": "S256",
                    "resource": resource,
                },
                follow_redirects=False,
            )
            cross_code = parse_qs(
                urlsplit(cross_authorize.headers["location"]).query
            )["code"][0]
            cross_code_exchange = await http.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": cross_code,
                    "redirect_uri": CLAUDE_CALLBACK,
                    "code_verifier": cross_verifier,
                    "resource": resource,
                    "client_id": client_b.client.client_id,
                    "client_secret": client_b.client_secret,
                },
            )
            assert cross_code_exchange.status_code == 400
            assert cross_code_exchange.json()["error"] == "invalid_grant"

            assert isolation_store.remove_client(client_a.client.client_id) == "Client A"
            revoked_access = await invoke_remote_health(
                http,
                tokens_a["access_token"],
                22,
            )
            assert revoked_access.status_code == 401
            assert "resource_metadata=" in revoked_access.headers["www-authenticate"]
            revoked_refresh = await http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens_a["refresh_token"],
                    "client_id": client_a.client.client_id,
                    "client_secret": client_a.client_secret,
                    "resource": resource,
                },
            )
            assert revoked_refresh.status_code in {400, 401}
            assert (await invoke_remote_health(http, tokens_b["access_token"], 23)).status_code == 200

    restarted_store = RemoteStore(isolation_database)
    restarted_app = create_remote_app(transport_config, restarted_store)
    async with restarted_app.router.lifespan_context(restarted_app):
        transport = httpx2.ASGITransport(app=restarted_app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="https://brain.example",
        ) as http:
            authorization_metadata = await http.get(
                "/.well-known/oauth-authorization-server"
            )
            assert authorization_metadata.status_code == 200
            assert authorization_metadata.json()["issuer"] == "https://brain.example"
            protected_metadata = await http.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
            assert protected_metadata.status_code == 200
            assert protected_metadata.json()["resource"] == resource
            refreshed_b = await http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens_b["refresh_token"],
                    "client_id": client_b.client.client_id,
                    "client_secret": client_b.client_secret,
                    "resource": resource,
                },
            )
            assert refreshed_b.status_code == 200, refreshed_b.text
            health_b = await invoke_remote_health(
                http,
                refreshed_b.json()["access_token"],
                24,
            )
            assert health_b.status_code == 200, health_b.text
            assert health_b.json()["result"]["content"][0]["text"] == (
                "pong from brain-mcp"
            )


anyio.run(multi_client_isolation_checks)
print("remote multi-client isolation tests: PASS")
