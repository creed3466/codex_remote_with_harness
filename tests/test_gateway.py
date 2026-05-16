from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codex_rc import gateway


def test_launchd_plist_points_at_project_without_persisting_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "CODEX_RC_DISCORD_TOKEN=super-secret-token\n",
        encoding="utf-8",
    )

    raw = gateway.build_launchd_plist(tmp_path)
    payload = plistlib.loads(raw.encode("utf-8"))

    assert payload["Label"] == gateway.SERVICE_LABEL
    assert payload["WorkingDirectory"] == str(tmp_path)
    assert payload["ProgramArguments"] == gateway.gateway_argv(tmp_path)
    assert "super-secret-token" not in raw
    assert "CODEX_RC_DISCORD_TOKEN" not in raw


def test_systemd_unit_points_at_project_without_persisting_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "CODEX_RC_DISCORD_TOKEN=super-secret-token\n",
        encoding="utf-8",
    )

    unit = gateway.build_systemd_unit(tmp_path)

    assert "Description=codex_rc Discord gateway" in unit
    assert f'WorkingDirectory="{tmp_path}"' in unit
    assert "python" in unit or sys.executable in unit
    assert "super-secret-token" not in unit
    assert "CODEX_RC_DISCORD_TOKEN" not in unit


def test_doctor_reads_env_without_printing_secret(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("CODEX_RC_DISCORD_TOKEN=\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "CODEX_RC_DISCORD_TOKEN=tok\n"
        "CODEX_RC_DISCORD_APP_ID=app\n"
        "CODEX_RC_ALLOWED_USER_IDS=uid\n",
        encoding="utf-8",
    )

    checks = gateway.collect_doctor_checks(tmp_path, environ={})
    by_name = {check.name: check for check in checks}

    assert by_name[".env"].ok is True
    assert by_name["CODEX_RC_DISCORD_TOKEN"].ok is True
    assert "tok" not in "\n".join(check.detail for check in checks)


def test_service_spec_selects_platform_paths(tmp_path: Path) -> None:
    launchd = gateway.service_spec(tmp_path, platform="darwin")
    systemd = gateway.service_spec(tmp_path, platform="linux")

    assert launchd.manager == "launchd"
    assert launchd.path.name.endswith(".plist")
    assert systemd.manager == "systemd"
    assert systemd.path.name == gateway.SYSTEMD_UNIT_NAME


def test_install_service_refuses_to_replace_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(gateway.Path, "home", lambda *_: home)
    monkeypatch.setattr(gateway.sys, "platform", "darwin")

    installed = gateway.install_service(project)
    assert installed.exists()

    with pytest.raises(FileExistsError):
        gateway.install_service(project)

    gateway.install_service(project, force=True)


def _install_fake_service_file(tmp_path: Path, name: str) -> Path:
    """Stand up an empty placeholder so request_self_restart's
    install-check thinks the service manager unit is registered on
    disk. Tests still have to fake the manager-probe returncode."""
    target = tmp_path / name
    target.write_text("")
    return target


def _fake_loaded_run(returncode: int = 0):
    """Build a fake ``subprocess.run`` that pretends the manager probe
    returned ``returncode``. Default 0 → service is loaded."""

    def _run(args, check=False, capture_output=False, **_kw):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = b""
        result.stderr = b""
        return result

    return _run


def test_request_self_restart_uses_launchd_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    monkeypatch.setattr(gateway.os, "getuid", lambda: 501)
    fake_plist = _install_fake_service_file(tmp_path, "dev.codex_rc.gateway.plist")
    monkeypatch.setattr(gateway, "_launchd_path", lambda: fake_plist)
    monkeypatch.setattr(gateway.subprocess, "run", _fake_loaded_run(0))

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        gateway.subprocess,
        "Popen",
        lambda args, stdout, stderr, start_new_session: captured.update(
            {"args": list(args)}
        ) or MagicMock(),
    )

    gateway.request_self_restart(delay_seconds=0.0)

    assert captured["args"] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/dev.codex_rc.gateway",
    ]


def test_request_self_restart_uses_systemd_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    fake_unit = _install_fake_service_file(tmp_path, "codex-rc-gateway.service")
    monkeypatch.setattr(gateway, "_systemd_path", lambda: fake_unit)
    monkeypatch.setattr(gateway.subprocess, "run", _fake_loaded_run(0))

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        gateway.subprocess,
        "Popen",
        lambda args, stdout, stderr, start_new_session: captured.update(
            {"args": list(args)}
        ) or MagicMock(),
    )

    gateway.request_self_restart(delay_seconds=0.0)

    assert captured["args"] == [
        "systemctl",
        "--user",
        "restart",
        gateway.SYSTEMD_UNIT_NAME,
    ]


def test_request_self_restart_raises_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="supported only"):
        gateway.request_self_restart(delay_seconds=0.0)


def test_request_self_restart_raises_when_launchd_unit_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 2026-05-15 incident #1: ``/codex restart`` killed the bot
    because ``launchctl kickstart`` silently failed when the plist
    wasn't installed. With no supervisor, the bot stayed dead. The fix
    raises a typed error so the button handler can skip ``os._exit``
    and surface a clear Discord message instead.
    """
    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    monkeypatch.setattr(gateway.os, "getuid", lambda: 501)
    monkeypatch.setattr(gateway, "_launchd_path", lambda: tmp_path / "absent.plist")

    popen = MagicMock()
    monkeypatch.setattr(gateway.subprocess, "Popen", popen)

    with pytest.raises(gateway.GatewayNotInstalled, match="not installed"):
        gateway.request_self_restart(delay_seconds=0.0)

    popen.assert_not_called()


def test_request_self_restart_raises_when_systemd_unit_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    monkeypatch.setattr(gateway, "_systemd_path", lambda: tmp_path / "absent.service")

    popen = MagicMock()
    monkeypatch.setattr(gateway.subprocess, "Popen", popen)

    with pytest.raises(gateway.GatewayNotInstalled, match="not installed"):
        gateway.request_self_restart(delay_seconds=0.0)

    popen.assert_not_called()


def test_request_self_restart_raises_when_launchd_plist_present_but_not_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 2026-05-15 incident #2: after my first fix, the agent ran
    ``codex-rc-gateway install`` which wrote the plist but never
    bootstrapped it into launchd. The next ``/codex restart`` click
    passed the file-exists check, fired ``launchctl kickstart`` which
    113'd silently (``Could not find service``), and ``os._exit``
    still killed the bot. The real check has to query the manager,
    not the disk.
    """
    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    monkeypatch.setattr(gateway.os, "getuid", lambda: 501)
    plist = _install_fake_service_file(tmp_path, "dev.codex_rc.gateway.plist")
    monkeypatch.setattr(gateway, "_launchd_path", lambda: plist)
    # ``launchctl print`` returns 113 when the plist exists but isn't loaded.
    monkeypatch.setattr(gateway.subprocess, "run", _fake_loaded_run(113))

    popen = MagicMock()
    monkeypatch.setattr(gateway.subprocess, "Popen", popen)

    with pytest.raises(gateway.GatewayNotInstalled, match="not loaded"):
        gateway.request_self_restart(delay_seconds=0.0)

    popen.assert_not_called()


def test_request_self_restart_raises_when_systemd_unit_present_but_inactive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    unit = _install_fake_service_file(tmp_path, "codex-rc-gateway.service")
    monkeypatch.setattr(gateway, "_systemd_path", lambda: unit)
    # ``systemctl is-active`` returns 3 when the unit is inactive.
    monkeypatch.setattr(gateway.subprocess, "run", _fake_loaded_run(3))

    popen = MagicMock()
    monkeypatch.setattr(gateway.subprocess, "Popen", popen)

    with pytest.raises(gateway.GatewayNotInstalled, match="not loaded"):
        gateway.request_self_restart(delay_seconds=0.0)

    popen.assert_not_called()
