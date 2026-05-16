"""End-to-end against the real `codex app-server`.

Skipped automatically when the codex CLI is not installed or the user is not
logged in — these tests need a live ChatGPT/API session to fully exercise
threads, but the ``initialize`` handshake and the immediate
``remoteControl/status/changed`` notification run without auth.

Run explicitly with::

    pytest -m integration tests/test_integration_codex.py
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from codex_rc.rpc_client import CodexRpcClient

pytestmark = pytest.mark.integration

CODEX = shutil.which("codex")
codex_required = pytest.mark.skipif(CODEX is None, reason="codex CLI not on PATH")


@codex_required
async def test_initialize_against_real_app_server() -> None:
    client = CodexRpcClient(codex_bin="codex", args=("app-server",))
    try:
        result = await client.start()
    finally:
        await client.stop()

    # The server's initialize response includes these fields per codex-cli 0.130.0.
    for key in ("userAgent", "codexHome", "platformFamily", "platformOs"):
        assert key in result, f"initialize result missing {key!r}: {result}"
    assert "codex_rc" in result["userAgent"], result["userAgent"]


@codex_required
async def test_remote_control_status_notification_arrives() -> None:
    client = CodexRpcClient(codex_bin="codex", args=("app-server",))
    await client.start()
    try:
        # First notification post-initialize is `remoteControl/status/changed`.
        async def first_notif() -> dict:
            async for n in client.notifications():
                return n
            raise AssertionError("notification stream ended before first message")

        notif = await asyncio.wait_for(first_notif(), timeout=5.0)
        assert notif["method"] == "remoteControl/status/changed"
        params = notif.get("params", {})
        assert "status" in params
    finally:
        await client.stop()
