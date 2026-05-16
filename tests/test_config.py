"""Unit tests for the settings/access-map parser."""

from __future__ import annotations

import pytest

from codex_rc.config import _normalise_event_log_mode, _parse_access, load_settings_from_env


def test_parse_access_empty_returns_empty_map() -> None:
    assert _parse_access("") == {}
    assert _parse_access("   ") == {}


def test_parse_access_legacy_csv_becomes_wildcard() -> None:
    assert _parse_access("42,99") == {"*": ("42", "99")}


def test_parse_access_legacy_csv_trims_and_drops_blanks() -> None:
    assert _parse_access("  42  ,  ,  99  ") == {"*": ("42", "99")}


def test_parse_access_json_object_with_wildcard_and_channels() -> None:
    raw = '{"*": ["42"], "100": ["99", "88"]}'
    assert _parse_access(raw) == {"*": ("42",), "100": ("99", "88")}


def test_parse_access_json_invalid_logs_and_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        result = _parse_access('{"*": "not-a-list"}')
    assert result == {}
    assert any("expected list" in r.message for r in caplog.records)


def test_parse_access_json_non_object_logs_and_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert _parse_access('["42"]') == {}
    assert any("expected JSON object" in r.message for r in caplog.records)


def test_parse_access_malformed_json_falls_through_to_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert _parse_access("{not valid") == {}
    assert any("invalid JSON" in r.message for r in caplog.records)


def test_parse_access_coerces_int_user_ids_to_str() -> None:
    raw = '{"*": [42, 99]}'
    assert _parse_access(raw) == {"*": ("42", "99")}


def test_load_settings_from_env_picks_up_access_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RC_ALLOWED_USER_IDS", '{"*": ["42"]}')
    s = load_settings_from_env()
    assert s.access_map == {"*": ("42",)}


def test_load_settings_defaults_log_format_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_RC_LOG_FORMAT", raising=False)
    assert load_settings_from_env().log_format == "text"


def test_load_settings_defaults_runtime_paths_and_event_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_RC_MEMORY_PATH", raising=False)
    monkeypatch.delenv("CODEX_RC_DB_PATH", raising=False)
    monkeypatch.delenv("CODEX_RC_LOG_ROOT", raising=False)
    monkeypatch.delenv("CODEX_RC_DEBUG_ROOT", raising=False)
    monkeypatch.delenv("CODEX_RC_EVENT_LOG_MODE", raising=False)
    monkeypatch.delenv("CODEX_RC_ERROR_LOG_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("CODEX_RC_THREAD_HISTORY_MAX", raising=False)
    s = load_settings_from_env()
    assert str(s.memory_path) == "data/state/memory.json"
    assert str(s.log_root) == "data/logs"
    assert str(s.debug_root) == "data/debug"
    assert s.event_log_mode == "errors"
    assert s.error_log_retention_days == 30
    assert s.thread_history_max == 100


def test_load_settings_respects_log_format_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RC_LOG_FORMAT", "json")
    assert load_settings_from_env().log_format == "json"


def test_load_settings_respects_retention_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RC_ERROR_LOG_RETENTION_DAYS", "14")
    monkeypatch.setenv("CODEX_RC_THREAD_HISTORY_MAX", "25")
    s = load_settings_from_env()
    assert s.error_log_retention_days == 14
    assert s.thread_history_max == 25


def test_load_settings_invalid_retention_limits_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CODEX_RC_ERROR_LOG_RETENTION_DAYS", "bad")
    monkeypatch.setenv("CODEX_RC_THREAD_HISTORY_MAX", "-1")
    with caplog.at_level("WARNING"):
        s = load_settings_from_env()
    assert s.error_log_retention_days == 30
    assert s.thread_history_max == 100
    assert any("CODEX_RC_ERROR_LOG_RETENTION_DAYS" in r.message for r in caplog.records)
    assert any("CODEX_RC_THREAD_HISTORY_MAX" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("errors", "errors"),
        ("error", "errors"),
        ("debug", "debug"),
        ("trace", "debug"),
        ("off", "off"),
        ("0", "off"),
        ("bad", "errors"),
    ],
)
def test_normalise_event_log_mode(raw: str, expected: str) -> None:
    assert _normalise_event_log_mode(raw) == expected
