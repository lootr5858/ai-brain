"""OAuth for the remote MCP server: one owner, no stored clients, signed tokens.

Clients identify themselves with a Client ID Metadata Document (CIMD). The owner approves each
connection at /authorize, which Cloudflare Access guards and the engine checks again. Codes and
tokens are JWTs signed with one secret, so deleting the secret revokes everything.
"""
from __future__ import annotations

import html
import http.client
import json
import os
import secrets
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError, construct_redirect_uri
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

# Only these apps may connect, and codes only ever go back to them.
CLIENT_HOSTS = {"claude.ai", "chatgpt.com"}
REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
}
ACCESS_HEADER = "cf-access-jwt-assertion"
CONSENT_TTL = 600
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CLIENT_TTL = 3600
MAX_DOCUMENT = 64 * 1024
MAX_TOKEN_REQUEST = 64 * 1024
PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Brain</title>
<style>
  :root { color-scheme: light dark; font: 16px/1.5 system-ui, sans-serif; }
  main { max-width: 28rem; margin: 15vh auto; padding: 0 1rem; }
  small { opacity: .7; }
  button { font: inherit; padding: .5rem 1.25rem; margin-right: .5rem; }
</style>
<main>{body}</main>
"""


def load_secret(state: Path) -> str:
    """Return the signing secret, creating it on first use; only the owner can read it."""
    path = Path(state) / "secret"
    if not path.exists():
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(secrets.token_urlsafe(48))
    return path.read_text().strip()


def fetch_document(url: str) -> dict:
    """Fetch a client's metadata document, bounded in time and size."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "brain-engine"})
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read(MAX_DOCUMENT + 1)
    if len(body) > MAX_DOCUMENT:
        raise ValueError("client metadata document too large")
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError("client metadata document is not an object")
    return document


class AccessCheck:
    """Verify the Cloudflare Access JWT on owner-only requests; return who signed in, or None."""

    def __init__(self, team: str, aud: str):
        self.issuer, self.aud = f"https://{team}", aud
        self.keys = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs")

    def __call__(self, token: str) -> str | None:
        if not token:
            return None
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer)
        except jwt.PyJWTError:
            return None
        return claims.get("email") or claims.get("sub") or "owner"


def _page(body: str, status: int = 200) -> HTMLResponse:
    headers = {
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
    return HTMLResponse(PAGE.replace("{body}", body), status, headers)


class OAuth:
    """The authorization server: /authorize is ours, /token runs on the SDK's handler over these methods."""

    def __init__(self, public_url: str, secret: str, owner: Callable[[str], str | None],
                 fetch: Callable[[str], dict] = fetch_document):
        self.issuer = public_url.rstrip("/")
        self.resource = f"{self.issuer}/mcp"
        self.secret, self.owner, self.fetch = secret, owner, fetch
        self._clients: dict[str, tuple[float, OAuthClientInformationFull]] = {}
        self._spent: dict[str, int] = {}

    def routes(self) -> list[Route]:
        token = TokenHandler(self, ClientAuthenticator(self))
        return [
            Route("/.well-known/oauth-authorization-server", self.metadata, methods=["GET"]),
            Route("/authorize", self.authorize, methods=["GET", "POST"]),
            Route("/token", RequestBodyLimitMiddleware(request_response(token.handle), MAX_TOKEN_REQUEST), methods=["POST"]),
        ]

    async def metadata(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": ["offline_access"],
        })

    async def authorize(self, request: Request) -> Response:
        subject = await run_in_threadpool(self.owner, request.headers.get(ACCESS_HEADER, ""))
        if subject is None:
            return _page("<p>Sign in through Cloudflare Access to continue.</p>", 403)
        if request.method == "POST":
            return await self._decide(request, subject)
        return await self._consent(request, subject)

    async def _consent(self, request: Request, subject: str) -> Response:
        query = request.query_params
        client_id, redirect_uri, state = query.get("client_id", ""), query.get("redirect_uri", ""), query.get("state")
        client = await self.get_client(client_id)
        if client is None or redirect_uri not in {str(uri) for uri in client.redirect_uris}:
            return _page("<p>This app or its return address is not allowed.</p>", 400)
        if query.get("response_type") != "code":
            return self._back(redirect_uri, state, error="unsupported_response_type")
        if not query.get("code_challenge") or query.get("code_challenge_method") != "S256":
            return self._back(redirect_uri, state, error="invalid_request", error_description="PKCE S256 is required")
        if query.get("resource", self.resource) != self.resource:
            return self._back(redirect_uri, state, error="invalid_target")
        signed = self._sign("consent", CONSENT_TTL, client_id=client_id, redirect_uri=redirect_uri, state=state,
                            code_challenge=query["code_challenge"], scope=query.get("scope", ""))
        return _page(
            "<h1>Connect to your brain?</h1>"
            f"<p><b>{html.escape(urlsplit(client_id).netloc)}</b> will be able to read your rules, skills, and "
            "personal knowledge, and to edit your knowledge files.</p>"
            f"<p><small>Signed in as {html.escape(subject)}. Returns to "
            f"{html.escape(urlsplit(redirect_uri).netloc)}.</small></p>"
            '<form method="post" action="/authorize">'
            f'<input type="hidden" name="request" value="{html.escape(signed)}">'
            '<button name="decision" value="allow">Allow</button>'
            '<button name="decision" value="deny">Deny</button></form>'
        )

    async def _decide(self, request: Request, subject: str) -> Response:
        form = await request.form()
        claims = self._open(str(form.get("request", "")), "consent")
        if claims is None:
            return _page("<p>This request expired. Start again from the app.</p>", 400)
        if form.get("decision") != "allow":
            return self._back(claims["redirect_uri"], claims["state"], error="access_denied")
        code = self._sign("code", CODE_TTL, client_id=claims["client_id"], sub=subject, scope=claims["scope"],
                          redirect_uri=claims["redirect_uri"], code_challenge=claims["code_challenge"])
        return self._back(claims["redirect_uri"], claims["state"], code=code)

    def _back(self, redirect_uri: str, state: str | None, **params: str) -> RedirectResponse:
        """Return to the client; every authorization response names the issuer (RFC 9207)."""
        url = construct_redirect_uri(redirect_uri, state=state, iss=self.issuer, **params)
        return RedirectResponse(url, 302, {"Cache-Control": "no-store"})

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Resolve a client from its document; hosts outside the allowlist are never fetched."""
        try:
            parts = urlsplit(client_id)
        except ValueError:
            return None
        if parts.scheme != "https" or parts.netloc not in CLIENT_HOSTS or parts.fragment or not parts.path.strip("/"):
            return None
        cached = self._clients.get(client_id)
        if cached and cached[0] > time.time():
            return cached[1]
        try:
            document = await run_in_threadpool(self.fetch, client_id)
        except (OSError, ValueError, http.client.HTTPException):
            return None
        redirects = [uri for uri in document.get("redirect_uris", []) if uri in REDIRECTS]
        if document.get("client_id") != client_id or not redirects:
            return None
        client = OAuthClientInformationFull(
            client_id=client_id, redirect_uris=redirects, token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        )
        self._clients[client_id] = (time.time() + CLIENT_TTL, client)
        return client

    async def load_authorization_code(self, client, code: str) -> AuthorizationCode | None:
        claims = self._open(code, "code")
        if claims is None or claims["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=code, scopes=claims["scope"].split(), expires_at=claims["exp"], client_id=claims["client_id"],
            code_challenge=claims["code_challenge"], redirect_uri=claims["redirect_uri"],
            redirect_uri_provided_explicitly=True, resource=self.resource, subject=claims["sub"],
        )

    async def exchange_authorization_code(self, client, code: AuthorizationCode) -> OAuthToken:
        self._spend(code.code, "code")
        return self._tokens(client.client_id, code.subject, code.scopes)

    async def load_refresh_token(self, client, token: str) -> RefreshToken | None:
        claims = self._open(token, "refresh")
        if claims is None or claims["client_id"] != client.client_id:
            return None
        return RefreshToken(token=token, client_id=claims["client_id"], scopes=claims["scope"].split(),
                            expires_at=claims["exp"], resource=self.resource, subject=claims["sub"])

    async def exchange_refresh_token(self, client, token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self._spend(token.token, "refresh")
        return self._tokens(client.client_id, token.subject, scopes)

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = self._open(token, "access")
        if claims is None:
            return None
        return AccessToken(token=token, client_id=claims["client_id"], scopes=claims["scope"].split(),
                           expires_at=claims["exp"], resource=self.resource, subject=claims["sub"])

    def _tokens(self, client_id: str, subject: str | None, scopes: list[str]) -> OAuthToken:
        claims = {"client_id": client_id, "sub": subject or "owner", "scope": " ".join(scopes)}
        return OAuthToken(
            access_token=self._sign("access", ACCESS_TTL, **claims), expires_in=ACCESS_TTL,
            refresh_token=self._sign("refresh", REFRESH_TTL, **claims), scope=claims["scope"] or None,
        )

    def _spend(self, token: str, kind: str) -> None:
        """Codes work once and refresh tokens rotate; the record of spent ones lasts until restart."""
        claims = self._open(token, kind)
        now = time.time()
        self._spent = {jti: expires for jti, expires in self._spent.items() if expires > now}
        if claims is None or claims["jti"] in self._spent:
            raise TokenError("invalid_grant", f"{kind} is expired or already used")
        self._spent[claims["jti"]] = claims["exp"]

    def _sign(self, kind: str, ttl: int, **claims: object) -> str:
        now = int(time.time())
        payload = {"iss": self.issuer, "aud": self.resource, "typ": kind, "jti": secrets.token_urlsafe(16),
                   "iat": now, "exp": now + ttl, **claims}
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def _open(self, token: str, kind: str) -> dict | None:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], audience=self.resource, issuer=self.issuer,
                                options={"require": ["exp", "jti", "typ"]})
        except jwt.PyJWTError:
            return None
        return claims if claims["typ"] == kind else None
