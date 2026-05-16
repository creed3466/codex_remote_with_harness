"""ApprovalRouter unit tests + integration with CodexRpcClient + a fake peer
that issues server-initiated approval requests."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

from codex_rc.approval_router import (
    STYLE_DANGER,
    STYLE_SUCCESS,
    ApprovalRouter,
    UnknownApprovalMethod,
    apply_resolution,
    is_approval_method,
    parse_custom_id,
)
from codex_rc.rpc_client import CodexRpcClient

# ====================================================================== custom_id

def test_parse_custom_id_ours() -> None:
    assert parse_custom_id("codex_rc:deadbeef:accept") == ("deadbeef", "accept")


def test_parse_custom_id_foreign() -> None:
    assert parse_custom_id("some_other_thing:abc:xyz") is None
    assert parse_custom_id("malformed") is None
    assert parse_custom_id("codex_rc:only_two") is None


# ====================================================================== prompts

@pytest.mark.parametrize(
    "method,expected_buttons",
    [
        ("item/commandExecution/requestApproval", 4),
        ("item/fileChange/requestApproval", 4),
        ("item/permissions/requestApproval", 2),
        ("applyPatchApproval", 4),
        ("execCommandApproval", 4),
    ],
)
def test_start_produces_prompt_with_buttons(method: str, expected_buttons: int) -> None:
    r = ApprovalRouter()
    prompt = r.start(method, {"reason": "test"}, request_id=42)
    assert prompt.prompt_id and len(prompt.prompt_id) == 8
    assert len(prompt.buttons) == expected_buttons
    payload = prompt.to_dict()
    assert payload["components"][0]["type"] == 1  # action row
    assert all(c["type"] == 2 for c in payload["components"][0]["components"])
    assert r.pending_count() == 1


def test_start_unknown_method_raises() -> None:
    r = ApprovalRouter()
    with pytest.raises(UnknownApprovalMethod):
        r.start("not/a/real/method", {}, request_id=1)


def test_is_approval_method_table() -> None:
    assert is_approval_method("item/commandExecution/requestApproval")
    assert is_approval_method("applyPatchApproval")
    assert not is_approval_method("turn/completed")


def test_summary_includes_command_and_cwd() -> None:
    r = ApprovalRouter()
    p = r.start(
        "item/commandExecution/requestApproval",
        {"command": "pytest -q", "cwd": "/tmp/proj", "reason": "verify"},
        request_id=1,
    )
    desc = p.embed.description or ""
    assert "pytest -q" in desc
    assert "/tmp/proj" in desc
    assert "verify" in desc


def test_command_approval_summary_truncates_long_command() -> None:
    r = ApprovalRouter()
    long_command = "python -c " + "x" * 1200
    p = r.start(
        "item/commandExecution/requestApproval",
        {"command": long_command, "cwd": "/tmp/proj"},
        request_id=1,
    )
    desc = p.embed.description or ""

    assert "python -c" in desc
    assert "command truncated" in desc
    assert len(desc) < 700


def test_button_styles_are_success_and_danger() -> None:
    r = ApprovalRouter()
    p = r.start("applyPatchApproval", {"fileChanges": {"foo.py": "x"}}, request_id=1)
    styles = [b.style for b in p.buttons]
    assert STYLE_SUCCESS in styles
    assert STYLE_DANGER in styles


# ====================================================================== resolve

def _start_and_click(method: str, params: dict, click_token: str) -> dict | None:
    r = ApprovalRouter()
    p = r.start(method, params, request_id=99)
    button = next(b for b in p.buttons if b.custom_id.endswith(f":{click_token}"))
    res = r.resolve(button.custom_id)
    if res is None:
        return None
    return {
        "request_id": res.request_id,
        "method": res.method,
        "result": res.result,
        "decision_token": res.decision_token,
    }, r.pending_count()  # type: ignore[return-value]


def test_resolve_command_accept() -> None:
    out, remaining = _start_and_click(
        "item/commandExecution/requestApproval",
        {"command": "ls"},
        "accept",
    )
    assert out["result"] == {"decision": "accept"}
    assert out["decision_token"] == "accept"
    assert remaining == 0


def test_resolve_command_accept_session() -> None:
    out, _ = _start_and_click(
        "item/commandExecution/requestApproval",
        {"command": "ls"},
        "accept_session",
    )
    assert out["result"] == {"decision": "acceptForSession"}


def test_resolve_file_change_decline() -> None:
    out, _ = _start_and_click(
        "item/fileChange/requestApproval",
        {"itemId": "i1"},
        "decline",
    )
    assert out["result"] == {"decision": "decline"}


def test_resolve_permissions_grant_passes_through_profile() -> None:
    profile = {"fileSystem": {"foo": True}, "network": None}
    out, _ = _start_and_click(
        "item/permissions/requestApproval",
        {"permissions": profile, "cwd": "/x"},
        "grant",
    )
    assert out["result"] == {"permissions": profile}


def test_resolve_permissions_deny_returns_empty_profile() -> None:
    out, _ = _start_and_click(
        "item/permissions/requestApproval",
        {"permissions": {"fileSystem": {"all": True}}, "cwd": "/x"},
        "deny",
    )
    assert out["result"] == {"permissions": {"fileSystem": {}, "network": {}}}


def test_resolve_v1_apply_patch_approved() -> None:
    out, _ = _start_and_click(
        "applyPatchApproval",
        {"fileChanges": {"a.py": "edit"}, "callId": "c", "conversationId": "t"},
        "approved",
    )
    assert out["result"] == {"decision": "approved"}


def test_resolve_v1_exec_denied() -> None:
    out, _ = _start_and_click(
        "execCommandApproval",
        {"command": ["ls"], "cwd": "/", "parsedCmd": [], "callId": "c",
         "conversationId": "t"},
        "denied",
    )
    assert out["result"] == {"decision": "denied"}


def test_resolve_unknown_prompt_id_returns_none() -> None:
    r = ApprovalRouter()
    assert r.resolve("codex_rc:00000000:accept") is None


def test_resolve_foreign_custom_id_returns_none() -> None:
    r = ApprovalRouter()
    assert r.resolve("not_ours:abc:accept") is None


def test_resolve_invalid_token_keeps_prompt_pending() -> None:
    r = ApprovalRouter()
    p = r.start("item/fileChange/requestApproval", {"itemId": "i"}, request_id=1)
    res = r.resolve(f"codex_rc:{p.prompt_id}:nonsense")
    assert res is not None and res.is_error()
    assert r.pending_count() == 1  # still pending — user can re-click


# ====================================================================== sweep

def test_sweep_auto_denies_expired() -> None:
    r = ApprovalRouter(timeout_s=10.0)
    p1 = r.start("item/fileChange/requestApproval", {"itemId": "i1"},
                 request_id=1, now=1000.0)
    p2 = r.start("item/permissions/requestApproval",
                 {"permissions": {}, "cwd": "/"}, request_id=2, now=1000.0)
    # not yet expired
    assert r.sweep(now=1005.0) == []
    expired = r.sweep(now=1100.0)
    assert {r_.method for r_ in expired} == {
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }
    # filechange auto-deny = decline; permissions auto-deny = empty profile
    by_method = {r_.method: r_ for r_ in expired}
    assert by_method["item/fileChange/requestApproval"].result == {"decision": "decline"}
    assert by_method["item/permissions/requestApproval"].result == {
        "permissions": {"fileSystem": {}, "network": {}}
    }
    assert r.pending_count() == 0
    # touch p1, p2 to silence unused warnings
    assert p1.prompt_id and p2.prompt_id


def test_cancel_all_drains_pending() -> None:
    r = ApprovalRouter()
    r.start("execCommandApproval",
            {"command": ["x"], "cwd": "/", "parsedCmd": [], "callId": "c",
             "conversationId": "t"},
            request_id=7)
    r.start("applyPatchApproval",
            {"fileChanges": {"f": "x"}, "callId": "c", "conversationId": "t"},
            request_id=8)
    drained = r.cancel_all(reason="test")
    assert {x.method for x in drained} == {"execCommandApproval", "applyPatchApproval"}
    assert all(x.result == {"decision": "denied"} for x in drained)
    assert r.pending_count() == 0


# ====================================================================== handler integration

@pytest.fixture
def approval_fake_server(tmp_path: Path) -> Path:
    """Fake JSON-RPC peer that, after initialize, fires a server-initiated
    approval request and waits for our response (echoing it back as a
    notification so the test can verify what we sent)."""
    script = tmp_path / "approval_fake.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading

            def send(m):
                sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()

            init_done = threading.Event()
            response_seen = threading.Event()
            last_response = []

            def reader():
                for line in sys.stdin:
                    msg = json.loads(line.strip())
                    if msg.get("method") == "initialize":
                        send({"jsonrpc":"2.0","id":msg["id"],
                              "result":{"userAgent":"approval-fake","codexHome":"/",
                                        "platformFamily":"unix","platformOs":"macos"}})
                        # fire approval request
                        send({"jsonrpc":"2.0","id":501,
                              "method":"item/commandExecution/requestApproval",
                              "params":{"command":"pytest","cwd":"/proj",
                                        "reason":"verify","itemId":"i1",
                                        "startedAtMs":0,"threadId":"t","turnId":"u"}})
                    elif msg.get("id") == 501:
                        # echo back as notification so the client can observe
                        send({"jsonrpc":"2.0","method":"test/approval/echo",
                              "params":{"received": msg}})
            reader()
            """
        ).strip()
        + "\n"
    )
    return script


async def test_handler_defers_and_apply_resolution_sends_decision(
    approval_fake_server: Path,
) -> None:
    router = ApprovalRouter()
    captured_prompt: list = []

    async def post_prompt(prompt, raw):
        captured_prompt.append((prompt, raw))

    client = CodexRpcClient(
        codex_bin=sys.executable,
        args=(str(approval_fake_server),),
        env=dict(os.environ),
    )
    client.server_request_handler = router.as_server_request_handler(post_prompt)
    await client.start()
    try:
        # Wait for the handler to be invoked + prompt captured.
        for _ in range(50):
            if captured_prompt:
                break
            await asyncio.sleep(0.02)
        assert captured_prompt, "server_request_handler was never invoked"
        prompt, raw = captured_prompt[0]
        assert raw["method"] == "item/commandExecution/requestApproval"
        assert router.pending_count() == 1

        # User "clicks" the Accept button.
        accept_button = next(
            b for b in prompt.buttons if b.custom_id.endswith(":accept")
        )
        resolution = router.resolve(accept_button.custom_id)
        assert resolution is not None
        await apply_resolution(client, resolution)

        # The fake peer echoes our response as a notification we can capture.
        async def collect_echo():
            async for notif in client.notifications():
                if notif.get("method") == "test/approval/echo":
                    return notif

        echo = await asyncio.wait_for(collect_echo(), timeout=5.0)
        received = echo["params"]["received"]
        assert received["id"] == 501
        assert received["result"] == {"decision": "accept"}
        assert router.pending_count() == 0
    finally:
        await client.stop()
