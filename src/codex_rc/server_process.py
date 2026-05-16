"""Project-scoped lifecycle wrapper around :class:`CodexRpcClient`.

A :class:`CodexServerProcess` binds one Codex session to one project
directory and records local event files according to the configured policy:

* debug mode: ``events.jsonl`` + ``events.txt`` — append-only full RPC trace.
* errors mode: ``logs/errors/<UTC date>.jsonl`` — redacted serious failures
  only.

Day 3 only handles the lifecycle + logging. Higher-level routing
(``notif_router``, ``approval_router``) consumes the same events on a
later day.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import logging
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rpc_client import (
    ClientInfo,
    CodexRpcClient,
    JsonObj,
    RpcError,
    ServerRequestHandler,
)

ChildExitHandler = Callable[[int | None], Awaitable[None]]
"""Async callback fired when the codex app-server child exits unexpectedly."""

logger = logging.getLogger(__name__)


def _maybe_path_hint(path: Path) -> str:
    """Surface a friendlier suggestion when macOS firmlinks bit the operator.

    Path("/home/alice/x").resolve() on macOS lands at
    /System/Volumes/Data/home/alice/x because /home is an automount.
    The directory the user actually meant lives at /Users/alice/x.
    """
    if sys.platform != "darwin":
        return ""
    s = str(path)
    prefix = "/System/Volumes/Data/home/"
    if not s.startswith(prefix):
        return ""
    tail = s[len(prefix):]
    if not tail:
        return ""
    suggested = Path("/Users") / tail
    if suggested.is_dir():
        return f". Did you mean `{suggested}`? (macOS doesn't use /home)"
    return ". On macOS try `/Users/<user>/...` or `~/...`."


def _reserve_free_port(host: str) -> int:
    """Ask the OS for a free TCP port on ``host``. Closed before returning
    so the codex child can rebind it almost immediately."""
    s = socket.socket()
    try:
        s.bind((host, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()

MAX_PRETTY_PARAM_LEN = 160
_EVENT_LOG_MODES = {"errors", "debug", "off"}
_ERROR_SYSTEM_EVENTS = {"start_failed", "child_crashed"}
_SECONDS_PER_DAY = 24 * 60 * 60


def _normalise_event_log_mode(raw: str) -> str:
    mode = raw.strip().lower() or "errors"
    if mode in _EVENT_LOG_MODES:
        return mode
    logger.warning(
        "codex_rc: invalid event_log_mode=%r; using errors", raw
    )
    return "errors"


def _ts() -> str:
    t = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d}"


def _summarize(obj: JsonObj | None) -> str:
    if not obj:
        return ""
    compact = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if len(compact) <= MAX_PRETTY_PARAM_LEN:
        return compact
    return compact[: MAX_PRETTY_PARAM_LEN - 1] + "…"


def _short(value: Any, *, limit: int = 1000) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _is_error_event(kind: str, payload: JsonObj) -> bool:
    if kind == "resp_error":
        return True
    if kind == "system" and payload.get("event") in _ERROR_SYSTEM_EVENTS:
        return True
    return False


def _redact_error_record(record: JsonObj) -> JsonObj:
    """Keep enough context to debug serious failures without storing prompts,
    diffs, command output, injected system text, or model responses."""
    allowed = (
        "t",
        "kind",
        "method",
        "code",
        "message",
        "event",
        "error",
        "returncode",
    )
    return {k: _short(record[k]) for k in allowed if k in record}


def _error_log_timestamp(path: Path) -> float:
    """Return a comparable timestamp for an error log.

    Daily codex_rc error logs are named ``YYYY-MM-DD.jsonl`` in UTC. Fall back
    to mtime for custom filenames so externally configured paths still prune.
    """
    try:
        return float(calendar.timegm(time.strptime(path.stem, "%Y-%m-%d")))
    except ValueError:
        return path.stat().st_mtime


def _prune_error_logs(
    current_log_path: Path,
    retention_days: int,
    *,
    now: float | None = None,
) -> None:
    """Delete old operational error logs next to ``current_log_path``.

    This deliberately only considers ``*.jsonl`` in the operational error-log
    directory. Debug RPC traces live under ``debug_root`` and are not touched.
    ``retention_days <= 0`` disables pruning.
    """
    if retention_days <= 0:
        return
    root = current_log_path.parent
    if not root.is_dir():
        return
    cutoff = (time.time() if now is None else now) - retention_days * _SECONDS_PER_DAY
    current = current_log_path.expanduser().resolve()
    for path in root.glob("*.jsonl"):
        try:
            if path.expanduser().resolve() == current:
                continue
            if _error_log_timestamp(path) >= cutoff:
                continue
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("codex_rc: failed to prune old error log %s", path)


@dataclass
class CodexServerProcess:
    """Run a Codex ``app-server`` bound to ``project_path`` and record events
    according to ``event_log_mode``.

    ``transport_mode``:

    * ``"stdio"`` (default) — spawn ``codex app-server`` as a child via stdio.
      Single consumer; no second tool can attach.
    * ``"ws"`` — spawn ``codex app-server --listen ws://127.0.0.1:<port>`` and
      connect via WebSocket. ``ws_port`` defaults to ``0`` (auto-allocate);
      after :meth:`start` the bound port is stored on :attr:`ws_port` and the
      URL is available via :attr:`ws_url`. A second client (``codex --remote
      ws://…`` in a tmux pane, or any other tool) can join the same backend.
    """

    project_path: Path
    log_dir: Path
    client_info: ClientInfo = field(default_factory=ClientInfo)
    codex_bin: str = "codex"
    codex_args: tuple[str, ...] = ("app-server",)
    env: dict[str, str] | None = None
    transport_mode: str = "stdio"
    ws_port: int = 0
    ws_host: str = "127.0.0.1"
    event_log_mode: str = "errors"
    error_log_path: Path | None = None
    error_log_retention_days: int = 30

    client: CodexRpcClient = field(init=False)
    _notif_task: asyncio.Task[None] | None = field(default=None, init=False)
    _jsonl_fh: Any = field(default=None, init=False)
    _pretty_fh: Any = field(default=None, init=False)
    _error_fh: Any = field(default=None, init=False)
    _log_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _ws_child: asyncio.subprocess.Process | None = field(default=None, init=False)
    _ws_stderr_task: asyncio.Task[None] | None = field(default=None, init=False)
    _child_watch_task: asyncio.Task[None] | None = field(default=None, init=False)
    _on_child_exit: ChildExitHandler | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ paths

    def __post_init__(self) -> None:
        self.event_log_mode = _normalise_event_log_mode(self.event_log_mode)
        self.error_log_retention_days = max(0, int(self.error_log_retention_days))

    @property
    def jsonl_path(self) -> Path:
        return self.log_dir / "events.jsonl"

    @property
    def pretty_path(self) -> Path:
        return self.log_dir / "events.txt"

    @property
    def effective_error_log_path(self) -> Path:
        return self.error_log_path or (self.log_dir / "errors.jsonl")

    @property
    def tail_path(self) -> Path | None:
        if self.event_log_mode == "debug":
            return self.pretty_path
        return None

    @property
    def ws_url(self) -> str | None:
        if self.transport_mode != "ws" or not self.ws_port:
            return None
        return f"ws://{self.ws_host}:{self.ws_port}"

    # ---------------------------------------------------------------- public

    async def start(self) -> JsonObj:
        self.project_path = self.project_path.expanduser().resolve()
        if not self.project_path.is_dir():
            hint = _maybe_path_hint(self.project_path)
            raise FileNotFoundError(
                f"project path not a directory: {self.project_path}{hint}"
            )
        if self.event_log_mode == "errors":
            _prune_error_logs(
                self.effective_error_log_path,
                self.error_log_retention_days,
            )
        if self.event_log_mode == "debug":
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # buffering=1 → line-buffered, important so tail -f sees writes promptly.
            self._jsonl_fh = self.jsonl_path.open("a", buffering=1, encoding="utf-8")
            self._pretty_fh = self.pretty_path.open("a", buffering=1, encoding="utf-8")

        if self.transport_mode == "ws":
            await self._start_ws_child()
            from .rpc_client import WebSocketTransport

            transport = WebSocketTransport(self.ws_url or "")
            self.client = CodexRpcClient(
                transport=transport,
                client_info=self.client_info,
            )
        else:
            self.client = CodexRpcClient(
                codex_bin=self.codex_bin,
                args=self.codex_args,
                cwd=self.project_path,
                env=self.env,
                client_info=self.client_info,
            )
        await self._log_event(
            "system",
            {
                "event": "starting",
                "cwd": str(self.project_path),
                "transport": self.transport_mode,
                "ws_url": self.ws_url,
            },
        )
        try:
            init_result = await self.client.start()
        except Exception as exc:
            await self._log_event("system", {"event": "start_failed", "error": str(exc)})
            await self._stop_ws_child()
            await self._close_logs()
            raise

        await self._log_event(
            "system",
            {"event": "initialized", "result": init_result},
        )
        self._notif_task = asyncio.create_task(
            self._consume_notifications(), name="codex-rc-notif-logger"
        )
        return init_result

    async def stop(self) -> None:
        self._stopping = True
        if self._notif_task and not self._notif_task.done():
            self._notif_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._notif_task
        self._notif_task = None
        if hasattr(self, "client"):
            await self.client.stop()
        await self._stop_ws_child()
        await self._log_event("system", {"event": "stopped"})
        await self._close_logs()

    def set_on_child_exit(self, cb: ChildExitHandler | None) -> None:
        """Register a callback invoked when the codex app-server child exits
        unexpectedly (i.e. before :meth:`stop` was called). Only fires in ``ws``
        mode where this class owns the child."""
        self._on_child_exit = cb

    async def __aenter__(self) -> CodexServerProcess:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # WS child lifecycle ---------------------------------------------------

    async def _start_ws_child(self) -> None:
        """Spawn ``codex app-server --listen ws://host:port`` as a child.

        If ``self.ws_port`` is 0, an OS-allocated free port is reserved
        before the child boots and re-used on the command line so the URL
        is deterministic. Health-check via ``/readyz`` HTTP endpoint that
        codex exposes alongside the WS listener.
        """
        if self.ws_port == 0:
            self.ws_port = _reserve_free_port(self.ws_host)
        listen = f"ws://{self.ws_host}:{self.ws_port}"
        args = (*self.codex_args, "--listen", listen)
        logger.info("codex_rc: spawning ws child %s %s", self.codex_bin, " ".join(args))
        self._ws_child = await asyncio.create_subprocess_exec(
            self.codex_bin,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.fspath(self.project_path),
            env=self.env,
        )
        assert self._ws_child.stderr is not None
        self._ws_stderr_task = asyncio.create_task(
            self._ws_stderr_loop(self._ws_child.stderr),
            name="codex-rc-ws-stderr",
        )
        self._child_watch_task = asyncio.create_task(
            self._watch_child(self._ws_child),
            name="codex-rc-child-watch",
        )
        await self._await_ws_ready()

    async def _await_ws_ready(self, *, timeout_s: float = 10.0) -> None:
        """Poll the child's TCP socket until the WS listener accepts."""
        import socket as _socket

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._ws_child is None or self._ws_child.returncode is not None:
                raise RuntimeError("codex app-server child exited before ready")
            with contextlib.suppress(OSError):
                s = _socket.socket()
                s.settimeout(0.5)
                s.connect((self.ws_host, self.ws_port))
                s.close()
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"codex ws listener {self.ws_host}:{self.ws_port} not ready after {timeout_s}s"
        )

    async def _stop_ws_child(self) -> None:
        if self._ws_child is None:
            return
        child, self._ws_child = self._ws_child, None
        if child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                child.terminate()
            try:
                await asyncio.wait_for(child.wait(), timeout=3.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                await child.wait()
        if self._ws_stderr_task and not self._ws_stderr_task.done():
            self._ws_stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_stderr_task
        self._ws_stderr_task = None
        if self._child_watch_task and not self._child_watch_task.done():
            self._child_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._child_watch_task
        self._child_watch_task = None

    async def _watch_child(self, child: asyncio.subprocess.Process) -> None:
        """Wait for the codex child to exit; if it dies before :meth:`stop`,
        log a system event and fire :attr:`_on_child_exit`. Cancellation is
        the normal teardown path and is swallowed here so :meth:`_stop_ws_child`
        can wait on us cleanly.
        """
        try:
            rc = await child.wait()
        except asyncio.CancelledError:
            raise
        if self._stopping or self._ws_child is None or self._ws_child is not child:
            # Planned shutdown raced ahead — not a crash.
            return
        await self._log_event(
            "system",
            {"event": "child_crashed", "returncode": rc},
        )
        cb = self._on_child_exit
        if cb is not None:
            try:
                await cb(rc)
            except Exception:
                logger.exception("codex_rc: on_child_exit callback failed")

    async def _ws_stderr_loop(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            logger.debug("codex ws child stderr: %s", line.decode(errors="replace").rstrip())

    # Pass-through with logging --------------------------------------------

    async def request(
        self,
        method: str,
        params: JsonObj | None = None,
        *,
        timeout: float | None = 60.0,  # noqa: ASYNC109 — public API ergonomic
    ) -> JsonObj:
        await self._log_event("req", {"method": method, "params": params})
        try:
            result = await self.client.request(method, params, timeout=timeout)
        except RpcError as exc:
            await self._log_event(
                "resp_error",
                {"method": method, "code": exc.code, "message": exc.message},
            )
            raise
        await self._log_event("resp", {"method": method, "result": result})
        return result

    async def notify(self, method: str, params: JsonObj | None = None) -> None:
        await self._log_event("notify_out", {"method": method, "params": params})
        await self.client.notify(method, params)

    def set_server_request_handler(self, handler: ServerRequestHandler | None) -> None:
        """Forwarder for clarity; sets the underlying client's handler.

        Day 5 (approval_router) will wrap this with logging + Discord dispatch.
        """
        self.client.server_request_handler = handler

    # --------------------------------------------------------------- internals

    async def _consume_notifications(self) -> None:
        async for notif in self.client.notifications():
            await self._log_event("notif", notif)

    async def _log_event(self, kind: str, payload: JsonObj) -> None:
        record: JsonObj = {
            "t": time.time(),
            "kind": kind,
            **payload,
        }
        if self.event_log_mode == "off":
            return
        if self.event_log_mode == "errors":
            if not _is_error_event(kind, payload):
                return
            async with self._log_lock:
                if self._error_fh is None:
                    path = self.effective_error_log_path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._error_fh = path.open("a", buffering=1, encoding="utf-8")
                line_json = (
                    json.dumps(
                        _redact_error_record(record),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                self._error_fh.write(line_json)
            return

        method = payload.get("method")
        if kind == "notif" and method:
            pretty = f"[{_ts()}] notif   {method:<40s} {_summarize(payload.get('params'))}"
        elif kind == "req" and method:
            pretty = f"[{_ts()}] req →   {method:<40s} {_summarize(payload.get('params'))}"
        elif kind == "resp" and method:
            pretty = f"[{_ts()}] ← resp {method:<40s} {_summarize(payload.get('result'))}"
        elif kind == "resp_error" and method:
            code = payload.get("code")
            msg = payload.get("message")
            pretty = f"[{_ts()}] ← err  {method:<40s} [{code}] {msg}"
        elif kind == "notify_out" and method:
            pretty = f"[{_ts()}] note →  {method:<40s} {_summarize(payload.get('params'))}"
        elif kind == "system":
            event = payload.get("event", "?")
            extra = _summarize({k: v for k, v in payload.items() if k != "event"})
            pretty = f"[{_ts()}] system  {event:<40s} {extra}"
        else:
            pretty = f"[{_ts()}] {kind:<7s} {_summarize(payload)}"
        pretty_line = pretty.rstrip() + "\n"

        async with self._log_lock:
            if self.event_log_mode == "debug":
                line_json = (
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                if self._jsonl_fh is not None:
                    self._jsonl_fh.write(line_json)
                if self._pretty_fh is not None:
                    self._pretty_fh.write(pretty_line)
                return

    async def _close_logs(self) -> None:
        async with self._log_lock:
            for fh in (self._jsonl_fh, self._pretty_fh, self._error_fh):
                if fh is not None:
                    with contextlib.suppress(Exception):
                        fh.close()
            self._jsonl_fh = None
            self._pretty_fh = None
            self._error_fh = None


def default_log_dir(base: Path, project_name: str) -> Path:
    """Return ``base/rpc/<project>/<UTC date>`` for debug RPC traces."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return base / "rpc" / project_name / day


def default_error_log_path(base: Path) -> Path:
    """Return ``base/errors/<UTC date>.jsonl`` for operational error logs."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return base / "errors" / f"{day}.jsonl"


__all__ = [
    "CodexServerProcess",
    "default_error_log_path",
    "default_log_dir",
]


# Re-export OS-specific helpers if needed downstream; keep PathLike alias clear.
_PathLike = str | os.PathLike[str]
