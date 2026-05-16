"""Tests for the text-prefix '/codex ...' argument parser."""

from __future__ import annotations

from codex_rc.discord_bot import _parse_text_codex_args


def test_positional_start() -> None:
    assert _parse_text_codex_args("start /Users/alice/Project/claude") == (
        "start",
        "/Users/alice/Project/claude",
    )


def test_positional_stop() -> None:
    assert _parse_text_codex_args("stop") == ("stop", None)


def test_positional_status() -> None:
    assert _parse_text_codex_args("status") == ("status", None)


def test_slash_menu_shape() -> None:
    assert _parse_text_codex_args(
        "action:start project_path:/Users/alice/Project/claude"
    ) == ("start", "/Users/alice/Project/claude")


def test_slash_menu_reversed_order() -> None:
    assert _parse_text_codex_args(
        "project_path:/x action:status"
    ) == ("status", "/x")


def test_empty_args() -> None:
    assert _parse_text_codex_args("") == (None, None)
    assert _parse_text_codex_args("   ") == (None, None)


def test_unknown_action() -> None:
    assert _parse_text_codex_args("frob") == (None, None)


def test_quoted_path() -> None:
    a, p = _parse_text_codex_args('start "/path with space"')
    assert a == "start"
    assert p == "/path with space"
