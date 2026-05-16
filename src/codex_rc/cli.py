"""Top-level codex-rc command dispatcher."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-rc")
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("setup", help="Create or update .env")
    setup.add_argument("--cwd", default=None, help="Directory where .env is written")
    setup.add_argument(
        "--guided",
        action="store_true",
        help="Run the Codex-first guided setup flow",
    )
    sub.add_parser("gateway", help="Manage the Discord gateway")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .setup_wizard import main as setup_main

        setup_main()
        return
    if argv[0] == "gateway":
        from .gateway import main as gateway_main

        gateway_main(argv[1:])
        return

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "setup":
        from pathlib import Path

        from .setup_wizard import run, run_guided

        cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
        if args.guided:
            raise SystemExit(run_guided(cwd=cwd))
        raise SystemExit(run(cwd=cwd))
    parser.print_help()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["main"]
