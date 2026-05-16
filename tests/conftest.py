"""Shared fixtures.

Provides a fake JSON-RPC server peer that speaks NDJSON over a child process's
stdin/stdout — exactly the contract CodexRpcClient expects from the real
``codex app-server``. The fake is a tiny Python script we spawn via the same
``codex_bin``-style override.
"""

from __future__ import annotations

import os
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _disable_soul_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force operator instructions empty inside the test suite so real local
    config doesn't bleed into channel tests that assert exact RPC sequences.
    Tests that exercise the injection path opt in explicitly."""
    from codex_rc.service import OperatorInstructions

    monkeypatch.setattr(
        "codex_rc.service._load_operator_instructions",
        lambda: OperatorInstructions(),
        raising=True,
    )


@pytest.fixture
def fake_server_script(tmp_path: Path) -> Path:
    """Write a single-file fake JSON-RPC peer; return its path.

    The peer is driven by a JSON config in env var ``FAKE_RPC_CONFIG`` so each
    test can script its behavior (initialize result, notifications to emit,
    responses to specific methods, optional server-initiated requests).
    """
    script = tmp_path / "fake_app_server.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, os, sys, threading, time

            CONFIG = json.loads(os.environ.get("FAKE_RPC_CONFIG", "{}"))

            def send(msg):
                sys.stdout.write(json.dumps(msg) + "\\n")
                sys.stdout.flush()

            def reader():
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    method = msg.get("method")
                    req_id = msg.get("id")
                    if method == "initialize":
                        send({"jsonrpc": "2.0", "id": req_id,
                              "result": CONFIG.get("init_result", {"ok": True})})
                        # post-init: emit any scripted notifications
                        for n in CONFIG.get("post_init_notifications", []):
                            send({"jsonrpc": "2.0", **n})
                        # post-init: emit any scripted server-initiated requests
                        for sr in CONFIG.get("post_init_server_requests", []):
                            send({"jsonrpc": "2.0", **sr})
                    elif req_id is not None:
                        handlers = CONFIG.get("handlers", {})
                        spec = handlers.get(method)
                        if spec is None:
                            send({"jsonrpc": "2.0", "id": req_id,
                                  "error": {"code": -32601,
                                            "message": f"Method not found: {method}"}})
                        else:
                            if "error" in spec:
                                send({"jsonrpc": "2.0", "id": req_id,
                                      "error": spec["error"]})
                            else:
                                send({"jsonrpc": "2.0", "id": req_id,
                                      "result": spec.get("result", {})})

            reader()
            """
        ).strip()
        + "\n"
    )
    return script


@pytest.fixture
def make_client(fake_server_script: Path) -> Iterator[object]:
    """Return a factory that builds a CodexRpcClient bound to the fake peer."""
    from codex_rc.rpc_client import CodexRpcClient

    created = []

    def _make(config: dict | None = None, **kwargs) -> CodexRpcClient:
        env = dict(os.environ)
        if config is not None:
            env["FAKE_RPC_CONFIG"] = __import__("json").dumps(config)
        client = CodexRpcClient(
            codex_bin=sys.executable,
            args=(str(fake_server_script),),
            env=env,
            **kwargs,
        )
        created.append(client)
        return client

    yield _make

    # cleanup is per-test via the client itself; nothing else to do here.
