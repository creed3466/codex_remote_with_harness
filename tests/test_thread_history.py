"""Unit tests for thread_history table on :class:`SessionStore`."""

from __future__ import annotations

import time as _time
from pathlib import Path

from codex_rc.session_store import SessionStore


def make_store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "s.db")


def test_record_thread_inserts_row(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.record_thread(
        codex_thread_id="t-1", channel_id="C", project_path="/p"
    )
    rec = s.get_thread("t-1")
    assert rec is not None
    assert rec.codex_thread_id == "t-1"
    assert rec.channel_id == "C"
    assert rec.preview == ""
    assert rec.turn_count == 0


def test_record_thread_is_idempotent(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.record_thread(codex_thread_id="t-1", channel_id="C", project_path="/p")
    s.record_thread(codex_thread_id="t-1", channel_id="C", project_path="/p")
    threads = s.list_threads("C")
    assert len(threads) == 1


def test_touch_thread_increments_count_and_bumps_timestamp(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.record_thread(codex_thread_id="t-1", channel_id="C", project_path="/p")
    before = s.get_thread("t-1")
    assert before
    _time.sleep(0.01)  # ensure monotonic forward
    s.touch_thread("t-1", preview_addition="first prompt", turn_increment=1)
    after = s.get_thread("t-1")
    assert after
    assert after.turn_count == 1
    assert after.preview == "first prompt"
    assert after.last_used_at >= before.last_used_at


def test_touch_thread_preview_locked_after_first_set(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.record_thread(codex_thread_id="t-1", channel_id="C", project_path="/p")
    s.touch_thread("t-1", preview_addition="first", turn_increment=1)
    s.touch_thread("t-1", preview_addition="second", turn_increment=1)
    rec = s.get_thread("t-1")
    assert rec and rec.preview == "first"
    assert rec.turn_count == 2


def test_list_threads_ordered_by_last_used_desc(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.record_thread(codex_thread_id="old", channel_id="C", project_path="/p")
    _time.sleep(0.01)
    s.record_thread(codex_thread_id="new", channel_id="C", project_path="/p")
    threads = s.list_threads("C")
    assert [t.codex_thread_id for t in threads] == ["new", "old"]


def test_list_threads_scoped_to_channel(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.record_thread(codex_thread_id="a", channel_id="C1", project_path="/p")
    s.record_thread(codex_thread_id="b", channel_id="C2", project_path="/p")
    assert {t.codex_thread_id for t in s.list_threads("C1")} == {"a"}
    assert {t.codex_thread_id for t in s.list_threads("C2")} == {"b"}


def test_list_threads_respects_limit(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    for i in range(20):
        s.record_thread(codex_thread_id=f"t-{i}", channel_id="C", project_path="/p")
    threads = s.list_threads("C", limit=5)
    assert len(threads) == 5
