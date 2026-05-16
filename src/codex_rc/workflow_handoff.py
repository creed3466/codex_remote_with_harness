"""Workflow stage handoff documents.

Each workflow stage runs on its own ephemeral codex thread. The only
context that travels between stages is a small markdown file written by
the previous stage and read by the next. This module owns the schema for
those files: typed dataclasses per stage transition, plus a parser that
turns the markdown back into a dataclass and surfaces missing or
malformed sections as a structured :class:`HandoffParseError`.

The generic :func:`parse_markdown_doc` produces a stage-agnostic
``MarkdownDoc`` (title + H2-keyed sections); per-stage parsers build on
top of it and only validate the fields each stage actually consumes.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

_VALID_FILE_CHANGE_KINDS = frozenset({"A", "M", "D"})


class HandoffParseError(ValueError):
    """Raised when a stage handoff markdown file is missing required
    sections or contains malformed entries.

    Callers (service layer) catch this and either retry the stage once
    with a "fix sections X/Y/Z" hint or abort the workflow with a clear
    operator message.
    """


@dataclass(slots=True, frozen=True)
class MarkdownDoc:
    """Parsed markdown: H1 title plus H2-keyed body sections.

    Section bodies preserve interior blank lines and indentation but are
    stripped of leading/trailing whitespace. Sections appear in document
    order via ``ordered_sections``.
    """

    title: str
    sections: dict[str, str]

    @property
    def ordered_sections(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.sections.items())


@dataclass(slots=True, frozen=True)
class FileChange:
    """One row of the ``## Files changed`` section.

    ``kind`` is one of ``A`` (added), ``M`` (modified), or ``D`` (deleted) —
    matching the git status shorthand the implementer is already trained on.
    ``summary`` is a single human line describing what changed.
    """

    path: str
    kind: str
    summary: str


@dataclass(slots=True, frozen=True)
class Stage1To2Handoff:
    """Analysis → Design handoff.

    Bot-written: after the user clicks a plan-selection button, the
    service constructs this from the parsed ``CODEX_RC_WORKFLOW_OPTIONS``
    block (plan A/B/recommended/titles) plus the original user task. The
    dataclass is still parsed back from markdown so the same code path
    serves crash-recovery and post-mortem inspection.

    ``trade_offs`` and ``hints`` are advisory; the writer may emit them
    as ``- None`` placeholders which parse back to empty tuples.
    """

    workflow_id: str
    original_task: str
    selected_plan: str  # "A" or "B"
    plan_title: str
    plan_summary: str
    selection_rationale: str
    trade_offs: tuple[str, ...]
    hints: tuple[str, ...]
    output_budget_tokens: int

    def to_markdown(self) -> str:
        trade_offs_block = (
            "\n".join(f"- {item}" for item in self.trade_offs) or "- None"
        )
        hints_block = "\n".join(f"- {item}" for item in self.hints) or "- None"
        sections = [
            f"# Plan Brief — {self.workflow_id}",
            "",
            "## Task",
            self.original_task,
            "",
            "## Approved plan",
            f"{self.selected_plan}: {self.plan_title}",
            "",
            "## Plan summary",
            self.plan_summary,
            "",
            "## Selection rationale",
            self.selection_rationale,
            "",
            "## Trade-offs",
            trade_offs_block,
            "",
            "## Hints for design stage",
            hints_block,
            "",
            "## Output budget for stage 2",
            f"≤ {self.output_budget_tokens} tokens.",
        ]
        return "\n".join(sections)


@dataclass(slots=True, frozen=True)
class Stage2To3Handoff:
    """Design → Implementation handoff.

    Agent-written by stage 2 (design model). Stage 3 reads this *before*
    touching any source file. The schema deliberately surfaces both the
    *plan* (architecture decisions, files to touch, ordered checklist)
    and the *evaluation hooks* (test strategy, acceptance criteria) so
    stage 3 knows what "done" looks like and stage 4 inherits a
    pre-written verification target.
    """

    workflow_id: str
    original_task: str
    selected_plan: str
    plan_title: str
    architecture_decisions: tuple[str, ...]
    files_to_modify: tuple[FileChange, ...]
    implementation_checklist: tuple[str, ...]
    test_strategy: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    risks: tuple[str, ...]
    output_budget_tokens: int

    def to_markdown(self) -> str:
        arch_block = "\n".join(f"- {d}" for d in self.architecture_decisions)
        files_block = "\n".join(
            f"- {fc.path} ({fc.kind}): {fc.summary}" for fc in self.files_to_modify
        )
        checklist_block = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(self.implementation_checklist)
        )
        test_strategy_block = "\n".join(f"- {s}" for s in self.test_strategy)
        accept_block = "\n".join(f"- [ ] {c}" for c in self.acceptance_criteria)
        risks_block = "\n".join(f"- {r}" for r in self.risks) or "- None"
        sections = [
            f"# Design Brief — {self.workflow_id}",
            "",
            "## Task",
            self.original_task,
            "",
            "## Approved plan",
            f"{self.selected_plan}: {self.plan_title}",
            "",
            "## Architecture decisions",
            arch_block,
            "",
            "## Files to modify / create",
            files_block,
            "",
            "## Implementation checklist (in order)",
            checklist_block,
            "",
            "## Test strategy",
            test_strategy_block,
            "",
            "## Acceptance criteria",
            accept_block,
            "",
            "## Risks / open questions",
            risks_block,
            "",
            "## Output budget for stage 3",
            f"≤ {self.output_budget_tokens} tokens.",
        ]
        return "\n".join(sections)


@dataclass(slots=True, frozen=True)
class FinalSummary:
    """Stage 4 → User summary.

    Becomes both (a) the user-facing Discord message and (b) the
    ``thread/inject_items`` payload that lands on the main session
    thread so the conversation stays coherent for the user's next
    request. Strings are kept short by stage 4's output budget; long
    summaries should spill into a markdown attachment rather than
    inflating this dataclass.
    """

    workflow_id: str
    what_was_asked: str
    what_was_done: str
    verification_results: tuple[str, ...]
    files_changed: tuple[str, ...]
    caveats: tuple[str, ...]

    def to_markdown(self) -> str:
        verification_block = "\n".join(f"- {r}" for r in self.verification_results)
        files_block = "\n".join(f"- {p}" for p in self.files_changed) or "- None"
        caveats_block = "\n".join(f"- {c}" for c in self.caveats) or "- None"
        sections = [
            f"# Workflow Result — {self.workflow_id}",
            "",
            "## What was asked",
            self.what_was_asked,
            "",
            "## What was done",
            self.what_was_done,
            "",
            "## Verification results",
            verification_block,
            "",
            "## Files changed",
            files_block,
            "",
            "## Caveats / follow-ups",
            caveats_block,
        ]
        return "\n".join(sections)


@dataclass(slots=True, frozen=True)
class Stage3To4Handoff:
    """Implementation → Verification handoff.

    Stage 3 (implementation) writes this file at the end of its turn. Stage
    4 (verification) reads it first thing, then runs the checklist commands
    and produces a final user-facing summary.

    Schema intentionally omits full diffs, reasoning traces, and per-file
    contents: the verifier reads those directly from the working tree when
    it needs them. What it *cannot* recover from git is the implementer's
    intent (``out_of_scope``, ``risks``) and the verification plan
    (``verification_checklist``), so those sections are required.
    """

    workflow_id: str
    original_task: str
    selected_plan: str  # "A" or "B"
    plan_title: str
    what_was_implemented: str
    files_changed: tuple[FileChange, ...]
    tests_added: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    verification_checklist: tuple[str, ...]
    risks: tuple[str, ...]
    output_budget_tokens: int

    def to_markdown(self) -> str:
        files_block = "\n".join(
            f"- {fc.path} ({fc.kind}): {fc.summary}" for fc in self.files_changed
        )
        tests_block = "\n".join(f"- {t}" for t in self.tests_added) or "- none"
        oos_block = "\n".join(f"- {item}" for item in self.out_of_scope) or "- None"
        checklist_block = (
            "\n".join(f"- [ ] {item}" for item in self.verification_checklist)
            or "- [ ] (no items)"
        )
        risks_block = "\n".join(f"- {r}" for r in self.risks) or "- None"
        sections = [
            f"# Verification Brief — {self.workflow_id}",
            "",
            "## Task",
            self.original_task,
            "",
            "## Approved plan",
            f"{self.selected_plan}: {self.plan_title}",
            "",
            "## What was implemented",
            self.what_was_implemented,
            "",
            "## Files changed",
            files_block,
            "",
            "## Tests added/modified",
            tests_block,
            "",
            "## Out of scope / deferred",
            oos_block,
            "",
            "## Verification checklist",
            checklist_block,
            "",
            "## Risks / scrutiny areas",
            risks_block,
            "",
            "## Output budget for stage 4",
            f"≤ {self.output_budget_tokens} tokens.",
        ]
        return "\n".join(sections)


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FILE_CHANGE_RE = re.compile(
    r"^-\s+(?P<path>\S+)\s+\((?P<kind>[A-Z])\):\s*(?P<summary>.+)$"
)


def parse_markdown_doc(text: str) -> MarkdownDoc:
    """Split ``text`` into an H1 title and H2 sections.

    Robust to:
    - leading indentation from triple-quoted source strings (dedented first)
    - blank lines inside section bodies
    - extra whitespace around headings
    - missing H1 (title becomes empty string)

    H3+ headings stay inline in their parent section's body — only H1 and
    H2 are structural here.
    """
    normalized = textwrap.dedent(text).strip()
    title = ""
    sections: dict[str, list[str]] = {}
    current_section: str | None = None

    for raw_line in normalized.splitlines():
        match = _HEADING_RE.match(raw_line)
        if match:
            level = len(match.group(1))
            heading_text = match.group(2).strip()
            if level == 1 and not title:
                title = heading_text
                current_section = None
                continue
            if level == 2:
                current_section = heading_text
                sections.setdefault(current_section, [])
                continue
            # H3+ stays in body
        if current_section is not None:
            sections[current_section].append(raw_line)

    return MarkdownDoc(
        title=title,
        sections={name: "\n".join(lines).strip() for name, lines in sections.items()},
    )


def _require_section(doc: MarkdownDoc, name: str) -> str:
    body = doc.sections.get(name)
    if body is None or not body.strip():
        raise HandoffParseError(f"missing or empty section: '## {name}'")
    return body


def _parse_bullet_list(body: str) -> tuple[str, ...]:
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        item = line[1:].strip()
        if item:
            items.append(item)
    return tuple(items)


def _parse_optional_bullet_list(body: str) -> tuple[str, ...]:
    """Like ``_parse_bullet_list`` but treats a single ``- None`` placeholder
    (case-insensitive) as an empty list.

    Several handoff sections (trade-offs, hints, out-of-scope, risks,
    caveats) are *advisory*: the writing stage may legitimately have
    nothing to add. To keep the markdown shape consistent the writer
    still emits a ``- None`` row, and this parser maps that back to an
    empty tuple so dataclass equality holds across round-trips.
    """
    items = _parse_bullet_list(body)
    if len(items) == 1 and items[0].lower() in {"none", "(none)"}:
        return ()
    return items


def _parse_ordered_list(body: str) -> tuple[str, ...]:
    """Parse ``1. item`` / ``2. item`` numbered rows in document order.

    The leading ``N.`` is stripped; the numbering itself is informational
    only (the tuple's index is the authoritative ordering).
    """
    pattern = re.compile(r"^\s*\d+\.\s+(.*\S)\s*$")
    items: list[str] = []
    for raw in body.splitlines():
        match = pattern.match(raw)
        if match:
            items.append(match.group(1).strip())
    return tuple(items)


def _parse_checkbox_list(body: str) -> tuple[str, ...]:
    """Parse ``- [ ] item`` and ``- [x] item`` lines, returning the item text.

    The checkbox state is *not* preserved — at handoff time everything is
    unchecked, and at verification time the verifier reports results in
    free-form prose rather than mutating the original file.
    """
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        rest = line[1:].strip()
        if rest.startswith("[ ]") or rest.startswith("[x]") or rest.startswith("[X]"):
            rest = rest[3:].strip()
        if rest:
            items.append(rest)
    return tuple(items)


def _parse_file_change_list(body: str) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        match = _FILE_CHANGE_RE.match(line)
        if not match:
            raise HandoffParseError(
                f"malformed file-change line (expected '- <path> (<A|M|D>): "
                f"<summary>'): {line!r}"
            )
        kind = match.group("kind")
        if kind not in _VALID_FILE_CHANGE_KINDS:
            raise HandoffParseError(
                f"unknown file-change kind {kind!r}; expected one of "
                f"{sorted(_VALID_FILE_CHANGE_KINDS)}"
            )
        changes.append(
            FileChange(
                path=match.group("path"),
                kind=kind,
                summary=match.group("summary").strip(),
            )
        )
    return tuple(changes)


def _parse_approved_plan(body: str) -> tuple[str, str]:
    line = body.strip().splitlines()[0] if body.strip() else ""
    if ":" not in line:
        raise HandoffParseError(
            f"approved plan must be '<A|B>: <title>'; got {line!r}"
        )
    plan, title = line.split(":", 1)
    plan = plan.strip()
    if plan not in {"A", "B"}:
        raise HandoffParseError(f"approved plan must be 'A' or 'B'; got {plan!r}")
    return plan, title.strip()


def _parse_output_budget(body: str) -> int:
    """Extract the integer token budget from text like ``≤ 1500 tokens.``"""
    match = re.search(r"(\d{2,7})", body)
    if not match:
        raise HandoffParseError(
            "output budget section must contain an integer token count"
        )
    return int(match.group(1))


def _parse_workflow_id_from_title(title: str) -> str:
    """Extract the workflow id from a title like ``Verification Brief — <id>``."""
    if "—" in title:
        return title.rsplit("—", 1)[-1].strip()
    if "-" in title:
        return title.rsplit("-", 1)[-1].strip()
    return title.strip()


def parse_stage_3_to_4(text: str) -> Stage3To4Handoff:
    """Parse an implementation → verification handoff markdown document.

    Raises :class:`HandoffParseError` with a section-pointing message when
    required sections are missing or malformed, so the service layer can
    feed that message back into a single retry attempt before giving up.
    """
    doc = parse_markdown_doc(text)
    if not doc.title:
        raise HandoffParseError("missing H1 title with workflow id")

    workflow_id = _parse_workflow_id_from_title(doc.title)

    task = _require_section(doc, "Task").strip()
    plan_body = _require_section(doc, "Approved plan")
    plan, plan_title = _parse_approved_plan(plan_body)
    impl = _require_section(doc, "What was implemented").strip()

    files = _parse_file_change_list(_require_section(doc, "Files changed"))
    if not files:
        raise HandoffParseError("'## Files changed' must list at least one file")

    tests = _parse_optional_bullet_list(_require_section(doc, "Tests added/modified"))
    out_of_scope = _parse_optional_bullet_list(
        _require_section(doc, "Out of scope / deferred")
    )
    checklist = _parse_checkbox_list(_require_section(doc, "Verification checklist"))
    if not checklist:
        raise HandoffParseError(
            "'## Verification checklist' must contain at least one checkbox item"
        )
    risks = _parse_optional_bullet_list(_require_section(doc, "Risks / scrutiny areas"))
    budget = _parse_output_budget(_require_section(doc, "Output budget for stage 4"))

    return Stage3To4Handoff(
        workflow_id=workflow_id,
        original_task=task,
        selected_plan=plan,
        plan_title=plan_title,
        what_was_implemented=impl,
        files_changed=files,
        tests_added=tests,
        out_of_scope=out_of_scope,
        verification_checklist=checklist,
        risks=risks,
        output_budget_tokens=budget,
    )


def parse_stage_1_to_2(text: str) -> Stage1To2Handoff:
    """Parse a Plan Brief markdown document (analysis → design handoff)."""
    doc = parse_markdown_doc(text)
    if not doc.title:
        raise HandoffParseError("missing H1 title with workflow id")
    workflow_id = _parse_workflow_id_from_title(doc.title)
    task = _require_section(doc, "Task").strip()
    plan, plan_title = _parse_approved_plan(_require_section(doc, "Approved plan"))
    plan_summary = _require_section(doc, "Plan summary").strip()
    rationale = _require_section(doc, "Selection rationale").strip()
    trade_offs = _parse_optional_bullet_list(_require_section(doc, "Trade-offs"))
    hints = _parse_optional_bullet_list(
        _require_section(doc, "Hints for design stage")
    )
    budget = _parse_output_budget(_require_section(doc, "Output budget for stage 2"))
    return Stage1To2Handoff(
        workflow_id=workflow_id,
        original_task=task,
        selected_plan=plan,
        plan_title=plan_title,
        plan_summary=plan_summary,
        selection_rationale=rationale,
        trade_offs=trade_offs,
        hints=hints,
        output_budget_tokens=budget,
    )


def parse_stage_2_to_3(text: str) -> Stage2To3Handoff:
    """Parse a Design Brief markdown document (design → implementation
    handoff).

    Rejects an empty implementation checklist: a design with no concrete
    steps means stage 3 has nothing actionable to do, and the workflow
    should retry stage 2 rather than silently produce nothing.
    """
    doc = parse_markdown_doc(text)
    if not doc.title:
        raise HandoffParseError("missing H1 title with workflow id")
    workflow_id = _parse_workflow_id_from_title(doc.title)
    task = _require_section(doc, "Task").strip()
    plan, plan_title = _parse_approved_plan(_require_section(doc, "Approved plan"))
    arch = _parse_bullet_list(_require_section(doc, "Architecture decisions"))
    files = _parse_file_change_list(
        _require_section(doc, "Files to modify / create")
    )
    if not files:
        raise HandoffParseError(
            "'## Files to modify / create' must list at least one file"
        )
    checklist = _parse_ordered_list(
        _require_section(doc, "Implementation checklist (in order)")
    )
    if not checklist:
        raise HandoffParseError(
            "'## Implementation checklist (in order)' must contain at least one "
            "numbered step"
        )
    test_strategy = _parse_bullet_list(_require_section(doc, "Test strategy"))
    acceptance = _parse_checkbox_list(_require_section(doc, "Acceptance criteria"))
    if not acceptance:
        raise HandoffParseError(
            "'## Acceptance criteria' must contain at least one checkbox item"
        )
    risks = _parse_optional_bullet_list(_require_section(doc, "Risks / open questions"))
    budget = _parse_output_budget(_require_section(doc, "Output budget for stage 3"))
    return Stage2To3Handoff(
        workflow_id=workflow_id,
        original_task=task,
        selected_plan=plan,
        plan_title=plan_title,
        architecture_decisions=arch,
        files_to_modify=files,
        implementation_checklist=checklist,
        test_strategy=test_strategy,
        acceptance_criteria=acceptance,
        risks=risks,
        output_budget_tokens=budget,
    )


def parse_final_summary(text: str) -> FinalSummary:
    """Parse a Workflow Result markdown document (stage 4 → user summary)."""
    doc = parse_markdown_doc(text)
    if not doc.title:
        raise HandoffParseError("missing H1 title with workflow id")
    workflow_id = _parse_workflow_id_from_title(doc.title)
    asked = _require_section(doc, "What was asked").strip()
    done = _require_section(doc, "What was done").strip()
    verification = _parse_bullet_list(_require_section(doc, "Verification results"))
    if not verification:
        raise HandoffParseError(
            "'## Verification results' must contain at least one bullet"
        )
    files = _parse_optional_bullet_list(_require_section(doc, "Files changed"))
    caveats = _parse_optional_bullet_list(
        _require_section(doc, "Caveats / follow-ups")
    )
    return FinalSummary(
        workflow_id=workflow_id,
        what_was_asked=asked,
        what_was_done=done,
        verification_results=verification,
        files_changed=files,
        caveats=caveats,
    )


__all__ = [
    "FileChange",
    "FinalSummary",
    "HandoffParseError",
    "MarkdownDoc",
    "Stage1To2Handoff",
    "Stage2To3Handoff",
    "Stage3To4Handoff",
    "parse_final_summary",
    "parse_markdown_doc",
    "parse_stage_1_to_2",
    "parse_stage_2_to_3",
    "parse_stage_3_to_4",
]
