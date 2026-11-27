"""Serve the built React UI straight from the backend (single-process mode).

In development the UI comes from the Vite dev server and in Docker from nginx;
both forward ``/api/...`` to this backend with the prefix stripped. The
Windows installer has neither — there the backend does the same job itself:

* ``GET /``               -> the single-page app (``index.html``, never cached,
                             so an updated install is picked up immediately)
* ``GET /assets/...`` etc -> the built static files (content-hashed)
* ``/api/<path>``         -> ``/<path>`` (REST and WebSocket alike)
* ``GET /monitor``        -> the tiny built-in HTML monitor that otherwise
                             lives at ``/``

It is a pure ASGI wrapper in front of the app, so the route table — and with
it the pinned route inventory in ``tests/test_api_surface.py`` — is untouched.
Enabled by ``NETZSIM_UI_DIST`` (see ``config.Settings.ui_dist``).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

API_PREFIX = "/api"

# On Windows `mimetypes` reads its table from the registry
# (HKCR\<ext>\"Content Type"), and other software is free to rewrite those
# entries — ".js" as text/plain is a classic. Browsers refuse to run a
# <script type="module"> with a non-JavaScript type, so the UI would stay
# blank on such a PC while the server looks healthy. Pin what a web build
# ships instead of trusting the machine.
WEB_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain",
}


def pin_web_types() -> None:
    for ext, kind in WEB_TYPES.items():
        mimetypes.add_type(kind, ext)


class UiMiddleware:
    def __init__(self, app: ASGIApp, dist: Path) -> None:
        self.app = app
        self.dist = Path(dist).resolve()
        self.index = self.dist / "index.html"
        if not self.index.is_file():
            raise FileNotFoundError(f"no index.html in UI folder {self.dist}")
        pin_web_types()
        self.static = StaticFiles(directory=self.dist)
        # The build output is immutable at runtime: an exact-match set of the
        # shipped files decides what is static (no path traversal possible,
        # and API routes can never be shadowed by accident).
        self.files = {
            "/" + p.relative_to(self.dist).as_posix()
            for p in self.dist.rglob("*") if p.is_file()
        }
        self.files.discard("/index.html")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            await self.app(_with_path(scope, path[len(API_PREFIX):] or "/"),
                           receive, send)
            return

        if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD"):
            if path in ("/", "/index.html"):
                response = FileResponse(
                    self.index, media_type="text/html",
                    headers={"Cache-Control": "no-cache"})
                await response(scope, receive, send)
                return
            if path == "/monitor":
                await self.app(_with_path(scope, "/"), receive, send)
                return
            if path in self.files:
                await self.static(scope, receive, send)
                return

        await self.app(scope, receive, send)


def _with_path(scope: Scope, path: str) -> Scope:
    new = dict(scope)
    new["path"] = path
    new["raw_path"] = path.encode("utf-8")
    return new


def attach_ui(app: FastAPI, dist: Path | str) -> None:
    """Put the UI layer in front of ``app`` (raises if ``dist`` has no index.html)."""
    dist = Path(dist)
    if not (dist / "index.html").is_file():
        raise FileNotFoundError(f"no index.html in UI folder {dist.resolve()}")
    app.add_middleware(UiMiddleware, dist=dist)
