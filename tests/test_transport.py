"""Unit tests for the Transport abstraction.

StdioTransport is covered via an echo Python peer. WebSocketTransport
runs against a tiny in-memory ``websockets`` server fixture.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from codex_rc.rpc_client import StdioTransport, WebSocketTransport

# ====================================================================== stdio

@pytest.fixture
def echo_peer(tmp_path: Path) -> Path:
    script = tmp_path / "echo_peer.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            # Echo each line back prefixed with 'echo:'.
            for line in sys.stdin:
                line = line.rstrip("\\n")
                sys.stdout.write(f"echo:{line}\\n")
                sys.stdout.flush()
            """
        ).strip()
        + "\n"
    )
    return script


async def test_stdio_transport_lifecycle(echo_peer: Path) -> None:
    t = StdioTransport(codex_bin=sys.executable, args=(str(echo_peer),))
    await t.start()
    try:
        await t.send_message(b"hello")
        line = await t.read_message()
        assert line and line.strip() == b"echo:hello"
    finally:
        await t.stop()


async def test_stdio_transport_read_returns_none_on_eof(tmp_path: Path) -> None:
    # Peer that exits immediately after a single line.
    script = tmp_path / "oneshot.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            sys.stdout.write("hi\\n")
            sys.stdout.flush()
            """
        ).strip()
        + "\n"
    )
    t = StdioTransport(codex_bin=sys.executable, args=(str(script),))
    await t.start()
    try:
        first = await t.read_message()
        assert first and first.strip() == b"hi"
        # After EOF, read_message returns None.
        second = await t.read_message()
        assert second is None
    finally:
        await t.stop()


async def test_stdio_transport_stop_terminates_busy_peer(tmp_path: Path) -> None:
    busy = tmp_path / "busy.py"
    busy.write_text(
        textwrap.dedent(
            """
            import time
            while True:
                time.sleep(1)
            """
        ).strip()
        + "\n"
    )
    t = StdioTransport(codex_bin=sys.executable, args=(str(busy),))
    await t.start()
    await t.stop(timeout=2.0)


# ====================================================================== websocket

async def test_websocket_transport_against_in_memory_server() -> None:
    import websockets

    received: list[str] = []

    async def handler(ws):
        async for message in ws:
            received.append(message)
            await ws.send(f"echo:{message}")

    server = await websockets.serve(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    url = f"ws://{host}:{port}"
    try:
        t = WebSocketTransport(url)
        await t.start()
        try:
            await t.send_message(b"hello")
            reply = await asyncio.wait_for(t.read_message(), timeout=2.0)
            assert reply == b"echo:hello"
        finally:
            await t.stop()
    finally:
        server.close()
        await server.wait_closed()
    assert received == ["hello"]


async def test_websocket_transport_recv_after_close_returns_none() -> None:
    import websockets

    async def handler(ws):
        # Send one message then close.
        await ws.send("first")
        await ws.close()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        t = WebSocketTransport(f"ws://{host}:{port}")
        await t.start()
        first = await asyncio.wait_for(t.read_message(), timeout=2.0)
        assert first == b"first"
        second = await asyncio.wait_for(t.read_message(), timeout=2.0)
        assert second is None
        await t.stop()
    finally:
        server.close()
        await server.wait_closed()
