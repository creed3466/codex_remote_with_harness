"""codex_rc — drive a local Codex CLI from Discord via the official app-server."""

__version__ = "0.2.0"


def cli_discord() -> None:  # pragma: no cover — wires console script
    from .discord_bot import main

    main()


def cli_setup() -> None:  # pragma: no cover — wires console script
    from .cli import main

    main()
