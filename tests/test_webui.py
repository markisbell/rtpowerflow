"""Single-process mode: the backend serves the built React UI itself.

This is what the Windows installer runs (no Node, no nginx): ``webui``
delivers the static build and strips the ``/api`` prefix the UI uses, and
``desktop`` picks a free port. The tests build a throw-away "dist" folder —
no real UI build is needed — and pin the contract the installed product
relies on.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import socket
import threading

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from netzsim import desktop
from netzsim.api import core
from netzsim.api.webui import attach_ui

INDEX = '<!doctype html><html><body><div id="root"></div></body></html>'


@pytest.fixture()
def client(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('ui')", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the dist folder", encoding="utf-8")

    app = FastAPI()
    app.include_router(core.router)

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        await ws.send_text(await ws.receive_text())
        await ws.close()

    attach_ui(app, dist)
    return TestClient(app)


def test_root_serves_the_single_page_app_uncached(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="root"' in r.text
    # index.html must never be cached: an updated install has new asset hashes
    assert r.headers["cache-control"] == "no-cache"


def test_static_build_files_are_served(client):
    r = client.get("/assets/app-abc123.js")
    assert r.status_code == 200 and "console.log" in r.text
    assert client.get("/favicon.svg").status_code == 200


def test_web_mime_types_do_not_depend_on_the_machine(tmp_path):
    """On Windows ``mimetypes`` reads the registry, and other software may
    have rewritten ".js" there (text/plain is a classic). A browser refuses a
    module script with a non-JavaScript type — the UI would stay blank."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("1", encoding="utf-8")
    (dist / "assets" / "app.css").write_text("a{}", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")

    saved = {ext: mimetypes.types_map.get(ext) for ext in (".js", ".css", ".svg")}
    try:
        for ext in saved:                      # a PC with a spoiled registry
            mimetypes.add_type("text/plain", ext)
        app = FastAPI()
        attach_ui(app, dist)
        with TestClient(app) as c:             # the middleware is built here
            assert c.get("/assets/app.js").headers["content-type"].startswith("text/javascript")
            assert c.get("/assets/app.css").headers["content-type"].startswith("text/css")
            assert c.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
            assert c.get("/").headers["content-type"].startswith("text/html")
    finally:
        for ext, kind in saved.items():
            if kind:
                mimetypes.add_type(kind, ext)


def test_only_shipped_files_are_static(client):
    assert client.get("/assets/missing.js").status_code == 404
    # nothing outside the dist folder is reachable
    assert client.get("/../secret.txt").status_code == 404
    assert client.get("/assets/../../secret.txt").status_code == 404


def test_api_prefix_is_stripped_like_nginx_does(client):
    direct = client.get("/health").json()
    prefixed = client.get("/api/health").json()
    assert prefixed == direct and prefixed["app"] == "netzsim"


def test_builtin_monitor_moves_to_slash_monitor(client):
    r = client.get("/monitor")
    assert r.status_code == 200 and "realtime power flow" in r.text
    assert 'id="root"' not in r.text


def test_websocket_prefix_is_stripped_too(client):
    with client.websocket_connect("/api/echo") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "ping"


def test_attach_ui_rejects_a_folder_without_index(tmp_path):
    with pytest.raises(FileNotFoundError):
        attach_ui(FastAPI(), tmp_path)


# --------------------------------------------------------------------------- #
# desktop launcher: port selection
# --------------------------------------------------------------------------- #
def _listener() -> tuple[socket.socket, int]:
    """A listening socket on an OS-chosen port with the whole probe range
    above it still inside the valid ports (Windows hands out ports up to
    65535; the tests below look at ``port .. port + PROBE_RANGE``)."""
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((desktop.HOST, 0))
        port = s.getsockname()[1]
        if port + desktop.PROBE_RANGE <= 65536:
            s.listen(1)
            return s, port
        s.close()


def test_probing_never_leaves_the_valid_port_range():
    # socket.bind raises OverflowError (not OSError) above 65535
    assert desktop.port_is_free(65536) is False
    assert list(desktop.probe_ports(65530)) == [65530, 65531, 65532, 65533, 65534, 65535]
    assert len(desktop.probe_ports(8000)) == desktop.PROBE_RANGE
    desktop.find_running(65530)          # scanning up to the top must not raise


def test_pick_port_skips_a_port_held_by_a_foreign_app():
    held, port = _listener()             # listens, but is not a netzsim
    try:
        assert desktop.port_is_free(port) is False
        chosen, reuse = desktop.pick_port(port)
        assert chosen != port and reuse is False
        assert desktop.port_is_free(chosen)
    finally:
        held.close()


def test_pick_port_takes_the_base_port_when_it_is_free():
    held, port = _listener()
    held.close()                          # known-free port
    assert desktop.pick_port(port) == (port, False)


class _FakeNetzsim(http.server.BaseHTTPRequestHandler):
    """Answers like a netzsim in single-process mode: /health + the UI at /."""

    def do_GET(self):  # noqa: N802 — http.server's naming
        if self.path == "/health":
            body, kind = json.dumps({"app": "netzsim", "version": "x"}).encode(), "application/json"
        else:
            body, kind = INDEX.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


def test_a_running_instance_further_up_is_found_before_a_free_port():
    """An instance started while the base port was taken sits on a higher
    port. Once the base port is free again, a second start must still join
    it — not launch a twin on the base port."""
    probe, base = _listener()
    probe.close()                                     # base: known-free
    server = None
    for port in range(base + 1, base + desktop.PROBE_RANGE):
        try:
            server = http.server.ThreadingHTTPServer((desktop.HOST, port), _FakeNetzsim)
            break
        except OSError:
            continue
    assert server is not None, "no free port for the fake instance"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert desktop.port_is_free(base)
        assert desktop.find_running(base) == server.server_port
        assert desktop.pick_port(base) == (server.server_port, True)
    finally:
        server.shutdown()
        server.server_close()


def test_launcher_reads_host_and_port_from_the_app_folders_dotenv(tmp_path):
    assert desktop.dotenv_settings(tmp_path) == {}            # no .env: nothing
    (tmp_path / ".env").write_text(
        "# comment\nNETZSIM_HOST=0.0.0.0\nNETZSIM_PORT=8123\nNETZSIM_EMPTY=\n",
        encoding="utf-8")
    env = desktop.dotenv_settings(tmp_path)
    assert env["NETZSIM_HOST"] == "0.0.0.0" and env["NETZSIM_PORT"] == "8123"
    assert "NETZSIM_EMPTY" not in env                          # empty = not set
