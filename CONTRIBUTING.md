# Contributing to codex_rc

Thanks for taking the time to contribute! This project is small and
opinionated — please read this short guide before opening a PR.

## Dev setup

```bash
git clone git@github.com:your-org/codex_remote_control.git codex_rc
cd codex_rc
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
```

Most contributors do not need to run the Discord bot itself to develop —
unit tests cover the bulk of the surface.

## Before you push

```bash
ruff check src tests           # lint
pytest -q                      # 184 tests, unit only
```

If you touched anything that talks to the real Codex CLI, also run:

```bash
pytest -m integration          # spawns codex app-server, slower
```

Coverage gate is **65 %** (branch). Stay above it:

```bash
pytest --cov --cov-report=term-missing
```

## Branch & PR flow

1. Branch off `main`: `git switch -c feat/<short-name>` or
   `fix/<short-name>`.
2. Keep PRs **small and focused** — one feature or fix per branch.
3. Commit messages follow conventional commits: `feat:`, `fix:`,
   `refactor:`, `docs:`, `test:`, `chore:`, `perf:`, `ci:`.
4. PR description should explain **why** (not just what). Link any
   relevant Discord screenshots / log excerpts.
5. CI (ruff + pytest on 3.12 and 3.13) must pass before review.

## Architecture quick reference

- `rpc_client.py` — JSON-RPC client + Transport Protocol (stdio, ws).
- `server_process.py` — spawns and supervises `codex app-server`.
- `service.py` — Service registry + ChannelService (thread, pump, cat).
- `discord_bot.py` — Discord surface; slash + text command handling.
- `notif_router.py` — Codex `ServerNotification` → Discord payloads.
- `approval_router.py` — server-initiated approval RPCs ↔ Discord buttons.
- `session_store.py` — SQLite thread store.
- `tmux_host.py` — dual-pane creator (TUI + event log).

## Regenerating protocol bindings

After a `codex` CLI bump:

```bash
./scripts/gen_protocol.sh
```

The output goes to `src/codex_rc/protocol/v2.py`. **Do not hand-edit** —
re-run the script and commit the diff.

## Reporting issues

- Open issues at <https://github.com/your-org/codex_remote_control/issues>.
- Please include: codex CLI version, Python version, OS, and a relevant
  excerpt of `$CODEX_RC_LOG_PATH` (default `/tmp/codex_rc.log`).

## Code style

- Python 3.12, type-annotated, ruff-clean (`E F I B UP ASYNC`).
- Prefer many small files (≤ 800 lines).
- Prefer immutable data (`@dataclass(frozen=True)` / `NamedTuple`) where
  it does not hurt clarity.
- New features ship with tests. Default marker is `unit`; mark anything
  that does real I/O `integration`.

## License

By contributing you agree your contributions are licensed under the
project's [MIT License](LICENSE).
