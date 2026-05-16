from __future__ import annotations

import pytest

from codex_rc.approval_router import STYLE_DANGER, STYLE_SECONDARY
from codex_rc.workflow_router import (
    WORKFLOW_DESIGN_EFFORT,
    WORKFLOW_DESIGN_MODEL,
    WORKFLOW_SPARK_FALLBACK_MODEL,
    WORKFLOW_SPARK_MODEL,
    WORKFLOW_VERIFICATION_EFFORT,
    WORKFLOW_VERIFICATION_MODEL,
    DevelopmentWorkflowRouter,
    extract_plan_metadata,
    parse_workflow_custom_id,
    should_start_development_workflow,
    strip_workflow_metadata,
)


def test_extract_plan_metadata_from_machine_block() -> None:
    meta = extract_plan_metadata(
        """
        analysis...

        CODEX_RC_WORKFLOW_OPTIONS:
        A: Minimal service hook
        B: Persistent workflow controller
        RECOMMENDED: B
        END_CODEX_RC_WORKFLOW_OPTIONS
        """
    )

    assert meta.parsed is True
    assert meta.plan_a_title == "Minimal service hook"
    assert meta.plan_b_title == "Persistent workflow controller"
    assert meta.recommended == "B"


def test_parse_workflow_custom_id_rejects_non_workflow() -> None:
    assert parse_workflow_custom_id("codex_rc:abcd:accept") is None
    assert parse_workflow_custom_id("other:wf:abcd:approve_a") is None


def test_strip_workflow_metadata_removes_machine_block() -> None:
    visible = strip_workflow_metadata(
        """
        Plan A: small.
        Plan B: big.

        CODEX_RC_WORKFLOW_OPTIONS:
        A: Small
        B: Big
        RECOMMENDED: A
        END_CODEX_RC_WORKFLOW_OPTIONS
        """
    )

    assert "Plan A: small." in visible
    assert "CODEX_RC_WORKFLOW_OPTIONS" not in visible


def test_workflow_begin_wraps_user_task() -> None:
    router = DevelopmentWorkflowRouter(enabled=True)
    prompt = router.begin("add retry handling")

    assert "CODEX_RC_DEVELOPMENT_WORKFLOW" in prompt
    assert "GPT Self Plan" in prompt
    assert "add retry handling" in prompt
    assert "Stop after step 1" in prompt


@pytest.mark.parametrize(
    "text",
    [
        "이 함수 뭐해?",
        "workflow는 어디서 켜져?",
        "구현 방식 설명해줘",
        "수정하지 말고 원인만 알려줘",
        "what does this router do?",
        "explain the current implementation",
    ],
)
def test_should_start_development_workflow_allows_information_requests(
    text: str,
) -> None:
    assert should_start_development_workflow(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "로그 포맷 개선",
        "workflow gate 구현해줘",
        "버튼 라벨 수정해주세요",
        "add retry handling",
        "fix the failing workflow test",
        "refactor the approval router",
    ],
)
def test_should_start_development_workflow_detects_implementation_requests(
    text: str,
) -> None:
    assert should_start_development_workflow(text) is True


def test_workflow_begin_passes_information_requests_through() -> None:
    router = DevelopmentWorkflowRouter(enabled=True)
    prompt = router.begin("workflow는 어디서 켜져?")

    assert prompt == "workflow는 어디서 켜져?"
    assert router.observe_notification({"method": "turn/completed", "params": {}}) is None


def test_workflow_posts_approval_prompt_after_recommendation_turn() -> None:
    router = DevelopmentWorkflowRouter(enabled=True)
    router.begin("add feature")
    assert router.observe_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "Plan A ...\nPlan B ...\n"
                        "CODEX_RC_WORKFLOW_OPTIONS:\n"
                        "A: Small patch\n"
                        "B: Controller\n"
                        "RECOMMENDED: A\n"
                        "END_CODEX_RC_WORKFLOW_OPTIONS"
                    ),
                }
            },
        }
    ) is None

    prompt = router.observe_notification({"method": "turn/completed", "params": {}})

    assert prompt is not None
    assert prompt.embed.footer_text
    assert prompt.embed.title == "Select Implementation Path"
    assert prompt.embed.color == 0x202123
    assert "Recommended path: **Plan A**" in (prompt.embed.description or "")
    assert len(prompt.buttons) == 3
    assert prompt.buttons[0].label == "Recommended · Plan A · Small patch"
    assert prompt.buttons[0].style == STYLE_SECONDARY
    # Must be a real Unicode emoji (⭐ U+2B50) — ◆ U+25C6 is a geometric
    # shape Discord 400s on as a button emoji.
    assert prompt.buttons[0].emoji == "⭐"
    assert prompt.buttons[1].label == "Plan B · Controller"
    assert prompt.buttons[1].style == STYLE_SECONDARY
    assert prompt.buttons[1].emoji is None
    assert prompt.buttons[2].label == "Cancel workflow"
    assert prompt.buttons[2].style == STYLE_DANGER
    assert "Small patch" in prompt.buttons[0].label
    assert "Controller" in prompt.buttons[1].label


def test_workflow_resolve_approval_returns_execution_turns_and_seed() -> None:
    """After plan approval the router emits (a) a typed seed_handoff the
    service writes to ``stage_1_handoff.md`` and (b) three execution
    turns each pointing at their prior/next handoff paths so each stage
    can run on a fresh ephemeral thread."""
    router = DevelopmentWorkflowRouter(enabled=True)
    router.begin("implement workflow")
    router.observe_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "CODEX_RC_WORKFLOW_OPTIONS:\n"
                        "A: Fast path\n"
                        "B: Durable path\n"
                        "RECOMMENDED: B\n"
                        "END_CODEX_RC_WORKFLOW_OPTIONS"
                    ),
                }
            },
        }
    )
    prompt = router.observe_notification({"method": "turn/completed", "params": {}})
    assert prompt is not None

    decision = router.resolve(prompt.buttons[1].custom_id)

    assert decision is not None
    assert decision.selected_plan == "B"
    # Seed handoff carries everything the bot needs to write stage 1's md
    # without any further parsing of the agent's free-text output.
    assert decision.seed_handoff is not None
    assert decision.seed_handoff.workflow_id == decision.workflow_id
    assert decision.seed_handoff.original_task == "implement workflow"
    assert decision.seed_handoff.selected_plan == "B"
    assert decision.seed_handoff.plan_title == "Durable path"
    # Continuation prompt is gone — replaced by the per-stage prompts
    # carried on each execution turn.
    assert decision.continuation_prompt is None

    assert [turn.stage for turn in decision.execution_turns] == [2, 3, 4]
    design, impl, verify = decision.execution_turns
    assert design.model == WORKFLOW_DESIGN_MODEL
    assert design.effort == WORKFLOW_DESIGN_EFFORT
    assert impl.model == WORKFLOW_SPARK_MODEL
    assert impl.fallback_model == WORKFLOW_SPARK_FALLBACK_MODEL
    assert verify.model == WORKFLOW_VERIFICATION_MODEL
    assert verify.effort == WORKFLOW_VERIFICATION_EFFORT
    assert verify.fallback_model is None

    assert "no placeholder or empty bullets" in design.prompt
    assert "no placeholder or empty bullets" in impl.prompt

    # Every turn knows where its prior context lives and where to write
    # the next handoff. Paths are project-cwd-relative so codex tools
    # resolve them against the workspace root.
    wf = decision.workflow_id
    assert design.prior_handoff_relpath == f"data/workflow/{wf}/stage_1_handoff.md"
    assert design.next_handoff_relpath == f"data/workflow/{wf}/stage_2_handoff.md"
    assert impl.prior_handoff_relpath == f"data/workflow/{wf}/stage_2_handoff.md"
    assert impl.next_handoff_relpath == f"data/workflow/{wf}/stage_3_handoff.md"
    assert verify.prior_handoff_relpath == f"data/workflow/{wf}/stage_3_handoff.md"
    assert verify.next_handoff_relpath == f"data/workflow/{wf}/stage_4_handoff.md"

    # Prompts must instruct the agent to (1) read the prior handoff and
    # (2) write the next handoff at the correct path. We don't pin
    # exact wording, just that the paths and the read/write verbs land.
    assert design.prior_handoff_relpath in design.prompt
    assert design.next_handoff_relpath in design.prompt
    assert "Design Brief" in design.prompt  # schema title for stage 2 output
    assert impl.prior_handoff_relpath in impl.prompt
    assert impl.next_handoff_relpath in impl.prompt
    assert "Verification Brief" in impl.prompt  # schema title for stage 3 output
    assert verify.prior_handoff_relpath in verify.prompt
    assert verify.next_handoff_relpath in verify.prompt
    assert "Workflow Result" in verify.prompt  # schema title for stage 4 output


def test_workflow_rejects_new_task_while_waiting_for_approval() -> None:
    router = DevelopmentWorkflowRouter(enabled=True)
    router.begin("add first feature")
    router.observe_notification({"method": "turn/completed", "params": {}})

    with pytest.raises(RuntimeError, match="waiting for plan approval"):
        router.begin("second")
