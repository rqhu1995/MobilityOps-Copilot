"""Untrusted experiment-plan proposals and review-bound execution requests."""

from typing import Literal

from pydantic import Field, JsonValue

from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.analysis import AnalysisModel, SelectionDecision
from mobilityops.domain.experiment import ExperimentBatch, ExperimentPlan, ExperimentPrecheck, ExperimentReport
from mobilityops.domain.intake import Clarification, ScenarioField


PlanField = Literal[
    "backend", "repetitions", "external_timeout_sec", "max_calls",
    "external_wait_budget_sec", "failure_policy",
]
CaseField = ScenarioField | Literal["repetitions", "external_timeout_sec"]
ExperimentField = PlanField | CaseField


class ProposedExperimentCase(AnalysisModel):
    role: Literal["baseline", "variant"]
    case_id: str = Field(min_length=1, max_length=80)
    message_index: int = Field(ge=0, strict=True)
    evidence: str = Field(min_length=1, max_length=4000)


class ProposedExperimentField(AnalysisModel):
    target: str = Field(min_length=1, max_length=80)
    field: ExperimentField
    value: JsonValue
    message_index: int = Field(ge=0, strict=True)
    evidence: str = Field(min_length=1, max_length=4000)


class ExperimentInterpretationIssue(AnalysisModel):
    target: str | None = Field(default=None, max_length=80)
    field: str | None = Field(default=None, max_length=80)
    question: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)
    message_index: int = Field(ge=0, strict=True)


class ExperimentExtraction(AnalysisModel):
    """The complete model output; it cannot contain execution actions."""

    cases: tuple[ProposedExperimentCase, ...] = Field(max_length=40)
    fields: tuple[ProposedExperimentField, ...] = Field(max_length=240)
    issues: tuple[ExperimentInterpretationIssue, ...] = Field(max_length=80)


class ExperimentLanguageInput(AnalysisModel):
    messages: tuple[str, ...] = Field(min_length=1, max_length=20)
    context: ScenarioSpec | None = None
    context_backend: BackendName | None = None


class ExperimentPlanDraft(AnalysisModel):
    format: Literal["experiment_plan_draft_v1"] = "experiment_plan_draft_v1"
    draft_sha256: str
    input: ExperimentLanguageInput
    extraction: ExperimentExtraction
    values: dict[str, JsonValue]
    field_sources: dict[str, Literal["context", "inherited", "user_text"]]
    plan: ExperimentPlan | None
    decisions: dict[str, SelectionDecision]
    clarifications: tuple[Clarification, ...]
    status: Literal["needs_clarification", "incompatible", "ready"]


class ExperimentPlanExecutionRequest(AnalysisModel):
    format: Literal["language_experiment_execution_request_v1"] = "language_experiment_execution_request_v1"
    request_sha256: str
    draft_sha256: str
    experiment_id: str
    report_id: str
    plan: ExperimentPlan
    precheck: ExperimentPrecheck
    execution_configurations: dict[str, dict[str, JsonValue]]


class ExperimentPlanExecution(AnalysisModel):
    request: ExperimentPlanExecutionRequest
    batch: ExperimentBatch
    report: ExperimentReport
