"""Tiny stdlib-only HTTP /healthz endpoint for codex_rc.

External uptime probes (UptimeRobot, Healthchecks.io, Grafana synthetic
checks, …) need to know whether the bot is alive without scraping
Discord. This module hosts a 30-line HTTP listener that accepts any GET
and replies with a single JSON line.

Why not aiohttp / FastAPI?
   We have exactly one endpoint and no auth surface. ``asyncio.start_server``
   is already in stdlib and runs on the same event loop as the bot, so we
   avoid a dependency for a feature that is otherwise inert.

Activate via:

    CODEX_RC_HEALTH_HOST=127.0.0.1   # default
    CODEX_RC_HEALTH_PORT=8765        # 0 / unset disables the endpoint
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

SnapshotProvider = Callable[[], dict[str, object]]
"""Callable returning a dict that is merged into the JSON response."""


def _build_response(body: bytes, *, status: str = "200 OK") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii") + body


async def _drain_request(reader: asyncio.StreamReader, timeout_s: float) -> None:
    """Consume request line + headers without parsing.

    Probe tools send well-formed but trivial requests; we accept any path.
    """
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
            if not line or line == b"\r\n":
                return
    except TimeoutError:
        return
    except Exception:  # noqa: BLE001 — never let the probe crash the listener
        return


def _build_payload(snapshot: SnapshotProvider, started_at: float) -> bytes:
    try:
        extras = snapshot() or {}
    except Exception:
        logger.exception("codex_rc: health snapshot provider raised")
        extras = {}
    payload: dict[str, object] = {
        "ok": True,
        "uptime_s": int(time.time() - started_at),
    }
    if isinstance(extras, dict):
        payload.update(extras)
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


async def serve_health(
    host: str,
    port: int,
    *,
    snapshot: SnapshotProvider,
    request_timeout_s: float = 2.0,
) -> asyncio.base_events.Server:
    """Start the HTTP listener and return the server handle.

    The caller is expected to ``server.close()`` + ``await
    server.wait_closed()`` on shutdown.
    """
    started_at = time.time()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await _drain_request(reader, request_timeout_s)
            body = _build_payload(snapshot, started_at)
            writer.write(_build_response(body))
            await writer.drain()
        except Exception:
            logger.debug("codex_rc: health connection error", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host, port)
    bound = server.sockets[0].getsockname() if server.sockets else (host, port)
    logger.info("codex_rc: health endpoint listening at %s:%d", bound[0], bound[1])
    return server


__all__ = ["SnapshotProvider", "serve_health"]
