"""One web app on loopback: remote MCP and its OAuth. A tunnel in front makes it public."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from brain import mcp as brain_mcp
from brain.auth import CLIENT_HOSTS, AccessCheck, OAuth, load_secret


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "brain"


def create(brain: Path, oauth: OAuth) -> Starlette:
    auth = AuthSettings(issuer_url=oauth.issuer, resource_server_url=oauth.resource, validate_token_resource=True)
    server = brain_mcp.server(brain, token_verifier=oauth, auth=auth)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[urlsplit(oauth.issuer).netloc],
            allowed_origins=[oauth.issuer, *(f"https://{host}" for host in CLIENT_HOSTS)],
        ),
    )
    app.routes.extend(oauth.routes())
    return app


def run(brain: Path, public_url: str, cf_team: str, cf_aud: str, port: int) -> None:
    oauth = OAuth(public_url, load_secret(state_dir()), AccessCheck(cf_team, cf_aud))
    uvicorn.run(create(brain, oauth), host="127.0.0.1", port=port)
