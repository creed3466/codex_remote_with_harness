"""Tests for the workflow stage handoff document layer.

The handoff layer is the *only* conduit between workflow stages once each
stage runs on its own ephemeral codex thread. These tests pin down the
contract: a stage writes a markdown file with a known set of H2 sections,
the next stage parses it back into a typed dataclass, and missing or
malformed sections surface as a structured ``HandoffParseError`` so the
service layer can retry once before failing the workflow.
"""

from __future__ import annotations

import pytest

from codex_rc.workflow_handoff import (
    FileChange,
    FinalSummary,
    HandoffParseError,
    Stage1To2Handoff,
    Stage2To3Handoff,
    Stage3To4Handoff,
    parse_final_summary,
    parse_markdown_doc,
    parse_stage_1_to_2,
    parse_stage_2_to_3,
    parse_stage_3_to_4,
)


def test_parse_markdown_doc_splits_h1_and_h2_sections() -> None:
    doc = parse_markdown_doc(
        """
        # Title text

        Intro paragraph that doesn't belong to a section.

        ## First Section

        body of first
        with multiple lines

        ## Second Section

        body of second
        """
    )

    assert doc.title == "Title text"
    assert doc.sections["First Section"].strip().startswith("body of first")
    assert "multiple lines" in doc.sections["First Section"]
    assert doc.sections["Second Section"].strip().startswith("body of second")


def test_parse_markdown_doc_handles_no_title() -> None:
    doc = parse_markdown_doc(
        """
        ## Only A Section

        body
        """
    )

    assert doc.title == ""
    assert "body" in doc.sections["Only A Section"]


def test_parse_stage_3_to_4_extracts_required_fields() -> None:
    md = """
    # Verification Brief — wf-abc

    ## Task
    add retry handling to the cron worker

    ## Approved plan
    A: Inline retry decorator

    ## What was implemented
    Added a retry decorator and applied it to the two cron entry points.

    ## Files changed
    - src/cron/worker.py (M): wrapped run() with retry decorator
    - src/cron/retry.py (A): new exponential-backoff retry decorator
    - tests/test_retry.py (A): 4 test cases covering success, failure, exhaustion

    ## Tests added/modified
    - tests/test_retry.py::test_retries_until_success
    - tests/test_retry.py::test_gives_up_after_max_attempts

    ## Out of scope / deferred
    - Metric emission: punted to follow-up because instrumentation lives elsewhere

    ## Verification checklist
    - [ ] `pytest tests/test_retry.py -v` — green
    - [ ] `mypy src/cron/` — clean
    - [ ] Acceptance: cron worker retries transient failures

    ## Risks / scrutiny areas
    - Backoff jitter only tested with a fixed seed; verify non-deterministic path

    ## Output budget for stage 4
    ≤ 1500 tokens.
    """

    handoff = parse_stage_3_to_4(md)

    assert handoff.workflow_id == "wf-abc"
    assert handoff.original_task == "add retry handling to the cron worker"
    assert handoff.selected_plan == "A"
    assert handoff.plan_title == "Inline retry decorator"
    assert "retry decorator" in handoff.what_was_implemented
    assert handoff.files_changed == (
        FileChange(
            path="src/cron/worker.py",
            kind="M",
            summary="wrapped run() with retry decorator",
        ),
        FileChange(
            path="src/cron/retry.py",
            kind="A",
            summary="new exponential-backoff retry decorator",
        ),
        FileChange(
            path="tests/test_retry.py",
            kind="A",
            summary="4 test cases covering success, failure, exhaustion",
        ),
    )
    assert "tests/test_retry.py::test_retries_until_success" in handoff.tests_added
    assert handoff.out_of_scope[0].startswith("Metric emission")
    assert any(
        "pytest tests/test_retry.py" in item for item in handoff.verification_checklist
    )
    assert handoff.risks[0].startswith("Backoff jitter")


def test_parse_stage_3_to_4_rejects_missing_required_section() -> None:
    md = """
    # Verification Brief — wf-abc

    ## Task
    do thing

    ## Approved plan
    A: Some plan

    ## What was implemented
    did thing

    ## Files changed
    - src/x.py (M): updated

    ## Verification checklist
    - [ ] `pytest` — green
    """

    with pytest.raises(HandoffParseError) as excinfo:
        parse_stage_3_to_4(md)
    msg = str(excinfo.value).lower()
    # Missing: Tests added/modified, Out of scope, Risks, Output budget
    assert "tests added" in msg or "risks" in msg or "missing" in msg


def test_parse_stage_3_to_4_files_changed_tolerates_blank_lines() -> None:
    md = """
    # Verification Brief — wf-xyz

    ## Task
    t

    ## Approved plan
    B: Other plan

    ## What was implemented
    w

    ## Files changed
    - src/a.py (M): a

    - src/b.py (A): b


    ## Tests added/modified
    - tests/test_a.py::test_a

    ## Out of scope / deferred
    - None

    ## Verification checklist
    - [ ] `pytest` — green

    ## Risks / scrutiny areas
    - None

    ## Output budget for stage 4
    ≤ 1500 tokens.
    """

    handoff = parse_stage_3_to_4(md)
    assert [fc.path for fc in handoff.files_changed] == ["src/a.py", "src/b.py"]


def test_parse_stage_3_to_4_rejects_unknown_file_change_kind() -> None:
    md = """
    # Verification Brief — wf-bad

    ## Task
    t

    ## Approved plan
    A: p

    ## What was implemented
    w

    ## Files changed
    - src/a.py (Z): bad kind

    ## Tests added/modified
    - none

    ## Out of scope / deferred
    - None

    ## Verification checklist
    - [ ] x

    ## Risks / scrutiny areas
    - None

    ## Output budget for stage 4
    ≤ 1500 tokens.
    """

    with pytest.raises(HandoffParseError):
        parse_stage_3_to_4(md)


def test_stage_3_to_4_round_trip_via_to_markdown() -> None:
    handoff = Stage3To4Handoff(
        workflow_id="wf-rt",
        original_task="add feature X",
        selected_plan="B",
        plan_title="Controller-based path",
        what_was_implemented="Implemented controller and wired into router.",
        files_changed=(
            FileChange(path="src/ctrl.py", kind="A", summary="new controller"),
            FileChange(path="src/router.py", kind="M", summary="register controller"),
        ),
        tests_added=(
            "tests/test_ctrl.py::test_handles_happy_path",
            "tests/test_ctrl.py::test_rejects_invalid_input",
        ),
        out_of_scope=("metrics: not in this slice",),
        verification_checklist=(
            "`pytest tests/test_ctrl.py -v` — green",
            "Acceptance: requests route through controller",
        ),
        risks=("Input validation uses regex; verify non-ASCII inputs",),
        output_budget_tokens=1500,
    )

    md = handoff.to_markdown()
    parsed = parse_stage_3_to_4(md)

    assert parsed == handoff


# ======================================================================
# Stage 1 → 2 (Analysis → Design): bot-written but symmetric so resume /
# inspection paths can parse it the same way as the others.
# ======================================================================


def test_stage_1_to_2_round_trip() -> None:
    handoff = Stage1To2Handoff(
        workflow_id="wf-12",
        original_task="add retry handling to the cron worker",
        selected_plan="A",
        plan_title="Inline retry decorator",
        plan_summary=(
            "Wrap each cron entry point in a small retry decorator that "
            "lives next to the worker."
        ),
        selection_rationale=(
            "Smaller surface area than a controller; no new module boundary."
        ),
        trade_offs=(
            "No central retry policy — duplicated config if more entry points appear",
            "Doesn't help non-cron callers",
        ),
        hints=(
            "Touch src/cron/worker.py and add src/cron/retry.py",
            "Test seam already exists at tests/test_cron.py",
        ),
        output_budget_tokens=3000,
    )

    md = handoff.to_markdown()
    parsed = parse_stage_1_to_2(md)

    assert parsed == handoff


def test_parse_stage_1_to_2_rejects_missing_plan_summary() -> None:
    md = """
    # Plan Brief — wf-12

    ## Task
    do thing

    ## Approved plan
    A: Some plan

    ## Selection rationale
    short reason

    ## Trade-offs
    - none

    ## Hints for design stage
    - none

    ## Output budget for stage 2
    ≤ 3000 tokens.
    """

    with pytest.raises(HandoffParseError):
        parse_stage_1_to_2(md)


def test_stage_1_to_2_accepts_empty_optional_lists() -> None:
    """``trade_offs`` and ``hints`` are advisory — Stage 1 may legitimately
    have nothing to add. The parser must accept ``- None`` placeholders
    and return empty tuples rather than failing the workflow."""
    handoff = Stage1To2Handoff(
        workflow_id="wf-opt",
        original_task="t",
        selected_plan="B",
        plan_title="P",
        plan_summary="s",
        selection_rationale="r",
        trade_offs=(),
        hints=(),
        output_budget_tokens=3000,
    )

    md = handoff.to_markdown()
    parsed = parse_stage_1_to_2(md)

    assert parsed.trade_offs == ()
    assert parsed.hints == ()


# ======================================================================
# Stage 2 → 3 (Design → Implementation)
# ======================================================================


def test_stage_2_to_3_round_trip() -> None:
    handoff = Stage2To3Handoff(
        workflow_id="wf-23",
        original_task="add retry handling",
        selected_plan="A",
        plan_title="Inline retry decorator",
        architecture_decisions=(
            "Decorator lives in src/cron/retry.py — closest to caller",
            "Backoff schedule is exponential with jitter, max 5 attempts",
        ),
        files_to_modify=(
            FileChange(
                path="src/cron/worker.py",
                kind="M",
                summary="wrap run() with @retry()",
            ),
            FileChange(
                path="src/cron/retry.py",
                kind="A",
                summary="new exponential-backoff decorator",
            ),
            FileChange(
                path="tests/test_retry.py",
                kind="A",
                summary="unit tests for the decorator",
            ),
        ),
        implementation_checklist=(
            "Create src/cron/retry.py with retry() factory",
            "Wrap worker.run() with @retry()",
            "Add tests for success/failure/exhaustion",
        ),
        test_strategy=(
            "Unit: decorator behavior under mocked clock",
            "Integration: worker run() retries transient errors",
        ),
        acceptance_criteria=(
            "Cron worker retries transient failures up to 5 attempts",
            "No regression in existing worker tests",
        ),
        risks=(
            "Backoff sleep blocks event loop if used in async path",
        ),
        output_budget_tokens=2500,
    )

    md = handoff.to_markdown()
    parsed = parse_stage_2_to_3(md)

    assert parsed == handoff


def test_parse_stage_2_to_3_rejects_empty_checklist() -> None:
    """A design with no implementation steps means stage 3 has nothing to
    do — the parser must surface that as a structured error so the
    workflow can retry stage 2 rather than silently moving on."""
    md = """
    # Design Brief — wf-23

    ## Task
    t

    ## Approved plan
    A: P

    ## Architecture decisions
    - decision

    ## Files to modify / create
    - src/x.py (M): touch

    ## Implementation checklist (in order)
    (none)

    ## Test strategy
    - approach

    ## Acceptance criteria
    - [ ] criterion

    ## Risks / open questions
    - risk

    ## Output budget for stage 3
    ≤ 2500 tokens.
    """

    with pytest.raises(HandoffParseError):
        parse_stage_2_to_3(md)


def test_parse_stage_2_to_3_parses_numbered_checklist() -> None:
    """Numbered ordered lists (``1.`` / ``2.``) are the natural shape for
    an in-order checklist; the parser must strip the numbering and return
    the items in document order."""
    md = """
    # Design Brief — wf-num

    ## Task
    t

    ## Approved plan
    A: P

    ## Architecture decisions
    - d

    ## Files to modify / create
    - src/x.py (M): touch

    ## Implementation checklist (in order)
    1. step one
    2. step two
    3. step three

    ## Test strategy
    - s

    ## Acceptance criteria
    - [ ] c

    ## Risks / open questions
    - r

    ## Output budget for stage 3
    ≤ 2500 tokens.
    """

    parsed = parse_stage_2_to_3(md)
    assert parsed.implementation_checklist == (
        "step one",
        "step two",
        "step three",
    )


# ======================================================================
# Stage 4 → User: final summary that becomes the Discord message and the
# thread/inject_items payload back to the user's main thread.
# ======================================================================


def test_final_summary_round_trip() -> None:
    summary = FinalSummary(
        workflow_id="wf-final",
        what_was_asked="add retry handling to the cron worker",
        what_was_done=(
            "Added an exponential-backoff retry decorator in src/cron/retry.py "
            "and applied it to worker.run(). Tests cover success, failure, and "
            "exhaustion paths."
        ),
        verification_results=(
            "✅ `pytest tests/test_retry.py -v` — 4 passed",
            "✅ `mypy src/cron/` — clean",
            "⚠️ Manual: non-deterministic jitter path not exercised",
        ),
        files_changed=(
            "src/cron/retry.py",
            "src/cron/worker.py",
            "tests/test_retry.py",
        ),
        caveats=(
            "Backoff jitter path needs a follow-up integration test",
        ),
    )

    md = summary.to_markdown()
    parsed = parse_final_summary(md)

    assert parsed == summary


def test_parse_final_summary_accepts_empty_caveats() -> None:
    summary = FinalSummary(
        workflow_id="wf-clean",
        what_was_asked="t",
        what_was_done="done",
        verification_results=("✅ pytest — green",),
        files_changed=("src/x.py",),
        caveats=(),
    )

    md = summary.to_markdown()
    parsed = parse_final_summary(md)

    assert parsed.caveats == ()


def test_parse_final_summary_rejects_missing_verification_results() -> None:
    md = """
    # Workflow Result — wf-bad

    ## What was asked
    t

    ## What was done
    d

    ## Files changed
    - src/x.py

    ## Caveats / follow-ups
    - None
    """

    with pytest.raises(HandoffParseError):
        parse_final_summary(md)
