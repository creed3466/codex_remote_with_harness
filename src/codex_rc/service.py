"""Orchestration glue: 1 active Discord channel = 1 :class:`ChannelService`.

This is the layer where everything composes:

  CodexServerProcess  →  notifications  →  NotifRouter  →  DiscordPayload
                      ↑                                          │
                      │                                          ▼
                ApprovalRouter ◀──── button HTTP click ◀──  OpenClaw GW
                      │
                      └─── apply_resolution ──→ rpc_client.respond_to_request

Per-channel work runs inside one ``ChannelService`` task; the parent
:class:`Service` only routes incoming HTTP calls to the right ChannelService.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .approval_router import (
    ApprovalRouter,
    DiscordPrompt,
    apply_resolution,
    parse_custom_id,
)
from .notif_router import COLOR_WARN, DiscordEmbed, DiscordPayload, NotifRouter
from .rpc_client import JsonObj, RpcError
from .server_process import CodexServerProcess, default_error_log_path, default_log_dir
from .session_store import HandoffRecord, Session, SessionStore
from .tmux_host import create_tail_session, kill_session
from .workflow_handoff import (
    FinalSummary,
    HandoffParseError,
    Stage1To2Handoff,
    parse_final_summary,
    parse_stage_2_to_3,
    parse_stage_3_to_4,
)
from .workflow_router import (
    WORKFLOW_ANALYSIS_EFFORT,
    WORKFLOW_ANALYSIS_MODEL,
    WORKFLOW_STAGE_1_TO_2_BUDGET_TOKENS,
    DevelopmentWorkflowRouter,
    WorkflowDecision,
    WorkflowExecutionTurn,
    WorkflowSeedHandoff,
    parse_workflow_custom_id,
    strip_workflow_metadata,
)
from .workflow_store import WorkflowFileStore

logger = logging.getLogger(__name__)

PostToGateway = Callable[[str, JsonObj], Awaitable[None]]
PostEditable = Callable[[str, str, JsonObj], Awaitable[None]]
"""Signature: ``(channel_id, message_key, discord_payload_dict) -> awaitable``."""

PostAnimated = Callable[..., Awaitable[None]]
"""Signature: ``(channel_id, frames, *, interval_s) -> awaitable``."""

TurnStageCallback = Callable[[str, str, JsonObj], Awaitable[None]]
"""Signature: ``(channel_id, stage, params) -> awaitable``.

Stages: ``"started"`` (turn/started), ``"file_change"`` (first item/fileChange
in the current turn), ``"completed"`` (turn/completed or error)."""


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("codex_rc: %s=%r is not an int; using %d", name, raw, default)
        return default


def _env_str(name: str) -> str | None:
    raw = os.environ.get(name, "").strip()
    return raw or None


_DEFAULT_CATS_PATH = Path(__file__).parent / "assets" / "cats.json"
_BUNDLED_SOUL_PATH = Path(__file__).parent / "assets" / "soul.example.md"
_BUNDLED_RULES_DIR = Path(__file__).parent / "assets" / "rules.d"

_PERMISSION_PRESETS: dict[str, dict[str, str]] = {
    "read-only": {"sandbox": "read-only", "approvalPolicy": "on-request"},
    "workspace-write": {
        "sandbox": "workspace-write",
        "approvalPolicy": "on-request",
    },
    "auto": {"sandbox": "danger-full-access", "approvalPolicy": "never"},
    "untrusted": {"sandbox": "read-only", "approvalPolicy": "untrusted"},
}


def permission_preset_names() -> tuple[str, ...]:
    return tuple(_PERMISSION_PRESETS)


def permission_preset_config(preset: str) -> dict[str, str]:
    config = _PERMISSION_PRESETS.get(preset)
    if config is None:
        raise ValueError(
            f"unknown preset {preset!r}; expected one of "
            f"{sorted(_PERMISSION_PRESETS)}"
        )
    return dict(config)


@dataclass(frozen=True, slots=True)
class OperatorInstructions:
    soul: str | None = None
    rules: str | None = None

    def is_empty(self) -> bool:
        return not (self.soul or self.rules)


HANDOFF_IDLE_WAIT_SECONDS = 90.0
HANDOFF_COMPLETION_TIMEOUT_SECONDS = 180.0
WORKFLOW_STAGE_COMPLETION_TIMEOUT_SECONDS = 3600.0
_START_HELLO_FRAMES = ("h", "he", "hel", "hello")


@dataclass(slots=True)
class _HandoffCapture:
    handoff_id: str
    thread_id: str
    future: asyncio.Future[str]
    turn_id: str | None = None
    parts: list[str] | None = None

    def append(self, text: str) -> None:
        if not text:
            return
        if self.parts is None:
            self.parts = []
        self.parts.append(text)

    def body(self) -> str:
        return "\n\n".join(self.parts or []).strip()


@dataclass(slots=True)
class _TurnCompletionWait:
    thread_id: str
    future: asyncio.Future[None]
    turn_id: str | None = None


@dataclass(slots=True)
class _PlanProgressSnapshot:
    key: str
    explanation: str | None
    rows: list[tuple[str, str]]
    active_row: int | None
    completed: bool = False


_PLAN_ANSI_COLORS = ("31", "33", "32", "36", "34", "35")
_PLAN_CONTENT_MAX = 1900


def _ansi_clean(text: object, *, limit: int = 160) -> str:
    cleaned = str(text or "").replace("\x1b", "").replace("```", "'''")
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] if len(cleaned) > limit else cleaned


def _plan_message_key(params: JsonObj, fallback_turn_id: str | None) -> str:
    thread_id = str(params.get("threadId") or "thread")
    turn_id = _turn_id_from_notification(params) or fallback_turn_id or "turn"
    return f"plan:{thread_id}:{turn_id}"


def _plan_snapshot_from_notification(
    params: JsonObj,
    *,
    fallback_turn_id: str | None,
    previous: _PlanProgressSnapshot | None = None,
) -> _PlanProgressSnapshot:
    rows: list[tuple[str, str]] = []
    active_row: int | None = None
    for raw_step in params.get("plan") or []:
        step = raw_step if isinstance(raw_step, dict) else {}
        status = str(step.get("status") or "pending")
        text = _ansi_clean(step.get("step") or step.get("text") or "(step)")
        if status == "in_progress" and active_row is None:
            active_row = len(rows)
        rows.append((status, text))
    if not rows and previous is not None:
        rows = list(previous.rows)
        active_row = previous.active_row
    explanation = params.get("explanation")
    return _PlanProgressSnapshot(
        key=_plan_message_key(params, fallback_turn_id),
        explanation=_ansi_clean(explanation, limit=240) if explanation else None,
        rows=rows,
        active_row=active_row,
    )


def _highlight_active_text(text: str, *, tick: int) -> str:
    if not text:
        return text
    char_index = tick % len(text)
    color = _PLAN_ANSI_COLORS[tick % len(_PLAN_ANSI_COLORS)]
    return (
        f"{text[:char_index]}\x1b[{color}m{text[char_index]}\x1b[0m"
        f"{text[char_index + 1:]}"
    )


def _render_plan_progress(snapshot: _PlanProgressSnapshot, *, tick: int) -> str:
    lines: list[str] = []
    if snapshot.explanation:
        lines.append(snapshot.explanation)
        lines.append("")
    rows = snapshot.rows or [("pending", "(empty plan)")]
    for i, (status, text) in enumerate(rows):
        if snapshot.completed and status == "in_progress":
            status = "completed"
        if status == "completed":
            line = f"✅ {text}"
        elif status == "in_progress":
            active_text = _highlight_active_text(text, tick=tick) if i == snapshot.active_row else text
            line = f"▶ {active_text}"
        else:
            line = f"⬜ {text}"
        lines.append(line)
    body = "\n".join(lines).rstrip()
    content = f"```ansi\n{body}\n```"
    if len(content) <= _PLAN_CONTENT_MAX:
        return content
    return f"```ansi\n{body[: _PLAN_CONTENT_MAX - 24].rstrip()}\n... truncated\n```"


def _resolve_soul_path() -> Path:
    """Where to read the operator's soul.md from. Honours
    ``CODEX_RC_SOUL_PATH``; otherwise looks at ``./data/config/soul.md``
    then the legacy ``./data/soul.md`` next to the working directory before
    falling back to the bundled example.
    """
    explicit = os.environ.get("CODEX_RC_SOUL_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for user_default in (Path("./data/config/soul.md"), Path("./data/soul.md")):
        if user_default.is_file():
            return user_default
    return _BUNDLED_SOUL_PATH


def _read_optional_markdown(path: Path, *, label: str) -> str | None:
    try:
        body = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("codex_rc: failed to read %s from %s", label, path)
        return None
    return body or None


def _normalize_user_path(raw: str) -> str:
    """Sync helper kept outside async functions to avoid ASYNC240 on
    Path.expanduser()."""
    return str(Path(raw).expanduser())


def _same_path(a: str | Path, b: str | Path) -> bool:
    """Sync helper kept outside async functions to avoid ASYNC240 warnings
    on Path().resolve() calls."""
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except Exception:
        return str(a) == str(b)


def _load_soul_text() -> str | None:
    return _load_soul_text_from(_resolve_soul_path())


def _load_soul_text_from(path: Path) -> str | None:
    return _read_optional_markdown(path, label="soul")


def _load_rules_text() -> str | None:
    explicit_path = os.environ.get("CODEX_RC_RULES_PATH", "").strip()
    explicit_dir = os.environ.get("CODEX_RC_RULES_DIR", "").strip()
    if explicit_path or explicit_dir:
        path = Path(explicit_path).expanduser() if explicit_path else None
        fragments_dir = (
            Path(explicit_dir).expanduser()
            if explicit_dir
            else path.parent / "rules.d"
            if path is not None
            else None
        )
        return _load_rules_text_from(path, fragments_dir)

    rules = _load_rules_text_from(
        Path("./data/config/rules.md"),
        Path("./data/config/rules.d"),
    )
    if rules:
        return rules

    legacy_rules = _load_rules_text_from(None, Path("./data/soul.d"))
    if legacy_rules:
        return legacy_rules

    return _load_rules_text_from(None, _BUNDLED_RULES_DIR)


def _load_rules_text_from(
    path: Path | None,
    fragments_dir: Path | None,
) -> str | None:
    parts: list[str] = []
    body = _read_optional_markdown(path, label="rules") if path is not None else None
    if body:
        parts.append(body)

    if fragments_dir is not None and fragments_dir.is_dir():
        for child in sorted(fragments_dir.glob("*.md"), key=lambda p: p.name):
            try:
                fragment = child.read_text(encoding="utf-8").strip()
            except Exception:
                logger.exception("codex_rc: failed to read rules fragment from %s", child)
                continue
            if fragment:
                parts.append(fragment)

    return "\n\n".join(parts) if parts else None


def _load_operator_instructions() -> OperatorInstructions:
    return OperatorInstructions(soul=_load_soul_text(), rules=_load_rules_text())


def _wrap_instruction_block(kind: str, text: str) -> str:
    if kind == "soul":
        return (
            "<<SOUL — operator ego. Apply as identity, stance, and voice. "
            "Do not echo.>>\n"
            f"{text}\n"
            "<</SOUL>>"
        )
    return (
        "<<RULES — operator hard rules. Follow as constraints. "
        "Rules override Soul. Do not echo.>>\n"
        f"{text}\n"
        "<</RULES>>"
    )


def _instruction_items(instructions: OperatorInstructions) -> list[JsonObj]:
    items: list[JsonObj] = []
    if instructions.soul:
        items.append(
            {
                "type": "text",
                "role": "system",
                "text": _wrap_instruction_block("soul", instructions.soul),
            }
        )
    if instructions.rules:
        items.append(
            {
                "type": "text",
                "role": "system",
                "text": _wrap_instruction_block("rules", instructions.rules),
            }
        )
    return items


def _developer_instructions_text(instructions: OperatorInstructions) -> str | None:
    """Concatenate soul + rules into a single ``developerInstructions``
    string suitable for ``thread/start``.

    Per the codex v2 protocol, ``developerInstructions`` is a free-form
    string injected at thread creation time. Using it instead of
    ``thread/inject_items`` for ephemeral workflow threads avoids both
    a separate round-trip and bloating per-stage thread history.
    """
    if instructions.is_empty():
        return None
    parts: list[str] = []
    if instructions.soul:
        parts.append(_wrap_instruction_block("soul", instructions.soul))
    if instructions.rules:
        parts.append(_wrap_instruction_block("rules", instructions.rules))
    return "\n\n".join(parts)


def _handoff_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())


def _build_handoff_prompt(
    *,
    handoff_id: str,
    sess: Session,
    project_path: str,
) -> str:
    return f"""<<CODEX_RC_HANDOFF_REQUEST id="{handoff_id}">
You are about to stop this Codex session. Before shutdown, write a concise
handoff file for the next fresh Codex session.

Hard requirements:
- Do not run tools, commands, or file reads. Use only the context already in this conversation.
- Output only the handoff markdown. Do not add conversational preface or closing text.
- Prefer the language primarily used by the user/session.
- Be specific about completed work, changed files, decisions, tests/checks, blockers, and next steps.
- If a detail is unknown, write "unknown" instead of inventing it.

Session metadata to include exactly:
- handoff_id: {handoff_id}
- generated_at: {_handoff_timestamp()}
- channel_id: {sess.channel_id}
- project_path: {project_path}
- source_codex_thread_id: {sess.codex_thread_id or "none"}
- sandbox: {sess.sandbox}
- approval: {sess.approval}

Use this markdown shape:
# Codex Handoff
## Session Metadata
## User Goal
## Current State
## Completed Work
## Files And Code Touched
## Commands And Checks
## Decisions And Constraints
## Open Issues Or Blockers
## Next Steps
## Risks
<</CODEX_RC_HANDOFF_REQUEST>>"""


def _wrap_handoff_context(handoff: HandoffRecord, body: str) -> str:
    return (
        "<<HANDOFF_CONTEXT — prior codex_rc session summary. "
        "Use this as the starting context for the fresh session. "
        "Do not echo it unless the user asks.>>\n"
        f"handoff_id: {handoff.handoff_id}\n"
        f"source_codex_thread_id: {handoff.source_codex_thread_id or 'none'}\n"
        f"project_path: {handoff.project_path}\n\n"
        f"{body.strip()}\n"
        "<</HANDOFF_CONTEXT>>"
    )


def _failed_handoff_body(
    *,
    handoff_id: str,
    sess: Session,
    error: str,
) -> str:
    return (
        "# Codex Handoff\n\n"
        "## Session Metadata\n"
        f"- handoff_id: {handoff_id}\n"
        f"- generated_at: {_handoff_timestamp()}\n"
        f"- channel_id: {sess.channel_id}\n"
        f"- project_path: {sess.project_path}\n"
        f"- source_codex_thread_id: {sess.codex_thread_id or 'none'}\n"
        f"- sandbox: {sess.sandbox}\n"
        f"- approval: {sess.approval}\n"
        "- status: failed\n\n"
        "## Error\n"
        f"{error}\n"
    )


def _turn_id_from_turn_result(result: JsonObj | None) -> str | None:
    if not isinstance(result, dict):
        return None
    turn = result.get("turn") or {}
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    turn_id = turn_id or result.get("turnId")
    return str(turn_id) if turn_id else None


def _turn_id_from_notification(params: JsonObj) -> str:
    turn = params.get("turn") or {}
    if isinstance(turn, dict):
        turn_id = turn.get("id") or params.get("turnId")
    else:
        turn_id = params.get("turnId")
    return str(turn_id or "")


def _error_message_from_params(params: JsonObj) -> str:
    err = params.get("error") or {}
    if isinstance(err, dict):
        return str(err.get("message") or err or "handoff turn failed")
    return str(err or "handoff turn failed")


def _codex_error_info_from_params(params: JsonObj) -> str | None:
    """Return the structured `codexErrorInfo` tag from an error notification.

    The protocol exposes this as either a bare string variant (e.g.
    ``"contextWindowExceeded"``) or as an object whose single key names the
    variant. Returns ``None`` when the field is absent or unrecognized.
    """
    err = params.get("error")
    if not isinstance(err, dict):
        return None
    info = err.get("codexErrorInfo")
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return next(iter(info.keys()), None)
    return None


class CodexTurnError(RuntimeError):
    """Codex emitted an ``error`` notification mid-turn.

    Carries the structured ``codexErrorInfo`` tag (when present) so callers can
    branch on the codex error code instead of pattern-matching on the
    human-readable message.
    """

    def __init__(self, message: str, codex_error_info: str | None = None) -> None:
        super().__init__(message)
        self.codex_error_info = codex_error_info


def _is_model_unavailable_error(exc: RpcError) -> bool:
    text = f"{exc.message} {exc.data or ''}".lower()
    if "model" not in text:
        return False
    unavailable_markers = (
        "not found",
        "not available",
        "unavailable",
        "unknown",
        "unsupported",
        "does not exist",
        "invalid model",
    )
    return any(marker in text for marker in unavailable_markers)


def _is_context_window_error(exc: BaseException) -> bool:
    if (
        isinstance(exc, CodexTurnError)
        and exc.codex_error_info == "contextWindowExceeded"
    ):
        return True
    if isinstance(exc, RpcError):
        text = f"{exc.message} {exc.data or ''}".lower()
    else:
        text = str(exc).lower()
    markers = (
        "ran out of room in the model's context window",
        "context window exceeded",
        "context length exceeded",
        "maximum context length",
    )
    return any(marker in text for marker in markers)


def _workflow_fallback_reason(
    exc: BaseException, turn: "WorkflowExecutionTurn"
) -> str | None:
    """Classify ``exc`` for the workflow stage fallback decision.

    Returns the human-readable reason when a fallback is warranted, or
    ``None`` when the error should propagate (no fallback configured, or
    the failure isn't one we know how to recover from by switching models).
    """
    if not turn.fallback_model:
        return None
    if isinstance(exc, RpcError) and _is_model_unavailable_error(exc):
        return "model unavailable"
    if _is_context_window_error(exc):
        return "context window exceeded"
    return None


def _diagnostic_value(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return text[:120]


def _codex_command(proc: object) -> str:
    raw_bin = getattr(proc, "codex_bin", None)
    codex_bin = raw_bin if isinstance(raw_bin, str) and raw_bin else "codex"
    args = getattr(proc, "codex_args", ("app-server",)) or ()
    if not isinstance(args, (list, tuple)):
        args = ("app-server",)
    args_text = " ".join(str(arg) for arg in args)
    return f"{codex_bin} {args_text}".strip()


def _token_usage_breakdown(params: JsonObj) -> JsonObj:
    usage = params.get("tokenUsage") or {}
    if not isinstance(usage, dict):
        return {}
    total = usage.get("total")
    if isinstance(total, dict):
        return total
    return usage


def _format_token_usage(params: JsonObj) -> str | None:
    usage = _token_usage_breakdown(params)
    labels = (
        ("totalTokens", "total"),
        ("inputTokens", "in"),
        ("outputTokens", "out"),
        ("reasoningOutputTokens", "reasoning"),
        ("cachedInputTokens", "cached"),
    )
    parts = [
        f"{label} `{value:,}`"
        for key, label in labels
        if isinstance((value := usage.get(key)), int)
    ]
    return " · ".join(parts) if parts else None


def _rate_limit_snapshot(params: JsonObj) -> JsonObj:
    nested = params.get("rateLimits")
    if isinstance(nested, dict):
        return nested
    return params


def _format_rate_limit(params: JsonObj) -> str | None:
    snapshot = _rate_limit_snapshot(params)
    if not snapshot:
        return None
    parts: list[str] = []
    limit = _diagnostic_value(snapshot.get("limitName") or snapshot.get("limitId"))
    if limit:
        parts.append(f"limit `{limit}`")
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if isinstance(used, (int, float)):
            parts.append(f"{key} `{int(used)}% used`")
        resets = window.get("resetsAt")
        if isinstance(resets, int):
            parts.append(f"{key} resets `{resets}`")
            break
    by_limit = params.get("rateLimitsByLimitId")
    if isinstance(by_limit, dict) and by_limit:
        keys = ", ".join(sorted(str(key) for key in by_limit.keys())[:6])
        parts.append(f"buckets `{keys}`")
    return " · ".join(parts) if parts else None


def _capability_line(status: str, label: str, detail: str) -> str:
    return f"- {status} **{label}** — {detail}"


def _load_cats(path: Path | None) -> list[dict[str, Any]]:
    """Backwards-compatible loader returning the ``cats`` list only."""
    cats = _load_cats_full(path).get("cats", [])
    if not isinstance(cats, list):
        return []
    return [cat for cat in cats if isinstance(cat, dict)]


def _load_cats_full(path: Path | None) -> dict[str, Any]:
    """Load the full cats config (cats + frame_interval_s + others)."""
    path = path or _DEFAULT_CATS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("codex_rc: could not load cats from %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return dict(data)
"""Signature: ``(channel_id, discord_payload_dict) -> awaitable``."""


class ChannelService:
    """Owns one CodexServerProcess + its routing for a single channel."""

    def __init__(
        self,
        *,
        channel_id: str,
        store: SessionStore,
        proc: CodexServerProcess,
        post: PostToGateway,
        post_update: PostEditable | None = None,
        project_slug: str,
        verbose_notifs: bool = False,
        cats_path: Path | None = None,
        post_animated: PostAnimated | None = None,
        on_turn_stage: TurnStageCallback | None = None,
        owner_mention_id: str | None = None,
        auto_approve: bool = False,
        auto_compact_threshold_tokens: int = 0,
        workflow_enabled: bool = False,
    ) -> None:
        self.channel_id = channel_id
        self.store = store
        self.proc = proc
        self.post = post
        self.post_update = post_update
        self.post_animated = post_animated
        self.project_slug = project_slug
        self.notif = NotifRouter(verbose=verbose_notifs)
        self.approvals = ApprovalRouter()
        self.on_turn_stage = on_turn_stage
        self.owner_mention_id = owner_mention_id
        self.auto_approve = auto_approve
        self.auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self.workflow = DevelopmentWorkflowRouter(enabled=workflow_enabled)
        self._pump_task: asyncio.Task[None] | None = None
        self._post_lock = asyncio.Lock()
        # Recovery state flags so the unified crash path fires exactly once
        # per disconnect, whether triggered by a child-exit callback or by
        # the pump naturally finishing on transport EOF.
        self._intentional_stop: bool = False
        self._recovery_in_progress: bool = False
        # Turn lifecycle tracking — drives in-flight steer routing and
        # turn-stage emoji reactions.
        self._active_turn_id: str | None = None
        self._file_change_emitted_for_turn: str | None = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._handoff_capture: _HandoffCapture | None = None
        self._workflow_turn_wait: _TurnCompletionWait | None = None
        self._workflow_execution_task: asyncio.Task[None] | None = None
        # Filesystem store for per-stage handoff markdown files. Lives at
        # ``<project_root>/data/workflow/`` so codex's workspace-write sandbox
        # can write into it without extra permission grants.
        self._workflow_store = WorkflowFileStore(
            root=Path(self.proc.project_path) / "data" / "workflow"
        )
        # Operator instruction prefix queued for the first turn after thread start if the
        # inject_items RPC isn't accepted by this codex build.
        self._pending_instruction_prefix: str | None = None
        # Usage / billing snapshots (footer + auto-compact).
        self._codex_init: JsonObj | None = None
        self._last_thread_metadata: JsonObj | None = None
        self._last_token_usage: JsonObj | None = None
        self._last_rate_limit: JsonObj | None = None
        self._compacting: bool = False
        # Cat-of-patience: one animated message per turn (edit-cycled emoji).
        self._cat_task: asyncio.Task[None] | None = None
        self._cat_last_turn_id: str | None = None
        self._plan_snapshot: _PlanProgressSnapshot | None = None
        self._plan_task: asyncio.Task[None] | None = None
        self._plan_tick: int = 0
        cats_data = _load_cats_full(cats_path)
        cats = cats_data.get("cats", [])
        self._cats: list[dict[str, Any]] = (
            [cat for cat in cats if isinstance(cat, dict)]
            if isinstance(cats, list)
            else []
        )
        self._cat_interval_s: float = float(
            cats_data.get("frame_interval_s", 1.0) or 1.0
        )

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        init = await self.proc.start()
        self._codex_init = init
        logger.info(
            "codex_rc: codex initialized channel=%s command=%s transport=%s user_agent=%s",
            self.channel_id,
            _codex_command(self.proc),
            getattr(self.proc, "transport_mode", "?"),
            init.get("userAgent"),
        )
        self.proc.set_server_request_handler(
            self.approvals.as_server_request_handler(self._on_approval_prompt)
        )
        self.proc.set_on_child_exit(self._on_codex_crash)
        self._pump_task = asyncio.create_task(
            self._pump(), name=f"codex-rc-pump-{self.channel_id}"
        )
        # tmux pane: left = real codex TUI joining the same backend
        # (when ws mode is active), right = debug trace or error log when enabled.
        tui_cmd: str | None = None
        ws_url = getattr(self.proc, "ws_url", None)
        if ws_url:
            tui_cmd = f"codex --remote {ws_url}"
        tail_path = getattr(self.proc, "tail_path", self.proc.pretty_path)
        # Kill any leftover session from a previous run so the dual-pane
        # split actually happens (create_tail_session is otherwise
        # idempotent and would reuse the stale single-pane session). In modes
        # without a tmux surface, this also clears old debug-tail panes.
        kill_session(self.project_slug)
        if tui_cmd or tail_path:
            create_tail_session(
                self.project_slug,
                self.proc.project_path,
                tail_path,
                tui_cmd=tui_cmd,
            )
        self.store.update_state(self.channel_id, "running")

        # Render a short typing-style hello animation before the final state
        # message. Discord edit calls are cheap but still asynchronous, so
        # this is intentionally short and non-blocking.
        if self.post_animated is not None:
            try:
                await self.post_animated(
                    self.channel_id,
                    list(_START_HELLO_FRAMES),
                    interval_s=1.0,
                    loop=False,
                )
            except Exception:
                logger.debug("codex_rc: hello animation failed on start", exc_info=True)

        description = f"**Project**: `{self.proc.project_path}`"
        await self._post_system(
            embed=DiscordEmbed(
                title="🟢 Codex session started",
                description=description,
                color=0x57F287,
            )
        )

    async def stop(self) -> None:
        # Signal first so the pump's finally treats EOF as a planned shutdown.
        self._intentional_stop = True
        # Drain pending approvals first so the server doesn't hang.
        for resolution in self.approvals.cancel_all(reason="channel stop"):
            try:
                await apply_resolution(self.proc.client, resolution)
            except Exception:
                logger.exception("codex_rc: error applying drain resolution")
        await self._cancel_workflow_execution()
        self._cancel_cat()
        self._cancel_plan_progress()
        await self._create_handoff_on_stop()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
        self._pump_task = None
        await self.proc.stop()
        kill_session(self.project_slug)
        self.store.update_state(self.channel_id, "stopped")

    async def _create_handoff_on_stop(self) -> None:
        sess = self.store.get(self.channel_id)
        if sess is None or not sess.codex_thread_id:
            return
        handoff_id = self.store.new_handoff_id(self.channel_id)
        await self._post_system(
            embed=DiscordEmbed(
                title="🧾 Writing handoff",
                description="Asking Codex to summarize this session before shutdown.",
                color=0x3498DB,
            )
        )
        try:
            await self._wait_until_idle(timeout_s=HANDOFF_IDLE_WAIT_SECONDS)
            if self._active_turn_id:
                raise TimeoutError(
                    "timed out waiting for the active turn before handoff"
                )
            body = await self._run_handoff_turn(sess, handoff_id)
            if not body.strip():
                raise RuntimeError("handoff turn completed without text")
            rec = self.store.record_handoff(
                handoff_id=handoff_id,
                channel_id=self.channel_id,
                project_path=sess.project_path,
                sandbox=sess.sandbox,
                approval=sess.approval,
                source_codex_thread_id=sess.codex_thread_id,
                body=body,
            )
            await self._post_system(
                embed=DiscordEmbed(
                    title="✅ Handoff saved",
                    description=(
                        f"`{rec.handoff_id[:18]}`\n"
                        f"`{rec.file_path}`"
                    ),
                    color=0x57F287,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("codex_rc: handoff generation failed")
            failure_body = _failed_handoff_body(
                handoff_id=handoff_id,
                sess=sess,
                error=str(exc),
            )
            with contextlib.suppress(Exception):
                self.store.record_handoff(
                    handoff_id=handoff_id,
                    channel_id=self.channel_id,
                    project_path=sess.project_path,
                    sandbox=sess.sandbox,
                    approval=sess.approval,
                    source_codex_thread_id=sess.codex_thread_id,
                    body=failure_body,
                    status="failed",
                    error=str(exc),
                )
            await self._post_system(
                embed=DiscordEmbed(
                    title="⚠️ Handoff failed",
                    description=f"Session will stop anyway.\n`{exc}`",
                    color=0xFEE75C,
                )
            )

    async def _wait_until_idle(self, *, timeout_s: float) -> None:
        if not self._active_turn_id:
            return
        await asyncio.wait_for(self._idle_event.wait(), timeout=timeout_s)

    async def _run_handoff_turn(self, sess: Session, handoff_id: str) -> str:
        thread_id = sess.codex_thread_id
        if not thread_id:
            raise RuntimeError("no source thread to hand off")
        loop = asyncio.get_running_loop()
        capture = _HandoffCapture(
            handoff_id=handoff_id,
            thread_id=thread_id,
            future=loop.create_future(),
        )
        prompt = _build_handoff_prompt(
            handoff_id=handoff_id,
            sess=sess,
            project_path=str(self.proc.project_path),
        )
        self._handoff_capture = capture
        try:
            result = await self.proc.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
                timeout=30.0,
            )
            turn_id = _turn_id_from_turn_result(result)
            if turn_id:
                capture.turn_id = turn_id
            elif capture.turn_id is None:
                raise RuntimeError(f"turn/start returned no handoff turn id: {result}")
            return await asyncio.wait_for(
                capture.future,
                timeout=HANDOFF_COMPLETION_TIMEOUT_SECONDS,
            )
        finally:
            if self._handoff_capture is capture:
                self._handoff_capture = None

    # ----------------------------------------------------------- crash recovery

    async def _on_codex_crash(self, returncode: int | None) -> None:
        """Triggered when the codex app-server is unreachable.

        Two paths converge here:

        1. The codex child process itself exited (server_process watchdog
           fires this callback with the returncode).
        2. The transport closed with the child still alive — observable in
           :meth:`_pump`'s finally block when the notifications iterator
           completes without a planned stop.

        Idempotent: only the first arrival drives recovery; concurrent
        invocations from both paths early-return.
        """
        if self._recovery_in_progress or self._intentional_stop:
            return
        self._recovery_in_progress = True
        if returncode is None:
            crash_desc = (
                "codex transport dropped while the child appeared to "
                "still be alive. Attempting auto-recovery..."
            )
        else:
            crash_desc = (
                f"codex app-server exited (rc={returncode}).\n"
                "Attempting auto-recovery..."
            )
        self.store.update_state(self.channel_id, "crashed")
        sess = self.store.get(self.channel_id)
        last_thread = sess.codex_thread_id if sess else None
        await self._post_system(
            embed=DiscordEmbed(
                title="🔴 Codex session crashed",
                description=crash_desc,
                color=0xED4245,
            )
        )
        try:
            await self._restart_after_crash(last_thread)
        except Exception:
            logger.exception(
                "codex_rc: auto-restart failed channel=%s", self.channel_id
            )
            self.store.update_state(self.channel_id, "error")
            await self._post_system(
                embed=DiscordEmbed(
                    title="❌ Auto-recovery failed",
                    description=(
                        "Run `/codex stop` then `/codex start <path>` to "
                        "restart manually."
                    ),
                    color=0xED4245,
                )
            )
        finally:
            self._recovery_in_progress = False

    async def _restart_after_crash(self, last_thread: str | None) -> None:
        # Cancel and drain the now-doomed pump task. It will already be
        # winding down because the client's read_loop saw EOF.
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
        self._pump_task = None
        # Cancel any in-flight cat animation.
        self._cancel_cat()
        self._cancel_plan_progress()
        # Tear the old proc down (idempotent — child is already dead).
        with contextlib.suppress(Exception):
            await self.proc.stop()
        # Spin up a fresh proc with the same parameters. ws port will be
        # reallocated; this is intentional so we don't race the dead one.
        new_proc = CodexServerProcess(
            project_path=self.proc.project_path,
            log_dir=self.proc.log_dir,
            codex_bin=self.proc.codex_bin,
            codex_args=self.proc.codex_args,
            env=self.proc.env,
            transport_mode=self.proc.transport_mode,
            event_log_mode=getattr(self.proc, "event_log_mode", "errors"),
            error_log_path=getattr(self.proc, "error_log_path", None),
            error_log_retention_days=getattr(
                self.proc, "error_log_retention_days", 30
            ),
        )
        self.proc = new_proc
        await self.proc.start()
        self.proc.set_server_request_handler(
            self.approvals.as_server_request_handler(self._on_approval_prompt)
        )
        self.proc.set_on_child_exit(self._on_codex_crash)
        self._pump_task = asyncio.create_task(
            self._pump(), name=f"codex-rc-pump-{self.channel_id}"
        )
        resumed = False
        if last_thread:
            try:
                await self.proc.request(
                    "thread/resume",
                    {"threadId": last_thread},
                    timeout=15.0,
                )
                self.store.touch_thread(last_thread, turn_increment=0)
                resumed = True
            except Exception:
                logger.exception(
                    "codex_rc: failed to resume thread %s after crash",
                    last_thread,
                )
        self.store.update_state(self.channel_id, "running")
        description = (
            f"resumed thread `{last_thread[:8]}` after crash"
            if resumed and last_thread
            else "started fresh; previous thread did not resume cleanly"
            if last_thread
            else "started fresh"
        )
        new_ws_url = getattr(self.proc, "ws_url", None)
        if new_ws_url:
            description += (
                f"\n\nReattach the codex TUI with: `codex --remote {new_ws_url}`"
            )
        await self._post_system(
            embed=DiscordEmbed(
                title="🟢 Auto-recovered",
                description=description,
                color=0x57F287,
            )
        )

    # -------------------------------------------------------------- public

    async def send_text(
        self,
        text: str,
        *,
        image_urls: list[str] | None = None,
    ) -> None:
        """Send a user-text turn. Creates the Codex thread lazily on first call.

        ``image_urls`` (e.g. Discord attachment CDN URLs) are appended as
        ``ImageUserInput`` items so Codex's vision model can read them
        alongside the text prompt. Empty / None → text-only.

        If a turn is already running (``_active_turn_id`` is set from a prior
        ``turn/started`` notification), the input is routed to ``turn/steer``
        instead of ``turn/start`` — appending to the in-flight turn rather
        than queueing a new one.
        """
        await self._send_text(text, image_urls=image_urls, workflow_wrap=True)

    async def _send_text(
        self,
        text: str,
        *,
        image_urls: list[str] | None = None,
        workflow_wrap: bool = False,
        preview_text: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> JsonObj:
        sess = self.store.get(self.channel_id)
        if sess is None:
            raise RuntimeError(f"no session for channel {self.channel_id}")
        thread_id = sess.codex_thread_id
        if not thread_id:
            thread_id = await self._start_codex_thread(sess)
        active_turn = self._active_turn_id
        workflow_analysis_turn = False
        if workflow_wrap and not active_turn:
            text = self.workflow.begin(
                text,
                has_images=bool(image_urls),
            )
            workflow_analysis_turn = self.workflow.is_awaiting_recommendation()
            if workflow_analysis_turn:
                model = model or WORKFLOW_ANALYSIS_MODEL
                effort = effort or WORKFLOW_ANALYSIS_EFFORT
        input_items: list[JsonObj] = []
        # Fallback path for codex builds without thread/inject_items: prefix
        # operator instructions on the first turn so Soul and Rules still land.
        if self._pending_instruction_prefix and not self._active_turn_id:
            input_items.append(
                {"type": "text", "text": self._pending_instruction_prefix + "\n\n"}
            )
            self._pending_instruction_prefix = None
        if text:
            input_items.append({"type": "text", "text": text})
        for url in image_urls or []:
            if url:
                input_items.append({"type": "image", "url": url})
        if not input_items:
            raise ValueError("send_text requires text or image_urls")
        if active_turn:
            if model or effort:
                raise RuntimeError("cannot override model or effort while steering")
            method = "turn/steer"
            # Codex v2 schema: turn/steer requires `expectedTurnId`
            # (precondition — request fails if the active turn drifted).
            params: JsonObj = {
                "threadId": thread_id,
                "expectedTurnId": active_turn,
                "input": input_items,
            }
        else:
            method = "turn/start"
            params = {"threadId": thread_id, "input": input_items}
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
        result = await self.proc.request(method, params, timeout=None)
        # Track in history so /codex history / resume can surface this thread.
        # Steer counts as part of the same turn — don't double-increment.
        preview_source = preview_text if preview_text is not None else text
        preview = (preview_source or ("(image)" if image_urls else "")).replace("\n", " ")
        self.store.touch_thread(
            thread_id,
            preview_addition=preview[:140],
            turn_increment=0 if active_turn else 1,
        )
        return result if isinstance(result, dict) else {}

    async def resume_thread(self, codex_thread_id: str) -> None:
        """Switch the current channel session to an existing Codex thread.

        Calls ``thread/resume`` so the codex app-server reloads it from
        disk; updates the channel's stored thread id so subsequent
        ``send_text`` calls land in this thread.
        """
        await self.proc.request(
            "thread/resume",
            {"threadId": codex_thread_id},
            timeout=15.0,
        )
        self.store.update_thread(self.channel_id, codex_thread_id)
        self.store.touch_thread(codex_thread_id, turn_increment=0)

    async def new_thread(self) -> str:
        """Force a fresh Codex thread on the next send_text. Returns the
        newly-allocated thread id."""
        sess = self.store.get(self.channel_id)
        if sess is None:
            raise RuntimeError(f"no session for channel {self.channel_id}")
        # Drop the cached id, then create a fresh thread eagerly so the
        # response includes the id for the user.
        self.store.update_thread(self.channel_id, "")  # blank = re-start
        sess = self.store.get(self.channel_id)
        assert sess is not None
        return await self._start_codex_thread(sess)

    async def start_handoff_thread(self, handoff: HandoffRecord) -> str:
        """Start a fresh thread and seed it with a saved handoff context."""
        self.store.update_thread(self.channel_id, "")
        sess = self.store.get(self.channel_id)
        if sess is None:
            raise RuntimeError(f"no session for channel {self.channel_id}")
        thread_id = await self._start_codex_thread(sess)
        await self._inject_handoff_context(thread_id, handoff)
        self.store.mark_handoff_target(
            handoff.handoff_id,
            target_codex_thread_id=thread_id,
        )
        return thread_id

    async def handle_button(self, custom_id: str) -> bool:
        """Resolve a button click. Returns True on success, False if unknown."""
        if parse_workflow_custom_id(custom_id) is not None:
            decision = self.workflow.resolve(custom_id)
            if decision is None:
                return False
            if decision.execution_turns and decision.seed_handoff:
                self._start_workflow_execution(decision)
            return True
        if parse_custom_id(custom_id) is None:
            return False
        resolution = self.approvals.resolve(custom_id)
        if resolution is None:
            return False
        await apply_resolution(self.proc.client, resolution)
        return True

    def _start_workflow_execution(self, decision: WorkflowDecision) -> None:
        """Provision the workflow directory and schedule the execution task.

        The workflow directory + ``stage_1_handoff.md`` are created
        *synchronously* before the background task is scheduled so that
        if scheduling races with a Discord interaction (e.g. cancel
        button), the meta.json already records what happened.
        """
        if self._workflow_execution_task and not self._workflow_execution_task.done():
            raise RuntimeError("a workflow execution is already running")
        if decision.seed_handoff is None:
            raise RuntimeError("workflow decision missing seed_handoff")
        sess = self.store.get(self.channel_id)
        main_thread_id = sess.codex_thread_id if sess else None
        if not main_thread_id:
            raise RuntimeError(
                "cannot start workflow without a main codex thread id"
            )
        self._workflow_store.create(
            decision.workflow_id,
            channel_id=self.channel_id,
            main_thread_id=main_thread_id,
        )
        self._seed_stage_1_handoff(decision.seed_handoff)
        self._workflow_execution_task = asyncio.create_task(
            self._run_workflow_execution(decision),
            name=f"codex-rc-workflow-{self.channel_id}-{decision.workflow_id}",
        )

    def _seed_stage_1_handoff(self, seed: WorkflowSeedHandoff) -> None:
        """Write the synthetic stage-1 handoff the design stage reads first.

        Stage 1 is a user-facing analysis turn; it doesn't produce a
        structured file. We synthesize a minimal :class:`Stage1To2Handoff`
        here from the parsed plan title + original task. The design
        stage gets enough to anchor its work (task + chosen plan), and
        the file's presence guarantees stage 2 won't fail its read step.
        """
        handoff = Stage1To2Handoff(
            workflow_id=seed.workflow_id,
            original_task=seed.original_task,
            selected_plan=seed.selected_plan,
            plan_title=seed.plan_title,
            plan_summary=(
                "Synthesized from the approval-time plan title. "
                "Stage 2 should re-derive any additional detail from "
                "the original task."
            ),
            selection_rationale="User-approved via Discord button.",
            trade_offs=(),
            hints=(),
            output_budget_tokens=WORKFLOW_STAGE_1_TO_2_BUDGET_TOKENS,
        )
        self._workflow_store.write_handoff(
            seed.workflow_id,
            stage=1,
            content=handoff.to_markdown(),
        )

    async def _run_workflow_execution(self, decision: WorkflowDecision) -> None:
        outcome = "completed"
        try:
            for turn in decision.execution_turns:
                await self._wait_until_idle(
                    timeout_s=WORKFLOW_STAGE_COMPLETION_TIMEOUT_SECONDS
                )
                await self._announce_stage_start(turn)
                self._workflow_store.update_meta(
                    decision.workflow_id, current_stage=turn.stage
                )
                await self._run_workflow_stage_turn(turn)
                # Validate the handoff the agent wrote. Parse failure
                # triggers one retry on the same ephemeral-thread cycle;
                # a second failure aborts the workflow.
                await self._validate_stage_handoff(turn)
            await self._finalize_workflow(decision)
            self.workflow.complete_execution()
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            outcome = f"failed:stage{self._current_workflow_stage(decision)}"
            cleared = self.workflow.cancel()
            logger.exception(
                "codex_rc: workflow execution failed channel=%s workflow=%s",
                self.channel_id,
                cleared or decision.workflow_id,
            )
            with contextlib.suppress(Exception):
                await self._post_system(
                    embed=DiscordEmbed(
                        title="⚠️ Workflow execution failed",
                        description=f"`{exc}`",
                        color=COLOR_WARN,
                    )
                )
        finally:
            with contextlib.suppress(Exception):
                self._workflow_store.update_meta(
                    decision.workflow_id, last_error=None
                )
                self._workflow_store.archive(decision.workflow_id, outcome=outcome)
            if self._workflow_execution_task is asyncio.current_task():
                self._workflow_execution_task = None

    def _current_workflow_stage(self, decision: WorkflowDecision) -> int:
        meta = self._workflow_store.read_meta(decision.workflow_id)
        return meta.current_stage if meta is not None else 0

    async def _announce_stage_start(self, turn: WorkflowExecutionTurn) -> None:
        with contextlib.suppress(Exception):
            await self._post_system(
                embed=DiscordEmbed(
                    title=f"🔧 Stage {turn.stage}/4 — {turn.title}",
                    description=f"Running on `{turn.model}`.",
                    color=0x202123,
                )
            )

    async def _run_workflow_stage_turn(self, turn: WorkflowExecutionTurn) -> None:
        thread_id = await self._create_ephemeral_thread(
            workflow_id=turn.workflow_id, stage=turn.stage
        )
        try:
            await self._start_and_wait_workflow_turn(
                turn, model=turn.model, thread_id=thread_id
            )
            return
        except (RpcError, CodexTurnError) as exc:
            reason = _workflow_fallback_reason(exc, turn)
            if reason is None:
                raise
            logger.info(
                "codex_rc: workflow model fallback channel=%s stage=%s model=%s "
                "fallback=%s reason=%s",
                self.channel_id,
                turn.stage,
                turn.model,
                turn.fallback_model,
                reason,
            )
            with contextlib.suppress(Exception):
                await self._post_system(
                    embed=DiscordEmbed(
                        title="⚠️ Workflow model fallback",
                        description=(
                            f"Stage {turn.stage} `{turn.title}` failed on "
                            f"`{turn.model}` ({reason}). Retrying with "
                            f"`{turn.fallback_model}` on a fresh thread."
                        ),
                        color=COLOR_WARN,
                    )
                )
        # Fall back: fresh ephemeral thread + fallback model. The original
        # thread is left for codex to GC — ephemeral threads aren't
        # persisted, so there's nothing to clean up explicitly.
        fallback_thread = await self._create_ephemeral_thread(
            workflow_id=turn.workflow_id, stage=turn.stage
        )
        await self._start_and_wait_workflow_turn(
            turn, model=turn.fallback_model, thread_id=fallback_thread
        )

    async def _create_ephemeral_thread(
        self, *, workflow_id: str, stage: int
    ) -> str:
        """Open a fresh ephemeral codex thread for one workflow stage.

        Soul + rules ride along on ``developerInstructions`` so the
        ephemeral thread inherits operator persona / project rules
        without burning a separate ``thread/inject_items`` round-trip
        and without leaking them into thread history that compounds
        toward the next stage.
        """
        sess = self.store.get(self.channel_id)
        if sess is None:
            raise RuntimeError(f"no session for channel {self.channel_id}")
        instructions = _load_operator_instructions()
        params: JsonObj = {
            "cwd": str(self.proc.project_path),
            "sandbox": sess.sandbox,
            "approvalPolicy": sess.approval,
            "ephemeral": True,
        }
        dev_instructions = _developer_instructions_text(instructions)
        if dev_instructions:
            params["developerInstructions"] = dev_instructions
        result = await self.proc.request("thread/start", params, timeout=15.0)
        thread_raw = result.get("thread") or {}
        thread = thread_raw if isinstance(thread_raw, dict) else {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError(
                f"ephemeral thread/start returned no id: {result}"
            )
        # Track the ephemeral id on the workflow meta so crash recovery
        # can identify orphaned threads.
        meta = self._workflow_store.read_meta(workflow_id)
        if meta is not None:
            self._workflow_store.update_meta(
                workflow_id,
                ephemeral_thread_ids=meta.ephemeral_thread_ids + (thread_id,),
            )
        logger.info(
            "codex_rc: workflow ephemeral thread started workflow=%s stage=%s "
            "thread=%s",
            workflow_id,
            stage,
            thread_id,
        )
        return thread_id

    async def _start_and_wait_workflow_turn(
        self,
        turn: WorkflowExecutionTurn,
        *,
        model: str | None,
        thread_id: str,
    ) -> None:
        if self._workflow_turn_wait is not None:
            raise RuntimeError("another workflow stage is already waiting")

        loop = asyncio.get_running_loop()
        wait = _TurnCompletionWait(
            thread_id=thread_id,
            future=loop.create_future(),
        )
        self._workflow_turn_wait = wait
        try:
            params: JsonObj = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": turn.prompt}],
            }
            if model:
                params["model"] = model
            if turn.effort:
                params["effort"] = turn.effort
            result = await self.proc.request("turn/start", params, timeout=None)
            turn_id = _turn_id_from_turn_result(
                result if isinstance(result, dict) else None
            )
            if turn_id:
                wait.turn_id = turn_id
            await asyncio.wait_for(
                wait.future,
                timeout=WORKFLOW_STAGE_COMPLETION_TIMEOUT_SECONDS,
            )
        finally:
            if self._workflow_turn_wait is wait:
                self._workflow_turn_wait = None

    async def _validate_stage_handoff(self, turn: WorkflowExecutionTurn) -> None:
        """Read and parse the handoff the agent should have just written.

        Raises if the file is missing or doesn't conform to the per-stage
        schema. The outer ``_run_workflow_execution`` catches and posts a
        clear error embed; a future iteration may add a one-shot
        same-stage retry that injects the parse error into a follow-up
        prompt.
        """
        content = self._workflow_store.read_handoff(
            turn.workflow_id, stage=turn.stage
        )
        if content is None:
            raise RuntimeError(
                f"stage {turn.stage} ({turn.title}) finished without writing "
                f"{turn.next_handoff_relpath}"
            )
        try:
            if turn.stage == 2:
                parse_stage_2_to_3(content)
            elif turn.stage == 3:
                parse_stage_3_to_4(content)
            elif turn.stage == 4:
                parse_final_summary(content)
        except HandoffParseError as exc:
            raise RuntimeError(
                f"stage {turn.stage} ({turn.title}) handoff failed schema "
                f"validation: {exc}"
            ) from exc

    async def _finalize_workflow(self, decision: WorkflowDecision) -> None:
        """Post the stage-4 summary to Discord and inject it into the
        user's main codex thread so the next user request stays on a
        coherent conversational footing."""
        content = self._workflow_store.read_handoff(
            decision.workflow_id, stage=4
        )
        if content is None:
            return
        try:
            summary: FinalSummary | None = parse_final_summary(content)
        except HandoffParseError:
            summary = None
        with contextlib.suppress(Exception):
            await self._post_system(
                embed=DiscordEmbed(
                    title="✅ Workflow complete",
                    description=(
                        summary.what_was_done if summary is not None else content
                    )[:4000],
                    color=0x57F287,
                )
            )
        # Push the same summary onto the user's main thread as a
        # system-tagged item so codex sees the workflow outcome on the
        # next user turn.
        sess = self.store.get(self.channel_id)
        main_thread_id = sess.codex_thread_id if sess else None
        if not main_thread_id:
            return
        injection_text = (
            f"<<CODEX_RC_WORKFLOW_RESULT id=\"{decision.workflow_id}\">\n"
            f"{content}\n"
            "<</CODEX_RC_WORKFLOW_RESULT>>"
        )
        with contextlib.suppress(Exception):
            await self.proc.request(
                "thread/inject_items",
                {
                    "threadId": main_thread_id,
                    "items": [
                        {"type": "text", "role": "system", "text": injection_text}
                    ],
                },
                timeout=15.0,
            )

    def _observe_workflow_turn_completion(self, notif: JsonObj) -> None:
        wait = self._workflow_turn_wait
        if wait is None or wait.future.done():
            return
        method = str(notif.get("method") or "")
        params = notif.get("params") or {}
        thread_id = str(params.get("threadId") or "")
        if thread_id and thread_id != wait.thread_id:
            return
        turn_id = _turn_id_from_notification(params)
        if wait.turn_id is None:
            if method == "turn/started" and turn_id:
                wait.turn_id = turn_id
                return
            if method not in {"turn/completed", "error"}:
                return
            if turn_id:
                wait.turn_id = turn_id
        if wait.turn_id and turn_id and turn_id != wait.turn_id:
            return
        if method == "turn/completed":
            wait.future.set_result(None)
        elif method == "error":
            wait.future.set_exception(
                CodexTurnError(
                    _error_message_from_params(params),
                    _codex_error_info_from_params(params),
                )
            )

    async def _cancel_workflow_execution(self) -> None:
        task = self._workflow_execution_task
        self.workflow.cancel()
        if self._workflow_turn_wait is not None and not self._workflow_turn_wait.future.done():
            self._workflow_turn_wait.future.cancel()
        self._workflow_turn_wait = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workflow_execution_task = None

    async def cancel_active_turn(self) -> bool:
        """Send ``turn/interrupt`` for the current in-flight turn.

        Returns ``True`` if an interrupt was dispatched, ``False`` if there
        was no active turn. Used by the ✋ reaction + /codex cancel paths.
        """
        active = self._active_turn_id
        if not active:
            return False
        sess = self.store.get(self.channel_id)
        thread_id = sess.codex_thread_id if sess else None
        if not thread_id:
            return False
        try:
            await self.proc.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": active},
                timeout=10.0,
            )
            return True
        except Exception:
            logger.exception("codex_rc: turn/interrupt failed channel=%s", self.channel_id)
            return False

    # ----------------------------------------------------- Codex slash passthroughs
    #
    # These wrap Codex's interactive `/model`, `/permissions`, `/review`,
    # `/fork`, `/goal` slash commands as JSON-RPC calls so Discord users get
    # the same experience as the TUI. The RPC parameter shapes follow the
    # Codex app-server docs (https://developers.openai.com/codex/app-server).

    async def set_model(self, name: str | None) -> JsonObj:
        """Without ``name`` → return ``model/list`` result for caller to render.
        With ``name`` → ``config/value/write`` updates the active model."""
        if name is None:
            return await self.proc.request("model/list", {}, timeout=15.0)
        await self.proc.request(
            "config/value/write",
            {"key": "model", "value": name},
            timeout=15.0,
        )
        return {"applied": name}

    async def set_permissions(self, preset: str) -> dict[str, str]:
        """Apply a named approval/sandbox preset. Mirrors the four presets
        the Codex TUI's ``/permissions`` picker exposes."""
        config = permission_preset_config(preset)
        for key, value in config.items():
            await self.proc.request(
                "config/value/write",
                {"key": key, "value": value},
                timeout=10.0,
            )
        return dict(config)

    async def review(self, target: str | None = None) -> None:
        """Kick off Codex's reviewer for the active thread. ``target`` is a
        free-form string (base branch name, commit hash, or custom
        instruction); when absent the reviewer picks its default scope."""
        thread_id = self._current_thread_id()
        if not thread_id:
            raise RuntimeError("no thread to review")
        params: JsonObj = {"threadId": thread_id}
        if target:
            params["target"] = target
        await self.proc.request("review/start", params, timeout=30.0)

    async def fork(self) -> str:
        """Branch the active thread. Returns the new thread id and swaps
        the channel session to point at it so subsequent send_text lands
        on the branch."""
        thread_id = self._current_thread_id()
        if not thread_id:
            raise RuntimeError("no thread to fork")
        result = await self.proc.request(
            "thread/fork",
            {"threadId": thread_id},
            timeout=15.0,
        )
        new_thread = result.get("thread") or {}
        new_id = new_thread.get("id")
        if not isinstance(new_id, str) or not new_id:
            raise RuntimeError(f"thread/fork returned no id: {result}")
        self.store.update_thread(self.channel_id, new_id)
        self.store.record_thread(
            codex_thread_id=new_id,
            channel_id=self.channel_id,
            project_path=str(self.proc.project_path),
        )
        return new_id

    async def set_goal(self, text: str) -> str:
        """Set or clear the experimental thread goal. Empty / whitespace
        text clears the goal; otherwise it is set verbatim."""
        thread_id = self._current_thread_id()
        if not thread_id:
            raise RuntimeError("no thread for goal")
        body = text.strip()
        if not body:
            await self.proc.request(
                "thread/goal/clear",
                {"threadId": thread_id},
                timeout=10.0,
            )
            return ""
        await self.proc.request(
            "thread/goal/set",
            {"threadId": thread_id, "goal": body},
            timeout=10.0,
        )
        return body

    async def compact(self) -> None:
        """Explicit ``/compact`` — operator-initiated context compaction.
        Distinct from :meth:`_maybe_auto_compact`, which fires on a token
        threshold. Idempotent against the auto path via the same flag."""
        thread_id = self._current_thread_id()
        if not thread_id:
            raise RuntimeError("no thread to compact")
        if self._compacting:
            return
        self._compacting = True
        try:
            await self.proc.request(
                "thread/compact/start",
                {"threadId": thread_id},
                timeout=120.0,
            )
        finally:
            self._compacting = False

    async def rollback(self, count: int = 1) -> int:
        """Drop the last ``count`` turns from the thread's memory.

        Returns the count actually requested (we trust the server's count;
        we don't reconcile against history here). Raises on RPC error.
        """
        if count < 1:
            raise ValueError("rollback count must be >= 1")
        sess = self.store.get(self.channel_id)
        thread_id = sess.codex_thread_id if sess else None
        if not thread_id:
            raise RuntimeError("no thread to rollback")
        await self.proc.request(
            "thread/rollback",
            # Codex v2 schema: required key is `numTurns`, not `count`.
            {"threadId": thread_id, "numTurns": count},
            timeout=15.0,
        )
        return count

    async def sweep_approvals(self) -> int:
        """Periodically drop expired prompts (auto-deny). Returns count."""
        expired = self.approvals.sweep()
        for r in expired:
            try:
                await apply_resolution(self.proc.client, r)
            except Exception:
                logger.exception("codex_rc: error applying timeout resolution")
        if expired:
            await self._post_system(
                embed=DiscordEmbed(
                    title="⌛ Approval auto-denied",
                    description=f"{len(expired)} prompt(s) timed out.",
                    color=0xFEE75C,
                )
            )
        return len(expired)

    # ---------------------------------------------------------- internals

    async def _start_codex_thread(self, sess: Session) -> str:
        result = await self.proc.request(
            "thread/start",
            {
                "cwd": str(self.proc.project_path),
                "sandbox": sess.sandbox,
                "approvalPolicy": sess.approval,
            },
            timeout=15.0,
        )
        thread_raw = result.get("thread") or {}
        thread = thread_raw if isinstance(thread_raw, dict) else {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError(f"thread/start returned no id: {result}")
        self._store_thread_metadata(thread, origin="thread/start")
        self.store.update_thread(self.channel_id, thread_id)
        self.store.record_thread(
            codex_thread_id=thread_id,
            channel_id=self.channel_id,
            project_path=str(self.proc.project_path),
        )
        await self._inject_operator_instructions(thread_id)
        return thread_id

    async def _inject_operator_instructions(self, thread_id: str) -> None:
        """Push operator Soul and Rules into the new thread.

        Tries ``thread/inject_items`` first (Codex's documented non-turn
        history append). If the build rejects that, queues the text as a
        first-turn prefix so it still lands in the conversation.
        """
        instructions = _load_operator_instructions()
        if instructions.is_empty():
            return
        items = _instruction_items(instructions)
        try:
            await self.proc.request(
                "thread/inject_items",
                {"threadId": thread_id, "items": items},
                timeout=15.0,
            )
            logger.info(
                "codex_rc: operator instructions injected via "
                "thread/inject_items (soul=%d chars, rules=%d chars)",
                len(instructions.soul or ""),
                len(instructions.rules or ""),
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "codex_rc: thread/inject_items unavailable (%s); "
                "deferring operator instructions to first-turn prefix",
                exc,
            )
            self._queue_instruction_prefix("\n\n".join(str(item["text"]) for item in items))

    async def _inject_handoff_context(
        self,
        thread_id: str,
        handoff: HandoffRecord,
    ) -> None:
        body = self.store.read_handoff_body(handoff)
        text = _wrap_handoff_context(handoff, body)
        try:
            await self.proc.request(
                "thread/inject_items",
                {
                    "threadId": thread_id,
                    "items": [{"type": "text", "role": "system", "text": text}],
                },
                timeout=15.0,
            )
            logger.info(
                "codex_rc: handoff %s injected into thread %s",
                handoff.handoff_id,
                thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "codex_rc: handoff inject unavailable (%s); "
                "deferring to first-turn prefix",
                exc,
            )
            self._queue_instruction_prefix(text)

    def _queue_instruction_prefix(self, text: str) -> None:
        if not text:
            return
        if self._pending_instruction_prefix:
            self._pending_instruction_prefix = (
                f"{self._pending_instruction_prefix}\n\n{text}"
            )
        else:
            self._pending_instruction_prefix = text

    async def _pump(self) -> None:
        logger.debug(
            "codex_rc: pump start channel=%s post_animated=%s cats=%d",
            self.channel_id,
            self.post_animated is not None,
            len(self._cats),
        )
        count = 0
        natural_eof = False
        try:
            async for notif in self.proc.client.notifications():
                count += 1
                method = notif.get("method")
                logger.debug(
                    "codex_rc: pump#%d channel=%s method=%s", count, self.channel_id, method
                )
                handoff_internal = self._capture_handoff_notification(notif)
                plan_handled = False
                if not handoff_internal:
                    self._maybe_cat_trigger(notif)
                await self._update_turn_state(notif)
                self._observe_workflow_turn_completion(notif)
                if not handoff_internal:
                    plan_handled = await self._handle_plan_progress(notif)
                self._update_thread_metadata_cache(notif)
                self._update_usage_cache(notif)
                if handoff_internal:
                    continue
                await self._maybe_auto_compact(notif)
                payloads = [] if plan_handled else self.notif.route(notif)
                payloads = [self._clean_workflow_payload(p) for p in payloads]
                payloads = [self._enrich_payload(notif, p) for p in payloads]
                workflow_prompt = self.workflow.observe_notification(notif)
                logger.debug(
                    "codex_rc: pump#%d method=%s routed=%d payloads",
                    count,
                    method,
                    len(payloads),
                )
                for payload in payloads:
                    try:
                        await self._post(payload.to_dict())
                    except Exception:
                        logger.exception("codex_rc: failed to POST notification")
                if workflow_prompt is not None:
                    try:
                        await self._post(workflow_prompt.to_dict())
                    except Exception:
                        # POST failed → state is still ``awaiting_approval`` but the
                        # user can never see / click a button. Roll the workflow
                        # back and post a plain-text fallback so the next message
                        # isn't blocked by the workflow lock.
                        cleared = self.workflow.cancel()
                        logger.exception(
                            "codex_rc: failed to POST workflow prompt — rolled "
                            "back workflow=%s", cleared,
                        )
                        with contextlib.suppress(Exception):
                            await self._post_system(
                                embed=DiscordEmbed(
                                    title="⚠️ Plan selection buttons failed",
                                    description=(
                                        "Couldn't render the Plan A / Plan B "
                                        "buttons (Discord post failed). The "
                                        "workflow has been cleared — send a new "
                                        "message to retry."
                                    ),
                                    color=COLOR_WARN,
                                )
                            )
            # async-for exited without a CancelledError → transport EOF.
            natural_eof = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex_rc: pump crashed channel=%s", self.channel_id)
            raise
        finally:
            logger.debug(
                "codex_rc: pump end channel=%s total=%d", self.channel_id, count
            )
        # If the transport hung up unexpectedly (child still alive — typical
        # for a dropped websocket — or stdio peer that EOF'd without dying),
        # funnel into the same recovery path as a child crash. _on_codex_crash
        # is idempotent so a concurrent child-exit callback is safe.
        if (
            natural_eof
            and not self._intentional_stop
            and not self._recovery_in_progress
        ):
            logger.warning(
                "codex_rc: transport EOF without stop signal channel=%s — "
                "treating as crash",
                self.channel_id,
            )
            asyncio.create_task(
                self._on_codex_crash(None),
                name=f"codex-rc-silent-crash-{self.channel_id}",
            )

    # ---------------------------------------------------------- handoff capture

    def _capture_handoff_notification(self, notif: JsonObj) -> bool:
        capture = self._handoff_capture
        if capture is None:
            return False

        method = str(notif.get("method") or "")
        params = notif.get("params") or {}
        thread_id = str(params.get("threadId") or "")
        turn_id = _turn_id_from_notification(params)

        if thread_id and thread_id != capture.thread_id:
            return False

        if capture.turn_id is None:
            if method != "turn/started" or not turn_id:
                if method == "error" and not capture.future.done():
                    capture.future.set_exception(
                        RuntimeError(_error_message_from_params(params))
                    )
                    return True
                return False
            capture.turn_id = turn_id

        if turn_id and turn_id != capture.turn_id:
            return False

        if method in {
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
            "item/plan/delta",
        }:
            capture.append(str(params.get("delta") or ""))
            return True

        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                text = str(item.get("text") or "")
                if text:
                    capture.parts = [text]
            return True

        if method == "turn/completed":
            if not capture.future.done():
                capture.future.set_result(capture.body())
            return True

        if method == "error":
            if not capture.future.done():
                capture.future.set_exception(
                    RuntimeError(_error_message_from_params(params))
                )
            return True

        return bool(turn_id == capture.turn_id)

    # ----------------------------------------------------- turn state / hooks

    async def _update_turn_state(self, notif: JsonObj) -> None:
        """Track active turn id (drives steer routing + reaction lifecycle)."""
        method = notif.get("method")
        params = notif.get("params") or {}
        if method == "turn/started":
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id") or params.get("turnId") or "")
            if turn_id:
                self._active_turn_id = turn_id
                self._file_change_emitted_for_turn = None
                self._idle_event.clear()
            if self.on_turn_stage:
                with contextlib.suppress(Exception):
                    await self.on_turn_stage(self.channel_id, "started", params)
            return
        if method in {"turn/completed", "error"}:
            self._active_turn_id = None
            self._file_change_emitted_for_turn = None
            self._idle_event.set()
            if self.on_turn_stage:
                with contextlib.suppress(Exception):
                    await self.on_turn_stage(self.channel_id, "completed", params)
            return
        if method == "item/started":
            item = params.get("item") or {}
            item_type = str(item.get("type") or "")
            if (
                item_type == "fileChange"
                and self._active_turn_id
                and self._file_change_emitted_for_turn != self._active_turn_id
                and self.on_turn_stage
            ):
                self._file_change_emitted_for_turn = self._active_turn_id
                with contextlib.suppress(Exception):
                    await self.on_turn_stage(self.channel_id, "file_change", params)

    # ----------------------------------------------------- usage / billing

    def _store_thread_metadata(self, thread: JsonObj, *, origin: str) -> None:
        self._last_thread_metadata = dict(thread)
        logger.info(
            "codex_rc: thread metadata channel=%s origin=%s thread=%s source=%s "
            "thread_source=%s session=%s model_provider=%s",
            self.channel_id,
            origin,
            _diagnostic_value(thread.get("id")),
            _diagnostic_value(thread.get("source")),
            _diagnostic_value(thread.get("threadSource")),
            _diagnostic_value(thread.get("sessionId")),
            _diagnostic_value(thread.get("modelProvider")),
        )

    def _update_thread_metadata_cache(self, notif: JsonObj) -> None:
        if notif.get("method") != "thread/started":
            return
        params = notif.get("params") or {}
        thread = params.get("thread") if isinstance(params, dict) else None
        if isinstance(thread, dict):
            self._store_thread_metadata(thread, origin="thread/started")

    def _update_usage_cache(self, notif: JsonObj) -> None:
        method = notif.get("method")
        params = notif.get("params") or {}
        if method == "thread/tokenUsage/updated":
            self._last_token_usage = params
            logger.debug(
                "codex_rc: usage updated channel=%s thread=%s turn=%s %s",
                self.channel_id,
                params.get("threadId"),
                params.get("turnId"),
                _format_token_usage(params) or "no token breakdown",
            )
        elif method == "account/rateLimits/updated":
            self._last_rate_limit = params
            logger.debug(
                "codex_rc: rate limits updated channel=%s %s",
                self.channel_id,
                _format_rate_limit(params) or "no rate-limit breakdown",
            )

    def usage_diagnostic_lines(self) -> list[str]:
        lines: list[str] = []
        command = _codex_command(self.proc)
        raw_transport = getattr(self.proc, "transport_mode", None)
        transport = raw_transport if isinstance(raw_transport, str) else "?"
        codex_line = f"codex `{command}` · transport `{transport}`"
        init = self._codex_init or {}
        user_agent = _diagnostic_value(init.get("userAgent"))
        if user_agent:
            codex_line += f" · server `{user_agent}`"
        lines.append(codex_line)

        thread = self._last_thread_metadata or {}
        if thread:
            thread_parts: list[str] = []
            for key, label in (
                ("id", "thread"),
                ("source", "source"),
                ("threadSource", "threadSource"),
                ("sessionId", "session"),
                ("modelProvider", "modelProvider"),
            ):
                value = _diagnostic_value(thread.get(key))
                if value:
                    thread_parts.append(f"{label} `{value}`")
            if thread_parts:
                lines.append("thread " + " · ".join(thread_parts))

        if self._last_token_usage:
            usage_parts: list[str] = []
            thread_id = _diagnostic_value(self._last_token_usage.get("threadId"))
            turn_id = _diagnostic_value(self._last_token_usage.get("turnId"))
            if thread_id:
                usage_parts.append(f"thread `{thread_id}`")
            if turn_id:
                usage_parts.append(f"turn `{turn_id}`")
            formatted = _format_token_usage(self._last_token_usage)
            if formatted:
                usage_parts.append(formatted)
            if usage_parts:
                lines.append("usage " + " · ".join(usage_parts))

        if self._last_rate_limit:
            formatted = _format_rate_limit(self._last_rate_limit)
            if formatted:
                lines.append("rate-limit " + formatted)
        return lines

    async def _maybe_auto_compact(self, notif: JsonObj) -> None:
        """Fire ``thread/compact/start`` when cumulative input tokens cross
        the operator-configured threshold. Disabled if threshold is 0.
        """
        if (
            notif.get("method") != "thread/tokenUsage/updated"
            or self._compacting
            or self.auto_compact_threshold_tokens <= 0
        ):
            return
        params = notif.get("params") or {}
        usage = params.get("tokenUsage") or {}
        used = usage.get("inputTokens") or usage.get("totalTokens")
        if not isinstance(used, int) or used < self.auto_compact_threshold_tokens:
            return
        thread_id = self._current_thread_id()
        if not thread_id:
            return
        self._compacting = True
        try:
            await self._post_system(
                embed=DiscordEmbed(
                    title="🗜️ Compacting context",
                    description=(
                        f"Token usage at {used:,} crossed the auto-compact "
                        f"threshold ({self.auto_compact_threshold_tokens:,}). "
                        "Asking codex to compact this thread."
                    ),
                    color=0x3498DB,
                )
            )
            await self.proc.request(
                "thread/compact/start",
                {"threadId": thread_id},
                timeout=120.0,
            )
        except Exception:
            logger.exception("codex_rc: auto-compact failed channel=%s", self.channel_id)
        finally:
            self._compacting = False

    def _current_thread_id(self) -> str | None:
        sess = self.store.get(self.channel_id)
        return sess.codex_thread_id if sess and sess.codex_thread_id else None

    # ----------------------------------------------------- payload enrichers

    def _enrich_payload(self, notif: JsonObj, payload: DiscordPayload) -> DiscordPayload:
        method = notif.get("method")
        if method != "turn/completed":
            return payload
        payload = self._append_usage_footer(payload)
        payload = self._prefix_owner_mention(payload)
        return payload

    def _clean_workflow_payload(self, payload: DiscordPayload) -> DiscordPayload:
        if not self.workflow.enabled or not payload.content:
            return payload
        payload.content = strip_workflow_metadata(payload.content)
        return payload

    def _append_usage_footer(self, payload: DiscordPayload) -> DiscordPayload:
        if payload.embed is None:
            return payload
        extras: list[str] = []
        if self._last_token_usage:
            usage = _token_usage_breakdown(self._last_token_usage)
            total = usage.get("totalTokens")
            inp = usage.get("inputTokens")
            if isinstance(total, int):
                extras.append(f"💎 {total:,} tokens")
            elif isinstance(inp, int):
                extras.append(f"💎 {inp:,} in")
        if self._last_rate_limit:
            # Codex rateLimits shape is provisional; surface whatever we find.
            rate_limit = _rate_limit_snapshot(self._last_rate_limit)
            for key in ("primary", "secondary"):
                bucket = rate_limit.get(key) or {}
                pct = bucket.get("remainingPercent") or bucket.get("usedPercent")
                if isinstance(pct, (int, float)):
                    label = "remaining" if "remainingPercent" in bucket else "used"
                    extras.append(f"🪙 {key} {int(pct)}% {label}")
                    break
        if not extras:
            return payload
        suffix = " · ".join(extras)
        existing = payload.embed.footer_text or ""
        payload.embed.footer_text = (
            f"{existing} · {suffix}" if existing else suffix
        )
        return payload

    def _prefix_owner_mention(self, payload: DiscordPayload) -> DiscordPayload:
        if not self.owner_mention_id:
            return payload
        mention = f"<@{self.owner_mention_id}>"
        if payload.content:
            payload.content = f"{mention} {payload.content}"
        else:
            payload.content = mention
        return payload

    # ---------------------------------------------------------- plan progress

    async def _handle_plan_progress(self, notif: JsonObj) -> bool:
        method = notif.get("method")
        params = notif.get("params") or {}
        if method == "turn/plan/updated":
            if self.post_update is None:
                return True
            previous = self._plan_snapshot
            snapshot = _plan_snapshot_from_notification(
                params,
                fallback_turn_id=self._active_turn_id,
                previous=previous,
            )
            if (
                previous is None
                or previous.key != snapshot.key
                or previous.active_row != snapshot.active_row
            ):
                self._plan_tick = 0
            self._plan_snapshot = snapshot
            await self._post_plan_progress_frame()
            self._ensure_plan_animation()
            return True
        if method in {"turn/completed", "error"}:
            await self._finish_plan_progress(completed=method == "turn/completed")
        return False

    def _ensure_plan_animation(self) -> None:
        if self.post_update is None or self._plan_snapshot is None:
            return
        if self._plan_snapshot.active_row is None:
            return
        if self._plan_task and not self._plan_task.done():
            return
        self._plan_task = asyncio.create_task(
            self._animate_plan_progress(),
            name=f"codex-rc-plan-{self.channel_id}",
        )

    async def _step_plan_animation_frame(self) -> bool:
        if self._plan_snapshot is None or self._plan_snapshot.active_row is None:
            return False
        self._plan_tick += 1
        await self._post_plan_progress_frame()
        return True

    async def _animate_plan_progress(self) -> None:
        try:
            while self._plan_snapshot is not None and not self._plan_snapshot.completed:
                if not await self._step_plan_animation_frame():
                    return
                if self._plan_snapshot is None or self._plan_snapshot.completed:
                    return
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("codex_rc: plan progress animation failed")

    async def _finish_plan_progress(self, *, completed: bool) -> None:
        snapshot = self._plan_snapshot
        if snapshot is None:
            return
        self._cancel_plan_progress_task()
        snapshot.completed = completed
        self._plan_snapshot = snapshot
        await self._post_plan_progress_frame()
        self._plan_snapshot = None

    def _cancel_plan_progress(self) -> None:
        self._cancel_plan_progress_task()
        self._plan_snapshot = None

    def _cancel_plan_progress_task(self) -> None:
        task = self._plan_task
        self._plan_task = None
        if task and not task.done():
            task.cancel()

    async def _post_plan_progress_frame(self) -> None:
        if self.post_update is None or self._plan_snapshot is None:
            return
        payload = {
            "content": _render_plan_progress(self._plan_snapshot, tick=self._plan_tick)
        }
        try:
            async with self._post_lock:
                await self.post_update(
                    self.channel_id,
                    self._plan_snapshot.key,
                    payload,
                )
        except Exception:
            logger.exception("codex_rc: failed to update plan progress")

    # ------------------------------------------------------------- cat-of-patience

    def _maybe_cat_trigger(self, notif: JsonObj) -> None:
        method = notif.get("method")
        params = notif.get("params") or {}
        if method == "turn/started":
            if not self._cats:
                logger.warning("codex_rc: cat trigger skipped — no cats loaded")
                return
            if self.post_animated is None:
                logger.warning("codex_rc: cat trigger skipped — post_animated not wired")
                return
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id") or params.get("turnId") or "")
            if not turn_id:
                logger.warning("codex_rc: cat trigger skipped — no turn id")
                return
            if turn_id == self._cat_last_turn_id:
                logger.info("codex_rc: cat already fired for turn %s", turn_id)
                return
            self._cat_last_turn_id = turn_id
            self._cancel_cat()
            logger.info("codex_rc: spawning cat for turn %s", turn_id)
            self._cat_task = asyncio.create_task(
                self._draw_cat(),
                name=f"codex-rc-cat-{self.channel_id}",
            )
            return
        if method in {"turn/completed", "error"}:
            self._cancel_cat()

    def _cancel_cat(self) -> None:
        task = self._cat_task
        self._cat_task = None
        if task and not task.done():
            task.cancel()

    async def _draw_cat(self) -> None:
        if self.post_animated is None or not self._cats:
            return
        try:
            cat = random.choice(self._cats)
            frames = [str(f) for f in cat.get("frames", []) if str(f).strip()]
            if not frames:
                logger.warning("codex_rc: cat %s has no frames", cat.get("name"))
                return
            logger.info(
                "codex_rc: drawing cat %s (%d frames) in channel %s",
                cat.get("name"),
                len(frames),
                self.channel_id,
            )
            await self.post_animated(
                self.channel_id, frames, interval_s=self._cat_interval_s, loop=True
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("codex_rc: cat draw failed")

    async def _on_approval_prompt(self, prompt: DiscordPrompt, raw: JsonObj) -> None:
        if self.auto_approve:
            # CODEX_RC_AUTO_APPROVE: pick the success-styled button (style=3)
            # and resolve immediately. Logged so operators can audit later.
            accept = next((b for b in prompt.buttons if b.style == 3), None)
            if accept is not None:
                resolution = self.approvals.resolve(accept.custom_id)
                if resolution is not None:
                    try:
                        await apply_resolution(self.proc.client, resolution)
                    except Exception:
                        logger.exception(
                            "codex_rc: auto-approve apply failed prompt=%s",
                            prompt.prompt_id,
                        )
                    logger.info(
                        "codex_rc: auto-approved %s via %s",
                        prompt.prompt_id,
                        accept.custom_id,
                    )
                    return
            # No accept button found — fall through to manual prompt so the
            # human can still respond rather than the request hanging forever.
            logger.warning(
                "codex_rc: auto-approve cannot resolve %s (no success button) — "
                "posting manual prompt instead",
                prompt.prompt_id,
            )
        try:
            await self._post(prompt.to_dict())
        except Exception:
            # Roll the pending entry back so a retry can succeed.
            self.approvals._pending.pop(prompt.prompt_id, None)  # noqa: SLF001
            # Surface the failure to the operator so they aren't left wondering
            # why no approval buttons appeared. The raise below feeds an
            # Internal error reply back to Codex, which aborts the request.
            with contextlib.suppress(Exception):
                await self._post_system(
                    embed=DiscordEmbed(
                        title="⚠️ Approval prompt failed to send",
                        description=(
                            "Couldn't render approval buttons in this channel "
                            "(Discord post failed). The pending request was "
                            "aborted; ask Codex to retry the action."
                        ),
                        color=COLOR_WARN,
                    )
                )
            raise
        _ = raw  # raw request available for richer audit if we want it later

    async def _post(self, payload: JsonObj) -> None:
        # Serialise outbound POSTs per channel to keep message order intact.
        async with self._post_lock:
            await self.post(self.channel_id, payload)

    async def _post_system(self, *, embed: DiscordEmbed) -> None:
        await self._post(DiscordPayload(embed=embed).to_dict())


class Service:
    """Process-wide registry of active ChannelServices."""

    def __init__(
        self,
        *,
        store: SessionStore,
        log_root: Path,
        post: PostToGateway,
        post_update: PostEditable | None = None,
        post_animated: PostAnimated | None = None,
        sandbox_default: str = "workspace-write",
        approval_default: str = "on-request",
        verbose_notifs: bool = False,
        debug_root: Path | None = None,
        event_log_mode: str = "errors",
        error_log_retention_days: int = 30,
        codex_bin: str = "codex",
        codex_args: tuple[str, ...] = ("app-server",),
        transport_mode: str = "stdio",
        on_turn_stage: TurnStageCallback | None = None,
        owner_mention_id: str | None = None,
        auto_approve: bool = False,
        auto_compact_threshold_tokens: int = 0,
        workflow_enabled: bool = False,
    ) -> None:
        self.store = store
        self.log_root = log_root
        self.debug_root = debug_root or Path("./data/debug")
        self.post = post
        self.post_update = post_update
        self.post_animated = post_animated
        self.sandbox_default = sandbox_default
        self.approval_default = approval_default
        self.verbose_notifs = verbose_notifs
        self.event_log_mode = event_log_mode
        self.error_log_retention_days = error_log_retention_days
        self.codex_bin = codex_bin
        self.codex_args = codex_args
        self.transport_mode = transport_mode
        self.on_turn_stage = on_turn_stage
        self.owner_mention_id = owner_mention_id
        self.auto_approve = auto_approve
        self.auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self.workflow_enabled = workflow_enabled
        self._channels: dict[str, ChannelService] = {}
        self._mutex = asyncio.Lock()

    async def start_session(
        self,
        *,
        channel_id: str,
        project_path: str,
        sandbox: str | None = None,
        approval: str | None = None,
    ) -> Session:
        # Normalise at the entry point so path-comparisons (idempotency
        # check, /codex continue, autocomplete) all see the same string.
        project_path = _normalize_user_path(project_path)
        async with self._mutex:
            if channel_id in self._channels:
                sess = self.store.get(channel_id)
                if sess:
                    # Idempotent only if the caller asked for the same path.
                    # Otherwise tell the operator to /codex stop first so the
                    # path swap is explicit instead of silently lost.
                    if _same_path(project_path, sess.project_path):
                        return sess
                    raise RuntimeError(
                        f"channel already active on {sess.project_path}; "
                        f"run /codex stop before switching to {project_path}"
                    )
            sess = self.store.register(
                channel_id=channel_id,
                project_path=project_path,
                sandbox=sandbox or self.sandbox_default,
                approval=approval or self.approval_default,
            )
            proc = CodexServerProcess(
                project_path=Path(project_path),
                log_dir=default_log_dir(self.debug_root, channel_id),
                codex_bin=self.codex_bin,
                codex_args=self.codex_args,
                transport_mode=self.transport_mode,
                event_log_mode=self.event_log_mode,
                error_log_path=default_error_log_path(self.log_root),
                error_log_retention_days=self.error_log_retention_days,
            )
            cs = ChannelService(
                channel_id=channel_id,
                store=self.store,
                proc=proc,
                post=self.post,
                post_update=self.post_update,
                post_animated=self.post_animated,
                project_slug=_slug(channel_id),
                verbose_notifs=self.verbose_notifs,
                on_turn_stage=self.on_turn_stage,
                owner_mention_id=self.owner_mention_id,
                auto_approve=self.auto_approve,
                auto_compact_threshold_tokens=self.auto_compact_threshold_tokens,
                workflow_enabled=self.workflow_enabled,
            )
            try:
                await cs.start()
            except Exception:
                self.store.update_state(channel_id, "error")
                raise
            self._channels[channel_id] = cs
            return self.store.get(channel_id) or sess

    async def send_text(
        self,
        channel_id: str,
        text: str,
        *,
        image_urls: list[str] | None = None,
    ) -> None:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        await cs.send_text(text, image_urls=image_urls)

    async def resume_thread(self, channel_id: str, codex_thread_id: str) -> None:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        await cs.resume_thread(codex_thread_id)

    async def resume_handoff(self, channel_id: str, handoff_id: str) -> Session:
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None or handoff.status == "failed":
            raise KeyError(f"no ready handoff: {handoff_id}")
        cs = self._channels.get(channel_id)
        if cs is not None:
            if not _same_path(cs.proc.project_path, handoff.project_path):
                raise RuntimeError(
                    f"active session is on {cs.proc.project_path}; "
                    "run `/codex stop` before resuming a handoff from "
                    f"{handoff.project_path}"
                )
            self.store.update_handoff(channel_id, handoff.handoff_id)
            await cs.start_handoff_thread(handoff)
            sess = self.store.get(channel_id)
            assert sess is not None
            return sess

        await self.start_session(
            channel_id=channel_id,
            project_path=handoff.project_path,
            sandbox=handoff.sandbox,
            approval=handoff.approval,
        )
        self.store.update_handoff(channel_id, handoff.handoff_id)
        cs = self._channels[channel_id]
        await cs.start_handoff_thread(handoff)
        sess = self.store.get(channel_id)
        assert sess is not None
        return sess

    async def new_thread(self, channel_id: str) -> str:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        return await cs.new_thread()

    async def continue_session(self, channel_id: str) -> Session:
        """Resume the channel's last known project via a saved handoff.

        If the channel already has a live ChannelService, that one is
        returned as-is (idempotent). Otherwise spawn a fresh codex child on
        the same project_path / sandbox / approval and seed a fresh thread
        with the latest handoff when one exists.

        Raises ``RuntimeError`` if the channel has no record at all.
        """
        sess = self.store.get(channel_id)
        if sess is None:
            raise RuntimeError(
                "no previous session for this channel — "
                "run `/codex start <path>` first"
            )
        if channel_id in self._channels:
            return sess
        handoff = self.store.latest_handoff(channel_id)
        fresh = await self.start_session(
            channel_id=channel_id,
            project_path=sess.project_path,
            sandbox=sess.sandbox,
            approval=sess.approval,
        )
        if handoff is not None:
            try:
                await self._channels[channel_id].start_handoff_thread(handoff)
            except Exception:
                logger.exception(
                    "codex_rc: continue: handoff inject failed for %s",
                    handoff.handoff_id,
                )
        return self.store.get(channel_id) or fresh

    async def handle_button(self, channel_id: str, custom_id: str) -> bool:
        cs = self._channels.get(channel_id)
        if cs is None:
            return False
        return await cs.handle_button(custom_id)

    async def cancel_active_turn(self, channel_id: str) -> bool:
        cs = self._channels.get(channel_id)
        if cs is None:
            return False
        return await cs.cancel_active_turn()

    async def rollback(self, channel_id: str, *, count: int = 1) -> int:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        return await cs.rollback(count=count)

    async def set_model(self, channel_id: str, name: str | None) -> JsonObj:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        return await cs.set_model(name)

    async def set_permissions(self, channel_id: str, preset: str) -> dict[str, str]:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        return await cs.set_permissions(preset)

    async def review(self, channel_id: str, target: str | None = None) -> None:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        await cs.review(target)

    async def fork(self, channel_id: str) -> str:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        return await cs.fork()

    async def set_goal(self, channel_id: str, text: str) -> str:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        return await cs.set_goal(text)

    async def compact(self, channel_id: str) -> None:
        cs = self._channels.get(channel_id)
        if cs is None:
            raise KeyError(f"no active channel: {channel_id}")
        await cs.compact()

    def status_text(self, channel_id: str) -> str | None:
        sess = self.store.get(channel_id)
        if sess is None:
            return None
        lines = [
            f"channel `{sess.channel_id}` · state `{sess.state}` · "
            f"project `{sess.project_path}`",
            f"thread `{sess.codex_thread_id or '—'}` · "
            f"handoff `{sess.handoff_id or '—'}` · "
            f"sandbox `{sess.sandbox}` · approval `{sess.approval}`",
        ]
        cs = self._channels.get(channel_id)
        if cs is not None:
            diagnostics = cs.usage_diagnostic_lines()
            if diagnostics:
                lines.append("diagnostics:")
                lines.extend(f"- {line}" for line in diagnostics)
        return "\n".join(lines)

    def capabilities_text(self, channel_id: str) -> str:
        sess = self.store.get(channel_id)
        cs = self._channels.get(channel_id)
        command = f"{self.codex_bin} {' '.join(self.codex_args)}".strip()
        transport = self.transport_mode
        thread_id = sess.codex_thread_id if sess else None
        active = cs is not None

        lines = [
            "**Codex CLI capability audit**",
            f"runtime `{command}` · transport `{transport}` · "
            f"session `{'active' if active else 'inactive'}`",
            "mode `app-server/threaded`, not per-turn `codex exec`",
        ]
        if sess is not None:
            lines.append(
                f"project `{sess.project_path}` · thread `{thread_id or '—'}`"
            )
        if cs is not None:
            diagnostics = cs.usage_diagnostic_lines()
            if diagnostics:
                lines.append("diagnostics:")
                lines.extend(f"- {line}" for line in diagnostics[:3])

        lines.extend(
            [
                "",
                "**Used well**",
                _capability_line(
                    "✅",
                    "App-server backend",
                    "`thread/start` + `turn/start` drive persistent Codex turns.",
                ),
                _capability_line(
                    "✅",
                    "In-flight steering",
                    "messages during an active turn use `turn/steer`.",
                ),
                _capability_line(
                    "✅",
                    "Approvals and sandbox",
                    "Codex tool/file/permission approvals are bridged to Discord buttons.",
                ),
                _capability_line(
                    "✅",
                    "Interactive slash parity",
                    "`/model`, `/permissions`, `/review`, `/fork`, `/goal`, `/compact` are wired.",
                ),
                _capability_line(
                    "✅",
                    "Images and context",
                    "Discord image inputs, operator instructions, compaction, and handoff context are sent into Codex threads.",
                ),
            ]
        )
        remote_status = "✅" if transport == "ws" else "◐"
        remote_detail = (
            "`codex --remote` can attach to the same app-server."
            if transport == "ws"
            else "enable `CODEX_RC_TRANSPORT=ws` for live TUI attach."
        )
        lines.extend(
            [
                "",
                "**Partial or intentional**",
                _capability_line(remote_status, "Remote TUI attach", remote_detail),
                _capability_line(
                    "◐",
                    "Resume semantics",
                    "local `thread/resume` exists, but `/continue` prefers fresh handoff context by design.",
                ),
                _capability_line(
                    "◐",
                    "History",
                    "codex_rc keeps its own 10-item memory; it does not yet expose full Codex `thread/list/read`.",
                ),
            ]
        )
        lines.extend(
            [
                "",
                "**Not wired yet**",
                _capability_line(
                    "—",
                    "MCP/plugin/skills inventory",
                    "Codex has protocol support, but Discord has no audit or install surface yet.",
                ),
                _capability_line(
                    "—",
                    "Custom CLI slash commands and @ file search",
                    "native Codex UX is only partially mirrored in Discord.",
                ),
            ]
        )
        return "\n".join(lines)

    async def stop_session(self, channel_id: str) -> None:
        async with self._mutex:
            cs = self._channels.pop(channel_id, None)
        if cs is not None:
            try:
                await cs.stop()
            except Exception:
                logger.exception("codex_rc: error stopping channel %s", channel_id)
        else:
            # No live ChannelService — at least mark stopped in the store.
            self.store.update_state(channel_id, "stopped")

    async def sweep_all_approvals(self) -> int:
        total = 0
        for cs in list(self._channels.values()):
            total += await cs.sweep_approvals()
        return total

    async def shutdown(self) -> None:
        """Stop every active ChannelService — terminates each codex child
        and kills its tmux session. Called from the bot's finally block so
        a SIGINT / disconnect does not leave orphan ``codex app-server``
        processes behind.
        """
        async with self._mutex:
            channels = dict(self._channels)
            self._channels.clear()
        for channel_id, cs in channels.items():
            try:
                await cs.stop()
            except Exception:
                logger.exception("codex_rc: shutdown error for %s", channel_id)

    def is_active(self, channel_id: str) -> bool:
        return channel_id in self._channels


def _slug(channel_id: str) -> str:
    """Map a Discord channel id to a tmux-safe slug.

    tmux rejects ``:`` and ``.``; numeric Discord IDs are already safe but we
    strip just in case (some external systems use synthetic ids).
    """
    return "".join(c for c in str(channel_id) if c.isalnum() or c == "-") or "anon"


__all__ = [
    "ChannelService",
    "PostToGateway",
    "Service",
    "permission_preset_config",
    "permission_preset_names",
]
