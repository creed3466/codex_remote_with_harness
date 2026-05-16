"""Behavioural tests for CodexRcBot routing without booting discord.py.

We bypass ``discord.Client.__init__`` via ``CodexRcBot.__new__(CodexRcBot)``
and stub the attributes the routing methods actually read. This lets us
drive ``on_message`` / ``on_interaction`` / the text-command dispatchers
with cheap MagicMock messages.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from codex_rc.discord_bot import (
    GATEWAY_RESTART_BUTTON_PREFIX,
    AccessControl,
    CodexRcBot,
    _parse_start_options,
    _RestartGatewayButton,
)
from codex_rc.session_store import HandoffRecord, Session, ThreadRecord

# ============================================================ fixtures

def make_service() -> MagicMock:
    svc = MagicMock()
    svc.is_active = MagicMock(return_value=True)
    svc.has_session_record = MagicMock(return_value=True)
    svc.start_session = AsyncMock()
    svc.stop_session = AsyncMock()
    svc.send_text = AsyncMock()
    svc.resume_thread = AsyncMock()
    svc.resume_handoff = AsyncMock(return_value=make_session())
    svc.new_thread = AsyncMock(return_value="thread-fresh-id")
    svc.handle_button = AsyncMock(return_value=True)
    svc.store = MagicMock()
    svc.store.list_threads = MagicMock(return_value=[])
    svc.store.list_handoffs = MagicMock(return_value=[])
    svc.store.get = MagicMock(return_value=None)
    return svc


def make_bot(
    *,
    service: MagicMock | None = None,
    allowed: set[str] | None = None,
    access: AccessControl | None = None,
    channels: set[str] | None = None,
    bot_user_id: int = 999_999,
) -> CodexRcBot:
    bot = CodexRcBot.__new__(CodexRcBot)
    bot.service = service or make_service()
    if access is not None:
        bot.access = access
    else:
        # Legacy ``allowed`` set is interpreted as a wildcard allowlist.
        uids = tuple(allowed) if allowed is not None else ("42",)
        bot.access = AccessControl({"*": uids} if uids else {})
    bot.channel_ids = channels
    bot.guild_id = None
    bot._connection = MagicMock()
    bot._connection.user = MagicMock(id=bot_user_id)
    bot._last_user_message = {}
    bot._editable_messages = {}
    bot._warned_blocked_users = set()
    bot._autocomplete_path_tokens = {}
    return bot


def make_message(
    *,
    content: str,
    author_id: int = 42,
    author_is_bot: bool = False,
    channel_id: int = 100,
    attachments: list[Any] | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = author_is_bot
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    msg.attachments = attachments or []
    msg.reply = AsyncMock()
    msg.add_reaction = AsyncMock()
    return msg


class _FakeCodexCommandTree:
    def __init__(self) -> None:
        self.codex_cmd = None

    class _FakeCommand:
        def __init__(self, callback) -> None:
            self.callback = callback

        def autocomplete(self, *args, **kwargs):  # type: ignore[override]
            def _decorate(func):
                return self

            return _decorate

    def command(self, *args, **kwargs):  # type: ignore[override]
        def _decorate(func):
            self.codex_cmd = self._FakeCommand(func)
            return self.codex_cmd

        return _decorate


def make_slash_interaction(
    *,
    user_id: int = 42,
    channel_id: int = 100,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.channel_id = channel_id
    inter.user = MagicMock(id=user_id)
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def register_codex_command(bot: CodexRcBot) -> Any:
    tree = _FakeCodexCommandTree()
    bot.tree = tree
    bot._register_commands()
    assert tree.codex_cmd is not None
    return tree.codex_cmd.callback


def make_attachment(*, url: str, content_type: str = "image/png") -> MagicMock:
    a = MagicMock()
    a.url = url
    a.content_type = content_type
    return a


def make_session(thread_id: str = "thread-1") -> Session:
    return Session(
        channel_id="100",
        project_path="/p",
        sandbox="workspace-write",
        approval="on-request",
        state="running",
        codex_thread_id=thread_id,
        created_at=0.0,
        updated_at=0.0,
    )


def make_thread_record(thread_id: str = "abcdef1234", turn_count: int = 3) -> ThreadRecord:
    return ThreadRecord(
        codex_thread_id=thread_id,
        channel_id="100",
        project_path="/p",
        started_at=0.0,
        last_used_at=0.0,
        preview="hello world",
        turn_count=turn_count,
    )


def make_handoff_record(handoff_id: str = "handoff-abcdef1234") -> HandoffRecord:
    return HandoffRecord(
        handoff_id=handoff_id,
        channel_id="100",
        project_path="/p",
        sandbox="workspace-write",
        approval="on-request",
        source_codex_thread_id="thread-old",
        target_codex_thread_id=None,
        file_path="/tmp/handoff.md",
        created_at=0.0,
        updated_at=0.0,
        preview="handoff preview",
        status="ready",
    )


# ============================================================ AccessControl

def test_access_control_open_admits_anyone() -> None:
    ac = AccessControl({})
    assert ac.is_open
    assert ac.allows("anyone", "C1")


def test_access_control_wildcard_admits_listed_user_in_any_channel() -> None:
    ac = AccessControl({"*": ("42",)})
    assert ac.allows("42", "C1")
    assert ac.allows("42", "C2")
    assert not ac.allows("999", "C1")


def test_access_control_channel_specific_does_not_leak() -> None:
    ac = AccessControl({"C1": ("42",), "C2": ("99",)})
    assert ac.allows("42", "C1")
    assert not ac.allows("42", "C2")
    assert ac.allows("99", "C2")
    assert not ac.allows("99", "C1")


def test_access_control_wildcard_combines_with_channel_specific() -> None:
    ac = AccessControl({"*": ("99",), "C1": ("42",)})
    assert ac.allows("42", "C1")  # channel-specific
    assert ac.allows("99", "C1")  # wildcard
    assert ac.allows("99", "C2")  # wildcard
    assert not ac.allows("42", "C2")  # 42 only allowed in C1


# Channel-aware routing tests at the on_message level:

async def test_on_message_channel_specific_admits_in_owned_channel() -> None:
    bot = make_bot(access=AccessControl({"100": ("42",)}))
    msg = make_message(content="hello", author_id=42, channel_id=100)
    await bot.on_message(msg)
    bot.service.send_text.assert_awaited_once_with(
        "100", "hello", image_urls=None
    )


async def test_on_message_channel_specific_rejects_in_foreign_channel() -> None:
    bot = make_bot(access=AccessControl({"100": ("42",)}))
    msg = make_message(content="hello", author_id=42, channel_id=200)
    await bot.on_message(msg)
    bot.service.send_text.assert_not_called()


# ============================================================ on_message: filters

async def test_on_message_ignores_bot_authors() -> None:
    bot = make_bot()
    msg = make_message(content="hi", author_is_bot=True)
    await bot.on_message(msg)
    msg.reply.assert_not_called()
    bot.service.send_text.assert_not_called()


async def test_on_message_ignores_self() -> None:
    bot = make_bot(bot_user_id=12345)
    msg = make_message(content="hi", author_id=12345)
    await bot.on_message(msg)
    bot.service.send_text.assert_not_called()


async def test_on_message_ignores_unallowed_user() -> None:
    bot = make_bot(allowed={"42"})
    msg = make_message(content="hi", author_id=999)
    await bot.on_message(msg)
    bot.service.send_text.assert_not_called()


async def test_on_message_ignores_unlisted_channel() -> None:
    bot = make_bot(channels={"222"})
    msg = make_message(content="hi", channel_id=100)
    await bot.on_message(msg)
    bot.service.send_text.assert_not_called()


async def test_on_message_ignores_empty_message() -> None:
    bot = make_bot()
    msg = make_message(content="   ")
    await bot.on_message(msg)
    bot.service.send_text.assert_not_called()
    msg.reply.assert_not_called()


async def test_on_message_warns_when_no_active_session() -> None:
    bot = make_bot()
    bot.service.is_active = MagicMock(return_value=False)
    msg = make_message(content="hello")
    await bot.on_message(msg)
    msg.reply.assert_called_once()
    args, kwargs = msg.reply.call_args
    assert "No active codex_rc session" in args[0]
    assert kwargs == {"mention_author": False}


async def test_on_message_forwards_to_service() -> None:
    bot = make_bot()
    msg = make_message(content="hello")
    await bot.on_message(msg)
    bot.service.send_text.assert_awaited_once_with(
        "100", "hello", image_urls=None
    )


async def test_on_message_forwards_image_attachments_and_reacts() -> None:
    bot = make_bot()
    msg = make_message(
        content="what is this",
        attachments=[
            make_attachment(url="https://cdn/a.png"),
            make_attachment(url="https://cdn/notes.txt", content_type="text/plain"),
        ],
    )
    await bot.on_message(msg)
    bot.service.send_text.assert_awaited_once()
    call = bot.service.send_text.await_args
    assert call.kwargs["image_urls"] == ["https://cdn/a.png"]
    msg.add_reaction.assert_awaited_once_with("🖼️")


async def test_on_message_replies_with_error_on_send_text_exception() -> None:
    bot = make_bot()
    bot.service.send_text = AsyncMock(side_effect=RuntimeError("kaboom"))
    msg = make_message(content="hi")
    await bot.on_message(msg)
    msg.reply.assert_called_once()
    assert "❌" in msg.reply.call_args.args[0]


async def test_on_message_ignores_other_slash_commands() -> None:
    bot = make_bot()
    msg = make_message(content="/help")
    await bot.on_message(msg)
    bot.service.send_text.assert_not_called()


async def test_on_message_dispatches_text_codex_command() -> None:
    bot = make_bot()
    msg = make_message(content="/codex stop")
    await bot.on_message(msg)
    bot.service.stop_session.assert_awaited_once_with("100")


# ============================================================ _handle_text_codex_command

async def test_text_codex_start_without_path_replies_usage() -> None:
    bot = make_bot()
    msg = make_message(content="/codex start")
    await bot._handle_text_codex_command(msg, "start")
    msg.reply.assert_called_once()
    assert "Usage" in msg.reply.call_args.args[0]
    bot.service.start_session.assert_not_called()


async def test_text_codex_start_invokes_service_and_replies() -> None:
    bot = make_bot()
    bot.service.start_session = AsyncMock(return_value=make_session("t-1"))
    msg = make_message(content="/codex start /tmp/proj")
    await bot._handle_text_codex_command(msg, "start /tmp/proj")
    bot.service.start_session.assert_awaited_once_with(
        channel_id="100", project_path="/tmp/proj"
    )
    assert "Project: `/p`" in msg.reply.call_args.args[0]


async def test_text_codex_start_with_autocomplete_token() -> None:
    bot = make_bot()
    bot.service.start_session = AsyncMock(return_value=make_session("t-1"))
    token = "codexrc-path:abcdef1234"
    bot._autocomplete_path_tokens[token] = (
        "100",
        "42",
        "/tmp/long/path",
        time.time() + 60,
    )
    msg = make_message(content="/codex start codexrc-path:abcdef1234")
    await bot._handle_text_codex_command(msg, "start codexrc-path:abcdef1234")
    bot.service.start_session.assert_awaited_once_with(
        channel_id="100",
        project_path="/tmp/long/path",
    )
    assert "Project: `/p`" in msg.reply.call_args.args[0]


async def test_text_codex_start_with_expired_autocomplete_token_errors() -> None:
    bot = make_bot()
    token = "codexrc-path:deadbeef1234"
    bot._autocomplete_path_tokens[token] = (
        "100",
        "42",
        "/tmp/long/path",
        time.time() - 1,
    )
    msg = make_message(content="/codex start codexrc-path:deadbeef1234")
    await bot._handle_text_codex_command(msg, "start codexrc-path:deadbeef1234")
    assert "reselect" in msg.reply.call_args.args[0]
    bot.service.start_session.assert_not_called()


def test_parse_start_options_extracts_permissions_flag() -> None:
    path, preset = _parse_start_options(
        "start /tmp/proj --permissions read-only",
        "/tmp/proj --permissions read-only",
    )
    assert path == "/tmp/proj"
    assert preset == "read-only"


def test_parse_start_options_extracts_permissions_key_shape() -> None:
    path, preset = _parse_start_options(
        "action:start project_path:/tmp/proj permissions:auto",
        "/tmp/proj",
    )
    assert path == "/tmp/proj"
    assert preset == "auto"


async def test_text_codex_start_with_permissions_invokes_service() -> None:
    bot = make_bot()
    bot.service.start_session = AsyncMock(return_value=make_session("t-1"))
    msg = make_message(content="/codex start /tmp/proj --permissions read-only")
    await bot._handle_text_codex_command(
        msg, "start /tmp/proj --permissions read-only"
    )
    bot.service.start_session.assert_awaited_once_with(
        channel_id="100",
        project_path="/tmp/proj",
        sandbox="read-only",
        approval="on-request",
    )
    assert "Project: `/p`" in msg.reply.call_args.args[0]


async def test_text_codex_start_with_invalid_permissions_replies() -> None:
    bot = make_bot()
    msg = make_message(content="/codex start /tmp/proj --permissions nope")
    await bot._handle_text_codex_command(
        msg, "start /tmp/proj --permissions nope"
    )
    assert "unknown preset" in msg.reply.call_args.args[0]
    bot.service.start_session.assert_not_called()


async def test_text_codex_start_handles_not_found() -> None:
    bot = make_bot()
    bot.service.start_session = AsyncMock(side_effect=FileNotFoundError("no"))
    msg = make_message(content="/codex start /nowhere")
    await bot._handle_text_codex_command(msg, "start /nowhere")
    assert "❌" in msg.reply.call_args.args[0]


async def test_text_codex_status_no_session() -> None:
    bot = make_bot()
    msg = make_message(content="/codex status")
    await bot._handle_text_codex_command(msg, "status")
    msg.reply.assert_called_once()
    assert "No session" in msg.reply.call_args.args[0]


async def test_text_codex_status_with_session() -> None:
    bot = make_bot()
    bot.service.store.get = MagicMock(return_value=make_session("t-1"))
    msg = make_message(content="/codex status")
    await bot._handle_text_codex_command(msg, "status")
    body = msg.reply.call_args.args[0]
    assert "t-1" in body
    assert "running" in body


async def test_text_codex_capabilities_uses_service_report() -> None:
    bot = make_bot()
    bot.service.capabilities_text = MagicMock(
        return_value="**Codex CLI capability audit**\nmode `app-server/threaded`"
    )
    msg = make_message(content="/codex capabilities")

    await bot._handle_text_codex_command(msg, "capabilities")

    body = msg.reply.call_args.args[0]
    assert "Codex CLI capability audit" in body
    assert "app-server/threaded" in body
    bot.service.capabilities_text.assert_called_once_with("100")


async def test_text_codex_unknown_action_prints_help() -> None:
    bot = make_bot()
    msg = make_message(content="/codex what")
    await bot._handle_text_codex_command(msg, "what")
    assert "Unknown action" in msg.reply.call_args.args[0]


async def test_text_codex_history_empty() -> None:
    bot = make_bot()
    msg = make_message(content="/codex history")
    await bot._handle_text_codex_command(msg, "history")
    msg.reply.assert_called_once()
    assert "No threads" in msg.reply.call_args.args[0]


async def test_text_codex_history_renders_embed() -> None:
    bot = make_bot()
    bot.service.store.list_threads = MagicMock(
        return_value=[make_thread_record("abcd1234ef", turn_count=2)]
    )
    msg = make_message(content="/codex history")
    await bot._handle_text_codex_command(msg, "history")
    msg.reply.assert_called_once()
    embed = msg.reply.call_args.kwargs.get("embed")
    assert embed is not None
    assert "abcd1234" in embed.description
    assert "**2** turns" in embed.description


async def test_text_codex_resume_no_arg_with_no_handoffs_says_so() -> None:
    bot = make_bot()
    bot.service.store.list_handoffs = MagicMock(return_value=[])
    msg = make_message(content="/codex resume")
    await bot._handle_text_codex_command(msg, "resume")
    body = msg.reply.call_args.args[0]
    assert "No handoffs recorded" in body


async def test_text_codex_resume_no_arg_with_handoffs_renders_picker() -> None:
    """With recent handoffs, /codex resume opens a button picker (no args)."""
    bot = make_bot()
    bot.service.store.list_handoffs = MagicMock(
        return_value=[make_handoff_record(f"handoff-abcdef{i}") for i in range(5)]
    )
    msg = make_message(content="/codex resume")
    await bot._handle_text_codex_command(msg, "resume")
    msg.reply.assert_called_once()
    kwargs = msg.reply.call_args.kwargs
    assert "embed" in kwargs
    assert "view" in kwargs and kwargs["view"] is not None
    # One button per handoff, capped at the picker limit.
    assert len(kwargs["view"].children) == 5


async def test_text_codex_resume_no_match() -> None:
    bot = make_bot()
    bot.service.store.list_handoffs = MagicMock(return_value=[])
    msg = make_message(content="/codex resume xyz")
    await bot._handle_text_codex_command(msg, "resume xyz")
    assert "No handoff matching" in msg.reply.call_args.args[0]


async def test_text_codex_resume_handles_missing_handoff() -> None:
    bot = make_bot()
    bot.service.store.list_handoffs = MagicMock(
        return_value=[make_handoff_record("handoff-abcd1234ef")]
    )
    bot.service.resume_handoff = AsyncMock(side_effect=KeyError("no handoff"))
    msg = make_message(content="/codex resume handoff-abcd")
    await bot._handle_text_codex_command(msg, "resume handoff-abcd")
    assert "Handoff not found" in msg.reply.call_args.args[0]


async def test_text_codex_resume_success() -> None:
    bot = make_bot()
    bot.service.store.list_handoffs = MagicMock(
        return_value=[make_handoff_record("handoff-abcd1234ef")]
    )
    msg = make_message(content="/codex resume handoff-abcd")
    await bot._handle_text_codex_command(msg, "resume handoff-abcd")
    bot.service.resume_handoff.assert_awaited_once_with(
        "100", "handoff-abcd1234ef"
    )
    assert "Started fresh from handoff" in msg.reply.call_args.args[0]


async def test_text_codex_new_handles_missing_session() -> None:
    bot = make_bot()
    bot.service.new_thread = AsyncMock(side_effect=KeyError("no session"))
    msg = make_message(content="/codex new")
    await bot._handle_text_codex_command(msg, "new")
    assert "No active session" in msg.reply.call_args.args[0]


async def test_text_codex_new_success() -> None:
    bot = make_bot()
    msg = make_message(content="/codex new")
    await bot._handle_text_codex_command(msg, "new")
    assert "New thread" in msg.reply.call_args.args[0]
    assert "thread-f" in msg.reply.call_args.args[0]  # 8-char prefix


async def test_text_codex_stop() -> None:
    bot = make_bot()
    msg = make_message(content="/codex stop")
    await bot._handle_text_codex_command(msg, "stop")
    bot.service.stop_session.assert_awaited_once_with("100")
    assert "Session stopped" in msg.reply.call_args.args[0]


async def test_text_codex_stop_without_session() -> None:
    bot = make_bot()
    bot.service.has_session_record = MagicMock(return_value=False)
    msg = make_message(content="/codex stop")
    await bot._handle_text_codex_command(msg, "stop")
    msg.reply.assert_called_once()
    assert "No session in this channel." in msg.reply.call_args.args[0]
    bot.service.stop_session.assert_not_called()


async def test_text_codex_restart_sends_confirmation_button() -> None:
    bot = make_bot()
    msg = make_message(content="/codex restart")
    await bot._handle_text_codex_command(msg, "restart")
    msg.reply.assert_called_once()
    assert "Confirm gateway restart" in msg.reply.call_args.args[0]
    view = msg.reply.call_args.kwargs["view"]
    assert len(view.children) == 1
    assert view.children[0].custom_id.startswith(GATEWAY_RESTART_BUTTON_PREFIX)


async def test_text_codex_exit_without_session() -> None:
    bot = make_bot()
    bot.service.has_session_record = MagicMock(return_value=False)
    msg = make_message(content="/exit")
    await bot._handle_text_codex_command(msg, "exit")
    msg.reply.assert_called_once()
    assert "No session in this channel." in msg.reply.call_args.args[0]
    bot.service.stop_session.assert_not_called()


async def test_slash_codex_stop_without_session() -> None:
    bot = make_bot()
    bot.service.has_session_record = MagicMock(return_value=False)
    cmd = register_codex_command(bot)
    inter = make_slash_interaction()
    await cmd(
        inter,
        action=discord.app_commands.Choice(name="Stop", value="stop"),
        project_path=None,
        permissions=None,
    )
    inter.response.send_message.assert_awaited_once_with(
        "No session in this channel.", ephemeral=True
    )
    bot.service.stop_session.assert_not_called()


async def test_slash_codex_restart_shows_confirmation() -> None:
    bot = make_bot()
    cmd = register_codex_command(bot)
    inter = make_slash_interaction()
    await cmd(
        inter,
        action=discord.app_commands.Choice(name="Restart gateway", value="restart"),
        project_path=None,
        permissions=None,
    )
    inter.response.send_message.assert_awaited_once()
    args = inter.response.send_message.await_args.args
    assert "Confirm gateway restart" in args[0]
    view = inter.response.send_message.await_args.kwargs["view"]
    assert len(view.children) == 1
    assert view.children[0].custom_id.startswith(GATEWAY_RESTART_BUTTON_PREFIX)


async def test_slash_codex_start_with_autocomplete_token() -> None:
    bot = make_bot()
    bot.service.start_session = AsyncMock(return_value=make_session("thread-start"))
    token = "codexrc-path:abcdef1234"
    bot._autocomplete_path_tokens[token] = (
        "100",
        "42",
        "/tmp/very/long/path",
        time.time() + 60,
    )
    cmd = register_codex_command(bot)
    inter = make_slash_interaction()
    await cmd(
        inter,
        action=discord.app_commands.Choice(name="Start", value="start"),
        project_path=token,
        permissions=None,
    )
    bot.service.start_session.assert_awaited_once_with(
        channel_id="100",
        project_path="/tmp/very/long/path",
    )
    assert bot._autocomplete_path_tokens[token][2] == "/tmp/very/long/path"


# ============================================================ on_interaction

def make_button_interaction(
    *,
    custom_id: str,
    user_id: int = 42,
    channel_id: int = 100,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.type = discord.InteractionType.component
    inter.data = {"custom_id": custom_id}
    inter.user = MagicMock(id=user_id)
    inter.channel_id = channel_id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


async def test_on_interaction_ignores_non_component() -> None:
    bot = make_bot()
    inter = make_button_interaction(custom_id="codex_rc:x:accept")
    inter.type = discord.InteractionType.application_command
    await bot.on_interaction(inter)
    inter.response.send_message.assert_not_called()


async def test_on_interaction_ignores_unrelated_custom_id() -> None:
    bot = make_bot()
    inter = make_button_interaction(custom_id="some_other:button")
    await bot.on_interaction(inter)
    inter.response.send_message.assert_not_called()


async def test_on_interaction_rejects_unallowed_user() -> None:
    bot = make_bot(allowed={"42"})
    inter = make_button_interaction(custom_id="codex_rc:x:accept", user_id=999)
    await bot.on_interaction(inter)
    inter.response.send_message.assert_awaited_once()
    assert "Not allowed" in inter.response.send_message.await_args.args[0]


async def test_on_interaction_accepts_button() -> None:
    bot = make_bot()
    bot.service.handle_button = AsyncMock(return_value=True)
    inter = make_button_interaction(custom_id="codex_rc:abc:accept")
    await bot.on_interaction(inter)
    bot.service.handle_button.assert_awaited_once_with("100", "codex_rc:abc:accept")
    assert "Recorded" in inter.response.send_message.await_args.args[0]
    assert "accept" in inter.response.send_message.await_args.args[0]


async def test_on_interaction_expired_button() -> None:
    bot = make_bot()
    bot.service.handle_button = AsyncMock(return_value=False)
    inter = make_button_interaction(custom_id="codex_rc:abc:accept")
    await bot.on_interaction(inter)
    assert "expired" in inter.response.send_message.await_args.args[0]


async def test_restart_button_requires_allowlist() -> None:
    bot = make_bot(allowed={"42"})
    btn = _RestartGatewayButton(bot=bot, channel_id="100")
    inter = make_button_interaction(custom_id=btn.custom_id, user_id=999)
    await btn.callback(inter)
    assert "Not allowed" in inter.response.send_message.await_args.args[0]


async def test_restart_button_checks_channel_match() -> None:
    bot = make_bot()
    btn = _RestartGatewayButton(bot=bot, channel_id="100")
    inter = make_button_interaction(custom_id=btn.custom_id, channel_id=200)
    await btn.callback(inter)
    assert "Channel mismatch" in inter.response.send_message.await_args.args[0]


async def test_restart_button_requests_restart_and_schedules_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = make_bot()
    btn = _RestartGatewayButton(bot=bot, channel_id="100")
    inter = make_button_interaction(custom_id=btn.custom_id)

    request = MagicMock()
    monkeypatch.setattr("codex_rc.discord_bot.request_self_restart", request)
    shutdown = AsyncMock()
    monkeypatch.setattr("codex_rc.discord_bot._shutdown_after_restart_ack", shutdown)

    await btn.callback(inter)

    request.assert_called_once_with(delay_seconds=1.0)
    shutdown.assert_called_once_with(bot)
    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once_with(
        "♻️ Gateway restart requested. Restarting gateway process."
    )


async def test_restart_button_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_bot()
    btn = _RestartGatewayButton(bot=bot, channel_id="100")
    inter = make_button_interaction(custom_id=btn.custom_id)

    def _raise() -> None:
        raise RuntimeError("no manager")

    shutdown = AsyncMock()
    monkeypatch.setattr("codex_rc.discord_bot.request_self_restart", _raise)
    monkeypatch.setattr("codex_rc.discord_bot._shutdown_after_restart_ack", shutdown)

    await btn.callback(inter)

    assert "gateway restart failed" in inter.followup.send.await_args.args[0]
    shutdown.assert_not_called()


# ============================================================ post()

async def test_post_invalid_channel_id_is_logged_not_raised() -> None:
    bot = make_bot()
    await bot.post("not-a-number", {"content": "x"})


async def test_post_fetches_when_get_channel_returns_none() -> None:
    bot = make_bot()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)
    await bot.post("100", {"content": "hello"})
    channel.send.assert_awaited_once_with(content="hello")


async def test_post_sends_with_embeds_and_view() -> None:
    bot = make_bot()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    payload = {
        "content": "x",
        "embeds": [{"title": "t", "description": "d"}],
        "components": [
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 2, "label": "ok", "custom_id": "codex_rc:x:y"}
                ],
            }
        ],
    }
    await bot.post("100", payload)
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "x"
    assert len(kwargs["embeds"]) == 1
    assert kwargs["view"] is not None


async def test_post_skips_when_payload_empty() -> None:
    bot = make_bot()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    await bot.post("100", {})
    channel.send.assert_not_called()


# ============================================================ post_update()

async def test_post_update_sends_then_edits_same_message() -> None:
    bot = make_bot()
    msg = MagicMock()
    msg.edit = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=msg)
    bot.get_channel = MagicMock(return_value=channel)

    await bot.post_update("100", "plan:t:u", {"content": "first"})
    await bot.post_update("100", "plan:t:u", {"content": "second"})

    channel.send.assert_awaited_once_with(content="first")
    msg.edit.assert_awaited_once_with(content="second")


# ============================================================ post_animated

async def test_post_animated_empty_frames_noop() -> None:
    bot = make_bot()
    bot.get_channel = MagicMock(return_value=MagicMock())
    await bot.post_animated("100", [])


async def test_post_animated_invalid_channel_id_noop() -> None:
    bot = make_bot()
    await bot.post_animated("not-a-number", ["a", "b"])


async def test_post_animated_runs_one_pass_when_not_looping() -> None:
    bot = make_bot()
    msg = MagicMock()
    msg.edit = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=msg)
    bot.get_channel = MagicMock(return_value=channel)
    await bot.post_animated(
        "100", ["a", "b", "c"], interval_s=0.0, loop=False
    )
    # initial send (a) + edits (b, c)
    channel.send.assert_awaited_once()
    assert msg.edit.await_count == 2
    edits = [c.kwargs["content"] for c in msg.edit.await_args_list]
    assert edits == ["b", "c"]


# ============================================================ _load_runtime guard rails

def test_load_runtime_raises_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_rc.discord_bot import _load_runtime

    monkeypatch.delenv("CODEX_RC_DISCORD_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CODEX_RC_DISCORD_TOKEN"):
        _load_runtime()


def test_load_runtime_parses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_rc.discord_bot import _load_runtime

    monkeypatch.setenv("CODEX_RC_DISCORD_TOKEN", "tok-xyz")
    monkeypatch.setenv("CODEX_RC_DISCORD_CHANNEL_IDS", "1,2")
    monkeypatch.setenv("CODEX_RC_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("CODEX_RC_DISCORD_GUILD_ID", "98765")
    settings, token, access, channels, guild = _load_runtime()
    assert token == "tok-xyz"
    assert channels == {"1", "2"}
    # Legacy CSV → wildcard allowlist
    assert access.allows("42", "any-channel")
    assert not access.allows("999", "any-channel")
    assert guild == 98765
    assert isinstance(settings.transport_mode, str)


def test_load_runtime_parses_json_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_rc.discord_bot import _load_runtime

    monkeypatch.setenv("CODEX_RC_DISCORD_TOKEN", "tok-xyz")
    monkeypatch.setenv(
        "CODEX_RC_ALLOWED_USER_IDS",
        '{"*": ["42"], "100": ["99"]}',
    )
    monkeypatch.delenv("CODEX_RC_DISCORD_CHANNEL_IDS", raising=False)
    monkeypatch.delenv("CODEX_RC_DISCORD_GUILD_ID", raising=False)
    _settings, _token, access, _channels, _guild = _load_runtime()
    assert access.allows("42", "100")    # wildcard
    assert access.allows("42", "other")  # wildcard
    assert access.allows("99", "100")    # channel-specific
    assert not access.allows("99", "other")  # 99 only in channel 100


# ensure parametrised SimpleNamespace fixture stays unused noise-free
_ = SimpleNamespace
