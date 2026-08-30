from __future__ import annotations

from dataclasses import dataclass

PHASE4_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class Phase4QuestionSpec:
    section: str
    question_id: str
    text: str


PHASE4_QUESTIONS: tuple[Phase4QuestionSpec, ...] = (
    Phase4QuestionSpec("contribution_identity", "identity.what", "What was the contribution?"),
    Phase4QuestionSpec(
        "contribution_identity", "identity.app_flow", "Which app and business flow?"
    ),
    Phase4QuestionSpec("contribution_identity", "identity.when", "When did it occur?"),
    Phase4QuestionSpec(
        "contribution_identity",
        "identity.origin",
        "Assigned, proposed, inherited, or discovered?",
    ),
    Phase4QuestionSpec(
        "contribution_identity",
        "identity.ownership",
        "Sole, main, or contributing role?",
    ),
    Phase4QuestionSpec("problem_context", "problem.what", "What problem existed?"),
    Phase4QuestionSpec("problem_context", "problem.before", "What happened before the change?"),
    Phase4QuestionSpec("problem_context", "problem.severity", "How serious was it?"),
    Phase4QuestionSpec("problem_context", "problem.affected", "Who or what was affected?"),
    Phase4QuestionSpec("problem_context", "problem.blocked", "What did it block?"),
    Phase4QuestionSpec("problem_context", "problem.constraints", "What constraints existed?"),
    Phase4QuestionSpec("problem_context", "problem.ambiguity", "Was the requirement unclear?"),
    Phase4QuestionSpec("action", "action.implemented", "What did the user implement?"),
    Phase4QuestionSpec(
        "action", "action.decisions", "Which technical decisions did the user make?"
    ),
    Phase4QuestionSpec("action", "action.technology", "Which tools or frameworks were involved?"),
    Phase4QuestionSpec("action", "action.reuse", "Was a reusable component produced?"),
    Phase4QuestionSpec("action", "action.architecture", "Which layers or data flow changed?"),
    Phase4QuestionSpec("action", "action.coordination", "What coordination occurred?"),
    Phase4QuestionSpec("action", "action.quality", "What tests, docs, or monitoring changed?"),
    Phase4QuestionSpec("action", "action.review", "Did the user review others?"),
    Phase4QuestionSpec("result", "result.change", "What changed?"),
    Phase4QuestionSpec("result", "result.measurement", "Is there a measurable before/after?"),
    Phase4QuestionSpec(
        "result",
        "result.scope",
        "Which modules, screens, app, or market were affected?",
    ),
    Phase4QuestionSpec("result", "result.errors_time", "Were errors or time reduced?"),
    Phase4QuestionSpec(
        "result",
        "result.business",
        "Was conversion, stability, or another outcome improved?",
    ),
    Phase4QuestionSpec("result", "result.release", "How far did delivery progress?"),
    Phase4QuestionSpec("result", "result.current_use", "Is it still used or enabled?"),
    Phase4QuestionSpec("result", "result.reuse", "Was it reused later?"),
    Phase4QuestionSpec("result", "result.feedback", "Was feedback recorded?"),
    Phase4QuestionSpec(
        "result",
        "result.defensibility",
        "Which parts are defensible in an interview?",
    ),
)
