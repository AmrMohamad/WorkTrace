from __future__ import annotations

import re
from pathlib import Path

from worktrace.packets.schema import PHASE4_QUESTIONS, PHASE4_SCHEMA_VERSION

EXPECTED_QUESTIONS = (
    ("contribution_identity", "identity.what", "What was the contribution?"),
    ("contribution_identity", "identity.app_flow", "Which app and business flow?"),
    ("contribution_identity", "identity.when", "When did it occur?"),
    (
        "contribution_identity",
        "identity.origin",
        "Assigned, proposed, inherited, or discovered?",
    ),
    (
        "contribution_identity",
        "identity.ownership",
        "Sole, main, or contributing role?",
    ),
    ("problem_context", "problem.what", "What problem existed?"),
    ("problem_context", "problem.before", "What happened before the change?"),
    ("problem_context", "problem.severity", "How serious was it?"),
    ("problem_context", "problem.affected", "Who or what was affected?"),
    ("problem_context", "problem.blocked", "What did it block?"),
    ("problem_context", "problem.constraints", "What constraints existed?"),
    ("problem_context", "problem.ambiguity", "Was the requirement unclear?"),
    ("action", "action.implemented", "What did the user implement?"),
    (
        "action",
        "action.decisions",
        "Which technical decisions did the user make?",
    ),
    ("action", "action.technology", "Which tools or frameworks were involved?"),
    ("action", "action.reuse", "Was a reusable component produced?"),
    ("action", "action.architecture", "Which layers or data flow changed?"),
    ("action", "action.coordination", "What coordination occurred?"),
    ("action", "action.quality", "What tests, docs, or monitoring changed?"),
    ("action", "action.review", "Did the user review others?"),
    ("result", "result.change", "What changed?"),
    ("result", "result.measurement", "Is there a measurable before/after?"),
    (
        "result",
        "result.scope",
        "Which modules, screens, app, or market were affected?",
    ),
    ("result", "result.errors_time", "Were errors or time reduced?"),
    (
        "result",
        "result.business",
        "Was conversion, stability, or another outcome improved?",
    ),
    ("result", "result.release", "How far did delivery progress?"),
    ("result", "result.current_use", "Is it still used or enabled?"),
    ("result", "result.reuse", "Was it reused later?"),
    ("result", "result.feedback", "Was feedback recorded?"),
    (
        "result",
        "result.defensibility",
        "Which parts are defensible in an interview?",
    ),
)


def test_phase4_v2_schema_has_exact_approved_questions_in_order() -> None:
    assert PHASE4_SCHEMA_VERSION == 2
    assert (
        tuple(
            (question.section, question.question_id, question.text) for question in PHASE4_QUESTIONS
        )
        == EXPECTED_QUESTIONS
    )
    assert len(PHASE4_QUESTIONS) == 30
    assert len({question.question_id for question in PHASE4_QUESTIONS}) == 30
    assert [
        sum(question.section == section for question in PHASE4_QUESTIONS)
        for section in ("contribution_identity", "problem_context", "action", "result")
    ] == [5, 7, 8, 10]


def test_phase4_documentation_matches_runtime_ids_and_wording() -> None:
    documentation = (Path(__file__).resolve().parents[1] / "docs" / "phase4-schema.md").read_text(
        encoding="utf-8"
    )
    question_map = documentation.split("## Question map", 1)[1].split(
        "## Participation summary", 1
    )[0]
    documented = tuple(
        (match.group(1), match.group(2).strip())
        for match in re.finditer(
            r"^\| `((?:identity|problem|action|result)\.[^`]+)` \| ([^|]+) \|",
            question_map,
            flags=re.MULTILINE,
        )
    )

    assert documented == tuple((question_id, text) for _, question_id, text in EXPECTED_QUESTIONS)
