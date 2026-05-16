from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .rpc_client import JsonObj

PostEditable = Callable[[str, str, dict[str, Any]], Awaitable[None]]
"""Signature: ``(channel_id, message_key, discord_payload_dict) -> awaitable``."""

_PLAN_CONTENT_MAX = 1900
_PLAN_ANIMATION_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _turn_id_from_notification(params: JsonObj) -> str:
    turn = params.get("turn") or {}
    if isinstance(turn, dict):
        turn_id = turn.get("id") or params.get("turnId")
    else:
        turn_id = params.get("turnId")
    return str(turn_id or "")


def _ansi_clean(text: object, *, limit: int = 160) -> str:
    cleaned = str(text or "")
    cleaned = _ANSI_ESCAPE.sub("", cleaned)
    cleaned = cleaned.replace("```", "'''")
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] if len(cleaned) > limit else cleaned


def _plan_message_key(params: JsonObj, fallback_turn_id: str | None) -> str:
    thread_id = str(params.get("threadId") or "thread")
    turn_id = _turn_id_from_notification(params) or str(fallback_turn_id or "turn")
    return f"plan:{thread_id}:{turn_id}"


@dataclass(slots=True)
class _PlanProgressSnapshot:
    key: str
    explanation: str | None
    rows: list[tuple[str, str]]
    active_row: int | None
    completed: bool = False
    thread_id: str = "thread"
    turn_id: str = "turn"


def _plan_snapshot_from_notification(
    params: JsonObj,
    *,
    fallback_turn_id: str | None,
    previous: _PlanProgressSnapshot | None = None,
) -> _PlanProgressSnapshot:
    rows: list[tuple[str, str]] = []
    active_row: int | None = None
    for raw_step in params.get("plan") or []:
        step = raw_step if isinstance(raw_step, dict) else {}
        status = str(step.get("status") or "pending")
        text = _ansi_clean(step.get("step") or step.get("text") or "(step)")
        if status == "in_progress" and active_row is None:
            active_row = len(rows)
        rows.append((status, text))

    if not rows and previous is not None:
        rows = list(previous.rows)
        active_row = previous.active_row

    explanation = params.get("explanation")
    thread_id = str(params.get("threadId") or "thread")
    turn_id = _turn_id_from_notification(params) or fallback_turn_id or "turn"

    return _PlanProgressSnapshot(
        key=f"plan:{thread_id}:{turn_id}",
        explanation=_ansi_clean(explanation, limit=240) if explanation else None,
        rows=rows,
        active_row=active_row,
        thread_id=thread_id,
        turn_id=turn_id,
    )


def _render_plan_progress(snapshot: _PlanProgressSnapshot, *, tick: int) -> str:
    lines: list[str] = []
    if snapshot.explanation:
        lines.append(snapshot.explanation)
        lines.append("")

    rows = snapshot.rows or [("pending", "(empty plan)")]
    marker = _PLAN_ANIMATION_FRAMES[tick % len(_PLAN_ANIMATION_FRAMES)]
    for _i, (status, text) in enumerate(rows):
        if snapshot.completed and status == "in_progress":
            status = "completed"
        if status == "completed":
            line = f"✅ {text}"
        elif status == "in_progress":
            line = f"{marker} {text}"
        else:
            line = f"⬜ {text}"
        lines.append(line)
    body = "\n".join(lines).rstrip()
    content = f"```\n{body}\n```"
    if len(content) <= _PLAN_CONTENT_MAX:
        return content
    limit = max(_PLAN_CONTENT_MAX - 24, 1)
    return f"```\n{body[:limit].rstrip()}\n... truncated\n```"


class PlanProgressController:
    """Serialize plan update rendering + animation updates for a channel."""

    def __init__(
        self,
        *,
        channel_id: str,
        post_update: PostEditable | None,
        post_lock: asyncio.Lock | None = None,
        interval_s: float = 1.0,
    ) -> None:
        self.channel_id = channel_id
        self.post_update = post_update
        self._post_lock = post_lock or asyncio.Lock()
        self._interval_s = interval_s
        self._snapshot: _PlanProgressSnapshot | None = None
        self._task: asyncio.Task[None] | None = None
        self._tick = 0

    @property
    def snapshot(self) -> _PlanProgressSnapshot | None:
        return self._snapshot

    async def handle_notification(self, notif: JsonObj, fallback_turn_id: str | None) -> bool:
        method = str(notif.get("method") or "")
        params = notif.get("params") or {}

        if method == "turn/plan/updated":
            if self.post_update is None:
                return True
            previous = self._snapshot
            snapshot = _plan_snapshot_from_notification(
                params,
                fallback_turn_id=fallback_turn_id,
                previous=(
                    previous
                    if previous and previous.key == _plan_message_key(
                        params, fallback_turn_id
                    )
                    else None
                ),
            )
            if (
                previous is None
                or previous.key != snapshot.key
                or previous.active_row != snapshot.active_row
            ):
                self._tick = 0
            self._snapshot = snapshot
            await self._post_plan_progress_frame()
            self._ensure_plan_animation()
            return True

        if method in {"turn/completed", "error"}:
            if not await self._handle_completion(
                method == "turn/completed", params, fallback_turn_id
            ):
                return False
            return True
        return False

    async def _handle_completion(
        self,
        completed: bool,
        params: JsonObj,
        fallback_turn_id: str | None,
    ) -> bool:
        snapshot = self._snapshot
        if snapshot is None:
            return False
        if not self._notif_matches_snapshot(params, snapshot, fallback_turn_id):
            return False
        await self._finish(completed=completed)
        return True

    async def _finish(self, *, completed: bool) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        self._cancel_plan_progress_task()
        snapshot.completed = completed
        self._snapshot = snapshot
        await self._post_plan_progress_frame()
        self._snapshot = None

    def cancel(self) -> None:
        self._cancel_plan_progress_task()
        self._snapshot = None

    def _ensure_plan_animation(self) -> None:
        if self.post_update is None or self._snapshot is None:
            return
        if self._snapshot.active_row is None:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._animate_plan_progress(),
            name=f"codex-rc-plan-{self.channel_id}",
        )

    def _cancel_plan_progress_task(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    async def _animate_plan_progress(self) -> None:
        try:
            while self._snapshot is not None and not self._snapshot.completed:
                if not await self._step_plan_animation_frame():
                    return
                if self._snapshot is None or self._snapshot.completed:
                    return
                await asyncio.sleep(self._interval_s)
        except asyncio.CancelledError:
            return
        except Exception:
            logger = logging.getLogger(__name__)
            logger.exception("codex_rc: plan progress animation failed")

    async def _step_plan_animation_frame(self) -> bool:
        if self._snapshot is None or self._snapshot.active_row is None:
            return False
        self._tick += 1
        await self._post_plan_progress_frame()
        return True

    async def _post_plan_progress_frame(self) -> None:
        if self.post_update is None or self._snapshot is None:
            return
        payload = {"content": _render_plan_progress(self._snapshot, tick=self._tick)}
        try:
            async with self._post_lock:
                await self.post_update(
                    self.channel_id,
                    self._snapshot.key,
                    payload,
                )
        except Exception:
            logger = logging.getLogger(__name__)
            logger.exception("codex_rc: failed to update plan progress")

    def _notif_matches_snapshot(
        self,
        params: JsonObj,
        snapshot: _PlanProgressSnapshot,
        fallback_turn_id: str | None,
    ) -> bool:
        notif_thread = str(params.get("threadId") or "")
        notif_turn = _turn_id_from_notification(params)
        if notif_thread and notif_turn:
            return notif_thread == snapshot.thread_id and notif_turn == snapshot.turn_id
        if notif_thread:
            return notif_thread == snapshot.thread_id
        if notif_turn:
            return notif_turn == snapshot.turn_id
        return fallback_turn_id is None or fallback_turn_id == snapshot.turn_id
