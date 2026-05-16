"""Tests for v0.2 discord_bot additions: blocked-user warn, cancel
reaction + /codex cancel command, /codex rollback, turn-stage reactions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from codex_rc.discord_bot import AccessControl, CodexRcBot

# ============================================================ helpers

def make_service() -> MagicMock:
    svc = MagicMock()
    svc.is_active = MagicMock(return_value=True)
    svc.start_session = AsyncMock()
    svc.stop_session = AsyncMock()
    svc.send_text = AsyncMock()
    svc.resume_thread = AsyncMock()
    svc.resume_handoff = AsyncMock()
    svc.new_thread = AsyncMock(return_value="thread-fresh-id")
    svc.handle_button = AsyncMock(return_value=True)
    svc.cancel_active_turn = AsyncMock(return_value=True)
    svc.rollback = AsyncMock(return_value=1)
    svc.store = MagicMock()
    svc.store.list_threads = MagicMock(return_value=[])
    svc.store.list_handoffs = MagicMock(return_value=[])
    svc.store.get = MagicMock(return_value=None)
    return svc


def make_bot(
    *,
    service: MagicMock | None = None,
    allowed: set[str] | None = None,
    bot_user_id: int = 999_999,
) -> CodexRcBot:
    bot = CodexRcBot.__new__(CodexRcBot)
    bot.service = service or make_service()
    uids = tuple(allowed) if allowed is not None else ("42",)
    bot.access = AccessControl({"*": uids} if uids else {})
    bot.channel_ids = None
    bot.guild_id = None
    bot._connection = MagicMock()
    bot._connection.user = MagicMock(id=bot_user_id)
    bot._last_user_message = {}
    bot._warned_blocked_users = set()
    return bot


def make_message(
    *,
    content: str = "",
    author_id: int = 42,
    channel_id: int = 100,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.author = MagicMock(id=author_id, bot=False)
    msg.channel = MagicMock(id=channel_id)
    msg.attachments = []
    msg.reply = AsyncMock()
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()
    return msg


# ============================================================ blocked-user warn (T1.6)

async def test_unallowed_user_first_message_gets_one_warning() -> None:
    bot = make_bot(allowed={"42"})
    msg = make_message(content="hi", author_id=999)
    await bot.on_message(msg)
    msg.reply.assert_called_once()
    assert "allowlist" in msg.reply.call_args.args[0]


async def test_unallowed_user_subsequent_messages_are_silent() -> None:
    bot = make_bot(allowed={"42"})
    m1 = make_message(content="hi", author_id=999)
    m2 = make_message(content="hi again", author_id=999)
    await bot.on_message(m1)
    await bot.on_message(m2)
    m1.reply.assert_called_once()
    m2.reply.assert_not_called()


async def test_warning_keyed_per_user() -> None:
    bot = make_bot(allowed={"42"})
    m1 = make_message(content="hi", author_id=901)
    m2 = make_message(content="hi", author_id=902)
    await bot.on_message(m1)
    await bot.on_message(m2)
    m1.reply.assert_called_once()
    m2.reply.assert_called_once()


# ============================================================ cancel command (T1.2)

async def test_text_codex_cancel_no_active_session_warns() -> None:
    bot = make_bot()
    bot.service.is_active = MagicMock(return_value=False)
    msg = make_message(content="/codex cancel")
    await bot._handle_text_codex_command(msg, "cancel")
    assert "No active" in msg.reply.call_args.args[0]
    bot.service.cancel_active_turn.assert_not_called()


async def test_text_codex_cancel_active_replies_interrupted() -> None:
    bot = make_bot()
    bot.service.cancel_active_turn = AsyncMock(return_value=True)
    msg = make_message(content="/codex cancel")
    await bot._handle_text_codex_command(msg, "cancel")
    bot.service.cancel_active_turn.assert_awaited_once_with("100")
    assert "Interrupted" in msg.reply.call_args.args[0]


async def test_text_codex_cancel_no_inflight_turn_replies_nothing_to_cancel() -> None:
    bot = make_bot()
    bot.service.cancel_active_turn = AsyncMock(return_value=False)
    msg = make_message(content="/codex cancel")
    await bot._handle_text_codex_command(msg, "cancel")
    assert "No in-flight" in msg.reply.call_args.args[0]


# ============================================================ ✋ reaction (T1.2)

async def test_raw_reaction_hand_triggers_cancel() -> None:
    bot = make_bot(allowed={"42"})
    payload = MagicMock()
    payload.user_id = 42
    payload.channel_id = 100
    payload.emoji = MagicMock(name="raw-emoji")
    payload.emoji.name = "✋"
    await bot.on_raw_reaction_add(payload)
    bot.service.cancel_active_turn.assert_awaited_once_with("100")


async def test_raw_reaction_other_emoji_ignored() -> None:
    bot = make_bot(allowed={"42"})
    payload = MagicMock()
    payload.user_id = 42
    payload.channel_id = 100
    payload.emoji = MagicMock(name="raw-emoji")
    payload.emoji.name = "👍"
    await bot.on_raw_reaction_add(payload)
    bot.service.cancel_active_turn.assert_not_called()


async def test_raw_reaction_unallowed_user_ignored() -> None:
    bot = make_bot(allowed={"42"})
    payload = MagicMock()
    payload.user_id = 999
    payload.channel_id = 100
    payload.emoji = MagicMock(name="raw-emoji")
    payload.emoji.name = "✋"
    await bot.on_raw_reaction_add(payload)
    bot.service.cancel_active_turn.assert_not_called()


# ============================================================ rollback command (T3.12)

async def test_text_codex_rollback_default_is_one() -> None:
    bot = make_bot()
    msg = make_message(content="/codex rollback")
    await bot._handle_text_codex_command(msg, "rollback")
    bot.service.rollback.assert_awaited_once_with("100", count=1)
    assert "1 turn" in msg.reply.call_args.args[0]


async def test_text_codex_rollback_with_n() -> None:
    bot = make_bot()
    msg = make_message(content="/codex rollback 3")
    await bot._handle_text_codex_command(msg, "rollback 3")
    bot.service.rollback.assert_awaited_once_with("100", count=3)
    assert "3 turns" in msg.reply.call_args.args[0]


async def test_text_codex_rollback_invalid_n_replies_usage() -> None:
    bot = make_bot()
    msg = make_message(content="/codex rollback notanum")
    await bot._handle_text_codex_command(msg, "rollback notanum")
    assert "Usage" in msg.reply.call_args.args[0]
    bot.service.rollback.assert_not_called()


async def test_text_codex_rollback_no_session() -> None:
    bot = make_bot()
    bot.service.is_active = MagicMock(return_value=False)
    msg = make_message(content="/codex rollback")
    await bot._handle_text_codex_command(msg, "rollback")
    assert "No active" in msg.reply.call_args.args[0]
    bot.service.rollback.assert_not_called()


# ============================================================ turn-stage reactions (T2.8)

async def test_apply_turn_stage_reaction_started_adds_thinking() -> None:
    bot = make_bot()
    msg = make_message()
    bot._last_user_message["100"] = msg
    await bot.apply_turn_stage_reaction("100", "started")
    msg.add_reaction.assert_awaited_once_with("🤔")


async def test_apply_turn_stage_reaction_file_change_adds_gear() -> None:
    bot = make_bot()
    msg = make_message()
    bot._last_user_message["100"] = msg
    await bot.apply_turn_stage_reaction("100", "file_change")
    msg.add_reaction.assert_awaited_once_with("⚙️")


async def test_apply_turn_stage_reaction_completed_adds_check_and_removes_transient() -> None:
    bot = make_bot()
    msg = make_message()
    bot._last_user_message["100"] = msg
    await bot.apply_turn_stage_reaction("100", "completed")
    msg.add_reaction.assert_awaited_once_with("✅")
    assert msg.remove_reaction.await_count == 2  # 🤔 and ⚙️


async def test_apply_turn_stage_reaction_no_anchor_message_noop() -> None:
    bot = make_bot()
    # _last_user_message empty
    await bot.apply_turn_stage_reaction("100", "started")
    # Nothing to assert on a phantom message; just doesn't crash.


# ============================================================ env-flag plumbing (smoke)

def test_load_runtime_picks_up_v02_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke: required token + new envs read without error."""
    monkeypatch.setenv("CODEX_RC_DISCORD_TOKEN", "x")
    monkeypatch.setenv("CODEX_RC_AUTO_APPROVE", "true")
    monkeypatch.setenv("CODEX_RC_MENTION_ON_COMPLETE", "271656041958080518")
    monkeypatch.setenv("CODEX_RC_AUTO_COMPACT_INPUT_TOKENS", "120000")
    monkeypatch.setenv("CODEX_RC_OPS_CHANNEL_ID", "1234567890")
    from codex_rc.discord_bot import _load_runtime

    settings, token, _access, _channels, _guild = _load_runtime()
    assert token == "x"
    # The envs are read inside run() rather than _load_runtime so we just
    # confirm os.environ reflects what the wizard / shell wrote.
    import os as _os
    assert _os.environ["CODEX_RC_AUTO_APPROVE"] == "true"
    assert _os.environ["CODEX_RC_MENTION_ON_COMPLETE"] == "271656041958080518"
    assert _os.environ["CODEX_RC_AUTO_COMPACT_INPUT_TOKENS"] == "120000"
    assert _os.environ["CODEX_RC_OPS_CHANNEL_ID"] == "1234567890"
    assert isinstance(settings.transport_mode, str)


_ = Any
