"""Tests for the workflow filesystem store.

``WorkflowFileStore`` owns the on-disk layout under ``data/workflow/``:
each active workflow gets its own directory, stage handoff markdown files
live inside, and finished workflows move to ``_archive/`` so they can be
inspected later or garbage-collected by retention age.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codex_rc.workflow_store import (
    WORKFLOW_DIR_RELPATH,
    WorkflowFileStore,
    WorkflowMeta,
    workflow_handoff_relpath,
)


def test_workflow_handoff_relpath_matches_layout_convention() -> None:
    """The cwd-relative path embedded in prompts must match the on-disk
    layout the store uses, otherwise codex writes to a place the bot
    never looks at (and vice versa)."""
    assert (
        workflow_handoff_relpath("wf-abc", stage=2)
        == f"{WORKFLOW_DIR_RELPATH}/wf-abc/stage_2_handoff.md"
    )


@pytest.fixture
def store(tmp_path: Path) -> WorkflowFileStore:
    return WorkflowFileStore(root=tmp_path)


def test_create_workflow_provisions_directory_and_meta(store: WorkflowFileStore) -> None:
    path = store.create("wf-001", channel_id="C1", main_thread_id="thr-main")

    assert path.exists() and path.is_dir()
    meta = store.read_meta("wf-001")
    assert meta is not None
    assert meta.workflow_id == "wf-001"
    assert meta.channel_id == "C1"
    assert meta.main_thread_id == "thr-main"
    assert meta.created_at_ms > 0
    assert meta.completed_at_ms is None
    assert meta.current_stage == 1
    assert meta.ephemeral_thread_ids == ()


def test_create_workflow_rejects_duplicate(store: WorkflowFileStore) -> None:
    store.create("wf-dup", channel_id="C1", main_thread_id="thr")
    with pytest.raises(FileExistsError):
        store.create("wf-dup", channel_id="C1", main_thread_id="thr")


def test_write_and_read_handoff_round_trip(store: WorkflowFileStore) -> None:
    store.create("wf-002", channel_id="C1", main_thread_id="thr")
    store.write_handoff("wf-002", stage=1, content="# Stage 1\n\n## Task\nhi")

    body = store.read_handoff("wf-002", stage=1)
    assert body is not None
    assert "## Task" in body
    assert "hi" in body


def test_read_handoff_returns_none_when_missing(store: WorkflowFileStore) -> None:
    store.create("wf-003", channel_id="C1", main_thread_id="thr")
    assert store.read_handoff("wf-003", stage=2) is None


def test_handoff_path_lives_inside_workflow_directory(store: WorkflowFileStore) -> None:
    store.create("wf-004", channel_id="C1", main_thread_id="thr")
    path = store.handoff_path("wf-004", stage=3)
    assert path.name == "stage_3_handoff.md"
    assert path.parent.name == "wf-004"


def test_update_meta_persists_changes(store: WorkflowFileStore) -> None:
    store.create("wf-005", channel_id="C1", main_thread_id="thr")
    store.update_meta(
        "wf-005",
        current_stage=3,
        ephemeral_thread_ids=("eph-a", "eph-b"),
    )

    meta = store.read_meta("wf-005")
    assert meta is not None
    assert meta.current_stage == 3
    assert meta.ephemeral_thread_ids == ("eph-a", "eph-b")


def test_archive_moves_workflow_dir_under_archive_with_timestamp(
    store: WorkflowFileStore,
) -> None:
    store.create("wf-006", channel_id="C1", main_thread_id="thr")
    store.write_handoff("wf-006", stage=1, content="x")

    archived = store.archive("wf-006", outcome="completed")

    assert archived.exists() and archived.is_dir()
    assert archived.parent.name == "_archive"
    assert "wf-006" in archived.name
    # Original active dir gone
    assert not (store.root / "wf-006").exists()
    # Meta updated with outcome
    meta_path = archived / "meta.json"
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    assert raw["outcome"] == "completed"
    assert raw["completed_at_ms"] is not None


def test_archive_unknown_workflow_raises(store: WorkflowFileStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.archive("wf-missing", outcome="cancelled")


def test_gc_archive_removes_old_directories(
    store: WorkflowFileStore, tmp_path: Path
) -> None:
    # Create + archive two workflows, then back-date one of them.
    store.create("wf-old", channel_id="C1", main_thread_id="thr")
    store.archive("wf-old", outcome="completed")
    store.create("wf-new", channel_id="C1", main_thread_id="thr")
    store.archive("wf-new", outcome="completed")

    old_dir = next((store.root / "_archive").glob("*wf-old*"))
    new_dir = next((store.root / "_archive").glob("*wf-new*"))

    # Back-date the old archive's mtime to 30 days ago.
    long_ago = time.time() - 30 * 24 * 3600
    import os

    os.utime(old_dir, (long_ago, long_ago))

    removed = store.gc_archive(retention_days=7)

    assert old_dir not in (store.root / "_archive").iterdir()
    assert new_dir.exists()
    assert removed == [old_dir]


def test_list_active_returns_only_unarchived_workflows(
    store: WorkflowFileStore,
) -> None:
    store.create("wf-active-1", channel_id="C1", main_thread_id="thr")
    store.create("wf-active-2", channel_id="C2", main_thread_id="thr")
    store.create("wf-done", channel_id="C3", main_thread_id="thr")
    store.archive("wf-done", outcome="completed")

    active = sorted(store.list_active())
    assert active == ["wf-active-1", "wf-active-2"]


def test_meta_round_trip_preserves_optional_fields(tmp_path: Path) -> None:
    """``WorkflowMeta`` must survive a write/read with full fidelity."""
    store = WorkflowFileStore(root=tmp_path)
    store.create("wf-meta", channel_id="C-meta", main_thread_id="thr-meta")
    store.update_meta(
        "wf-meta",
        current_stage=4,
        ephemeral_thread_ids=("e1", "e2", "e3"),
        last_error="boom",
    )

    meta = store.read_meta("wf-meta")
    assert meta is not None
    assert meta.current_stage == 4
    assert meta.ephemeral_thread_ids == ("e1", "e2", "e3")
    assert meta.last_error == "boom"
