"""JSON-backed mapping: Discord channel ↔ Codex session metadata.

Replaces the previous SQLite backend so the operator can read (and, if
they want, hand-edit) ``data/state/memory.json`` alongside
``data/config/soul.md``.

Layout::

    {
      "version": 1,
      "sessions": {
        "<channel_id>": Session as dict
      },
      "threads": {
        "<codex_thread_id>": ThreadRecord as dict
      },
      "handoffs": {
        "<handoff_id>": HandoffRecord as dict
      }
    }

Atomic writes (temp file + ``os.replace``) protect against torn writes;
a synchronous ``threading.RLock`` serialises concurrent mutations even
though the bot runs on one asyncio loop. The class name is kept as
``SessionStore`` so every caller stays the same.

A one-shot migration runs on first construction: if the operator still
has a legacy ``sessions.db`` next to the new memory path and the new
file is empty, sessions and thread history are copied across so
``/codex continue`` keeps working without the user noticing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Session:
    channel_id: str
    project_path: str
    sandbox: str
    approval: str
    state: str  # "starting" | "running" | "stopped" | "error" | "crashed"
    codex_thread_id: str | None
    created_at: float
    updated_at: float
    handoff_id: str | None = None


@dataclass(slots=True, frozen=True)
class ThreadRecord:
    """A single Codex thread surfaced by ``/codex history``."""

    codex_thread_id: str
    channel_id: str
    project_path: str
    started_at: float
    last_used_at: float
    preview: str
    turn_count: int


@dataclass(slots=True, frozen=True)
class HandoffRecord:
    """A model-generated context package written on stop and injected into a
    fresh Codex thread on continue/resume."""

    handoff_id: str
    channel_id: str
    project_path: str
    sandbox: str
    approval: str
    source_codex_thread_id: str | None
    target_codex_thread_id: str | None
    file_path: str
    created_at: float
    updated_at: float
    preview: str
    status: str  # "ready" | "failed"
    error: str | None = None


_MEMORY_FILE_VERSION = 1
MAX_SESSIONS = 10
MAX_HANDOFFS = 10
MAX_THREADS = 100


class SessionStore:
    """JSON-on-disk session + thread store.

    Synchronous API: the bot runs on a single asyncio loop, the file is
    small, and writes are infrequent (one per session lifecycle event),
    so ``anyio.to_thread.run_sync`` would be theatre. Keep it simple.
    """

    def __init__(
        self,
        memory_path: str | Path,
        *,
        max_threads: int = MAX_THREADS,
    ) -> None:
        self.memory_path = Path(memory_path)
        self.max_threads = max(0, int(max_threads))
        parent = self.memory_path.parent
        if str(parent) and parent != Path(""):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load_or_init()
        self._maybe_migrate_from_sqlite()

    # -------------------------------------------------------------- internals

    def _load_or_init(self) -> dict[str, Any]:
        if not self.memory_path.is_file():
            return self._empty()
        try:
            with self.memory_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "codex_rc: memory file %s unreadable (%s) — starting empty",
                self.memory_path,
                exc,
            )
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("version", _MEMORY_FILE_VERSION)
        data.setdefault("sessions", {})
        data.setdefault("threads", {})
        data.setdefault("handoffs", {})
        return data

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": _MEMORY_FILE_VERSION,
            "sessions": {},
            "threads": {},
            "handoffs": {},
        }

    @property
    def handoff_dir(self) -> Path:
        return self.memory_path.parent / "handoffs"

    def _flush(self) -> None:
        self._prune_locked()
        tmp = self.memory_path.with_suffix(self.memory_path.suffix + ".tmp")
        body = json.dumps(self._data, indent=2, ensure_ascii=False) + "\n"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self.memory_path)

    def _prune_locked(self) -> None:
        sessions = self._data.get("sessions") or {}
        if len(sessions) > MAX_SESSIONS:
            ordered = sorted(
                sessions.items(),
                key=lambda item: -float((item[1] or {}).get("updated_at", 0)),
            )
            keep = {channel_id for channel_id, _ in ordered[:MAX_SESSIONS]}
            for channel_id in list(sessions.keys()):
                if channel_id not in keep:
                    sessions.pop(channel_id, None)

        handoffs = self._data.get("handoffs") or {}
        if len(handoffs) > MAX_HANDOFFS:
            ordered_handoffs = sorted(
                handoffs.items(),
                key=lambda item: -float((item[1] or {}).get("updated_at", 0)),
            )
            keep_handoffs = {
                handoff_id for handoff_id, _ in ordered_handoffs[:MAX_HANDOFFS]
            }
            for handoff_id, row in list(handoffs.items()):
                if handoff_id in keep_handoffs:
                    continue
                self._delete_handoff_file(row)
                handoffs.pop(handoff_id, None)

        self._prune_threads_locked()

    def _prune_threads_locked(self) -> None:
        threads = self._data.get("threads") or {}
        if self.max_threads <= 0 or len(threads) <= self.max_threads:
            return
        sessions = self._data.get("sessions") or {}
        keep: set[str] = set()
        for row in sessions.values():
            if not isinstance(row, dict):
                continue
            thread_id = row.get("codex_thread_id")
            if thread_id:
                keep.add(str(thread_id))
        ordered = sorted(
            threads.items(),
            key=lambda item: -float((item[1] or {}).get("last_used_at", 0)),
        )
        slots = max(0, self.max_threads - len(keep))
        for thread_id, _row in ordered:
            if thread_id in keep:
                continue
            if slots <= 0:
                break
            keep.add(thread_id)
            slots -= 1
        for thread_id in list(threads.keys()):
            if thread_id not in keep:
                threads.pop(thread_id, None)

    def _delete_handoff_file(self, row: dict[str, Any]) -> None:
        raw = str(row.get("file_path") or "")
        if not raw:
            return
        try:
            path = Path(raw).expanduser().resolve()
            base = self.handoff_dir.expanduser().resolve()
            path.relative_to(base)
        except Exception:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("codex_rc: failed to delete old handoff file %s", path)

    def _maybe_migrate_from_sqlite(self) -> None:
        """One-shot best-effort: pull rows out of a legacy ``sessions.db``
        sitting next to our memory file when the JSON file is fresh."""
        if self._data["sessions"] or self._data["threads"]:
            return
        sqlite_path = self.memory_path.parent / "sessions.db"
        if not sqlite_path.is_file():
            return
        try:
            import sqlite3

            with self._lock:
                conn = sqlite3.connect(str(sqlite_path))
                conn.row_factory = sqlite3.Row
                try:
                    for row in conn.execute("SELECT * FROM sessions"):
                        d = {k: row[k] for k in row.keys()}
                        self._data["sessions"][str(d["channel_id"])] = d
                except sqlite3.Error:
                    pass
                try:
                    for row in conn.execute("SELECT * FROM thread_history"):
                        d = {k: row[k] for k in row.keys()}
                        self._data["threads"][str(d["codex_thread_id"])] = d
                except sqlite3.Error:
                    pass
                conn.close()
                if self._data["sessions"] or self._data["threads"]:
                    self._flush()
                    logger.info(
                        "codex_rc: migrated %d session(s) + %d thread(s) from %s "
                        "→ %s",
                        len(self._data["sessions"]),
                        len(self._data["threads"]),
                        sqlite_path,
                        self.memory_path,
                    )
        except Exception:
            logger.exception(
                "codex_rc: SQLite migration failed; starting with empty memory"
            )

    # -------------------------------------------------------------- sessions

    def register(
        self,
        *,
        channel_id: str,
        project_path: str,
        sandbox: str = "workspace-write",
        approval: str = "on-request",
    ) -> Session:
        """Insert or replace the session for ``channel_id``.

        State always starts as ``"starting"`` and ``codex_thread_id`` is
        cleared, so re-registering a channel is a clean restart.
        ``created_at`` is preserved across re-registers when present.
        """
        now = time.time()
        with self._lock:
            existing = self._data["sessions"].get(channel_id) or {}
            self._data["sessions"][channel_id] = {
                "channel_id": channel_id,
                "project_path": project_path,
                "sandbox": sandbox,
                "approval": approval,
                "state": "starting",
                "codex_thread_id": None,
                "handoff_id": existing.get("handoff_id") or None,
                "created_at": float(existing.get("created_at", now)),
                "updated_at": now,
            }
            self._flush()
        sess = self.get(channel_id)
        assert sess is not None
        return sess

    def get(self, channel_id: str) -> Session | None:
        with self._lock:
            row = self._data["sessions"].get(channel_id)
        if row is None:
            return None
        return Session(
            channel_id=str(row.get("channel_id", channel_id)),
            project_path=str(row["project_path"]),
            sandbox=str(row["sandbox"]),
            approval=str(row["approval"]),
            state=str(row["state"]),
            codex_thread_id=(row.get("codex_thread_id") or None) and str(row["codex_thread_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            handoff_id=(row.get("handoff_id") or None) and str(row["handoff_id"]),
        )

    def list_active(self) -> list[Session]:
        return [s for s in self.list_all() if s.state in {"starting", "running", "error"}]

    def list_all(self) -> list[Session]:
        with self._lock:
            channel_ids = list(self._data["sessions"].keys())
        out: list[Session] = []
        for cid in channel_ids:
            s = self.get(cid)
            if s is not None:
                out.append(s)
        out.sort(key=lambda s: -s.updated_at)
        return out

    def update_state(self, channel_id: str, state: str) -> None:
        with self._lock:
            row = self._data["sessions"].get(channel_id)
            if row is None:
                return
            row["state"] = state
            row["updated_at"] = time.time()
            self._flush()

    def update_thread(self, channel_id: str, codex_thread_id: str) -> None:
        with self._lock:
            row = self._data["sessions"].get(channel_id)
            if row is None:
                return
            row["codex_thread_id"] = codex_thread_id or None
            row["updated_at"] = time.time()
            self._flush()

    def update_handoff(self, channel_id: str, handoff_id: str | None) -> None:
        with self._lock:
            row = self._data["sessions"].get(channel_id)
            if row is None:
                return
            row["handoff_id"] = handoff_id or None
            row["updated_at"] = time.time()
            self._flush()

    def remove(self, channel_id: str) -> None:
        with self._lock:
            self._data["sessions"].pop(channel_id, None)
            self._flush()

    # --------------------------------------------------------------- handoffs

    def new_handoff_id(self, channel_id: str) -> str:
        slug = "".join(c for c in str(channel_id) if c.isalnum() or c == "-")
        slug = slug[:24] or "channel"
        return f"handoff-{int(time.time())}-{slug}-{uuid.uuid4().hex[:8]}"

    def record_handoff(
        self,
        *,
        handoff_id: str,
        channel_id: str,
        project_path: str,
        sandbox: str,
        approval: str,
        source_codex_thread_id: str | None,
        body: str,
        status: str = "ready",
        error: str | None = None,
    ) -> HandoffRecord:
        now = time.time()
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.handoff_dir / f"{handoff_id}.md"
        self._write_text_atomic(file_path, body)
        preview = _handoff_preview(body)
        with self._lock:
            existing = self._data["handoffs"].get(handoff_id) or {}
            self._data["handoffs"][handoff_id] = {
                "handoff_id": handoff_id,
                "channel_id": channel_id,
                "project_path": project_path,
                "sandbox": sandbox,
                "approval": approval,
                "source_codex_thread_id": source_codex_thread_id or None,
                "target_codex_thread_id": existing.get("target_codex_thread_id"),
                "file_path": str(file_path),
                "created_at": float(existing.get("created_at", now)),
                "updated_at": now,
                "preview": preview,
                "status": status,
                "error": error,
            }
            session = self._data["sessions"].get(channel_id)
            if session is not None and status == "ready":
                session["handoff_id"] = handoff_id
                session["updated_at"] = now
            self._flush()
        rec = self.get_handoff(handoff_id)
        assert rec is not None
        return rec

    def mark_handoff_target(
        self,
        handoff_id: str,
        *,
        target_codex_thread_id: str,
    ) -> None:
        with self._lock:
            row = self._data["handoffs"].get(handoff_id)
            if row is None:
                return
            row["target_codex_thread_id"] = target_codex_thread_id or None
            row["updated_at"] = time.time()
            self._flush()

    def get_handoff(self, handoff_id: str) -> HandoffRecord | None:
        with self._lock:
            row = self._data["handoffs"].get(handoff_id)
        if row is None:
            return None
        return self._handoff_record(row)

    def list_handoffs(
        self,
        channel_id: str | None = None,
        *,
        limit: int = 10,
        include_failed: bool = False,
    ) -> list[HandoffRecord]:
        with self._lock:
            rows = list((self._data.get("handoffs") or {}).values())
        if channel_id is not None:
            rows = [r for r in rows if r.get("channel_id") == channel_id]
        if not include_failed:
            rows = [r for r in rows if r.get("status") != "failed"]
        rows.sort(key=lambda r: -float(r.get("updated_at", 0)))
        return [self._handoff_record(r) for r in rows[:limit]]

    def latest_handoff(self, channel_id: str) -> HandoffRecord | None:
        sess = self.get(channel_id)
        if sess and sess.handoff_id:
            rec = self.get_handoff(sess.handoff_id)
            if rec is not None and rec.status != "failed":
                return rec
        handoffs = self.list_handoffs(channel_id, limit=1)
        return handoffs[0] if handoffs else None

    def read_handoff_body(self, handoff: HandoffRecord) -> str:
        return Path(handoff.file_path).read_text(encoding="utf-8")

    @staticmethod
    def _write_text_atomic(path: Path, body: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(body.rstrip() + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    @staticmethod
    def _handoff_record(row: dict[str, Any]) -> HandoffRecord:
        return HandoffRecord(
            handoff_id=str(row["handoff_id"]),
            channel_id=str(row["channel_id"]),
            project_path=str(row["project_path"]),
            sandbox=str(row.get("sandbox") or "workspace-write"),
            approval=str(row.get("approval") or "on-request"),
            source_codex_thread_id=(
                (row.get("source_codex_thread_id") or None)
                and str(row["source_codex_thread_id"])
            ),
            target_codex_thread_id=(
                (row.get("target_codex_thread_id") or None)
                and str(row["target_codex_thread_id"])
            ),
            file_path=str(row["file_path"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            preview=str(row.get("preview", "") or ""),
            status=str(row.get("status") or "ready"),
            error=(row.get("error") or None) and str(row["error"]),
        )

    # ---------------------------------------------------------- thread history

    def record_thread(
        self,
        *,
        codex_thread_id: str,
        channel_id: str,
        project_path: str,
    ) -> None:
        """Idempotent: first turn of a fresh Codex thread."""
        now = time.time()
        with self._lock:
            existing = self._data["threads"].get(codex_thread_id)
            if existing:
                existing["last_used_at"] = now
            else:
                self._data["threads"][codex_thread_id] = {
                    "codex_thread_id": codex_thread_id,
                    "channel_id": channel_id,
                    "project_path": project_path,
                    "started_at": now,
                    "last_used_at": now,
                    "preview": "",
                    "turn_count": 0,
                }
            self._flush()

    def touch_thread(
        self,
        codex_thread_id: str,
        *,
        preview_addition: str | None = None,
        turn_increment: int = 1,
    ) -> None:
        """Bump last_used_at + turn count, optionally set the preview on
        the first user turn (subsequent turns don't overwrite it)."""
        now = time.time()
        with self._lock:
            row = self._data["threads"].get(codex_thread_id)
            if row is None:
                return
            row["last_used_at"] = now
            row["turn_count"] = int(row.get("turn_count", 0)) + int(turn_increment)
            if preview_addition is not None and not (row.get("preview") or "").strip():
                row["preview"] = preview_addition
            self._flush()

    def list_threads(self, channel_id: str, *, limit: int = 10) -> list[ThreadRecord]:
        with self._lock:
            rows = [
                r for r in self._data["threads"].values()
                if r.get("channel_id") == channel_id
            ]
        rows.sort(key=lambda r: -float(r.get("last_used_at", 0)))
        return [self._thread_record(r) for r in rows[:limit]]

    def get_thread(self, codex_thread_id: str) -> ThreadRecord | None:
        with self._lock:
            row = self._data["threads"].get(codex_thread_id)
        if row is None:
            return None
        return self._thread_record(row)

    @staticmethod
    def _thread_record(row: dict[str, Any]) -> ThreadRecord:
        return ThreadRecord(
            codex_thread_id=str(row["codex_thread_id"]),
            channel_id=str(row["channel_id"]),
            project_path=str(row["project_path"]),
            started_at=float(row["started_at"]),
            last_used_at=float(row["last_used_at"]),
            preview=str(row.get("preview", "") or ""),
            turn_count=int(row.get("turn_count", 0) or 0),
        )


def _handoff_preview(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:140]
    return ""


__all__ = ["HandoffRecord", "Session", "SessionStore", "ThreadRecord"]
