"""Desktop launcher: API + built UI in ONE process, then open the browser.

This is what the Windows installer's start-menu entry runs
(``python.exe -m netzsim.desktop``). Unlike the developer launchers
(``start_netzsim.bat`` / ``.sh``) it needs neither Node nor a second server:
the backend serves the pre-built React UI itself (``NETZSIM_UI_DIST``, see
``netzsim.api.webui``).

It works from a source checkout too, once the UI is built::

    cd ui && npm run build && cd ..
    PYTHONPATH=src python -m netzsim.desktop

Behaviour:

* The port is not hard-wired. Probing starts at ``--port`` / ``NETZSIM_PORT``
  (environment or the app folder's ``.env``) / 8000 and moves upward past
  ports held by other applications.
* One instance per user session. A netzsim that already serves the UI is
  recognised through ``/health`` and reused — a second start just opens the
  browser on it; a second start *while the first is still loading* waits for
  it instead of launching a twin.
* The server binds ``127.0.0.1`` unless ``NETZSIM_HOST`` says otherwise
  (``0.0.0.0`` lets other devices reach it, e.g. an external-node feeder).
  The browser is always pointed at ``127.0.0.1`` — not ``localhost``, which
  on Windows may resolve to ``::1`` first, where a different application
  could be listening on the same port.
* The console window IS the simulator. Closing it (or Ctrl+C) stops the
  server gracefully — an active recording is finalised first. QuickEdit is
  switched off: with it, a mouse click into the window would freeze every
  console write, and with that the whole server.

Nothing from the ``netzsim`` package is imported before the environment is
prepared: ``netzsim.config`` reads its settings at import time.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

PROBE_RANGE = 20
HOST = "127.0.0.1"               # where we probe and where the browser goes
# Lets the installer / uninstaller notice a running instance (Inno Setup
# AppMutex) and a second launcher notice a first one that is still loading.
MUTEX_NAME = "netzsim-desktop-running"
STARTING_WAIT_S = 120            # how long a second start waits for the first
CLOSE_GRACE_S = 4.5              # Windows allows ~5 s after a console close

# Local probes must never travel through a configured HTTP proxy (campus
# networks): the proxy would answer instead of our own server.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def app_root() -> Path:
    """The folder that holds ``data/``, ``ui/dist`` and ``docs/``."""
    env = os.environ.get("NETZSIM_HOME")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[2]


def dotenv_settings(root: Path) -> dict[str, str]:
    """The keys of ``<root>/.env`` — the server reads the same file, but the
    launcher needs host and port BEFORE the settings module is imported."""
    path = root / ".env"
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values   # ships with pydantic-settings

        return {k: v for k, v in dotenv_values(path).items() if v}
    except Exception:  # noqa: BLE001 — a broken .env must not stop the start
        return {}


def _get(url: str, timeout: float = 1.5) -> tuple[int, bytes]:
    with _OPENER.open(url, timeout=timeout) as r:
        return r.status, r.read()


def port_is_free(port: int, host: str = HOST) -> bool:
    if not 0 < port < 65536:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):       # Windows: no port sharing
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def health(port: int, timeout: float = 1.5) -> dict | None:
    """The ``/health`` document of whatever listens on ``port`` (None = nothing usable)."""
    try:
        status, body = _get(f"http://{HOST}:{port}/health", timeout)
        doc = json.loads(body)
    except Exception:  # noqa: BLE001 — refused, timed out, not JSON: all "not ours"
        return None
    return doc if status == 200 and isinstance(doc, dict) else None


def serves_ui(port: int) -> bool:
    """True when a netzsim on ``port`` also delivers the React UI at ``/``."""
    try:
        _, body = _get(f"http://{HOST}:{port}/")
    except Exception:  # noqa: BLE001
        return False
    return b'id="root"' in body


def probe_ports(base: int) -> range:
    """The ports tried from ``base`` on — never past the last valid one."""
    return range(base, min(base + PROBE_RANGE, 65536))


def find_running(base: int) -> int | None:
    """Port of a netzsim that already serves the UI, anywhere in the range.

    The whole range is scanned, not just up to the first free port: an
    instance started while port ``base`` was taken sits further up, and must
    still be found after ``base`` has become free again.
    """
    for port in probe_ports(base):
        if port_is_free(port):          # nothing listens — skip the slow HTTP probe
            continue
        doc = health(port)
        if doc and doc.get("app") == "netzsim" and serves_ui(port):
            return port
    return None


def pick_port(base: int, host: str = HOST) -> tuple[int, bool]:
    """``(port, reuse)`` — reuse=True: a netzsim with UI already runs there."""
    running = find_running(base)
    if running is not None:
        return running, True
    ports = probe_ports(base)
    for port in ports:
        # free on loopback AND on the address we are going to bind
        if port_is_free(port) and (host == HOST or port_is_free(port, host)):
            return port, False
    last = ports[-1] if ports else base
    raise RuntimeError(f"Kein freier Port im Bereich {base}–{last} gefunden.")


def _open_when_ready(port: int, url: str, open_browser: bool) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        doc = health(port, timeout=1.0)
        if doc and doc.get("app") == "netzsim":
            print(f"\n  netzsim ist bereit:  {url}\n", flush=True)
            if open_browser:
                webbrowser.open(url)
            return
        time.sleep(0.5)
    print("\n  WARNUNG: Der Server hat nach 3 Minuten noch nicht geantwortet.\n",
          flush=True)


# --------------------------------------------------------------------------- #
# Windows console plumbing (every helper is a no-op elsewhere and never raises)
# --------------------------------------------------------------------------- #
_KEEP_ALIVE: list[object] = []      # ctypes callbacks / handles must not be collected


def _hold_mutex() -> bool:
    """Create the instance mutex; True when another launcher already holds it."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        already = ctypes.get_last_error() == 183          # ERROR_ALREADY_EXISTS
        _KEEP_ALIVE.append(handle)                        # held for the process's life
        return bool(handle) and already
    except Exception:  # noqa: BLE001 — cosmetic; never block the start
        return False


def _set_title(text: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except Exception:  # noqa: BLE001
            pass


def _disable_quick_edit() -> None:
    """In QuickEdit mode a click into the console starts a text selection, and
    while it lasts every write to the console blocks — the server's event
    loop included. This window is meant to be looked at, not selected in."""
    if os.name != "nt":
        return
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = ctypes.c_void_p
        k32.GetStdHandle.argtypes = [ctypes.c_uint32]
        k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        stdin = k32.GetStdHandle(0xFFFFFFF6)              # STD_INPUT_HANDLE (-10)
        mode = ctypes.c_uint32()
        if stdin and k32.GetConsoleMode(stdin, ctypes.byref(mode)):
            # clear ENABLE_QUICK_EDIT_MODE, set ENABLE_EXTENDED_FLAGS
            k32.SetConsoleMode(stdin, (mode.value & ~0x0040) | 0x0080)
    except Exception:  # noqa: BLE001 — no console (redirected), old Windows, …
        pass


def _stop_gracefully_on_close(server: object, stopped: threading.Event) -> None:
    """Closing the console window sends CTRL_CLOSE_EVENT and Windows kills the
    process about five seconds later. Use that time: ask uvicorn to stop and
    wait for the lifespan shutdown, which finalises an active recording."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        routine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def on_ctrl(ctrl_type: int) -> bool:
            if ctrl_type in (2, 5, 6):                    # CLOSE, LOGOFF, SHUTDOWN
                server.should_exit = True                 # type: ignore[attr-defined]
                stopped.wait(CLOSE_GRACE_S)
                return True
            return False                                  # Ctrl+C / Break: Python's own handling

        callback = routine(on_ctrl)
        _KEEP_ALIVE.append(callback)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
    except Exception:  # noqa: BLE001
        pass


def _pause_before_exit() -> None:
    """Keep a double-clicked console open long enough to read the error."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nEingabetaste zum Schließen … ")
        except (EOFError, KeyboardInterrupt):
            pass


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="netzsim.desktop",
        description="netzsim starten: Simulator und Oberfläche in einem Prozess.")
    ap.add_argument("--port", type=int, default=None,
                    help="erster Port, der probiert wird (Standard: NETZSIM_PORT oder 8000)")
    ap.add_argument("--no-browser", action="store_true",
                    help="den Browser nicht automatisch öffnen")
    args = ap.parse_args(argv)

    # A redirected stdout uses the ANSI code page; umlauts and dashes in the
    # banner must degrade to "?" there instead of raising UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    root = app_root()
    dist = Path(os.environ.get("NETZSIM_UI_DIST") or root / "ui" / "dist")
    if not (dist / "index.html").is_file():
        print(f"FEHLER: Die gebaute Oberfläche fehlt ({dist}).\n"
              "        Im Quellcode-Checkout zuerst bauen:  cd ui && npm run build",
              file=sys.stderr)
        return 2

    # Relative defaults (./data/..., .env) resolve against the app folder.
    os.chdir(root)
    env_file = dotenv_settings(root)

    def configured(name: str) -> str | None:
        return os.environ.get(name) or env_file.get(name) or None

    bind_host = configured("NETZSIM_HOST") or HOST
    base = args.port or int(configured("NETZSIM_PORT") or 8000)

    # Take the mutex BEFORE looking for a running instance: a second launcher
    # started during the first one's cold start must see it.
    other_launcher = _hold_mutex()
    port, reuse = pick_port(base, bind_host)
    waited = False
    if not reuse and other_launcher:
        print("netzsim startet bereits — warte auf die laufende Instanz …", flush=True)
        deadline = time.monotonic() + STARTING_WAIT_S
        while time.monotonic() < deadline:
            running = find_running(base)
            if running is not None:
                port, reuse, waited = running, True, True
                break
            time.sleep(1.0)
        else:
            port, reuse = pick_port(base, bind_host)      # it never came up: start our own
    url = f"http://{HOST}:{port}/"

    if reuse:
        print(f"netzsim läuft bereits — {url}")
        # the instance we waited for opens its own browser tab
        if not args.no_browser and not waited:
            webbrowser.open(url)
        return 0

    os.environ["NETZSIM_HOST"] = bind_host
    os.environ["NETZSIM_PORT"] = str(port)
    os.environ["NETZSIM_UI_DIST"] = str(dist)
    # The UI polls every 2 s: no per-request log lines in this window, unless
    # the environment or the app folder's .env asks for them. (An environment
    # variable would override .env, so only set it when neither says anything.)
    if configured("NETZSIM_ACCESS_LOG") is None:
        os.environ["NETZSIM_ACCESS_LOG"] = "false"
    _set_title(f"netzsim — {url}")
    _disable_quick_edit()

    from netzsim import __version__   # noqa: PLC0415 — after the env is set

    print("=" * 62)
    print(f"  netzsim {__version__} — Echtzeit-Netzsimulator")
    print("=" * 62)
    print(f"  Adresse:   {url}")
    if bind_host != HOST:
        print(f"  Im Netz:   erreichbar über {bind_host}:{port} (NETZSIM_HOST)")
    print(f"  Daten:     {root / 'data'}")
    print()
    print("  Dieses Fenster offen lassen — es IST der Simulator.")
    print("  Beenden:   Fenster schließen oder Strg+C.")
    print("  Der erste Start nach der Installation dauert bis zu einer Minute.")
    print("=" * 62, flush=True)

    from netzsim.main import STARTUP_FAILURE, build_server   # noqa: PLC0415

    # a bounded graceful shutdown: an open browser tab must not hold it up
    server = build_server(timeout_graceful_shutdown=3)
    stopped = threading.Event()
    _stop_gracefully_on_close(server, stopped)
    threading.Thread(target=_open_when_ready, args=(port, url, not args.no_browser),
                     daemon=True).start()
    try:
        server.run()
    finally:
        stopped.set()
    return 0 if server.started else STARTUP_FAILURE


def main() -> None:
    try:
        code = run()
    except KeyboardInterrupt:
        code = 0
    except SystemExit as exc:      # uvicorn exits this way when it cannot bind
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:  # noqa: BLE001 — show it instead of a vanishing window
        traceback.print_exc()
        code = 1
    if code:
        print("\nFEHLER: netzsim konnte nicht gestartet werden (siehe oben).",
              file=sys.stderr)
        _pause_before_exit()
    sys.exit(code)


if __name__ == "__main__":
    main()
