"""Tests for the project_path autocomplete callback.

The callback is sync (no IO besides filesystem iterdir) so we can drive
it directly with a fabricated interaction object.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import discord

from codex_rc.discord_bot import (
    AccessControl,
    CodexRcBot,
    _project_path_choices,
)


def _make_bot(history_paths: list[str] | None = None) -> CodexRcBot:
    bot = CodexRcBot.__new__(CodexRcBot)
    bot.service = MagicMock()
    bot.access = AccessControl({"*": ("42",)})
    bot.channel_ids = None
    bot.guild_id = None
    bot._connection = MagicMock()
    bot._connection.user = MagicMock(id=999_999)
    bot._last_user_message = {}
    bot._warned_blocked_users = set()
    # store.list_all returns Session-like objects with .project_path only
    sessions = [MagicMock(project_path=p) for p in (history_paths or [])]
    bot.service.store = MagicMock()
    bot.service.store.list_all = MagicMock(return_value=sessions)
    return bot


def _make_interaction(channel_id: int = 100, user_id: int = 42) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.channel_id = channel_id
    inter.user = MagicMock(id=user_id)
    return inter


def test_empty_current_lists_home_children(tmp_path: Path, monkeypatch) -> None:
    """No prefix → list home directory children; hidden dot-dirs filtered.

    pytest's tmp_path is long enough on macOS that the bot's 100-char
    truncate eats the trailing basename, so we assert on counts and the
    `.hidden` filter rather than tail-match the full value.
    """
    (tmp_path / "ProjectA").mkdir()
    (tmp_path / "ProjectB").mkdir()
    (tmp_path / ".hidden").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    bot = _make_bot()
    choices = _project_path_choices(bot, _make_interaction(), "")
    assert len(choices) >= 2
    assert not any(".hidden" in c.value for c in choices)


def test_prefix_filters_children_by_name(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpine").mkdir()
    (tmp_path / "beta").mkdir()
    bot = _make_bot()
    choices = _project_path_choices(bot, _make_interaction(), str(tmp_path / "alp"))
    assert len(choices) == 2  # alpha + alpine, beta filtered out
    assert not any("/beta" in c.value for c in choices)


def test_history_paths_appear_first_when_matched(tmp_path: Path) -> None:
    """Operator-recent project_path's surface before filesystem entries."""
    (tmp_path / "codex_rc").mkdir()
    history = ["/Users/alice/work/codex_rc"]
    bot = _make_bot(history_paths=history)
    choices = _project_path_choices(bot, _make_interaction(), "codex")
    # First few choices should be history matches.
    values = [c.value for c in choices]
    assert values[0] == "/Users/alice/work/codex_rc"


def test_unallowed_user_gets_empty_list() -> None:
    bot = _make_bot()
    inter = _make_interaction(user_id=999)
    assert _project_path_choices(bot, inter, "anything") == []


def test_caps_at_25_choices(tmp_path: Path) -> None:
    for i in range(40):
        (tmp_path / f"d{i:02d}").mkdir()
    bot = _make_bot()
    choices = _project_path_choices(bot, _make_interaction(), str(tmp_path) + "/")
    assert len(choices) <= 25


def test_long_path_is_displayed_truncated(tmp_path: Path) -> None:
    deep = tmp_path
    for chunk in ["a" * 30, "b" * 30, "c" * 30, "d" * 30]:
        deep = deep / chunk
        deep.mkdir()
    bot = _make_bot()
    choices = _project_path_choices(bot, _make_interaction(), str(tmp_path) + "/")
    # No name exceeds Discord's 100-char limit.
    for c in choices:
        assert len(c.name) <= 100
        assert len(c.value) <= 100
