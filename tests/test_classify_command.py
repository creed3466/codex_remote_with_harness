"""Tests for command classification (Claude Code style output)."""

from __future__ import annotations

import pytest

from codex_rc.notif_router import classify_command, command_summary


@pytest.mark.parametrize(
    "cmd,expected_icon,expected_label",
    [
        ("/bin/zsh -lc 'sed -n 1,260p scripts/foo.py'", "📖", "read"),
        ("cat README.md", "📖", "read"),
        ("/bin/zsh -lc 'ls -la'", "📖", "read"),
        ("/bin/zsh -lc 'rg -n foo src/'", "📖", "read"),
        ("git log --oneline", "🌿", "git"),
        ("git diff HEAD~1", "🌿", "git"),
        ("/bin/zsh -lc 'pytest -q'", "🧪", "test"),
        ("npm test", "🧪", "test"),
        ("make build", "🔨", "build"),
        ("cargo build --release", "🔨", "build"),
        ("apply_patch < diff.patch", "✏️", "wrote"),
        ("/bin/zsh -lc 'sed -i s/foo/bar/ file.txt'", "✏️", "wrote"),
        ("rm -rf node_modules", "📁", "fs"),
        ("mkdir -p build/", "📁", "fs"),
        ("python custom_script.py", "🔧", "exec"),
    ],
)
def test_classify(cmd: str, expected_icon: str, expected_label: str) -> None:
    icon, label = classify_command(cmd)
    assert icon == expected_icon
    assert label == expected_label


def test_summary_read_extracts_path() -> None:
    s = command_summary("sed -n '1,260p' scripts/foo.py", "read")
    assert s == "scripts/foo.py"


def test_summary_read_strips_zsh_wrapper() -> None:
    s = command_summary("/bin/zsh -lc 'sed -n 1,260p scripts/foo.py'", "read")
    assert s == "scripts/foo.py"


def test_summary_git_keeps_subcommand() -> None:
    s = command_summary("git log --oneline -n 20", "git")
    assert s.startswith("git log")


def test_summary_exec_truncates_long_input() -> None:
    long_cmd = "echo " + "x" * 200
    s = command_summary(long_cmd, "exec")
    assert len(s) <= 81  # 80 + "…"
    assert s.endswith("…")
