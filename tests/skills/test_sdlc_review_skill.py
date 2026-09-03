"""Contract tests for the bundled SDLC review skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "devops"
    / "sdlc-review"
    / "SKILL.md"
)
CONTRACT_FIRST_REFERENCE = SKILL_MD.parent / "references" / "contract-first-review.md"
REQUIRED_SECTIONS = [
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Review Lenses",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
]
REVIEW_ACTIONS = {
    "kanban_show",
    "kanban_comment",
    "kanban_complete",
    "kanban_request_changes",
    "kanban_block",
}


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    assert match, f"missing frontmatter field: {key}"
    return match.group(1).strip()


def test_frontmatter_meets_hardline_standard(skill_text: str) -> None:
    assert skill_text.startswith("---\n")
    assert _frontmatter_value(skill_text, "name") == "sdlc-review"

    description = _frontmatter_value(skill_text, "description")
    assert len(description) <= 60
    assert description.endswith(".")

    for field in ("version", "author", "license", "platforms"):
        assert _frontmatter_value(skill_text, field)
    assert not _frontmatter_value(skill_text, "author").startswith("Hermes Agent")


def test_body_uses_required_modern_section_order(skill_text: str) -> None:
    assert "# SDLC Review Skill" in skill_text
    positions = [skill_text.index(section) for section in REQUIRED_SECTIONS]
    assert positions == sorted(positions)


@pytest.mark.parametrize("tool_name", sorted(REVIEW_ACTIONS))
def test_skill_documents_native_review_actions(
    skill_text: str,
    tool_name: str,
) -> None:
    assert f"`{tool_name}`" in skill_text


def test_verdicts_route_through_distinct_terminal_actions(skill_text: str) -> None:
    quick_reference = skill_text.split("## Quick Reference", 1)[1].split(
        "## Review Lenses", 1
    )[0]
    assert "Approve" in quick_reference and "`kanban_complete`" in quick_reference
    assert "Request changes" in quick_reference
    assert "`kanban_request_changes`" in quick_reference
    assert "Escalate" in quick_reference and "`kanban_block`" in quick_reference


def test_review_stages_bound_full_review_to_one_closure(skill_text: str) -> None:
    lenses = " ".join(
        skill_text.split("## Review Lenses", 1)[1]
        .split("## Procedure", 1)[0]
        .split()
    )
    assert "`changes_requested`" in lenses
    assert "Prior attempts on this task" in lenses
    for stage in ("Independent review", "Focused closure", "Architecture stop"):
        assert stage in lenses
    assert "`terminal`" in lenses
    assert "`delegate_task`" in lenses
    assert "sole independent reviewer" in lenses
    assert "changes an operation contract" in lenses
    assert "two permitted full-review rounds" in lenses
    assert "new architecture task or issue" in lenses


def test_skill_routes_same_card_and_downstream_review_work(skill_text: str) -> None:
    when_to_use = skill_text.split("## When to Use", 1)[1].split(
        "## Prerequisites", 1
    )[0]
    assert "task claimed from the `review` lane" in when_to_use
    assert "downstream review card" in when_to_use
    assert "Do not use it for a separate downstream review card" not in when_to_use


def test_contract_first_reference_is_mandatory_and_routed(skill_text: str) -> None:
    assert "**REQUIRED METHOD:**" in skill_text
    assert "`references/contract-first-review.md`" in skill_text
    assert CONTRACT_FIRST_REFERENCE.is_file()


def test_contract_first_reference_covers_review_contract() -> None:
    reference = " ".join(
        CONTRACT_FIRST_REFERENCE.read_text(encoding="utf-8").split()
    )

    assert "3–7 operation invariants" in reference
    assert (
        "producer → validation → authoritative/stored state → consumer → "
        "event/operator-visible outcome"
    ) in reference
    for failure_case in (
        "success",
        "transport exception",
        "typed/returned error",
        "malformed payload",
        "empty result",
        "partial result",
        "timeout/rate limit",
        "crash/reclaim",
        "retry exhaustion",
    ):
        assert failure_case in reference

    for ownership in (
        "retry owner",
        "terminal-state owner",
        "checkpoint owner",
        "exactly-once/external-write owner",
        "operator-visible-outcome owner",
    ):
        assert ownership in reference

    for required_rule in (
        "negative probe",
        "one batch",
        "exact SHA",
        "at most one focused closure",
        "changes an operation contract",
        "HIGH blocks",
        "literal `APPROVED`",
        "architecture stop",
        "path and line",
        "evidence gap",
        "separately from an implementation defect",
        "never produce approval",
        "queue or dispatch",
        "retry and recovery checkpoints",
    ):
        assert required_rule in reference


def test_contract_first_reference_preserves_review_responsibilities() -> None:
    reference = " ".join(
        CONTRACT_FIRST_REFERENCE.read_text(encoding="utf-8").split()
    )

    for responsibility in (
        "The author implements and tests",
        "Open Code Review",
        "at most once",
        "never a verdict",
        "Gauge performs one independent cold-read review",
        "Gauge is the only verdict authority",
        "CI and tests are technical evidence",
        "optional navigation aid",
        "Never treat a graph as a reviewer or verdict source",
        "Never pass a large raw graph",
        "deadline",
        "author seniority",
        "green CI or tests",
        "new architecture task or issue",
    ):
        assert responsibility in reference
