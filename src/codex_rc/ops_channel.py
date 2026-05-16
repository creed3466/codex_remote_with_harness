"""Forward ERROR / EXCEPTION log records to a dedicated Discord channel.

When ``CODEX_RC_OPS_CHANNEL_ID`` is set, every ``logging.ERROR`` (or higher)
record from the ``codex_rc`` namespace is enqueued and posted to that
channel as an embed. Failures inside the drain itself never re-enter the
logging pipeline (would loop).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


PostCallable = Callable[[str, dict], Awaitable[None]]


class OpsChannelLogHandler(logging.Handler):
    """Non-blocking handler that pushes formatted records into a queue.

    The actual Discord send happens in :func:`drain_ops_channel`, so the
    handler stays safe to call from any thread / any moment in the loop.
    """

    def __init__(self, queue: asyncio.Queue[str], level: int = logging.ERROR) -> None:
        super().__init__(level=level)
        self.queue = queue
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s "
                "%(funcName)s:%(lineno)d — %(message)s"
            )
        )

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover — IO path
        # Skip records emitted by our own draining task to avoid infinite loop.
        if record.name == __name__:
            return
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 — formatter bug; don't crash logging
            return
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Better to drop a record than block the producer.
            pass


async def drain_ops_channel(
    queue: asyncio.Queue[str],
    channel_id: str,
    post: PostCallable,
    *,
    poll_idle_s: float = 0.5,
) -> None:
    """Consume the queue forever, posting each batch as one embed.

    We coalesce up to 5 records per send so a burst of stack traces doesn't
    fan out into 50 channel messages.
    """
    while True:
        first = await queue.get()
        batch = [first]
        # Drain a few more if they're already waiting.
        for _ in range(4):
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        body = "\n".join(batch)
        if len(body) > 3800:
            body = body[:3700] + "\n... (truncated)"
        payload = {
            "embeds": [
                {
                    "title": (
                        "🚨 codex_rc error"
                        if len(batch) == 1
                        else f"🚨 codex_rc errors ({len(batch)})"
                    ),
                    "description": f"```\n{body}\n```",
                    "color": 0xED4245,
                }
            ]
        }
        try:
            await post(channel_id, payload)
        except Exception:
            # Don't recurse into logging.exception — handler would re-enqueue
            # and the loop would never drain.
            pass
        # Small breather so a hammered queue doesn't hot-loop.
        if queue.empty():
            await asyncio.sleep(poll_idle_s)


def install_ops_handler(
    queue: asyncio.Queue[str], level: int = logging.ERROR
) -> OpsChannelLogHandler:
    """Attach an :class:`OpsChannelLogHandler` to the ``codex_rc`` logger
    namespace and return it (so callers can later remove it on teardown).
    """
    handler = OpsChannelLogHandler(queue, level=level)
    codex_rc_logger = logging.getLogger("codex_rc")
    codex_rc_logger.addHandler(handler)
    return handler


__all__ = ["OpsChannelLogHandler", "drain_ops_channel", "install_ops_handler"]
