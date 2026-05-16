"""JSON-RPC 2.0 client for the Codex `app-server` over child-process stdio.

Wire format (verified against codex-cli 0.130.0):

  * NDJSON: one JSON object per line, terminated by ``\\n``.
  * Messages without ``id`` are notifications.
  * Messages with ``id`` plus ``result`` or ``error`` are responses to our
    requests.
  * Messages with ``id`` plus ``method`` are server-initiated requests we must
    answer (used for approvals, file-search, MCP elicitation, etc.).

The handshake is a v1/shared method::

    --> {"jsonrpc":"2.0","id":1,"method":"initialize",
         "params":{"clientInfo":{"name":"...","version":"..."}}}
    <-- {"id":1,"result":{"userAgent":"...","codexHome":"...",
                          "platformFamily":"unix","platformOs":"macos"}}
    <-- {"method":"remoteControl/status/changed","params":{...}}

The server omits the ``jsonrpc`` field in responses; we accept either form.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

JsonObj = dict[str, Any]
ServerRequestHandler = Callable[[str, JsonObj, JsonObj], Awaitable[JsonObj | None]]
"""Signature: ``(method, params, raw_message) -> result | None``.

Return a dict to send as the JSON-RPC ``result``. Return ``None`` to leave the
server request unanswered (e.g., the caller will fulfill it later)."""


class RpcError(RuntimeError):
    """Raised when the server returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class TransportClosed(RuntimeError):
    """Raised when an operation is attempted after the transport shut down."""


# ====================================================================== transports

class Transport(Protocol):
    """Single-message NDJSON-ish duplex over arbitrary backends.

    Implementations: :class:`StdioTransport` (current default — codex
    app-server as a child process over stdin/stdout) and
    :class:`WebSocketTransport` (codex app-server listening on a WS port;
    lets ``codex --remote ws://…`` join the same backend).
    """

    async def start(self) -> None: ...
    async def stop(self, *, timeout: float = 3.0) -> None: ...  # noqa: ASYNC109
    async def send_message(self, payload: bytes) -> None: ...
    async def read_message(self) -> bytes | None: ...


class StdioTransport:
    """Spawn ``codex app-server`` (or any NDJSON peer) and talk over its
    stdin/stdout. The peer's stderr is forwarded to the logger at DEBUG."""

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        args: tuple[str, ...] = ("app-server",),
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.args = args
        self.cwd = cwd
        self.env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.codex_bin,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.fspath(self.cwd) if self.cwd else None,
            env=self.env,
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="codex-rpc-stderr"
        )

    async def stop(self, *, timeout: float = 3.0) -> None:  # noqa: ASYNC109
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.stdin and not proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        self._stderr_task = None

    async def send_message(self, payload: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(payload + b"\n")
        await self._proc.stdin.drain()

    async def read_message(self) -> bytes | None:
        assert self._proc is not None and self._proc.stdout is not None
        line = await self._proc.stdout.readline()
        return line if line else None

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while True:
            line = await stderr.readline()
            if not line:
                return
            logger.debug("codex stderr: %s", line.decode(errors="replace").rstrip())


class WebSocketTransport:
    """Talk to an external ``codex app-server`` already listening on a WS
    URL (e.g. ``ws://127.0.0.1:9876``). The server child process is the
    caller's responsibility — typically :class:`CodexServerProcess` in
    ws-mode spawns it before opening this transport."""

    def __init__(self, url: str, *, token: str | None = None) -> None:
        self.url = url
        self.token = token
        self._ws: Any = None  # websockets.WebSocketClientProtocol

    async def start(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover — covered by dep
            raise RuntimeError(
                "websockets package required for ws transport; "
                "pip install websockets"
            ) from exc
        headers: list[tuple[str, str]] = []
        if self.token:
            headers.append(("Authorization", f"Bearer {self.token}"))
        # additional_headers preserves compatibility with both legacy and 13+
        connect_kwargs: dict[str, Any] = {
            # websockets' 1 MiB default is too small for codex turn payloads —
            # a single item/completed with reasoning trace can exceed 1 MiB and
            # tear down the transport with CLOSE 1009. 16 MiB matches the
            # codex stdio path which has no framing limit in practice.
            "max_size": 16 * 1024 * 1024,
        }
        if headers:
            connect_kwargs["additional_headers"] = headers
        self._ws = await websockets.connect(self.url, **connect_kwargs)

    async def stop(self, *, timeout: float = 3.0) -> None:  # noqa: ASYNC109
        if self._ws is None:
            return
        ws, self._ws = self._ws, None
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), timeout=timeout)

    async def send_message(self, payload: bytes) -> None:
        if self._ws is None:
            raise TransportClosed("websocket not connected")
        # websockets accepts both str and bytes; text frames keep wire
        # format consistent with stdio NDJSON.
        await self._ws.send(payload.decode("utf-8"))

    async def read_message(self) -> bytes | None:
        if self._ws is None:
            return None
        try:
            msg = await self._ws.recv()
        except Exception:  # noqa: BLE001 — websockets.ConnectionClosed*
            return None
        return msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[JsonObj]
    method: str


@dataclass(slots=True)
class ClientInfo:
    name: str = "codex_rc"
    version: str = "0.2.0"
    title: str | None = None

    def to_params(self) -> JsonObj:
        info: JsonObj = {"name": self.name, "version": self.version}
        if self.title is not None:
            info["title"] = self.title
        return {"clientInfo": info, "capabilities": {"experimentalApi": True}}


@dataclass
class CodexRpcClient:
    """Async client for ``codex app-server`` over stdio.

    Lifecycle:

        client = CodexRpcClient(...)
        await client.start()                  # spawns + initialize handshake
        async for notif in client.notifications():
            ...
        result = await client.request("threads/start", {...})
        await client.stop()
    """

    transport: Transport | None = None
    # Stdio-mode shortcuts (used when ``transport`` is None — keeps backwards
    # compat with every test and caller from Day 1-7).
    codex_bin: str = "codex"
    args: tuple[str, ...] = ("app-server",)
    cwd: str | os.PathLike[str] | None = None
    env: dict[str, str] | None = None
    client_info: ClientInfo = field(default_factory=ClientInfo)
    server_request_handler: ServerRequestHandler | None = None

    _started: bool = field(default=False, init=False)
    _writer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _id_iter: itertools.count[int] = field(
        default_factory=lambda: itertools.count(1), init=False
    )
    _pending: dict[int, _Pending] = field(default_factory=dict, init=False)
    _notification_subscribers: list[asyncio.Queue[JsonObj]] = field(
        default_factory=list, init=False
    )
    _notification_backlog: list[JsonObj] = field(default_factory=list, init=False)
    _notification_backlog_max: int = 256
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False)
    _closed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _initialize_result: JsonObj | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = StdioTransport(
                codex_bin=self.codex_bin,
                args=self.args,
                cwd=self.cwd,
                env=self.env,
            )

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> JsonObj:
        """Open the transport and perform the ``initialize`` handshake."""
        if self._started:
            raise RuntimeError("CodexRpcClient already started")
        assert self.transport is not None
        await self.transport.start()
        self._started = True

        self._reader_task = asyncio.create_task(
            self._read_loop(), name="codex-rpc-reader"
        )

        try:
            self._initialize_result = await self.request(
                "initialize",
                self.client_info.to_params(),
                timeout=10.0,
            )
        except Exception:
            await self.stop()
            raise
        return self._initialize_result

    async def stop(self, timeout: float = 3.0) -> None:  # noqa: ASYNC109 — public API ergonomic
        """Close the transport and drain background tasks."""
        if not self._started:
            return
        self._started = False

        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(TransportClosed("client stopped"))
        self._pending.clear()

        assert self.transport is not None
        with contextlib.suppress(Exception):
            await self.transport.stop(timeout=timeout)

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._reader_task = None
        self._closed.set()

    async def __aenter__(self) -> CodexRpcClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    @property
    def initialize_result(self) -> JsonObj | None:
        return self._initialize_result

    # --- public API --------------------------------------------------------

    async def request(
        self,
        method: str,
        params: JsonObj | None = None,
        *,
        timeout: float | None = 60.0,  # noqa: ASYNC109 — public API ergonomic
    ) -> JsonObj:
        """Send a JSON-RPC request and await the response.

        Raises :class:`RpcError` if the server returns an error response, or
        :class:`TransportClosed` if the transport drops before a response
        arrives.
        """
        if not self._started:
            raise TransportClosed("client not started")

        request_id = next(self._id_iter)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[JsonObj] = loop.create_future()
        self._pending[request_id] = _Pending(future=fut, method=method)

        payload: JsonObj = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        try:
            await self._send(payload)
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: JsonObj | None = None) -> None:
        if not self._started:
            raise TransportClosed("client not started")
        payload: JsonObj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def respond_to_request(
        self,
        request_id: int,
        *,
        result: JsonObj | None = None,
        error: JsonObj | None = None,
    ) -> None:
        """Send a deferred response to a server-initiated request.

        Use this from a ``server_request_handler`` that returned ``None`` to
        defer (typical for human-in-the-loop approval flows). Exactly one of
        ``result`` / ``error`` must be supplied.
        """
        if not self._started:
            raise TransportClosed("client not started")
        if (result is None) == (error is None):
            raise ValueError("Exactly one of result/error must be provided")
        payload: JsonObj = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        await self._send(payload)

    async def notifications(self) -> AsyncIterator[JsonObj]:
        """Async stream of notifications from the server.

        Each call returns an independent subscriber stream — every
        notification is broadcast to every active subscriber. This keeps
        multiple consumers (e.g. server_process logger + ChannelService
        router) from racing for the same queue.

        Stops cleanly when the transport closes.
        """
        queue: asyncio.Queue[JsonObj] = asyncio.Queue()
        # Replay the backlog so subscribers that register after early
        # notifications (e.g. server's first ``remoteControl/status/changed``
        # or post-initialize bursts) still see them.
        for past in self._notification_backlog:
            queue.put_nowait(past)
        self._notification_subscribers.append(queue)
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                closer = asyncio.create_task(self._closed.wait())
                done, pending = await asyncio.wait(
                    {getter, closer}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if getter in done:
                    yield getter.result()
                else:
                    return
        finally:
            with contextlib.suppress(ValueError):
                self._notification_subscribers.remove(queue)

    # --- internals ---------------------------------------------------------

    async def _send(self, payload: JsonObj) -> None:
        assert self.transport is not None
        line = json.dumps(payload, separators=(",", ":")).encode()
        async with self._writer_lock:
            await self.transport.send_message(line)

    async def _read_loop(self) -> None:
        assert self.transport is not None
        try:
            while True:
                line = await self.transport.read_message()
                if line is None:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("codex_rc: malformed JSON from server: %r", line)
                    continue
                if not isinstance(msg, dict):
                    logger.warning("codex_rc: non-object frame from server: %r", msg)
                    continue
                await self._dispatch(msg)
        finally:
            self._closed.set()
            for pending in list(self._pending.values()):
                if not pending.future.done():
                    pending.future.set_exception(TransportClosed("server EOF"))
            self._pending.clear()

    async def _dispatch(self, msg: JsonObj) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")

        if msg_id is not None and method is None:
            # Response or error to one of our requests.
            pending = self._pending.pop(int(msg_id), None)
            if pending is None:
                logger.warning("codex_rc: response for unknown id=%s", msg_id)
                return
            if "error" in msg:
                err = msg["error"] or {}
                pending.future.set_exception(
                    RpcError(
                        code=int(err.get("code", -32000)),
                        message=str(err.get("message", "unknown error")),
                        data=err.get("data"),
                    )
                )
            else:
                pending.future.set_result(msg.get("result") or {})
            return

        if msg_id is not None and method is not None:
            # Server-initiated request.
            await self._handle_server_request(int(msg_id), str(method), msg)
            return

        if method is not None:
            # Remember in backlog for late subscribers, cap to bound memory.
            self._notification_backlog.append(msg)
            if len(self._notification_backlog) > self._notification_backlog_max:
                self._notification_backlog = self._notification_backlog[
                    -self._notification_backlog_max:
                ]
            # Broadcast: every active subscriber gets its own copy.
            for sub in list(self._notification_subscribers):
                await sub.put(msg)
            return

        logger.warning("codex_rc: unrecognized frame: %r", msg)

    async def _handle_server_request(
        self, request_id: int, method: str, raw: JsonObj
    ) -> None:
        params = raw.get("params") or {}
        result: JsonObj | None = None
        error: JsonObj | None = None
        if self.server_request_handler is None:
            error = {"code": -32601, "message": f"Method not found: {method}"}
        else:
            try:
                result = await self.server_request_handler(method, params, raw)
            except Exception as exc:  # noqa: BLE001 — must surface to peer
                logger.exception(
                    "codex_rc: server-request handler crashed (method=%s)", method
                )
                error = {"code": -32603, "message": f"Internal error: {exc}"}
                result = None
        if result is None and error is None:
            return  # caller will respond out-of-band
        reply: JsonObj = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        await self._send(reply)

    # _stderr_loop moved to StdioTransport.
