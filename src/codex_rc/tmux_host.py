"""Thin libtmux facade for codex_rc observability.

One Discord channel maps to one tmux session that can surface the Codex TUI,
an enabled local debug/error tail, or both. Attach with::

    tmux attach -t codex-rc-<project>

Everything else (approval, control, replies) flows through Discord.

tmux reserves ``:`` and ``.`` as session/window separators, so the prefix uses
``-``.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

import libtmux
from libtmux.constants import PaneDirection

SESSION_PREFIX = "codex-rc-"


def session_name_for(project_slug: str) -> str:
    if any(c in project_slug for c in (":", ".")):
        raise ValueError("project_slug must not contain ':' or '.'")
    return f"{SESSION_PREFIX}{project_slug}"


@dataclass(slots=True)
class TmuxSessionInfo:
    name: str
    cwd: Path
    tail_path: Path | None
    tui_cmd: str | None = None  # command running in pane A when in dual-pane mode


def session_exists(name: str, *, server: libtmux.Server | None = None) -> bool:
    srv = server or libtmux.Server()
    return any(s.name == name for s in srv.sessions)


def create_tail_session(
    project_slug: str,
    cwd: Path,
    tail_path: Path | None,
    *,
    server: libtmux.Server | None = None,
    tui_cmd: str | None = None,
) -> TmuxSessionInfo:
    """Create (or reuse) a tmux session named ``codex-rc-<slug>``.

    The session opens one window in ``cwd``. If ``tui_cmd`` is None, the
    window has a single pane running ``tail -F <tail_path>``. If
    ``tui_cmd`` is provided (typically ``codex --remote ws://…``), the
    window is split horizontally when ``tail_path`` is also provided:

    * pane A (left, ~70%) — runs ``tui_cmd`` so the human can chat with
      the same Codex backend.
    * pane B (right, ~30%) — runs ``tail -F <tail_path>`` so the enabled
      debug/error log is visible alongside.

    ``-F`` (not ``-f``) so tail survives log rotation.
    """
    srv = server or libtmux.Server()
    name = session_name_for(project_slug)
    cwd = cwd.resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    if tail_path is not None:
        tail_path.parent.mkdir(parents=True, exist_ok=True)
        tail_path.touch(exist_ok=True)

    if session_exists(name, server=srv):
        return TmuxSessionInfo(
            name=name, cwd=cwd, tail_path=tail_path, tui_cmd=tui_cmd
        )

    tail_cmd = f"tail -F {shlex.quote(str(tail_path))}" if tail_path else None
    primary_cmd = tui_cmd or tail_cmd or os.environ.get("SHELL", "/bin/sh")
    session = srv.new_session(
        session_name=name,
        start_directory=str(cwd),
        attach=False,
        window_command=primary_cmd,
    )
    if tui_cmd and tail_cmd:
        window = session.active_window
        pane = window.active_pane
        if pane is not None:
            # 30% wide split on the right running the tail.
            pane.split(
                direction=PaneDirection.Right,
                start_directory=str(cwd),
                percentage=30,
                shell=tail_cmd,
            )
    return TmuxSessionInfo(
        name=name, cwd=cwd, tail_path=tail_path, tui_cmd=tui_cmd
    )


def kill_session(
    project_slug: str, *, server: libtmux.Server | None = None
) -> bool:
    srv = server or libtmux.Server()
    name = session_name_for(project_slug)
    for sess in srv.sessions:
        if sess.name == name:
            sess.kill()
            return True
    return False


def attach_command(project_slug: str) -> str:
    """Return the shell command a human runs to watch this session."""
    return f"tmux attach -t {shlex.quote(session_name_for(project_slug))}"


__all__ = [
    "TmuxSessionInfo",
    "attach_command",
    "create_tail_session",
    "kill_session",
    "session_exists",
    "session_name_for",
]
