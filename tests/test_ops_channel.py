"""Tests for the OPS channel log handler + drain loop."""

from __future__ import annotations

import asyncio
import logging

import pytest

from codex_rc.ops_channel import OpsChannelLogHandler, drain_ops_channel


def test_ops_handler_enqueues_record() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    h = OpsChannelLogHandler(queue, level=logging.ERROR)
    record = logging.LogRecord(
        name="codex_rc.test",
        level=logging.ERROR,
        pathname="x.py",
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
        func="fn",
    )
    h.emit(record)
    assert queue.qsize() == 1
    msg = queue.get_nowait()
    assert "boom" in msg
    assert "ERROR" in msg


def test_ops_handler_skips_records_from_its_own_module() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    h = OpsChannelLogHandler(queue)
    record = logging.LogRecord(
        name="codex_rc.ops_channel",
        level=logging.ERROR,
        pathname="x.py",
        lineno=1,
        msg="recursion",
        args=(),
        exc_info=None,
        func="fn",
    )
    h.emit(record)
    assert queue.empty()


def test_ops_handler_drops_when_queue_full() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    h = OpsChannelLogHandler(queue)
    for _ in range(3):
        h.emit(logging.LogRecord(
            name="codex_rc.x", level=logging.ERROR, pathname="x.py", lineno=1,
            msg="m", args=(), exc_info=None, func="fn",
        ))
    assert queue.qsize() == 1  # only first one stuck


async def test_drain_posts_single_message() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    posts: list[tuple[str, dict]] = []

    async def post(channel_id, payload):
        posts.append((channel_id, payload))

    await queue.put("ERROR codex_rc.x — boom")
    task = asyncio.create_task(
        drain_ops_channel(queue, "777", post, poll_idle_s=0.01)
    )
    # Give the loop a tick to drain.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert posts, "drain should have posted at least once"
    channel_id, payload = posts[0]
    assert channel_id == "777"
    titles = [e.get("title", "") for e in payload.get("embeds", [])]
    assert any("error" in t.lower() for t in titles)


async def test_drain_coalesces_batch_into_one_embed() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    posts: list[tuple[str, dict]] = []

    async def post(channel_id, payload):
        posts.append((channel_id, payload))

    for i in range(4):
        await queue.put(f"err {i}")
    task = asyncio.create_task(
        drain_ops_channel(queue, "777", post, poll_idle_s=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Should be one batched post, not four.
    assert len(posts) == 1
    payload = posts[0][1]
    title = payload["embeds"][0]["title"]
    assert "(4)" in title
    desc = payload["embeds"][0]["description"]
    for i in range(4):
        assert f"err {i}" in desc


async def test_drain_survives_post_failure() -> None:
    """A post that raises must be swallowed so the drain task can keep
    running. We verify the second batch (added after the first failure)
    is still picked up."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    calls = 0

    async def flaky_post(channel_id, payload):
        nonlocal calls
        calls += 1
        raise RuntimeError("discord down")

    await queue.put("err1")
    task = asyncio.create_task(
        drain_ops_channel(queue, "777", flaky_post, poll_idle_s=0.01)
    )
    # Let drain process err1 (post raises, swallowed).
    await asyncio.sleep(0.05)
    assert calls == 1
    # Enqueue another batch — drain should still be alive.
    await queue.put("err2")
    await asyncio.sleep(0.05)
    assert calls == 2
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# silence pytest "no fixtures" lint
_ = pytest
