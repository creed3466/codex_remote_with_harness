from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from codex_rc import gateway


def test_build_discord_invite_url() -> None:
    url = gateway.build_discord_invite_url("1234567890")

    assert "https://discord.com/oauth2/authorize?" in url
    assert "client_id=1234567890" in url
    assert "permissions=85056" in url
    assert "scope=bot%20applications.commands" in url


def test_invite_url_from_env_reads_application_id(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "CODEX_RC_DISCORD_APP_ID=1234567890\n",
        encoding="utf-8",
    )

    url = gateway.invite_url_from_env(tmp_path)
    assert url is not None
    assert "client_id=1234567890" in url


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


def test_doctor_can_check_codex_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "CODEX_RC_DISCORD_TOKEN=tok\n"
        "CODEX_RC_DISCORD_APP_ID=app\n"
        "CODEX_RC_ALLOWED_USER_IDS=uid\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway.shutil, "which", lambda _name: "/usr/bin/codex")

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(["codex"], 0, stdout="private", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)

    checks = gateway.collect_doctor_checks(
        tmp_path, environ={}, check_codex_login=True
    )
    by_name = {check.name: check for check in checks}

    assert by_name["codex login"].ok is True
    assert by_name["codex login"].detail == "logged in"


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
