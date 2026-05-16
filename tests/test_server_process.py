"""Tests for CodexServerProcess: log shape, lifecycle, integration."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_rc.server_process import CodexServerProcess, _prune_error_logs


def test_prune_error_logs_only_removes_old_operational_jsonl(tmp_path: Path) -> None:
    errors = tmp_path / "logs" / "errors"
    errors.mkdir(parents=True)
    debug = tmp_path / "debug" / "rpc" / "C1" / "2026-04-01"
    debug.mkdir(parents=True)
    old = errors / "2026-04-01.jsonl"
    recent = errors / "2026-05-13.jsonl"
    current = errors / "2026-05-14.jsonl"
    not_jsonl = errors / "2026-04-01.txt"
    debug_trace = debug / "events.jsonl"
    for path in (old, recent, current, not_jsonl, debug_trace):
        path.write_text("{}\n", encoding="utf-8")

    now = datetime(2026, 5, 14, 12, tzinfo=UTC).timestamp()
    _prune_error_logs(current, retention_days=7, now=now)

    assert not old.exists()
    assert recent.exists()
    assert current.exists()
    assert not_jsonl.exists()
    assert debug_trace.exists()


async def test_logs_initialize_and_notifications(
    tmp_path: Path, fake_server_script: Path
) -> None:
    log_dir = tmp_path / "logs"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    env = dict(os.environ)
    env["FAKE_RPC_CONFIG"] = json.dumps(
        {
            "init_result": {"userAgent": "fake/1.0", "codexHome": "/x"},
            "post_init_notifications": [
                {"method": "remoteControl/status/changed",
                 "params": {"status": "disabled"}},
            ],
            "handlers": {"echo": {"result": {"value": 7}}},
        }
    )

    proc = CodexServerProcess(
        project_path=project_dir,
        log_dir=log_dir,
        codex_bin=sys.executable,
        codex_args=(str(fake_server_script),),
        env=env,
        event_log_mode="debug",
    )
    async with proc:
        # give the notification logger a beat
        for _ in range(50):
            if proc.pretty_path.exists() and "remoteControl" in proc.pretty_path.read_text():
                break
            await asyncio.sleep(0.02)
        result = await proc.request("echo")
        assert result == {"value": 7}

    jsonl_lines = [
        json.loads(line)
        for line in proc.jsonl_path.read_text().splitlines()
        if line
    ]
    kinds = [r["kind"] for r in jsonl_lines]
    assert "system" in kinds  # starting + initialized
    assert "notif" in kinds  # remoteControl/status/changed
    assert any(r["kind"] == "req" and r["method"] == "echo" for r in jsonl_lines)
    assert any(r["kind"] == "resp" and r["method"] == "echo" for r in jsonl_lines)

    pretty = proc.pretty_path.read_text()
    assert "remoteControl/status/changed" in pretty
    assert "echo" in pretty
    assert "system" in pretty


async def test_request_error_logged_and_raised(
    tmp_path: Path, fake_server_script: Path
) -> None:
    log_dir = tmp_path / "logs"
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    env = dict(os.environ)
    env["FAKE_RPC_CONFIG"] = json.dumps(
        {"handlers": {"explode": {"error": {"code": -32001, "message": "boom"}}}}
    )

    proc = CodexServerProcess(
        project_path=project_dir,
        log_dir=log_dir,
        codex_bin=sys.executable,
        codex_args=(str(fake_server_script),),
        env=env,
        event_log_mode="debug",
    )
    async with proc:
        from codex_rc.rpc_client import RpcError

        with pytest.raises(RpcError):
            await proc.request("explode")

    text = proc.pretty_path.read_text()
    assert "← err" in text and "[-32001]" in text


async def test_errors_mode_records_only_redacted_failures(
    tmp_path: Path, fake_server_script: Path
) -> None:
    log_dir = tmp_path / "debug"
    error_log = tmp_path / "logs" / "errors" / "today.jsonl"
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    env = dict(os.environ)
    env["FAKE_RPC_CONFIG"] = json.dumps(
        {
            "handlers": {
                "ok": {"result": {"value": 7}},
                "explode": {"error": {"code": -32001, "message": "boom"}},
            }
        }
    )

    proc = CodexServerProcess(
        project_path=project_dir,
        log_dir=log_dir,
        error_log_path=error_log,
        codex_bin=sys.executable,
        codex_args=(str(fake_server_script),),
        env=env,
    )
    async with proc:
        assert await proc.request("ok", {"text": "do not persist"}) == {"value": 7}
        assert not proc.jsonl_path.exists()
        assert not proc.pretty_path.exists()

        from codex_rc.rpc_client import RpcError

        with pytest.raises(RpcError):
            await proc.request("explode", {"text": "do not persist"})

    lines = [json.loads(line) for line in error_log.read_text().splitlines()]
    assert lines == [
        {
            "t": lines[0]["t"],
            "kind": "resp_error",
            "method": "explode",
            "code": -32001,
            "message": "boom",
        }
    ]
    assert "do not persist" not in error_log.read_text()


async def test_off_mode_writes_no_event_files(
    tmp_path: Path, fake_server_script: Path
) -> None:
    log_dir = tmp_path / "debug"
    error_log = tmp_path / "logs" / "errors" / "today.jsonl"
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    env = dict(os.environ)
    env["FAKE_RPC_CONFIG"] = json.dumps(
        {"handlers": {"explode": {"error": {"code": -32001, "message": "boom"}}}}
    )

    proc = CodexServerProcess(
        project_path=project_dir,
        log_dir=log_dir,
        error_log_path=error_log,
        codex_bin=sys.executable,
        codex_args=(str(fake_server_script),),
        env=env,
        event_log_mode="off",
    )
    async with proc:
        from codex_rc.rpc_client import RpcError

        with pytest.raises(RpcError):
            await proc.request("explode")

    assert not proc.jsonl_path.exists()
    assert not proc.pretty_path.exists()
    assert not error_log.exists()


async def test_missing_project_path_rejected(
    tmp_path: Path, fake_server_script: Path
) -> None:
    log_dir = tmp_path / "logs"
    bad_project = tmp_path / "does-not-exist"

    proc = CodexServerProcess(
        project_path=bad_project,
        log_dir=log_dir,
        codex_bin=sys.executable,
        codex_args=(str(fake_server_script),),
        event_log_mode="debug",
    )
    with pytest.raises(FileNotFoundError):
        await proc.start()


@pytest.mark.integration
async def test_integration_against_real_codex(tmp_path: Path) -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not on PATH")

    log_dir = tmp_path / "logs"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    proc = CodexServerProcess(
        project_path=project_dir,
        log_dir=log_dir,
        event_log_mode="debug",
    )
    async with proc:
        await asyncio.sleep(0.5)  # let remoteControl notif arrive
    pretty = proc.pretty_path.read_text()
    assert "remoteControl/status/changed" in pretty
    assert "initialized" in pretty


@pytest.mark.integration
async def test_integration_ws_mode_against_real_codex(tmp_path: Path) -> None:
    """Spawn codex with --listen ws://, connect from Python, verify the
    same lifecycle works. The ws_url should be exposed so a tmux pane
    could run `codex --remote <url>` to join the same backend."""
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not on PATH")

    log_dir = tmp_path / "logs"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    proc = CodexServerProcess(
        project_path=project_dir,
        log_dir=log_dir,
        transport_mode="ws",
        event_log_mode="debug",
    )
    async with proc:
        assert proc.ws_url is not None
        assert proc.ws_url.startswith("ws://127.0.0.1:")
        assert proc.ws_port > 0
        await asyncio.sleep(0.5)
    pretty = proc.pretty_path.read_text()
    assert "remoteControl/status/changed" in pretty
    assert "initialized" in pretty
    assert '"transport":"ws"' in pretty
