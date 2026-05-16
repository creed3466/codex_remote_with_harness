"""On-disk store for in-flight and archived workflows.

Each workflow lives in its own directory under ``<root>/<workflow_id>/`` and
holds the per-stage handoff markdown files plus a ``meta.json`` describing
the workflow's state. Completed or cancelled workflows are moved to
``<root>/_archive/<workflow_id>-<timestamp>/`` so the markdown trail and
metadata stay available for debugging without crowding the active set.

The store is intentionally tiny: filesystem-only, no locking. Each
``ChannelService`` owns one workflow at a time and runs on a single
asyncio loop, so there is no contention to defend against.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path

_META_FILENAME = "meta.json"
_ARCHIVE_DIRNAME = "_archive"

#: Project-root-relative directory the codex agent sees for workflow files.
#: Kept as a string constant (not a ``Path``) because it's embedded
#: verbatim into stage prompts that codex tools resolve against the
#: project cwd.
WORKFLOW_DIR_RELPATH = "data/workflow"


def workflow_handoff_relpath(workflow_id: str, *, stage: int) -> str:
    """Return the cwd-relative path the codex agent should read / write
    for the given stage's handoff markdown file.

    This is the *one* place the stage-handoff filename pattern is
    defined; ``WorkflowFileStore`` uses the same pattern for on-disk
    layout. Keeping them in sync via this helper avoids drift between
    the prompt the agent sees and the path the service watches.
    """
    return f"{WORKFLOW_DIR_RELPATH}/{workflow_id}/stage_{stage}_handoff.md"


@dataclass(slots=True, frozen=True)
class WorkflowMeta:
    """In-memory view of a workflow's ``meta.json`` file.

    Mutations are expressed as ``replace()`` calls on a frozen dataclass so
    the store's read/modify/write cycle stays explicit.
    """

    workflow_id: str
    channel_id: str
    main_thread_id: str
    created_at_ms: int
    current_stage: int
    ephemeral_thread_ids: tuple[str, ...] = ()
    completed_at_ms: int | None = None
    outcome: str | None = None
    last_error: str | None = None

    def to_json_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "channel_id": self.channel_id,
            "main_thread_id": self.main_thread_id,
            "created_at_ms": self.created_at_ms,
            "current_stage": self.current_stage,
            "ephemeral_thread_ids": list(self.ephemeral_thread_ids),
            "completed_at_ms": self.completed_at_ms,
            "outcome": self.outcome,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json_dict(cls, raw: dict) -> WorkflowMeta:
        return cls(
            workflow_id=str(raw["workflow_id"]),
            channel_id=str(raw["channel_id"]),
            main_thread_id=str(raw["main_thread_id"]),
            created_at_ms=int(raw["created_at_ms"]),
            current_stage=int(raw.get("current_stage", 1)),
            ephemeral_thread_ids=tuple(
                str(x) for x in raw.get("ephemeral_thread_ids", [])
            ),
            completed_at_ms=(
                int(raw["completed_at_ms"])
                if raw.get("completed_at_ms") is not None
                else None
            ),
            outcome=(str(raw["outcome"]) if raw.get("outcome") is not None else None),
            last_error=(
                str(raw["last_error"]) if raw.get("last_error") is not None else None
            ),
        )


class WorkflowFileStore:
    """Filesystem-backed storage for workflow directories and metadata."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / _ARCHIVE_DIRNAME).mkdir(exist_ok=True)

    # ---- path helpers ---------------------------------------------------

    def workflow_dir(self, workflow_id: str) -> Path:
        return self.root / workflow_id

    def handoff_path(self, workflow_id: str, *, stage: int) -> Path:
        return self.workflow_dir(workflow_id) / f"stage_{stage}_handoff.md"

    def meta_path(self, workflow_id: str) -> Path:
        return self.workflow_dir(workflow_id) / _META_FILENAME

    # ---- lifecycle ------------------------------------------------------

    def create(
        self,
        workflow_id: str,
        *,
        channel_id: str,
        main_thread_id: str,
    ) -> Path:
        """Provision a new workflow directory and initial ``meta.json``.

        Raises :class:`FileExistsError` if the workflow id is already
        active. (Callers should generate unique ids — duplicates here mean
        a state-machine bug, not user input.)
        """
        wf_dir = self.workflow_dir(workflow_id)
        if wf_dir.exists():
            raise FileExistsError(
                f"workflow directory already exists: {wf_dir}"
            )
        wf_dir.mkdir(parents=True)
        meta = WorkflowMeta(
            workflow_id=workflow_id,
            channel_id=channel_id,
            main_thread_id=main_thread_id,
            created_at_ms=int(time.time() * 1000),
            current_stage=1,
        )
        self._write_meta_file(self.meta_path(workflow_id), meta)
        return wf_dir

    def archive(self, workflow_id: str, *, outcome: str) -> Path:
        """Move an active workflow directory under ``_archive/`` with an
        outcome stamp on its meta.

        ``outcome`` is a free-form short string (e.g. ``"completed"``,
        ``"cancelled"``, ``"failed:stage3"``) recorded on ``meta.json`` so
        post-mortem inspection can distinguish how each archived workflow
        ended without re-reading the handoff files.
        """
        wf_dir = self.workflow_dir(workflow_id)
        if not wf_dir.exists():
            raise FileNotFoundError(f"no active workflow at {wf_dir}")
        # Stamp outcome before move so meta is consistent at rest.
        meta = self.read_meta(workflow_id)
        if meta is not None:
            stamped = replace(
                meta,
                completed_at_ms=int(time.time() * 1000),
                outcome=outcome,
            )
            self._write_meta_file(self.meta_path(workflow_id), stamped)
        timestamp = time.strftime("%Y%m%dT%H%M%S")
        target = self.root / _ARCHIVE_DIRNAME / f"{workflow_id}-{timestamp}"
        # Collision is theoretically possible within the same second; tack
        # a nanosecond suffix on if so.
        if target.exists():
            target = target.with_name(f"{target.name}-{time.time_ns()}")
        shutil.move(str(wf_dir), str(target))
        return target

    def gc_archive(self, *, retention_days: int) -> list[Path]:
        """Delete archived directories older than ``retention_days``.

        Mtime of the directory entry is used as the age signal — the
        archive process moves the directory atomically, so its mtime
        reflects when it was archived, not when it was first created.
        """
        archive_root = self.root / _ARCHIVE_DIRNAME
        if not archive_root.exists():
            return []
        cutoff = time.time() - retention_days * 24 * 3600
        removed: list[Path] = []
        for entry in archive_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed.append(entry)
        return removed

    # ---- queries --------------------------------------------------------

    def list_active(self) -> list[str]:
        if not self.root.exists():
            return []
        return [
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and entry.name != _ARCHIVE_DIRNAME
        ]

    def read_meta(self, workflow_id: str) -> WorkflowMeta | None:
        path = self.meta_path(workflow_id)
        if not path.exists():
            return None
        return WorkflowMeta.from_json_dict(json.loads(path.read_text(encoding="utf-8")))

    def update_meta(self, workflow_id: str, **fields: object) -> WorkflowMeta:
        """Read, replace specified fields, and write back atomically."""
        meta = self.read_meta(workflow_id)
        if meta is None:
            raise FileNotFoundError(
                f"no meta.json for workflow {workflow_id}"
            )
        # `replace` validates field names against the dataclass schema.
        updated = replace(meta, **fields)  # type: ignore[arg-type]
        self._write_meta_file(self.meta_path(workflow_id), updated)
        return updated

    # ---- handoff files --------------------------------------------------

    def write_handoff(self, workflow_id: str, *, stage: int, content: str) -> Path:
        path = self.handoff_path(workflow_id, stage=stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_handoff(self, workflow_id: str, *, stage: int) -> str | None:
        path = self.handoff_path(workflow_id, stage=stage)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    # ---- internals ------------------------------------------------------

    def _write_meta_file(self, path: Path, meta: WorkflowMeta) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(meta.to_json_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)


__all__ = [
    "WORKFLOW_DIR_RELPATH",
    "WorkflowFileStore",
    "WorkflowMeta",
    "workflow_handoff_relpath",
]
