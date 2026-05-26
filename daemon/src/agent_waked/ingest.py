"""HTTP ingest stub — phase 1.

Binds an aiohttp application to the configured host/port.
``POST /`` returns ``501 Not Implemented`` until phase 2 wires in the
full ingest logic.
"""

import json
import logging

from aiohttp import web

log = logging.getLogger("agent_waked.ingest")


async def _post_root(request: web.Request) -> web.Response:
    return web.Response(
        status=501,
        content_type="application/json",
        body=json.dumps({"error": "not implemented"}).encode("utf-8"),
    )


async def _default(request: web.Request) -> web.Response:
    return web.Response(
        status=404,
        content_type="application/json",
        body=json.dumps({"error": "not found"}).encode("utf-8"),
    )


def create_ingest_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/", _post_root)
    app.router.add_route("*", "/{path:.*}", _default)
    return app
