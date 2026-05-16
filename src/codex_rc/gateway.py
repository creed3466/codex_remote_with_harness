"""Gateway lifecycle CLI for codex_rc.

The gateway is the long-running Discord bridge. This module keeps the public
operator workflow small:

    codex-rc gateway discord
    codex-rc gateway doctor
    codex-rc gateway install
    codex-rc gateway start

Managed service files intentionally do not persist Discord tokens. They only
pin the project directory; the process loads secrets from that directory's
``.env`` file at runtime.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SERVICE_LABEL = "dev.codex_rc.gateway"
SYSTEMD_UNIT_NAME = "codex-rc-gateway.service"

REQUIRED_ENV_KEYS = (
    "CODEX_RC_DISCORD_TOKEN",
    "CODEX_RC_DISCORD_APP_ID",
    "CODEX_RC_ALLOWED_USER_IDS",
)


@dataclass(slots=True, frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(slots=True, frozen=True)
class ServiceSpec:
    manager: str
    path: Path
    content: str


def _project_dir(raw: str | Path | None = None) -> Path:
    return Path(raw or Path.cwd()).expanduser().resolve()


def gateway_argv(cwd: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "codex_rc.gateway",
        "run",
        "--cwd",
        str(cwd),
    ]


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _log_path(cwd: Path, name: str) -> str:
    return str(cwd / "data" / "logs" / name)


def build_launchd_plist(cwd: Path) -> str:
    payload = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": gateway_argv(cwd),
        "WorkingDirectory": str(cwd),
        "RunAtLoad": False,
        "KeepAlive": {"Crashed": True},
        "StandardOutPath": _log_path(cwd, "gateway.out.log"),
        "StandardErrorPath": _log_path(cwd, "gateway.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def _systemd_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_systemd_unit(cwd: Path) -> str:
    argv = " ".join(f'"{_systemd_quote(part)}"' for part in gateway_argv(cwd))
    return "\n".join(
        [
            "[Unit]",
            "Description=codex_rc Discord gateway",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f'WorkingDirectory="{_systemd_quote(str(cwd))}"',
            f"ExecStart={argv}",
            "Restart=on-failure",
            "RestartSec=5",
            "Environment=PYTHONUNBUFFERED=1",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def service_spec(cwd: Path, *, platform: str | None = None) -> ServiceSpec:
    platform = platform or sys.platform
    if platform == "darwin":
        return ServiceSpec(
            manager="launchd",
            path=_launchd_path(),
            content=build_launchd_plist(cwd),
        )
    if platform.startswith("linux"):
        return ServiceSpec(
            manager="systemd",
            path=_systemd_path(),
            content=build_systemd_unit(cwd),
        )
    raise RuntimeError(
        f"gateway service install is unsupported on {platform!r}; use "
        "`codex-rc gateway run` under your process manager"
    )


def install_service(cwd: Path, *, force: bool = False) -> Path:
    spec = service_spec(cwd)
    if spec.path.exists() and not force:
        raise FileExistsError(f"{spec.path} already exists; pass --force to replace it")
    (cwd / "data" / "logs").mkdir(parents=True, exist_ok=True)
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    spec.path.write_text(spec.content, encoding="utf-8")
    return spec.path


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def collect_doctor_checks(
    cwd: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[DoctorCheck]:
    env_path = cwd / ".env"
    file_env = _load_env_file(env_path)
    merged = dict(file_env)
    merged.update(dict(environ or os.environ))

    checks = [
        DoctorCheck(".env", env_path.exists(), str(env_path)),
        DoctorCheck(".env.example", (cwd / ".env.example").exists(), "template present"),
    ]
    for key in REQUIRED_ENV_KEYS:
        checks.append(DoctorCheck(key, bool(merged.get(key, "").strip()), "configured"))
    checks.extend(
        [
            DoctorCheck("codex", shutil.which("codex") is not None, "codex CLI on PATH"),
            DoctorCheck(
                "runtime directory",
                _can_create_dir(cwd / "data" / "state"),
                str(cwd / "data" / "state"),
            ),
        ]
    )
    return checks


def _can_create_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


def print_doctor(cwd: Path) -> int:
    checks = collect_doctor_checks(cwd)
    for check in checks:
        status = "ok" if check.ok else "missing"
        print(f"{status:7} {check.name} - {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def _run_manager(args: Sequence[str]) -> int:
    return subprocess.run(list(args), check=False).returncode


def _launchctl(*args: str) -> int:
    return _run_manager(("launchctl", *args))


def _systemctl(*args: str) -> int:
    return _run_manager(("systemctl", "--user", *args))


class GatewayNotInstalled(RuntimeError):
    """Raised when a self-restart is requested but no service manager unit
    is registered for the gateway. The caller should surface the message
    to the operator and NOT terminate the process — there is no
    supervisor to bring it back up."""


def _restart_manager_command(platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    if platform == "darwin":
        return [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/{SERVICE_LABEL}",
        ]
    if platform.startswith("linux"):
        return ["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME]
    raise RuntimeError(
        "gateway restart is supported only on macOS launchd or Linux systemd"
    )


def _service_install_path(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform == "darwin":
        return _launchd_path()
    if platform.startswith("linux"):
        return _systemd_path()
    raise RuntimeError(
        "gateway restart is supported only on macOS launchd or Linux systemd"
    )


def _probe_manager_command(platform: str | None = None) -> list[str]:
    """Manager command that reports whether the service unit is loaded /
    bootstrapped (not just on disk). ``returncode == 0`` means loaded."""
    platform = platform or sys.platform
    if platform == "darwin":
        return [
            "launchctl",
            "print",
            f"gui/{os.getuid()}/{SERVICE_LABEL}",
        ]
    if platform.startswith("linux"):
        return ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT_NAME]
    raise RuntimeError(
        "gateway restart is supported only on macOS launchd or Linux systemd"
    )


def _service_is_loaded() -> bool:
    """Return ``True`` only when the service manager has the unit
    actually loaded — file on disk alone is not enough (the previous
    incident: ``codex-rc-gateway install`` wrote the plist but never
    bootstrapped it, ``launchctl kickstart`` then silently 113'd while
    ``_shutdown_after_restart_ack`` killed the bot anyway).
    """
    try:
        probe = _probe_manager_command()
    except RuntimeError:
        return False
    result = subprocess.run(probe, check=False, capture_output=True)
    return result.returncode == 0


def request_self_restart(delay_seconds: float = 1.0) -> None:
    """Request installed gateway restart from the running gateway process.

    Raises :class:`GatewayNotInstalled` when the service manager unit is
    not loaded — file on disk is necessary but not sufficient. Callers
    must surface that to the operator and skip any ``os._exit``
    follow-up; otherwise the bot dies with no supervisor to revive it.
    """

    install_path = _service_install_path()
    if not install_path.exists():
        raise GatewayNotInstalled(
            f"gateway service unit is not installed at {install_path}. "
            f"Run `codex-rc-gateway install` first, or restart the bot "
            f"manually."
        )
    if not _service_is_loaded():
        raise GatewayNotInstalled(
            f"gateway service unit at {install_path} is installed but "
            f"not loaded into the service manager. Run "
            f"`codex-rc-gateway start` to bootstrap it, or restart the "
            f"bot manually."
        )

    delay_seconds = max(0.0, delay_seconds)
    manager_command = _restart_manager_command()
    if delay_seconds == 0.0:
        args = manager_command
    else:
        script = (
            "import subprocess, time; "
            f"time.sleep({delay_seconds!r}); "
            f"subprocess.run({manager_command!r}, check=False)"
        )
        args = [sys.executable, "-c", script]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def start_service() -> int:
    if sys.platform == "darwin":
        return _launchctl("bootstrap", f"gui/{os.getuid()}", str(_launchd_path()))
    if sys.platform.startswith("linux"):
        _systemctl("daemon-reload")
        return _systemctl("enable", "--now", SYSTEMD_UNIT_NAME)
    raise RuntimeError("gateway start is supported only on macOS launchd or Linux systemd")


def stop_service() -> int:
    if sys.platform == "darwin":
        return _launchctl("bootout", f"gui/{os.getuid()}", str(_launchd_path()))
    if sys.platform.startswith("linux"):
        return _systemctl("stop", SYSTEMD_UNIT_NAME)
    raise RuntimeError("gateway stop is supported only on macOS launchd or Linux systemd")


def status_service() -> int:
    if sys.platform == "darwin":
        return _launchctl("print", f"gui/{os.getuid()}/{SERVICE_LABEL}")
    if sys.platform.startswith("linux"):
        return _systemctl("status", SYSTEMD_UNIT_NAME)
    raise RuntimeError("gateway status is supported only on macOS launchd or Linux systemd")


def uninstall_service() -> int:
    rc = stop_service()
    spec = service_spec(Path.cwd())
    try:
        spec.path.unlink()
    except FileNotFoundError:
        pass
    if sys.platform.startswith("linux"):
        _systemctl("daemon-reload")
    return rc


def run_gateway(cwd: Path) -> None:
    os.chdir(cwd)
    from .discord_bot import main as discord_main

    discord_main()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-rc gateway")
    parser.add_argument(
        "--cwd",
        default=None,
        help="Project directory that contains .env and runtime data",
    )
    cwd_parent = argparse.ArgumentParser(add_help=False)
    cwd_parent.add_argument(
        "--cwd",
        default=None,
        help="Project directory that contains .env and runtime data",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "run", parents=[cwd_parent], help="Run the Discord gateway in the foreground"
    )
    sub.add_parser("discord", parents=[cwd_parent], help="Create or update .env for Discord")
    sub.add_parser("doctor", parents=[cwd_parent], help="Check local gateway readiness")
    install = sub.add_parser(
        "install",
        parents=[cwd_parent],
        help="Install a user launchd/systemd service",
    )
    install.add_argument("--force", action="store_true", help="Replace an existing service file")
    sub.add_parser("start", help="Start the installed service")
    sub.add_parser("stop", help="Stop the installed service")
    sub.add_parser("restart", help="Restart the installed service")
    sub.add_parser("status", help="Show service status")
    sub.add_parser("uninstall", help="Stop and remove the installed service")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cwd = _project_dir(args.cwd or Path.cwd())

    if args.command == "run":
        run_gateway(cwd)
        return
    if args.command == "discord":
        from .setup_wizard import run as setup_run

        raise SystemExit(setup_run(cwd=cwd))
    if args.command == "doctor":
        raise SystemExit(print_doctor(cwd))
    if args.command == "install":
        path = install_service(cwd, force=bool(args.force))
        print(f"installed {path}")
        return
    if args.command == "start":
        raise SystemExit(start_service())
    if args.command == "stop":
        raise SystemExit(stop_service())
    if args.command == "restart":
        stop_service()
        raise SystemExit(start_service())
    if args.command == "status":
        raise SystemExit(status_service())
    if args.command == "uninstall":
        raise SystemExit(uninstall_service())

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "DoctorCheck",
    "ServiceSpec",
    "build_launchd_plist",
    "build_systemd_unit",
    "collect_doctor_checks",
    "gateway_argv",
    "install_service",
    "main",
    "service_spec",
    "GatewayNotInstalled",
    "_restart_manager_command",
    "request_self_restart",
]
