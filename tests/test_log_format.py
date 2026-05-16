"""Unit tests for the JSON log formatter."""

from __future__ import annotations

import json
import logging

import pytest

from codex_rc.log_format import JsonLogFormatter


def _make_record(
    *,
    name: str = "codex_rc.test",
    level: int = logging.INFO,
    msg: str = "hello %s",
    args: tuple = ("world",),
    exc_info: tuple | None = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="/tmp/test.py",
        lineno=42,
        msg=msg,
        args=args,
        exc_info=exc_info,
        func="test_fn",
    )


def test_json_formatter_emits_required_fields() -> None:
    fmt = JsonLogFormatter()
    record = _make_record()
    line = fmt.format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "codex_rc.test"
    assert payload["message"] == "hello world"
    assert payload["func"] == "test_fn"
    assert payload["line"] == 42
    # ISO-8601-ish UTC timestamp.
    assert payload["ts"].endswith("Z")
    assert "T" in payload["ts"]


def test_json_formatter_can_omit_funcname() -> None:
    fmt = JsonLogFormatter(include_funcname=False)
    payload = json.loads(fmt.format(_make_record()))
    assert "func" not in payload
    assert "line" not in payload


def test_json_formatter_one_line_no_newlines() -> None:
    fmt = JsonLogFormatter()
    record = _make_record(msg="multi\nline", args=())
    line = fmt.format(record)
    assert "\n" not in line  # JSON encodes \n as \\n


def test_json_formatter_renders_exception() -> None:
    fmt = JsonLogFormatter()
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        import sys

        record = _make_record(exc_info=sys.exc_info())
    payload = json.loads(fmt.format(record))
    assert "exc" in payload
    assert "RuntimeError" in payload["exc"]
    assert "kaboom" in payload["exc"]


def test_json_formatter_handles_unicode() -> None:
    fmt = JsonLogFormatter()
    record = _make_record(msg="🚀 %s", args=("ok",))
    payload = json.loads(fmt.format(record))
    assert payload["message"] == "🚀 ok"


@pytest.mark.parametrize("level", [logging.DEBUG, logging.ERROR, logging.CRITICAL])
def test_json_formatter_records_level_name(level: int) -> None:
    fmt = JsonLogFormatter()
    payload = json.loads(fmt.format(_make_record(level=level)))
    assert payload["level"] == logging.getLevelName(level)
