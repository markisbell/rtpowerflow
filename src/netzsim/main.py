"""Entry point: launch the uvicorn server hosting the realtime simulation."""
from __future__ import annotations

import logging
import warnings

import uvicorn

from .config import settings


def main() -> None:
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
    uvicorn.run(
        "netzsim.api:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
