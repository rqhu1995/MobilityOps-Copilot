"""Bounded Gemini decisions over deterministic MobilityOps tools."""

from typing import Literal

from pydantic import Field, JsonValue

from mobilityops.domain.analysis import AnalysisModel


CopilotAction = Literal[
    "draft_experiment",
    "explain_experiment",
    "propose_next_experiment",
    "prepare_execution_review",
    "answer",
    "clarify",
]


class CopilotEvidenceItem(AnalysisModel):
    evidence_id: str = Field(pattern=r"^evidence-[0-9]{3}$")
    kind: Literal[
        "plan_draft",
        "decision_explanation",
        "next_experiment_proposal",
        "execution_review",
        "execution_report",
    ]
    payload: dict[str, JsonValue]


class CopilotStateView(AnalysisModel):
    remaining_model_calls: int = Field(default=0, ge=0)
    draft_status: Literal["needs_clarification", "incompatible", "ready"] | None = None
    draft_sha256: str | None = None
    has_complete_plan: bool = False
    current_experiment_id: str | None = None
    selected_variant_case_id: str | None = None
    proposal_ready: bool = False
    execution_review_ready: bool = False
    execution_completed: bool = False
    plan_summary: dict[str, JsonValue] | None = None
    clarifications: tuple[str, ...] = Field(default=(), max_length=80)


class CopilotModelInput(AnalysisModel):
    messages: tuple[str, ...] = Field(min_length=1, max_length=20)
    state: CopilotStateView
    available_actions: tuple[CopilotAction, ...] = Field(min_length=1)
    evidence: tuple[CopilotEvidenceItem, ...] = Field(default=(), max_length=20)


class CopilotDecision(AnalysisModel):
    """A proposal to invoke one local tool; never an execution authorization."""

    format: Literal["gemini_copilot_decision_v1"] = "gemini_copilot_decision_v1"
    action: CopilotAction
    response: str = Field(min_length=1, max_length=4000)
    variant_case_id: str | None = Field(default=None, max_length=40)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
