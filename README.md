# codex_rc

> Run your local Codex CLI from anywhere via Discord.
> ChatGPT/Codex subscription only — no API keys, no cloud.

`codex_rc` bridges a Discord channel to a local `codex app-server` so you can
drive Codex coding turns from your phone, laptop, or browser. The same backend
can be attached by a real `codex --remote` TUI in a tmux pane, so a human and
the bot share one conversation.

<!-- demo: docs/demo.gif (recorded separately) -->

## Status

Current release status:

- 184 tests passing · branch coverage 69 % · CI green on Python 3.12 / 3.13
- Discord-driven Codex turns end-to-end (text + image attachments)
- Per-channel tmux dual-pane with live `codex --remote` join
- Server-initiated approval prompts → Discord buttons
- Thread history with `/codex history` / `/codex resume` / `/codex new`
- Rich diff embed with per-file `+N/−N` stats
- 6-frame cat-of-patience emoji animation per turn

## Why

Codex CLI runs locally and ships no mobile or chat client. `codex_rc` turns any
Discord channel into a remote terminal for Codex working in a target project,
built on Codex's official `app-server` JSON-RPC protocol — no PTY screen
scraping, no API key juggling, no cloud relay.

## Architecture

```
Discord channel ── message ──▶ CodexRcBot (discord.py)
                                  │
                                  ▼
                              Service ── per channel ──▶ ChannelService
                                                            │
                                                            ▼
                                       CodexServerProcess
                                          │
                                          │  spawns `codex app-server
                                          │  --listen ws://127.0.0.1:<port>`
                                          ▼
                                       WebSocketTransport ── JSON-RPC ──▶ codex
                                                                            ▲
                                                                            │
                              tmux pane "codex --remote ws://..."  ◀────────┘
```

Wire format is NDJSON. Notifications are fanned out to subscribers such as the
`ChannelService` router via the per-call queue model in
`rpc_client.notifications()`, with a 256-entry backlog for late subscribers.

## Easiest Setup

Prerequisite: install Codex CLI and sign in once:

```bash
codex login status
codex login
```

Then open this repository in Codex and say:

> Set up codex_rc for Discord. Use the guided setup, explain each Discord
> Developer Portal value I need to paste, create the local `.env`, run the
> readiness check, generate the invite URL, and start the gateway in foreground.

Codex should call:

```bash
codex-rc setup --guided --cwd ~/.codex-rc
```

The guided flow writes `.env` with `chmod 0600`, checks the local Codex CLI,
creates runtime directories, and prints the Discord bot invite URL. See
[docs/SETUP_WITH_CODEX.md](docs/SETUP_WITH_CODEX.md) for the full prompt.

## Manual Quick Start

Install the release CLI, connect Discord, then install the gateway service:

```bash
pipx install "git+https://github.com/your-org/codex_remote_control.git"
# or, without pipx:
uv tool install "git+https://github.com/your-org/codex_remote_control.git"

mkdir -p ~/.codex-rc
cd ~/.codex-rc
codex-rc gateway discord      # writes .env with chmod 0600
codex-rc gateway doctor --fix # checks Discord config, Codex login, runtime paths
codex-rc gateway invite-url   # prints the Discord bot install URL
codex-rc gateway install      # writes launchd/systemd user service
codex-rc gateway start
```

The gateway service is the long-running Discord bridge. It stores no Discord
token in the launchd plist or systemd unit; secrets stay in the working
directory's `.env` file. Use `codex-rc gateway run` instead of `install/start`
when you want a foreground process for debugging.

Then in any allowed Discord channel:

```
/codex start ~/work/my-repo
> please add a sleep() to main.py
```

## Development Install

```bash
git clone git@github.com:your-org/codex_remote_control.git codex_rc
cd codex_rc
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env
$EDITOR .env              # fill CODEX_RC_DISCORD_TOKEN + CODEX_RC_ALLOWED_USER_IDS

codex-rc gateway run      # starts the bot in foreground
```

By default, runtime files are minimal: channel/thread state goes under
`data/state/`, redacted serious errors go under `data/logs/errors/`, and full
RPC traces are written only when `CODEX_RC_EVENT_LOG_MODE=debug`.

## Gateway CLI

`codex-rc gateway` manages the local Discord gateway:

| Command | Purpose |
| --- | --- |
| `codex-rc gateway discord` | Interactive Discord `.env` setup |
| `codex-rc gateway doctor [--fix]` | Readiness checks without printing secrets |
| `codex-rc gateway invite-url` | Print the Discord OAuth install URL |
| `codex-rc gateway run` | Foreground gateway process |
| `codex-rc gateway install [--force]` | Install a user launchd/systemd service |
| `codex-rc gateway start` | Start the installed service |
| `codex-rc gateway stop` | Stop the installed service |
| `codex-rc gateway restart` | Restart the installed service |
| `codex-rc gateway status` | Show service manager status |
| `codex-rc gateway uninstall` | Stop and remove the service file |

Pass `--cwd <dir>` before the subcommand to use a different config/runtime
directory, for example `codex-rc gateway --cwd ~/codex-rc-prod doctor`.

## Discord commands

Text-prefix is the reliable surface (slash autocomplete may not render on every
client even though the command is registered):

| Command | Purpose |
| --- | --- |
| `/codex start <path>` | Start a channel session anchored at `<path>` |
| `/codex stop` | Ask Codex to write a handoff file, then stop the active session |
| `/codex status` | Show session metadata |
| `/codex capabilities` | Audit which Codex CLI/app-server capabilities are currently used |
| `/codex history` | Show the last 10 threads (prefix, preview, turns, age) |
| `/codex resume <id>` | Start a fresh session from a saved handoff id/prefix |
| `/codex new` | Start a fresh Codex thread in the same channel |
| `/codex cancel` | Interrupt the in-flight turn (or react ✋ on any message) |
| `/codex rollback [N]` | Drop the last `N` turns from thread memory (default 1) |

You can also use Codex CLI's native slashes directly in any allowed channel
(both forms are equivalent — `/codex model` and `/model`):

| Native slash | Equivalent action |
| --- | --- |
| `/model [id]` | List models or switch the active one |
| `/permissions <preset>` | Apply a sandbox + approval preset |
| `/review [target]` | Start a Codex review of the working tree |
| `/fork` | Branch the active thread |
| `/goal [text]` | Set or clear the experimental thread goal |
| `/capabilities` | Audit codex_rc's Codex CLI/app-server capability coverage |
| `/compact` | Compact the current thread context |

`/codex continue` also starts a fresh session from the latest saved handoff
instead of calling Codex `thread/resume` on the previous thread. Handoff
records and files are stored under `data/state/memory.json` and
`data/state/handoffs/`. Session and handoff memory are each capped to the
latest 10 records; thread metadata is capped by `CODEX_RC_THREAD_HISTORY_MAX`.

Implementation-like messages in a started channel become gated development
workflows: Codex first performs self-planning and analysis, proposes exactly two
plans, and waits for a Discord button approval. Plain information requests pass
through as normal Codex turns. Clicking Plan A or Plan B continues the same
thread through separate design, implementation, and verification turns. The
workflow harness pins analysis to `gpt-5.5` with `high` effort, design to
`gpt-5.5` with `medium` effort, and implementation/verification to
`gpt-5.3-codex-spark`; if that Spark model is unavailable, those stages retry
with `gpt-5.4`. Image attachments are forwarded as `ImageUserInput`
(vision-capable). Approval prompts arrive as Discord embed + 2–4 buttons whose
`custom_id` is
`codex_rc:<prompt_id>:<decision>`.

## Configuration

Everything is environment-driven via `.env` (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `CODEX_RC_DISCORD_TOKEN` | Discord bot token (required) |
| `CODEX_RC_DISCORD_APP_ID` | Discord application ID (required) |
| `CODEX_RC_DISCORD_GUILD_ID` | Guild for instant slash registration |
| `CODEX_RC_DISCORD_CHANNEL_IDS` | Optional channel allowlist (empty = all) |
| `CODEX_RC_ALLOWED_USER_IDS` | Allowlist. CSV (`uid1,uid2`) or JSON `{"*": ["uid"], "<channel>": ["uid"]}` for per-channel RBAC |
| `CODEX_RC_DEFAULT_SANDBOX` | `workspace-write` (default) / `read-only` / `danger-full-access` |
| `CODEX_RC_DEFAULT_APPROVAL` | `on-request` (default) / `never` / `untrusted` |
| `CODEX_RC_TRANSPORT` | `ws` (default, dual-pane) or `stdio` |
| `CODEX_RC_MEMORY_PATH` | JSON path for channel/thread state (default `./data/state/memory.json`) |
| `CODEX_RC_SOUL_PATH` | Operator Soul file: ego, stance, and voice (default lookup starts at `./data/config/soul.md`) |
| `CODEX_RC_RULES_PATH` / `CODEX_RC_RULES_DIR` | Operator Rules: hard constraints injected after Soul; default lookup uses `./data/config/rules.md` and `./data/config/rules.d/*.md` |
| `CODEX_RC_LOG_ROOT` | Redacted serious error log root (default `./data/logs`) |
| `CODEX_RC_DEBUG_ROOT` | Development trace root used by `CODEX_RC_EVENT_LOG_MODE=debug` (default `./data/debug`) |
| `CODEX_RC_EVENT_LOG_MODE` | `errors` (default, redacted serious failures only), `debug` (full RPC trace), or `off` |
| `CODEX_RC_ERROR_LOG_RETENTION_DAYS` | Days to keep operational `logs/errors/*.jsonl` files (default `30`; `0` disables pruning) |
| `CODEX_RC_THREAD_HISTORY_MAX` | Max Codex thread metadata records kept in `memory.json` (default `100`; `0` disables pruning) |
| `CODEX_RC_LOG_PATH` | Optional rotating Python debug log |
| `CODEX_RC_LOG_LEVEL` | `WARNING` in `.env.example`; set `DEBUG` while developing |
| `CODEX_RC_LOG_FORMAT` | `text` (default) or `json` — JSON emits one object per line for log shippers |
| `CODEX_RC_OPS_CHANNEL_ID` | Optional Discord channel ID for ERROR/EXCEPTION log forwarding |
| `CODEX_RC_MENTION_ON_COMPLETE` | Optional Discord user ID — prefixes `turn/completed` payloads to trigger push notifications |
| `CODEX_RC_AUTO_APPROVE` | When truthy, auto-resolves approval prompts via the success button (no Discord prompt) |
| `CODEX_RC_AUTO_COMPACT_INPUT_TOKENS` | When > 0, fires `thread/compact/start` once input tokens cross the threshold |
| `CODEX_RC_HEALTH_HOST` / `CODEX_RC_HEALTH_PORT` | Optional stdlib HTTP `/healthz` listener. Port 0 (default) disables it |

Runtime files are intentionally split by purpose:

```text
data/
├── config/              # operator-editable prompt config
│   ├── soul.md
│   ├── rules.md
│   └── rules.d/*.md
├── state/               # required app state, not logs
│   └── memory.json
├── logs/                # production-safe redacted errors
│   └── errors/YYYY-MM-DD.jsonl
└── debug/               # development-only full traces
    ├── codex_rc.log
    └── rpc/<channel>/<YYYY-MM-DD>/events.{jsonl,txt}
```

## Development

```bash
ruff check src tests       # lint
pytest -q                  # 184 tests, unit only by default
pytest -m integration      # opt-in: spawns real codex CLI
pytest --cov --cov-report=html && open htmlcov/index.html
./scripts/gen_protocol.sh  # regenerate Pydantic models after codex CLI bump
```

CI runs ruff + pytest on Python 3.12 and 3.13 — see
[.github/workflows/ci.yml](.github/workflows/ci.yml).

## Project layout

```
src/codex_rc/
├── rpc_client.py       # JSON-RPC client + stdio / ws transports
├── server_process.py   # codex app-server lifecycle + error/debug event logs
├── service.py          # registry + per-channel service (thread, pump, cat)
├── discord_bot.py      # CodexRcBot, slash + text command surface
├── notif_router.py     # ServerNotification → Discord embed/diff/buttons
├── approval_router.py  # 5 server-request approvals → Discord buttons
├── session_store.py    # JSON channel/thread state
├── tmux_host.py        # libtmux dual-pane creator
├── protocol/v2.py      # auto-generated Pydantic models (do not hand-edit)
└── assets/cats.json    # 6 cat-of-patience emoji frames
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome — keep changes small and
green (`ruff check` + `pytest -q` before pushing).

## License

[MIT](LICENSE).
