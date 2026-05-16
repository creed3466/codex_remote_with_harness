# codex_rc

> Run local Codex from Discord. No cloud relay, no API-key juggling, no
> separate remote service.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/creed3466/codex_remote_with_harness/ci.yml?branch=main)](https://github.com/creed3466/codex_remote_with_harness/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/creed3466/codex_remote_with_harness)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/creed3466/codex_remote_with_harness?style=social)](https://github.com/creed3466/codex_remote_with_harness/stargazers)

`codex_rc` turns an allowed Discord channel into a remote control for a local
`codex app-server`. It can stay in one workspace, one codex install, and one
approval posture while driving turns from chat.

Demo placeholder: `docs/assets/demo.gif` (add after recording a real walk-through).

## 30-second onboarding

1. Install (any supported method below).
2. Run `codex-rc` once and create `.env` from `.env.example`.
3. Run `codex-rc-discord`.
4. In an allowed Discord channel: `/codex start ~/Project/my-repo`.

Codex-assisted setup is also documented in
[Setup With Codex](docs/SETUP_WITH_CODEX.md).

## What this repo gives you

- Chat-native, per-channel sessions with per-project state.
- Explicit approvals through Discord UI buttons.
- Text+image prompts directly to Codex in chat.
- Optional local tmux dual-pane (`codex --remote`) for terminal parity.
- Local-first logs and state: no external telemetry.

## Install (3 supported methods)

This distribution is published from `creed3466/codex_remote_with_harness`.

### 1) Recommended (no checkout)

```bash
uv tool install git+https://github.com/creed3466/codex_remote_with_harness.git
codex-rc
codex-rc-discord
```

### 2) pipx

```bash
pipx install git+https://github.com/creed3466/codex_remote_with_harness.git
codex-rc
codex-rc-discord
```

### 3) Editable source

```bash
git clone git@github.com:creed3466/codex_remote_with_harness.git codex_rc
cd codex_rc
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

codex-rc
codex-rc-discord
```

## Publish This Distribution

This checkout is ready to publish once the working tree is cleanly committed
and a Git remote is set. Before pushing, run:

```bash
git diff --check
PYTHONPATH=src python -m pytest -q
```

If you are validating from a separate development checkout, use that checkout's
virtualenv explicitly:

```bash
PYTHONPATH=src /path/to/dev/.venv/bin/python -m pytest -q
```

Then commit and push:

```bash
git add -A
git commit -m "Prepare harness distribution"
git remote add origin git@github.com:creed3466/codex_remote_with_harness.git
git push -u origin main
```

If `origin` already exists, update it instead:

```bash
git remote set-url origin git@github.com:creed3466/codex_remote_with_harness.git
git push -u origin main
```

Do not commit `.env`, `data/`, `dist/`, `tmp/`, caches, or local log files.
They are ignored by default and should stay local to each operator.

## First successful run

```text
/codex start ~/Project/my-repo
Please add type checks and tests for the new parser branch.
```

Expected output:

- `/codex start` confirmation in Discord.
- Session status embed with project path.
- Active reaction and progress updates during long turns.

If `CODEX_RC_TRANSPORT=ws`, tmux creates a local pane pair with `codex --remote`
attached to the running backend.

## Command surface

### Slash command: `/codex`

| Command | Behavior |
| --- | --- |
| `/codex` | Continue from latest handoff |
| `/codex start <path>` | Start or restart a session at `<path>` |
| `/codex stop` | Stop session and persist handoff |
| `/codex restart` | Restart the installed gateway process for code deployment |
| `/codex status` | Show project, thread, sandbox, approval state |
| `/codex capabilities` | Display current `codex` capability status |
| `/codex history` | Show recent thread IDs and previews |
| `/codex resume <handoff-id>` | Resume from saved handoff |
| `/codex new` | Start fresh thread in same project |
| `/codex continue` | Continue from latest saved handoff |
| `/codex cancel` | Cancel in-flight turn |
| `/codex rollback [N]` | Drop last `N` turns (default 1) |
| `/codex compact` | Compact thread context |

### Native Codex slash passthrough

These work directly in allowed channels (equivalent to `/codex` variants in many cases):
`/model`, `/permissions`, `/review`, `/fork`, `/goal`, `/compact`,
`/new`, `/resume`, `/status`, `/capabilities`, `/exit`, `/quit`.

### Text-prefix compatibility

`/codex model`, `/codex permissions`, `/codex review`, `/codex fork`,
`/codex goal` and core control commands remain supported for copy/paste clients.

## Architecture snapshot

```text
Discord channel
    │
    ▼
CodexRcBot
    │
    ▼
Service (per channel)
    │
    ▼
Codex app-server (stdio or ws)
    │
    ▼
Codex CLI
```

For `ws` transport, a local tmux pane hosts `codex --remote <ws-url>` so a
human and bot can observe the same running backend.

## Safety and reliability model

- Local execution: all sensitive operations stay on your machine.
- Auth surface: allowlist by user ID, optional per-channel allowlist.
- Approval-first flow: destructive operations route through Codex approval RPCs.
- Failure visibility: `ERROR`/`EXCEPTION` logs can be routed to a Discord ops channel.
- Optional `/healthz` endpoint for external uptime probes.

## Configuration highlights

Core values from `.env`:

- `CODEX_RC_DISCORD_TOKEN`, `CODEX_RC_DISCORD_APP_ID`: required.
- `CODEX_RC_ALLOWED_USER_IDS`: CSV (`uid1,uid2`) or JSON (`{"*": ["uid"]}` or
  per-channel map).
- `CODEX_RC_DEFAULT_SANDBOX`, `CODEX_RC_DEFAULT_APPROVAL`: baseline defaults.
- `CODEX_RC_TRANSPORT`: `ws` (default) or `stdio`.
- `CODEX_RC_MEMORY_PATH`, `CODEX_RC_LOG_ROOT`, `CODEX_RC_DEBUG_ROOT`: runtime paths.
- `CODEX_RC_EVENT_LOG_MODE`: `errors`, `debug`, or `off`.
- `CODEX_RC_HEALTH_HOST`, `CODEX_RC_HEALTH_PORT`: optional probe endpoint.
- `CODEX_RC_MENTION_ON_COMPLETE`, `CODEX_RC_OPS_CHANNEL_ID`: optional production
  notifications.

See [INSTALL.md](docs/INSTALL.md) for the complete variable reference and setup
walkthrough.

## Documentation

- [Install Guide](docs/INSTALL.md)
- [Setup With Codex](docs/SETUP_WITH_CODEX.md)
- [Usage Guide](docs/USAGE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Launch Analysis](docs/LAUNCH.md)
- [Contributing](CONTRIBUTING.md)

## Runtime files

```text
data/
├─ config/                    editable operator instructions
│  ├─ soul.md
│  ├─ rules.md
│  └─ rules.d/*.md
├─ state/                     durable session state
│  ├─ memory.json
│  └─ handoffs/handoff-*.md
├─ workflow/                  staged development workflow handoffs
│  ├─ wf-*/stage_*_handoff.md
│  └─ _archive/
├─ logs/
│  ├─ errors/YYYY-MM-DD.jsonl redacted production-safe errors
│  ├─ gateway.out.log         managed-service stdout
│  └─ gateway.err.log         managed-service stderr
└─ debug/                     full traces when CODEX_RC_EVENT_LOG_MODE=debug
```

## Development quick checks

```bash
ruff check src tests
pytest -q
pytest -m integration     # optional, requires local codex CLI
./scripts/gen_protocol.sh # regenerate protocol models after codex CLI upgrades
```

## Contributing

Small PRs, clear intent, and tests/docs updates:

- Keep surface changes minimal.
- Include reproduction steps in PR description.
- Run lint + tests before requesting review.

## License

MIT. See [LICENSE](LICENSE).
