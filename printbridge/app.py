"""The HTTP surface: one ASGI app that turns a connector link into a `Principal`.

    /mcp/<key>   the link a person pastes into Claude, ChatGPT, Grok …
    /mcp         the same thing for agents, with `Authorization: Bearer <key>`
    /            a one-line page that says printbridge is running and nothing else
    /healthz     for the container healthcheck

The key never reaches the MCP layer: this middleware resolves it, attaches the `Principal` to the
request scope and rewrites the path to `/mcp`, so everything downstream sees one endpoint and one
identity. An unknown, revoked or expired key gets **404** — a prober cannot tell a wrong key from a
server that has no MCP at all.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from printbridge import __version__
from printbridge.auth import (
    SCOPE_CLIENT_IP,
    SCOPE_KEY_ID,
    SCOPE_PRINCIPAL,
    extract_bearer,
    forwarded_ip,
)
from printbridge.runtime import Runtime
from printbridge.server import build_server

log = logging.getLogger("printbridge.http")

PROBE_ALERT_THRESHOLD = 20  # 404s per hour on /mcp/* before we call it key guessing

LANDING_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>printbridge</title>
<meta name="robots" content="noindex">
<style>body{font:16px/1.5 system-ui,sans-serif;margin:12vh auto;max-width:32rem;padding:0 1.5rem;
color:#222}code{background:#f2f2f2;padding:.1em .3em;border-radius:3px}</style></head>
<body><h1>printbridge is running</h1>
<p>Connect through your assistant — there is nothing to see or do on this page.</p>
</body></html>"""


class KeyRoutingMiddleware:
    """ASGI wrapper: connector key (path or bearer) → `Principal` on the request scope."""

    def __init__(self, app: Any, runtime: Runtime):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        socket_ip = (scope.get("client") or ("-", 0))[0]
        scope[SCOPE_CLIENT_IP] = forwarded_ip(headers, socket_ip)

        path_key: str | None = None
        if path.startswith("/mcp/"):
            path_key = path[len("/mcp/") :].split("/", 1)[0] or None
        bearer = extract_bearer(headers)

        principal = self.runtime.auth.resolve(path_key=path_key, bearer=bearer)
        if principal is None:
            await self._not_found(scope, receive, send, had_key=bool(path_key or bearer))
            return

        scope[SCOPE_PRINCIPAL] = principal
        scope[SCOPE_KEY_ID] = principal.key_id
        # One endpoint downstream, whichever shape the caller used.
        scope["path"] = "/mcp"
        scope["raw_path"] = b"/mcp"
        await self.app(scope, receive, send)

    async def _not_found(self, scope: dict, receive: Any, send: Any, *, had_key: bool) -> None:
        if had_key:
            count = self.runtime.probes.record()
            if count >= PROBE_ALERT_THRESHOLD and self.runtime.notifier is not None:
                self.runtime.notifier.on_state(
                    "probe_404",
                    "guessing",
                    f"{count} requests in the last hour used a connector key that does not exist — "
                    f"someone is guessing. Nothing is exposed by this, but if you recognise the "
                    f"pattern you can rotate keys with `printbridge keys rotate <person>`.",
                )
        response = PlainTextResponse("Not Found", status_code=404)
        await response(scope, receive, send)


def create_app(runtime: Runtime) -> Any:
    """The complete ASGI application: MCP behind the key router, plus two harmless pages."""
    mcp = build_server(runtime.settings, runtime)
    mcp_app = mcp.http_app(path="/mcp")
    guarded_mcp = KeyRoutingMiddleware(mcp_app, runtime)

    async def landing(request: Request) -> HTMLResponse:
        return HTMLResponse(LANDING_PAGE)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    side = Starlette(routes=[Route("/", landing), Route("/healthz", healthz)])

    async def dispatch(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            # FastMCP's session manager lives in its own lifespan; the side app needs none.
            await mcp_app(scope, receive, send)
            return
        if scope.get("path", "").startswith("/mcp"):
            await guarded_mcp(scope, receive, send)
            return
        await side(scope, receive, send)

    dispatch.mcp = mcp  # type: ignore[attr-defined]  # handy for tests and for mounting
    return dispatch
