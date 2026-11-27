"""Entry point: launch the uvicorn server hosting the realtime simulation."""
from __future__ import annotations

import logging
import sys
import warnings

import uvicorn

from .config import settings

STARTUP_FAILURE = 3   # the exit code uvicorn.run() uses when startup fails


class _OnceFilter(logging.Filter):
    """Let a log message that repeats on every solve through exactly once."""

    def __init__(self, needle: str) -> None:
        super().__init__()
        self.needle = needle
        self.seen = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self.needle in record.getMessage():
            if self.seen:
                return False
            self.seen = True
        return True


def configure() -> None:
    """Process-wide logging / warning setup (idempotent enough to call once)."""
    # pandapower <=3.4 estimation/results.py negates a bool with `~` — an
    # upstream bug that Python 3.12+ answers with a DeprecationWarning per
    # WLS run, flooding the console. Harmless for the results; silence it.
    warnings.filterwarnings(
        "ignore",
        message=r"Bitwise inversion '~' on bool is deprecated",
        category=DeprecationWarning,
    )
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Without numba (it is optional and not part of requirements.txt)
    # pandapower logs a four-line "numba cannot be imported" warning on EVERY
    # power flow — once per second in the live loop. Say it once.
    logging.getLogger("pandapower.auxiliary").addFilter(
        _OnceFilter("numba cannot be imported"))


def build_server(**overrides) -> uvicorn.Server:
    """The configured (not yet running) server.

    ``netzsim.desktop`` keeps the handle so it can ask for a graceful stop when
    the console window is closed; ``overrides`` are extra ``uvicorn.Config``
    arguments.
    """
    configure()
    options = dict(
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=settings.access_log,
    )
    options.update(overrides)
    return uvicorn.Server(uvicorn.Config("netzsim.api:app", **options))


def main() -> None:
    """Same contract as ``uvicorn.run()``, which this replaces."""
    server = build_server()
    try:
        server.run()
    except KeyboardInterrupt:
        # After its graceful stop Server.run() re-raises Ctrl+C; uvicorn.run()
        # swallows that, and so must we — no traceback on a normal exit.
        pass
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
