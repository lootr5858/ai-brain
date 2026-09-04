#!/usr/bin/env python3
"""OAuth and Streamable HTTP boundary for remote brain-mcp access."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlsplit, urlunsplit

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    ProviderTokenVerifier,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from lib.brain_mcp import BrainMcpConfig
from lib.brain_mcp_remote_store import RemoteStore
from lib.mcp_protocol import create_mcp_server


CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PROFILES = frozenset({"chatgpt", "claude", "hermes"})
PKCE_CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
PKCE_VERIFIER_PATTERN = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
RESERVED_RESOURCE_PATHS = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/authorize",
        "/token",
    }
)


class RemoteAccessError(ValueError):
    pass


def _parsed_url(value: str, *, scheme: str, label: str):
    text = str(value or "").strip()
    if not text or any(character.isspace() for character in text):
        raise RemoteAccessError(f"{label} URL is invalid")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise RemoteAccessError(f"{label} URL is invalid") from exc
    if (
        parsed.scheme.casefold() != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "#" in text
    ):
        raise RemoteAccessError(f"{label} URL is invalid")
    return text, parsed, port


def normalize_public_url(value: str) -> str:
    text, parsed, port = _parsed_url(
        value,
        scheme="https",
        label="public HTTPS",
    )
    if "?" in text:
        raise RemoteAccessError("public HTTPS URL must not include a query")
    host = parsed.hostname.casefold()
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host if port in (None, 443) else f"{rendered_host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    if unquote(path) in RESERVED_RESOURCE_PATHS:
        raise RemoteAccessError("public HTTPS URL path conflicts with OAuth endpoints")
    return urlunsplit(("https", authority, path, "", ""))


def client_redirect_uri(profile: str, supplied: str | None) -> str | None:
    if profile not in PROFILES:
        raise RemoteAccessError(f"unknown client profile: {profile}")
    if profile == "hermes":
        if supplied:
            raise RemoteAccessError("Hermes callback is selected at authorization time")
        return None
    if profile == "claude":
        if supplied not in (None, CLAUDE_CALLBACK):
            raise RemoteAccessError("Claude uses its fixed callback")
        return CLAUDE_CALLBACK
    if not supplied:
        raise RemoteAccessError("ChatGPT callback is required")
    _parsed_url(supplied, scheme="https", label="ChatGPT HTTPS callback")
    return supplied


def redirect_uri_matches(
    profile: str,
    requested: str,
    registered: str | None,
) -> bool:
    if profile == "hermes":
        try:
            text, parsed, port = _parsed_url(
                requested,
                scheme="http",
                label="Hermes callback",
            )
        except RemoteAccessError:
            return False
        return (
            parsed.hostname.casefold() in {"127.0.0.1", "localhost"}
            and port is not None
            and 1024 <= port <= 65535
            and parsed.path == "/callback"
            and "?" not in text
        )
    if profile in {"chatgpt", "claude"}:
        return registered is not None and requested == registered
    return False


def issuer_from_resource(resource: str) -> str:
    parsed = urlsplit(normalize_public_url(resource))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class StoredRefreshToken(RefreshToken):
    resource: str


class StoredAuthorizationCode(AuthorizationCode):
    registered_redirect_uri: str


class RemoteOAuthProvider:
    def __init__(self, store: RemoteStore) -> None:
        resource = store.get_canonical_resource()
        if resource is None:
            raise RemoteAccessError("remote brain-MCP is not initialized")
        self.store = store
        self.resource = resource
        self.issuer = issuer_from_resource(resource)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self.store.get_client(client_id)
        if client is None:
            return None
        redirects = [client.redirect_uri] if client.redirect_uri else None
        return OAuthClientInformationFull(
            client_id=client.client_id,
            client_name=client.unique_name,
            redirect_uris=redirects,
            scope="offline_access",
            token_endpoint_auth_method="client_secret_basic",
            grant_types=["authorization_code", "refresh_token"],
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("dynamic client registration is disabled")

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
        *,
        registered_redirect_uri: str | None = None,
    ) -> str:
        scope = " ".join(params.scopes or [])
        callback = registered_redirect_uri or str(params.redirect_uri)
        code = self.store.create_authorization_code(
            client_id=client.client_id,
            redirect_uri=callback,
            code_challenge=params.code_challenge,
            resource=self.resource,
            scope=scope,
        )
        return construct_redirect_uri(
            callback,
            code=code,
            state=params.state,
            iss=self.issuer,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        record = self.store.load_authorization_code(
            authorization_code,
            client_id=client.client_id,
        )
        if record is None:
            return None
        return StoredAuthorizationCode(
            code=authorization_code,
            scopes=record.scope.split() if record.scope else [],
            expires_at=record.expires_at,
            client_id=record.client_id,
            code_challenge=record.code_challenge,
            redirect_uri=record.redirect_uri,
            redirect_uri_provided_explicitly=True,
            resource=record.resource,
            registered_redirect_uri=record.redirect_uri,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        callback = (
            authorization_code.registered_redirect_uri
            if isinstance(authorization_code, StoredAuthorizationCode)
            else str(authorization_code.redirect_uri)
        )
        tokens = self.store.exchange_authorization_code(
            authorization_code.code,
            client_id=client.client_id,
            redirect_uri=callback,
            code_challenge=authorization_code.code_challenge,
            resource=self.resource,
        )
        if tokens is None:
            raise TokenError("invalid_grant", "authorization code is invalid")
        return OAuthToken(
            access_token=tokens.access_token,
            expires_in=3600,
            scope=tokens.scope or None,
            refresh_token=tokens.refresh_token,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> StoredRefreshToken | None:
        record = self.store.load_refresh_token(
            refresh_token,
            client_id=client.client_id,
        )
        if record is None:
            return None
        return StoredRefreshToken(
            token=refresh_token,
            client_id=record.client_id,
            scopes=record.scope.split() if record.scope else [],
            expires_at=int(record.expires_at),
            resource=record.resource,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        tokens = self.store.rotate_refresh_token(
            refresh_token.token,
            client_id=client.client_id,
            resource=refresh_token.resource,
            scope=" ".join(scopes),
        )
        if tokens is None:
            raise TokenError("invalid_grant", "refresh token is invalid")
        scope = " ".join(scopes)
        return OAuthToken(
            access_token=tokens.access_token,
            expires_in=3600,
            scope=scope or None,
            refresh_token=tokens.refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.store.lookup_access_token(token, resource=self.resource)
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scope.split() if record.scope else [],
            expires_at=int(record.expires_at),
            resource=record.resource,
            claims={"iss": self.issuer},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        raise NotImplementedError("remote token revocation endpoint is disabled")


def _oauth_error(
    error: str,
    description: str,
    *,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _authorization_error(
    redirect_uri: str,
    *,
    state: str | None,
    issuer: str,
    error: str,
    description: str,
) -> RedirectResponse:
    return RedirectResponse(
        construct_redirect_uri(
            redirect_uri,
            error=error,
            error_description=description,
            state=state,
            iss=issuer,
        ),
        status_code=302,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _string_value(values: Any, name: str) -> str | None:
    value = values.get(name)
    return value if isinstance(value, str) else None


async def _authorize_request(
    request: Request,
    provider: RemoteOAuthProvider,
) -> Response:
    values = request.query_params if request.method == "GET" else await request.form()
    client_id = _string_value(values, "client_id")
    redirect_uri = _string_value(values, "redirect_uri")
    challenge = _string_value(values, "code_challenge")
    method = _string_value(values, "code_challenge_method")
    resource = _string_value(values, "resource")
    state = _string_value(values, "state")
    scope = _string_value(values, "scope")
    if not client_id:
        return _oauth_error("invalid_request", "client_id is required")
    client_record = provider.store.get_client(client_id)
    client = await provider.get_client(client_id)
    if client_record is None or client is None:
        return _oauth_error("unauthorized_client", "client is not registered")
    if not redirect_uri or not redirect_uri_matches(
        client_record.profile,
        redirect_uri,
        client_record.redirect_uri,
    ):
        return _oauth_error("invalid_request", "redirect_uri is not registered")
    if _string_value(values, "response_type") != "code":
        return _authorization_error(
            redirect_uri,
            state=state,
            issuer=provider.issuer,
            error="unsupported_response_type",
            description="response_type must be code",
        )
    if (
        method != "S256"
        or not challenge
        or not PKCE_CHALLENGE_PATTERN.fullmatch(challenge)
    ):
        return _authorization_error(
            redirect_uri,
            state=state,
            issuer=provider.issuer,
            error="invalid_request",
            description="explicit PKCE S256 is required",
        )
    if resource != provider.resource:
        return _authorization_error(
            redirect_uri,
            state=state,
            issuer=provider.issuer,
            error="invalid_target",
            description="resource is not the canonical MCP URL",
        )
    if scope not in (None, "", "offline_access"):
        return _authorization_error(
            redirect_uri,
            state=state,
            issuer=provider.issuer,
            error="invalid_scope",
            description="scope is not supported",
        )
    params = AuthorizationParams(
        state=state,
        scopes=["offline_access"] if scope == "offline_access" else [],
        code_challenge=challenge,
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )
    location = await provider.authorize(
        client,
        params,
        registered_redirect_uri=redirect_uri,
    )
    return RedirectResponse(
        location,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


async def _authenticate_client(
    request: Request,
    values: Any,
    provider: RemoteOAuthProvider,
) -> OAuthClientInformationFull | None:
    form_client_id = _string_value(values, "client_id")
    form_secret = _string_value(values, "client_secret")
    header = request.headers.get("Authorization", "")
    client_id = form_client_id
    secret = form_secret
    if header:
        if not header.startswith("Basic ") or form_secret is not None:
            return None
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
            encoded_id, encoded_secret = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        client_id = unquote(encoded_id)
        secret = unquote(encoded_secret)
        if form_client_id is not None and form_client_id != client_id:
            return None
    if not client_id or not secret:
        return None
    if not provider.store.verify_client_secret(client_id, secret):
        return None
    return await provider.get_client(client_id)


async def _token_request(
    request: Request,
    provider: RemoteOAuthProvider,
) -> Response:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        return _oauth_error(
            "invalid_request",
            "token requests must be form encoded",
            status_code=415,
        )
    values = await request.form()
    client = await _authenticate_client(request, values, provider)
    if client is None:
        return _oauth_error(
            "invalid_client",
            "client authentication failed",
            status_code=401,
        )
    resource = _string_value(values, "resource")
    if resource != provider.resource:
        return _oauth_error("invalid_target", "resource is not the canonical MCP URL")
    grant_type = _string_value(values, "grant_type")
    try:
        if grant_type == "authorization_code":
            code = _string_value(values, "code")
            redirect_uri = _string_value(values, "redirect_uri")
            verifier = _string_value(values, "code_verifier")
            if (
                not code
                or not redirect_uri
                or not verifier
                or not PKCE_VERIFIER_PATTERN.fullmatch(verifier)
            ):
                return _oauth_error("invalid_request", "authorization code request is incomplete")
            authorization_code = await provider.load_authorization_code(client, code)
            if (
                authorization_code is None
                or authorization_code.resource != provider.resource
                or (
                    authorization_code.registered_redirect_uri
                    if isinstance(authorization_code, StoredAuthorizationCode)
                    else str(authorization_code.redirect_uri)
                )
                != redirect_uri
            ):
                return _oauth_error("invalid_grant", "authorization code is invalid")
            verifier_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).decode().rstrip("=")
            if verifier_challenge != authorization_code.code_challenge:
                return _oauth_error("invalid_grant", "code_verifier is incorrect")
            result = await provider.exchange_authorization_code(
                client,
                authorization_code,
            )
        elif grant_type == "refresh_token":
            raw_refresh = _string_value(values, "refresh_token")
            if not raw_refresh:
                return _oauth_error("invalid_request", "refresh_token is required")
            refresh = await provider.load_refresh_token(client, raw_refresh)
            if refresh is None or refresh.resource != provider.resource:
                return _oauth_error("invalid_grant", "refresh token is invalid")
            requested_scope = _string_value(values, "scope")
            if requested_scope is not None and requested_scope.strip() != requested_scope:
                return _oauth_error("invalid_scope", "scope is not supported")
            scopes = requested_scope.split() if requested_scope else refresh.scopes
            if any(scope not in refresh.scopes for scope in scopes):
                return _oauth_error("invalid_scope", "scope exceeds the original grant")
            result = await provider.exchange_refresh_token(client, refresh, scopes)
        else:
            return _oauth_error("unsupported_grant_type", "grant type is not supported")
    except TokenError as exc:
        return _oauth_error(exc.error, exc.error_description or "token exchange failed")
    return JSONResponse(
        result.model_dump(exclude_none=True),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def remote_oauth_routes(store: RemoteStore) -> list[Route]:
    provider = RemoteOAuthProvider(store)
    issuer = AuthSettings(
        issuer_url=provider.issuer,
        resource_server_url=provider.resource,
    ).issuer_url
    metadata = build_metadata(
        issuer,
        None,
        ClientRegistrationOptions(
            enabled=False,
            valid_scopes=["offline_access"],
        ),
        RevocationOptions(enabled=False),
    )
    metadata.authorization_response_iss_parameter_supported = True

    async def authorize(request: Request) -> Response:
        return await _authorize_request(request, provider)

    async def token(request: Request) -> Response:
        return await _token_request(request, provider)

    return [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(
                MetadataHandler(metadata).handle,
                ["GET", "OPTIONS"],
            ),
            methods=["GET", "OPTIONS"],
        ),
        Route("/authorize", endpoint=authorize, methods=["GET", "POST"]),
        Route(
            "/token",
            endpoint=cors_middleware(token, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
    ]


def create_remote_app(config: BrainMcpConfig, store: RemoteStore | None = None):
    state = store or RemoteStore()
    provider = RemoteOAuthProvider(state)
    parsed_resource = urlsplit(provider.resource)
    protected_path = parsed_resource.path or "/"
    rendered_host = (
        f"[{parsed_resource.hostname}]"
        if ":" in parsed_resource.hostname
        else parsed_resource.hostname
    )
    allowed_hosts = [parsed_resource.netloc]
    if parsed_resource.port is None:
        allowed_hosts.append(f"{rendered_host}:443")
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            *allowed_hosts,
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ],
        allowed_origins=[
            provider.issuer,
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )
    auth = AuthSettings(
        issuer_url=provider.issuer,
        resource_server_url=provider.resource,
        required_scopes=None,
        client_registration_options=ClientRegistrationOptions(
            enabled=False,
            valid_scopes=["offline_access"],
        ),
        revocation_options=RevocationOptions(enabled=False),
    )
    server = create_mcp_server(config)
    return server.streamable_http_app(
        streamable_http_path=protected_path,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
        host="127.0.0.1",
        auth=auth,
        token_verifier=ProviderTokenVerifier(provider),
        auth_server_provider=None,
        custom_starlette_routes=remote_oauth_routes(state),
    )


__all__ = [
    "CLAUDE_CALLBACK",
    "RemoteOAuthProvider",
    "RemoteAccessError",
    "client_redirect_uri",
    "create_remote_app",
    "issuer_from_resource",
    "normalize_public_url",
    "remote_oauth_routes",
    "redirect_uri_matches",
]
