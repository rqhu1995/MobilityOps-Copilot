"""Evidence-only contracts for Gemini Decision Copilot v2."""

from typing import Literal

from pydantic import Field, JsonValue

from mobilityops.domain.analysis import AnalysisModel


class DecisionCopilotEvidence(AnalysisModel):
    evidence_id: str = Field(pattern=r"^evidence-[0-9]{3}$")
    kind: Literal["lineage", "gate", "fact", "observation", "limitation"]
    payload: dict[str, JsonValue]


class DecisionCopilotInput(AnalysisModel):
    format: Literal["decision_copilot_v2_input"] = "decision_copilot_v2_input"
    dossier_id: str
    dossier_sha256: str
    deterministic_recommendation: Literal[
        "review_blockers", "collect_replicates", "human_decision_review"
    ]
    selected_variant_case_id: str
    evidence: tuple[DecisionCopilotEvidence, ...] = Field(max_length=80)


class DecisionBrief(AnalysisModel):
    format: Literal["decision_brief_v1"] = "decision_brief_v1"
    outcome: Literal[
        "adopt_variant",
        "retain_baseline",
        "collect_more_evidence",
        "review_blockers",
    ]
    executive_summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    risks: tuple[str, ...] = Field(default=(), max_length=20)
    cannot_conclude: tuple[str, ...] = Field(default=(), max_length=20)
    human_approval_required: Literal[True] = True
    auto_execute: Literal[False] = False


class DecisionBriefReviewInput(AnalysisModel):
    source: DecisionCopilotInput
    draft: DecisionBrief


class DecisionBriefReview(AnalysisModel):
    format: Literal["decision_brief_review_v1"] = "decision_brief_review_v1"
    verdict: Literal["approved", "rejected"]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    unsupported_claims: tuple[str, ...] = Field(default=(), max_length=20)
    human_approval_required: Literal[True] = True
    auto_execute: Literal[False] = False


class DecisionCopilotRequest(AnalysisModel):
    format: Literal["decision_copilot_v2_request"] = "decision_copilot_v2_request"
    request_sha256: str
    session_id: str
    model: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    budget_hkd: float
    max_calls: Literal[4] = 4
    source: DecisionCopilotInput


class DecisionCopilotReport(AnalysisModel):
    format: Literal["decision_copilot_v2_report"] = "decision_copilot_v2_report"
    session_id: str
    request_sha256: str
    dossier_id: str
    dossier_sha256: str
    model: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    calls_invoked: Literal[2] = 2
    brief_sha256: str
    review_sha256: str
    status: Literal["approved", "review_rejected"]
    brief: DecisionBrief
    review: DecisionBriefReview
