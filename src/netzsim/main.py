"""Entry point: launch the uvicorn server hosting the realtime simulation."""
from __future__ import annotations

import logging

import uvicorn

from .config import settings


def main() -> None:
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
