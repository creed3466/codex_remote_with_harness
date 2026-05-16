"""Tests for tmux_host. Skipped when tmux is not installed."""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


@pytest.fixture
def project_slug() -> str:
    # Random per-test, prefixed so we never collide with real sessions.
    return f"test-{uuid.uuid4().hex[:8]}"


def test_create_and_kill_session(tmp_path: Path, project_slug: str) -> None:
    from codex_rc.tmux_host import (
        create_tail_session,
        kill_session,
        session_exists,
        session_name_for,
    )

    tail = tmp_path / "events.txt"
    tail.write_text("preexisting line\n")

    info = create_tail_session(project_slug, tmp_path, tail)
    name = session_name_for(project_slug)
    try:
        assert info.name == name
        assert session_exists(name)
    finally:
        assert kill_session(project_slug)
    assert not session_exists(name)


def test_create_is_idempotent(tmp_path: Path, project_slug: str) -> None:
    from codex_rc.tmux_host import (
        create_tail_session,
        kill_session,
        session_exists,
        session_name_for,
    )

    tail = tmp_path / "events.txt"
    try:
        info1 = create_tail_session(project_slug, tmp_path, tail)
        info2 = create_tail_session(project_slug, tmp_path, tail)
        assert info1.name == info2.name == session_name_for(project_slug)
        assert session_exists(info1.name)
    finally:
        kill_session(project_slug)


def test_pane_tails_log_file(tmp_path: Path, project_slug: str) -> None:
    """Smoke: write to the log file and verify tmux pane sees it via capture-pane."""
    from codex_rc.tmux_host import (
        create_tail_session,
        kill_session,
        session_name_for,
    )

    tail = tmp_path / "events.txt"
    tail.write_text("")
    info = create_tail_session(project_slug, tmp_path, tail)
    name = session_name_for(project_slug)
    try:
        # Write a distinctive token; give tail -F a beat to pick it up.
        token = f"hello-codex-rc-{uuid.uuid4().hex[:6]}"
        with tail.open("a") as f:
            f.write(token + "\n")

        for _ in range(30):
            time.sleep(0.1)
            out = subprocess.run(
                ["tmux", "capture-pane", "-pt", name],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if token in out:
                break
        else:
            raise AssertionError(
                f"token {token!r} never appeared in pane; got:\n{out}"
            )
        assert info.tail_path == tail
    finally:
        kill_session(project_slug)


def test_kill_nonexistent_returns_false() -> None:
    from codex_rc.tmux_host import kill_session

    assert kill_session(f"nonexistent-{uuid.uuid4().hex[:6]}") is False


@pytest.mark.parametrize("bad", ["bad:slug", "bad.slug"])
def test_session_name_rejects_separators(bad: str) -> None:
    from codex_rc.tmux_host import session_name_for

    with pytest.raises(ValueError, match="must not contain"):
        session_name_for(bad)
