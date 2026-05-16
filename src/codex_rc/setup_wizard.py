"""Interactive ``codex-rc`` setup wizard.

Creates a ``.env`` file in the current working directory by copying
``.env.example`` (looked up next to the package) and prompting for the
required Discord values. Existing ``.env`` is never overwritten without
explicit confirmation.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TextIO

REQUIRED_PROMPTS: tuple[tuple[str, str, str | None], ...] = (
    (
        "CODEX_RC_DISCORD_TOKEN",
        "Discord bot token (Developer Portal → Bot → Reset Token)",
        None,
    ),
    (
        "CODEX_RC_DISCORD_APP_ID",
        "Discord application ID (Developer Portal → General Information)",
        None,
    ),
    (
        "CODEX_RC_ALLOWED_USER_IDS",
        "Your Discord user ID (the only ID allowed to drive codex)",
        None,
    ),
)

OPTIONAL_PROMPTS: tuple[tuple[str, str, str | None], ...] = (
    (
        "CODEX_RC_DISCORD_GUILD_ID",
        "Guild ID for instant slash-command registration (optional, blank to skip)",
        "",
    ),
)


def _find_example(start: Path) -> Path | None:
    candidates = [
        start / ".env.example",
        Path(__file__).resolve().parent.parent.parent / ".env.example",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _prompt(label: str, default: str | None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    return raw


def _write_env(env_path: Path, values: dict[str, str], template: str) -> None:
    out_lines: list[str] = []
    keys_remaining = set(values)
    for line in template.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            out_lines.append(f"{key}={values[key]}")
            keys_remaining.discard(key)
        else:
            out_lines.append(line)
    # Append any new keys that the template did not already contain.
    for k in sorted(keys_remaining):
        out_lines.append(f"{k}={values[k]}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def run(cwd: Path | None = None, stdout: TextIO = sys.stdout) -> int:
    """Execute the wizard. Returns a process exit code."""
    here = (cwd or Path.cwd()).resolve()
    env_path = here / ".env"
    example = _find_example(here)
    if example is None:
        print(
            "could not find .env.example next to the package — "
            "please clone the repo or download it manually",
            file=sys.stderr,
        )
        return 2

    template = example.read_text(encoding="utf-8")

    if env_path.exists():
        ans = input(f"{env_path} already exists. Overwrite? [y/N]: ").strip().lower()
        if ans not in {"y", "yes"}:
            print("aborted; nothing changed.", file=stdout)
            return 0
        shutil.copy(env_path, env_path.with_name(".env.bak"))
        print(f"backed up existing config to {env_path.with_suffix('.env.bak')}", file=stdout)

    print("\ncodex_rc setup — fill in the required Discord values.\n", file=stdout)

    values: dict[str, str] = {}
    for key, label, default in REQUIRED_PROMPTS:
        existing = os.environ.get(key, "")
        v = _prompt(label, existing or default)
        if not v:
            print(f"{key} is required; aborting.", file=sys.stderr)
            return 1
        values[key] = v

    for key, label, default in OPTIONAL_PROMPTS:
        existing = os.environ.get(key, "")
        v = _prompt(label, existing or default)
        if v:
            values[key] = v

    _write_env(env_path, values, template)
    print(f"\nwrote {env_path} (chmod 0600).", file=stdout)
    print("next: `codex-rc-discord` to launch the bot.", file=stdout)
    return 0


def main() -> None:  # pragma: no cover — console script
    raise SystemExit(run())


__all__ = ["run", "main"]
