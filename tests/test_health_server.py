"""Tests for the stdlib /healthz HTTP endpoint."""

from __future__ import annotations

import asyncio
import json

import pytest

from codex_rc.health_server import serve_health


async def _http_get(host: str, port: int, *, path: str = "/healthz") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        # Status line
        status_line = await reader.readline()
        status_parts = status_line.split(b" ", 2)
        status = int(status_parts[1]) if len(status_parts) > 1 else 0
        # Drain headers
        content_length = 0
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        body = await reader.readexactly(content_length) if content_length else b""
        return status, body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def test_serve_health_returns_200_json_with_snapshot() -> None:
    snapshot_calls: list[int] = []

    def snap() -> dict:
        snapshot_calls.append(1)
        return {"version": "9.9.9", "active_channels": 2, "transport": "ws"}

    server = await serve_health("127.0.0.1", 0, snapshot=snap)
    try:
        port = server.sockets[0].getsockname()[1]
        status, body = await _http_get("127.0.0.1", port)
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        assert data["version"] == "9.9.9"
        assert data["active_channels"] == 2
        assert data["transport"] == "ws"
        assert isinstance(data["uptime_s"], int)
        assert snapshot_calls, "snapshot provider should have been invoked"
    finally:
        server.close()
        await server.wait_closed()


async def test_serve_health_handles_multiple_probes() -> None:
    def snap() -> dict:
        return {"version": "0.2.0"}

    server = await serve_health("127.0.0.1", 0, snapshot=snap)
    try:
        port = server.sockets[0].getsockname()[1]
        for _ in range(5):
            status, body = await _http_get("127.0.0.1", port)
            assert status == 200
            assert json.loads(body)["ok"] is True
    finally:
        server.close()
        await server.wait_closed()


async def test_serve_health_survives_snapshot_raising() -> None:
    def boom() -> dict:
        raise RuntimeError("kaboom")

    server = await serve_health("127.0.0.1", 0, snapshot=boom)
    try:
        port = server.sockets[0].getsockname()[1]
        status, body = await _http_get("127.0.0.1", port)
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True  # we still report 200; snapshot extras absent
        assert "uptime_s" in data
    finally:
        server.close()
        await server.wait_closed()


async def test_serve_health_accepts_any_path() -> None:
    """Probes that hit /, /healthz, /status/probe etc. all see 200."""
    server = await serve_health("127.0.0.1", 0, snapshot=lambda: {"x": 1})
    try:
        port = server.sockets[0].getsockname()[1]
        for path in ("/", "/healthz", "/status", "/anything"):
            status, _body = await _http_get("127.0.0.1", port, path=path)
            assert status == 200
    finally:
        server.close()
        await server.wait_closed()


_ = pytest
