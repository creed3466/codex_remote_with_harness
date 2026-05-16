# Setup With Codex

This is the recommended first-run path. The user only needs a working Codex
CLI login; Codex can drive the gateway setup and explain the Discord steps.

## 1. Sign In To Codex

Run this once in a normal terminal:

```bash
codex login status
codex login
```

For a headless or SSH-only machine:

```bash
codex login --device-auth
```

`codex_rc` does not ask for an OpenAI API key. It starts a local
`codex app-server` and uses the Codex CLI credentials already present on the
machine.

## 2. Ask Codex To Install

Open the repository in Codex and paste:

```text
Set up codex_rc for Discord.

Assume Codex CLI is already installed and logged in.
Please:
1. run `codex-rc setup --guided --cwd ~/.codex-rc`
2. explain each Discord Developer Portal value I need to paste
3. never print my Discord token after I paste it
4. create the local `.env`
5. run `codex-rc gateway doctor --fix --cwd ~/.codex-rc`
6. print the Discord invite URL
7. start the gateway with `codex-rc gateway run --cwd ~/.codex-rc`
8. stop and tell me the exact next command if any step needs manual action
```

## 3. Discord Values

The guided setup asks for:

| Value | Where to find it |
| --- | --- |
| Bot token | Discord Developer Portal -> Application -> Bot -> Reset Token |
| Application ID | Developer Portal -> Application -> General Information |
| Your user ID | Discord Settings -> Advanced -> Developer Mode, then right-click yourself |
| Guild ID | Optional. Right-click your server. Makes slash commands appear faster. |

After `.env` is written, use:

```bash
codex-rc gateway invite-url --cwd ~/.codex-rc
```

Open that URL, select your server, and authorize the bot.

## 4. Verify

```bash
codex-rc gateway doctor --fix --cwd ~/.codex-rc
codex-rc gateway run --cwd ~/.codex-rc
```

Then in an allowed Discord channel:

```text
/codex start ~/work/my-repo
```

Messages in that channel are routed to the local Codex gateway.
