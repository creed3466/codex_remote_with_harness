"""Unit tests for :class:`Service` registry.

Patches :class:`CodexServerProcess` so no real codex subprocess is
spawned, and patches the tmux create call so libtmux doesn't run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_rc.service import Service
from codex_rc.session_store import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


@pytest.fixture
def post():
    async def _post(channel_id: str, payload: dict) -> None:
        pass
    return _post


@pytest.fixture
def fake_proc_factory():
    """Returns a function that builds a FakeProc-like mock for any project_path."""
    procs: list[Any] = []

    def _factory(
        *,
        project_path,
        log_dir,
        codex_bin,
        codex_args,
        transport_mode,
        event_log_mode="errors",
        error_log_path=None,
        error_log_retention_days=30,
    ):
        proc = MagicMock()
        proc.project_path = project_path
        proc.log_dir = log_dir
        proc.pretty_path = log_dir / "events.txt"
        proc.tail_path = proc.pretty_path if event_log_mode == "debug" else None
        proc.event_log_mode = event_log_mode
        proc.error_log_path = error_log_path
        proc.error_log_retention_days = error_log_retention_days
        proc.ws_url = None
        proc.client = MagicMock()
        proc.client.respond_to_request = AsyncMock()
        proc.set_server_request_handler = MagicMock()

        async def _request(method, params=None, **_kwargs):
            if method == "thread/start":
                return {"thread": {"id": f"thread-{len(procs) + 1}"}}
            return {}

        proc.request = AsyncMock(side_effect=_request)
        proc.start = AsyncMock(return_value={"userAgent": "fake/1.0"})
        proc.stop = AsyncMock()

        async def _no_notif():
            # Pump never yields — keeps the pump_task quietly parked.
            await asyncio.Event().wait()
            yield  # pragma: no cover
        proc.client.notifications = lambda: _no_notif()
        procs.append(proc)
        return proc

    _factory.procs = procs  # type: ignore[attr-defined]
    return _factory


@pytest.fixture
def patched_service_env(fake_proc_factory):
    """Patches the heavy module dependencies inside service.py."""
    with patch(
        "codex_rc.service.CodexServerProcess", side_effect=fake_proc_factory
    ), patch("codex_rc.service.create_tail_session", return_value=MagicMock()), \
            patch("codex_rc.service.kill_session", return_value=True):
        yield


def make_service(*, store: SessionStore, log_root: Path, post) -> Service:
    return Service(store=store, log_root=log_root, post=post)


# ====================================================================== tests

async def test_start_session_creates_channel_service(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    sess = await svc.start_session(channel_id="C1", project_path=str(project))
    assert sess.state == "running"
    assert svc.is_active("C1")
    assert len(fake_proc_factory.procs) == 1
    # Lifecycle methods called.
    fake_proc_factory.procs[0].start.assert_awaited_once()


async def test_status_text_includes_codex_usage_diagnostics(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(project))

    status = svc.status_text("C1")

    assert status is not None
    assert "diagnostics:" in status
    assert "codex `codex app-server`" in status
    assert "server `fake/1.0`" in status
    await svc.stop_session("C1")


async def test_capabilities_text_reports_app_server_usage(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(project))

    report = svc.capabilities_text("C1")

    assert "Codex CLI capability audit" in report
    assert "mode `app-server/threaded`, not per-turn `codex exec`" in report
    assert "App-server backend" in report
    assert "In-flight steering" in report
    assert "Approvals and sandbox" in report
    assert "MCP/plugin/skills inventory" in report
    await svc.stop_session("C1")


async def test_start_session_idempotent(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    sess1 = await svc.start_session(channel_id="C1", project_path=str(project))
    sess2 = await svc.start_session(channel_id="C1", project_path=str(project))
    assert sess1.channel_id == sess2.channel_id
    assert len(fake_proc_factory.procs) == 1  # not spawned twice


async def test_start_session_marks_error_on_proc_failure(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()

    def boom_factory(**kwargs):
        proc = fake_proc_factory(**kwargs)
        proc.start = AsyncMock(side_effect=RuntimeError("nope"))
        return proc

    with patch("codex_rc.service.CodexServerProcess", side_effect=boom_factory):
        with pytest.raises(RuntimeError, match="nope"):
            await svc.start_session(channel_id="C-err", project_path=str(project))
    sess = store.get("C-err")
    assert sess and sess.state == "error"
    assert not svc.is_active("C-err")


async def test_send_text_unknown_channel_raises_keyerror(
    store, post, tmp_path, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    with pytest.raises(KeyError, match="no active channel"):
        await svc.send_text("ghost", "hello")


async def test_stop_session_idempotent_for_unknown_channel(
    store, post, tmp_path, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    # No exception expected.
    await svc.stop_session("never-existed")


async def test_stop_session_marks_state_stopped(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(project))
    assert svc.is_active("C1")
    await svc.stop_session("C1")
    sess = store.get("C1")
    assert sess and sess.state == "stopped"
    assert not svc.is_active("C1")


async def test_start_session_expands_tilde_before_persisting(
    store, post, tmp_path, fake_proc_factory, patched_service_env, monkeypatch
) -> None:
    """~/foo should be expanded to the user's home so the path-equality
    checks (continue, idempotency) all see the same string."""
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    fake_home = tmp_path / "home_root"
    project = fake_home / "Project" / "codex_rc"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    await svc.start_session(
        channel_id="C1", project_path="~/Project/codex_rc"
    )
    sess = store.get("C1")
    assert sess is not None
    assert sess.project_path == str(fake_home / "Project" / "codex_rc")
    assert "~" not in sess.project_path


async def test_start_session_rejects_different_path_when_already_active(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    p1.mkdir()
    p2.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(p1))
    # Same path is a no-op (idempotent)
    sess_same = await svc.start_session(channel_id="C1", project_path=str(p1))
    assert sess_same.project_path == str(p1)
    # Different path explicitly errors instead of silently keeping the old one.
    with pytest.raises(RuntimeError, match="run /codex stop"):
        await svc.start_session(channel_id="C1", project_path=str(p2))


async def test_continue_session_restarts_with_last_path_and_injects_handoff(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(project))
    store.record_handoff(
        handoff_id="handoff-1",
        channel_id="C1",
        project_path=str(project),
        sandbox="workspace-write",
        approval="on-request",
        source_codex_thread_id="thread-abc",
        body="# Codex Handoff\n\nContinue here.",
    )
    await svc.stop_session("C1")
    assert not svc.is_active("C1")

    sess = await svc.continue_session("C1")
    assert sess.project_path == str(project)
    assert svc.is_active("C1")
    # Last proc should start a fresh thread and inject the saved handoff.
    methods = [c.args[0] for c in fake_proc_factory.procs[-1].request.call_args_list]
    assert "thread/start" in methods
    assert "thread/inject_items" in methods
    assert "thread/resume" not in methods


async def test_continue_session_idempotent_when_already_active(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    sess = await svc.start_session(channel_id="C1", project_path=str(project))
    again = await svc.continue_session("C1")
    assert again.project_path == sess.project_path
    # No new proc was constructed; only the original one.
    assert len(fake_proc_factory.procs) == 1


async def test_continue_session_raises_when_no_prior_record(
    store, post, tmp_path, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    with pytest.raises(RuntimeError, match="no previous session"):
        await svc.continue_session("nope")


async def test_shutdown_stops_every_active_channel(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    p1.mkdir()
    p2.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(p1))
    await svc.start_session(channel_id="C2", project_path=str(p2))
    assert svc.is_active("C1") and svc.is_active("C2")
    await svc.shutdown()
    assert not svc.is_active("C1")
    assert not svc.is_active("C2")
    for proc in fake_proc_factory.procs:
        proc.stop.assert_awaited()


async def test_shutdown_continues_when_one_channel_raises(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    p1.mkdir()
    p2.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(p1))
    await svc.start_session(channel_id="C2", project_path=str(p2))
    # First channel's proc raises mid-stop; second must still be torn down.
    fake_proc_factory.procs[0].stop = AsyncMock(side_effect=RuntimeError("boom"))
    await svc.shutdown()
    assert not svc.is_active("C1")
    assert not svc.is_active("C2")
    fake_proc_factory.procs[1].stop.assert_awaited()


async def test_handle_button_returns_false_when_no_active_channel(
    store, post, tmp_path, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    accepted = await svc.handle_button("ghost", "codex_rc:abc:accept")
    assert accepted is False


async def test_sweep_all_approvals_aggregates(
    store, post, tmp_path, fake_proc_factory, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    project = tmp_path / "p"
    project.mkdir()
    await svc.start_session(channel_id="C1", project_path=str(project))
    await svc.start_session(channel_id="C2", project_path=str(project))
    # Mock sweep_approvals on each ChannelService to return a count.
    svc._channels["C1"].sweep_approvals = AsyncMock(return_value=2)
    svc._channels["C2"].sweep_approvals = AsyncMock(return_value=3)
    total = await svc.sweep_all_approvals()
    assert total == 5


async def test_resume_thread_unknown_channel_raises_keyerror(
    store, post, tmp_path, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    with pytest.raises(KeyError, match="no active channel"):
        await svc.resume_thread("ghost", "thread-x")


async def test_new_thread_unknown_channel_raises_keyerror(
    store, post, tmp_path, patched_service_env
) -> None:
    svc = make_service(store=store, log_root=tmp_path / "logs", post=post)
    with pytest.raises(KeyError, match="no active channel"):
        await svc.new_thread("ghost")
