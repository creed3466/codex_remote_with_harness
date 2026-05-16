"""Unit tests for NotifRouter — synthetic V2 notifications in, Discord
payloads out."""

from __future__ import annotations

from codex_rc.notif_router import (
    COLOR_ERROR,
    COLOR_WARN,
    FLUSH_AT,
    DiscordEmbed,
    DiscordPayload,
    NotifRouter,
    chunk_text,
    compact_outbound_text,
)

# ---------------------------------------------------------------- helpers

def _make_delta_notif(
    delta: str,
    *,
    method: str = "item/agentMessage/delta",
    thread_id: str = "t1",
    turn_id: str = "u1",
    item_id: str = "i1",
) -> dict:
    return {
        "method": method,
        "params": {
            "delta": delta,
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": item_id,
        },
    }


# ---------------------------------------------------------------- chunk_text

def test_chunk_text_below_limit_is_one_piece() -> None:
    assert chunk_text("hello") == ["hello"]


def test_chunk_text_prefers_double_newline() -> None:
    a = "x" * (FLUSH_AT - 10)
    b = "y" * 200
    pieces = chunk_text(a + "\n\n" + b)
    assert len(pieces) == 2
    assert pieces[0].endswith("\n\n")
    assert pieces[1].startswith("y")


def test_chunk_text_falls_back_to_hard_cut_when_no_breaks() -> None:
    text = "x" * (FLUSH_AT + 500)
    pieces = chunk_text(text)
    assert sum(len(p) for p in pieces) == len(text)
    assert all(len(p) <= FLUSH_AT for p in pieces)


def test_chunk_text_empty_input() -> None:
    assert chunk_text("") == []
    assert chunk_text(None) == []  # type: ignore[arg-type]


# ----------------------------------------------------------- outbound compact

def test_compact_outbound_text_short_answer_passes_through() -> None:
    text = "짧은 답변은 그대로 전달한다."
    assert compact_outbound_text(text) == text


def test_compact_outbound_text_long_fenced_read_uses_preview() -> None:
    text = (
        "Here is file:\n```python\n"
        + "\n".join(f"line {i}" for i in range(100))
        + "\n```\nDone"
    )
    compacted = compact_outbound_text(text)
    assert "line 0" in compacted
    assert "Read output omitted" in compacted
    assert "line 99" not in compacted
    assert compacted.endswith("Done")


# ----------------------------------------------------------- agent streaming

def test_agent_delta_below_flush_buffers_silently() -> None:
    r = NotifRouter()
    out = r.route(_make_delta_notif("hello "))
    assert out == []
    out2 = r.route(_make_delta_notif("world"))
    assert out2 == []


def test_agent_delta_flushes_at_threshold_when_streaming_enabled() -> None:
    r = NotifRouter(stream_deltas=True)
    chunk = "x" * 600
    assert r.route(_make_delta_notif(chunk)) == []  # 600
    assert r.route(_make_delta_notif(chunk)) == []  # 1200
    flushed = r.route(_make_delta_notif(chunk))  # 1800 → over threshold
    assert flushed, "expected a flushed payload at threshold"
    assert flushed[0].content
    assert len(flushed[0].content) <= FLUSH_AT


def test_agent_delta_default_suppresses_streaming() -> None:
    # Default (stream_deltas=False): deltas accumulate silently; nothing is
    # emitted to Discord until item/completed lands the authoritative text.
    r = NotifRouter()
    for _ in range(5):
        assert r.route(_make_delta_notif("x" * 600)) == []
    # Now item/completed lands with clean reconstructed text:
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {"id": "i1", "type": "agentMessage", "text": "Done."},
            },
        }
    )
    assert out and out[0].content == "Done."


def test_item_completed_uses_authoritative_text_discarding_delta_buffer() -> None:
    r = NotifRouter()
    r.route(_make_delta_notif("c"))  # buffer something garbled
    r.route(_make_delta_notif("c"))
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "i1",
                    "type": "agentMessage",
                    "text": "Hello, clean output!",
                },
            },
        }
    )
    assert out and out[0].content == "Hello, clean output!"
    # The buffered (garbled) deltas were discarded, not concatenated.
    assert "cc" not in (out[0].content or "")


def test_item_completed_long_fenced_read_is_compacted_before_chunking() -> None:
    r = NotifRouter()
    text = (
        "Here is file:\n```python\n"
        + "\n".join(f"line {i}" for i in range(100))
        + "\n```\nDone"
    )
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {"id": "i1", "type": "agentMessage", "text": text},
            },
        }
    )
    assert len(out) == 1
    content = out[0].content or ""
    assert "line 0" in content
    assert "Read output omitted" in content
    assert "line 99" not in content


def test_item_completed_commandExecution_success_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "/bin/zsh -lc ls",
                    "aggregatedOutput": "README.md\ndata\ndocs\n",
                },
            },
        }
    )
    assert out == []


def test_item_completed_commandExecution_verbose_success_is_hidden() -> None:
    r = NotifRouter(verbose=True)
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "/bin/zsh -lc ls",
                    "aggregatedOutput": "README.md\ndata\ndocs\n",
                },
            },
        }
    )
    assert out == []


def test_command_execution_build_failure_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "make build",
                    "exitCode": 1,
                    "aggregatedOutput": "FAILED test_x\nAssertionError: nope\n",
                },
            },
        }
    )
    assert out == []


def test_command_execution_read_failure_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "sed -n 1,20p missing.py",
                    "exitCode": 2,
                    "aggregatedOutput": "sed: missing.py: No such file or directory\n",
                },
            },
        }
    )

    assert out == []


def test_command_execution_exec_failure_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "python scripts/check.py",
                    "exitCode": 1,
                    "aggregatedOutput": "Traceback...\nRuntimeError: nope\n",
                },
            },
        }
    )

    assert out == []


def test_command_execution_test_failure_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "pytest -q tests/test_large.py",
                    "exitCode": 1,
                    "aggregatedOutput": "FAILED test_x\nAssertionError: nope\n",
                },
            },
        }
    )

    assert out == []


def test_command_execution_build_failure_compacts_long_output() -> None:
    r = NotifRouter()
    output = "\n".join(f"line {i}" for i in range(80))
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "make build",
                    "exitCode": 1,
                    "aggregatedOutput": output,
                },
            },
        }
    )

    assert out == []


def test_command_execution_exit_127_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "custom-tool run",
                    "exitCode": 127,
                    "aggregatedOutput": "custom-tool: command not found\n",
                },
            },
        }
    )

    assert out == []


def test_command_execution_exit_128_is_hidden() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {
                    "id": "e1",
                    "type": "commandExecution",
                    "command": "git status",
                    "exitCode": 128,
                    "aggregatedOutput": "fatal: not a git repository (or any of the parent directories): .git\n",
                },
            },
        }
    )

    assert out == []


def test_item_completed_flushes_pending_buffer() -> None:
    r = NotifRouter()
    r.route(_make_delta_notif("partial answer."))
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {"id": "i1", "type": "agentMessage"},
            },
        }
    )
    assert out, "item/completed should flush"
    assert "partial answer." in (out[0].content or "")


def test_turn_completed_flushes_all_streams_and_appends_marker() -> None:
    r = NotifRouter()
    r.route(_make_delta_notif("alpha "))
    r.route(_make_delta_notif("beta", method="item/reasoning/summaryTextDelta",
                              item_id="i2"))
    out = r.route(
        {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "u1"}},
        }
    )
    # v0.2+: turn/completed flushes any pending stream payloads but does
    # NOT emit a standalone "✅ Turn complete" embed any more — completion
    # is signalled by the ✅ reaction the bot attaches to the user message.
    assert len(out) >= 1
    contents = [p.content for p in out if p.content]
    assert any("alpha" in (c or "") for c in contents)
    assert any("beta" in (c or "") for c in contents)
    assert all(p.embed is None or p.embed.title != "✅ Turn complete" for p in out)


def test_turn_completed_compacts_pending_large_liney_buffer() -> None:
    r = NotifRouter()
    r.route(_make_delta_notif("\n".join(f"read line {i}" for i in range(100))))
    out = r.route(
        {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "u1"}},
        }
    )
    assert len(out) == 1
    content = out[0].content or ""
    assert "read line 0" in content
    assert "Long read/log output omitted" in content
    assert "read line 99" not in content


# ---------------------------------------------------------------- exec output

def test_exec_output_suppressed_on_item_complete() -> None:
    r = NotifRouter()
    assert r.route(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {
                "delta": "build line 1\n",
                "threadId": "t1",
                "turnId": "u1",
                "itemId": "e1",
            },
        }
    ) == []
    out = r.route(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {"id": "e1"},
            },
        }
    )
    assert out == []


def test_exec_output_suppressed_on_turn_complete() -> None:
    r = NotifRouter()
    assert r.route(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {
                "delta": "build line 1\n",
                "threadId": "t1",
                "turnId": "u1",
                "itemId": "e1",
            },
        }
    ) == []
    out = r.route(
        {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "u1"}},
        }
    )
    assert out == []


# ---------------------------------------------------------------- turn/diff/plan

def test_turn_diff_is_hidden() -> None:
    r = NotifRouter()
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-print('old')\n"
        "+print('new')\n"
        "diff --git a/bar.py b/bar.py\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1,0 +1,3 @@\n"
        "+a = 1\n"
        "+b = 2\n"
        "+c = 3\n"
    )
    out = r.route(
        {
            "method": "turn/diff/updated",
            "params": {"diff": diff, "threadId": "t", "turnId": "u"},
        }
    )
    assert out == []


def test_diff_stats_parse_per_file_counts() -> None:
    from codex_rc.notif_router import parse_diff_stats

    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/y.py b/y.py\n"
        "--- a/y.py\n"
        "+++ b/y.py\n"
        "@@ -1,0 +1,2 @@\n"
        "+a\n"
        "+b\n"
    )
    stats = parse_diff_stats(diff)
    assert len(stats) == 2
    assert stats[0].path == "x.py" and stats[0].adds == 1 and stats[0].dels == 1
    assert stats[1].path == "y.py" and stats[1].adds == 2 and stats[1].dels == 0


def test_turn_plan_is_handled_by_channel_service() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "turn/plan/updated",
            "params": {
                "explanation": "Here's the plan",
                "plan": [
                    {"step": "Open file", "status": "completed"},
                    {"step": "Edit lines", "status": "in_progress"},
                    {"step": "Run tests", "status": "pending"},
                ],
                "threadId": "t",
                "turnId": "u",
            },
        }
    )
    assert out == []


# ---------------------------------------------------------------- error/warn

def test_error_emits_red_embed() -> None:
    r = NotifRouter()
    out = r.route(
        {
            "method": "error",
            "params": {
                "error": {"message": "model timeout"},
                "willRetry": True,
                "threadId": "t",
                "turnId": "u",
            },
        }
    )
    assert out and out[0].embed is not None
    assert out[0].embed.color == COLOR_ERROR
    assert "model timeout" in (out[0].embed.description or "")
    assert "retry" in (out[0].embed.title or "").lower()


def test_warning_emits_yellow_embed() -> None:
    r = NotifRouter()
    out = r.route({"method": "warning", "params": {"message": "deprecated flag"}})
    assert out and out[0].embed and out[0].embed.color == COLOR_WARN


# ---------------------------------------------------------------- quiet/fallback

def test_quiet_methods_suppressed_by_default() -> None:
    r = NotifRouter()
    assert r.route({"method": "remoteControl/status/changed",
                    "params": {"status": "disabled"}}) == []


def test_verbose_surfaces_quiet_methods() -> None:
    r = NotifRouter(verbose=True)
    out = r.route({"method": "remoteControl/status/changed",
                   "params": {"status": "disabled"}})
    assert out and out[0].embed is not None


def test_unknown_method_quiet_by_default() -> None:
    r = NotifRouter()
    assert r.route({"method": "novel/unknown/event", "params": {"x": 1}}) == []


def test_unknown_method_verbose_falls_back() -> None:
    r = NotifRouter(verbose=True)
    out = r.route({"method": "novel/unknown/event", "params": {"x": 1}})
    assert out and out[0].embed and "novel/unknown/event" in (out[0].embed.title or "")


# ---------------------------------------------------------------- payload shape

def test_payload_to_dict_round_trip() -> None:
    p = DiscordPayload(content="hi")
    assert p.to_dict() == {"content": "hi"}

    p2 = DiscordPayload(embed=DiscordEmbed(title="t", description="d", color=1))
    d2 = p2.to_dict()
    assert d2 == {"embeds": [{"title": "t", "description": "d", "color": 1}]}


def test_payload_to_dict_compacts_embed_description() -> None:
    description = "\n".join(f"log line {i}" for i in range(100))
    p = DiscordPayload(embed=DiscordEmbed(description=description))
    desc = p.to_dict()["embeds"][0]["description"]
    assert "log line 0" in desc
    assert "Long read/log output omitted" in desc
    assert "log line 99" not in desc
