from __future__ import annotations

import pytest

from codex_rc.plan_progress import (
    _PLAN_CONTENT_MAX,
    PlanProgressController,
    _plan_snapshot_from_notification,
    _PlanProgressSnapshot,
    _render_plan_progress,
)


def test_plan_snapshot_parser_strips_formatting() -> None:
    notification = {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "explanation": "  Build  index\nwith spaces  ",
        "plan": [
            {"status": "completed", "step": "Open `file`"},
            {"status": "in_progress", "step": " \x1b[31mDo work\x1b[0m "},
        ],
    }
    snapshot = _plan_snapshot_from_notification(
        notification,
        fallback_turn_id="turn-fallback",
    )

    assert snapshot.key == "plan:thread-1:turn-1"
    assert snapshot.explanation == "Build index with spaces"
    assert snapshot.rows == [("completed", "Open `file`"), ("in_progress", "Do work")]
    assert snapshot.active_row == 1


def test_render_plan_progress_frame_changes_visibility_without_ansi() -> None:
    snapshot = _PlanProgressSnapshot(
        key="plan:thread-1:turn-1",
        explanation="doing work",
        rows=[("completed", "start"), ("in_progress", "compile"), ("pending", "finish")],
        active_row=1,
    )

    first = _render_plan_progress(snapshot, tick=0)
    second = _render_plan_progress(snapshot, tick=1)
    assert first != second
    assert "```" in first and "```" in second
    assert "\x1b[" not in first
    assert "\x1b[" not in second
    assert "⠋" in first


def test_render_plan_progress_marks_in_progress_as_completed_on_completion() -> None:
    snapshot = _PlanProgressSnapshot(
        key="plan:thread-1:turn-1",
        explanation=None,
        rows=[("completed", "start"), ("in_progress", "compile")],
        active_row=1,
        completed=True,
    )
    content = _render_plan_progress(snapshot, tick=5)
    assert "✅ compile" in content


def test_render_plan_progress_truncates_long_output() -> None:
    long_step = "very long step " * 300
    snapshot = _PlanProgressSnapshot(
        key="plan:thread-1:turn-1",
        explanation=long_step,
        rows=[("in_progress", long_step)],
        active_row=0,
    )
    content = _render_plan_progress(snapshot, tick=0)
    assert "... truncated" in content
    assert len(content) <= _PLAN_CONTENT_MAX


@pytest.mark.asyncio
async def test_controller_keeps_plan_row_key_and_ignores_unrelated_completion() -> None:
    calls: list[tuple[str, str, dict]] = []

    async def post_update(_channel_id: str, _key: str, payload: dict) -> None:
        calls.append((_channel_id, _key, payload))

    controller = PlanProgressController(
        channel_id="C1",
        post_update=post_update,
        interval_s=999.0,
    )

    handled_update = await controller.handle_notification(
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "plan": [
                    {"status": "in_progress", "step": "first"},
                ],
            },
        },
        fallback_turn_id=None,
    )
    assert handled_update is True
    assert calls and calls[0][1] == "plan:thread-1:turn-1"

    handled_other_turn = await controller.handle_notification(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-2"}},
        },
        fallback_turn_id="turn-1",
    )
    assert handled_other_turn is False
    assert len(calls) == 1
    assert controller.snapshot is not None

    handled_matching = await controller.handle_notification(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        },
        fallback_turn_id="turn-1",
    )
    assert handled_matching is True
    assert controller.snapshot is None
    assert len(calls) == 2
    assert calls[1][0] == "C1"
    assert calls[1][1] == "plan:thread-1:turn-1"
    assert "✅ first" in calls[1][2]["content"]
