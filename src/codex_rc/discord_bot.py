"""Run codex_rc as a standalone Discord bot.

Wire-up:
  Discord gateway  ──messages──▶  CodexRcBot
                                   │
                                   ▼
                                Service ──▶ CodexServerProcess ──▶ codex app-server
                                   │
                                   ▼ post callback
                                CodexRcBot.send  ──▶  Discord channel

* slash command ``/codex action:start|stop|status`` controls the per-channel
  session lifecycle.
* plain channel messages get forwarded to the active session's ``send_text``.
* button clicks with custom_id starting ``codex_rc:`` flow through the
  approval router.

Auth surface: only users in ``CODEX_RC_ALLOWED_USER_IDS`` are accepted; if
``CODEX_RC_DISCORD_CHANNEL_IDS`` is set, only those channels are listened to.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import Any

import discord
from discord import app_commands

from .config import Settings, load_settings_from_env
from .service import Service, permission_preset_config, permission_preset_names
from .session_store import SessionStore
from .workflow_router import WORKFLOW_CUSTOM_ID_PREFIX, parse_workflow_custom_id

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- helpers

_STYLE_MAP = {
    1: discord.ButtonStyle.primary,
    2: discord.ButtonStyle.secondary,
    3: discord.ButtonStyle.success,
    4: discord.ButtonStyle.danger,
}


def _components_to_view(components_raw: list[dict[str, Any]] | None) -> discord.ui.View | None:
    """Convert our wire-format components (action rows + buttons) to discord.py
    View. Returns None when there are no components."""
    if not components_raw:
        return None
    view = discord.ui.View(timeout=None)
    for row in components_raw:
        for comp in row.get("components", []):
            if comp.get("type") != 2:
                continue
            emoji = comp.get("emoji", {}).get("name") if comp.get("emoji") else None
            view.add_item(
                discord.ui.Button(
                    label=comp.get("label", "?")[:80],
                    style=_STYLE_MAP.get(comp.get("style", 2), discord.ButtonStyle.secondary),
                    custom_id=comp.get("custom_id", "")[:100],
                    emoji=emoji,
                )
            )
    return view


def _embeds_from_payload(embeds_raw: list[dict[str, Any]] | None) -> list[discord.Embed]:
    if not embeds_raw:
        return []
    return [discord.Embed.from_dict(e) for e in embeds_raw]


def _payload_to_message_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    content = payload.get("content")
    embeds = _embeds_from_payload(payload.get("embeds"))
    view = _components_to_view(payload.get("components"))
    kwargs: dict[str, Any] = {}
    if content:
        kwargs["content"] = content
    if embeds:
        kwargs["embeds"] = embeds
    if view is not None:
        kwargs["view"] = view
    return kwargs


# --------------------------------------------------------- project path autocomplete

_AUTOCOMPLETE_LIMIT = 25
_AUTOCOMPLETE_HISTORY_SLOTS = 5


def _project_path_choices(
    bot: CodexRcBot, interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest directories: recent project_path's first, then live
    filesystem children of the prefix the user is typing. Capped at
    Discord's 25-item limit per autocomplete response.
    """
    from pathlib import Path as _Path

    # Defense-in-depth: same allowlist gating as the rest of the bot.
    if interaction.channel_id is None or not bot.access.allows(
        str(interaction.user.id), str(interaction.channel_id)
    ):
        return []

    current = (current or "").strip()
    expanded_input = _Path(current).expanduser() if current else None

    # 1) History from memory.json — operator's recent project paths.
    seen: set[str] = set()
    history: list[str] = []
    for sess in bot.service.store.list_all():
        p = sess.project_path
        if p in seen:
            continue
        seen.add(p)
        if not current or current.lower() in p.lower():
            history.append(p)
        if len(history) >= _AUTOCOMPLETE_HISTORY_SLOTS:
            break

    # 2) Filesystem children of the directory the user is typing into.
    fs_items: list[str] = []
    if expanded_input is not None:
        if expanded_input.is_dir():
            base = expanded_input
            name_prefix = ""
        else:
            parent = expanded_input.parent
            base = parent if parent.is_dir() else _Path.home()
            name_prefix = expanded_input.name
    else:
        base = _Path.home()
        name_prefix = ""

    try:
        for child in sorted(base.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if name_prefix and not child.name.lower().startswith(name_prefix.lower()):
                continue
            fs_items.append(str(child))
            if len(fs_items) >= _AUTOCOMPLETE_LIMIT:
                break
    except (OSError, PermissionError):
        pass

    # 3) Merge: history first, then filesystem; dedupe; truncate to 25.
    combined: list[str] = []
    for p in history + fs_items:
        if p in combined:
            continue
        combined.append(p)
        if len(combined) >= _AUTOCOMPLETE_LIMIT:
            break

    # Discord caps each Choice name + value at 100 chars; truncate the
    # display name so long absolute paths still surface a tail.
    out: list[app_commands.Choice[str]] = []
    for p in combined:
        display = p if len(p) <= 95 else "…" + p[-94:]
        value = p[:100]
        out.append(app_commands.Choice(name=display, value=value))
    return out


# --------------------------------------------------------- resume picker view

RESUME_BUTTON_PREFIX = "codex_rc:resume:"
RESUME_PICKER_LIMIT = 5


def _format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


class _ResumeButton(discord.ui.Button):
    """One button per recent handoff; clicking starts a fresh handoff thread."""

    def __init__(
        self,
        *,
        bot: CodexRcBot,
        channel_id: str,
        handoff_id: str,
        label: str,
    ) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"{RESUME_BUTTON_PREFIX}{handoff_id}",
        )
        self._bot = bot
        self._channel_id = channel_id
        self._handoff_id = handoff_id

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = self._bot
        if not bot.access.allows(str(interaction.user.id), self._channel_id):
            await interaction.response.send_message(
                "❌ Not allowed.", ephemeral=True
            )
            return
        if str(interaction.channel_id) != self._channel_id:
            await interaction.response.send_message(
                "❌ Channel mismatch.", ephemeral=True
            )
            return
        try:
            sess = await bot.service.resume_handoff(
                self._channel_id,
                self._handoff_id,
            )
        except KeyError:
            await interaction.response.send_message(
                "❌ Handoff not found.",
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("resume via button failed")
            await interaction.response.send_message(
                f"❌ resume failed: {exc}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"⏮ Started fresh from handoff `{self._handoff_id[:18]}` "
            f"on `{sess.project_path}`."
        )


def _build_resume_picker(
    bot: CodexRcBot, channel_id: str, handoffs: list
) -> tuple[discord.Embed, discord.ui.View]:
    """Build (embed, view) for the resume picker. ``handoffs`` is expected
    to be capped at RESUME_PICKER_LIMIT."""
    import time as _time

    view = discord.ui.View(timeout=300)
    lines: list[str] = []
    for h in handoffs:
        short = (h.preview or "(no preview)")[:32]
        age = _format_age(max(0.0, _time.time() - h.updated_at))
        label = f"{short} · {h.handoff_id[:8]}"
        view.add_item(
            _ResumeButton(
                bot=bot,
                channel_id=channel_id,
                handoff_id=h.handoff_id,
                label=label[:80],
            )
        )
        lines.append(
            f"`{h.handoff_id[:18]}` · {age}\n  {short}"
        )
    embed = discord.Embed(
        title="Resume Handoff",
        description="\n\n".join(lines),
        color=0x202123,
    )
    embed.set_footer(text="Available for 5 minutes.")
    return embed, view


_VALID_ACTIONS: frozenset[str] = frozenset({
    "start", "stop", "status", "capabilities", "history", "resume", "new",
    "continue", "cancel", "rollback",
    "model", "permissions", "review", "fork", "goal",
    "compact", "exit", "quit",
})
_ACTIONS_WITH_REST: frozenset[str] = frozenset({
    "start", "resume", "rollback",
    "model", "permissions", "review", "goal",
})
# Native Codex CLI slashes we accept directly (no /codex prefix).
_NATIVE_SLASHES: frozenset[str] = frozenset({
    "/model", "/permissions", "/review", "/fork", "/goal",
    "/new", "/resume", "/status", "/capabilities", "/compact", "/exit", "/quit",
})
_ACTION_CHOICES = [
    app_commands.Choice(name="Continue latest", value="continue"),
    app_commands.Choice(name="Start project", value="start"),
    app_commands.Choice(name="Stop session", value="stop"),
    app_commands.Choice(name="Session status", value="status"),
    app_commands.Choice(name="Capability audit", value="capabilities"),
    app_commands.Choice(name="New thread", value="new"),
    app_commands.Choice(name="Cancel turn", value="cancel"),
    app_commands.Choice(name="Thread history", value="history"),
    app_commands.Choice(name="Resume handoff", value="resume"),
    app_commands.Choice(name="Rollback turns", value="rollback"),
    app_commands.Choice(name="Compact context", value="compact"),
]
_PERMISSION_CHOICE_LABELS = {
    "read-only": "Read only",
    "workspace-write": "Workspace write",
    "auto": "Auto trusted",
    "untrusted": "Untrusted",
}
_PERMISSION_CHOICES = [
    app_commands.Choice(
        name=_PERMISSION_CHOICE_LABELS.get(name, name),
        value=name,
    )
    for name in permission_preset_names()
]
_PERMISSION_KEY_PREFIXES = (
    "permissions:",
    "permission:",
    "permissions=",
    "permission=",
)
_PERMISSION_FLAG_TOKENS = {"--permissions", "--permission"}


def _parse_text_codex_args(args_text: str) -> tuple[str | None, str | None]:
    """Parse args portion of a text-prefix '/codex ...' command.

    Supports both shapes:
      ``start /path/to/project``        — positional, shell-style
      ``action:start project_path:/p``  — slash-menu copy-paste shape

    Returns ``(action, rest_arg)`` where ``rest_arg`` is whatever follows
    the action (path, thread id, count, model name, free-form text…) or
    ``None``.
    """
    args_text = args_text.strip()
    if not args_text:
        return None, None
    action: str | None = None
    project_path: str | None = None
    parts = args_text.split(maxsplit=1)
    for token in args_text.split():
        if token.startswith("action:"):
            action = token[len("action:") :].strip().lower() or None
        elif token.startswith("project_path:"):
            project_path = token[len("project_path:") :].strip() or None
    if action is None and parts:
        first = parts[0].strip().lower()
        if first in _VALID_ACTIONS:
            action = first
            if first in _ACTIONS_WITH_REST and len(parts) > 1:
                project_path = parts[1].strip().strip('"')
    return action, project_path


def _shlex_split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _parse_start_options(
    args_text: str,
    rest_arg: str | None,
) -> tuple[str | None, str | None]:
    """Return ``(project_path, permission_preset)`` for text ``/codex start``.

    Supported permission shapes:
      ``/codex start /repo --permissions read-only``
      ``/codex start /repo permissions:read-only``
      ``/codex action:start project_path:/repo permissions:read-only``
    """
    tokens = _shlex_split(args_text.strip())
    permission_preset: str | None = None
    project_path: str | None = None

    for i, token in enumerate(tokens):
        lower = token.lower()
        if lower.startswith("project_path:"):
            project_path = token[len("project_path:") :].strip() or None
            continue
        for prefix in _PERMISSION_KEY_PREFIXES:
            if lower.startswith(prefix):
                permission_preset = token[len(prefix) :].strip() or None
                break
        if lower in _PERMISSION_FLAG_TOKENS and i + 1 < len(tokens):
            permission_preset = tokens[i + 1].strip() or None

    if project_path is None and rest_arg:
        rest_tokens = _shlex_split(rest_arg)
        filtered: list[str] = []
        skip_next = False
        for token in rest_tokens:
            if skip_next:
                skip_next = False
                continue
            lower = token.lower()
            if lower in _PERMISSION_FLAG_TOKENS:
                skip_next = True
                continue
            if any(lower.startswith(prefix) for prefix in _PERMISSION_KEY_PREFIXES):
                continue
            filtered.append(token)
        project_path = " ".join(filtered).strip() or None

    if project_path:
        project_path = project_path.strip().strip('"')
    return project_path, permission_preset


def _start_permission_kwargs(permission_preset: str | None) -> dict[str, str]:
    if not permission_preset:
        return {}
    config = permission_preset_config(permission_preset)
    return {
        "sandbox": config["sandbox"],
        "approval": config["approvalPolicy"],
    }


def _route_native_slash(content: str) -> str | None:
    """Map a native Codex slash (``/model …``) to our ``<action> <rest>``
    args string. Returns None if the content doesn't start with a known
    native slash."""
    for slash in _NATIVE_SLASHES:
        if content == slash:
            return slash[1:]  # drop leading "/"
        if content.startswith(slash + " "):
            rest = content[len(slash) + 1:].strip()
            return f"{slash[1:]} {rest}".strip()
    return None


def _capabilities_text(service: object, channel_id: str) -> str:
    renderer = getattr(service, "capabilities_text", None)
    if callable(renderer):
        body = renderer(channel_id)
        if isinstance(body, str):
            return body
    return (
        "**Codex CLI capability audit**\n"
        "runtime unavailable outside the real Service instance."
    )


# ============================================================ bot

class AccessControl:
    """Channel-aware Discord user allowlist.

    Construct from a map ``{"*": (...), "<channel_id>": (...)}``: ``"*"``
    is the cross-channel wildcard, channel-specific keys are additive.
    An empty map admits everyone (single-tenant local-dev convenience).
    """

    __slots__ = ("_map",)

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._map: dict[str, frozenset[str]] = {
            k: frozenset(v) for k, v in mapping.items()
        }

    def allows(self, user_id: str, channel_id: str) -> bool:
        if not self._map:
            return True
        if user_id in self._map.get(channel_id, frozenset()):
            return True
        return user_id in self._map.get("*", frozenset())

    @property
    def is_open(self) -> bool:
        return not self._map


class CodexRcBot(discord.Client):
    """Single-bot, multi-channel codex_rc front-end.

    Designed for personal use: one bot, one user (allowlist), several
    channels each bound to its own project via ``/codex start``.
    """

    def __init__(
        self,
        *,
        access: AccessControl,
        channel_ids: set[str] | None,
        guild_id: int | None,
        service: Service,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.access = access
        self.channel_ids = channel_ids
        self.guild_id = guild_id
        self.service = service
        self.tree = app_commands.CommandTree(self)
        # Anchor message per channel for turn-stage reaction lifecycle.
        self._last_user_message: dict[str, discord.Message] = {}
        # Editable status messages keyed by (channel_id, logical_key).
        self._editable_messages: dict[tuple[str, str], discord.Message] = {}
        # Users we've already warned about not being on the allowlist.
        self._warned_blocked_users: set[str] = set()
        self._register_commands()

    # ------------------------------------------------------ slash commands

    def _register_commands(self) -> None:
        bot = self  # alias for closure

        @self.tree.command(name="codex", description="codex_rc session control")
        @app_commands.describe(
            action="Action to run. Empty continues the latest handoff.",
            project_path="Path, handoff id, rollback count, model, or command arg.",
            permissions="Permission preset for Start. Empty uses the configured default.",
        )
        @app_commands.choices(
            action=_ACTION_CHOICES,
            permissions=_PERMISSION_CHOICES,
        )
        async def codex_cmd(
            interaction: discord.Interaction,
            action: app_commands.Choice[str] | None = None,
            project_path: str | None = None,
            permissions: app_commands.Choice[str] | None = None,
        ) -> None:
            channel_id = str(interaction.channel_id)
            if not bot.access.allows(str(interaction.user.id), channel_id):
                await interaction.response.send_message(
                    "❌ Not allowed.", ephemeral=True
                )
                return
            kind = action.value if action else "continue"
            if kind == "start":
                if not project_path:
                    await interaction.response.send_message(
                        "❌ `project_path` is required for start.", ephemeral=True
                    )
                    return
                permission_preset = permissions.value if permissions else None
                try:
                    permission_kwargs = _start_permission_kwargs(permission_preset)
                except ValueError as exc:
                    await interaction.response.send_message(
                        f"❌ {exc}", ephemeral=True
                    )
                    return
                await interaction.response.defer(thinking=True)
                try:
                    sess = await bot.service.start_session(
                        channel_id=channel_id,
                        project_path=project_path,
                        **permission_kwargs,
                    )
                except FileNotFoundError as exc:
                    await interaction.followup.send(f"❌ {exc}")
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("start_session failed")
                    await interaction.followup.send(f"❌ start failed: {exc}")
                    return
                await interaction.followup.send(
                    f"🟢 Session started. Project: `{sess.project_path}`"
                )
            elif kind == "stop":
                await interaction.response.defer(thinking=True, ephemeral=False)
                await bot.service.stop_session(channel_id)
                await interaction.followup.send("🛑 Session stopped.")
            elif kind == "status":
                sess = bot.service.store.get(channel_id)
                if sess is None:
                    await interaction.response.send_message(
                        "No session in this channel.", ephemeral=True
                    )
                else:
                    status_text = (
                        bot.service.status_text(channel_id)
                        if isinstance(bot.service, Service)
                        else None
                    )
                    await interaction.response.send_message(
                        status_text
                        or (
                            f"channel `{sess.channel_id}` · state `{sess.state}` · "
                            f"project `{sess.project_path}`\n"
                            f"thread `{sess.codex_thread_id or '—'}` · "
                            f"handoff `{sess.handoff_id or '—'}` · "
                            f"sandbox `{sess.sandbox}` · approval `{sess.approval}`"
                        )
                    )
            elif kind == "capabilities":
                await interaction.response.send_message(
                    _capabilities_text(bot.service, channel_id)
                )
            elif kind == "continue":
                await interaction.response.defer(thinking=True)
                try:
                    sess = await bot.service.continue_session(channel_id)
                except RuntimeError as exc:
                    await interaction.followup.send(f"❌ {exc}")
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("continue_session failed")
                    await interaction.followup.send(f"❌ continue failed: {exc}")
                    return
                await interaction.followup.send(
                    f"▶️ Continued. Project: `{sess.project_path}`"
                )
            elif kind == "new":
                await interaction.response.defer(thinking=True)
                try:
                    new_id = await bot.service.new_thread(channel_id)
                except KeyError:
                    await interaction.followup.send(
                        "❌ No active session. `/codex start <path>` or `/codex continue` first."
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("new_thread failed")
                    await interaction.followup.send(f"❌ new failed: {exc}")
                    return
                await interaction.followup.send(
                    f"🆕 New thread `{new_id[:8]}` started."
                )
            elif kind == "cancel":
                if not bot.service.is_active(channel_id):
                    await interaction.response.send_message(
                        "⚠️ No active session.", ephemeral=True
                    )
                    return
                ok = await bot.service.cancel_active_turn(channel_id)
                await interaction.response.send_message(
                    "✋ Interrupted." if ok else "⚠️ No in-flight turn."
                )
            elif kind == "history":
                threads = bot.service.store.list_threads(channel_id, limit=10)
                if not threads:
                    await interaction.response.send_message(
                        "No threads recorded for this channel.", ephemeral=True
                    )
                    return
                import time as _time

                lines: list[str] = []
                for t in threads:
                    age_s = _time.time() - t.last_used_at
                    if age_s < 90:
                        age = f"{int(age_s)}s ago"
                    elif age_s < 3600:
                        age = f"{int(age_s / 60)}m ago"
                    elif age_s < 86400:
                        age = f"{int(age_s / 3600)}h ago"
                    else:
                        age = f"{int(age_s / 86400)}d ago"
                    preview = (t.preview or "(no preview)")[:80]
                    lines.append(
                        f"`{t.codex_thread_id[:8]}` · **{t.turn_count}** turns · {age}\n  {preview}"
                    )
                embed = discord.Embed(
                    title="🧵 Thread history",
                    description="\n\n".join(lines),
                    color=0x3498DB,
                )
                embed.set_footer(text="Resume now uses handoff ids from /codex stop")
                await interaction.response.send_message(embed=embed)
            elif kind == "resume":
                if not project_path:
                    handoffs = bot.service.store.list_handoffs(
                        channel_id, limit=RESUME_PICKER_LIMIT
                    )
                    if not handoffs:
                        await interaction.response.send_message(
                            "No handoffs recorded for this channel yet. "
                            "`/codex stop` writes one.",
                            ephemeral=True,
                        )
                        return
                    embed, view = _build_resume_picker(bot, channel_id, handoffs)
                    await interaction.response.send_message(embed=embed, view=view)
                    return
                match = bot._resolve_handoff_id(channel_id, project_path)
                if match is None:
                    await interaction.response.send_message(
                        f"❌ No handoff matching `{project_path}` in this channel.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.defer(thinking=True)
                try:
                    sess = await bot.service.resume_handoff(channel_id, match)
                except KeyError:
                    await interaction.followup.send(
                        "❌ Handoff not found."
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("resume_handoff failed")
                    await interaction.followup.send(f"❌ resume failed: {exc}")
                    return
                await interaction.followup.send(
                    f"⏮ Started fresh from handoff `{match[:18]}` "
                    f"on `{sess.project_path}`."
                )
            elif kind == "rollback":
                count = 1
                if project_path:
                    try:
                        count = max(1, int(project_path))
                    except ValueError:
                        await interaction.response.send_message(
                            "❌ `project_path` must be a positive integer count.",
                            ephemeral=True,
                        )
                        return
                if not bot.service.is_active(channel_id):
                    await interaction.response.send_message(
                        "⚠️ No active session.", ephemeral=True
                    )
                    return
                try:
                    await bot.service.rollback(channel_id, count=count)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("rollback failed")
                    await interaction.response.send_message(
                        f"❌ rollback failed: {exc}", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    f"⏪ Rolled back {count} turn{'s' if count != 1 else ''}."
                )
            elif kind == "compact":
                if not bot.service.is_active(channel_id):
                    await interaction.response.send_message(
                        "⚠️ No active session.", ephemeral=True
                    )
                    return
                await interaction.response.defer(thinking=True)
                try:
                    await bot.service.compact(channel_id)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("compact failed")
                    await interaction.followup.send(f"❌ compact failed: {exc}")
                    return
                await interaction.followup.send("🗜️ Compacting thread context.")

        @codex_cmd.autocomplete("project_path")
        async def project_path_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            return _project_path_choices(bot, interaction, current)

    # ------------------------------------------------------ event handlers

    async def setup_hook(self) -> None:
        if self.guild_id is not None:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("codex_rc: synced slash commands to guild=%s", self.guild_id)
        else:
            await self.tree.sync()
            logger.info("codex_rc: synced slash commands globally")

    async def on_ready(self) -> None:
        logger.info(
            "codex_rc bot ready: %s (id=%s) channels=%s",
            self.user,
            self.user.id if self.user else "?",
            "all" if not self.channel_ids else f"{len(self.channel_ids)} listed",
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.user and message.author.id == self.user.id:
            return
        channel_id_str = str(message.channel.id)
        uid_str = str(message.author.id)
        if not self.access.allows(uid_str, channel_id_str):
            await self._warn_blocked_user_once(message, uid_str)
            return
        if self.channel_ids and channel_id_str not in self.channel_ids:
            return
        content = message.content.strip()
        # Text-prefix fallback for clients where the slash autocomplete is
        # unavailable (mobile cache, copy-paste, etc.). Accepts three shapes:
        #   /codex start /path/to/project
        #   /codex action:start project_path:/path/to/project
        #   /model gpt-5-codex      (native Codex CLI slash; mapped through)
        if content.startswith("/codex"):
            await self._handle_text_codex_command(message, content[len("/codex"):].strip())
            return
        native = _route_native_slash(content)
        if native is not None:
            await self._handle_text_codex_command(message, native)
            return
        if content.startswith("/"):
            return  # other slash-style messages: not ours
        channel_id = str(message.channel.id)
        # Image attachments: Discord CDN URLs are publicly fetchable by Codex.
        image_urls = [
            a.url for a in message.attachments
            if (a.content_type or "").startswith("image/")
        ]
        if not content and not image_urls:
            return  # ignore empty messages (e.g. bare mentions, stickers)
        if not self.service.is_active(channel_id):
            await message.reply(
                "⚠️ No active codex_rc session here. Run `/codex start /path/to/project` first.",
                mention_author=False,
            )
            return
        try:
            await self.service.send_text(
                channel_id, content, image_urls=image_urls or None
            )
            # Remember the user message that drove this turn so the
            # turn-stage callback can attach reactions to the right anchor.
            self._last_user_message[channel_id] = message
            if image_urls:
                await message.add_reaction("🖼️")
        except Exception as exc:  # noqa: BLE001
            logger.exception("send_text failed")
            await message.reply(f"❌ {exc}", mention_author=False)

    async def _handle_text_codex_command(
        self, message: discord.Message, args_text: str
    ) -> None:
        channel_id = str(message.channel.id)
        action, project_path = _parse_text_codex_args(args_text)
        if action == "start":
            project_path, permission_preset = _parse_start_options(
                args_text, project_path
            )
            if not project_path:
                await message.reply(
                    "❌ Usage: `/codex start /path/to/project [--permissions <preset>]`",
                    mention_author=False,
                )
                return
            try:
                permission_kwargs = _start_permission_kwargs(permission_preset)
            except ValueError as exc:
                await message.reply(f"❌ {exc}", mention_author=False)
                return
            try:
                sess = await self.service.start_session(
                    channel_id=channel_id,
                    project_path=project_path,
                    **permission_kwargs,
                )
            except FileNotFoundError as exc:
                await message.reply(f"❌ {exc}", mention_author=False)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("start_session failed")
                await message.reply(f"❌ start failed: {exc}", mention_author=False)
                return
            await message.reply(
                f"🟢 Session started. Project: `{sess.project_path}`",
                mention_author=False,
            )
        elif action == "stop":
            await self.service.stop_session(channel_id)
            await message.reply("🛑 Session stopped.", mention_author=False)
        elif action == "history":
            await self._reply_thread_history(message, channel_id)
        elif action == "resume":
            await self._resume_thread(message, channel_id, project_path)
        elif action == "new":
            await self._new_thread(message, channel_id)
        elif action == "continue":
            await self._continue_session(message, channel_id)
        elif action == "cancel":
            await self._cancel_active_turn(message, channel_id)
        elif action == "rollback":
            await self._rollback_thread(message, channel_id, project_path)
        elif action == "model":
            await self._slash_model(message, channel_id, project_path)
        elif action == "permissions":
            await self._slash_permissions(message, channel_id, project_path)
        elif action == "review":
            await self._slash_review(message, channel_id, project_path)
        elif action == "fork":
            await self._slash_fork(message, channel_id)
        elif action == "goal":
            await self._slash_goal(message, channel_id, project_path)
        elif action in {"compact", "exit", "quit"}:
            # Codex CLI parity: /compact is an explicit ask; /exit ≡ /codex stop.
            if action == "compact":
                await self._slash_compact(message, channel_id)
            else:
                await self.service.stop_session(channel_id)
                await message.reply("🛑 Session stopped.", mention_author=False)
        elif action == "status":
            sess = self.service.store.get(channel_id)
            if sess is None:
                await message.reply(
                    "No session in this channel.", mention_author=False
                )
            else:
                status_text = (
                    self.service.status_text(channel_id)
                    if isinstance(self.service, Service)
                    else None
                )
                await message.reply(
                    status_text
                    or (
                        f"channel `{sess.channel_id}` · state `{sess.state}` · "
                        f"project `{sess.project_path}`\n"
                        f"thread `{sess.codex_thread_id or '—'}` · "
                        f"handoff `{sess.handoff_id or '—'}` · "
                        f"sandbox `{sess.sandbox}` · approval `{sess.approval}`"
                    ),
                    mention_author=False,
                )
        elif action == "capabilities":
            await message.reply(
                _capabilities_text(self.service, channel_id),
                mention_author=False,
            )
        else:
            await message.reply(
                "❌ Unknown action. Supported:\n"
                "`/codex start <path>`, `/codex stop`, `/codex status`, "
                "`/codex capabilities`, "
                "`/codex history`, `/codex resume <handoff-id>`, `/codex new`, "
                "`/codex cancel`, `/codex rollback [N]`.\n"
                "Codex slashes: `/model [id]`, `/permissions <preset>`, "
                "`/review [target]`, `/fork`, `/goal [text]`, `/compact`, "
                "`/new`, `/resume <handoff-id>`, `/status`, `/capabilities`, `/exit`.",
                mention_author=False,
            )

    async def _reply_thread_history(
        self, message: discord.Message, channel_id: str
    ) -> None:
        threads = self.service.store.list_threads(channel_id, limit=10)
        if not threads:
            await message.reply(
                "No threads recorded for this channel yet.", mention_author=False
            )
            return
        import time as _time

        lines: list[str] = []
        for t in threads:
            short_id = t.codex_thread_id[:8]
            age_s = _time.time() - t.last_used_at
            if age_s < 90:
                age = f"{int(age_s)}s ago"
            elif age_s < 3600:
                age = f"{int(age_s / 60)}m ago"
            elif age_s < 86400:
                age = f"{int(age_s / 3600)}h ago"
            else:
                age = f"{int(age_s / 86400)}d ago"
            preview = (t.preview or "(no preview)")[:80]
            lines.append(
                f"`{short_id}` · **{t.turn_count}** turns · {age}\n  {preview}"
            )
        embed = discord.Embed(
            title="🧵 Thread history",
            description="\n\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(text="Resume now uses handoff ids from /codex stop")
        await message.reply(embed=embed, mention_author=False)

    async def _resume_thread(
        self,
        message: discord.Message,
        channel_id: str,
        thread_id_arg: str | None,
    ) -> None:
        if not thread_id_arg:
            # No id provided → show last RESUME_PICKER_LIMIT handoffs as
            # clickable buttons.
            handoffs = self.service.store.list_handoffs(
                channel_id, limit=RESUME_PICKER_LIMIT
            )
            if not handoffs:
                await message.reply(
                    "No handoffs recorded for this channel yet. "
                    "`/codex stop` writes one.",
                    mention_author=False,
                )
                return
            embed, view = _build_resume_picker(self, channel_id, handoffs)
            await message.reply(embed=embed, view=view, mention_author=False)
            return
        # Allow prefix match for convenience.
        match = self._resolve_handoff_id(channel_id, thread_id_arg)
        if match is None:
            await message.reply(
                f"❌ No handoff matching `{thread_id_arg}` in this channel.",
                mention_author=False,
            )
            return
        try:
            sess = await self.service.resume_handoff(channel_id, match)
        except KeyError:
            await message.reply(
                "❌ Handoff not found.",
                mention_author=False,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("resume_handoff failed")
            await message.reply(f"❌ resume failed: {exc}", mention_author=False)
            return
        await message.reply(
            f"⏮ Started fresh from handoff `{match[:18]}` "
            f"on `{sess.project_path}`.",
            mention_author=False,
        )

    async def _new_thread(
        self, message: discord.Message, channel_id: str
    ) -> None:
        try:
            new_id = await self.service.new_thread(channel_id)
        except KeyError:
            await message.reply(
                "❌ No active session. Run `/codex start /path/to/project` first.",
                mention_author=False,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("new_thread failed")
            await message.reply(f"❌ new failed: {exc}", mention_author=False)
            return
        await message.reply(
            f"🆕 New thread `{new_id[:8]}` started.", mention_author=False
        )

    async def _continue_session(
        self, message: discord.Message, channel_id: str
    ) -> None:
        try:
            sess = await self.service.continue_session(channel_id)
        except RuntimeError as exc:
            await message.reply(f"❌ {exc}", mention_author=False)
            return
        except FileNotFoundError as exc:
            await message.reply(f"❌ {exc}", mention_author=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("continue_session failed")
            await message.reply(
                f"❌ continue failed: {exc}", mention_author=False
            )
            return
        await message.reply(
            f"▶️ Continued. Project: `{sess.project_path}`",
            mention_author=False,
        )

    async def _cancel_active_turn(
        self, message: discord.Message, channel_id: str
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply(
                "⚠️ No active codex_rc session in this channel.",
                mention_author=False,
            )
            return
        cancelled = await self.service.cancel_active_turn(channel_id)
        if cancelled:
            await message.reply("✋ Interrupted.", mention_author=False)
        else:
            await message.reply(
                "⚠️ No in-flight turn to cancel.",
                mention_author=False,
            )

    async def _rollback_thread(
        self,
        message: discord.Message,
        channel_id: str,
        count_arg: str | None,
    ) -> None:
        count = 1
        if count_arg:
            try:
                count = max(1, int(count_arg))
            except ValueError:
                await message.reply(
                    "❌ Usage: `/codex rollback [N]` (N is a positive integer).",
                    mention_author=False,
                )
                return
        if not self.service.is_active(channel_id):
            await message.reply(
                "⚠️ No active codex_rc session in this channel.",
                mention_author=False,
            )
            return
        try:
            await self.service.rollback(channel_id, count=count)
        except Exception as exc:  # noqa: BLE001
            logger.exception("rollback failed")
            await message.reply(f"❌ rollback failed: {exc}", mention_author=False)
            return
        await message.reply(
            f"⏪ Rolled back {count} turn{'s' if count != 1 else ''}.",
            mention_author=False,
        )

    async def _warn_blocked_user_once(
        self, message: discord.Message, uid: str
    ) -> None:
        if uid in self._warned_blocked_users:
            return
        self._warned_blocked_users.add(uid)
        try:
            await message.reply(
                "⚠️ You are not on this bot's allowlist. Ask the operator to "
                "add your Discord user ID. Subsequent messages will be ignored "
                "silently.",
                mention_author=False,
                delete_after=30,
            )
        except discord.HTTPException:
            logger.warning(
                "codex_rc: could not deliver blocked-user warning to uid=%s", uid
            )

    # ----------------------------------------------------- Codex slash passthroughs

    async def _slash_model(
        self, message: discord.Message, channel_id: str, name: str | None
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply(
                "⚠️ No active session.", mention_author=False
            )
            return
        try:
            result = await self.service.set_model(channel_id, name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("/model failed")
            await message.reply(f"❌ /model failed: {exc}", mention_author=False)
            return
        if name is None:
            models = result.get("models") if isinstance(result, dict) else None
            if isinstance(models, list) and models:
                lines = []
                for m in models[:20]:
                    if isinstance(m, dict):
                        mid = m.get("id") or m.get("name") or "?"
                        label = m.get("displayName") or m.get("label") or mid
                        lines.append(f"`{mid}` — {label}")
                    elif isinstance(m, str):
                        lines.append(f"`{m}`")
                body = "\n".join(lines) or "(no models listed)"
            else:
                body = f"```\n{result}\n```"[:1900]
            await message.reply(
                f"**Available models**\n{body}\n\nUse `/model <id>` to switch.",
                mention_author=False,
            )
        else:
            await message.reply(
                f"🔀 Switched model to `{name}`.", mention_author=False
            )

    async def _slash_permissions(
        self, message: discord.Message, channel_id: str, preset: str | None
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply("⚠️ No active session.", mention_author=False)
            return
        if not preset:
            await message.reply(
                "❌ Usage: `/permissions <read-only|workspace-write|auto|untrusted>`",
                mention_author=False,
            )
            return
        try:
            applied = await self.service.set_permissions(channel_id, preset)
        except ValueError as exc:
            await message.reply(f"❌ {exc}", mention_author=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("/permissions failed")
            await message.reply(
                f"❌ /permissions failed: {exc}", mention_author=False
            )
            return
        body = " · ".join(f"{k}=`{v}`" for k, v in applied.items())
        await message.reply(
            f"🛂 Permissions set to **{preset}** ({body}).",
            mention_author=False,
        )

    async def _slash_review(
        self, message: discord.Message, channel_id: str, target: str | None
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply("⚠️ No active session.", mention_author=False)
            return
        try:
            await self.service.review(channel_id, target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("/review failed")
            await message.reply(f"❌ /review failed: {exc}", mention_author=False)
            return
        tail = f" against `{target}`" if target else ""
        await message.reply(
            f"🔍 Review started{tail}. Results will stream into this channel.",
            mention_author=False,
        )

    async def _slash_fork(
        self, message: discord.Message, channel_id: str
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply("⚠️ No active session.", mention_author=False)
            return
        try:
            new_id = await self.service.fork(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("/fork failed")
            await message.reply(f"❌ /fork failed: {exc}", mention_author=False)
            return
        await message.reply(
            f"🌿 Forked into new thread `{new_id[:8]}`. Next message lands on the branch.",
            mention_author=False,
        )

    async def _slash_goal(
        self, message: discord.Message, channel_id: str, text: str | None
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply("⚠️ No active session.", mention_author=False)
            return
        try:
            applied = await self.service.set_goal(channel_id, text or "")
        except Exception as exc:  # noqa: BLE001
            logger.exception("/goal failed")
            await message.reply(f"❌ /goal failed: {exc}", mention_author=False)
            return
        if not applied:
            await message.reply("🎯 Goal cleared.", mention_author=False)
        else:
            await message.reply(
                f"🎯 Goal set: {applied[:200]}", mention_author=False
            )

    async def _slash_compact(
        self, message: discord.Message, channel_id: str
    ) -> None:
        if not self.service.is_active(channel_id):
            await message.reply("⚠️ No active session.", mention_author=False)
            return
        try:
            await self.service.compact(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("/compact failed")
            await message.reply(f"❌ /compact failed: {exc}", mention_author=False)
            return
        await message.reply("🗜️ Compacting thread context.", mention_author=False)

    async def apply_turn_stage_reaction(
        self, channel_id: str, stage: str
    ) -> None:
        """Attach a per-stage emoji to the user message that drove this turn.

        Called by the Service via the on_turn_stage callback. Stages map:
          started     → 🤔
          file_change → ⚙️
          completed   → ✅ (and transient stages are removed)
        """
        msg = self._last_user_message.get(channel_id)
        if msg is None:
            return
        emoji_map = {"started": "🤔", "file_change": "⚙️", "completed": "✅"}
        emoji = emoji_map.get(stage)
        if not emoji:
            return
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            logger.debug(
                "codex_rc: stage reaction add failed channel=%s stage=%s",
                channel_id, stage,
            )
        if stage == "completed" and self.user is not None:
            for transient in ("🤔", "⚙️"):
                try:
                    await msg.remove_reaction(transient, self.user)
                except (discord.HTTPException, AttributeError):
                    pass

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        """✋ on any message in an allowed channel → cancel current turn."""
        if payload.user_id == (self.user.id if self.user else 0):
            return
        emoji = payload.emoji.name if payload.emoji else None
        if emoji != "✋":
            return
        channel_id = str(payload.channel_id)
        if not self.access.allows(str(payload.user_id), channel_id):
            return
        if not self.service.is_active(channel_id):
            return
        cancelled = await self.service.cancel_active_turn(channel_id)
        if cancelled:
            logger.info(
                "codex_rc: turn interrupted via ✋ reaction channel=%s user=%s",
                channel_id, payload.user_id,
            )

    def _resolve_thread_id(self, channel_id: str, prefix_or_full: str) -> str | None:
        prefix_or_full = prefix_or_full.strip("`").strip()
        threads = self.service.store.list_threads(channel_id, limit=50)
        for t in threads:
            if t.codex_thread_id == prefix_or_full:
                return t.codex_thread_id
        # Prefix match
        for t in threads:
            if t.codex_thread_id.startswith(prefix_or_full):
                return t.codex_thread_id
        return None

    def _resolve_handoff_id(self, channel_id: str, prefix_or_full: str) -> str | None:
        prefix_or_full = prefix_or_full.strip("`").strip()
        handoffs = self.service.store.list_handoffs(channel_id, limit=50)
        for h in handoffs:
            if h.handoff_id == prefix_or_full:
                return h.handoff_id
        for h in handoffs:
            if h.handoff_id.startswith(prefix_or_full):
                return h.handoff_id
        return None

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        if not interaction.data:
            return
        custom_id = str(interaction.data.get("custom_id", ""))
        if not custom_id.startswith("codex_rc:"):
            return
        # ui.View attached buttons (resume picker etc.) have their own
        # callbacks; don't double-fire approval logic on them.
        if custom_id.startswith(RESUME_BUTTON_PREFIX):
            return
        channel_id = str(interaction.channel_id)
        if not self.access.allows(str(interaction.user.id), channel_id):
            await interaction.response.send_message(
                "❌ Not allowed.", ephemeral=True
            )
            return
        accepted = await self.service.handle_button(channel_id, custom_id)
        if accepted:
            workflow_decision = parse_workflow_custom_id(custom_id)
            if custom_id.startswith(WORKFLOW_CUSTOM_ID_PREFIX) and workflow_decision:
                token = workflow_decision[1]
                decision = {
                    "approve_a": "Plan A approved; continuing",
                    "approve_b": "Plan B approved; continuing",
                    "cancel": "workflow canceled",
                }.get(token, token)
            else:
                decision = custom_id.split(":")[-1]
            await interaction.response.send_message(
                f"✅ Recorded: `{decision}`"
            )
        else:
            await interaction.response.send_message(
                "❌ Prompt expired or unknown.", ephemeral=True
            )

    # ------------------------------------------------------ outbound (post)

    async def post(self, channel_id: str, payload: dict[str, Any]) -> None:
        """Service → Discord channel send. Wired into Service.post."""
        try:
            cid = int(channel_id)
        except ValueError:
            logger.warning("post: invalid channel_id %r", channel_id)
            return
        channel = self.get_channel(cid)
        if channel is None:
            try:
                channel = await self.fetch_channel(cid)
            except discord.HTTPException:
                logger.exception("post: cannot fetch channel %s", cid)
                return
        kwargs = _payload_to_message_kwargs(payload)
        if not kwargs:
            return
        try:
            await channel.send(**kwargs)  # type: ignore[union-attr]
        except discord.RateLimited as exc:
            # discord.py 2.4 raises this only when ``max_ratelimit_timeout``
            # is exceeded; otherwise it transparently sleeps and retries.
            # Surface anything that escapes so ops notices.
            logger.error(
                "codex_rc: channel.send rate-limited beyond timeout "
                "channel=%s retry_after=%.1fs",
                cid, getattr(exc, "retry_after", -1.0),
            )
            # Re-raise: the caller may need to roll back deferred state.
            raise
        except discord.HTTPException as exc:
            status = getattr(exc, "status", "?")
            logger.exception(
                "codex_rc: channel.send failed channel=%s status=%s — %s",
                cid, status, exc,
            )
            # Re-raise so the caller (ChannelService) can detect post failures
            # and roll back any pending state (approval / workflow prompts that
            # never reached Discord, leaving the bot effectively frozen).
            raise

    async def post_update(
        self,
        channel_id: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None:
        """Send once for ``message_key`` and edit that same message after.

        Used by animated status surfaces such as plan progress. If Discord no
        longer has the old message, a fresh one is sent and stored.
        """
        try:
            cid = int(channel_id)
        except ValueError:
            logger.warning("post_update: invalid channel_id %r", channel_id)
            return
        channel = self.get_channel(cid)
        if channel is None:
            try:
                channel = await self.fetch_channel(cid)
            except discord.HTTPException:
                logger.exception("post_update: cannot fetch channel %s", cid)
                return
        kwargs = _payload_to_message_kwargs(payload)
        if not kwargs:
            return
        editable = getattr(self, "_editable_messages", None)
        if editable is None:
            editable = {}
            self._editable_messages = editable
        key = (channel_id, message_key)
        msg = editable.get(key)
        if msg is not None:
            try:
                await msg.edit(**kwargs)
                return
            except discord.NotFound:
                editable.pop(key, None)
            except discord.HTTPException as exc:
                logger.debug(
                    "post_update: edit failed channel=%s key=%s (%s)",
                    cid,
                    message_key,
                    exc,
                )
                return
        try:
            editable[key] = await channel.send(**kwargs)  # type: ignore[union-attr]
        except discord.HTTPException as exc:
            logger.warning(
                "post_update: send failed channel=%s key=%s (%s)",
                cid,
                message_key,
                exc,
            )

    async def post_animated(
        self,
        channel_id: str,
        frames: list[str],
        *,
        interval_s: float = 1.0,
        loop: bool = True,
    ) -> None:
        """Send ``frames[0]`` and edit the same message through the rest
        at ``interval_s`` cadence.

        ``loop=True`` (default) cycles forever until cancelled; the caller
        is expected to cancel via ``Task.cancel()`` on turn end.
        ``loop=False`` runs one pass and stops on ``frames[-1]``.

        Cheap: one ``send`` + N ``edit``s, no upload. Discord channel
        edit rate limit ≈ 1/sec, so keep ``interval_s`` >= 1.0.
        """
        if not frames:
            return
        try:
            cid = int(channel_id)
        except ValueError:
            return
        channel = self.get_channel(cid)
        if channel is None:
            try:
                channel = await self.fetch_channel(cid)
            except discord.HTTPException:
                return
        try:
            msg = await channel.send(content=frames[0])  # type: ignore[union-attr]
        except discord.HTTPException as exc:
            logger.warning("post_animated: send failed: %s", exc)
            return
        i = 1
        while True:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return
            try:
                await msg.edit(content=frames[i % len(frames)])
            except asyncio.CancelledError:
                return
            except discord.HTTPException as exc:
                logger.debug("post_animated: edit stopped (%s)", exc)
                return
            i += 1
            if not loop and i >= len(frames):
                return

# ============================================================ entry

def _load_runtime() -> tuple[Settings, str, AccessControl, set[str] | None, int | None]:
    settings = load_settings_from_env()
    token = os.environ.get("CODEX_RC_DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CODEX_RC_DISCORD_TOKEN is not set in environment / .env")
    channel_ids_raw = os.environ.get("CODEX_RC_DISCORD_CHANNEL_IDS", "").strip()
    channel_ids: set[str] | None = (
        {s.strip() for s in channel_ids_raw.split(",") if s.strip()}
        if channel_ids_raw
        else None
    )
    access = AccessControl(settings.access_map)
    guild_raw = os.environ.get("CODEX_RC_DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_raw) if guild_raw else None
    return settings, token, access, channel_ids, guild_id


async def run() -> None:
    settings, token, access, channel_ids, guild_id = _load_runtime()
    store = SessionStore(
        settings.memory_path,
        max_threads=settings.thread_history_max,
    )

    bot_ref: list[CodexRcBot | None] = [None]

    async def post(channel_id: str, payload: dict[str, Any]) -> None:
        bot = bot_ref[0]
        if bot is None:
            return
        await bot.post(channel_id, payload)

    async def post_update(
        channel_id: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None:
        bot = bot_ref[0]
        if bot is None:
            return
        await bot.post_update(channel_id, message_key, payload)

    async def post_animated(
        channel_id: str,
        frames: list[str],
        *,
        interval_s: float = 1.0,
        loop: bool = True,
    ) -> None:
        bot = bot_ref[0]
        if bot is None:
            return
        await bot.post_animated(
            channel_id, frames, interval_s=interval_s, loop=loop
        )

    async def on_turn_stage(channel_id: str, stage: str, _params: dict) -> None:
        bot = bot_ref[0]
        if bot is None:
            return
        await bot.apply_turn_stage_reaction(channel_id, stage)

    auto_approve = os.environ.get(
        "CODEX_RC_AUTO_APPROVE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    owner_mention_id = os.environ.get("CODEX_RC_MENTION_ON_COMPLETE", "").strip() or None
    try:
        auto_compact_threshold = int(
            os.environ.get("CODEX_RC_AUTO_COMPACT_INPUT_TOKENS", "0").strip() or 0
        )
    except ValueError:
        auto_compact_threshold = 0

    service = Service(
        store=store,
        log_root=settings.log_root,
        debug_root=settings.debug_root,
        post=post,
        post_update=post_update,
        post_animated=post_animated,
        sandbox_default=settings.sandbox_default,
        approval_default=settings.approval_default,
        verbose_notifs=settings.verbose_notifs,
        event_log_mode=settings.event_log_mode,
        error_log_retention_days=settings.error_log_retention_days,
        transport_mode=settings.transport_mode,
        on_turn_stage=on_turn_stage,
        owner_mention_id=owner_mention_id,
        auto_approve=auto_approve,
        auto_compact_threshold_tokens=auto_compact_threshold,
        workflow_enabled=True,
    )

    bot = CodexRcBot(
        access=access,
        channel_ids=channel_ids,
        guild_id=guild_id,
        service=service,
    )
    bot_ref[0] = bot

    # Optional ops-channel log forwarding. Only installs while the bot runs
    # so unit tests / setup don't grab a logger handler.
    from .ops_channel import drain_ops_channel, install_ops_handler

    ops_channel_id = os.environ.get("CODEX_RC_OPS_CHANNEL_ID", "").strip()
    ops_drain_task: asyncio.Task[None] | None = None
    ops_handler = None
    if ops_channel_id:
        ops_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        ops_handler = install_ops_handler(ops_queue)
        ops_drain_task = asyncio.create_task(
            drain_ops_channel(ops_queue, ops_channel_id, post),
            name="codex-rc-ops-drain",
        )
        logger.info("codex_rc: ops channel forwarding → %s", ops_channel_id)

    # Optional /healthz endpoint for external uptime probes.
    health_server = None
    try:
        health_port = int(
            os.environ.get("CODEX_RC_HEALTH_PORT", "0").strip() or 0
        )
    except ValueError:
        health_port = 0
    if health_port > 0:
        from . import __version__ as _version
        from .health_server import serve_health

        health_host = os.environ.get("CODEX_RC_HEALTH_HOST", "127.0.0.1").strip()

        def health_snapshot() -> dict:
            return {
                "version": _version,
                "active_channels": len(service._channels),  # noqa: SLF001
                "transport": settings.transport_mode,
            }

        health_server = await serve_health(
            health_host, health_port, snapshot=health_snapshot
        )

    try:
        await bot.start(token)
    finally:
        # Tear down active channels first so each codex app-server child is
        # terminated and its tmux session killed before we let go of the
        # event loop. Without this, SIGINT / disconnect leaves orphan
        # `codex app-server` processes behind.
        try:
            await service.shutdown()
        except Exception:
            logger.exception("codex_rc: service shutdown failed")
        if health_server is not None:
            health_server.close()
            try:
                await health_server.wait_closed()
            except Exception:
                pass
        if ops_drain_task is not None:
            ops_drain_task.cancel()
            try:
                await ops_drain_task
            except (asyncio.CancelledError, Exception):
                pass
        if ops_handler is not None:
            logging.getLogger("codex_rc").removeHandler(ops_handler)
        await bot.close()


def _configure_logging() -> None:
    """Console at ``CODEX_RC_LOG_LEVEL`` (default WARNING); optional rotating
    file at ``CODEX_RC_LOG_PATH`` always at DEBUG for post-mortem analysis.

    ``CODEX_RC_LOG_FORMAT=json`` swaps both handlers to a JSON formatter
    that shippers like Vector/Loki/CloudWatch can ingest directly.
    """
    from logging.handlers import RotatingFileHandler

    from .log_format import JsonLogFormatter

    level_name = os.environ.get("CODEX_RC_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_path = os.environ.get("CODEX_RC_LOG_PATH", "").strip()
    log_format = os.environ.get("CODEX_RC_LOG_FORMAT", "text").strip().lower()

    if log_format == "json":
        console_fmt: logging.Formatter = JsonLogFormatter(include_funcname=False)
        file_fmt: logging.Formatter = JsonLogFormatter(include_funcname=True)
    else:
        console_fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s — %(message)s"
        )
        file_fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s %(funcName)s:%(lineno)d — %(message)s"
        )

    root = logging.getLogger()
    root.setLevel(min(level, logging.DEBUG))

    # Clean slate so re-runs don't double up.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(console_fmt)
    root.addHandler(console)

    if log_path:
        from pathlib import Path as _Path

        _Path(log_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            _Path(log_path).expanduser(),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(file_fmt)
        # Scope the file handler to our namespace so we don't capture every
        # third-party DEBUG line.
        codex_rc_logger = logging.getLogger("codex_rc")
        codex_rc_logger.addHandler(fh)
        codex_rc_logger.setLevel(logging.DEBUG)
        codex_rc_logger.propagate = True
        logging.getLogger("discord").addHandler(fh)
        logging.getLogger("discord").setLevel(logging.INFO)


def main() -> None:  # pragma: no cover — console script
    from dotenv import load_dotenv

    load_dotenv()
    _configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


__all__ = ["CodexRcBot", "run", "main"]
