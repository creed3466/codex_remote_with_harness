"""Pure-function helper tests for discord_bot (no Bot lifecycle here)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codex_rc.discord_bot import (
    CodexRcBot,
    _components_to_view,
    _embeds_from_payload,
)


def test_components_to_view_empty_returns_none() -> None:
    assert _components_to_view(None) is None
    assert _components_to_view([]) is None


def test_components_to_view_renders_action_row_with_buttons() -> None:
    row = {
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Approve",
             "custom_id": "codex_rc:abc:accept", "emoji": {"name": "✅"}},
            {"type": 2, "style": 4, "label": "Deny",
             "custom_id": "codex_rc:abc:decline"},
        ],
    }
    view = _components_to_view([row])
    assert view is not None
    children = view.children
    assert len(children) == 2
    labels = [c.label for c in children]
    assert labels == ["Approve", "Deny"]


def test_components_to_view_ignores_non_button_types() -> None:
    row = {
        "type": 1,
        "components": [
            {"type": 999, "label": "weird"},
            {"type": 2, "style": 2, "label": "ok", "custom_id": "x"},
        ],
    }
    view = _components_to_view([row])
    assert view is not None
    assert len(view.children) == 1


def test_embeds_from_payload_round_trip() -> None:
    raw = [{"title": "T", "description": "D", "color": 0x123456}]
    embeds = _embeds_from_payload(raw)
    assert len(embeds) == 1
    assert embeds[0].title == "T"
    assert embeds[0].description == "D"
    assert embeds[0].color.value == 0x123456


def test_embeds_from_payload_empty() -> None:
    assert _embeds_from_payload(None) == []
    assert _embeds_from_payload([]) == []


def _make_bot_with_threads(threads):
    bot = CodexRcBot.__new__(CodexRcBot)
    bot.service = MagicMock()
    bot.service.store = MagicMock()
    bot.service.store.list_threads = MagicMock(return_value=threads)
    return bot


def _make_thread_record(thread_id: str):
    from codex_rc.session_store import ThreadRecord
    return ThreadRecord(
        codex_thread_id=thread_id,
        channel_id="C",
        project_path="/p",
        started_at=0.0,
        last_used_at=0.0,
        preview="",
        turn_count=0,
    )


def test_resolve_thread_id_exact_match() -> None:
    bot = _make_bot_with_threads([_make_thread_record("019e2-full-id-here")])
    assert bot._resolve_thread_id("C", "019e2-full-id-here") == "019e2-full-id-here"


def test_resolve_thread_id_prefix_match() -> None:
    bot = _make_bot_with_threads([
        _make_thread_record("019e2-aaaaa-bbb"),
        _make_thread_record("019e2-ccccc-ddd"),
    ])
    assert bot._resolve_thread_id("C", "019e2-aa") == "019e2-aaaaa-bbb"


def test_resolve_thread_id_returns_none_when_no_match() -> None:
    bot = _make_bot_with_threads([_make_thread_record("019e2-aaaaa")])
    assert bot._resolve_thread_id("C", "deadbeef") is None


def test_resolve_thread_id_strips_backticks() -> None:
    bot = _make_bot_with_threads([_make_thread_record("019e2-aaaa")])
    assert bot._resolve_thread_id("C", "`019e2-aaaa`") == "019e2-aaaa"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  019e2-aaaa  ", "019e2-aaaa"),
        ("`  019e2-aaaa`", "019e2-aaaa"),
    ],
)
def test_resolve_thread_id_strips_whitespace(raw: str, expected: str) -> None:
    bot = _make_bot_with_threads([_make_thread_record("019e2-aaaa")])
    assert bot._resolve_thread_id("C", raw) == expected
