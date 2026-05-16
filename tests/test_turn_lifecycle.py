"""Tests for the v0.2 turn-lifecycle features added on top of ChannelService:

- in-flight message → turn/steer (T1.1)
- cancel_active_turn → turn/interrupt (T1.2)
- auto-compact at token threshold (T1.3)
- usage footer + owner mention on turn/completed (T1.4, T2.11)
- auto-approve (T2.10)
- rollback (T3.12)
- on_turn_stage callback fires for started/file_change/completed (T2.8)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_rc.approval_router import (
    STYLE_DANGER,
    STYLE_SUCCESS,
    DiscordButton,
    DiscordPrompt,
)
from codex_rc.notif_router import DiscordEmbed
from codex_rc.session_store import SessionStore
from tests.test_channel_service import FakeProc, make_channel_service

# ============================================================ fixtures

@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


@pytest.fixture
def fake_proc(tmp_path: Path) -> FakeProc:
    proj = tmp_path / "project"
    proj.mkdir()
    return FakeProc(project_path=proj, pretty_path=tmp_path / "events.txt")


@pytest.fixture
def post_calls() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def post(post_calls):
    async def _post(channel_id: str, payload: dict) -> None:
        post_calls.append((channel_id, payload))
    return _post


# ============================================================ T1.1 in-flight steer

async def test_send_text_uses_turn_start_when_no_active_turn(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}

    await cs.send_text("hello")
    method = fake_proc.request.call_args.args[0]
    assert method == "turn/start"


async def test_send_text_uses_turn_steer_when_turn_is_active(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._active_turn_id = "u-42"
    fake_proc.request.return_value = {}

    await cs.send_text("more info")
    method, params = fake_proc.request.call_args.args
    assert method == "turn/steer"
    assert params["threadId"] == "thread-1"
    assert params["expectedTurnId"] == "u-42"
    assert params["input"] == [{"type": "text", "text": "more info"}]


async def test_send_text_steer_does_not_increment_turn_count(store, fake_proc, post) -> None:
    """An in-flight steer is part of the same turn — store turn_count must
    NOT advance again."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    store.record_thread(
        codex_thread_id="thread-1",
        channel_id="C1",
        project_path=str(fake_proc.project_path),
    )
    store.touch_thread("thread-1", turn_increment=1)  # one turn already
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._active_turn_id = "u-1"
    fake_proc.request.return_value = {}

    await cs.send_text("steer1")
    await cs.send_text("steer2")
    threads = store.list_threads("C1")
    assert threads[0].turn_count == 1  # still one


# ============================================================ T1.1 turn state updates

async def test_update_turn_state_sets_active_id_on_started(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    await cs._update_turn_state({
        "method": "turn/started",
        "params": {"turn": {"id": "u-99"}},
    })
    assert cs._active_turn_id == "u-99"


async def test_update_turn_state_clears_active_id_on_completed(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._active_turn_id = "u-99"
    await cs._update_turn_state({"method": "turn/completed", "params": {}})
    assert cs._active_turn_id is None


async def test_on_turn_stage_callback_fires_for_started_and_completed(
    store, fake_proc, post
) -> None:
    calls: list[tuple[str, str]] = []

    async def cb(channel_id, stage, _params):
        calls.append((channel_id, stage))

    cs = make_channel_service(store=store, proc=fake_proc, post=post, on_turn_stage=cb)
    await cs._update_turn_state({
        "method": "turn/started",
        "params": {"turn": {"id": "u-1"}},
    })
    await cs._update_turn_state({"method": "turn/completed", "params": {}})
    assert calls == [("C1", "started"), ("C1", "completed")]


async def test_on_turn_stage_callback_fires_once_for_file_change(
    store, fake_proc, post
) -> None:
    """file_change is per-turn idempotent — first item/fileChange triggers
    it, subsequent ones in the same turn do not."""
    calls: list[str] = []

    async def cb(channel_id, stage, _params):
        calls.append(stage)

    cs = make_channel_service(store=store, proc=fake_proc, post=post, on_turn_stage=cb)
    await cs._update_turn_state({"method": "turn/started", "params": {"turn": {"id": "u-1"}}})
    await cs._update_turn_state({
        "method": "item/started",
        "params": {"item": {"type": "fileChange"}},
    })
    await cs._update_turn_state({
        "method": "item/started",
        "params": {"item": {"type": "fileChange"}},
    })
    assert calls.count("file_change") == 1


# ============================================================ T1.2 cancel

async def test_cancel_active_turn_returns_false_when_no_turn(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    assert await cs.cancel_active_turn() is False
    fake_proc.request.assert_not_called()


async def test_cancel_active_turn_calls_turn_interrupt(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._active_turn_id = "u-1"
    fake_proc.request.return_value = {}

    assert await cs.cancel_active_turn() is True
    method, params = fake_proc.request.call_args.args
    assert method == "turn/interrupt"
    assert params == {"threadId": "thread-1", "turnId": "u-1"}


# ============================================================ T1.3 auto-compact

async def test_auto_compact_below_threshold_is_noop(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        auto_compact_threshold_tokens=100_000,
    )
    await cs._maybe_auto_compact({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"inputTokens": 50_000}},
    })
    fake_proc.request.assert_not_called()


async def test_auto_compact_disabled_when_threshold_is_zero(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        auto_compact_threshold_tokens=0,
    )
    await cs._maybe_auto_compact({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"inputTokens": 9_999_999}},
    })
    fake_proc.request.assert_not_called()


async def test_auto_compact_above_threshold_fires_compact_rpc(
    store, fake_proc, post, post_calls
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        auto_compact_threshold_tokens=100_000,
    )
    fake_proc.request.return_value = {}
    await cs._maybe_auto_compact({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"inputTokens": 150_000}},
    })
    methods = [c.args[0] for c in fake_proc.request.call_args_list]
    assert "thread/compact/start" in methods
    titles = [
        e.get("title", "")
        for _, p in post_calls
        for e in p.get("embeds", [])
    ]
    assert any("Compacting" in t for t in titles)


async def test_auto_compact_is_idempotent_while_running(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        auto_compact_threshold_tokens=100_000,
    )
    cs._compacting = True
    await cs._maybe_auto_compact({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"inputTokens": 200_000}},
    })
    fake_proc.request.assert_not_called()


# ============================================================ T1.4 usage footer + T2.11 mention

async def test_usage_footer_appends_token_count(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._last_token_usage = {"tokenUsage": {"totalTokens": 12345}}
    from codex_rc.notif_router import DiscordPayload
    payload = DiscordPayload(embed=DiscordEmbed(title="✅ Done", description="ok"))
    out = cs._enrich_payload({"method": "turn/completed"}, payload)
    assert "💎" in (out.embed.footer_text or "")
    assert "12,345" in (out.embed.footer_text or "")


async def test_usage_footer_reads_nested_total_tokens(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._last_token_usage = {
        "tokenUsage": {
            "last": {"totalTokens": 10},
            "total": {"inputTokens": 12000, "totalTokens": 12345},
        }
    }
    from codex_rc.notif_router import DiscordPayload
    payload = DiscordPayload(embed=DiscordEmbed(title="✅ Done", description="ok"))
    out = cs._enrich_payload({"method": "turn/completed"}, payload)
    assert "💎" in (out.embed.footer_text or "")
    assert "12,345" in (out.embed.footer_text or "")


async def test_usage_footer_skipped_for_non_completed_notifs(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._last_token_usage = {"tokenUsage": {"totalTokens": 12345}}
    from codex_rc.notif_router import DiscordPayload
    payload = DiscordPayload(embed=DiscordEmbed(title="thinking"))
    out = cs._enrich_payload({"method": "turn/started"}, payload)
    assert out.embed.footer_text is None


async def test_owner_mention_prefixes_completed_payload(store, fake_proc, post) -> None:
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        owner_mention_id="111111111111111111",
    )
    from codex_rc.notif_router import DiscordPayload
    payload = DiscordPayload(content="done!", embed=None)
    out = cs._enrich_payload({"method": "turn/completed"}, payload)
    assert out.content is not None
    assert "<@111111111111111111>" in out.content
    assert "done!" in out.content


async def test_owner_mention_skipped_when_unset(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    from codex_rc.notif_router import DiscordPayload
    payload = DiscordPayload(content="done!")
    out = cs._enrich_payload({"method": "turn/completed"}, payload)
    assert out.content == "done!"


# ============================================================ T2.10 auto-approve

async def test_auto_approve_resolves_success_button_without_posting(
    store, fake_proc, post, post_calls
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post, auto_approve=True,
    )
    # Register a pending entry the prompt id refers to.
    cs.approvals._pending["abcd"] = MagicMock(  # type: ignore[arg-type]
        prompt_id="abcd",
        request_id=99,
        method="item/commandExecution/requestApproval",
        started_at=0.0,
        params={},
    )
    # Patch resolve to return a fake resolution so apply_resolution runs.
    fake_resolution = MagicMock(
        request_id=99,
        result={"decision": "approved"},
        error=None,
    )
    cs.approvals.resolve = MagicMock(return_value=fake_resolution)
    prompt = DiscordPrompt(
        prompt_id="abcd",
        embed=DiscordEmbed(title="approve?"),
        buttons=[
            DiscordButton(label="Deny", style=STYLE_DANGER, custom_id="codex_rc:abcd:reject"),
            DiscordButton(label="Approve", style=STYLE_SUCCESS, custom_id="codex_rc:abcd:accept"),
        ],
    )
    fake_proc.client.respond_to_request = AsyncMock()

    await cs._on_approval_prompt(prompt, {})
    cs.approvals.resolve.assert_called_once_with("codex_rc:abcd:accept")
    fake_proc.client.respond_to_request.assert_awaited_once()
    # No Discord prompt was posted.
    titles = [
        e.get("title", "")
        for _, p in post_calls
        for e in p.get("embeds", [])
    ]
    assert not any("approve" in t.lower() for t in titles)


async def test_auto_approve_falls_back_to_manual_when_no_success_button(
    store, fake_proc, post, post_calls
) -> None:
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post, auto_approve=True,
    )
    prompt = DiscordPrompt(
        prompt_id="defg",
        embed=DiscordEmbed(title="custom decision"),
        buttons=[
            DiscordButton(label="A", style=STYLE_DANGER, custom_id="codex_rc:defg:a"),
        ],
    )
    await cs._on_approval_prompt(prompt, {})
    # Falls through to posting the prompt for a human.
    assert post_calls, "fallback should have posted the prompt"


# ============================================================ T3.12 rollback

async def test_rollback_calls_thread_rollback_rpc(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}

    assert await cs.rollback(count=3) == 3
    method, params = fake_proc.request.call_args.args
    assert method == "thread/rollback"
    assert params == {"threadId": "thread-1", "numTurns": 3}


async def test_rollback_raises_when_no_thread(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    with pytest.raises(RuntimeError, match="no thread"):
        await cs.rollback()


async def test_rollback_rejects_zero_count(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    with pytest.raises(ValueError):
        await cs.rollback(count=0)


# silence linter — Any/AsyncMock are kept as documentation imports
_ = Any
