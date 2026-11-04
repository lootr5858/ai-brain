"""The owner's dashboard: browse the brain and edit knowledge files under the same store rules as MCP.

Agents can write these files, so the page treats their text as untrusted; see the CSP below. Every
request needs the owner's Cloudflare Access login. Cloudflare adds that login to any request carrying
its cookie, even one started by another site, so writes must also prove they came from this page.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brain.auth import ACCESS_HEADER
from brain.mcp import SKILL_FILE, SKILLS, skill_description, skill_names
from brain.store import MAX_HITS, Conflict, Store, StoreError, digest

STATIC = Path(__file__).parent / "static"
VIA = "via dashboard"
MAX_BODY = 1_000_000
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class OwnerOnly:
    """Let through only the signed-in owner, and for writes only requests sent by the dashboard's own page."""

    def __init__(self, app: ASGIApp, owner: Callable[[str], str | None], public_url: str):
        parts = urlsplit(public_url)
        self.app, self.owner, self.origin = app, owner, f"{parts.scheme}://{parts.netloc}".lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.update(HEADERS)
                headers["Cache-Control"] = "no-store" if scope["path"].startswith("/api/") else "no-cache"
            await send(message)

        refusal = await self._refusal(Request(scope))
        await (refusal or self.app)(scope, receive, with_headers)

    async def _refusal(self, request: Request) -> Response | None:
        headers = request.headers
        if await run_in_threadpool(self.owner, headers.get(ACCESS_HEADER, "")) is None:
            return JSONResponse({"error": "Sign in through Cloudflare Access.", "signin": True}, 403)
        if request.method in ("GET", "HEAD"):
            return None
        if headers.get("origin", "").lower() != self.origin:
            return JSONResponse({"error": "This request did not come from the dashboard."}, 403)
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "Send JSON."}, 415)
        return None


def _field(args: Any, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise StoreError(f"`{name}` is required")
    return value


def _endpoint(handler: Callable[[Any], Any]) -> Callable[[Request], Any]:
    """Run a store call off the event loop; a refusal becomes a JSON error the page can show."""

    async def endpoint(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool(handler, await _arguments(request)))
        except Conflict as exc:
            return JSONResponse({"error": str(exc)}, 409)
        except (StoreError, UnicodeError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, 400)

    return endpoint


async def _arguments(request: Request) -> Any:
    if request.method == "GET":
        return request.query_params
    try:
        body = await request.json()
    except ValueError:
        raise StoreError("the request body is not JSON") from None
    if not isinstance(body, dict):
        raise StoreError("the request body must be a JSON object")
    return body


def dashboard(brain: Path, owner: Callable[[str], str | None], public_url: str) -> Mount:
    """Build the dashboard as a catch-all mount: whatever isn't public before it needs the owner."""
    agents, user = Store(Path(brain) / "agents"), Store(Path(brain) / "user")

    def skill(name: str) -> dict:
        base = f"{SKILLS}/{name}"
        try:
            description = skill_description(agents.read(f"{base}/{SKILL_FILE}"))
        except StoreError:
            description = ""
        files = [rel.removeprefix(f"{base}/") for rel, _ in agents.list(base)]
        return {"name": name, "description": description, "files": sorted(files, key=lambda f: (f != SKILL_FILE, f))}

    def tree(args: Any) -> dict:
        dirty = user.dirty()
        return {
            "user": [{"path": rel, "size": size, "dirty": rel in dirty} for rel, size in user.list()],
            "skills": [skill(name) for name in skill_names(agents)],
        }

    def read_file(args: Any) -> dict:
        path = _field(args, "path")
        if args.get("store") != "skills":
            text = user.read(path)
            return {"text": text, "hash": digest(text), "dirty": path in user.dirty(), "writable": True}
        if path.split("/")[0] not in skill_names(agents):
            raise StoreError(f"not a skill file: {path}")
        text = agents.read(f"{SKILLS}/{path}")
        return {"text": text, "hash": digest(text), "dirty": False, "writable": False}

    def saved(path: str, commit: str) -> dict:
        return {"commit": commit, "hash": digest(user.read(path))}

    def save_file(args: Any) -> dict:
        path = _field(args, "path")
        return saved(path, user.replace(path, _field(args, "text"), _field(args, "base"), VIA))

    def create_file(args: Any) -> dict:
        path = _field(args, "path")
        return saved(path, user.create(path, _field(args, "text"), VIA))

    def restore_file(args: Any) -> dict:
        path = _field(args, "path")
        return saved(path, user.restore(path, _field(args, "sha"), VIA))

    def search(args: Any) -> list[dict]:
        query = _field(args, "q")
        hits = [{"store": "user", "path": rel, "line": line, "text": text} for rel, line, text in user.matches(query)]
        found: list[dict] = []
        for name in skill_names(agents):
            for rel, line, text in agents.matches(query, f"{SKILLS}/{name}"):
                found.append({"store": "skills", "path": rel.removeprefix(f"{SKILLS}/"), "line": line, "text": text})
        return hits + found[:MAX_HITS]

    def history(args: Any) -> list[dict]:
        return user.history(_field(args, "path"))

    def diff(args: Any) -> dict:
        return {"diff": user.diff(_field(args, "path"), args.get("sha") or "")}

    routes = Router([
        Route("/api/tree", _endpoint(tree), methods=["GET"]),
        Route("/api/file", _endpoint(read_file), methods=["GET"]),
        Route("/api/file", _endpoint(save_file), methods=["PUT"]),
        Route("/api/file", _endpoint(create_file), methods=["POST"]),
        Route("/api/search", _endpoint(search), methods=["GET"]),
        Route("/api/history", _endpoint(history), methods=["GET"]),
        Route("/api/diff", _endpoint(diff), methods=["GET"]),
        Route("/api/restore", _endpoint(restore_file), methods=["POST"]),
        Mount("/", StaticFiles(directory=STATIC, html=True)),
    ])
    return Mount("/", OwnerOnly(RequestBodyLimitMiddleware(routes, MAX_BODY), owner, public_url))
