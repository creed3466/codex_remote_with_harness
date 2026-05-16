"""Server-initiated approval requests → Discord buttons (and back).

Codex's V2 protocol routes human-in-the-loop approval through five
server-initiated JSON-RPC requests:

================================================  ============================
method                                            response payload
================================================  ============================
``item/commandExecution/requestApproval``         ``{"decision": <enum>}``
``item/fileChange/requestApproval``               ``{"decision": <enum>}``
``item/permissions/requestApproval``              ``{"permissions": <profile>}``
``applyPatchApproval`` (v1 legacy)                ``{"decision": <enum>}``
``execCommandApproval`` (v1 legacy)               ``{"decision": <enum>}``
================================================  ============================

When one arrives, the rpc_client's ``server_request_handler`` is invoked.
The handler:

1. asks :class:`ApprovalRouter` to mint a :class:`DiscordPrompt`,
2. posts that prompt via a caller-supplied async callback,
3. returns ``None`` to leave the JSON-RPC response *deferred*.

Later, when the user clicks a Discord button, the gateway POSTs the
button's ``custom_id`` to our HTTP surface (Day 6). That handler calls
:meth:`ApprovalRouter.resolve` and forwards the resulting
:class:`ApprovalResolution` to :meth:`CodexRpcClient.respond_to_request`.

``custom_id`` format::

    codex_rc:<prompt_id>:<decision_token>

* ``prompt_id`` — 8 hex chars, minted at prompt time
* ``decision_token`` — short stable label tied to the method's decision enum

Day 5 covers the enum-style decisions only. Object-form decisions
(``acceptWithExecpolicyAmendment``, ``applyNetworkPolicyAmendment``,
``approved_execpolicy_amendment``, …) need richer UX and land later.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .notif_router import (
    COLOR_WARN,
    DiscordEmbed,
)
from .rpc_client import CodexRpcClient, JsonObj, ServerRequestHandler

logger = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "codex_rc"
PROMPT_ID_BYTES = 4  # → 8 hex chars
DEFAULT_TIMEOUT_S = 30 * 60.0
COMMAND_PREVIEW_CHARS = 500


# ====================================================================== styles

# Discord button styles (https://discord.com/developers/docs/interactions/message-components#button-object-button-styles)
STYLE_PRIMARY = 1
STYLE_SECONDARY = 2
STYLE_SUCCESS = 3
STYLE_DANGER = 4

COLOR_APPROVAL_SURFACE = 0x202123


# ====================================================================== payload

@dataclass(slots=True)
class DiscordButton:
    label: str
    style: int
    custom_id: str
    emoji: str | None = None
    disabled: bool = False

    def to_dict(self) -> JsonObj:
        d: JsonObj = {
            "type": 2,
            "style": self.style,
            "label": self.label[:80],
            "custom_id": self.custom_id[:100],
        }
        if self.emoji:
            d["emoji"] = {"name": self.emoji}
        if self.disabled:
            d["disabled"] = True
        return d


@dataclass(slots=True)
class DiscordPrompt:
    """Discord message ready for ``Message.send`` with action-row buttons."""

    prompt_id: str
    embed: DiscordEmbed
    buttons: list[DiscordButton]

    def to_dict(self) -> JsonObj:
        # Up to 5 buttons in one row (we never exceed 4 for an approval).
        rows = []
        if self.buttons:
            rows.append(
                {"type": 1, "components": [b.to_dict() for b in self.buttons[:5]]}
            )
        return {
            "embeds": [self.embed.to_dict()],
            "components": rows,
        }


# ====================================================================== state

@dataclass(slots=True)
class _Pending:
    prompt_id: str
    request_id: int
    method: str
    started_at: float
    params: JsonObj


@dataclass(slots=True)
class ApprovalResolution:
    """The JSON-RPC reply we owe the server for a previously-deferred request."""

    request_id: int
    method: str
    result: JsonObj | None = None
    error: JsonObj | None = None
    decision_token: str = ""

    def is_error(self) -> bool:
        return self.error is not None


# ====================================================================== custom_id

def _make_custom_id(prompt_id: str, token: str) -> str:
    return f"{CUSTOM_ID_PREFIX}:{prompt_id}:{token}"


def parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return ``(prompt_id, decision_token)`` or ``None`` if not ours."""
    parts = custom_id.split(":", 2)
    if len(parts) != 3 or parts[0] != CUSTOM_ID_PREFIX:
        return None
    return parts[1], parts[2]


# ====================================================================== prompts

def _command_preview(command: str) -> str:
    command = str(command)
    if len(command) <= COMMAND_PREVIEW_CHARS:
        return command
    omitted = len(command) - COMMAND_PREVIEW_CHARS
    return (
        f"{command[:COMMAND_PREVIEW_CHARS].rstrip()}\n"
        f"[command truncated: omitted {omitted} char(s)]"
    )


def _summarize_params(method: str, params: JsonObj) -> str:
    if method == "item/commandExecution/requestApproval":
        command = params.get("command") or ""
        cwd = params.get("cwd")
        reason = params.get("reason")
        lines = []
        if command:
            lines.append(f"```\n{_command_preview(str(command))}\n```")
        if cwd:
            lines.append(f"**cwd:** `{cwd}`")
        if reason:
            lines.append(f"**why:** {reason}")
        return "\n".join(lines) or "(no details)"
    if method == "execCommandApproval":
        cmd = params.get("command") or []
        cwd = params.get("cwd")
        parts = []
        if cmd:
            shown = _command_preview(" ".join(map(str, cmd)))
            parts.append(f"```\n{shown}\n```")
        if cwd:
            parts.append(f"**cwd:** `{cwd}`")
        if params.get("reason"):
            parts.append(f"**why:** {params['reason']}")
        return "\n".join(parts) or "(no details)"
    if method == "applyPatchApproval":
        changes = params.get("fileChanges") or {}
        files = list(changes.keys()) if isinstance(changes, dict) else []
        lines = [f"**files ({len(files)}):**"]
        for f in files[:10]:
            lines.append(f"  · `{f}`")
        if len(files) > 10:
            lines.append(f"  · …and {len(files) - 10} more")
        if params.get("reason"):
            lines.append(f"\n**why:** {params['reason']}")
        return "\n".join(lines)
    if method == "item/fileChange/requestApproval":
        bits = [f"**itemId:** `{params.get('itemId', '?')}`"]
        if params.get("grantRoot"):
            bits.append(f"**root:** `{params['grantRoot']}`")
        if params.get("reason"):
            bits.append(f"**why:** {params['reason']}")
        return "\n".join(bits)
    if method == "item/permissions/requestApproval":
        bits = [f"**cwd:** `{params.get('cwd', '?')}`"]
        perms = params.get("permissions") or {}
        if isinstance(perms, dict):
            if perms.get("fileSystem") is not None:
                bits.append(f"**filesystem:** `{perms['fileSystem']}`")
            if perms.get("network") is not None:
                bits.append(f"**network:** `{perms['network']}`")
        if params.get("reason"):
            bits.append(f"**why:** {params['reason']}")
        return "\n".join(bits)
    return "(unknown method)"


# Map each method to (decision_token → response builder).
# The builder takes the original ``params`` and returns the JSON-RPC result dict.

ResponseBuilder = Callable[[JsonObj], JsonObj]


def _decision(token: str) -> ResponseBuilder:
    return lambda _params: {"decision": token}


def _permissions_grant(params: JsonObj) -> JsonObj:
    return {"permissions": params.get("permissions") or {}}


def _permissions_deny(_params: JsonObj) -> JsonObj:
    # Empty profile = effectively deny everything that was requested.
    return {"permissions": {"fileSystem": {}, "network": {}}}


@dataclass(slots=True)
class _DecisionOption:
    token: str
    label: str
    style: int
    emoji: str | None = None


_COMMAND_BUILDERS: dict[str, ResponseBuilder] = {
    "accept": _decision("accept"),
    "accept_session": _decision("acceptForSession"),
    "decline": _decision("decline"),
    "cancel": _decision("cancel"),
}
_FILE_BUILDERS: dict[str, ResponseBuilder] = dict(_COMMAND_BUILDERS)
_PERMISSION_BUILDERS: dict[str, ResponseBuilder] = {
    "grant": _permissions_grant,
    "deny": _permissions_deny,
}
_V1_DECISION_BUILDERS: dict[str, ResponseBuilder] = {
    "approved": _decision("approved"),
    "approved_session": _decision("approved_for_session"),
    "denied": _decision("denied"),
    "abort": _decision("abort"),
}


_METHOD_OPTIONS: dict[str, tuple[dict[str, ResponseBuilder], tuple[_DecisionOption, ...]]] = {
    # V2 command exec — decision enum has 6 forms; we expose 4 (enum-only).
    "item/commandExecution/requestApproval": (
        _COMMAND_BUILDERS,
        (
            _DecisionOption("accept", "Run once", STYLE_SUCCESS),
            _DecisionOption("accept_session", "Run for session", STYLE_PRIMARY),
            _DecisionOption("decline", "Decline", STYLE_DANGER, "❌"),
            _DecisionOption("cancel", "Cancel turn", STYLE_SECONDARY),
        ),
    ),
    "item/fileChange/requestApproval": (
        _FILE_BUILDERS,
        (
            _DecisionOption("accept", "Apply once", STYLE_SUCCESS),
            _DecisionOption("accept_session", "Apply for session", STYLE_PRIMARY),
            _DecisionOption("decline", "Decline", STYLE_DANGER, "❌"),
            _DecisionOption("cancel", "Cancel turn", STYLE_SECONDARY),
        ),
    ),
    "item/permissions/requestApproval": (
        _PERMISSION_BUILDERS,
        (
            _DecisionOption("grant", "Grant access", STYLE_SUCCESS),
            _DecisionOption("deny", "Deny access", STYLE_DANGER),
        ),
    ),
    "applyPatchApproval": (
        _V1_DECISION_BUILDERS,
        (
            _DecisionOption("approved", "Approve once", STYLE_SUCCESS),
            _DecisionOption("approved_session", "Approve for session", STYLE_PRIMARY),
            _DecisionOption("denied", "Deny", STYLE_DANGER, "❌"),
            _DecisionOption("abort", "Abort", STYLE_SECONDARY),
        ),
    ),
    "execCommandApproval": (
        _V1_DECISION_BUILDERS,
        (
            _DecisionOption("approved", "Run once", STYLE_SUCCESS),
            _DecisionOption("approved_session", "Run for session", STYLE_PRIMARY),
            _DecisionOption("denied", "Deny", STYLE_DANGER, "❌"),
            _DecisionOption("abort", "Abort", STYLE_SECONDARY),
        ),
    ),
}


_TITLES = {
    "item/commandExecution/requestApproval": "Review Command",
    "item/fileChange/requestApproval": "Review File Changes",
    "item/permissions/requestApproval": "Review Permissions",
    "applyPatchApproval": "Review Patch",
    "execCommandApproval": "Review Command",
}


def is_approval_method(method: str) -> bool:
    return method in _METHOD_OPTIONS


# ====================================================================== router

class UnknownApprovalMethod(ValueError):
    pass


@dataclass
class ApprovalRouter:
    """Pure (no IO) state machine for human-in-the-loop approval."""

    timeout_s: float = DEFAULT_TIMEOUT_S
    _pending: dict[str, _Pending] = field(default_factory=dict, init=False)

    # ---------------------------------------------------- public surface

    def start(
        self,
        method: str,
        params: JsonObj,
        *,
        request_id: int,
        now: float | None = None,
    ) -> DiscordPrompt:
        if method not in _METHOD_OPTIONS:
            raise UnknownApprovalMethod(method)
        _, options = _METHOD_OPTIONS[method]
        prompt_id = secrets.token_hex(PROMPT_ID_BYTES)
        self._pending[prompt_id] = _Pending(
            prompt_id=prompt_id,
            request_id=request_id,
            method=method,
            started_at=now if now is not None else time.time(),
            params=params,
        )
        embed = DiscordEmbed(
            title=_TITLES.get(method, "Review Approval"),
            description=_summarize_params(method, params),
            color=COLOR_APPROVAL_SURFACE,
            footer_text=f"prompt {prompt_id}",
        )
        buttons = [
            DiscordButton(
                label=opt.label,
                style=opt.style,
                custom_id=_make_custom_id(prompt_id, opt.token),
                emoji=opt.emoji,
            )
            for opt in options
        ]
        return DiscordPrompt(prompt_id=prompt_id, embed=embed, buttons=buttons)

    def resolve(self, custom_id: str) -> ApprovalResolution | None:
        parsed = parse_custom_id(custom_id)
        if parsed is None:
            return None
        prompt_id, token = parsed
        pending = self._pending.pop(prompt_id, None)
        if pending is None:
            return None
        builders, options = _METHOD_OPTIONS[pending.method]
        valid_tokens = {opt.token for opt in options}
        if token not in valid_tokens:
            # Re-pend so a follow-up click can still resolve.
            self._pending[prompt_id] = pending
            return ApprovalResolution(
                request_id=pending.request_id,
                method=pending.method,
                error={"code": -32602, "message": f"unknown decision: {token}"},
            )
        result = builders[token](pending.params)
        return ApprovalResolution(
            request_id=pending.request_id,
            method=pending.method,
            result=result,
            decision_token=token,
        )

    def sweep(self, *, now: float | None = None) -> list[ApprovalResolution]:
        """Return auto-deny resolutions for any prompt past ``timeout_s``."""
        t = now if now is not None else time.time()
        expired: list[ApprovalResolution] = []
        for pid in list(self._pending.keys()):
            pending = self._pending[pid]
            if t - pending.started_at < self.timeout_s:
                continue
            self._pending.pop(pid, None)
            expired.append(self._auto_deny(pending, reason="timeout"))
        return expired

    def cancel_all(self, *, reason: str = "shutdown") -> list[ApprovalResolution]:
        out = []
        for pid in list(self._pending.keys()):
            pending = self._pending.pop(pid)
            out.append(self._auto_deny(pending, reason=reason))
        return out

    def pending_count(self) -> int:
        return len(self._pending)

    # ---------------------------------------------------- factories

    def as_server_request_handler(
        self,
        post_prompt: Callable[[DiscordPrompt, JsonObj], Awaitable[None]],
    ) -> ServerRequestHandler:
        """Bind to a CodexRpcClient.

        ``post_prompt`` is invoked with ``(prompt, raw_request)`` so the
        gateway side can decide where to send the prompt (which channel,
        which thread). The handler always returns ``None`` (deferred); the
        gateway's HTTP layer feeds button clicks back through
        :meth:`resolve` + :meth:`apply_resolution`.
        """

        async def _handle(method: str, params: JsonObj, raw: JsonObj) -> JsonObj | None:
            if not is_approval_method(method):
                # Not ours — let other handlers chain in, but the rpc_client
                # only supports a single handler. We respond "method not found"
                # so the server can fail fast for anything we don't grok yet.
                return None  # rpc_client will leave it unanswered; safer than guessing
            request_id = int(raw.get("id"))
            prompt = self.start(method, params, request_id=request_id)
            try:
                await post_prompt(prompt, raw)
            except Exception:
                # Roll the prompt back so a retry can succeed.
                self._pending.pop(prompt.prompt_id, None)
                raise
            return None

        return _handle

    # ---------------------------------------------------- internals

    def _auto_deny(self, pending: _Pending, *, reason: str) -> ApprovalResolution:
        method = pending.method
        if method == "item/permissions/requestApproval":
            result: JsonObj = _permissions_deny(pending.params)
            token = "deny"
        elif method in {"applyPatchApproval", "execCommandApproval"}:
            result = {"decision": "denied"}
            token = "denied"
        else:
            result = {"decision": "decline"}
            token = "decline"
        logger.warning(
            "codex_rc: auto-denying %s prompt=%s reason=%s",
            method,
            pending.prompt_id,
            reason,
        )
        return ApprovalResolution(
            request_id=pending.request_id,
            method=method,
            result=result,
            decision_token=token,
        )


# ====================================================================== glue

async def apply_resolution(
    client: CodexRpcClient, resolution: ApprovalResolution
) -> None:
    """Push an :class:`ApprovalResolution` to the JSON-RPC server."""
    await client.respond_to_request(
        resolution.request_id, result=resolution.result, error=resolution.error
    )


def make_timed_out_embed(prompt_id: str) -> DiscordEmbed:
    """Embed to edit-in-place when a prompt is auto-denied by timeout."""
    return DiscordEmbed(
        title="⌛ Approval timed out",
        description=f"Prompt `{prompt_id}` auto-declined after timeout.",
        color=COLOR_WARN,
    )


__all__ = [
    "ApprovalResolution",
    "ApprovalRouter",
    "DiscordButton",
    "DiscordPrompt",
    "UnknownApprovalMethod",
    "apply_resolution",
    "is_approval_method",
    "make_timed_out_embed",
    "parse_custom_id",
]


# Suppress unused alarms from utility re-export.
_ = Any
