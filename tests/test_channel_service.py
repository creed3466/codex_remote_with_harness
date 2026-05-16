"""Unit tests for :class:`ChannelService`.

These tests don't spawn codex — they use a FakeProc double in place of
CodexServerProcess and verify the channel-level orchestration: thread
creation, image input forwarding, cat-of-patience triggering, button
routing, pump exception handling, and shutdown draining.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_rc.rpc_client import RpcError
from codex_rc.service import (
    ChannelService,
    OperatorInstructions,
    _load_rules_text,
    _load_rules_text_from,
    _load_soul_text_from,
    _TurnCompletionWait,
)
from codex_rc.session_store import SessionStore
from codex_rc.workflow_router import (
    WORKFLOW_ANALYSIS_EFFORT,
    WORKFLOW_ANALYSIS_MODEL,
    WORKFLOW_DESIGN_EFFORT,
    WORKFLOW_DESIGN_MODEL,
    WORKFLOW_SPARK_FALLBACK_MODEL,
    WORKFLOW_SPARK_MODEL,
    WORKFLOW_VERIFICATION_EFFORT,
    WORKFLOW_VERIFICATION_MODEL,
    WorkflowExecutionTurn,
)

# ====================================================================== fakes

class FakeProc:
    """Replaces CodexServerProcess: tracks RPC calls + fakes notifications."""

    def __init__(
        self,
        *,
        project_path: Path,
        pretty_path: Path | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.project_path = project_path
        self.log_dir = log_dir or project_path.parent / "logs"
        self.pretty_path = pretty_path or self.log_dir / "events.txt"
        self.client = MagicMock()
        self.client.respond_to_request = AsyncMock()
        self.set_server_request_handler = MagicMock()
        self.set_on_child_exit = MagicMock()
        self.request = AsyncMock(return_value={})
        self.stop = AsyncMock()
        self.ws_url: str | None = None
        # Mirror the fields ChannelService._restart_after_crash needs to
        # reconstruct a fresh process.
        self.codex_bin = "codex"
        self.codex_args = ("app-server",)
        self.env: dict[str, str] | None = None
        self.transport_mode = "stdio"
        # Pump's iterator: we expose a controllable async generator.
        self._notif_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.client.notifications = lambda: self._iter_notifs()

    async def start(self) -> dict:
        return {"userAgent": "fake/1.0"}

    async def emit(self, notif: dict) -> None:
        """Push a notification the pump will see on next iteration."""
        await self._notif_queue.put(notif)

    async def _iter_notifs(self):
        while True:
            notif = await self._notif_queue.get()
            if notif is None:
                return
            yield notif


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


@pytest.fixture
def fake_proc(tmp_path: Path) -> FakeProc:
    proj = tmp_path / "project"
    proj.mkdir()
    return FakeProc(project_path=proj, pretty_path=tmp_path / "events.txt")


@pytest.fixture
def post_calls() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def post(post_calls):
    async def _post(channel_id: str, payload: dict) -> None:
        post_calls.append((channel_id, payload))
    return _post


@pytest.fixture
def post_update_calls() -> list[tuple[str, str, dict]]:
    return []


@pytest.fixture
def post_update(post_update_calls):
    async def _post_update(channel_id: str, message_key: str, payload: dict) -> None:
        post_update_calls.append((channel_id, message_key, payload))
    return _post_update


@pytest.fixture
def post_animated_calls() -> list[tuple[str, list[str], dict]]:
    return []


@pytest.fixture
def post_animated(post_animated_calls):
    async def _post_animated(channel_id, frames, *, interval_s=1.0, loop=True):
        post_animated_calls.append((channel_id, list(frames), {"interval_s": interval_s, "loop": loop}))
    return _post_animated


def make_channel_service(
    *,
    channel_id: str = "C1",
    store: SessionStore,
    proc: FakeProc,
    post,
    post_update=None,
    post_animated=None,
    cats_path: Path | None = None,
    on_turn_stage=None,
    owner_mention_id: str | None = None,
    auto_approve: bool = False,
    auto_compact_threshold_tokens: int = 0,
    workflow_enabled: bool = False,
) -> ChannelService:
    return ChannelService(
        channel_id=channel_id,
        store=store,
        proc=proc,  # type: ignore[arg-type]
        post=post,
        post_update=post_update,
        post_animated=post_animated,
        project_slug=f"test-{channel_id}",
        cats_path=cats_path,
        on_turn_stage=on_turn_stage,
        owner_mention_id=owner_mention_id,
        auto_approve=auto_approve,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        workflow_enabled=workflow_enabled,
    )


# ====================================================================== send_text

async def test_send_text_creates_thread_lazily(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    # First request returns thread/start result; second is turn/start (any).
    fake_proc.request.side_effect = [
        {"thread": {"id": "thread-1"}},
        {},
    ]
    await cs.send_text("hello")

    # Two RPC calls: thread/start, then turn/start.
    methods = [call.args[0] for call in fake_proc.request.call_args_list]
    assert methods == ["thread/start", "turn/start"]

    # thread id persisted.
    sess = store.get("C1")
    assert sess and sess.codex_thread_id == "thread-1"

    # turn/start input contains the user text.
    turn_params = fake_proc.request.call_args_list[1].args[1]
    assert turn_params["threadId"] == "thread-1"
    assert turn_params["input"] == [{"type": "text", "text": "hello"}]


async def test_usage_diagnostics_capture_thread_source_and_usage(
    store, fake_proc, post
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._codex_init = {"userAgent": "fake/1.0"}  # noqa: SLF001
    fake_proc.request.side_effect = [
        {
            "thread": {
                "id": "thread-1",
                "source": "appServer",
                "threadSource": "user",
                "sessionId": "session-1",
                "modelProvider": "openai",
            }
        },
        {},
    ]

    await cs.send_text("hello")
    cs._update_usage_cache(  # noqa: SLF001
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "total": {
                        "cachedInputTokens": 5,
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "reasoningOutputTokens": 3,
                        "totalTokens": 123,
                    }
                },
            },
        }
    )

    body = "\n".join(cs.usage_diagnostic_lines())
    assert "codex `codex app-server`" in body
    assert "server `fake/1.0`" in body
    assert "source `appServer`" in body
    assert "threadSource `user`" in body
    assert "session `session-1`" in body
    assert "total `123`" in body


async def test_send_text_reuses_existing_thread(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-existing")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    fake_proc.request.return_value = {}
    await cs.send_text("hi")
    methods = [call.args[0] for call in fake_proc.request.call_args_list]
    assert methods == ["turn/start"]


async def test_send_text_with_images_emits_image_input(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}

    await cs.send_text(
        "what is this?",
        image_urls=["https://cdn.discordapp.com/a.png", "https://cdn.discordapp.com/b.png"],
    )
    turn_params = fake_proc.request.call_args.args[1]
    types = [item["type"] for item in turn_params["input"]]
    assert types == ["text", "image", "image"]
    urls = [item.get("url") for item in turn_params["input"]]
    assert urls == [None, "https://cdn.discordapp.com/a.png", "https://cdn.discordapp.com/b.png"]


async def test_send_text_empty_text_and_no_images_raises(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    with pytest.raises(ValueError, match="requires text or image_urls"):
        await cs.send_text("", image_urls=None)


async def test_send_text_records_thread_in_history(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.side_effect = [
        {"thread": {"id": "thread-history-1"}},
        {},
    ]
    await cs.send_text("first message")
    threads = store.list_threads("C1")
    assert len(threads) == 1
    t = threads[0]
    assert t.codex_thread_id == "thread-history-1"
    assert t.preview == "first message"
    assert t.turn_count == 1


async def test_send_text_wraps_new_turn_when_workflow_enabled(
    store, fake_proc, post
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )
    fake_proc.request.return_value = {}

    await cs.send_text("add approval workflow")

    params = fake_proc.request.call_args.args[1]
    text = params["input"][0]["text"]
    assert "CODEX_RC_DEVELOPMENT_WORKFLOW" in text
    assert "add approval workflow" in text
    assert "Stop after step 1" in text
    assert params["model"] == WORKFLOW_ANALYSIS_MODEL
    assert params["effort"] == WORKFLOW_ANALYSIS_EFFORT


async def test_send_text_does_not_wrap_information_request_when_workflow_enabled(
    store, fake_proc, post
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )
    fake_proc.request.return_value = {}

    await cs.send_text("workflow는 어디서 켜져?")

    params = fake_proc.request.call_args.args[1]
    assert params["input"] == [{"type": "text", "text": "workflow는 어디서 켜져?"}]
    assert "model" not in params
    assert "effort" not in params


async def test_workflow_end_to_end_uses_ephemeral_threads_and_handoff_files(
    store, fake_proc, post, post_calls, tmp_path
) -> None:
    """Full happy-path drive of the new workflow:

    button click → write stage_1_handoff.md → for each stage:
       ephemeral thread/start → turn/start → agent writes stage_N md →
       service parses + validates handoff
    → after stage 4, service injects final summary into main thread and
       archives the workflow directory.

    Each stage agent's "output" is faked here by writing the expected
    handoff markdown to disk just before the turn/completed notification
    fires — simulating what a real codex agent would do via its shell
    tools in response to the stage prompt.
    """
    # Use tmp_path as project root so the workflow_store lives in a
    # throwaway directory.
    fake_proc.project_path = tmp_path
    store.register(channel_id="C1", project_path=str(tmp_path))
    store.update_thread("C1", "thread-main")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    # Drive stage 1 to the awaiting-approval state and get the buttons.
    await cs.send_text("add retry to cron worker")
    cs.workflow.observe_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "Plan A: Inline retry decorator\n"
                        "Plan B: Central retry service\n"
                        "CODEX_RC_WORKFLOW_OPTIONS:\n"
                        "A: Inline retry decorator\n"
                        "B: Central retry service\n"
                        "RECOMMENDED: A\n"
                        "END_CODEX_RC_WORKFLOW_OPTIONS"
                    ),
                }
            },
        }
    )
    prompt = cs.workflow.observe_notification(
        {"method": "turn/completed", "params": {}}
    )
    assert prompt is not None

    # Stage handoff markdown the fake "agents" will write. Each must
    # parse against its stage's schema or service.py raises.
    workflow_dir = tmp_path / "data" / "workflow"
    # We need the workflow id, which is in the buttons' custom_id.
    # Easiest: extract it from the seed handoff once the button fires.
    stage_md = {
        2: (
            "# Design Brief — {wf}\n\n"
            "## Task\nadd retry to cron worker\n\n"
            "## Approved plan\nA: Inline retry decorator\n\n"
            "## Architecture decisions\n- Add @retry decorator in src/cron/retry.py\n\n"
            "## Files to modify / create\n"
            "- src/cron/worker.py (M): wrap run() with @retry()\n"
            "- src/cron/retry.py (A): new exponential-backoff decorator\n\n"
            "## Implementation checklist (in order)\n"
            "1. Create src/cron/retry.py with retry() factory\n"
            "2. Wrap worker.run() with @retry()\n\n"
            "## Test strategy\n- Unit: decorator under mocked clock\n\n"
            "## Acceptance criteria\n- [ ] cron worker retries transient failures\n\n"
            "## Risks / open questions\n- None\n\n"
            "## Output budget for stage 3\n≤ 3000 tokens.\n"
        ),
        3: (
            "# Verification Brief — {wf}\n\n"
            "## Task\nadd retry to cron worker\n\n"
            "## Approved plan\nA: Inline retry decorator\n\n"
            "## What was implemented\n"
            "Added @retry decorator in src/cron/retry.py and applied to "
            "worker.run().\n\n"
            "## Files changed\n"
            "- src/cron/worker.py (M): wrapped run() with @retry()\n"
            "- src/cron/retry.py (A): new exponential-backoff decorator\n\n"
            "## Tests added/modified\n- tests/test_retry.py::test_retries\n\n"
            "## Out of scope / deferred\n- None\n\n"
            "## Verification checklist\n"
            "- [ ] `pytest tests/test_retry.py -v` — green\n"
            "- [ ] Acceptance: worker retries transient failures\n\n"
            "## Risks / scrutiny areas\n- None\n\n"
            "## Output budget for stage 4\n≤ 2500 tokens.\n"
        ),
        4: (
            "# Workflow Result — {wf}\n\n"
            "## What was asked\nAdd retry to cron worker.\n\n"
            "## What was done\n"
            "Added exponential-backoff retry decorator and applied it to "
            "the worker entry point.\n\n"
            "## Verification results\n"
            "- ✅ `pytest tests/test_retry.py -v` — 2 passed\n\n"
            "## Files changed\n"
            "- src/cron/retry.py\n"
            "- src/cron/worker.py\n\n"
            "## Caveats / follow-ups\n- None\n"
        ),
    }

    rpc_calls: list[tuple[str, dict]] = []
    eph_counter = {"n": 0}
    turn_counter = {"n": 0}

    async def request_side_effect(method, params=None, **_kw):
        params = dict(params or {})
        rpc_calls.append((method, params))
        if method == "turn/start" and "input" in params:
            text = params["input"][0].get("text", "")
            # Initial stage-1 turn carries the workflow-begin wrapper —
            # for that one we don't write any handoff (stage 1 is the
            # plan-recommendation turn whose handoff is bot-synthesized).
            if "CODEX_RC_DEVELOPMENT_WORKFLOW" in text:
                return {"turn": {"id": "turn-stage1"}}
        if method == "thread/start":
            eph_counter["n"] += 1
            return {"thread": {"id": f"eph-{eph_counter['n']}"}}
        if method == "turn/start":
            turn_counter["n"] += 1
            turn_id = f"turn-{turn_counter['n']}"
            thread_id = params.get("threadId", "")
            stage = _extract_stage_from_prompt(params["input"][0]["text"])
            workflow_id = _extract_workflow_id_from_prompt(
                params["input"][0]["text"]
            )

            async def complete() -> None:
                await asyncio.sleep(0)
                # Simulate the agent writing the handoff file.
                handoff_path = (
                    workflow_dir
                    / workflow_id
                    / f"stage_{stage}_handoff.md"
                )
                handoff_path.parent.mkdir(parents=True, exist_ok=True)
                handoff_path.write_text(
                    stage_md[stage].format(wf=workflow_id), encoding="utf-8"
                )
                cs._observe_workflow_turn_completion(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id},
                        },
                    }
                )

            asyncio.create_task(complete())
            return {"turn": {"id": turn_id}}
        if method == "thread/inject_items":
            return {}
        return {}

    fake_proc.request.side_effect = request_side_effect

    # Click Plan A — schedules the workflow execution task.
    assert await cs.handle_button(prompt.buttons[0].custom_id) is True

    # Drain the background workflow task to completion.
    assert cs._workflow_execution_task is not None
    await asyncio.wait_for(cs._workflow_execution_task, timeout=5.0)

    # The stage-1 handoff was bot-synthesized synchronously before the
    # task was scheduled — must exist and parse.
    seed = cs.workflow._pending  # noqa: SLF001 — None after completion
    assert seed is None  # workflow.complete_execution() cleared it
    # Workflow archived to _archive/.
    archive_root = workflow_dir / "_archive"
    assert archive_root.exists()
    archived = list(archive_root.iterdir())
    assert len(archived) == 1
    # All four handoff files survived into the archive.
    archived_dir = archived[0]
    for stage in (1, 2, 3, 4):
        assert (archived_dir / f"stage_{stage}_handoff.md").exists()
    # meta.json records completion outcome.
    meta_raw = (archived_dir / "meta.json").read_text(encoding="utf-8")
    assert '"outcome": "completed"' in meta_raw

    # Stage 1's turn/start happens before fake_proc.request.side_effect
    # is installed, so it doesn't land in rpc_calls. The 3 ephemeral
    # thread/start + 3 turn/start below correspond to stages 2/3/4 only.
    thread_starts = [m for m, _ in rpc_calls if m == "thread/start"]
    turn_starts = [
        params for m, params in rpc_calls if m == "turn/start"
    ]
    assert len(thread_starts) == 3
    assert len(turn_starts) == 3
    stage_to_model = {
        2: WORKFLOW_DESIGN_MODEL,
        3: WORKFLOW_SPARK_MODEL,
        4: WORKFLOW_VERIFICATION_MODEL,
    }
    stage_to_effort = {
        2: WORKFLOW_DESIGN_EFFORT,
        4: WORKFLOW_VERIFICATION_EFFORT,
    }
    for turn_params in turn_starts:
        stage = _extract_stage_from_prompt(turn_params["input"][0]["text"])
        assert turn_params.get("model") == stage_to_model[stage]
        if stage in stage_to_effort:
            assert turn_params.get("effort") == stage_to_effort[stage]
    # The 3 workflow-stage turn/starts target distinct ephemeral threads.
    stage_thread_ids = [
        params["threadId"]
        for params in turn_starts
        if params.get("threadId", "").startswith("eph-")
    ]
    assert sorted(stage_thread_ids) == ["eph-1", "eph-2", "eph-3"]
    # And final inject_items posted onto the user's main thread.
    inject_calls = [
        params for m, params in rpc_calls if m == "thread/inject_items"
    ]
    assert any(p.get("threadId") == "thread-main" for p in inject_calls)
    # Discord saw both per-stage start embeds and the completion embed.
    embed_titles = [
        embed.get("title", "")
        for _, payload in post_calls
        for embed in payload.get("embeds", [])
    ]
    assert any("Stage 2/4" in t for t in embed_titles)
    assert any("Stage 3/4" in t for t in embed_titles)
    assert any("Stage 4/4" in t for t in embed_titles)
    assert any("Workflow complete" in t for t in embed_titles)


async def test_workflow_end_to_end_fallback_stage3_on_context_window_exceeded(
    store, fake_proc, post, post_calls, tmp_path
) -> None:
    """End-to-end workflow run where stage-3 spark overflows context and must
    retry on the fallback model using a fresh thread before stage-4.
    """
    fake_proc.project_path = tmp_path
    store.register(channel_id="C1", project_path=str(tmp_path))
    store.update_thread("C1", "thread-main")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    await cs.send_text("add retry to cron worker")
    cs.workflow.observe_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "Plan A: Inline retry decorator\n"
                        "Plan B: Central retry service\n"
                        "CODEX_RC_WORKFLOW_OPTIONS:\n"
                        "A: Inline retry decorator\n"
                        "B: Central retry service\n"
                        "RECOMMENDED: A\n"
                        "END_CODEX_RC_WORKFLOW_OPTIONS"
                    ),
                }
            },
        }
    )
    prompt = cs.workflow.observe_notification(
        {"method": "turn/completed", "params": {}}
    )
    assert prompt is not None

    workflow_dir = tmp_path / "data" / "workflow"
    stage_md = {
        2: (
            "# Design Brief — {wf}\n\n"
            "## Task\nadd retry to cron worker\n\n"
            "## Approved plan\nA: Inline retry decorator\n\n"
            "## Architecture decisions\n- Add @retry decorator in src/cron/retry.py\n\n"
            "## Files to modify / create\n"
            "- src/cron/worker.py (M): wrap run() with @retry()\n"
            "- src/cron/retry.py (A): new exponential-backoff decorator\n\n"
            "## Implementation checklist (in order)\n"
            "1. Create src/cron/retry.py with retry() factory\n"
            "2. Wrap worker.run() with @retry()\n\n"
            "## Test strategy\n- Unit: decorator under mocked clock\n\n"
            "## Acceptance criteria\n- [ ] cron worker retries transient failures\n\n"
            "## Risks / open questions\n- None\n\n"
            "## Output budget for stage 3\n≤ 3000 tokens.\n"
        ),
        3: (
            "# Verification Brief — {wf}\n\n"
            "## Task\nadd retry to cron worker\n\n"
            "## Approved plan\nA: Inline retry decorator\n\n"
            "## What was implemented\n"
            "Added @retry decorator in src/cron/retry.py and applied to "
            "worker.run().\n\n"
            "## Files changed\n"
            "- src/cron/worker.py (M): wrapped run() with @retry()\n"
            "- src/cron/retry.py (A): new exponential-backoff decorator\n\n"
            "## Tests added/modified\n- tests/test_retry.py::test_retries\n\n"
            "## Out of scope / deferred\n- None\n\n"
            "## Verification checklist\n"
            "- [ ] `pytest tests/test_retry.py -v` — green\n"
            "- [ ] Acceptance: worker retries transient failures\n\n"
            "## Risks / scrutiny areas\n- None\n\n"
            "## Output budget for stage 4\n≤ 2500 tokens.\n"
        ),
        4: (
            "# Workflow Result — {wf}\n\n"
            "## What was asked\nAdd retry to cron worker.\n\n"
            "## What was done\n"
            "Added exponential-backoff retry decorator and applied it to "
            "the worker entry point.\n\n"
            "## Verification results\n"
            "- ✅ `pytest tests/test_retry.py -v` — 2 passed\n\n"
            "## Files changed\n"
            "- src/cron/retry.py\n"
            "- src/cron/worker.py\n\n"
            "## Caveats / follow-ups\n- None\n"
        ),
    }

    rpc_calls: list[tuple[str, dict]] = []
    eph_counter = {"n": 0}
    turn_counter = {"n": 0}
    stage3_attempts = {"n": 0}

    async def request_side_effect(method, params=None, **_kw):
        params = dict(params or {})
        rpc_calls.append((method, params))
        if method == "thread/start":
            eph_counter["n"] += 1
            return {"thread": {"id": f"eph-{eph_counter['n']}"}}

        if method == "turn/start":
            if "CODEX_RC_DEVELOPMENT_WORKFLOW" in params.get("input", [{}])[0].get(
                "text", ""
            ):
                return {"turn": {"id": "turn-stage1"}}

            turn_counter["n"] += 1
            turn_id = f"turn-{turn_counter['n']}"
            thread_id = params.get("threadId", "")
            stage = _extract_stage_from_prompt(params["input"][0]["text"])
            workflow_id = _extract_workflow_id_from_prompt(
                params["input"][0]["text"]
            )

            if stage == 3 and params.get("model") == WORKFLOW_SPARK_MODEL:
                stage3_attempts["n"] += 1
                if stage3_attempts["n"] == 1:
                    async def fail_context() -> None:
                        await asyncio.sleep(0)
                        cs._observe_workflow_turn_completion(
                            {
                                "method": "error",
                                "params": {
                                    "threadId": thread_id,
                                    "turnId": turn_id,
                                    "error": {
                                        "message": (
                                            "Codex ran out of room in the model's context window. "
                                            "Start a new thread or clear earlier history before "
                                            "retrying."
                                        ),
                                        "codexErrorInfo": "contextWindowExceeded",
                                    },
                                },
                            }
                        )

                    asyncio.create_task(fail_context())
                    return {"turn": {"id": turn_id}}

            async def complete() -> None:
                await asyncio.sleep(0)
                handoff_path = (
                    workflow_dir
                    / workflow_id
                    / f"stage_{stage}_handoff.md"
                )
                handoff_path.parent.mkdir(parents=True, exist_ok=True)
                handoff_path.write_text(
                    stage_md[stage].format(wf=workflow_id), encoding="utf-8"
                )
                cs._observe_workflow_turn_completion(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id},
                        },
                    }
                )

            asyncio.create_task(complete())
            return {"turn": {"id": turn_id}}

        if method == "thread/inject_items":
            return {}
        return {}

    fake_proc.request.side_effect = request_side_effect

    assert await cs.handle_button(prompt.buttons[0].custom_id) is True
    assert cs._workflow_execution_task is not None
    await asyncio.wait_for(cs._workflow_execution_task, timeout=5.0)

    archive_root = workflow_dir / "_archive"
    assert archive_root.exists()
    archived = list(archive_root.iterdir())
    assert len(archived) == 1
    archived_dir = archived[0]
    for stage in (1, 2, 3, 4):
        assert (archived_dir / f"stage_{stage}_handoff.md").exists()

    meta_raw = (archived_dir / "meta.json").read_text(encoding="utf-8")
    assert '"outcome": "completed"' in meta_raw
    assert stage3_attempts["n"] == 1

    thread_starts = [m for m, _ in rpc_calls if m == "thread/start"]
    turn_starts = [params for m, params in rpc_calls if m == "turn/start"]
    assert len(thread_starts) == 4
    assert len(turn_starts) == 4
    stage3_models = [
        params.get("model")
        for params in turn_starts
        if _extract_stage_from_prompt(params["input"][0]["text"]) == 3
    ]
    assert stage3_models == [WORKFLOW_SPARK_MODEL, WORKFLOW_SPARK_FALLBACK_MODEL]

    embed_titles = [
        embed.get("title", "")
        for _, payload in post_calls
        for embed in payload.get("embeds", [])
    ]
    assert any("Workflow model fallback" in t for t in embed_titles)
    assert any(
        "context window exceeded" in str(payload).lower()
        for _, payload in post_calls
    )
    assert any("Workflow complete" in t for t in embed_titles)


async def test_pump_skips_discord_posting_for_workflow_ephemeral_notifications(
    store, fake_proc, post, post_calls
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    loop = asyncio.get_running_loop()
    workflow_future = loop.create_future()
    cs._workflow_turn_wait = _TurnCompletionWait(
        thread_id="eph-workflow",
        future=workflow_future,
    )

    cs._pump_task = asyncio.create_task(
        cs._pump(),
        name="test-workflow-ephemeral-suppression",
    )

    await fake_proc.emit(
        {
            "method": "item/completed",
            "params": {
                "threadId": "eph-workflow",
                "turnId": "turn-workflow",
                "item": {
                    "id": "cmd-1",
                    "type": "commandExecution",
                    "command": "git status",
                    "exitCode": 1,
                    "aggregatedOutput": (
                        "fatal: not a git repository (or any of the parent "
                        "directories): .git\n"
                    ),
                },
            },
        }
    )
    await fake_proc.emit(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "eph-workflow",
                "turn": {"id": "turn-workflow"},
            },
        }
    )
    await asyncio.wait_for(workflow_future, timeout=1.0)
    await asyncio.sleep(0)
    assert workflow_future.done()
    assert post_calls == []

    cs._pump_task.cancel()
    try:
        await asyncio.wait_for(cs._pump_task, timeout=1.0)
    except (asyncio.CancelledError, Exception):
        pass


async def test_workflow_end_to_end_retries_schema_repair_for_stage2_handoff(
    store, fake_proc, post, post_calls, tmp_path
) -> None:
    """If stage 2 writes a malformed file-change row, the workflow should
    retry once with repair instructions and then continue normally on the
    corrected handoff.
    """
    fake_proc.project_path = tmp_path
    store.register(channel_id="C1", project_path=str(tmp_path))
    store.update_thread("C1", "thread-main")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    await cs.send_text("add retry handling")
    cs.workflow.observe_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "CODEX_RC_WORKFLOW_OPTIONS:\n"
                        "A: Approach A\n"
                        "B: Approach B\n"
                        "RECOMMENDED: A\n"
                        "END_CODEX_RC_WORKFLOW_OPTIONS"
                    ),
                }
            },
        }
    )
    button_prompt = cs.workflow.observe_notification(
        {"method": "turn/completed", "params": {}}
    )
    assert button_prompt is not None

    workflow_dir = tmp_path / "data" / "workflow"
    stage2_invalid = """
    # Design Brief — {wf}

    ## Task
    add retry handling

    ## Approved plan
    A: Approach A

    ## Architecture decisions
    - Add @retry decorator in src/cron/retry.py

    ## Files to modify / create
    -
    - src/cron/worker.py (M): wrap run() with @retry()

    ## Implementation checklist (in order)
    1. Create src/cron/retry.py with retry() factory
    2. Wrap worker.run() with @retry()

    ## Test strategy
    - Unit: decorator under mocked clock

    ## Acceptance criteria
    - [ ] cron worker retries transient failures

    ## Risks / open questions
    - None

    ## Output budget for stage 3
    ≤ 3000 tokens.
    """

    stage2_valid = """
    # Design Brief — {wf}

    ## Task
    add retry handling

    ## Approved plan
    A: Approach A

    ## Architecture decisions
    - Add @retry decorator in src/cron/retry.py

    ## Files to modify / create
    - src/cron/worker.py (M): wrap run() with @retry()
    - src/cron/retry.py (A): new exponential-backoff decorator

    ## Implementation checklist (in order)
    1. Create src/cron/retry.py with retry() factory
    2. Wrap worker.run() with @retry()

    ## Test strategy
    - Unit: decorator under mocked clock

    ## Acceptance criteria
    - [ ] cron worker retries transient failures

    ## Risks / open questions
    - None

    ## Output budget for stage 3
    ≤ 3000 tokens.
    """

    stage3_md = """
    # Verification Brief — {wf}

    ## Task
    add retry handling

    ## Approved plan
    A: Approach A

    ## What was implemented
    Added @retry decorator in src/cron/retry.py and applied to worker.run().

    ## Files changed
    - src/cron/worker.py (M): wrapped run() with @retry()
    - src/cron/retry.py (A): new exponential-backoff decorator

    ## Tests added/modified
    - tests/test_retry.py::test_retries

    ## Out of scope / deferred
    - None

    ## Verification checklist
    - [ ] `pytest tests/test_retry.py -v` — green
    - [ ] Acceptance: worker retries transient failures

    ## Risks / scrutiny areas
    - None

    ## Output budget for stage 4
    ≤ 2500 tokens.
    """

    stage4_md = """
    # Workflow Result — {wf}

    ## What was asked
    Add retry handling.

    ## What was done
    Added exponential-backoff retry decorator and applied it to the worker
    entry point.

    ## Verification results
    - ✅ `pytest tests/test_retry.py -v` — 2 passed

    ## Files changed
    - src/cron/retry.py
    - src/cron/worker.py

    ## Caveats / follow-ups
    - None
    """

    rpc_calls: list[tuple[str, dict]] = []
    eph_counter = {"n": 0}
    turn_counter = {"n": 0}
    stage2_attempts = {"n": 0}

    async def request_side_effect(method, params=None, **_kw):
        params = dict(params or {})
        rpc_calls.append((method, params))
        if method == "turn/start" and "input" in params:
            text = params["input"][0].get("text", "")
            if "CODEX_RC_DEVELOPMENT_WORKFLOW" in text:
                return {"turn": {"id": "turn-stage1"}}
        if method == "thread/start":
            eph_counter["n"] += 1
            return {"thread": {"id": f"eph-{eph_counter['n']}"}}
        if method == "turn/start":
            turn_counter["n"] += 1
            turn_id = f"turn-{turn_counter['n']}"
            thread_id = params.get("threadId", "")
            stage = _extract_stage_from_prompt(params["input"][0]["text"])
            workflow_id = _extract_workflow_id_from_prompt(
                params["input"][0]["text"]
            )

            if stage == 2:
                stage2_attempts["n"] += 1
                content = stage2_invalid if stage2_attempts["n"] == 1 else stage2_valid
            elif stage == 3:
                content = stage3_md
            elif stage == 4:
                content = stage4_md
            else:
                content = ""

            async def complete() -> None:
                await asyncio.sleep(0)
                handoff_path = (
                    workflow_dir / workflow_id / f"stage_{stage}_handoff.md"
                )
                handoff_path.parent.mkdir(parents=True, exist_ok=True)
                handoff_path.write_text(
                    content.format(wf=workflow_id), encoding="utf-8"
                )
                cs._observe_workflow_turn_completion(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id},
                        },
                    }
                )

            asyncio.create_task(complete())
            return {"turn": {"id": turn_id}}

        if method == "thread/inject_items":
            return {}
        return {}

    fake_proc.request.side_effect = request_side_effect

    assert await cs.handle_button(button_prompt.buttons[0].custom_id) is True
    assert cs._workflow_execution_task is not None
    await asyncio.wait_for(cs._workflow_execution_task, timeout=5.0)

    assert stage2_attempts["n"] == 2

    archive_root = workflow_dir / "_archive"
    assert archive_root.exists()
    archived = list(archive_root.iterdir())
    assert len(archived) == 1
    meta_raw = (archived[0] / "meta.json").read_text(encoding="utf-8")
    assert '"outcome": "completed"' in meta_raw

    thread_starts = [m for m, _ in rpc_calls if m == "thread/start"]
    turn_starts = [params for m, params in rpc_calls if m == "turn/start"]
    assert len(thread_starts) == 4
    assert len(turn_starts) == 4

    stage2_turns = [
        _extract_stage_from_prompt(p["input"][0]["text"]) for p in turn_starts
    ]
    assert stage2_turns.count(2) == 2
    for turn_params in turn_starts:
        stage = _extract_stage_from_prompt(turn_params["input"][0]["text"])
        if stage == 2:
            assert turn_params["model"] == WORKFLOW_DESIGN_MODEL
            assert turn_params["effort"] == WORKFLOW_DESIGN_EFFORT
        elif stage == 3:
            assert turn_params["model"] == WORKFLOW_SPARK_MODEL
        elif stage == 4:
            assert turn_params["model"] == WORKFLOW_VERIFICATION_MODEL
            assert turn_params["effort"] == WORKFLOW_VERIFICATION_EFFORT

    titles = [
        embed.get("title", "")
        for _, payload in post_calls
        for embed in payload.get("embeds", [])
    ]
    assert any("Workflow handoff schema repair" in t for t in titles)
    assert any("Workflow complete" in t for t in titles)

    inject_calls = [
        params for m, params in rpc_calls if m == "thread/inject_items"
    ]
    assert any(p.get("threadId") == "thread-main" for p in inject_calls)


async def test_workflow_aborts_when_stage_handoff_missing(
    store, fake_proc, post, post_calls, tmp_path
) -> None:
    """If the agent ends a stage without writing its handoff file, the
    service must surface a clear error and archive the workflow as
    failed instead of silently skipping to the next stage.
    """
    fake_proc.project_path = tmp_path
    store.register(channel_id="C1", project_path=str(tmp_path))
    store.update_thread("C1", "thread-main")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    await cs.send_text("add retry handling")
    cs.workflow.observe_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "CODEX_RC_WORKFLOW_OPTIONS:\n"
                        "A: Approach A\n"
                        "B: Approach B\n"
                        "RECOMMENDED: A\n"
                        "END_CODEX_RC_WORKFLOW_OPTIONS"
                    ),
                }
            },
        }
    )
    button_prompt = cs.workflow.observe_notification(
        {"method": "turn/completed", "params": {}}
    )
    assert button_prompt is not None

    eph_counter = {"n": 0}

    async def request_side_effect(method, params=None, **_kw):
        params = dict(params or {})
        if method == "thread/start":
            eph_counter["n"] += 1
            return {"thread": {"id": f"eph-{eph_counter['n']}"}}
        if method == "turn/start":
            thread_id = params.get("threadId", "")
            text = params.get("input", [{}])[0].get("text", "")
            # Stage 1 (main thread): no handoff expected, just return.
            if "CODEX_RC_DEVELOPMENT_WORKFLOW" in text:
                return {"turn": {"id": "turn-stage1"}}

            async def complete() -> None:
                await asyncio.sleep(0)
                # NO handoff file written — simulates an agent that
                # ignored the write-handoff instruction.
                cs._observe_workflow_turn_completion(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": "turn-2"},
                        },
                    }
                )

            asyncio.create_task(complete())
            return {"turn": {"id": "turn-2"}}
        return {}

    fake_proc.request.side_effect = request_side_effect

    assert await cs.handle_button(button_prompt.buttons[0].custom_id) is True
    assert cs._workflow_execution_task is not None
    await asyncio.wait_for(cs._workflow_execution_task, timeout=5.0)

    # Workflow archived with a failure outcome.
    archive_root = tmp_path / "data" / "workflow" / "_archive"
    assert archive_root.exists()
    archived = list(archive_root.iterdir())
    assert len(archived) == 1
    meta_raw = (archived[0] / "meta.json").read_text(encoding="utf-8")
    assert '"outcome": "failed' in meta_raw
    # Discord saw the failure embed.
    titles = [
        embed.get("title", "")
        for _, payload in post_calls
        for embed in payload.get("embeds", [])
    ]
    assert any("Workflow execution failed" in t for t in titles)


def _extract_stage_from_prompt(prompt: str) -> int:
    import re

    match = re.search(r'stage="(\d+)"', prompt)
    return int(match.group(1)) if match else 0


def _extract_workflow_id_from_prompt(prompt: str) -> str:
    import re

    match = re.search(r'id="([^"]+)"', prompt)
    return match.group(1) if match else ""





def _ephemeral_request_router(cs, turn_completion_provider):
    """Build a ``fake_proc.request`` side_effect that handles the new
    workflow-stage RPC sequence: ``thread/start`` (ephemeral) then
    ``turn/start``. ``turn_completion_provider`` decides what
    notification to deliver per ``turn/start`` call (a 2-arg callable
    taking the params dict and the running call list, returning either
    None for turn/completed or a dict to pass as the error notification
    body).
    """
    calls: list[tuple[str, dict]] = []
    ephemeral_counter = {"n": 0}
    turn_counter = {"n": 0}

    async def request_side_effect(method, params=None, **_kw):
        params = dict(params or {})
        calls.append((method, params))
        if method == "thread/start":
            ephemeral_counter["n"] += 1
            return {"thread": {"id": f"eph-{ephemeral_counter['n']}"}}
        if method == "turn/start":
            turn_counter["n"] += 1
            turn_id = f"turn-{turn_counter['n']}"
            thread_id = params.get("threadId", "")
            error_payload = turn_completion_provider(params, calls)

            async def deliver_notification() -> None:
                await asyncio.sleep(0)
                if error_payload is not None:
                    cs._observe_workflow_turn_completion(
                        {
                            "method": "error",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "error": error_payload,
                            },
                        }
                    )
                else:
                    cs._observe_workflow_turn_completion(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": thread_id,
                                "turn": {"id": turn_id},
                            },
                        }
                    )

            asyncio.create_task(deliver_notification())
            return {"turn": {"id": turn_id}}
        return {}

    return calls, request_side_effect


async def test_workflow_stage_falls_back_when_spark_model_unavailable(
    store, fake_proc, post, post_calls
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    def completion(params, _calls):
        if params.get("model") == WORKFLOW_SPARK_MODEL:
            raise RpcError(400, "model gpt-5.3-codex-spark is not available")
        return None  # turn/completed

    calls, side_effect = _ephemeral_request_router(cs, completion)
    fake_proc.request.side_effect = side_effect

    turn = WorkflowExecutionTurn(
        stage=3,
        title="Implementation",
        prompt="implement",
        model=WORKFLOW_SPARK_MODEL,
        fallback_model=WORKFLOW_SPARK_FALLBACK_MODEL,
    )

    await cs._run_workflow_stage_turn(turn)

    # Spec: spark turn/start raises → fresh thread + fallback turn/start.
    # That means 2x thread/start + 2x turn/start, in interleaved order.
    methods = [m for m, _ in calls]
    assert methods == ["thread/start", "turn/start", "thread/start", "turn/start"]
    turn_models = [p.get("model") for m, p in calls if m == "turn/start"]
    assert turn_models == [WORKFLOW_SPARK_MODEL, WORKFLOW_SPARK_FALLBACK_MODEL]
    assert any("Workflow model fallback" in str(payload) for _, payload in post_calls)


async def test_workflow_stage_falls_back_on_context_window_exceeded(
    store, fake_proc, post, post_calls
) -> None:
    """Spark's 128K window is half of gpt-5.4's 272K. When the running turn
    raises a ``contextWindowExceeded`` error notification, the workflow must
    retry the same stage on the larger-context fallback model — on a new
    ephemeral thread since the original is in systemError state.
    """
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    def completion(params, _calls):
        if params.get("model") == WORKFLOW_SPARK_MODEL:
            return {
                "message": (
                    "Codex ran out of room in the model's context window. "
                    "Start a new thread or clear earlier history before "
                    "retrying."
                ),
                "codexErrorInfo": "contextWindowExceeded",
            }
        return None

    calls, side_effect = _ephemeral_request_router(cs, completion)
    fake_proc.request.side_effect = side_effect

    turn = WorkflowExecutionTurn(
        stage=3,
        title="Implementation",
        prompt="implement",
        model=WORKFLOW_SPARK_MODEL,
        fallback_model=WORKFLOW_SPARK_FALLBACK_MODEL,
    )

    await cs._run_workflow_stage_turn(turn)

    methods = [m for m, _ in calls]
    assert methods == ["thread/start", "turn/start", "thread/start", "turn/start"]
    turn_models = [p.get("model") for m, p in calls if m == "turn/start"]
    assert turn_models == [WORKFLOW_SPARK_MODEL, WORKFLOW_SPARK_FALLBACK_MODEL]
    fallback_payloads = [
        payload for _, payload in post_calls if "Workflow model fallback" in str(payload)
    ]
    assert fallback_payloads
    assert "context window exceeded" in str(fallback_payloads[-1])


# ====================================================================== plan progress

async def test_plan_progress_updates_one_editable_message(
    store, fake_proc, post, post_update, post_update_calls
) -> None:
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        post_update=post_update,
    )

    handled = await cs._handle_plan_progress(  # noqa: SLF001
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "explanation": "plan",
                "plan": [
                    {"step": "Open file", "status": "completed"},
                    {"step": "Edit lines", "status": "in_progress"},
                    {"step": "Run tests", "status": "pending"},
                ],
            },
        }
    )

    assert handled is True
    assert len(post_update_calls) == 1
    channel_id, key, payload = post_update_calls[0]
    assert channel_id == "C1"
    assert key == "plan:t1:u1"
    content = payload["content"]
    assert content.startswith("```")
    assert "✅ Open file" in content
    assert "⬜ Run tests" in content
    assert any("⠋" in line for line in content.splitlines())
    assert "⬜ Run tests" in content

    await cs._handle_plan_progress(  # noqa: SLF001
        {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "u1"}},
        }
    )

    assert len(post_update_calls) == 2
    assert post_update_calls[-1][1] == "plan:t1:u1"
    assert "✅ Edit lines" in post_update_calls[-1][2]["content"]
    assert cs._plan_progress.snapshot is None  # noqa: SLF001


async def test_plan_progress_animates_immediately(
    store, fake_proc, post, post_update, post_update_calls
) -> None:
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        post_update=post_update,
    )

    await cs._handle_plan_progress(  # noqa: SLF001
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "explanation": "plan",
                "plan": [
                    {"step": "Start", "status": "in_progress"},
                    {"step": "Finalize", "status": "pending"},
                ],
            },
        }
    )

    await asyncio.sleep(0)
    assert len(post_update_calls) >= 2
    first_content = post_update_calls[0][2]["content"]
    second_content = post_update_calls[1][2]["content"]
    assert first_content != second_content
    assert "..." not in second_content

    await cs._handle_plan_progress(  # noqa: SLF001
        {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "u1"}},
        }
    )
    assert cs._plan_progress.snapshot is None  # noqa: SLF001


# ====================================================================== resume / new

async def test_resume_thread_calls_thread_resume_rpc(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-old")
    store.record_thread(
        codex_thread_id="thread-other",
        channel_id="C1",
        project_path=str(fake_proc.project_path),
    )
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {}

    await cs.resume_thread("thread-other")

    methods = [call.args[0] for call in fake_proc.request.call_args_list]
    assert methods == ["thread/resume"]
    params = fake_proc.request.call_args.args[1]
    assert params == {"threadId": "thread-other"}

    sess = store.get("C1")
    assert sess and sess.codex_thread_id == "thread-other"


async def test_new_thread_clears_old_and_creates_fresh(store, fake_proc, post) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-old")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {"thread": {"id": "thread-fresh"}}

    new_id = await cs.new_thread()
    assert new_id == "thread-fresh"
    sess = store.get("C1")
    assert sess and sess.codex_thread_id == "thread-fresh"


async def test_stop_generates_handoff_before_proc_stop(
    store, fake_proc, post, post_calls
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-source")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._pump_task = asyncio.create_task(cs._pump(), name="test-handoff-pump")

    async def request_side(method, params=None, **_kw):
        if method == "turn/start":
            async def emit_handoff():
                await fake_proc.emit(
                    {
                        "method": "turn/started",
                        "params": {
                            "threadId": "thread-source",
                            "turn": {"id": "turn-handoff"},
                        },
                    }
                )
                await fake_proc.emit(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-source",
                            "turnId": "turn-handoff",
                            "item": {
                                "id": "item-1",
                                "type": "agentMessage",
                                "text": "# Codex Handoff\n\nReady for next session.",
                            },
                        },
                    }
                )
                await fake_proc.emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-source",
                            "turn": {"id": "turn-handoff"},
                        },
                    }
                )

            asyncio.create_task(emit_handoff())
            return {"turn": {"id": "turn-handoff"}}
        return {}

    fake_proc.request.side_effect = request_side

    await cs.stop()

    handoffs = store.list_handoffs("C1")
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff.source_codex_thread_id == "thread-source"
    assert "Ready for next session" in store.read_handoff_body(handoff)
    sess = store.get("C1")
    assert sess and sess.state == "stopped"
    assert sess.handoff_id == handoff.handoff_id
    fake_proc.stop.assert_awaited_once()
    titles = _embed_titles(post_calls)
    assert any("Handoff saved" in t for t in titles)


async def test_start_handoff_thread_creates_fresh_thread_and_injects(
    store, fake_proc, post
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-old")
    handoff = store.record_handoff(
        handoff_id="handoff-1",
        channel_id="C1",
        project_path=str(fake_proc.project_path),
        sandbox="workspace-write",
        approval="on-request",
        source_codex_thread_id="thread-old",
        body="# Codex Handoff\n\nUse this context.",
    )
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.side_effect = [
        {"thread": {"id": "thread-new"}},
        {},
    ]

    new_id = await cs.start_handoff_thread(handoff)

    assert new_id == "thread-new"
    methods = [c.args[0] for c in fake_proc.request.call_args_list]
    assert methods == ["thread/start", "thread/inject_items"]
    inject_params = fake_proc.request.call_args_list[1].args[1]
    assert inject_params["threadId"] == "thread-new"
    assert "HANDOFF_CONTEXT" in inject_params["items"][0]["text"]
    assert "Use this context" in inject_params["items"][0]["text"]
    assert store.get("C1").codex_thread_id == "thread-new"
    assert store.get_handoff("handoff-1").target_codex_thread_id == "thread-new"


# ====================================================================== cat trigger


async def test_start_posts_hello_animation(
    store, fake_proc, post, post_animated, post_animated_calls, post_calls
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        post_animated=post_animated,
    )

    with patch("codex_rc.service.kill_session"), patch(
        "codex_rc.service.create_tail_session"
    ):
        await cs.start()

    assert post_animated_calls, "hello animation should run on start"
    channel_id, frames, kw = post_animated_calls[0]
    assert channel_id == "C1"
    assert frames == ["h", "he", "hel", "hello"]
    assert kw["interval_s"] == 1.0
    assert kw["loop"] is False

    embed = post_calls[-1][1].get("embeds", [])[0]
    assert embed["title"] == "🟢 Codex session started"
    assert embed["description"] == f"**Project**: `{fake_proc.project_path}`"


CATS_JSON = {
    "frame_interval_s": 0.01,
    "cats": [{"name": "tiny", "frames": ["😺 1", "😺 2"]}],
}


@pytest.fixture
def cats_file(tmp_path: Path) -> Path:
    import json
    p = tmp_path / "cats.json"
    p.write_text(json.dumps(CATS_JSON))
    return p


async def test_maybe_cat_trigger_fires_on_turn_started(
    store, fake_proc, post, post_animated, post_animated_calls, cats_file
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        post_animated=post_animated, cats_path=cats_file,
    )
    cs._maybe_cat_trigger({"method": "turn/started", "params": {"turn": {"id": "u1"}}})
    # Cat task is fire-and-forget; give it a tick.
    await asyncio.sleep(0.05)
    assert post_animated_calls, "cat animator should have been invoked"
    channel_id, frames, _kwargs = post_animated_calls[0]
    assert channel_id == "C1"
    assert frames == ["😺 1", "😺 2"]


async def test_maybe_cat_trigger_idempotent_per_turn(
    store, fake_proc, post, post_animated, post_animated_calls, cats_file
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        post_animated=post_animated, cats_path=cats_file,
    )
    cs._maybe_cat_trigger({"method": "turn/started", "params": {"turn": {"id": "u1"}}})
    cs._maybe_cat_trigger({"method": "turn/started", "params": {"turn": {"id": "u1"}}})
    await asyncio.sleep(0.05)
    assert len(post_animated_calls) == 1


async def test_maybe_cat_trigger_skipped_without_post_animated(
    store, fake_proc, post, post_animated_calls, cats_file
) -> None:
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        post_animated=None, cats_path=cats_file,
    )
    cs._maybe_cat_trigger({"method": "turn/started", "params": {"turn": {"id": "u1"}}})
    await asyncio.sleep(0.05)
    assert not post_animated_calls


async def test_turn_completed_cancels_cat(
    store, fake_proc, post, post_animated, post_animated_calls, cats_file
) -> None:
    """The cat task should be cancelled when turn/completed arrives — the
    animator (a long-running infinite loop) would otherwise keep going."""
    import json
    long_cats = {
        "frame_interval_s": 0.5,
        "cats": [{"name": "long", "frames": ["a", "b", "c"]}],
    }
    p = cats_file
    p.write_text(json.dumps(long_cats))

    store.register(channel_id="C1", project_path=str(fake_proc.project_path))

    # An animator that actually loops so cancel is observable.
    cancelled = asyncio.Event()
    async def slow_post_animated(channel_id, frames, *, interval_s=1.0, loop=True):
        try:
            await asyncio.Event().wait()  # blocks until cancelled
        except asyncio.CancelledError:
            cancelled.set()
            raise

    cs = make_channel_service(
        store=store, proc=fake_proc, post=post,
        post_animated=slow_post_animated, cats_path=p,
    )
    cs._maybe_cat_trigger({"method": "turn/started", "params": {"turn": {"id": "u1"}}})
    await asyncio.sleep(0.05)
    cs._maybe_cat_trigger({"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u1"}}})
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)


# ====================================================================== crash recovery

def _embed_titles(post_calls: list[tuple[str, dict]]) -> list[str]:
    """DiscordPayload renders embeds as the plural `embeds: [...]` field."""
    titles: list[str] = []
    for _, p in post_calls:
        for e in p.get("embeds", []):
            titles.append(e.get("title", ""))
    return titles


async def test_on_codex_crash_swaps_proc_and_resumes_thread(
    monkeypatch, store, fake_proc, post, post_calls, tmp_path
) -> None:
    """Crash callback should swap in a fresh proc, resume the last thread,
    and post both a crash and an auto-recovered embed to the channel."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-X")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    new_proc = FakeProc(
        project_path=fake_proc.project_path,
        pretty_path=tmp_path / "events2.txt",
        log_dir=tmp_path / "logs2",
    )
    monkeypatch.setattr(
        "codex_rc.service.CodexServerProcess",
        lambda **_kw: new_proc,
    )

    await cs._on_codex_crash(returncode=139)

    assert cs.proc is new_proc
    sess = store.get("C1")
    assert sess and sess.state == "running"
    methods = [c.args[0] for c in new_proc.request.call_args_list]
    assert "thread/resume" in methods
    titles = _embed_titles(post_calls)
    assert any("crashed" in t.lower() for t in titles)
    assert any("auto-recovered" in t.lower() for t in titles)
    new_proc.set_on_child_exit.assert_called_once()
    # Drain the new pump task so the event loop doesn't warn.
    if cs._pump_task and not cs._pump_task.done():
        cs._pump_task.cancel()
        try:
            await cs._pump_task
        except (asyncio.CancelledError, Exception):
            pass


async def test_on_codex_crash_marks_error_when_restart_fails(
    monkeypatch, store, fake_proc, post, post_calls
) -> None:
    """If the fresh proc can't start, channel transitions to 'error' state
    and the operator is told how to restart manually."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    class FailingProc:
        async def start(self):
            raise RuntimeError("ws port already in use")

        async def stop(self):
            pass

    monkeypatch.setattr(
        "codex_rc.service.CodexServerProcess",
        lambda **_kw: FailingProc(),
    )

    await cs._on_codex_crash(returncode=137)

    sess = store.get("C1")
    assert sess and sess.state == "error"
    titles = _embed_titles(post_calls)
    assert any("auto-recovery failed" in t.lower() for t in titles)


async def test_on_codex_crash_short_circuits_when_recovery_in_progress(
    monkeypatch, store, fake_proc, post, post_calls
) -> None:
    """A second crash arriving while recovery is already running must
    early-return — no duplicate channel embeds, no duplicate proc swap.
    Simulated by setting the flag directly; the real flag is owned by
    the first arrival's stack frame between the guard check and the
    finally block."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._recovery_in_progress = True

    monkeypatch.setattr(
        "codex_rc.service.CodexServerProcess",
        lambda **_kw: pytest.fail("recovery should not have been re-entered"),
    )
    await cs._on_codex_crash(returncode=42)

    assert post_calls == []
    # Flag was not flipped by the no-op call.
    assert cs._recovery_in_progress is True


async def test_on_codex_crash_skipped_after_intentional_stop(
    monkeypatch, store, fake_proc, post, post_calls
) -> None:
    """A late transport EOF after stop() should not trigger recovery."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    cs._intentional_stop = True

    monkeypatch.setattr(
        "codex_rc.service.CodexServerProcess",
        lambda **_kw: pytest.fail("recovery should not have started"),
    )

    await cs._on_codex_crash(returncode=137)

    titles = _embed_titles(post_calls)
    assert not any("crashed" in t.lower() for t in titles)


async def test_silent_transport_eof_triggers_recovery(
    monkeypatch, store, fake_proc, post, post_calls, tmp_path
) -> None:
    """When the pump's notifications iterator finishes naturally without
    a CancelledError or a planned stop, _on_codex_crash should fire on
    behalf of the WS/transport drop."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    new_proc = FakeProc(
        project_path=fake_proc.project_path,
        pretty_path=tmp_path / "events2.txt",
        log_dir=tmp_path / "logs2",
    )
    monkeypatch.setattr(
        "codex_rc.service.CodexServerProcess",
        lambda **_kw: new_proc,
    )

    # Start the pump, then close the notifications stream to mimic transport
    # EOF while the child appears alive (no _on_child_exit callback fired).
    cs._pump_task = asyncio.create_task(cs._pump(), name="test-pump")
    await fake_proc.emit({"method": "irrelevant", "params": {}})
    await asyncio.sleep(0.01)
    await fake_proc._notif_queue.put(None)  # terminate iterator naturally
    # Wait for the pump to finish and the spawned crash task to settle.
    try:
        await asyncio.wait_for(cs._pump_task, timeout=1.0)
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.05)

    titles = _embed_titles(post_calls)
    assert any("crashed" in t.lower() for t in titles)
    # Crash description should mention the "still appeared to be alive" branch.
    descs = []
    for _, p in post_calls:
        for e in p.get("embeds", []):
            descs.append(e.get("description", ""))
    assert any("appeared to still be alive" in d for d in descs)
    if cs._pump_task and not cs._pump_task.done():
        cs._pump_task.cancel()
        try:
            await cs._pump_task
        except (asyncio.CancelledError, Exception):
            pass


async def test_operator_instructions_injected_via_inject_items_when_rpc_succeeds(
    monkeypatch, store, fake_proc, post
) -> None:
    """Happy path: thread/inject_items accepts the payload → no fallback
    prefix queued."""
    monkeypatch.setattr(
        "codex_rc.service._load_operator_instructions",
        lambda: OperatorInstructions(soul="ego here", rules="rules here"),
    )
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.side_effect = [
        {"thread": {"id": "thread-x"}},  # thread/start
        {},                                # thread/inject_items
    ]
    await cs._start_codex_thread(store.get("C1"))
    methods = [c.args[0] for c in fake_proc.request.call_args_list]
    assert methods == ["thread/start", "thread/inject_items"]
    inject_params = fake_proc.request.call_args_list[1].args[1]
    assert inject_params["threadId"] == "thread-x"
    assert [item["role"] for item in inject_params["items"]] == ["system", "system"]
    assert "ego here" in inject_params["items"][0]["text"]
    assert "<<SOUL" in inject_params["items"][0]["text"]
    assert "rules here" in inject_params["items"][1]["text"]
    assert "<<RULES" in inject_params["items"][1]["text"]
    assert cs._pending_instruction_prefix is None


def test_load_soul_text_reads_only_soul_file(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    soul.write_text("ego\n", encoding="utf-8")
    fragments = tmp_path / "soul.d"
    fragments.mkdir()
    (fragments / "20-karpathy.md").write_text("karpathy rules\n", encoding="utf-8")

    assert _load_soul_text_from(soul) == "ego"


def test_load_rules_text_appends_rules_d_fragments_in_name_order(tmp_path: Path) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("base rules\n", encoding="utf-8")
    fragments = tmp_path / "rules.d"
    fragments.mkdir()
    (fragments / "20-karpathy.md").write_text("karpathy rules\n", encoding="utf-8")
    (fragments / "10-discord.md").write_text("discord rules\n", encoding="utf-8")
    (fragments / "notes.txt").write_text("ignored\n", encoding="utf-8")

    assert _load_rules_text_from(rules, fragments) == (
        "base rules\n\n"
        "discord rules\n\n"
        "karpathy rules"
    )


def test_load_rules_text_allows_fragment_only_layout(tmp_path: Path) -> None:
    fragments = tmp_path / "rules.d"
    fragments.mkdir()
    (fragments / "20-karpathy.md").write_text("karpathy rules\n", encoding="utf-8")

    assert _load_rules_text_from(None, fragments) == "karpathy rules"


def test_load_rules_text_prefers_config_rules_over_legacy_soul_d(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_rules = tmp_path / "data" / "config" / "rules.d"
    config_rules.mkdir(parents=True)
    (config_rules / "10-new.md").write_text("new rules\n", encoding="utf-8")
    legacy_rules = tmp_path / "data" / "soul.d"
    legacy_rules.mkdir(parents=True)
    (legacy_rules / "10-legacy.md").write_text("legacy rules\n", encoding="utf-8")

    assert _load_rules_text() == "new rules"


def test_load_rules_text_uses_legacy_soul_d_when_no_config_rules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    legacy_rules = tmp_path / "data" / "soul.d"
    legacy_rules.mkdir(parents=True)
    (legacy_rules / "10-legacy.md").write_text("legacy rules\n", encoding="utf-8")

    assert _load_rules_text() == "legacy rules"


async def test_operator_instructions_fall_back_to_first_turn_prefix_when_inject_fails(
    monkeypatch, store, fake_proc, post
) -> None:
    """If the codex build rejects thread/inject_items, the soul text is
    queued and prefixed onto the first send_text input."""
    monkeypatch.setattr(
        "codex_rc.service._load_operator_instructions",
        lambda: OperatorInstructions(soul="ego here", rules="rules here"),
    )
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    async def request_side(method, params=None, **_kw):
        if method == "thread/start":
            return {"thread": {"id": "thread-x"}}
        if method == "thread/inject_items":
            raise RuntimeError("method not found")
        return {}

    fake_proc.request.side_effect = request_side

    await cs._start_codex_thread(store.get("C1"))
    assert cs._pending_instruction_prefix is not None
    # First send_text now carries the instruction prefix in the input list.
    fake_proc.request.side_effect = None
    fake_proc.request.return_value = {}
    await cs.send_text("hello")
    turn_params = fake_proc.request.call_args.args[1]
    first_text = turn_params["input"][0]["text"]
    assert "ego here" in first_text
    assert "rules here" in first_text
    assert "<<SOUL" in first_text
    assert "<<RULES" in first_text
    assert cs._pending_instruction_prefix is None


async def test_operator_instructions_skipped_when_empty(
    monkeypatch, store, fake_proc, post
) -> None:
    monkeypatch.setattr("codex_rc.service._load_soul_text", lambda: None)
    monkeypatch.setattr("codex_rc.service._load_rules_text", lambda: None)
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)
    fake_proc.request.return_value = {"thread": {"id": "thread-x"}}
    await cs._start_codex_thread(store.get("C1"))
    methods = [c.args[0] for c in fake_proc.request.call_args_list]
    assert methods == ["thread/start"]
    assert cs._pending_instruction_prefix is None


async def test_on_codex_crash_without_prior_thread_starts_fresh(
    monkeypatch, store, fake_proc, post, post_calls, tmp_path
) -> None:
    """A channel that never started a thread before should still recover,
    just without attempting thread/resume."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    new_proc = FakeProc(
        project_path=fake_proc.project_path,
        pretty_path=tmp_path / "events2.txt",
        log_dir=tmp_path / "logs2",
    )
    monkeypatch.setattr(
        "codex_rc.service.CodexServerProcess",
        lambda **_kw: new_proc,
    )

    await cs._on_codex_crash(returncode=None)

    methods = [c.args[0] for c in new_proc.request.call_args_list]
    assert "thread/resume" not in methods
    sess = store.get("C1")
    assert sess and sess.state == "running"
    if cs._pump_task and not cs._pump_task.done():
        cs._pump_task.cancel()
        try:
            await cs._pump_task
        except (asyncio.CancelledError, Exception):
            pass


# ============================================================== workflow guards
#
# These cover the two bugs surfaced by the 2026-05-15 17:49 log:
#
#   1. ``turn/started`` on a workflow ephemeral thread used to clobber
#      ``_active_turn_id`` on the main channel session. The next user
#      message took the ``turn/steer`` branch in ``send_text`` and codex
#      rejected with ``no active turn to steer`` because the main thread
#      had no live turn — the steer was implicitly targeting the wrong
#      thread.
#
#   2. ``send_text`` had no awareness of workflow execution being
#      in-flight, so the raw RpcError above propagated to the Discord
#      bot's exception handler with no user-visible feedback.
#

async def test_update_turn_state_ignores_workflow_ephemeral_turn_started(
    store, fake_proc, post,
) -> None:
    """``_active_turn_id`` is the main-thread's turn id (drives steer and
    ✋-interrupt). A ``turn/started`` on the workflow ephemeral thread
    must not poison it — otherwise the next user message issues
    ``turn/steer`` with a turnId codex can't match to the main thread.
    """
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    cs._workflow_turn_wait = _TurnCompletionWait(
        thread_id="ephemeral-stage-3", future=asyncio.get_running_loop().create_future()
    )
    try:
        await cs._update_turn_state({
            "method": "turn/started",
            "params": {
                "threadId": "ephemeral-stage-3",
                "turn": {"id": "ephemeral-turn"},
            },
        })

        assert cs._active_turn_id is None
    finally:
        cs._workflow_turn_wait = None


async def test_update_turn_state_still_tracks_main_thread_turn(
    store, fake_proc, post,
) -> None:
    """Mirror of the guard above: a ``turn/started`` on a thread that
    isn't the workflow ephemeral must still set ``_active_turn_id`` so
    steer / interrupt continue to work for the main channel session."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    cs._workflow_turn_wait = _TurnCompletionWait(
        thread_id="ephemeral-stage-3", future=asyncio.get_running_loop().create_future()
    )
    try:
        await cs._update_turn_state({
            "method": "turn/started",
            "params": {
                "threadId": "main-thread",
                "turn": {"id": "main-turn"},
            },
        })

        assert cs._active_turn_id == "main-turn"
    finally:
        cs._workflow_turn_wait = None


async def test_send_text_rejects_while_workflow_executing(
    store, fake_proc, post, post_calls,
) -> None:
    """During workflow execution the user's main channel must be locked:
    send_text posts a Discord warning and does NOT issue ``turn/start``
    or ``turn/steer`` on the main thread."""
    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-main")
    cs = make_channel_service(store=store, proc=fake_proc, post=post)

    # Mimic a workflow stage being in-flight: the execution task is
    # alive and a stage is waiting on its ephemeral thread.
    cs._workflow_turn_wait = _TurnCompletionWait(
        thread_id="ephemeral-stage-3", future=asyncio.get_running_loop().create_future()
    )

    async def _never_completes() -> None:
        await asyncio.sleep(3600)

    cs._workflow_execution_task = asyncio.create_task(_never_completes())
    try:
        await cs.send_text("hello mid workflow")

        # No turn/start or turn/steer hit the main thread.
        methods = [c.args[0] for c in fake_proc.request.call_args_list]
        assert "turn/start" not in methods
        assert "turn/steer" not in methods

        # User got a clear Discord warning.
        titles: list[str] = []
        for _channel, payload in post_calls:
            for embed in payload.get("embeds") or []:
                titles.append(str(embed.get("title", "")))
        assert any("Workflow in progress" in t for t in titles), titles
    finally:
        cs._workflow_execution_task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await cs._workflow_execution_task
        cs._workflow_execution_task = None
        cs._workflow_turn_wait = None


async def test_workflow_stage_falls_back_on_timeout(
    monkeypatch, store, fake_proc, post, post_calls,
) -> None:
    """If a stage runs past WORKFLOW_STAGE_COMPLETION_TIMEOUT_SECONDS
    (degenerate Spark output loop with no turn/completed), the stage
    must (a) issue turn/interrupt on the stuck ephemeral, (b) retry on
    the fallback model with a fresh ephemeral thread, and (c) post a
    ``Workflow model fallback`` warning citing the timeout.
    """
    monkeypatch.setattr(
        "codex_rc.service.WORKFLOW_STAGE_COMPLETION_TIMEOUT_SECONDS",
        0.05,
    )

    store.register(channel_id="C1", project_path=str(fake_proc.project_path))
    store.update_thread("C1", "thread-1")
    cs = make_channel_service(
        store=store,
        proc=fake_proc,
        post=post,
        workflow_enabled=True,
    )

    def completion(params, _calls):
        # Spark turn never completes → timeout. Fallback completes.
        if params.get("model") == WORKFLOW_SPARK_MODEL:
            return "no-completion"
        return None

    calls: list[tuple[str, dict]] = []
    ephemeral_counter = {"n": 0}
    turn_counter = {"n": 0}

    async def request_side_effect(method, params=None, **_kw):
        params = dict(params or {})
        calls.append((method, params))
        if method == "thread/start":
            ephemeral_counter["n"] += 1
            return {"thread": {"id": f"eph-{ephemeral_counter['n']}"}}
        if method == "turn/start":
            turn_counter["n"] += 1
            turn_id = f"turn-{turn_counter['n']}"
            thread_id = params.get("threadId", "")
            outcome = completion(params, calls)

            async def deliver() -> None:
                if outcome == "no-completion":
                    return  # never complete → timeout in waiter
                await asyncio.sleep(0)
                cs._observe_workflow_turn_completion({
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": {"id": turn_id}},
                })

            asyncio.create_task(deliver())
            return {"turn": {"id": turn_id}}
        if method == "turn/interrupt":
            return {}
        return {}

    fake_proc.request.side_effect = request_side_effect

    turn = WorkflowExecutionTurn(
        stage=3,
        title="Implementation",
        prompt="implement",
        model=WORKFLOW_SPARK_MODEL,
        fallback_model=WORKFLOW_SPARK_FALLBACK_MODEL,
    )

    await cs._run_workflow_stage_turn(turn)

    methods = [m for m, _ in calls]
    # spark thread + spark turn → timeout interrupt → fresh thread + fallback turn
    assert methods.count("thread/start") == 2
    assert methods.count("turn/start") == 2
    assert "turn/interrupt" in methods
    turn_models = [p.get("model") for m, p in calls if m == "turn/start"]
    assert turn_models == [WORKFLOW_SPARK_MODEL, WORKFLOW_SPARK_FALLBACK_MODEL]

    fallback_payloads = [
        payload for _, payload in post_calls if "Workflow model fallback" in str(payload)
    ]
    assert fallback_payloads
    assert "timeout" in str(fallback_payloads[-1]).lower()
