"""Unit tests for the codex-rc setup wizard."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from codex_rc import setup_wizard


@pytest.fixture(autouse=True)
def _clear_setup_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, *_ in setup_wizard.REQUIRED_PROMPTS + setup_wizard.OPTIONAL_PROMPTS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def example_template(tmp_path: Path) -> Path:
    src = tmp_path / ".env.example"
    src.write_text(
        "# header\n"
        "CODEX_RC_DISCORD_TOKEN=\n"
        "CODEX_RC_DISCORD_APP_ID=\n"
        "CODEX_RC_ALLOWED_USER_IDS=\n"
        "# block\n"
        "CODEX_RC_DISCORD_GUILD_ID=\n"
        "CODEX_RC_DEFAULT_SANDBOX=workspace-write\n",
        encoding="utf-8",
    )
    return src


def _stub_inputs(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(it))


def test_run_creates_env_with_required_values(
    tmp_path: Path, example_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_inputs(
        monkeypatch,
        ["tok-abc", "app-123", "271656041958080518", ""],  # guild blank
    )
    code = setup_wizard.run(cwd=tmp_path, stdout=io.StringIO())
    assert code == 0
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CODEX_RC_DISCORD_TOKEN=tok-abc" in env
    assert "CODEX_RC_DISCORD_APP_ID=app-123" in env
    assert "CODEX_RC_ALLOWED_USER_IDS=271656041958080518" in env
    assert "CODEX_RC_DISCORD_GUILD_ID=\n" in env or "CODEX_RC_DISCORD_GUILD_ID=" in env
    # Preserves comments and unrelated keys.
    assert "# header" in env
    assert "CODEX_RC_DEFAULT_SANDBOX=workspace-write" in env


def test_run_guided_prints_invite_and_runs_doctor(
    tmp_path: Path,
    example_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_wizard, "_codex_login_hint", lambda: "ok")
    _stub_inputs(monkeypatch, ["tok-abc", "1234567890", "111", ""])

    from codex_rc import gateway

    called: dict[str, object] = {}

    def fake_print_doctor(cwd: Path, *, fix: bool = False) -> int:
        called["cwd"] = cwd
        called["fix"] = fix
        return 0

    monkeypatch.setattr(gateway, "print_doctor", fake_print_doctor)
    out = io.StringIO()

    code = setup_wizard.run_guided(cwd=tmp_path, stdout=out)

    assert code == 0
    assert called == {"cwd": tmp_path, "fix": True}
    text = out.getvalue()
    assert "codex_rc guided setup" in text
    assert "discord.com/oauth2/authorize" in text
    assert "client_id=1234567890" in text


def test_run_writes_optional_guild_when_provided(
    tmp_path: Path, example_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_inputs(monkeypatch, ["t", "a", "u", "9876543210"])
    setup_wizard.run(cwd=tmp_path, stdout=io.StringIO())
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CODEX_RC_DISCORD_GUILD_ID=9876543210" in env


def test_run_refuses_empty_required(
    tmp_path: Path, example_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_inputs(monkeypatch, [""])
    code = setup_wizard.run(cwd=tmp_path, stdout=io.StringIO())
    assert code == 1
    assert not (tmp_path / ".env").exists()


def test_run_aborts_when_env_exists_and_user_declines(
    tmp_path: Path, example_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("CODEX_RC_DISCORD_TOKEN=keep\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _stub_inputs(monkeypatch, ["n"])
    code = setup_wizard.run(cwd=tmp_path, stdout=io.StringIO())
    assert code == 0
    # Untouched.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "CODEX_RC_DISCORD_TOKEN=keep\n"


def test_run_backs_up_when_overwriting(
    tmp_path: Path, example_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("CODEX_RC_DISCORD_TOKEN=old\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _stub_inputs(monkeypatch, ["y", "new", "app", "uid", ""])
    code = setup_wizard.run(cwd=tmp_path, stdout=io.StringIO())
    assert code == 0
    backup = tmp_path / ".env.bak"
    # NOTE: with_name(".env.bak") → keeps directory, replaces name entirely
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "CODEX_RC_DISCORD_TOKEN=old\n"
    assert "CODEX_RC_DISCORD_TOKEN=new" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_run_uses_embedded_template_when_env_example_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_wizard, "_find_example", lambda _start: None)
    _stub_inputs(monkeypatch, ["tok", "app", "uid", ""])

    code = setup_wizard.run(cwd=tmp_path, stdout=io.StringIO())
    assert code == 0
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CODEX_RC_DISCORD_TOKEN=tok" in env
    assert "CODEX_RC_TRANSPORT=ws" in env
