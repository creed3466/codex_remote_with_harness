# Changelog

All notable changes to codex_rc. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **Runtime files are split by purpose.** Channel/thread state now defaults
  to `data/state/memory.json`; operator prompt config defaults to
  `data/config/soul.md` and `data/config/rules.d/`; redacted operational
  errors live under `data/logs/errors/`; full RPC traces are
  development-only under `data/debug/rpc/` when
  `CODEX_RC_EVENT_LOG_MODE=debug`.
- **Event file logging is minimal by default.** `CODEX_RC_EVENT_LOG_MODE`
  defaults to `errors`, so successful RPC requests, responses,
  notifications, prompts, diffs, and command output are not persisted.
  `debug` restores the previous full `events.jsonl`/`events.txt` trace;
  `off` disables codex_rc event files entirely.
- **Test command failures stay out of Discord.** Failed commands classified
  as `test` are now hidden the same way successful test commands already
  were, keeping failed test output in Codex context without posting a red
  Discord embed.

### Added
- **Stop-time handoffs.** `/codex stop` now asks Codex to write a
  structured handoff markdown file before the real shutdown. Handoff
  metadata is synced into `data/state/memory.json`, files live under
  `data/state/handoffs/`, and `/codex continue` / `/codex resume <id>`
  start fresh sessions by injecting that handoff instead of calling
  Codex `thread/resume`.
- **Bounded session/handoff memory.** `memory.json` now keeps only the
  latest 10 session records and latest 10 handoff records, deleting old
  handoff files when their records are pruned.
- **Soul and Rules are separate injection layers.** Soul is ego, stance,
  and voice (`data/config/soul.md` or `CODEX_RC_SOUL_PATH`). Rules are hard
  constraints (`data/config/rules.md`, `data/config/rules.d/*.md`,
  `CODEX_RC_RULES_PATH`, or `CODEX_RC_RULES_DIR`) and are injected after
  Soul with an explicit "Rules override Soul" wrapper.
- **Legacy `soul.d/*.md` fallback.** Existing operator rule fragments under
  `data/soul.d/*.md` are still loaded as Rules when no new `rules.md` or
  `rules.d` content exists.

### Removed
- Trailing "✅ Turn complete" embed on `turn/completed`. The ✅
  reaction the bot attaches to the user message already signals
  completion; the extra embed was noise. Stream buffers still flush
  on `turn/completed`.

### Fixed
- `Service.shutdown()` was clearing `_channels` *before* trying to
  call `cs.stop()` via a broken `_reconstruct_for_shutdown`. Combined
  with `run()`'s finally never calling `service.shutdown()` at all,
  every bot exit left orphan `codex app-server` children. Both paths
  are now wired correctly; SIGINT / disconnect terminates every child
  and kills its tmux session.

## [0.2.0] - 2026-05-13

Second release. Daily-use polish: cancel + steer + compact + rollback,
ops observability (errors → channel, /healthz endpoint), per-channel
RBAC, JSON logging, and a passthrough layer for Codex CLI's native
slash commands.

### Added
- **In-flight turn auto-steer (T1.1):** a message arriving while a turn is
  running is routed to `turn/steer` instead of starting a second queued turn.
  No new command — the routing decision uses `_active_turn_id`, set from
  the `turn/started` notification.
- **Cancel (T1.2):** ✋ reaction on any allowed-channel message or
  `/codex cancel` issues `turn/interrupt` for the active turn.
- **Auto-compact (T1.3):** when input tokens cross
  `CODEX_RC_AUTO_COMPACT_INPUT_TOKENS`, `thread/compact/start` fires
  automatically. Default 0 = disabled.
- **Usage footer (T1.4):** `turn/completed` embeds now include
  `💎 N tokens · 🪙 rate-limit%` populated from `thread/tokenUsage/updated`
  and `account/rateLimits/updated` notifications.
- **OPS channel error routing (T1.5):** `CODEX_RC_OPS_CHANNEL_ID` forwards
  `logging.ERROR` and `EXCEPTION` records (`codex_rc.*` namespace) to a
  dedicated Discord channel as batched embeds.
- **First-time blocked-user reply (T1.6):** the first message from a user
  outside the allowlist gets one ephemeral warning; subsequent ones are
  silently dropped.
- **Rate-limit observability (T1.7):** `discord.RateLimited` and HTTP
  errors out of `channel.send` are logged with status + retry-after so the
  OPS channel sees them.
- **Turn-stage emoji reactions (T2.8):** the user message that drove the
  current turn picks up 🤔 (started), ⚙️ (first file change), ✅ (completed).
  Transient stages are removed when the turn completes.
- **`CODEX_RC_AUTO_APPROVE` (T2.10):** auto-resolves approval prompts via
  the success-styled button. Falls back to a manual prompt if no success
  button exists.
- **`CODEX_RC_MENTION_ON_COMPLETE` (T2.11):** prefixes `turn/completed`
  payloads with `<@<uid>>` so Discord delivers a push notification.
- **`/codex rollback [N]` (T3.12):** drops the last N turns from thread
  memory via `thread/rollback`. Default N=1.
- **`/healthz` HTTP endpoint (T3.14):** stdlib-only listener via
  `asyncio.start_server`. Activated by `CODEX_RC_HEALTH_PORT`
  (default 0 = disabled). Replies 200 JSON with `version`,
  `active_channels`, `transport`, and `uptime_s` so external monitors
  (UptimeRobot, Healthchecks.io, Grafana) can probe liveness.
- **Codex CLI slash passthrough (5 commands).** Discord messages
  starting with the native slashes `/model`, `/permissions`, `/review`,
  `/fork`, `/goal` (and also `/compact`, `/new`, `/resume`, `/status`,
  `/exit`, `/quit`) are routed to the corresponding Codex RPC. The
  `/codex` prefix continues to work, so `/codex model gpt-5-codex` and
  `/model gpt-5-codex` are equivalent.
  - `/model [name]` — `model/list` when no name, otherwise
    `config/value/write` for the active model.
  - `/permissions <preset>` — `read-only` / `workspace-write` /
    `auto` / `untrusted` map to a pair of `config/value/write` calls
    (sandbox + approvalPolicy).
  - `/review [target]` — `review/start` for the active thread.
  - `/fork` — `thread/fork`, swaps the channel session onto the new
    branch so subsequent messages land there.
  - `/goal [text]` — `thread/goal/set` (or `thread/goal/clear` when
    the body is blank).
  - `/compact` — explicit `thread/compact/start`, distinct from the
    automatic threshold path.

### Changed
- `ChannelService.__init__` and `Service.__init__` gained four optional
  parameters: `on_turn_stage`, `owner_mention_id`, `auto_approve`,
  `auto_compact_threshold_tokens`. All default to a no-op.
- `CodexRcBot` now requests the `reactions` intent so ✋ cancel works.
- `_parse_text_codex_args` recognises `cancel` and `rollback`; rollback
  carries the optional count argument.

## [0.1.0] - 2026-05-13

First public-ready release. Open-source under MIT.

### Added
- Per-channel RBAC. `CODEX_RC_ALLOWED_USER_IDS` now accepts a JSON object
  `{"*": ["uid", ...], "<channel_id>": ["uid", ...]}` where `"*"` is the
  cross-channel wildcard. Plain comma-separated lists keep working and are
  interpreted as the wildcard for backwards compatibility.
- Structured JSON logging. Set `CODEX_RC_LOG_FORMAT=json` to emit one JSON
  object per log line (`ts`, `level`, `logger`, `message`, optional
  `func`/`line`/`exc`). Designed for Vector/Loki/CloudWatch shippers; the
  default `text` formatter stays in place for local dev.
- Auto-recovery from `codex app-server` crashes. The child is watched while
  the session runs; if it exits unexpectedly the bot posts a red embed,
  spins up a fresh process with the same parameters, and tries
  `thread/resume` on the channel's last thread id. Manual-restart
  instructions are surfaced when restart itself fails.
- `codex-rc` console command — interactive setup wizard that copies
  `.env.example` → `.env`, prompts for required Discord values, backs up
  any existing config, and chmods 0600.
- MIT LICENSE, CONTRIBUTING.md, project URLs in `pyproject.toml`.
- 80+ new unit tests; branch coverage 69% → 77%.

### Changed
- README rewritten from "Day 1 / nothing functional" to an accurate
  status, command surface, and configuration table.
- Default transport flipped from `stdio` → `ws` to match the bot's
  actual production default.
- `discord_bot.py` coverage 21% → 69% via a `__new__`-based fixture
  pattern that bypasses `discord.Client.__init__` for routing tests.

### Removed
- HMAC HTTP surface (`server.py`, `auth.py`) and the OpenClaw-GW
  `cli_main` entry point. Discord is the only supported front-end now.
  This cuts ~700 lines plus `fastapi`, `uvicorn`, and `httpx`
  dependencies.

### Fixed
- `pyproject.toml`: `[project.urls]` was placed before `dependencies =
  [...]`, so TOML parsers treated `dependencies` as a member of the
  urls table. pip on 3.12 tolerated it; hatchling on 3.13 rejected it
  outright. Moved urls after scripts; install + tests now succeed on
  both 3.12 and 3.13.
- WS / stdio transport drops without an underlying child exit are now
  funneled into the same auto-recovery path as child crashes
  (idempotent `_recovery_in_progress` guard prevents duplicate
  recovery from concurrent signals).
- ruff ASYNC110 violation in `test_channel_service.py` (replaced the
  `while True: sleep` poll with `Event().wait()` — same cancel semantics).
- Python 3.13 compatibility: added `audioop-lts` as a dependency on 3.13
  to back-fill the stdlib module discord.py 2.4 still imports.

[0.2.0]: https://github.com/your-org/codex_remote_control/releases/tag/v0.2.0
[0.1.0]: https://github.com/your-org/codex_remote_control/releases/tag/v0.1.0
