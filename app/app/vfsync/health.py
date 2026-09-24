"""Health endpoint of the VF-Sync worker (R-12).

`GET /healthz` returns the same snapshot that `main.py` writes to the
Redis hash `vfsync:health`; 503 when the worker is not healthy (feed
dead for more than 30 minutes while tenants are enabled, or not running).
Served by uvicorn inside the worker process on VFSYNC_HEALTH_PORT.
"""

import asyncio
from datetime import datetime
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.vfsync.alerts import FEED_DEAD_AFTER

SnapshotFn = Callable[[], dict[str, Any]]


def is_healthy(snapshot: dict[str, Any], now: datetime | None = None) -> bool:
    """Health rule over a snapshot dict (as in vfsync:health).

    Unhealthy when the worker is not running, or - with tenants enabled -
    the APRS worker has been disconnected from OGN for more than 30 min.
    The absence of flight events is *not* a fault (no flying, no events).
    """
    if snapshot.get("status") not in ("ok", "starting"):
        return False
    tenants = snapshot.get("tenants") or []
    if not tenants:
        return True  # nothing enabled: idle but fine
    if snapshot.get("aprs_connected") is False:
        down_s = float(snapshot.get("aprs_down_for_s") or 0)
        if down_s > FEED_DEAD_AFTER.total_seconds():
            return False
    return True


def build_health_app(snapshot: SnapshotFn) -> FastAPI:
    """FastAPI app exposing /healthz from a snapshot callable."""
    app = FastAPI(title="vfsync-health", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        snap = snapshot()
        healthy = is_healthy(snap)
        body = {**_jsonable(snap), "healthy": healthy}
        return JSONResponse(body, status_code=200 if healthy else 503)

    return app


async def serve_health(app: FastAPI, port: int, shutdown: asyncio.Event) -> None:
    """Run uvicorn in-process until `shutdown` is set."""
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning",
                            lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="vfsync-health-http")
    waiter = asyncio.create_task(shutdown.wait(), name="vfsync-health-shutdown")
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            # Server ended before shutdown: bind error etc. - surface it.
            exc = task.exception()
            raise RuntimeError(f"health server stopped unexpectedly: {exc}")
    finally:
        waiter.cancel()
        server.should_exit = True
        if not task.done():
            await task


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _jsonable(v)
        else:
            out[k] = v
    return out
