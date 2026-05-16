"""Tests for Codex CLI slash command passthrough.

Covers:
- _parse_text_codex_args recognises model/permissions/review/fork/goal/
  capabilities/compact/exit/quit and splits off the rest argument
- _route_native_slash maps native '/model foo' → 'model foo'
- ChannelService.{set_model, set_permissions, review, fork, set_goal,
  compact} dispatch the right Codex RPC
- discord_bot handlers reply with the expected confirmation
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from codex_rc.discord_bot import (
    AccessControl,
    CodexRcBot,
    _parse_text_codex_args,
    _route_native_slash,
)
from codex_rc.session_store import SessionStore
from tests.test_channel_service import FakeProc, make_channel_service

# ============================================================ parser

@pytest.mark.parametrize("raw,expected_action,expected_rest", [
    ("model", "model", None),
    ("model gpt-5-codex", "model", "gpt-5-codex"),
    ("permissions auto", "permissions", "auto"),
    ("review base/main", "review", "base/main"),
    ("fork", "fork", None),
    ("goal ship the bot", "goal", "ship the bot"),
    ("capabilities", "capabilities", None),
    ("compact", "compact", None),
    ("exit", "exit", None),
    ("quit", "quit", None),
])
def test_parse_recognises_new_actions(raw, expected_action, expected_rest) -> None:
    action, rest = _parse_text_codex_args(raw)
    assert action == expected_action
    assert rest == expected_rest


@pytest.mark.parametrize("raw,expected_action,expected_rest", [
    ("/model", "model", None),
    ("/model gpt-5", "model", "gpt-5"),
    ("/permissions read-only", "permissions", "read-only"),
    ("/review", "review", None),
    ("/fork", "fork", None),
    ("/goal ship v0.3", "goal", "ship v0.3"),
    ("/capabilities", "capabilities", None),
    ("/compact", "compact", None),
    ("/exit", "exit", None),
    ("/quit", "quit", None),
    ("/status", "status", None),
])
def test_route_native_slash_maps_known_commands(
    raw, expected_action, expected_rest
) -> None:
    args = _route_native_slash(raw)
    assert args is not None
    action, rest = _parse_text_codex_args(args)
    assert action == expected_action
    assert rest == expected_rest


def test_route_native_slash_ignores_unknown_slashes() -> None:
    assert _route_native_slash("/help") is None
    assert _route_native_slash("/random thing") is None
    assert _route_native_slash("not a slash") is None


# ============================================================ ChannelService RPCs

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
    async def _post(channel_id, payload):
        post_calls.append((channel_id, payload))
    return _post


async def test_set_model_without_name_calls_model_list(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {"models": [{"id": "gpt-5", "displayName": "GPT-5"}]}
    out = await cs.set_model(None)
    method = fake_proc.request.call_args.args[0]
    assert method == "model/list"
    assert "models" in out


async def test_set_model_with_name_writes_config(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    out = await cs.set_model("gpt-5-codex")
    method, params = fake_proc.request.call_args.args
    assert method == "config/value/write"
    assert params == {"key": "model", "value": "gpt-5-codex"}
    assert out == {"applied": "gpt-5-codex"}


async def test_set_permissions_writes_both_keys(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    applied = await cs.set_permissions("workspace-write")
    assert applied == {"sandbox": "workspace-write", "approvalPolicy": "on-request"}
    methods = [c.args[0] for c in fake_proc.request.call_args_list]
    assert methods.count("config/value/write") == 2
    keys = [c.args[1]["key"] for c in fake_proc.request.call_args_list]
    assert set(keys) == {"sandbox", "approvalPolicy"}


async def test_set_permissions_rejects_unknown_preset(store, fake_proc, post) -> None:
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    with pytest.raises(ValueError, match="unknown preset"):
        await cs.set_permissions("wild-west")


async def test_review_uses_thread_id_and_optional_target(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    await cs.review("base/main")
    method, params = fake_proc.request.call_args.args
    assert method == "review/start"
    assert params == {"threadId": "thread-1", "target": "base/main"}


async def test_review_omits_target_when_none(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    await cs.review(None)
    params = fake_proc.request.call_args.args[1]
    assert "target" not in params


async def test_fork_swaps_thread_id(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-old")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {"thread": {"id": "thread-new"}}
    new_id = await cs.fork()
    assert new_id == "thread-new"
    sess = store.get("C1")
    assert sess and sess.codex_thread_id == "thread-new"


async def test_set_goal_with_text_calls_goal_set(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    out = await cs.set_goal("ship v0.3")
    assert out == "ship v0.3"
    method, params = fake_proc.request.call_args.args
    assert method == "thread/goal/set"
    assert params == {"threadId": "thread-1", "goal": "ship v0.3"}


async def test_set_goal_blank_calls_goal_clear(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    out = await cs.set_goal("   ")
    assert out == ""
    method = fake_proc.request.call_args.args[0]
    assert method == "thread/goal/clear"


async def test_compact_explicit_calls_thread_compact_start(
    store, fake_proc, post
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}
    await cs.compact()
    method = fake_proc.request.call_args.args[0]
    assert method == "thread/compact/start"


# ============================================================ bot handler smoke

def make_bot(service: MagicMock | None = None) -> CodexRcBot:
    svc = service or MagicMock()
    svc.is_active = MagicMock(return_value=True)
    svc.set_model = AsyncMock()
    svc.set_permissions = AsyncMock()
    svc.review = AsyncMock()
    svc.fork = AsyncMock(return_value="thread-new-id")
    svc.set_goal = AsyncMock(return_value="ship v0.3")
    svc.compact = AsyncMock()
    svc.resume_handoff = AsyncMock()
    svc.store = MagicMock()
    svc.store.list_threads = MagicMock(return_value=[])
    svc.store.list_handoffs = MagicMock(return_value=[])
    svc.store.get = MagicMock(return_value=None)

    bot = CodexRcBot.__new__(CodexRcBot)
    bot.service = svc
    bot.access = AccessControl({"*": ("42",)})
    bot.channel_ids = None
    bot.guild_id = None
    bot._connection = MagicMock()
    bot._connection.user = MagicMock(id=999_999)
    bot._last_user_message = {}
    bot._warned_blocked_users = set()
    return bot


def make_message(content: str = "", channel_id: int = 100) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.author = MagicMock(id=42, bot=False)
    msg.channel = MagicMock(id=channel_id)
    msg.attachments = []
    msg.reply = AsyncMock()
    msg.add_reaction = AsyncMock()
    return msg


async def test_native_model_slash_routes_to_handler() -> None:
    bot = make_bot()
    msg = make_message("/model gpt-5-codex")
    await bot.on_message(msg)
    bot.service.set_model.assert_awaited_once_with("100", "gpt-5-codex")
    assert "gpt-5-codex" in msg.reply.call_args.args[0]


async def test_native_permissions_slash_routes_to_handler() -> None:
    bot = make_bot()
    bot.service.set_permissions = AsyncMock(
        return_value={"sandbox": "read-only", "approvalPolicy": "on-request"}
    )
    msg = make_message("/permissions read-only")
    await bot.on_message(msg)
    bot.service.set_permissions.assert_awaited_once_with("100", "read-only")
    assert "read-only" in msg.reply.call_args.args[0]


async def test_native_review_slash_routes_to_handler() -> None:
    bot = make_bot()
    msg = make_message("/review base/main")
    await bot.on_message(msg)
    bot.service.review.assert_awaited_once_with("100", "base/main")
    assert "Review started" in msg.reply.call_args.args[0]


async def test_native_fork_slash_routes_to_handler() -> None:
    bot = make_bot()
    msg = make_message("/fork")
    await bot.on_message(msg)
    bot.service.fork.assert_awaited_once_with("100")
    assert "Forked" in msg.reply.call_args.args[0]


async def test_native_goal_slash_with_text() -> None:
    bot = make_bot()
    msg = make_message("/goal ship v0.3")
    await bot.on_message(msg)
    bot.service.set_goal.assert_awaited_once_with("100", "ship v0.3")


async def test_native_goal_slash_blank_clears() -> None:
    bot = make_bot()
    bot.service.set_goal = AsyncMock(return_value="")
    msg = make_message("/goal")
    await bot.on_message(msg)
    bot.service.set_goal.assert_awaited_once_with("100", "")
    assert "cleared" in msg.reply.call_args.args[0]


async def test_native_compact_slash() -> None:
    bot = make_bot()
    msg = make_message("/compact")
    await bot.on_message(msg)
    bot.service.compact.assert_awaited_once_with("100")


async def test_codex_prefix_model_also_works() -> None:
    """`/codex model gpt-5` should route to the same handler as `/model gpt-5`."""
    bot = make_bot()
    msg = make_message("/codex model gpt-5")
    await bot.on_message(msg)
    bot.service.set_model.assert_awaited_once_with("100", "gpt-5")


async def test_permissions_missing_preset_replies_usage() -> None:
    bot = make_bot()
    msg = make_message("/permissions")
    await bot.on_message(msg)
    assert "Usage" in msg.reply.call_args.args[0]
    bot.service.set_permissions.assert_not_called()


_ = pytest
