"""Environment-driven runtime settings for codex_rc.

Settings are loaded once at startup from environment variables (which
``python-dotenv`` populates from ``.env`` during ``cli_discord``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Settings(BaseModel):
    access_map: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """Channel-aware allowlist.

    Shape: ``{"*": ("uid1", ...), "<channel_id>": ("uid2", ...)}``. The
    ``"*"`` key is the wildcard / cross-channel allowlist. Legacy CSV
    ``"uid1,uid2"`` is parsed into ``{"*": ("uid1", "uid2")}``. Empty
    map admits everyone (single-tenant convenience for local dev).
    """
    sandbox_default: str = "workspace-write"
    approval_default: str = "on-request"
    memory_path: Path = Field(default_factory=lambda: Path("./data/state/memory.json"))
    log_root: Path = Field(default_factory=lambda: Path("./data/logs"))
    debug_root: Path = Field(default_factory=lambda: Path("./data/debug"))
    verbose_notifs: bool = False
    transport_mode: str = "ws"  # "stdio" | "ws"
    log_format: str = "text"  # "text" | "json"
    event_log_mode: str = "errors"  # "errors" | "debug" | "off"
    error_log_retention_days: int = 30
    thread_history_max: int = 100


def _resolve_memory_path(raw: str) -> Path:
    """If the operator left a legacy ``CODEX_RC_DB_PATH=...sqlite``-style
    env value in place, transparently route it to a JSON file next to
    it. The legacy SQLite file is still picked up by the store's one-shot
    migration."""
    p = Path(raw or "./data/state/memory.json").expanduser()
    if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        json_neighbour = p.with_name("memory.json")
        logger.info(
            "codex_rc: %s looks like a legacy SQLite path; "
            "using JSON memory at %s instead",
            p,
            json_neighbour,
        )
        return json_neighbour
    return p


def _normalise_event_log_mode(raw: str) -> str:
    mode = raw.strip().lower() or "errors"
    if mode in {"error", "minimal", "prod", "production"}:
        return "errors"
    if mode in {"trace", "dev", "development"}:
        return "debug"
    if mode in {"none", "false", "0", "disabled"}:
        return "off"
    if mode in {"errors", "debug", "off"}:
        return mode
    logger.warning(
        "CODEX_RC_EVENT_LOG_MODE=%r is invalid; using errors "
        "(valid: errors, debug, off)",
        raw,
    )
    return "errors"


def _non_negative_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; using %d", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%r is negative; using %d", name, raw, default)
        return default
    return value


def _parse_access(raw: str) -> dict[str, tuple[str, ...]]:
    """Parse CODEX_RC_ALLOWED_USER_IDS into a channel→user-ids map.

    Accepts two shapes:

    * **JSON object** (new) — ``{"*": ["uid1"], "123": ["uid2"]}``. Keys
      are channel ids; ``"*"`` is the wildcard.
    * **CSV list** (legacy) — ``"uid1,uid2"`` is mapped to
      ``{"*": ("uid1", "uid2")}`` so existing setups keep working.

    Empty / blank input → empty map (admits everyone).
    """
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith(("{", "[")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("CODEX_RC_ALLOWED_USER_IDS: invalid JSON (%s); ignoring", exc)
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "CODEX_RC_ALLOWED_USER_IDS: expected JSON object, got %s; "
                "use {\"*\": [\"uid\", ...]} for a wildcard or a plain CSV "
                "for legacy behaviour",
                type(data).__name__,
            )
            return {}
        out: dict[str, tuple[str, ...]] = {}
        for key, value in data.items():
            if not isinstance(value, list):
                logger.warning(
                    "CODEX_RC_ALLOWED_USER_IDS[%r]: expected list, skipping", key
                )
                continue
            uids = tuple(str(v).strip() for v in value if str(v).strip())
            if uids:
                out[str(key)] = uids
        return out
    uids = tuple(s.strip() for s in raw.split(",") if s.strip())
    return {"*": uids} if uids else {}


def load_settings_from_env() -> Settings:
    return Settings(
        access_map=_parse_access(os.environ.get("CODEX_RC_ALLOWED_USER_IDS", "")),
        sandbox_default=os.environ.get(
            "CODEX_RC_DEFAULT_SANDBOX", "workspace-write"
        ),
        approval_default=os.environ.get(
            "CODEX_RC_DEFAULT_APPROVAL", "on-request"
        ),
        memory_path=_resolve_memory_path(
            os.environ.get("CODEX_RC_MEMORY_PATH", "")
            or os.environ.get("CODEX_RC_DB_PATH", "")
            or "./data/state/memory.json"
        ),
        log_root=Path(os.environ.get("CODEX_RC_LOG_ROOT", "./data/logs")),
        debug_root=Path(os.environ.get("CODEX_RC_DEBUG_ROOT", "./data/debug")),
        verbose_notifs=os.environ.get("CODEX_RC_VERBOSE_NOTIFS", "0") in {"1", "true"},
        transport_mode=os.environ.get("CODEX_RC_TRANSPORT", "ws").strip().lower(),
        log_format=os.environ.get("CODEX_RC_LOG_FORMAT", "text").strip().lower(),
        event_log_mode=_normalise_event_log_mode(
            os.environ.get("CODEX_RC_EVENT_LOG_MODE", "errors")
        ),
        error_log_retention_days=_non_negative_int_from_env(
            "CODEX_RC_ERROR_LOG_RETENTION_DAYS", 30
        ),
        thread_history_max=_non_negative_int_from_env(
            "CODEX_RC_THREAD_HISTORY_MAX", 100
        ),
    )


__all__ = [
    "Settings",
    "load_settings_from_env",
    "_normalise_event_log_mode",
    "_parse_access",
]
