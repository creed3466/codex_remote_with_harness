"""V2 ``ServerNotification`` → Discord payload routing.

Each Codex notification (method + params dict, exactly the wire shape
:class:`CodexRpcClient` surfaces) is turned into zero or more
:class:`DiscordPayload` objects ready for ``Message.send``.

Streaming events (``item/agentMessage/delta``, ``item/reasoning/summaryTextDelta``,
``item/plan/delta``, ``item/commandExecution/outputDelta``) accumulate per
``(method, threadId, turnId, itemId)`` and flush when:

* the buffer is about to exceed Discord's 2000-char content limit
  (we use ``FLUSH_AT`` = 1800 to leave headroom for any prefix), or
* a matching ``item/completed`` / ``turn/completed`` arrives.

This module produces payloads only — it does not touch Discord HTTP.
Day 6's ``server.py`` is what actually POSTs to the gateway.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

JsonObj = dict[str, Any]

# Discord limits (https://discord.com/developers/docs/resources/channel)
DISCORD_CONTENT_MAX = 2000
DISCORD_EMBED_DESC_MAX = 4096
FLUSH_AT = 1800  # leave room for prefixes/footers
EMBED_FLUSH_AT = 3800
OUTBOUND_TEXT_LINE_LIMIT = 60
OUTBOUND_TEXT_CHAR_LIMIT = 5000
OUTBOUND_TEXT_PREVIEW_LINES = 28
OUTBOUND_TEXT_PREVIEW_CHARS = 1200
OUTBOUND_BLOCK_LINE_LIMIT = 45
OUTBOUND_BLOCK_CHAR_LIMIT = 2400
OUTBOUND_BLOCK_PREVIEW_LINES = 28
OUTBOUND_BLOCK_PREVIEW_CHARS = 1200
EXEC_FAILURE_TAIL_LINES = 4
EXEC_FAILURE_TAIL_CHARS = 700

# Brand-aligned palette.
COLOR_AGENT = 0x5865F2  # blurple
COLOR_REASONING = 0x99AAB5  # gray
COLOR_PLAN = 0x9B59B6  # purple
COLOR_DIFF = 0x2F3136  # near-black
COLOR_EXEC = 0x23272A  # darker
COLOR_TURN_OK = 0x57F287  # green
COLOR_INFO = 0x3498DB  # blue
COLOR_WARN = 0xFEE75C  # yellow
COLOR_ERROR = 0xED4245  # red
COLOR_SYSTEM = 0x95A5A6  # neutral

# Natural break preferences for streaming flush (longest-first).
_BREAKS = ("\n\n", "\n```\n", ". ", "! ", "? ", "\n")
_FENCED_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def _human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kib = num_bytes / 1024
    if kib < 1024:
        return f"{kib:.1f} KB"
    return f"{kib / 1024:.1f} MB"


def _preview_text(text: str, *, max_lines: int, max_chars: int) -> tuple[str, int]:
    lines = text.splitlines()
    out: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if out and (len(out) >= max_lines or used + extra > max_chars):
            break
        if not out and extra > max_chars:
            return line[:max_chars], 1
        out.append(line)
        used += extra
    return "\n".join(out), len(out)


def _omitted_note(kind: str, *, omitted_lines: int, omitted_bytes: int) -> str:
    if omitted_lines > 0:
        amount = f"{omitted_lines} lines / {_human_size(omitted_bytes)}"
    else:
        amount = _human_size(omitted_bytes)
    return (
        f"[{kind} omitted: {amount}. "
        "Codex has read it locally; ask for a specific section if needed.]"
    )


def _compact_fenced_block(match: re.Match[str]) -> str:
    info = match.group(1).strip()
    body = match.group(2).rstrip("\n")
    lines = body.splitlines()
    if len(lines) <= OUTBOUND_BLOCK_LINE_LIMIT and len(body) <= OUTBOUND_BLOCK_CHAR_LIMIT:
        return match.group(0)

    preview, preview_lines = _preview_text(
        body,
        max_lines=OUTBOUND_BLOCK_PREVIEW_LINES,
        max_chars=OUTBOUND_BLOCK_PREVIEW_CHARS,
    )
    preview = preview.replace("```", "'''")
    omitted_text = body[len(preview):]
    note = _omitted_note(
        "Read output",
        omitted_lines=max(0, len(lines) - preview_lines),
        omitted_bytes=len(omitted_text.encode("utf-8")),
    )
    opener = f"```{info}\n" if info else "```\n"
    return f"{opener}{preview}\n\n{note}\n```"


def compact_outbound_text(text: str) -> str:
    """Trim large paste-like assistant output before it reaches Discord.

    Short answers pass through unchanged. Large file/log dumps are replaced
    with a bounded preview plus an omission marker so Discord stays readable
    while Codex still keeps the full content in its local context.
    """
    if not text:
        return text

    compacted = _FENCED_BLOCK_RE.sub(_compact_fenced_block, text)
    lines = compacted.splitlines()
    if not (
        len(lines) >= OUTBOUND_TEXT_LINE_LIMIT
        or (len(compacted) >= OUTBOUND_TEXT_CHAR_LIMIT and len(lines) >= 20)
    ):
        return compacted

    preview, preview_lines = _preview_text(
        compacted,
        max_lines=OUTBOUND_TEXT_PREVIEW_LINES,
        max_chars=OUTBOUND_TEXT_PREVIEW_CHARS,
    )
    omitted_text = compacted[len(preview):]
    note = _omitted_note(
        "Long read/log output",
        omitted_lines=max(0, len(lines) - preview_lines),
        omitted_bytes=len(omitted_text.encode("utf-8")),
    )
    return f"{preview.rstrip()}\n\n{note}"


def _exec_failure_excerpt(output: str) -> str:
    """Return a compact tail for failed commandExecution embeds."""
    if not output:
        return ""
    lines = output.splitlines()
    tail_lines = lines[-EXEC_FAILURE_TAIL_LINES:]
    tail = "\n".join(tail_lines).strip()
    if len(tail) > EXEC_FAILURE_TAIL_CHARS:
        tail = tail[-EXEC_FAILURE_TAIL_CHARS:].lstrip()

    omitted_lines = max(0, len(lines) - len(tail_lines))
    omitted_chars = max(0, len(output) - len(tail))
    if omitted_lines or omitted_chars:
        note = (
            f"[output truncated: showing last {min(len(lines), EXEC_FAILURE_TAIL_LINES)} "
            f"line(s), omitted {omitted_lines} line(s) / {_human_size(omitted_chars)}]"
        )
        return f"{tail}\n\n{note}" if tail else note
    return tail


# --------------------------------------------------------------------- payload

@dataclass(slots=True)
class DiscordEmbed:
    title: str | None = None
    description: str | None = None
    color: int | None = None
    fields: list[dict[str, Any]] = field(default_factory=list)
    footer_text: str | None = None
    timestamp: str | None = None

    def to_dict(self) -> JsonObj:
        out: JsonObj = {}
        if self.title is not None:
            out["title"] = self.title[:256]
        if self.description is not None:
            description = compact_outbound_text(self.description)
            out["description"] = description[:DISCORD_EMBED_DESC_MAX]
        if self.color is not None:
            out["color"] = self.color
        if self.fields:
            out["fields"] = self.fields[:25]
        if self.footer_text is not None:
            out["footer"] = {"text": self.footer_text[:2048]}
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        return out


@dataclass(slots=True)
class DiscordPayload:
    content: str | None = None
    embed: DiscordEmbed | None = None

    def to_dict(self) -> JsonObj:
        out: JsonObj = {}
        if self.content is not None:
            content = compact_outbound_text(self.content)
            out["content"] = content[:DISCORD_CONTENT_MAX]
        if self.embed is not None:
            out["embeds"] = [self.embed.to_dict()]
        return out


# ------------------------------------------------------------------- chunking

def chunk_text(text: str, max_len: int = FLUSH_AT) -> list[str]:
    """Split ``text`` into pieces each <= ``max_len`` chars.

    Prefers breaks on ``\\n\\n``, then ``\\n``, then space. Falls back to a hard
    cut. Empty input → empty list.
    """
    text = text or ""
    if not text:
        return []
    pieces: list[str] = []
    while len(text) > max_len:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            i = text.rfind(sep, 0, max_len)
            if i > max_len // 2:
                cut = i + len(sep)
                break
        if cut <= 0:
            cut = max_len
        pieces.append(text[:cut])
        text = text[cut:]
    if text:
        pieces.append(text)
    return pieces


# --------------------------------------------------------------------- router

StreamKey = tuple[str, str, str, str]  # (method, threadId, turnId, itemId)


@dataclass
class _StreamBuffer:
    method: str
    item_id: str
    thread_id: str
    turn_id: str
    text: str = ""


# Methods that should be accumulated and chunked as plain text.
_TEXT_STREAM_METHODS: set[str] = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "item/plan/delta",
}
# Methods accumulated and flushed inside a code block (embed description).
_EXEC_STREAM_METHODS: set[str] = {
    "item/commandExecution/outputDelta",
}

# Quiet, non-content events we suppress unless verbose=True.
_QUIET_DEFAULT: set[str] = {
    "thread/started",
    "thread/tokenUsage/updated",
    "thread/status/changed",
    "remoteControl/status/changed",
    "fs/changed",
    "skills/changed",
    "account/updated",
    "account/login/completed",
    "account/rateLimits/updated",
    "model/rerouted",
    "model/verification",
    "mcpServer/startupStatus/updated",
    "thread/realtime/transcript/delta",
    "thread/realtime/transcript/done",
    "thread/realtime/outputAudio/delta",
    "thread/realtime/sdp",
    "item/started",         # we summarise on item/completed instead
    "turn/started",         # cat-of-patience covers the 'in progress' UX
}


def _key(method: str, params: JsonObj) -> StreamKey:
    return (
        method,
        str(params.get("threadId", "")),
        str(params.get("turnId", "")),
        str(params.get("itemId", "")),
    )


def _natural_break_index(text: str) -> int:
    """Return the latest "good" break index in ``text`` (within the last 256
    chars) or -1 if none."""
    window_start = max(0, len(text) - 256)
    best = -1
    for sep in _BREAKS:
        i = text.rfind(sep, window_start)
        if i > best:
            best = i + len(sep)
    return best


class NotifRouter:
    """Stateful Codex notification → Discord payload converter."""

    def __init__(self, *, verbose: bool = False, stream_deltas: bool = False) -> None:
        """``stream_deltas``: when True, emit chunked "live preview" output as
        delta notifications cross :data:`FLUSH_AT`. Off by default because
        some models (e.g. gpt-5.5 with high reasoning effort) emit sub-token
        deltas that concatenate into garbled text; the authoritative final
        text always lands cleanly via ``item/completed``."""
        self.verbose = verbose
        self.stream_deltas = stream_deltas
        self._buffers: dict[StreamKey, _StreamBuffer] = {}

    # ----------------------------------------------------------- entry point

    def route(self, notif: JsonObj) -> list[DiscordPayload]:
        method = notif.get("method")
        params: JsonObj = notif.get("params") or {}
        if not isinstance(method, str):
            return []
        if method in _TEXT_STREAM_METHODS:
            return self._stream_text(method, params)
        if method in _EXEC_STREAM_METHODS:
            return self._stream_exec(method, params)

        handler = _HANDLERS.get(method)
        if handler:
            return handler(self, params)
        if method in _QUIET_DEFAULT and not self.verbose:
            return []
        return self._fallback(method, params)

    def flush_all(self) -> list[DiscordPayload]:
        out: list[DiscordPayload] = []
        for key in list(self._buffers.keys()):
            out.extend(self._drain(key))
        return out

    def flush_turn(self, thread_id: str, turn_id: str) -> list[DiscordPayload]:
        out: list[DiscordPayload] = []
        for key in list(self._buffers.keys()):
            if key[1] == thread_id and key[2] == turn_id:
                out.extend(self._drain(key))
        return out

    # ------------------------------------------------------------- streaming

    def _stream_text(self, method: str, params: JsonObj) -> list[DiscordPayload]:
        key = _key(method, params)
        buf = self._buffers.setdefault(
            key,
            _StreamBuffer(
                method=method,
                item_id=str(params.get("itemId", "")),
                thread_id=str(params.get("threadId", "")),
                turn_id=str(params.get("turnId", "")),
            ),
        )
        buf.text += str(params.get("delta", ""))

        if not self.stream_deltas:
            # Suppress live emission; cap buffer to bound memory in case the
            # turn never completes. The authoritative text lands at
            # item/completed.
            if len(buf.text) > 1_000_000:
                buf.text = buf.text[-500_000:]
            return []

        if len(buf.text) < FLUSH_AT:
            return []

        # Buffer crossed threshold — try to flush at a natural break.
        cut = _natural_break_index(buf.text[:FLUSH_AT])
        if cut <= 0:
            cut = FLUSH_AT
        head, buf.text = buf.text[:cut], buf.text[cut:]
        if not head.strip():
            return []
        head = compact_outbound_text(head)
        return [DiscordPayload(content=head)]

    def _stream_exec(self, method: str, params: JsonObj) -> list[DiscordPayload]:
        key = _key(method, params)
        buf = self._buffers.setdefault(
            key,
            _StreamBuffer(
                method=method,
                item_id=str(params.get("itemId", "")),
                thread_id=str(params.get("threadId", "")),
                turn_id=str(params.get("turnId", "")),
            ),
        )
        buf.text += str(params.get("delta", ""))
        # Tool output is intentionally not posted to Discord on success; the
        # final commandExecution item decides whether there is a failure worth
        # surfacing. Keep only a bounded tail for that final failure path.
        if len(buf.text) > 6 * FLUSH_AT:
            buf.text = buf.text[-3 * FLUSH_AT:]
        return []

    def _drain(self, key: StreamKey) -> list[DiscordPayload]:
        buf = self._buffers.pop(key, None)
        if buf is None or not buf.text:
            return []
        text = buf.text
        if buf.method in _EXEC_STREAM_METHODS:
            return []
        text = compact_outbound_text(text)
        return [DiscordPayload(content=chunk) for chunk in chunk_text(text)]

    # --------------------------------------------------------- handlers (non-streaming)

    def _on_turn_started(self, params: JsonObj) -> list[DiscordPayload]:
        thread_id = params.get("threadId", "?")
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title="⏳ Codex turn started",
                    color=COLOR_INFO,
                    footer_text=f"thread {thread_id}",
                )
            )
        ]

    def _on_turn_completed(self, params: JsonObj) -> list[DiscordPayload]:
        thread_id = str(params.get("threadId", ""))
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id") or params.get("turnId", ""))
        # No standalone "Turn complete" embed — the ✅ reaction on the user
        # message is the completion marker. We only flush any partial
        # stream buffers so trailing tokens don't get dropped.
        return (
            self.flush_turn(thread_id, turn_id)
            if thread_id and turn_id
            else self.flush_all()
        )

    def _on_turn_diff_updated(self, params: JsonObj) -> list[DiscordPayload]:
        _ = params
        return []

    def _on_turn_plan_updated(self, params: JsonObj) -> list[DiscordPayload]:
        _ = params
        # ChannelService owns plan progress so it can edit one Discord message
        # instead of posting a new embed for every plan update.
        return []

    def _on_error(self, params: JsonObj) -> list[DiscordPayload]:
        err = params.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        retry = params.get("willRetry")
        suffix = " (will retry)" if retry else ""
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title=f"❌ Error{suffix}",
                    description=str(msg or "(no message)"),
                    color=COLOR_ERROR,
                )
            )
        ]

    def _on_warning(self, params: JsonObj) -> list[DiscordPayload]:
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title="⚠️ Warning",
                    description=str(params.get("message", "")),
                    color=COLOR_WARN,
                )
            )
        ]

    def _on_guardian_warning(self, params: JsonObj) -> list[DiscordPayload]:
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title="🛡️ Guardian warning",
                    description=str(params.get("message", "")),
                    color=COLOR_WARN,
                )
            )
        ]

    def _on_context_compacted(self, params: JsonObj) -> list[DiscordPayload]:
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title="🗜️ Context compacted",
                    description="Older history was summarized to free room.",
                    color=COLOR_SYSTEM,
                    footer_text=f"thread {params.get('threadId', '?')}",
                )
            )
        ]

    def _on_thread_started(self, params: JsonObj) -> list[DiscordPayload]:
        tid = params.get("threadId", "?")
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title="🧵 Thread started",
                    description=f"`{tid}`",
                    color=COLOR_INFO,
                )
            )
        ]

    def _on_item_completed(self, params: JsonObj) -> list[DiscordPayload]:
        # Streaming deltas can be sub-token chunked and concat-corrupt with some
        # models. Prefer the server's authoritative reconstructed text on
        # ``item/completed`` and discard any buffered preview deltas.
        thread_id = str(params.get("threadId", ""))
        turn_id = str(params.get("turnId", ""))
        item = params.get("item") or {}
        item_id = str(item.get("id") or "")
        item_type = item.get("type", "")
        out: list[DiscordPayload] = []
        used_authoritative = False

        if item_type == "agentMessage":
            text = item.get("text") or ""
            if text:
                for method in _TEXT_STREAM_METHODS:
                    self._buffers.pop((method, thread_id, turn_id, item_id), None)
                text = compact_outbound_text(str(text))
                out.extend(DiscordPayload(content=c) for c in chunk_text(text))
                used_authoritative = True
        elif item_type == "commandExecution":
            for method in _EXEC_STREAM_METHODS:
                self._buffers.pop((method, thread_id, turn_id, item_id), None)
            out.extend(self._render_command_execution(item))
            used_authoritative = True

        if not used_authoritative:
            for method in _TEXT_STREAM_METHODS:
                out.extend(self._drain((method, thread_id, turn_id, item_id)))
            for method in _EXEC_STREAM_METHODS:
                self._buffers.pop((method, thread_id, turn_id, item_id), None)
        return out

    def _render_command_execution(self, item: JsonObj) -> list[DiscordPayload]:
        """Render only commandExecution failures that need operator attention.

        Successful tool actions like read/exec/bash/test are intentionally
        hidden to keep Discord focused on the assistant's answer and material
        file diffs. Read/exec/test failures are also hidden because the agent
        usually recovers immediately. Other failures still surface a red embed
        with exit code + tail so the operator can see why a turn is stuck.
        """
        cmd = str(item.get("command") or "")
        exit_code = item.get("exitCode")
        output = str(item.get("aggregatedOutput") or item.get("output") or "")
        _icon, label = classify_command(cmd)

        if label in {"read", "exec", "test", "build"}:
            return []

        summary = command_summary(cmd, label, max_len=56)

        # Failure path: surface why so the user can act.
        if isinstance(exit_code, int) and exit_code != 0:
            if exit_code == 127:
                return []
            tail = _exec_failure_excerpt(output)
            desc = f"exit `{exit_code}`"
            if tail.strip():
                desc += f"\n```\n{tail}\n```"
            return [
                DiscordPayload(
                    embed=DiscordEmbed(
                        title=f"❌ {label} · `{summary}`",
                        description=desc,
                        color=COLOR_ERROR,
                    )
                )
            ]

        return []

    def _on_token_usage(self, params: JsonObj) -> list[DiscordPayload]:
        if not self.verbose:
            return []
        usage = params.get("tokenUsage") or {}
        parts = []
        for k in ("inputTokens", "outputTokens", "totalTokens", "cachedTokens"):
            if k in usage:
                parts.append(f"{k}={usage[k]}")
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title="📊 Tokens",
                    description=" · ".join(parts) or "(no breakdown)",
                    color=COLOR_SYSTEM,
                )
            )
        ]

    # ----------------------------------------------------------- fallback

    def _fallback(self, method: str, params: JsonObj) -> list[DiscordPayload]:
        if not self.verbose:
            return []
        summary = _summarize_params(params)
        return [
            DiscordPayload(
                embed=DiscordEmbed(
                    title=f"ℹ️ {method}",
                    description=summary,
                    color=COLOR_SYSTEM,
                )
            )
        ]


# --------------------------------------------------------------------- helpers

def _summarize_params(params: JsonObj) -> str:
    if not params:
        return "(no params)"
    import json as _json

    s = _json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    return s[:1024] + ("…" if len(s) > 1024 else "")


_SHELL_WRAPPER_PREFIXES = (
    "/bin/zsh -lc ",
    "/bin/zsh -c ",
    "/bin/sh -c ",
    "/bin/bash -c ",
    "bash -c ",
    "sh -c ",
)

_READ_BINS = {
    "sed", "cat", "head", "tail", "less", "more", "wc", "ls", "find",
    "rg", "grep", "tree", "stat", "file", "awk",
}
_FS_BINS = {"mv", "cp", "rm", "mkdir", "touch", "ln", "chmod", "chown"}
_GIT_BINS = {"git"}
_TEST_BINS = {"pytest", "jest", "mocha", "vitest", "go-test"}
_BUILD_BINS = {"make", "cargo", "npm", "yarn", "pnpm", "go", "tsc", "ruff", "mypy"}
_WRITE_BINS = {"apply_patch", "tee", "patch"}


def _strip_shell_wrapper(cmd: str) -> str:
    s = cmd.strip()
    for prefix in _SHELL_WRAPPER_PREFIXES:
        if s.startswith(prefix):
            rest = s[len(prefix):].strip()
            for q in ('"', "'"):
                if rest.startswith(q) and rest.endswith(q) and len(rest) >= 2:
                    rest = rest[1:-1]
                    break
            return rest
    return s


def classify_command(cmd: str) -> tuple[str, str]:
    """Return ``(emoji, label)`` for a Claude-Code-style one-line summary."""
    stripped = _strip_shell_wrapper(cmd)
    tokens = stripped.split()
    first = tokens[0] if tokens else ""
    second = tokens[1] if len(tokens) > 1 else ""
    # Subcommand-sensitive overrides.
    if first == "sed" and " -i" in stripped:
        return "✏️", "wrote"
    if first in {"npm", "yarn", "pnpm"}:
        nxt = second
        if nxt == "run" and len(tokens) > 2:
            nxt = tokens[2]
        if nxt in {"test", "vitest", "jest"}:
            return "🧪", "test"
        # Other npm/yarn invocations: package install + build pipelines fall
        # under build.
        return "🔨", "build"
    if first == "go" and second == "test":
        return "🧪", "test"
    if first in _READ_BINS:
        return "📖", "read"
    if first in _FS_BINS:
        return "📁", "fs"
    if first in _GIT_BINS:
        return "🌿", "git"
    if first in _TEST_BINS:
        return "🧪", "test"
    if first in _BUILD_BINS:
        return "🔨", "build"
    if first in _WRITE_BINS:
        return "✏️", "wrote"
    return "🔧", "exec"


def command_summary(cmd: str, label: str, *, max_len: int = 80) -> str:
    """Compact human-friendly summary of a command for a single-line output."""
    stripped = _strip_shell_wrapper(cmd)
    if label == "read":
        # Pull out the first path-ish token after the command
        tokens = stripped.split()[1:]
        for tok in tokens:
            t = tok.strip("'\"")
            if not t:
                continue
            if t.startswith("-"):
                continue
            if "/" in t or "." in t:
                return t[:max_len]
    if label == "git":
        # git <subcommand> ...
        parts = stripped.split(maxsplit=2)
        if len(parts) >= 2:
            return " ".join(parts[:2])[:max_len]
    s = stripped.replace("\n", " ")
    return s[:max_len] + ("…" if len(s) > max_len else "")


_DIFF_HUNK = re.compile(r"^@@", re.MULTILINE)
_DIFF_FILE_HEADER = re.compile(r"^diff --git [ab]/(.+?) [ab]/(.+?)$", re.MULTILINE)
_DIFF_PLUS_PLUS = re.compile(r"^\+\+\+ [ab]/(.+?)$", re.MULTILINE)


@dataclass(slots=True, frozen=True)
class _DiffStat:
    """Per-file diff stats."""

    path: str
    adds: int
    dels: int


def parse_diff_stats(diff: str) -> list[_DiffStat]:
    """Split a unified diff into per-file ``+N/−N`` rows in stable order.

    Tolerates two header shapes:
      ``diff --git a/foo b/foo``       (git-style)
      ``+++ b/foo``                    (plain unified diff)
    """
    if not diff.strip():
        return []
    # Split on diff --git boundaries if present; otherwise on +++ lines.
    chunks: list[tuple[str, str]] = []  # (path, body)
    matches = list(_DIFF_FILE_HEADER.finditer(diff))
    if matches:
        for i, m in enumerate(matches):
            path = m.group(2)  # post-rename path
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
            chunks.append((path, diff[start:end]))
    else:
        plus = list(_DIFF_PLUS_PLUS.finditer(diff))
        for i, m in enumerate(plus):
            path = m.group(1)
            start = m.end()
            end = plus[i + 1].start() if i + 1 < len(plus) else len(diff)
            chunks.append((path, diff[start:end]))
    stats: list[_DiffStat] = []
    for path, body in chunks:
        adds = 0
        dels = 0
        for line in body.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                adds += 1
            elif line.startswith("-") and not line.startswith("---"):
                dels += 1
        stats.append(_DiffStat(path=path, adds=adds, dels=dels))
    return stats


def _diff_summary(diff: str) -> str:
    stats = parse_diff_stats(diff)
    if not stats:
        return ""
    total_adds = sum(s.adds for s in stats)
    total_dels = sum(s.dels for s in stats)
    return f"{len(stats)} file(s) · +{total_adds}/−{total_dels}"


def _diff_per_file_table(stats: list[_DiffStat], *, max_rows: int = 12) -> str:
    if not stats:
        return ""
    rows = []
    for s in stats[:max_rows]:
        rows.append(f"  {s.path}  +{s.adds}/−{s.dels}")
    if len(stats) > max_rows:
        rows.append(f"  …and {len(stats) - max_rows} more")
    return "\n".join(rows)


# Dispatch table built after methods exist on the class so the reference is
# resolvable.
_HANDLERS: dict[str, Any] = {
    "turn/completed": NotifRouter._on_turn_completed,
    "turn/diff/updated": NotifRouter._on_turn_diff_updated,
    "turn/plan/updated": NotifRouter._on_turn_plan_updated,
    "error": NotifRouter._on_error,
    "warning": NotifRouter._on_warning,
    "guardianWarning": NotifRouter._on_guardian_warning,
    "thread/compacted": NotifRouter._on_context_compacted,
    "item/completed": NotifRouter._on_item_completed,
    "thread/tokenUsage/updated": NotifRouter._on_token_usage,
}


__all__ = [
    "COLOR_AGENT",
    "COLOR_DIFF",
    "COLOR_ERROR",
    "COLOR_EXEC",
    "COLOR_INFO",
    "COLOR_PLAN",
    "COLOR_REASONING",
    "COLOR_SYSTEM",
    "COLOR_TURN_OK",
    "COLOR_WARN",
    "DISCORD_CONTENT_MAX",
    "DISCORD_EMBED_DESC_MAX",
    "DiscordEmbed",
    "DiscordPayload",
    "FLUSH_AT",
    "NotifRouter",
    "chunk_text",
    "classify_command",
    "compact_outbound_text",
    "command_summary",
]


# Suppress an unused import alarm if someone is wondering — `time` is here for
# future timestamp formatting hooks; safe to drop if it grows stale.
_ = time
