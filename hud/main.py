"""Run the standalone AdventedHUD aiohttp server."""

from __future__ import annotations

import logging
import os

from aiohttp import web

from hud.server import create_app

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HUD_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("HUD_HOST", "127.0.0.1")
    port = int(os.environ.get("HUD_PORT", "8200"))
    app = create_app()
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
