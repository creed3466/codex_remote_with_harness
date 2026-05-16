"""JSON log formatter for codex_rc.

Selected via ``CODEX_RC_LOG_FORMAT=json``. Designed for production
log shippers (Vector, Loki, CloudWatch) that prefer one JSON object per
line. The default text formatter is more readable for local development.
"""

from __future__ import annotations

import json
import logging
import time


class JsonLogFormatter(logging.Formatter):
    """Render a LogRecord as one compact JSON object per line.

    Fields:
        ts (str)       — ISO-8601 UTC timestamp with millisecond precision
        level (str)    — log level name (e.g. "INFO")
        logger (str)   — logger name
        message (str)  — fully-resolved formatted message
        func (str)    — function name (when ``include_funcname`` is True)
        line (int)    — source line number
        exc (str)     — exception traceback (only on errors)
    """

    def __init__(self, *, include_funcname: bool = True) -> None:
        super().__init__()
        self.include_funcname = include_funcname

    def format(self, record: logging.LogRecord) -> str:
        # Build ISO-8601 UTC timestamp with ms precision.
        ts_secs = record.created
        ms = int((ts_secs - int(ts_secs)) * 1000)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_secs))
        ts = f"{ts}.{ms:03d}Z"

        payload: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.include_funcname:
            payload["func"] = record.funcName
            payload["line"] = record.lineno
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = ["JsonLogFormatter"]
